//! 백업 파이프라인 단계별 처리량 측정(원인 분석용).
//! 사용: cargo run --release -p kova-engine --example stage_bench -- <작업폴더> [파일MB=512] [동시파일수=8]
use kova_engine::{chunker, compressor};
use rayon::prelude::*;
use std::fs;
use std::path::Path;
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

fn mbs(bytes: usize, t: Instant) -> String {
    format!(
        "{:.0}MB/s ({:.2}s)",
        bytes as f64 / 1e6 / t.elapsed().as_secs_f64(),
        t.elapsed().as_secs_f64()
    )
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let dir = Path::new(&a[1]);
    let mb: usize = a.get(2).and_then(|v| v.parse().ok()).unwrap_or(512);
    let par: usize = a.get(3).and_then(|v| v.parse().ok()).unwrap_or(8);
    fs::create_dir_all(dir).unwrap();
    let n = mb << 20;
    let f = dir.join("one.bin");
    fs::write(&f, rng(1, n)).unwrap();

    // 단일 스레드 단계별
    let t = Instant::now();
    let data = fs::read(&f).unwrap();
    println!("A 파일 읽기            {}", mbs(n, t));
    let t = Instant::now();
    let chunks = chunker::chunk_slice(&data);
    println!(
        "B 청크+해시+복사        {} 청크 {}",
        mbs(n, t),
        chunks.len()
    );
    let t = Instant::now();
    let enc: Vec<Vec<u8>> = chunks
        .iter()
        .map(|c| compressor::compress(&c.data, 3).unwrap())
        .collect();
    println!("C zstd(3)              {}", mbs(n, t));
    let t = Instant::now();
    let t2 = Instant::now();
    let _ = t2;
    let blobs = dir.join("blobs");
    fs::create_dir_all(&blobs).unwrap();
    for (i, e) in enc.iter().enumerate() {
        fs::write(blobs.join(format!("{i}.blob")), e).unwrap();
    }
    println!("D 블롭 파일 쓰기        {}", mbs(n, t));
    drop((data, chunks, enc));

    // 파일 여러 개 동시(엔진과 같은 방식: 파일 단위 병렬, 파일 안은 순차)
    for i in 0..par {
        fs::write(dir.join(format!("p{i}.bin")), rng(100 + i as u64, n)).unwrap();
    }
    let t = Instant::now();
    let total: usize = (0..par)
        .into_par_iter()
        .map(|i| {
            let p = dir.join(format!("p{i}.bin"));
            let ch = chunker::chunk_file(&p).unwrap();
            let bd = dir.join(format!("pb{i}"));
            fs::create_dir_all(&bd).unwrap();
            let mut sz = 0;
            for (k, c) in ch.iter().enumerate() {
                let e = compressor::compress(&c.data, 3).unwrap();
                fs::write(bd.join(format!("{k}.blob")), &e).unwrap();
                sz += c.length;
            }
            sz
        })
        .sum();
    println!(
        "E {par}개 동시 전체(엔진 방식) {} (rayon 스레드 {})",
        mbs(total, t),
        rayon::current_num_threads()
    );
    let _ = fs::remove_dir_all(dir);
}
