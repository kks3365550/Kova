//! 실제 릴리즈에 대한 읽기 전용 점검: 최신 태그 조회 → 패키지·서명 다운로드 → Ed25519 검증(설치 안 함).
fn main() {
    let (base, repo) = ("https://github.com", "kks3365550/Kova");
    let tag = kova_core::fetch::latest_tag(base, repo).unwrap();
    println!("latest={tag}");
    match kova_core::fetch::fetch_verified(base, repo, &tag, kova_core::fetch::TRUSTED_KEYS, 200 << 20) {
        Ok(b) => println!("VERIFIED {} bytes", b.len()),
        Err(e) => println!("FAIL {e}"),
    }
}
