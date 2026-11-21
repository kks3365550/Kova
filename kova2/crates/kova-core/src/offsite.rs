//! 오프사이트 복제(폴더/SMB 대상): 저장소의 스냅샷을 다른 경로(UNC 포함)로 복사한다.
//! - 블롭을 먼저 복사하고 매니페스트는 마지막에 복사 → 대상에는 "완전한 스냅샷"만 나타난다.
//! - 파일은 임시 이름으로 쓴 뒤 이름을 바꾸고, 이미 있는 같은 크기의 블롭은 건너뛴다(중단 후 재개).
//! - 취소 플래그를 블롭 단위로 확인한다. 원본 저장소는 읽기만 한다.
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use kova_engine::storage::SnapshotManifest;

#[derive(Debug, Default, PartialEq)]
pub struct ReplicaReport {
    pub snapshots_copied: usize,
    pub snapshots_skipped: usize,
    pub blobs_copied: usize,
    pub blobs_skipped: usize,
    pub bytes_copied: u64,
    /// 원본이 깨져(매니페스트 손상·블롭 누락·잘못된 해시) 복제하지 못하고 건너뛴 스냅샷 수
    pub snapshots_failed: usize,
    /// 건너뛴 스냅샷별 사유(`매니페스트 이름: 사유`)
    pub failures: Vec<String>,
}

impl ReplicaReport {
    /// 건너뛴 스냅샷이 있으면 경고 문구(이 경우 성공으로 표시하지 말 것).
    pub fn warning(&self) -> Option<String> {
        (self.snapshots_failed > 0).then(|| {
            format!(
                "오프사이트 복제 중 원본 스냅샷 {}개를 건너뜀: {}",
                self.snapshots_failed,
                self.failures.join("; ")
            )
        })
    }
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

/// 매니페스트의 해시는 16진수여야 한다(경로 조각으로 쓰이므로 `..`·구분자·다중 바이트 문자를 막는다).
pub(crate) fn valid_hash(h: &str) -> bool {
    h.len() >= 2 && h.bytes().all(|b| b.is_ascii_hexdigit())
}

fn blob_path(root: &Path, hash: &str) -> PathBuf {
    let prefix = if hash.len() >= 2 { &hash[0..2] } else { "misc" };
    root.join("blobs").join(prefix).join(format!("{hash}.blob"))
}

fn copy_atomic_throttled(
    src: &Path,
    dst: &Path,
    cancel: &AtomicBool,
    max_bytes_per_sec: u64,
) -> Result<u64, OffsiteError> {
    if let Some(p) = dst.parent() {
        fs::create_dir_all(p)?;
    }
    let tmp = dst.with_extension("part");
    // 원본이 읽기 전용(랜섬웨어 보호)이면 복사본도 읽기 전용이 된다. 중단된 이전 시도의 .part 가 남아 있으면 덮어쓸 수 없으므로 먼저 치운다
    let _ = crate::worm::remove_protected(&tmp);
    // 직접 복사해 디스크에 내려쓴(sync_all) 뒤에만 이름을 바꾼다(전원 차단 시 잘린 파일이 완성본으로 보이지 않게)
    let mut r = fs::File::open(src)?;
    let mut w = fs::File::create(&tmp)?;
    let mut buf = vec![0u8; 256 * 1024];
    let mut n = 0u64;
    let started = Instant::now();
    loop {
        if cancel.load(Ordering::Relaxed) {
            drop(w);
            let _ = fs::remove_file(&tmp);
            return Err(OffsiteError::Cancelled);
        }
        let read_n = r.read(&mut buf)?;
        if read_n == 0 {
            break;
        }
        w.write_all(&buf[..read_n])?;
        n += read_n as u64;
        if max_bytes_per_sec > 0 {
            let expected = Duration::from_secs_f64(n as f64 / max_bytes_per_sec as f64);
            let elapsed = started.elapsed();
            if expected > elapsed {
                std::thread::sleep((expected - elapsed).min(Duration::from_secs(2)));
            }
        }
    }
    w.flush()?;
    w.sync_all()?;
    drop(w);
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
    replicate_with_limit(repo, dest, cancel, 0)
}

/// `max_bytes_per_sec > 0` 이면 초당 전송 바이트 수를 제한하며 복제한다.
pub fn replicate_with_limit(
    repo: &Path,
    dest: &Path,
    cancel: &AtomicBool,
    max_bytes_per_sec: u64,
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
    // 암호화 표식(crypto.json)을 스냅샷보다 먼저 복제한다. 없으면 복제본만으로는 복호화할 수 없다.
    let marker = repo.join(kova_engine::codec::MARKER);
    if marker.is_file() {
        let dmk = dest.join(kova_engine::codec::MARKER);
        let src_bytes = fs::read(&marker)?;
        match fs::read(&dmk) {
            Ok(b) if b == src_bytes => {}
            Ok(_) => {
                return Err(OffsiteError::Io(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "대상의 암호화 표식이 원본 저장소와 다릅니다(다른 저장소의 복제본): {}",
                        dmk.display()
                    ),
                )))
            }
            Err(_) => {
                rep.bytes_copied +=
                    copy_atomic_throttled(&marker, &dmk, cancel, max_bytes_per_sec)?;
            }
        }
    }
    let packs = kova_engine::pack::load_index(repo);
    let mut done_packs = std::collections::HashSet::new();
    for mp in manifests {
        let name = mp.file_name().unwrap().to_owned();
        let dm = dest.join("snapshots").join(&name);
        if dm.exists() {
            rep.snapshots_skipped += 1;
            continue;
        }
        // 원본 스냅샷 하나가 깨져도(매니페스트 손상·잘못된 해시·블롭 누락) 그 스냅샷만 건너뛰고 나머지는 계속 복제한다
        let fail = |rep: &mut ReplicaReport, why: String| {
            rep.snapshots_failed += 1;
            rep.failures
                .push(format!("{}: {why}", name.to_string_lossy()));
        };
        let m: SnapshotManifest = match fs::File::open(&mp)
            .map_err(|e| e.to_string())
            .and_then(|f| serde_json::from_reader(io::BufReader::new(f)).map_err(|e| e.to_string()))
        {
            Ok(m) => m,
            Err(e) => {
                fail(&mut rep, format!("매니페스트를 읽지 못함({e})"));
                continue;
            }
        };
        let mut hashes = hashes_of(m);
        hashes.retain(|h| !h.is_empty());
        if let Some(bad) = hashes.iter().find(|h| !valid_hash(h)) {
            fail(&mut rep, format!("잘못된 해시 {bad:?}"));
            continue;
        }
        // 누락 블롭을 먼저 확인해, 있으면 이 스냅샷의 어떤 파일도 복사하지 않는다
        if let Some(h) = hashes
            .iter()
            .find(|h| fs::metadata(blob_path(repo, h)).is_err() && !packs.contains_key(*h))
        {
            fail(&mut rep, OffsiteError::MissingBlob(h.clone()).to_string());
            continue;
        }
        let mut seen = std::collections::HashSet::new();
        for h in hashes {
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
                        rep.bytes_copied +=
                            copy_atomic_throttled(&sp, &dp, cancel, max_bytes_per_sec)?;
                        rep.bytes_copied +=
                            copy_atomic_throttled(&si, &di, cancel, max_bytes_per_sec)?;
                        rep.blobs_copied += 1;
                    }
                }
                continue;
            };
            if fs::metadata(&d).is_ok_and(|dm| dm.len() == sm.len()) {
                rep.blobs_skipped += 1;
                continue;
            }
            rep.bytes_copied += copy_atomic_throttled(&s, &d, cancel, max_bytes_per_sec)?;
            rep.blobs_copied += 1;
        }
        copy_atomic_throttled(&mp, &dm, cancel, max_bytes_per_sec)?;
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
    fn throttled_replication_copies_and_verifies() {
        let d = tempfile::tempdir().unwrap();
        let repo = make_repo(d.path(), &"throttle-test-payload".repeat(200));
        let dest = d.path().join("offsite_throttled");
        let no = AtomicBool::new(false);
        let r = replicate_with_limit(&repo, &dest, &no, 10 * 1024 * 1024).unwrap();
        assert_eq!(r.snapshots_copied, 1);
        assert!(r.bytes_copied > 0);
        assert!(crate::verify::verify_repo(&dest, true).unwrap().ok());
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
        let r = replicate(&repo, &dest, &AtomicBool::new(false)).unwrap();
        assert_eq!((r.snapshots_copied, r.snapshots_failed), (0, 1));
        assert!(r.warning().unwrap().contains("원본에 블롭이 없음"));
        assert!(
            !dest.join("snapshots").exists()
                || fs::read_dir(dest.join("snapshots")).unwrap().count() == 0
        );
    }

    #[test]
    fn broken_snapshots_are_skipped_and_reported_while_healthy_ones_replicate() {
        let d = tempfile::tempdir().unwrap();
        let repo = make_repo(d.path(), "건강한 내용");
        let snaps = repo.join("snapshots");
        // 경로 탈출 해시, 누락 블롭(16진 해시), 깨진 JSON
        fs::write(
            snaps.join("00000000_000001.json"),
            r#"{"id":"x1","files":[{"path":"e.txt","size":1,"chunks":[{"hash":"../../evil","offset":0,"length":1,"compressed_length":1}]}]}"#,
        )
        .unwrap();
        fs::write(
            snaps.join("00000000_000002.json"),
            format!(
                r#"{{"id":"x2","files":[{{"path":"m.txt","size":1,"chunks":[{{"hash":"{}","offset":0,"length":1,"compressed_length":1}}]}}]}}"#,
                "ab".repeat(32)
            ),
        )
        .unwrap();
        fs::write(snaps.join("00000000_000003.json"), b"{broken").unwrap();
        // 다중 바이트 문자로 시작하는 해시도 패닉 없이 거부
        fs::write(
            snaps.join("00000000_000004.json"),
            r#"{"id":"x4","files":[{"path":"k.txt","size":1,"chunks":[{"hash":"한글","offset":0,"length":1,"compressed_length":1}]}]}"#,
        )
        .unwrap();
        let dest = d.path().join("offsite");
        let r = replicate(&repo, &dest, &AtomicBool::new(false)).unwrap();
        assert_eq!((r.snapshots_copied, r.snapshots_failed), (1, 4));
        assert_eq!(r.failures.len(), 4);
        assert!(r.warning().is_some());
        let copied: Vec<_> = fs::read_dir(dest.join("snapshots")).unwrap().collect();
        assert_eq!(copied.len(), 1);
        assert!(!d.path().join("evil").exists() && !dest.parent().unwrap().join("evil").exists());
        assert!(crate::verify::verify_repo(&dest, true).unwrap().ok());
    }

    fn walk(p: &Path) -> Vec<PathBuf> {
        let mut v = Vec::new();
        for e in fs::read_dir(p).unwrap().flatten() {
            let q = e.path();
            if q.is_dir() {
                v.extend(walk(&q));
            } else {
                v.push(q);
            }
        }
        v
    }

    #[test]
    fn encrypted_repo_replica_carries_marker_and_restores_from_replica() {
        let _g = crate::repocrypto::TEST_KEY_ENV
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let keys = tempfile::tempdir().unwrap();
        std::env::set_var("KOVA2_KEY_DIR", keys.path());
        crate::repocrypto::install_provider();
        let d = tempfile::tempdir().unwrap();
        let repo = d.path().join("repo");
        crate::repocrypto::enable(&repo)
            .unwrap()
            .expect("새로 켜짐");
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        let secret = "OFFSITE-ENCRYPTED-PAYLOAD".repeat(30);
        fs::write(src.join("a.txt"), &secret).unwrap();
        let opts = BackupOptions {
            sources: vec![src],
            repo: repo.clone(),
            session_id: Some("20260101_000000".into()),
            zstd_level: 3,
            exclude: vec![],
            profile_id: None,
            scan_from: None,
        };
        run_backup(&opts, &|_| {}, &AtomicBool::new(false)).unwrap();
        let dest = d.path().join("offsite");
        let r = replicate(&repo, &dest, &AtomicBool::new(false)).unwrap();
        assert_eq!((r.snapshots_copied, r.snapshots_failed), (1, 0));
        let mk = kova_engine::codec::MARKER;
        assert_eq!(
            fs::read(dest.join(mk)).unwrap(),
            fs::read(repo.join(mk)).unwrap()
        );
        // 복제본에서 복원: 표식이 있어야 키로 복호화된다
        let out = d.path().join("out");
        kova_engine::ops::run_restore(
            &kova_engine::ops::RestoreOptions {
                repo: dest.clone(),
                session_id: "20260101_000000".into(),
                target: Some(out.clone()),
                only: None,
            },
            &|_| {},
            &AtomicBool::new(false),
        )
        .unwrap();
        let f = walk(&out)
            .into_iter()
            .find(|p| p.file_name().is_some_and(|n| n == "a.txt"))
            .expect("복원된 파일");
        assert_eq!(fs::read_to_string(f).unwrap(), secret);
        // 대상에 다른 저장소의 표식이 있으면 섞지 않고 거부
        let other = d.path().join("other");
        fs::create_dir_all(&other).unwrap();
        fs::write(other.join(mk), b"{\"version\":1}").unwrap();
        assert!(matches!(
            replicate(&repo, &other, &AtomicBool::new(false)),
            Err(OffsiteError::Io(_))
        ));
        assert!(!other.join("snapshots").exists());
    }
}
