//! 복구 모의훈련: 프로파일의 가장 최근 스냅샷을 임시 폴더에 실제로 복원해 본다.
//! 복원 과정에서 블롭마다 해시를 확인하므로, 통과하면 "지금 이 백업으로 복구가 된다"는 뜻이다.
//! 원본·저장소는 읽기만 하고, 임시 폴더는 끝나면 지운다.
use crate::config::Profile;
use crate::history::History;
use kova_engine::ops::{run_restore, RestoreOptions};
use std::path::Path;
use std::sync::atomic::AtomicBool;

pub fn run(p: &Profile, scratch_root: &Path) -> String {
    let repo = Path::new(&p.repo_dir);
    let snap = match History::open(repo).and_then(|mut h| {
        h.sync_snapshots()?;
        h.list_snapshots()
    }) {
        Ok(mut v) => {
            v.sort_by(|a, b| a.created_at.total_cmp(&b.created_at));
            match v.pop() {
                Some(s) => s,
                None => return format!("{}: 스냅샷이 없어 훈련할 수 없습니다", p.name),
            }
        }
        Err(e) => return format!("{}: 저장소를 읽지 못했습니다 ({e})", p.name),
    };
    let need = (snap.total_bytes.max(0) as u64).saturating_add(snap.total_bytes.max(0) as u64 / 10);
    if let Some(free) = crate::diskspace::free_bytes(scratch_root) {
        if free < need {
            return format!(
                "{}: 임시 공간이 모자라 건너뜁니다 (필요 약 {:.1} GB, 여유 {:.1} GB)",
                p.name,
                need as f64 / 1e9,
                free as f64 / 1e9
            );
        }
    }
    let tmp = scratch_root.join(format!("kova_drill_{}_{}", std::process::id(), snap.id));
    let opts = RestoreOptions {
        repo: repo.to_path_buf(),
        session_id: snap.id.clone(),
        only: None,
        target: Some(tmp.clone()),
    };
    let res = run_restore(&opts, &|_| {}, &AtomicBool::new(false));
    // 이 함수가 만든 임시 폴더만 지운다
    if tmp
        .file_name()
        .is_some_and(|n| n.to_string_lossy().starts_with("kova_drill_"))
    {
        let _ = std::fs::remove_dir_all(&tmp);
    }
    match res {
        Ok(s) => format!(
            "{}: 복구 훈련 통과 — 최근 스냅샷 {}개 항목을 {:.0}초에 복원·해시 확인",
            p.name, s.total_files, s.elapsed_seconds
        ),
        Err(e) => format!("{}: 복구 훈련 실패 — {e}", p.name),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_engine::ops::{run_backup, BackupOptions};

    #[test]
    fn drill_restores_latest_snapshot_cleans_up_and_catches_corruption() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("a.txt"), "hello drill").unwrap();
        let repo = d.path().join("repo");
        let opts = BackupOptions {
            sources: vec![src.clone()],
            repo: repo.clone(),
            session_id: None,
            zstd_level: 3,
            exclude: vec![],
            profile_id: None,
            scan_from: None,
        };
        run_backup(&opts, &|_| {}, &AtomicBool::new(false)).unwrap();
        let mut p = Profile::new("p", "훈련");
        p.sources = vec![src.to_string_lossy().into()];
        p.repo_dir = repo.to_string_lossy().into();
        let scratch = d.path().join("scratch");
        std::fs::create_dir_all(&scratch).unwrap();
        let ok = run(&p, &scratch);
        assert!(ok.contains("통과"), "{ok}");
        assert_eq!(
            std::fs::read_dir(&scratch).unwrap().count(),
            0,
            "임시 폴더가 남음"
        );
        // 블롭을 망가뜨리면 훈련이 실패해야 한다
        for e in walk(&repo.join("blobs")) {
            std::fs::write(e, b"garbage").unwrap();
        }
        let bad = run(&p, &scratch);
        assert!(bad.contains("실패"), "{bad}");
    }

    fn walk(dir: &Path) -> Vec<std::path::PathBuf> {
        let mut out = vec![];
        for e in std::fs::read_dir(dir).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                out.extend(walk(&p));
            } else {
                out.push(p);
            }
        }
        out
    }
}
