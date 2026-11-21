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

/// 바이트열에서 16진수 64자로 된 토큰(BLAKE3/SHA-256 해시 모양)을 모두 뽑는다.
fn hash_like_tokens(bytes: &[u8]) -> Vec<String> {
    bytes
        .split(|b| !b.is_ascii_hexdigit())
        .filter(|t| t.len() == 64)
        .map(|t| String::from_utf8_lossy(t).to_lowercase())
        .collect()
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
        let name = p
            .file_name()
            .map(|n| n.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        // 격리·백업해 둔 매니페스트(`*.json.corrupt_bak`, `*.json.bak`)가 가리키는 블롭도 지우지 않는다.
        // 손상돼 해석이 안 될 수 있으므로 내용에서 해시 모양(16진 64자)을 모두 찾아 보호한다.
        if name.contains(".json") && name.ends_with("bak") {
            set.extend(hash_like_tokens(&fs::read(&p)?));
            continue;
        }
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
        // 색인에 읽지 못한 줄이 있으면 그 줄의 블롭이 살아 있는지 알 수 없으므로 팩을 지우지 않는다
        let Some((entries, 0)) = pack::read_idx_checked(repo, &id) else {
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
            res.deleted_blobs += entries.len();
            res.freed_bytes += idx_len;
            // 팩 삭제가 실패하면(잠김 등) 공간이 풀린 것으로 세지 않는다. 색인 없는 팩은 아래 정리에서 다시 시도한다
            if crate::worm::remove_protected(&pp).is_ok() {
                res.freed_bytes += meta.len();
            }
        }
    }
    prune_pack_leftovers(repo, min_age_secs, res);
}

/// 색인 없는 팩(삭제 실패·중단된 쓰기의 잔재)과 `*.pack.tmp`/`*.idx.tmp` 임시 파일 중 오래된 것을 지운다.
/// 쓰는 중인 팩은 min_age 동안 보호된다(팩을 먼저 놓고 색인을 나중에 놓기 때문).
fn prune_pack_leftovers(repo: &Path, min_age_secs: u64, res: &mut GcResult) {
    use kova_engine::pack;
    let now = SystemTime::now();
    let Ok(rd) = fs::read_dir(pack::packs_dir(repo)) else {
        return;
    };
    for e in rd.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        let leftover = if name.ends_with(".pack.tmp") || name.ends_with(".idx.tmp") {
            true
        } else if let Some(id) = name.strip_suffix(".pack") {
            !pack::idx_file(repo, id).exists()
        } else {
            false
        };
        if !leftover {
            continue;
        }
        let Ok(meta) = e.metadata() else {
            continue;
        };
        // 복사로 놓인 팩은 수정 시각이 원본 그대로일 수 있으므로 만든 시각도 함께 본다
        let young = [meta.modified(), meta.created()].into_iter().any(|t| {
            t.ok()
                .and_then(|m| now.duration_since(m).ok())
                .is_none_or(|age| age < Duration::from_secs(min_age_secs))
        });
        if young {
            continue;
        }
        if crate::worm::remove_protected(&p).is_ok() {
            res.freed_bytes += meta.len();
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

    /// 격리된 매니페스트(`.json.corrupt_bak`, 해석 불가여도)와 `.json.bak` 이 가리키는 블롭은 고아로 지우지 않는다.
    #[test]
    fn quarantined_manifest_references_are_protected() {
        let (a, b, c) = ("a".repeat(64), "b".repeat(64), "c".repeat(64));
        let d = repo_with(&[&a, &b, &c], &[]);
        fs::write(
            d.path().join("snapshots/s0.json.corrupt_bak"),
            format!(r#"{{"id":"s0","files":[{{"chunks":[{{"hash":"{a}"}}] 깨짐"#),
        )
        .unwrap();
        fs::write(
            d.path().join("snapshots/s9.json.bak"),
            format!(r#"{{"files":[{{"chunks":[{{"hash":"{b}"}}]}}]}}"#),
        )
        .unwrap();
        let r = prune_orphans(d.path(), 0).unwrap();
        assert_eq!(r.deleted_blobs, 1);
        assert!(d.path().join(format!("blobs/aa/{a}.blob")).exists());
        assert!(d.path().join(format!("blobs/bb/{b}.blob")).exists());
        assert!(!d.path().join(format!("blobs/cc/{c}.blob")).exists());
    }

    /// 색인에 읽지 못한 줄이 있는 팩은(살아 있는 블롭일 수 있어) 지우지 않고,
    /// 색인 없는 오래된 팩과 `*.pack.tmp` 잔재는 정리한다.
    #[test]
    fn packs_with_bad_index_lines_are_kept_and_leftovers_are_cleaned() {
        use kova_engine::pack;
        let d = repo_with(&[], &["aa01"]);
        let id = pack::write_pack(d.path(), &[("cc03".into(), b"x".to_vec())]).unwrap();
        let ip = pack::idx_file(d.path(), &id);
        let text = fs::read_to_string(&ip).unwrap();
        fs::write(&ip, format!("{text}aa01 garbage\n")).unwrap();
        let orphan = pack::packs_dir(d.path()).join("porphan.pack");
        let tmp = pack::packs_dir(d.path()).join("ptmp.pack.tmp");
        fs::write(&orphan, b"zz").unwrap();
        fs::write(&tmp, b"zz").unwrap();
        // 갓 만든 잔재는 보호
        prune_orphans(d.path(), 3600).unwrap();
        assert!(orphan.exists() && tmp.exists());
        prune_orphans(d.path(), 0).unwrap();
        assert!(
            pack::pack_file(d.path(), &id).exists(),
            "잘못된 줄이 있는 팩이 지워짐"
        );
        assert!(!orphan.exists() && !tmp.exists());
    }

    #[test]
    fn unreadable_manifest_deletes_nothing() {
        let d = repo_with(&["aa01", "bb02"], &["aa01"]);
        fs::write(d.path().join("snapshots/broken.json"), "{{{").unwrap();
        assert!(prune_orphans(d.path(), 0).is_err());
        assert!(d.path().join("blobs/bb/bb02.blob").exists());
    }
}
