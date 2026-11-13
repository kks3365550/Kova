//! 저장소 정리: 낱개로 흩어진 블롭(blobs/xx/*.blob)을 팩 컨테이너(packs/*.pack)로 묶는다.
//! 작은 파일 수십만 개는 NTFS 에서 느리고(스캔·복사·클라우드 동기화), 팩은 파일 수를 크게 줄인다.
//! 순서: 팩 쓰기 → 팩에서 다시 읽어 원본과 바이트 비교 → 그 뒤에야 낱개 블롭 삭제. 어느 단계에서 끊겨도
//! 낱개 블롭이나 팩 중 하나는 항상 완전하다(같은 블롭이 둘 다 있어도 읽기에는 문제없다).
//! 호출자가 저장소 잠금(RepoLock)을 잡고 부른다.
use kova_engine::pack;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

/// 팩 하나의 목표 크기. 이보다 큰 블롭은 낱개로 둔다.
pub const DEFAULT_PACK_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Debug, Default, PartialEq)]
pub struct PackReport {
    pub packed_blobs: usize,
    pub packs: usize,
    pub freed_bytes: u64,
    pub kept_young: usize,
    pub kept_large: usize,
}

fn loose_blobs(repo: &Path) -> Vec<PathBuf> {
    let mut out = vec![];
    let Ok(dirs) = fs::read_dir(repo.join("blobs")) else {
        return out;
    };
    for d in dirs.flatten().filter(|d| d.path().is_dir()) {
        if let Ok(files) = fs::read_dir(d.path()) {
            out.extend(
                files
                    .flatten()
                    .map(|f| f.path())
                    .filter(|p| p.extension().is_some_and(|x| x == "blob")),
            );
        }
    }
    out.sort();
    out
}

struct Item {
    hash: String,
    path: PathBuf,
    bytes: Vec<u8>,
    readonly: bool,
}

fn flush(repo: &Path, batch: &mut Vec<Item>, rep: &mut PackReport) -> io::Result<()> {
    if batch.is_empty() {
        return Ok(());
    }
    let items: Vec<(String, Vec<u8>)> = batch
        .iter()
        .map(|i| (i.hash.clone(), i.bytes.clone()))
        .collect();
    let id = pack::write_pack(repo, &items)?;
    // 팩에서 다시 읽어 원본과 같은지 확인한 뒤에만 낱개 블롭을 지운다
    let verified = pack::read_idx(repo, &id).is_some_and(|entries| {
        entries.len() == batch.len()
            && entries.iter().zip(batch.iter()).all(|((h, e), it)| {
                *h == it.hash && pack::read_entry(repo, e).is_ok_and(|b| b == it.bytes)
            })
    });
    if !verified {
        let _ = fs::remove_file(pack::idx_file(repo, &id));
        let _ = fs::remove_file(pack::pack_file(repo, &id));
        return Err(io::Error::other(
            "팩 검증에 실패해 낱개 블롭을 그대로 둡니다",
        ));
    }
    if batch.iter().any(|i| i.readonly) {
        crate::worm::protect_file(&pack::pack_file(repo, &id), false);
        crate::worm::protect_file(&pack::idx_file(repo, &id), false);
    }
    for it in batch.drain(..) {
        if crate::worm::remove_protected(&it.path).is_ok() {
            rep.packed_blobs += 1;
            rep.freed_bytes += it.bytes.len() as u64;
        }
    }
    rep.packs += 1;
    Ok(())
}

/// `min_age_secs` 보다 오래된 낱개 블롭을 묶는다(방금 만든 블롭은 건드리지 않는다).
pub fn compact(repo: &Path, min_age_secs: u64, target_bytes: u64) -> io::Result<PackReport> {
    let mut rep = PackReport::default();
    let now = SystemTime::now();
    let mut batch: Vec<Item> = vec![];
    let mut batch_bytes = 0u64;
    for p in loose_blobs(repo) {
        let Ok(meta) = fs::metadata(&p) else { continue };
        let young = meta
            .modified()
            .ok()
            .and_then(|m| now.duration_since(m).ok())
            .is_none_or(|age| age < Duration::from_secs(min_age_secs));
        if young {
            rep.kept_young += 1;
            continue;
        }
        if meta.len() > target_bytes {
            rep.kept_large += 1;
            continue;
        }
        let Some(hash) = p.file_stem().map(|s| s.to_string_lossy().to_string()) else {
            continue;
        };
        let Ok(bytes) = fs::read(&p) else { continue };
        batch_bytes += bytes.len() as u64;
        batch.push(Item {
            hash,
            path: p,
            bytes,
            readonly: meta.permissions().readonly(),
        });
        if batch_bytes >= target_bytes {
            flush(repo, &mut batch, &mut rep)?;
            batch_bytes = 0;
        }
    }
    flush(repo, &mut batch, &mut rep)?;
    Ok(rep)
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_engine::ops::{run_backup, run_restore, BackupOptions, RestoreOptions};
    use kova_engine::storage::CasStorage;
    use std::sync::atomic::AtomicBool;

    fn backup(src: &Path, repo: &Path, id: &str) {
        let opts = BackupOptions {
            sources: vec![src.to_path_buf()],
            repo: repo.to_path_buf(),
            session_id: Some(id.into()),
            zstd_level: 3,
            exclude: vec![],
            profile_id: None,
            scan_from: None,
        };
        run_backup(&opts, &|_| {}, &AtomicBool::new(false)).unwrap();
    }

    fn make_repo() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        for i in 0..8 {
            fs::write(
                src.join(format!("f{i}.txt")),
                format!("내용 {i} ").repeat(200),
            )
            .unwrap();
        }
        let repo = d.path().join("repo");
        backup(&src, &repo, "s1");
        loosen(&repo);
        (d, src, repo)
    }

    /// 백업이 쓴 팩을 낱개 블롭으로 풀어 놓는다(정리 도구가 낱개 블롭을 묶는 경우를 시험하기 위해)
    fn loosen(repo: &Path) {
        use kova_engine::pack;
        let cas = CasStorage::new(repo).unwrap();
        for id in pack::pack_ids(repo) {
            for (h, e) in pack::read_idx(repo, &id).unwrap() {
                let data = pack::read_entry(repo, &e).unwrap();
                let p = cas.blob_path(&h);
                fs::create_dir_all(p.parent().unwrap()).unwrap();
                fs::write(p, data).unwrap();
            }
            fs::remove_file(pack::idx_file(repo, &id)).unwrap();
            fs::remove_file(pack::pack_file(repo, &id)).unwrap();
        }
    }

    #[test]
    fn compacting_keeps_verify_restore_and_dedup_working() {
        let (d, src, repo) = make_repo();
        let before = loose_blobs(&repo).len();
        assert!(before >= 8);
        let r = compact(&repo, 0, DEFAULT_PACK_BYTES).unwrap();
        assert_eq!(r.packed_blobs, before);
        assert_eq!(r.packs, 1);
        assert!(loose_blobs(&repo).is_empty());
        // 팩만 남은 저장소도 깊은 검증을 통과한다
        assert!(crate::verify::verify_repo(&repo, true).unwrap().ok());
        // 복원이 원본과 같다
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo: repo.clone(),
                session_id: "s1".into(),
                only: None,
                target: Some(out.clone()),
            },
            &|_| {},
            &AtomicBool::new(false),
        )
        .unwrap();
        let restored: Vec<PathBuf> = walk(&out);
        assert_eq!(restored.len(), 8);
        for f in restored {
            let name = f.file_name().unwrap();
            assert_eq!(fs::read(&f).unwrap(), fs::read(src.join(name)).unwrap());
        }
        // 같은 내용을 다시 백업해도 새 낱개 블롭이 생기지 않는다(팩 안의 블롭으로 중복 제거)
        backup(&src, &repo, "s2");
        assert!(loose_blobs(&repo).is_empty());
        let cas = CasStorage::new(&repo).unwrap();
        assert_eq!(cas.get_all_blob_hashes().len(), before);
    }

    #[test]
    fn young_and_oversized_blobs_stay_loose_and_second_run_is_noop() {
        let (_d, _src, repo) = make_repo();
        let n = loose_blobs(&repo).len();
        let young = compact(&repo, 3600, DEFAULT_PACK_BYTES).unwrap();
        assert_eq!((young.packs, young.kept_young), (0, n));
        let big = compact(&repo, 0, 1).unwrap();
        assert_eq!((big.packs, big.kept_large), (0, n));
        assert_eq!(loose_blobs(&repo).len(), n);
        compact(&repo, 0, DEFAULT_PACK_BYTES).unwrap();
        assert_eq!(
            compact(&repo, 0, DEFAULT_PACK_BYTES).unwrap(),
            PackReport::default()
        );
    }

    #[test]
    fn small_target_makes_several_packs() {
        let (_d, _src, repo) = make_repo();
        let n = loose_blobs(&repo).len();
        let sizes: Vec<u64> = loose_blobs(&repo)
            .iter()
            .map(|p| fs::metadata(p).unwrap().len())
            .collect();
        let target = *sizes.iter().max().unwrap();
        let r = compact(&repo, 0, target).unwrap();
        assert_eq!(r.packed_blobs, n);
        assert!(r.packs > 1, "{r:?}");
        assert!(crate::verify::verify_repo(&repo, true).unwrap().ok());
    }

    fn walk(dir: &Path) -> Vec<PathBuf> {
        let mut out = vec![];
        let mut st = vec![dir.to_path_buf()];
        while let Some(d) = st.pop() {
            for e in fs::read_dir(&d).into_iter().flatten().flatten() {
                let p = e.path();
                if p.is_dir() {
                    st.push(p)
                } else {
                    out.push(p)
                }
            }
        }
        out
    }
}
