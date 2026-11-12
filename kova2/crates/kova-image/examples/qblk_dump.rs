//! 사용: qblk_dump <image.qblk> <N>  — 앞 N개 청크의 "lcn count raw_len crc32(raw)" 를 출력한다(읽기 전용, 파이썬 리더와 대조용).
use kova_image::Reader;
use std::fs::File;
use std::io::BufReader;

fn main() {
    let mut a = std::env::args().skip(1);
    let path = a.next().expect("경로가 필요합니다");
    let n: usize = a.next().and_then(|s| s.parse().ok()).unwrap_or(1000);
    let mut r = Reader::new(BufReader::with_capacity(8 << 20, File::open(&path).expect("열기 실패"))).expect("헤더");
    for _ in 0..n {
        match r.next_chunk().expect("청크") {
            Some(c) => println!("{} {} {} {}", c.lcn, c.cluster_count, c.raw.len(), crc32fast::hash(&c.raw)),
            None => break,
        }
    }
}
