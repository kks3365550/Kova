//! 보존 정책 (core/retention.py 이식).
//! - 프로필별로 그룹을 나눠 세대 수 초과분과 보관 기간 만료분을 고른다.
//! - 그룹의 가장 최신 스냅샷은 절대 삭제하지 않는다. 스냅샷이 1개 이하면 아무것도 지우지 않는다.
//! - 디스크 여유 공간 부족을 이유로 스냅샷을 지우지 않는다(fail-closed).
use chrono::{Local, NaiveDateTime, TimeZone};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub struct SnapshotMeta {
    pub id: String,
    /// 초 단위 유닉스 시각, 해석 불가면 0
    pub created_at: f64,
    pub profile_id: String,
}

#[derive(Debug, Clone, Copy)]
pub struct Policy {
    pub retention_count: Option<usize>,
    pub retention_days: Option<u32>,
}

impl Default for Policy {
    fn default() -> Self {
        Policy {
            retention_count: Some(30),
            retention_days: Some(60),
        }
    }
}

#[derive(Debug, Default, PartialEq)]
pub struct RetentionResult {
    pub deleted_snapshots: Vec<String>,
    pub deleted_blobs: usize,
    pub freed_bytes: u64,
    pub status: String,
}

/// created_at 값(숫자, 숫자 문자열, ISO 8601 문자열)을 초 단위 시각으로 바꾼다. 실패하면 0.
pub fn parse_timestamp(v: &serde_json::Value) -> f64 {
    match v {
        serde_json::Value::Number(n) => normalize(n.as_f64().unwrap_or(0.0)),
        serde_json::Value::String(s) => {
            let s = s.trim();
            if s.is_empty() {
                return 0.0;
            }
            if let Ok(f) = s.parse::<f64>() {
                return normalize(f);
            }
            if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(s) {
                return dt.timestamp_millis() as f64 / 1000.0;
            }
            for fmt in [
                "%Y-%m-%dT%H:%M:%S%.f",
                "%Y-%m-%d %H:%M:%S%.f",
                "%Y-%m-%dT%H:%M:%S",
            ] {
                if let Ok(nd) = NaiveDateTime::parse_from_str(s, fmt) {
                    if let Some(dt) = Local.from_local_datetime(&nd).single() {
                        return dt.timestamp_millis() as f64 / 1000.0;
                    }
                }
            }
            0.0
        }
        _ => 0.0,
    }
}

fn normalize(f: f64) -> f64 {
    // 밀리초로 기록된 값은 초로 낮춘다
    if f >= 100_000_000_000.0 {
        f / 1000.0
    } else {
        f
    }
}

/// 순수 선택 로직: 지울 스냅샷 id 목록(정렬됨)을 돌려준다.
pub fn select_expired(snapshots: &[SnapshotMeta], policy: Policy, now: f64) -> Vec<String> {
    if snapshots.len() <= 1 {
        return Vec::new();
    }
    let mut groups: BTreeMap<&str, Vec<&SnapshotMeta>> = BTreeMap::new();
    for s in snapshots {
        groups.entry(s.profile_id.as_str()).or_default().push(s);
    }
    let cutoff = policy
        .retention_days
        .filter(|d| *d > 0)
        .map(|d| now - d as f64 * 86400.0);
    let mut out = std::collections::BTreeSet::new();
    for group in groups.values_mut() {
        group.sort_by(|a, b| {
            b.created_at
                .partial_cmp(&a.created_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if let Some(count) = policy.retention_count.filter(|c| *c > 0) {
            if group.len() > count {
                for s in &group[count..] {
                    out.insert(s.id.clone());
                }
            }
        }
        if let Some(cut) = cutoff {
            for s in group.iter().skip(1) {
                if s.created_at > 0.0 && s.created_at < cut {
                    out.insert(s.id.clone());
                }
            }
        }
        // 그룹의 최신 스냅샷은 항상 보호
        if let Some(first) = group.first() {
            out.remove(&first.id);
        }
    }
    out.into_iter().collect()
}

#[derive(Deserialize)]
struct MetaOnly {
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    created_at: Option<serde_json::Value>,
    #[serde(default)]
    profile_id: Option<serde_json::Value>,
}

/// 저장소의 snapshots/*.json 에서 메타 정보만 읽는다. 하나라도 읽지 못하면 오류(삭제 안전).
pub fn list_snapshots(repo: &Path) -> std::io::Result<Vec<SnapshotMeta>> {
    let dir = repo.join("snapshots");
    let mut out = Vec::new();
    if !dir.is_dir() {
        return Ok(out);
    }
    for e in fs::read_dir(&dir)? {
        let p = e?.path();
        if p.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let f = fs::File::open(&p)?;
        let m: MetaOnly = serde_json::from_reader(std::io::BufReader::new(f)).map_err(|e| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("{}: {}", p.display(), e),
            )
        })?;
        let id = m
            .session_id
            .or(m.id)
            .or_else(|| p.file_stem().map(|s| s.to_string_lossy().to_string()))
            .unwrap_or_default();
        let profile_id = match m.profile_id {
            Some(serde_json::Value::String(s)) => s,
            Some(serde_json::Value::Null) | None => String::new(),
            Some(v) => v.to_string(),
        };
        out.push(SnapshotMeta {
            id,
            created_at: m.created_at.as_ref().map(parse_timestamp).unwrap_or(0.0),
            profile_id,
        });
    }
    Ok(out)
}

/// 보존 정책을 적용한다. 목록을 읽지 못하면 status="list_failed" 로 아무것도 지우지 않는다.
pub fn apply(repo: &Path, policy: Policy) -> RetentionResult {
    apply_inner(repo, policy, None)
}

/// 한 프로파일의 스냅샷에만 정책을 적용한다. 다른 프로파일과 profile_id 가 없는 구형 스냅샷은 건드리지 않는다.
/// (한 저장소를 여러 프로파일이 공유할 때 다른 프로파일의 정책으로 잘못 지우지 않기 위함)
pub fn apply_for_profile(repo: &Path, profile_id: &str, policy: Policy) -> RetentionResult {
    if profile_id.is_empty() {
        return RetentionResult {
            status: "no_profile".into(),
            ..Default::default()
        };
    }
    apply_inner(repo, policy, Some(profile_id))
}

fn apply_inner(repo: &Path, policy: Policy, only: Option<&str>) -> RetentionResult {
    let mut snaps = match list_snapshots(repo) {
        Ok(s) => s,
        Err(e) => {
            return RetentionResult {
                status: format!("list_failed: {}", e),
                ..Default::default()
            };
        }
    };
    if let Some(pid) = only {
        snaps.retain(|s| s.profile_id == pid);
    }
    if snaps.len() <= 1 {
        return RetentionResult {
            status: "safe_minimum".into(),
            ..Default::default()
        };
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let mut res = RetentionResult {
        status: "ok".into(),
        ..Default::default()
    };
    for id in select_expired(&snaps, policy, now) {
        // 경로 조작 방지: id 는 파일 이름 한 조각이어야 한다
        if id.is_empty() || id.contains(['/', '\\']) || id == "." || id == ".." {
            continue;
        }
        if fs::remove_file(repo.join("snapshots").join(format!("{}.json", id))).is_ok() {
            res.deleted_snapshots.push(id);
        }
    }
    if !res.deleted_snapshots.is_empty() {
        if let Ok(g) = crate::gc::prune_orphans(repo, crate::gc::DEFAULT_MIN_AGE_SECS) {
            res.deleted_blobs = g.deleted_blobs;
            res.freed_bytes = g.freed_bytes;
        }
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(id: &str, ts: f64, p: &str) -> SnapshotMeta {
        SnapshotMeta {
            id: id.into(),
            created_at: ts,
            profile_id: p.into(),
        }
    }
    const DAY: f64 = 86400.0;

    #[test]
    fn single_snapshot_is_never_deleted() {
        let v = vec![s("a", 1.0, "")];
        assert!(select_expired(
            &v,
            Policy {
                retention_count: Some(1),
                retention_days: Some(1)
            },
            1e9
        )
        .is_empty());
    }

    #[test]
    fn count_limit_keeps_newest_n() {
        let v: Vec<_> = (0..5)
            .map(|i| s(&format!("s{}", i), 1000.0 + i as f64, ""))
            .collect();
        let del = select_expired(
            &v,
            Policy {
                retention_count: Some(2),
                retention_days: None,
            },
            2000.0,
        );
        assert_eq!(del, vec!["s0", "s1", "s2"]);
    }

    #[test]
    fn age_limit_never_deletes_newest_even_if_old() {
        let now = 1_000_000_000.0;
        let v = vec![
            s("old1", now - 100.0 * DAY, ""),
            s("old2", now - 90.0 * DAY, ""),
        ];
        let del = select_expired(
            &v,
            Policy {
                retention_count: None,
                retention_days: Some(60),
            },
            now,
        );
        assert_eq!(del, vec!["old1"]);
    }

    #[test]
    fn groups_by_profile_are_independent() {
        let now = 1_000_000_000.0;
        let v = vec![
            s("a1", now - 1.0, "A"),
            s("a2", now - 2.0, "A"),
            s("b1", now - 3.0, "B"),
        ];
        let del = select_expired(
            &v,
            Policy {
                retention_count: Some(1),
                retention_days: None,
            },
            now,
        );
        assert_eq!(del, vec!["a2"]);
    }

    #[test]
    fn parse_timestamp_handles_formats() {
        use serde_json::json;
        assert_eq!(parse_timestamp(&json!(1_700_000_000)), 1_700_000_000.0);
        assert_eq!(
            parse_timestamp(&json!(1_700_000_000_000u64)),
            1_700_000_000.0
        );
        assert_eq!(parse_timestamp(&json!("1700000000.5")), 1_700_000_000.5);
        assert_eq!(
            parse_timestamp(&json!("2023-11-14T22:13:20+00:00")),
            1_700_000_000.0
        );
        assert!(parse_timestamp(&json!("2026-11-11T10:00:00")) > 0.0);
        assert_eq!(parse_timestamp(&json!("garbage")), 0.0);
        assert_eq!(parse_timestamp(&json!(null)), 0.0);
    }

    #[test]
    fn unreadable_manifest_aborts_without_deleting() {
        let d = tempfile::tempdir().unwrap();
        let snaps = d.path().join("snapshots");
        fs::create_dir_all(&snaps).unwrap();
        fs::write(snaps.join("good.json"), r#"{"id":"good","created_at":1}"#).unwrap();
        fs::write(snaps.join("bad.json"), "not json").unwrap();
        let r = apply(
            d.path(),
            Policy {
                retention_count: Some(1),
                retention_days: None,
            },
        );
        assert!(r.status.starts_with("list_failed"));
        assert!(snaps.join("good.json").exists() && snaps.join("bad.json").exists());
    }

    #[test]
    fn apply_for_profile_leaves_other_profiles_and_legacy_alone() {
        let d = tempfile::tempdir().unwrap();
        let snap = d.path().join("snapshots");
        fs::create_dir_all(&snap).unwrap();
        for (id, ts, p) in [
            ("a1", 100, "A"),
            ("a2", 200, "A"),
            ("a3", 300, "A"),
            ("b1", 100, "B"),
            ("b2", 200, "B"),
            ("old1", 1, ""),
            ("old2", 2, ""),
        ] {
            let pj = if p.is_empty() {
                String::new()
            } else {
                format!(",\"profile_id\":\"{p}\"")
            };
            fs::write(
                snap.join(format!("{id}.json")),
                format!("{{\"id\":\"{id}\",\"created_at\":{ts}{pj}}}"),
            )
            .unwrap();
        }
        let r = apply_for_profile(
            d.path(),
            "A",
            Policy {
                retention_count: Some(1),
                retention_days: None,
            },
        );
        let mut del = r.deleted_snapshots.clone();
        del.sort();
        assert_eq!(del, vec!["a1", "a2"]);
        for keep in ["a3", "b1", "b2", "old1", "old2"] {
            assert!(
                snap.join(format!("{keep}.json")).exists(),
                "{keep} 가 지워짐"
            );
        }
        assert_eq!(
            apply_for_profile(d.path(), "", Policy::default()).status,
            "no_profile"
        );
    }
}
