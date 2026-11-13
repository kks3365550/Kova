//! 규모 시험(기본 실행에서는 제외): 작은 파일 수천 개 + 큰 파일을 백업·취소·재개·증분·팩 묶기·검증·복원해 원본과 비교한다.
//! 실행: cargo test --release -p kova-core --test stress -- --ignored --nocapture
use kova_engine::ops::{run_backup, run_restore, BackupOptions, RestoreOptions};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

fn rng(seed: u64, n: usize) -> Vec<u8> {
    let mut x = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
    let mut v = Vec::with_capacity(n);
    while v.len() < n {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        v.extend_from_slice(&x.to_le_bytes());
    }
    v.truncate(n);
    v
}

fn make_tree(root: &Path, small: usize, big: usize, big_mb: usize) {
    for i in 0..small {
        let d = root.join(format!("d{:02}", i % 40));
        fs::create_dir_all(&d).unwrap();
        // 일부는 같은 내용(중복 제거 확인), 나머지는 서로 다름
        let seed = if i % 10 == 0 { 7 } else { i as u64 + 100 };
        fs::write(
            d.join(format!("f{i}.bin")),
            rng(seed, 2_000 + (i % 30) * 1_500),
        )
        .unwrap();
    }
    let b = root.join("big");
    fs::create_dir_all(&b).unwrap();
    for i in 0..big {
        fs::write(
            b.join(format!("b{i}.bin")),
            rng(900_000 + i as u64, big_mb << 20),
        )
        .unwrap();
    }
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
    out.sort();
    out
}

fn opts(src: &Path, repo: &Path, id: &str) -> BackupOptions {
    BackupOptions {
        sources: vec![src.to_path_buf()],
        repo: repo.to_path_buf(),
        session_id: Some(id.into()),
        zstd_level: 3,
        exclude: vec![],
        profile_id: None,
        scan_from: None,
    }
}

#[test]
#[ignore]
fn scale_backup_cancel_resume_pack_verify_restore() {
    let d = tempfile::tempdir_in(
        std::env::var_os("KOVA_STRESS_DIR").unwrap_or(std::env::temp_dir().into()),
    )
    .unwrap();
    let src = d.path().join("src");
    let repo = d.path().join("repo");
    let t = Instant::now();
    make_tree(&src, 4000, 8, 32);
    let files = walk(&src);
    println!("데이터 생성 {}개 {:?}", files.len(), t.elapsed());

    // 1) 취소: 시작 직후 취소 → 오류, 스냅샷(매니페스트) 없음
    let flag = AtomicBool::new(false);
    let cancel_repo = d.path().join("repo_cancel");
    std::thread::scope(|s| {
        s.spawn(|| {
            std::thread::sleep(std::time::Duration::from_millis(300));
            flag.store(true, Ordering::Relaxed);
        });
        let r = run_backup(&opts(&src, &cancel_repo, "c1"), &|_| {}, &flag);
        println!("취소 결과: {:?}", r.as_ref().err().map(|e| e.to_string()));
        assert!(
            r.is_err(),
            "취소했는데 성공으로 끝남(300ms 안에 끝났을 수 있음)"
        );
    });
    let snaps = cancel_repo.join("snapshots");
    let n = fs::read_dir(&snaps).map(|r| r.count()).unwrap_or(0);
    assert_eq!(n, 0, "취소된 백업이 스냅샷을 남김");
    // 취소 뒤 같은 저장소로 이어서 백업 성공
    let t = Instant::now();
    run_backup(
        &opts(&src, &cancel_repo, "c2"),
        &|_| {},
        &AtomicBool::new(false),
    )
    .unwrap();
    println!("취소 후 재개 백업 {:?}", t.elapsed());

    // 2) 전체 백업 + 증분
    let t = Instant::now();
    let s1 = run_backup(&opts(&src, &repo, "s1"), &|_| {}, &AtomicBool::new(false)).unwrap();
    println!("전체 백업 {:?}", t.elapsed());
    assert!(
        s1.skipped_files.is_empty(),
        "백업에서 빠진 파일: {:?}",
        s1.skipped_files
    );
    for i in 0..50 {
        fs::write(
            src.join(format!("d00/new{i}.bin")),
            rng(5_000_000 + i, 10_000),
        )
        .unwrap();
    }
    let t = Instant::now();
    run_backup(&opts(&src, &repo, "s2"), &|_| {}, &AtomicBool::new(false)).unwrap();
    println!("증분 백업 {:?}", t.elapsed());

    // 3) 팩 묶기 → 깊은 검증
    let t = Instant::now();
    let rep = kova_core::packer::compact(&repo, 0, kova_core::packer::DEFAULT_PACK_BYTES).unwrap();
    println!("팩 묶기 {rep:?} {:?}", t.elapsed());
    let t = Instant::now();
    assert!(kova_core::verify::verify_repo(&repo, true).unwrap().ok());
    println!("깊은 검증 {:?}", t.elapsed());

    // 4) 복원 후 원본과 바이트 비교(s1 은 새 파일 50개가 없는 시점)
    let out = d.path().join("out");
    let t = Instant::now();
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
    println!("복원 {:?}", t.elapsed());
    let restored = walk(&out);
    let expect: Vec<&PathBuf> = files.iter().collect();
    if restored.len() != expect.len() {
        let miss: Vec<_> = files
            .iter()
            .filter(|f| {
                let rel = f.strip_prefix(&src).unwrap();
                !restored.iter().any(|r| r.ends_with(rel))
            })
            .take(10)
            .collect();
        println!("복원에서 빠진 파일 예: {miss:?}");
        println!("복원 예: {:?}", restored.iter().take(3).collect::<Vec<_>>());
    }
    assert_eq!(restored.len(), expect.len(), "복원 파일 수 불일치");
    let mut bad = 0;
    for f in files.iter() {
        let rel = f.strip_prefix(&src).unwrap();
        let hit = restored.iter().find(|r| r.ends_with(rel));
        match hit {
            Some(r) if fs::read(r).unwrap() == fs::read(f).unwrap() => {}
            _ => bad += 1,
        }
    }
    assert_eq!(bad, 0, "원본과 다른 파일 {bad}개");
    println!("복원 {}개 전부 원본과 일치", restored.len());
}
