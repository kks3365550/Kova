//! 이력 DB (core/metadata_db.py 이식). 저장소의 `metadata.db` 는 스냅샷 매니페스트(진실의 원천)의
//! 재생성 가능한 색인이다. 손상되면 격리하고 매니페스트에서 다시 만든다.
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SnapshotRow {
    pub id: String,
    pub created_at: f64,
    pub profile_id: String,
    pub profile_name: String,
    pub total_files: i64,
    pub total_bytes: i64,
    pub dedup_saved_bytes: i64,
    pub sources_json: String,
    pub file_mtime: f64,
    pub is_verified: bool,
    pub verify_timestamp: f64,
    pub verify_error_count: i64,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct SyncReport {
    pub added_or_updated: usize,
    pub removed: usize,
    pub unreadable: usize,
}

pub struct History {
    repo: PathBuf,
    conn: Connection,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS snapshots_meta (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL DEFAULT 0,
    profile_id TEXT NOT NULL DEFAULT '',
    profile_name TEXT NOT NULL DEFAULT '',
    total_files INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    dedup_saved_bytes INTEGER NOT NULL DEFAULT 0,
    sources_json TEXT NOT NULL DEFAULT '[]',
    file_mtime REAL NOT NULL DEFAULT 0,
    is_verified INTEGER NOT NULL DEFAULT 0,
    verify_timestamp REAL NOT NULL DEFAULT 0,
    verify_error_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blobs_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_blobs INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL DEFAULT 0
);";

fn is_healthy(path: &Path) -> bool {
    match Connection::open(path)
        .and_then(|c| c.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0)))
    {
        Ok(s) => s.eq_ignore_ascii_case("ok"),
        Err(rusqlite::Error::SqliteFailure(e, _)) => {
            // 잠금·바쁨은 손상이 아니다 (정상 DB를 격리하지 않는다)
            !matches!(
                e.code,
                rusqlite::ErrorCode::NotADatabase | rusqlite::ErrorCode::DatabaseCorrupt
            )
        }
        Err(_) => false,
    }
}

fn ts_of(v: &Value) -> f64 {
    match v {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => {
            if let Ok(f) = s.parse::<f64>() {
                return f;
            }
            chrono::DateTime::parse_from_rfc3339(s)
                .map(|d| d.timestamp() as f64)
                .or_else(|_| {
                    chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                        .map(|d| d.and_utc().timestamp() as f64)
                })
                .unwrap_or(0.0)
        }
        _ => 0.0,
    }
}

impl History {
    /// 저장소의 metadata.db 를 연다. 손상되었으면 `.corrupt_<ms>` 로 격리하고 새로 만든다.
    pub fn open(repo: &Path) -> rusqlite::Result<Self> {
        let _ = fs::create_dir_all(repo);
        let db = repo.join("metadata.db");
        if db.exists() && !is_healthy(&db) {
            let ms = std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis())
                .unwrap_or(0);
            for ext in ["-wal", "-shm"] {
                let _ = fs::remove_file(repo.join(format!("metadata.db{ext}")));
            }
            let _ = fs::rename(&db, repo.join(format!("metadata.db.corrupt_{ms}")));
        }
        let conn = Connection::open(&db)?;
        conn.busy_timeout(std::time::Duration::from_secs(30))?;
        let _ = conn.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()));
        conn.execute_batch(SCHEMA)?;
        Ok(Self {
            repo: repo.to_path_buf(),
            conn,
        })
    }

    fn row_from_manifest(id: &str, v: &Value, mtime: f64) -> SnapshotRow {
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let n = |k: &str| v.get(k).and_then(Value::as_i64).unwrap_or(0);
        let entries = v
            .get("files")
            .or_else(|| v.get("entries"))
            .and_then(Value::as_array);
        SnapshotRow {
            id: id.to_string(),
            created_at: v.get("created_at").map(ts_of).unwrap_or(mtime),
            profile_id: s("profile_id"),
            profile_name: s("profile_name"),
            total_files: v
                .get("total_files")
                .and_then(Value::as_i64)
                .unwrap_or(entries.map_or(0, |e| e.len() as i64)),
            total_bytes: n("total_bytes"),
            dedup_saved_bytes: n("dedup_bytes").max(n("dedup_saved_bytes")),
            sources_json: v
                .get("sources")
                .map(|x| x.to_string())
                .unwrap_or_else(|| "[]".into()),
            file_mtime: mtime,
            is_verified: false,
            verify_timestamp: 0.0,
            verify_error_count: 0,
        }
    }

    /// snapshots/*.json 과 DB 를 맞춘다. 수정시각이 같은 행은 건너뛰고, 사라진 파일의 행은 지운다.
    /// 읽을 수 없는 매니페스트는 기존 행을 지우지 않는다(일시적 오류로 이력이 사라지면 안 됨).
    pub fn sync_snapshots(&mut self) -> rusqlite::Result<SyncReport> {
        let mut rep = SyncReport::default();
        let mut seen: Vec<String> = vec![];
        let dir = self.repo.join("snapshots");
        let tx = self.conn.transaction()?;
        if let Ok(rd) = fs::read_dir(&dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().is_none_or(|x| x != "json") {
                    continue;
                }
                let id = p.file_stem().unwrap().to_string_lossy().to_string();
                seen.push(id.clone());
                let mtime = e
                    .metadata()
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                    .map_or(0.0, |d| d.as_secs_f64());
                let old: Option<f64> = tx
                    .query_row(
                        "SELECT file_mtime FROM snapshots_meta WHERE id=?1",
                        [&id],
                        |r| r.get(0),
                    )
                    .optional()?;
                if old.is_some_and(|o| (o - mtime).abs() < 1e-6) {
                    continue;
                }
                let parsed = fs::read(&p)
                    .ok()
                    .and_then(|b| serde_json::from_slice::<Value>(&b).ok());
                let Some(v) = parsed else {
                    rep.unreadable += 1;
                    continue;
                };
                let r = Self::row_from_manifest(&id, &v, mtime);
                // 검증 상태는 매니페스트가 바뀌면 무효(다시 검증해야 함)
                tx.execute(
                    "INSERT INTO snapshots_meta (id,created_at,profile_id,profile_name,total_files,total_bytes,dedup_saved_bytes,sources_json,file_mtime)
                     VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
                     ON CONFLICT(id) DO UPDATE SET created_at=?2,profile_id=?3,profile_name=?4,total_files=?5,total_bytes=?6,
                       dedup_saved_bytes=?7,sources_json=?8,file_mtime=?9,is_verified=0,verify_timestamp=0,verify_error_count=0",
                    params![r.id, r.created_at, r.profile_id, r.profile_name, r.total_files, r.total_bytes, r.dedup_saved_bytes, r.sources_json, r.file_mtime],
                )?;
                rep.added_or_updated += 1;
            }
        }
        let known: Vec<String> = {
            let mut st = tx.prepare("SELECT id FROM snapshots_meta")?;
            let v = st.query_map([], |r| r.get(0))?.collect::<Result<_, _>>()?;
            v
        };
        for id in known.into_iter().filter(|k| !seen.contains(k)) {
            tx.execute("DELETE FROM snapshots_meta WHERE id=?1", [id])?;
            rep.removed += 1;
        }
        tx.commit()?;
        Ok(rep)
    }

    fn map(r: &rusqlite::Row) -> rusqlite::Result<SnapshotRow> {
        Ok(SnapshotRow {
            id: r.get(0)?,
            created_at: r.get(1)?,
            profile_id: r.get(2)?,
            profile_name: r.get(3)?,
            total_files: r.get(4)?,
            total_bytes: r.get(5)?,
            dedup_saved_bytes: r.get(6)?,
            sources_json: r.get(7)?,
            file_mtime: r.get(8)?,
            is_verified: r.get::<_, i64>(9)? != 0,
            verify_timestamp: r.get(10)?,
            verify_error_count: r.get(11)?,
        })
    }
    const COLS: &'static str = "id,created_at,profile_id,profile_name,total_files,total_bytes,dedup_saved_bytes,sources_json,file_mtime,is_verified,verify_timestamp,verify_error_count";

    /// 최신순 목록.
    pub fn list_snapshots(&self) -> rusqlite::Result<Vec<SnapshotRow>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {} FROM snapshots_meta ORDER BY created_at DESC, id DESC",
            Self::COLS
        ))?;
        let v = st.query_map([], Self::map)?.collect();
        v
    }

    pub fn get_snapshot(&self, id: &str) -> rusqlite::Result<Option<SnapshotRow>> {
        self.conn
            .query_row(
                &format!("SELECT {} FROM snapshots_meta WHERE id=?1", Self::COLS),
                [id],
                Self::map,
            )
            .optional()
    }

    pub fn set_verification(
        &self,
        id: &str,
        ok: bool,
        errors: i64,
        now: f64,
    ) -> rusqlite::Result<bool> {
        Ok(self.conn.execute(
            "UPDATE snapshots_meta SET is_verified=?2, verify_timestamp=?3, verify_error_count=?4 WHERE id=?1",
            params![id, ok as i64, now, errors],
        )? > 0)
    }

    pub fn delete_snapshot_record(&self, id: &str) -> rusqlite::Result<bool> {
        Ok(self
            .conn
            .execute("DELETE FROM snapshots_meta WHERE id=?1", [id])?
            > 0)
    }

    /// blobs 폴더를 훑어 요약(개수·저장 바이트)을 다시 계산해 저장한다.
    pub fn rebuild_blobs_summary(&self, now: f64) -> rusqlite::Result<(i64, i64)> {
        let (mut n, mut bytes) = (0i64, 0i64);
        let mut stack = vec![self.repo.join("blobs")];
        while let Some(d) = stack.pop() {
            if let Ok(rd) = fs::read_dir(&d) {
                for e in rd.flatten() {
                    let p = e.path();
                    if p.is_dir() {
                        stack.push(p);
                    } else if p.extension().is_some_and(|x| x == "blob") {
                        n += 1;
                        bytes += e.metadata().map_or(0, |m| m.len() as i64);
                    }
                }
            }
        }
        self.conn.execute(
            "INSERT INTO blobs_summary (id,total_blobs,stored_bytes,last_updated) VALUES (1,?1,?2,?3)
             ON CONFLICT(id) DO UPDATE SET total_blobs=?1,stored_bytes=?2,last_updated=?3",
            params![n, bytes, now],
        )?;
        Ok((n, bytes))
    }

    pub fn blobs_summary(&self) -> rusqlite::Result<Option<(i64, i64)>> {
        self.conn
            .query_row(
                "SELECT total_blobs,stored_bytes FROM blobs_summary WHERE id=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
    }

    /// WAL 을 본 파일에 합친다.
    pub fn checkpoint(&self) -> bool {
        self.conn
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
                r.get::<_, i64>(0)
            })
            == Ok(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(d: &Path, id: &str, body: &str) {
        fs::create_dir_all(d.join("snapshots")).unwrap();
        fs::write(d.join("snapshots").join(format!("{id}.json")), body).unwrap();
    }

    #[test]
    fn sync_list_update_and_remove() {
        let d = tempfile::tempdir().unwrap();
        manifest(
            d.path(),
            "s1",
            r#"{"created_at":1000.5,"total_files":2,"total_bytes":50,"profile_name":"메인","sources":["C:\\a"]}"#,
        );
        manifest(
            d.path(),
            "s2",
            r#"{"created_at":"2026-11-01T10:00:00","files":[{"path":"a"},{"path":"b"},{"path":"c"}],"dedup_bytes":7}"#,
        );
        let mut h = History::open(d.path()).unwrap();
        assert_eq!(
            h.sync_snapshots().unwrap(),
            SyncReport {
                added_or_updated: 2,
                removed: 0,
                unreadable: 0
            }
        );
        let l = h.list_snapshots().unwrap();
        assert_eq!(l[0].id, "s2");
        assert_eq!((l[0].total_files, l[0].dedup_saved_bytes), (3, 7));
        assert_eq!(l[1].profile_name, "메인");
        // 변화 없으면 건너뜀
        assert_eq!(h.sync_snapshots().unwrap().added_or_updated, 0);
        // 파일 삭제 → 행 삭제
        fs::remove_file(d.path().join("snapshots/s1.json")).unwrap();
        assert_eq!(h.sync_snapshots().unwrap().removed, 1);
        assert_eq!(h.list_snapshots().unwrap().len(), 1);
    }

    #[test]
    fn unreadable_manifest_keeps_existing_row() {
        let d = tempfile::tempdir().unwrap();
        manifest(d.path(), "s1", r#"{"total_files":1}"#);
        let mut h = History::open(d.path()).unwrap();
        h.sync_snapshots().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(30));
        manifest(d.path(), "s1", "{{{ 깨짐");
        let r = h.sync_snapshots().unwrap();
        assert_eq!((r.unreadable, r.removed), (1, 0));
        assert!(h.get_snapshot("s1").unwrap().is_some());
    }

    #[test]
    fn verification_state_and_reset_on_manifest_change() {
        let d = tempfile::tempdir().unwrap();
        manifest(d.path(), "s1", r#"{"total_files":1}"#);
        let mut h = History::open(d.path()).unwrap();
        h.sync_snapshots().unwrap();
        assert!(h.set_verification("s1", true, 0, 123.0).unwrap());
        assert!(h.get_snapshot("s1").unwrap().unwrap().is_verified);
        std::thread::sleep(std::time::Duration::from_millis(30));
        manifest(d.path(), "s1", r#"{"total_files":2}"#);
        h.sync_snapshots().unwrap();
        let r = h.get_snapshot("s1").unwrap().unwrap();
        assert!(!r.is_verified && r.total_files == 2);
        assert!(!h.set_verification("nope", true, 0, 1.0).unwrap());
    }

    #[test]
    fn corrupt_db_is_quarantined_and_rebuilt() {
        let d = tempfile::tempdir().unwrap();
        manifest(d.path(), "s1", r#"{"total_files":1}"#);
        fs::write(d.path().join("metadata.db"), vec![0x41u8; 4096]).unwrap();
        let mut h = History::open(d.path()).unwrap();
        assert_eq!(h.sync_snapshots().unwrap().added_or_updated, 1);
        let q = fs::read_dir(d.path())
            .unwrap()
            .flatten()
            .any(|e| e.file_name().to_string_lossy().contains(".corrupt_"));
        assert!(q, "손상 DB 는 격리되어야 함");
    }

    #[test]
    fn blobs_summary_counts_blob_files() {
        let d = tempfile::tempdir().unwrap();
        fs::create_dir_all(d.path().join("blobs/ab")).unwrap();
        fs::write(d.path().join("blobs/ab/x.blob"), b"12345").unwrap();
        fs::write(d.path().join("blobs/ab/y.blob.tmp_1"), b"zz").unwrap();
        let h = History::open(d.path()).unwrap();
        assert_eq!(h.rebuild_blobs_summary(1.0).unwrap(), (1, 5));
        assert_eq!(h.blobs_summary().unwrap(), Some((1, 5)));
        assert!(h.checkpoint());
    }
}
