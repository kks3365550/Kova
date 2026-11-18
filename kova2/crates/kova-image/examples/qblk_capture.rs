//! 사용: qblk_capture <원본 raw 파일> <새 출력.qblk> [클러스터 바이트(기본 4096)] — 시험용 이미지 생성(원본은 읽기 전용).
use kova_image::capture::capture_file;
use std::path::Path;
use std::sync::atomic::AtomicBool;

fn main() {
    let mut a = std::env::args().skip(1);
    let (src, out) = (a.next().expect("원본"), a.next().expect("출력"));
    let cs: u32 = a.next().map_or(4096, |s| s.parse().expect("클러스터 크기"));
    match capture_file(
        Path::new(&src),
        Path::new(&out),
        cs,
        3,
        "kova-test",
        &AtomicBool::new(false),
        &|_| {},
    ) {
        Ok((h, s)) => println!("OK header={h:?} stats={s:?}"),
        Err(e) => {
            println!("FAIL {e}");
            std::process::exit(1);
        }
    }
}
