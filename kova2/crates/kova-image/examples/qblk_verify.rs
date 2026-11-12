//! 사용: qblk_verify <image.qblk>  — 읽기 전용으로 전체 검증(청크 CRC·클러스터 합계)하고 진행·결과를 출력한다.
use kova_image::{verify, QblkError};
use std::fs::File;
use std::io::BufReader;
use std::sync::atomic::AtomicBool;
use std::time::Instant;

fn main() {
    let path = std::env::args().nth(1).expect("경로가 필요합니다");
    let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    let f = File::open(&path).expect("열기 실패(읽기 전용)");
    let t = Instant::now();
    let cancel = AtomicBool::new(false);
    let res = verify(BufReader::with_capacity(8 << 20, f), &cancel, &|s| {
        let secs = t.elapsed().as_secs_f64().max(0.001);
        eprintln!(
            "chunks={} raw={:.2}GB comp={:.2}GB {:.0}MB/s(압축본 기준)",
            s.chunks,
            s.raw_bytes as f64 / 1e9,
            s.compressed_bytes as f64 / 1e9,
            s.compressed_bytes as f64 / 1e6 / secs
        );
    });
    match res {
        Ok((h, s)) => {
            println!("OK file={} size={} header={:?}", path, size, h);
            println!("OK stats={:?} elapsed={:.1}s", s, t.elapsed().as_secs_f64());
        }
        Err(e @ QblkError::Io(_)) | Err(e) => {
            println!("FAIL {e}");
            std::process::exit(1);
        }
    }
}
