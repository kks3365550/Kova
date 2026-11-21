//! 구글 드라이브 업로드 실측 도구(수동). 앱에서 먼저 구글에 연결해 둬야 한다.
//!   drive_bench quota
//!   drive_bench synth <작업자수> <파일수> <파일KB> <폴더이름>   (무작위 블롭으로 만든 임시 저장소를 올리고, 끝나면 그 폴더를 휴지통으로)
//!   drive_bench repo <저장소경로> <작업자수> <폴더이름>        (실제 저장소를 올린다. 폴더는 남긴다)
//!   drive_bench clean                                          ("Kova2 Backup" 아래 `_bench_` 로 시작하는 시험 폴더를 휴지통으로)
use kova_core::gdrive::{self, DriveClient, Endpoints};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::time::Instant;

fn config_dir() -> PathBuf {
    PathBuf::from(std::env::var("ProgramData").expect("ProgramData"))
        .join("Kova2")
        .join("config")
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let dir = config_dir();
    let creds = gdrive::load_client(&dir).expect("client");
    let mut c = DriveClient::open(&dir, Endpoints::default(), creds).expect("open");
    match a.get(1).map(|s| s.as_str()) {
        Some("quota") => {
            let (limit, usage) = c.storage_quota().expect("quota");
            println!(
                "limit={} usage={} ({:.1} GB used)",
                limit.map_or("unlimited".into(), |l| format!("{:.1} GB", l as f64 / 1e9)),
                usage,
                usage as f64 / 1e9
            );
        }
        Some("synth") => {
            let workers: usize = a[2].parse().unwrap();
            let n: usize = a[3].parse().unwrap();
            let kb: usize = a[4].parse().unwrap();
            let folder = a[5].clone();
            let tmp = std::env::temp_dir().join(format!("drive_bench_{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&tmp);
            std::fs::create_dir_all(tmp.join("snapshots")).unwrap();
            let mut files = Vec::new();
            let mut seed = 0x9E37_79B9_7F4A_7C15u64 ^ std::process::id() as u64;
            for i in 0..n {
                let h = format!("{:02x}{:062x}", i % 64, ((seed as u128) << 20) | i as u128);
                let d = tmp.join("blobs").join(&h[0..2]);
                std::fs::create_dir_all(&d).unwrap();
                let mut buf = vec![0u8; kb * 1024];
                for ch in buf.chunks_mut(8) {
                    seed ^= seed << 13;
                    seed ^= seed >> 7;
                    seed ^= seed << 17;
                    let b = seed.to_le_bytes();
                    ch.copy_from_slice(&b[..ch.len()]);
                }
                std::fs::write(d.join(format!("{h}.blob")), &buf).unwrap();
                files.push(format!(
                    r#"{{"path":"f{i}.bin","size":{l},"chunks":[{{"hash":"{h}","offset":0,"length":{l},"compressed_length":{l}}}]}}"#,
                    l = buf.len()
                ));
            }
            std::fs::write(
                tmp.join("snapshots").join("20261121_000000.json"),
                format!(r#"{{"id":"20261121_000000","files":[{}]}}"#, files.join(",")),
            )
            .unwrap();
            c.set_workers(workers);
            let path = ["Kova2 Backup", folder.as_str()];
            let t0 = Instant::now();
            let r = gdrive::replicate_to_drive(&tmp, &mut c, &path, &AtomicBool::new(false))
                .expect("replicate");
            let s = t0.elapsed().as_secs_f64();
            println!(
                "workers={workers} files={n} x {kb}KB -> {:.1}s, {:.2} MB/s, {:.1} files/s ({r:?})",
                s,
                r.bytes_uploaded as f64 / 1e6 / s,
                n as f64 / s
            );
            match c.trash_path(&path) {
                Ok(t) => println!("trashed test folder: {t}"),
                Err(e) => eprintln!("trash failed (run `clean` later): {e}"),
            }
            let _ = std::fs::remove_dir_all(&tmp);
        }
        Some("clean") => {
            for n in c.child_folder_names(&["Kova2 Backup"]).expect("list") {
                if n.starts_with("_bench_") {
                    let ok = c.trash_path(&["Kova2 Backup", n.as_str()]);
                    println!("{n}: {ok:?}");
                }
            }
        }
        Some("repo") => {
            let repo = PathBuf::from(&a[2]);
            let workers: usize = a[3].parse().unwrap();
            c.set_workers(workers);
            let path = ["Kova2 Backup", a[4].as_str()];
            let t0 = Instant::now();
            let r = gdrive::replicate_to_drive(&repo, &mut c, &path, &AtomicBool::new(false))
                .expect("replicate");
            let s = t0.elapsed().as_secs_f64();
            println!(
                "workers={workers} -> {:.0}s, {:.2} MB/s ({r:?})",
                s,
                r.bytes_uploaded as f64 / 1e6 / s
            );
        }
        _ => eprintln!(
            "usage: drive_bench quota | clean | synth <workers> <files> <kb> <folder> | repo <repo> <workers> <folder>"
        ),
    }
}
