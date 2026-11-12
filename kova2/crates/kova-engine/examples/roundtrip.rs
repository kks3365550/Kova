//! 백업→복원 왕복 시험용. 사용: roundtrip backup <repo> <src>... | roundtrip restore <repo> <session> [target]
use kova_engine::ops::*;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let cancel = AtomicBool::new(false);
    match a[0].as_str() {
        "backup" => {
            let o = BackupOptions {
                sources: a[2..].iter().map(PathBuf::from).collect(),
                repo: a[1].clone().into(),
                session_id: None,
                zstd_level: 3,
                exclude: vec![],
                profile_id: Some("rt".into()),
                scan_from: None,
            };
            let s = run_backup(&o, &|_| {}, &cancel).unwrap();
            println!(
                "session={} files={} bytes={} skipped={:?}",
                s.session_id, s.total_files, s.total_bytes, s.skipped_files
            );
        }
        "restore" => {
            let o = RestoreOptions {
                repo: a[1].clone().into(),
                session_id: a[2].clone(),
                only: None,
                target: a.get(3).map(PathBuf::from),
            };
            match run_restore(&o, &|_| {}, &cancel) {
                Ok(s) => println!("restored files={}", s.total_files),
                Err(e) => {
                    println!("RESTORE FAIL {e}");
                    std::process::exit(1);
                }
            }
        }
        _ => panic!("backup|restore"),
    }
}
