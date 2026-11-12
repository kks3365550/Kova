//! 시험용: capture_volume <드라이브:> <출력.qblk>  (관리자 권한 필요. 섀도 복사본을 만들었다가 바로 지운다)
use std::sync::atomic::AtomicBool;
fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    if a.len() != 2 {
        eprintln!("usage: capture_volume <C:> <out.qblk>");
        std::process::exit(2);
    }
    let cancel = AtomicBool::new(false);
    match kova_image::volume::capture_volume(&a[0], std::path::Path::new(&a[1]), 3, &cancel, &|s| {
        if s.chunks % 50 == 0 {
            eprintln!("chunks={} clusters={}", s.chunks, s.clusters);
        }
    }) {
        Ok((h, s)) => println!(
            "OK total={} used={} chunks={} cs={} vsn={}",
            h.total_clusters, h.used_clusters, s.chunks, h.cluster_size, h.vsn
        ),
        Err(e) => {
            eprintln!("FAIL: {e}");
            std::process::exit(1);
        }
    }
}
