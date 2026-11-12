//! 사용법: verify_sig <파일> <서명파일(.sig)> <공개키.pub>  — Python 이 만든 릴리즈 서명을 Rust 로 검증한다.
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let data = std::fs::read(&a[1]).expect("read data");
    let sig = std::fs::read_to_string(&a[2]).expect("read sig");
    let pubpem = std::fs::read_to_string(&a[3]).expect("read pub");
    let ok = kova_core::sign::verify_bytes(&data, &sig, &pubpem);
    println!("verify={} (data {} bytes, sig {} chars)", ok, data.len(), sig.trim().len());
    std::process::exit(if ok { 0 } else { 1 });
}
