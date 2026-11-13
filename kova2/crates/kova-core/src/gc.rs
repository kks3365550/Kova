//! 고아 블롭 정리: 어떤 스냅샷도 참조하지 않는 blobs/*/*.blob 만 지운다.
//! 팩 컨테이너는 안의 블롭이 전부 고아일 때만 통째로 지운다(일부만 고아면 그대로 둔다).
//! 갓 만들어진 블롭(진행 중인 백업이 아직 매니페스트를 쓰기 전)은 min_age 동안 보호한다.
use kova_engine::storage::SnapshotManifest;
use std::collections::HashSet;
use std::fs;
use std::io;
use std::path::Path;
use std::time::{Duration, SystemTime};

pub const DEFAULT_MIN_AGE_SECS: u64 = 3600;

#[derive(Debug, Default, PartialEq)]
pub struct GcResult {
    pub deleted_blobs: usize,
    pub freed_bytes: u64,
    pub kept_young: usize,
}

/// 모든 스냅샷이 참조하는 청크 해시 집합. 매니페스트를 하나라도 못 읽으면 오류(그러면 정리하지 않는다).
pub fn referenced_hashes(repo: &Path) -> io::Result<HashSet<String>> {
    let mut set = HashSet::new();
    let dir = repo.join("snapshots");
    if !dir.is_dir() {
        return Ok(set);
    }
    for e in fs::read_dir(&dir)? {
        let p = e?.path();
        if p.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let f = fs::File::open(&p)?;
        let m: SnapshotManifest = serde_json::from_reader(io::BufReader::new(f)).map_err(|e| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{}: {}", p.display(), e),
            )
        })?;
        for fe in m.files() {
            for c in fe.chunks {
                set.insert(c.hash);
            }
            if let Some(b) = fe.blob_id {
                set.insert(b);
            }
            if let Some(s) = fe.sha256 {
                set.insert(s);
            }
        }
    }
    Ok(set)
}

pub fn prune_orphans(repo: &Path, min_age_secs: u64) -> io::Result<GcResult> {
    let live = referenced_hashes(repo)?;
    let mut res = GcResult::default();
    let blobs = repo.join("blobs");
    if !blobs.is_dir() {
        prune_packs(repo, &live, min_age_secs, &mut res);
        return Ok(res);
    }
    let now = SystemTime::now();
    for d in fs::read_dir(&blobs)? {
        let d = d?.path();
        if !d.is_dir() {
            continue;
        }
        for f in fs::read_dir(&d)? {
            let f = f?;
            let p = f.path();
            if p.extension().is_none_or(|x| x != "blob") {
                continue;
            }
            let stem = match p.file_stem() {
                Some(s) => s.to_string_lossy().to_string(),
                None => continue,
            };
            if live.contains(&stem) {
                continue;
            }
            let meta = f.metadata()?;
            let young = meta
                .modified()
                .ok()
                .and_then(|m| now.duration_since(m).ok())
                .is_none_or(|age| age < Duration::from_secs(min_age_secs));
            if young {
                res.kept_young += 1;
                continue;
            }
            let len = meta.len();
            if crate::worm::remove_protected(&p).is_ok() {
                res.deleted_blobs += 1;
                res.freed_bytes += len;
            }
        }
    }
    prune_packs(repo, &live, min_age_secs, &mut res);
    Ok(res)
}

fn prune_packs(repo: &Path, live: &HashSet<String>, min_age_secs: u64, res: &mut GcResult) {
    use kova_engine::pack;
    let now = SystemTime::now();
    for id in pack::pack_ids(repo) {
        let Some(entries) = pack::read_idx(repo, &id) else {
            continue;
        };
        if entries.iter().any(|(h, _)| live.contains(h)) {
            continue;
        }
        let (pp, ip) = (pack::pack_file(repo, &id), pack::idx_file(repo, &id));
        let Ok(meta) = fs::metadata(&pp) else {
            continue;
        };
        let young = meta
            .modified()
            .ok()
            .and_then(|m| now.duration_since(m).ok())
            .is_none_or(|age| age < Duration::from_secs(min_age_secs));
        if young {
            res.kept_young += entries.len();
            continue;
        }
        let idx_len = fs::metadata(&ip).map_or(0, |m| m.len());
        // 색인을 먼저 지워 팩을 무효로 만든 뒤 팩을 지운다
        if crate::worm::remove_protected(&ip).is_ok() {
            let _ = crate::worm::remove_protected(&pp);
            res.deleted_blobs += entries.len();
            res.freed_bytes += meta.len() + idx_len;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn repo_with(blobs: &[&str], referenced: &[&str]) -> tempfile::TempDir {
        let d = tempfile::tempdir().unwrap();
        for h in blobs {
            let dir = d.path().join("blobs").join(&h[..2]);
            fs::create_dir_all(&dir).unwrap();
            fs::write(dir.join(format!("{}.blob", h)), b"x").unwrap();
        }
        fs::create_dir_all(d.path().join("snapshots")).unwrap();
        let chunks: Vec<String> = referenced
            .iter()
            .map(|h| format!(r#"{{"hash":"{}"}}"#, h))
            .collect();
        fs::write(
            d.path().join("snapshots").join("s1.json"),
            format!(
                r#"{{"id":"s1","files":[{{"path":"a","chunks":[{}]}}]}}"#,
                chunks.join(",")
            ),
        )
        .unwrap();
        d
    }

    #[test]
    fn removes_only_unreferenced_old_blobs() {
        let d = repo_with(&["aa01", "bb02", "cc03"], &["aa01"]);
        let r = prune_orphans(d.path(), 0).unwrap();
        assert_eq!(r.deleted_blobs, 2);
        assert!(d.path().join("blobs/aa/aa01.blob").exists());
        assert!(!d.path().join("blobs/bb/bb02.blob").exists());
    }

    #[test]
    fn young_blobs_are_kept() {
        let d = repo_with(&["aa01", "bb02"], &["aa01"]);
        let r = prune_orphans(d.path(), 3600).unwrap();
        assert_eq!(r.deleted_blobs, 0);
        assert_eq!(r.kept_young, 1);
    }

    #[test]
    fn pack_is_removed_only_when_every_blob_in_it_is_orphan() {
        let d = repo_with(&[], &["aa01"]);
        let live_pack = kova_engine::pack::write_pack(
            d.path(),
            &[
                ("aa01".into(), b"x".to_vec()),
                ("bb02".into(), b"y".to_vec()),
            ],
        )
        .unwrap();
        let dead_pack = kova_engine::pack::write_pack(
            d.path(),
            &[
                ("cc03".into(), b"x".to_vec()),
                ("dd04".into(), b"y".to_vec()),
            ],
        )
        .unwrap();
        let young = prune_orphans(d.path(), 3600).unwrap();
        assert_eq!(young.deleted_blobs, 0);
        let r = prune_orphans(d.path(), 0).unwrap();
        assert_eq!(r.deleted_blobs, 2);
        assert!(kova_engine::pack::pack_file(d.path(), &live_pack).exists());
        assert!(!kova_engine::pack::pack_file(d.path(), &dead_pack).exists());
        assert!(!kova_engine::pack::idx_file(d.path(), &dead_pack).exists());
    }

    #[test]
    fn unreadable_manifest_deletes_nothing() {
        let d = repo_with(&["aa01", "bb02"], &["aa01"]);
        fs::write(d.path().join("snapshots/broken.json"), "{{{").unwrap();
        assert!(prune_orphans(d.path(), 0).is_err());
        assert!(d.path().join("blobs/bb/bb02.blob").exists());
    }
}
