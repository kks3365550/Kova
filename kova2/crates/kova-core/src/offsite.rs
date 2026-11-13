//! 오프사이트 복제(폴더/SMB 대상): 저장소의 스냅샷을 다른 경로(UNC 포함)로 복사한다.
//! - 블롭을 먼저 복사하고 매니페스트는 마지막에 복사 → 대상에는 "완전한 스냅샷"만 나타난다.
//! - 파일은 임시 이름으로 쓴 뒤 이름을 바꾸고, 이미 있는 같은 크기의 블롭은 건너뛴다(중단 후 재개).
//! - 취소 플래그를 블롭 단위로 확인한다. 원본 저장소는 읽기만 한다.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use kova_engine::storage::SnapshotManifest;

#[derive(Debug, Default, PartialEq)]
pub struct ReplicaReport {
    pub snapshots_copied: usize,
    pub snapshots_skipped: usize,
    pub blobs_copied: usize,
    pub blobs_skipped: usize,
    pub bytes_copied: u64,
}

#[derive(Debug)]
pub enum OffsiteError {
    Io(io::Error),
    Cancelled,
    /// 원본에 있어야 할 블롭이 없음 (원본 저장소 손상)
    MissingBlob(String),
}
impl std::fmt::Display for OffsiteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "{e}"),
            Self::Cancelled => write!(f, "취소됨"),
            Self::MissingBlob(h) => write!(f, "원본에 블롭이 없음: {h}"),
        }
    }
}
impl From<io::Error> for OffsiteError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

fn blob_path(root: &Path, hash: &str) -> PathBuf {
    let prefix = if hash.len() >= 2 { &hash[0..2] } else { "misc" };
    root.join("blobs").join(prefix).join(format!("{hash}.blob"))
}

fn copy_atomic(src: &Path, dst: &Path) -> io::Result<u64> {
    if let Some(p) = dst.parent() {
        fs::create_dir_all(p)?;
    }
    let tmp = dst.with_extension("part");
    // 원본이 읽기 전용(랜섬웨어 보호)이면 복사본도 읽기 전용이 된다. 중단된 이전 시도의 .part 가 남아 있으면 덮어쓸 수 없으므로 먼저 치운다
    let _ = crate::worm::remove_protected(&tmp);
    let n = fs::copy(src, &tmp)?;
    fs::rename(&tmp, dst)?;
    Ok(n)
}

fn hashes_of(m: SnapshotManifest) -> Vec<String> {
    let mut v = vec![];
    for fe in m.files() {
        v.extend(fe.chunks.into_iter().map(|c| c.hash));
        if let Some(b) = fe.blob_id {
            v.push(b);
        }
        if let Some(s) = fe.sha256 {
            v.push(s);
        }
    }
    v
}

/// repo 의 스냅샷을 dest 로 복제한다. 대상에 이미 있는 스냅샷은 건너뛴다.
pub fn replicate(
    repo: &Path,
    dest: &Path,
    cancel: &AtomicBool,
) -> Result<ReplicaReport, OffsiteError> {
    let mut rep = ReplicaReport::default();
    let snaps = repo.join("snapshots");
    if !snaps.is_dir() {
        return Ok(rep);
    }
    let mut manifests: Vec<PathBuf> = fs::read_dir(&snaps)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    manifests.sort();
    let packs = kova_engine::pack::load_index(repo);
    let mut done_packs = std::collections::HashSet::new();
    for mp in manifests {
        let name = mp.file_name().unwrap().to_owned();
        let dm = dest.join("snapshots").join(&name);
        if dm.exists() {
            rep.snapshots_skipped += 1;
            continue;
        }
        let m: SnapshotManifest = serde_json::from_reader(io::BufReader::new(fs::File::open(&mp)?))
            .map_err(|e| {
                io::Error::new(io::ErrorKind::InvalidData, format!("{}: {e}", mp.display()))
            })?;
        let mut seen = std::collections::HashSet::new();
        for h in hashes_of(m) {
            if !seen.insert(h.clone()) {
                continue;
            }
            if cancel.load(Ordering::Relaxed) {
                return Err(OffsiteError::Cancelled);
            }
            let s = blob_path(repo, &h);
            let d = blob_path(dest, &h);
            let Ok(sm) = fs::metadata(&s) else {
                // 낱개 블롭이 없으면 팩 컨테이너에 있는지 보고, 있으면 팩 전체(.pack 먼저, .idx 나중)를 복사한다
                let Some(e) = packs.get(&h) else {
                    return Err(OffsiteError::MissingBlob(h));
                };
                if done_packs.insert(e.pack.clone()) {
                    use kova_engine::pack::{idx_file, pack_file};
                    let (sp, si) = (pack_file(repo, &e.pack), idx_file(repo, &e.pack));
                    let (dp, di) = (pack_file(dest, &e.pack), idx_file(dest, &e.pack));
                    let have = fs::metadata(&di).is_ok()
                        && fs::metadata(&dp)
                            .is_ok_and(|m| fs::metadata(&sp).is_ok_and(|s| s.len() == m.len()));
                    if have {
                        rep.blobs_skipped += 1;
                    } else {
                        rep.bytes_copied += copy_atomic(&sp, &dp)?;
                        rep.bytes_copied += copy_atomic(&si, &di)?;
                        rep.blobs_copied += 1;
                    }
                }
                continue;
            };
            if fs::metadata(&d).is_ok_and(|dm| dm.len() == sm.len()) {
                rep.blobs_skipped += 1;
                continue;
            }
            rep.bytes_copied += copy_atomic(&s, &d)?;
            rep.blobs_copied += 1;
        }
        copy_atomic(&mp, &dm)?;
        rep.snapshots_copied += 1;
    }
    Ok(rep)
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_engine::ops::{run_backup, BackupOptions};

    fn make_repo(d: &Path, content: &str) -> PathBuf {
        let src = d.join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.txt"), content).unwrap();
        let repo = d.join("repo");
        let opts = BackupOptions {
            sources: vec![src],
            repo: repo.clone(),
            session_id: None,
            zstd_level: 3,
            exclude: vec![],
            profile_id: None,
            scan_from: None,
        };
        run_backup(&opts, &|_| {}, &AtomicBool::new(false)).unwrap();
        repo
    }

    #[test]
    fn replicates_then_skips_and_resumes() {
        let d = tempfile::tempdir().unwrap();
        let repo = make_repo(d.path(), "첫번째");
        let dest = d.path().join("offsite");
        let no = AtomicBool::new(false);
        let r1 = replicate(&repo, &dest, &no).unwrap();
        assert_eq!(r1.snapshots_copied, 1);
        assert!(r1.blobs_copied >= 1);
        // 복제본도 검증을 통과한다
        assert!(crate::verify::verify_repo(&dest, true).unwrap().ok());
        // 두 번째는 전부 건너뜀
        let r2 = replicate(&repo, &dest, &no).unwrap();
        assert_eq!(
            (r2.snapshots_copied, r2.snapshots_skipped, r2.blobs_copied),
            (0, 1, 0)
        );
        // 매니페스트가 지워진 상태(중단 후 재개)에서는 블롭은 건너뛰고 매니페스트만 다시 복사
        for e in fs::read_dir(dest.join("snapshots")).unwrap() {
            fs::remove_file(e.unwrap().path()).unwrap();
        }
        let r3 = replicate(&repo, &dest, &no).unwrap();
        assert_eq!((r3.snapshots_copied, r3.blobs_copied), (1, 0));
        assert!(r3.blobs_skipped >= 1);
    }

    #[test]
    fn packed_blobs_are_replicated_as_whole_packs_and_verify() {
        let d = tempfile::tempdir().unwrap();
        let repo = make_repo(d.path(), "묶인 내용");
        crate::packer::compact(&repo, 0, crate::packer::DEFAULT_PACK_BYTES).unwrap();
        let dest = d.path().join("offsite");
        let no = AtomicBool::new(false);
        let r1 = replicate(&repo, &dest, &no).unwrap();
        assert_eq!((r1.snapshots_copied, r1.blobs_copied), (1, 1));
        assert!(crate::verify::verify_repo(&dest, true).unwrap().ok());
        for e in fs::read_dir(dest.join("snapshots")).unwrap() {
            fs::remove_file(e.unwrap().path()).unwrap();
        }
        let r2 = replicate(&repo, &dest, &no).unwrap();
        assert_eq!(
            (r2.snapshots_copied, r2.blobs_copied, r2.blobs_skipped),
            (1, 0, 1)
        );
    }

    #[test]
    fn cancel_and_missing_blob_never_publish_a_manifest() {
        let d = tempfile::tempdir().unwrap();
        let repo = make_repo(d.path(), "내용");
        let dest = d.path().join("offsite");
        assert!(matches!(
            replicate(&repo, &dest, &AtomicBool::new(true)),
            Err(OffsiteError::Cancelled)
        ));
        assert!(
            !dest.join("snapshots").exists()
                || fs::read_dir(dest.join("snapshots")).unwrap().count() == 0
        );
        // 원본 블롭이 사라지면 오류이고 대상에 스냅샷이 생기지 않는다
        // (백업은 새 블롭을 팩으로 쓰므로 팩 파일을 지운다)
        let pack = fs::read_dir(repo.join("packs"))
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|x| x == "pack"))
            .expect("팩 없음");
        fs::remove_file(pack).unwrap();
        assert!(matches!(
            replicate(&repo, &dest, &AtomicBool::new(false)),
            Err(OffsiteError::MissingBlob(_))
        ));
        assert!(
            !dest.join("snapshots").exists()
                || fs::read_dir(dest.join("snapshots")).unwrap().count() == 0
        );
    }
}
