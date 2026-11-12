//! 업데이트 판정과 패키지 검증 (core/updater.py 의 parse_version·verify_update 이식).
//! 파이썬은 서명이 비어 있으면 서명 검사를 건너뛰지만, 여기서는 서명이 없거나 신뢰 키가 없으면 거부한다(Fail-Closed).
use sha2::{Digest, Sha256};

/// 'v2.9.1' → [2, 9, 1]. 숫자 접두부만 읽고(예: '3rc1' → 3) 숫자가 없으면 0. 빈 문자열은 [0,0,0].
pub fn parse_version(s: &str) -> Vec<u64> {
    let clean = s.trim().trim_start_matches(['v', 'V']);
    let parts: Vec<u64> = clean
        .split('.')
        .map(|p| {
            let d: String = p.chars().take_while(|c| c.is_ascii_digit()).collect();
            d.parse().unwrap_or(0)
        })
        .collect();
    if parts.is_empty() { vec![0, 0, 0] } else { parts }
}

/// 자리 수가 달라도 뒤를 0 으로 채워 비교한다('2.17' == '2.17.0').
pub fn is_newer(latest: &str, current: &str) -> bool {
    let (mut a, mut b) = (parse_version(latest), parse_version(current));
    let n = a.len().max(b.len());
    a.resize(n, 0);
    b.resize(n, 0);
    a > b
}

#[derive(Debug, PartialEq)]
pub enum VerifyError {
    HashMismatch { expected: String, actual: String },
    NoSignature,
    NoTrustedKeys,
    BadSignature,
}
impl std::fmt::Display for VerifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::HashMismatch { expected, actual } => write!(f, "SHA-256 불일치(기대 {expected}, 실제 {actual})"),
            Self::NoSignature => write!(f, "서명이 없는 업데이트는 설치하지 않습니다"),
            Self::NoTrustedKeys => write!(f, "신뢰할 공개키가 없습니다"),
            Self::BadSignature => write!(f, "서명 검증 실패"),
        }
    }
}

/// 패키지 바이트의 SHA-256(기대값이 있으면)과 Ed25519 서명(신뢰 키 중 하나)을 검증한다.
pub fn verify_package(bytes: &[u8], expected_sha256: &str, signature_hex: &str, trusted_pems: &[&str]) -> Result<(), VerifyError> {
    let actual = format!("{:x}", Sha256::digest(bytes));
    if !expected_sha256.is_empty() && !actual.eq_ignore_ascii_case(expected_sha256) {
        return Err(VerifyError::HashMismatch { expected: expected_sha256.to_lowercase(), actual });
    }
    if signature_hex.is_empty() { return Err(VerifyError::NoSignature); }
    if trusted_pems.is_empty() { return Err(VerifyError::NoTrustedKeys); }
    if crate::sign::verify_bytes_any(bytes, signature_hex, trusted_pems) { Ok(()) } else { Err(VerifyError::BadSignature) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_parsing_matches_python_rules() {
        assert_eq!(parse_version("v2.9.1"), vec![2, 9, 1]);
        assert_eq!(parse_version(" V2.17.82 "), vec![2, 17, 82]);
        assert_eq!(parse_version("3rc1.x"), vec![3, 0]);
        assert_eq!(parse_version(""), vec![0]);
    }

    #[test]
    fn newer_means_strictly_greater_numerically() {
        assert!(is_newer("2.17.83", "2.17.82"));
        assert!(is_newer("2.18", "2.17.99"));
        assert!(is_newer("v10.0.0", "9.99.99"), "문자열 비교가 아니라 숫자 비교");
        assert!(!is_newer("2.17.82", "2.17.82"));
        assert!(!is_newer("2.17", "2.17.0"));
        assert!(!is_newer("2.17.81", "2.17.82"));
    }

    #[test]
    fn package_verification_is_fail_closed() {
        let (private, public) = crate::sign::generate_key_pair().unwrap();
        let data = b"kova update package";
        let sig = crate::sign::sign_bytes(data, &private).unwrap();
        let sha = format!("{:x}", Sha256::digest(data));
        assert_eq!(verify_package(data, &sha, &sig, &[&public]), Ok(()));
        assert_eq!(verify_package(data, "", &sig, &[&public]), Ok(()), "기대 해시를 안 줘도 서명이 맞으면 통과");
        assert!(matches!(verify_package(data, &"0".repeat(64), &sig, &[&public]), Err(VerifyError::HashMismatch { .. })));
        assert_eq!(verify_package(data, &sha, "", &[&public]), Err(VerifyError::NoSignature));
        assert_eq!(verify_package(data, &sha, &sig, &[]), Err(VerifyError::NoTrustedKeys));
        let (_, other_public) = crate::sign::generate_key_pair().unwrap();
        assert_eq!(verify_package(data, &sha, &sig, &[&other_public]), Err(VerifyError::BadSignature));
        assert_eq!(verify_package(b"tampered", "", &sig, &[&public]), Err(VerifyError::BadSignature));
    }
}
