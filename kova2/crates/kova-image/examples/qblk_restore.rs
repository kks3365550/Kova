//! 사용: qblk_restore <image.qblk> <새 대상 파일(.img/.raw/.vhd/.vhdx/.bin)> — 이미지를 새 파일로만 복원한다(실제 디스크 대상 거부).
use kova_image::restore::restore_image_to_file;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::time::Instant;

fn main() {
    let mut a = std::env::args().skip(1);
    let (img, out) = (a.next().expect("이미지 경로"), a.next().expect("대상 파일"));
    let t = Instant::now();
    let r = restore_image_to_file(Path::new(&img), Path::new(&out), &AtomicBool::new(false), &|s| {
        eprintln!("chunks={} raw={:.2}GB {:.0}MB/s", s.chunks, s.raw_bytes as f64 / 1e9, s.raw_bytes as f64 / 1e6 / t.elapsed().as_secs_f64().max(0.001));
    });
    match r {
        Ok((h, s)) => println!("OK header={h:?} stats={s:?} elapsed={:.0}s", t.elapsed().as_secs_f64()),
        Err(e) => { println!("FAIL {e}"); std::process::exit(1); }
    }
}
