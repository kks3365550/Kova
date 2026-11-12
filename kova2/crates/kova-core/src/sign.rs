//! Ed25519 서명 (core/crypto_sign.py 호환): 키는 PEM(PKCS#8 개인키 / SPKI 공개키), 서명은 원시 64바이트의 hex 문자열.
use ed25519_dalek::pkcs8::spki::der::pem::LineEnding;
use ed25519_dalek::pkcs8::{DecodePrivateKey, DecodePublicKey, EncodePrivateKey, EncodePublicKey};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};

#[derive(Debug)]
pub struct SignError(pub String);

impl std::fmt::Display for SignError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for SignError {}

fn err<E: std::fmt::Display>(e: E) -> SignError {
    SignError(e.to_string())
}

/// 새 키 쌍을 만든다. (개인키 PEM, 공개키 PEM)
pub fn generate_key_pair() -> Result<(String, String), SignError> {
    let mut seed = [0u8; 32];
    getrandom_fill(&mut seed)?;
    let sk = SigningKey::from_bytes(&seed);
    let priv_pem = sk.to_pkcs8_pem(LineEnding::LF).map_err(err)?.to_string();
    let pub_pem = sk
        .verifying_key()
        .to_public_key_pem(LineEnding::LF)
        .map_err(err)?;
    Ok((priv_pem, pub_pem))
}

fn getrandom_fill(buf: &mut [u8]) -> Result<(), SignError> {
    getrandom::getrandom(buf).map_err(err)
}

pub fn sign_bytes(data: &[u8], private_pem: &str) -> Result<String, SignError> {
    let sk = SigningKey::from_pkcs8_pem(private_pem).map_err(err)?;
    Ok(hex_encode(&sk.sign(data).to_bytes()))
}

/// 서명이 맞으면 true. 키·서명 형식이 잘못돼도 false(검증 실패는 항상 닫힌 쪽으로).
pub fn verify_bytes(data: &[u8], signature_hex: &str, public_pem: &str) -> bool {
    let vk = match VerifyingKey::from_public_key_pem(public_pem) {
        Ok(k) => k,
        Err(_) => return false,
    };
    let bytes = match hex_decode(signature_hex.trim()) {
        Some(b) if b.len() == 64 => b,
        _ => return false,
    };
    let mut arr = [0u8; 64];
    arr.copy_from_slice(&bytes);
    vk.verify(data, &Signature::from_bytes(&arr)).is_ok()
}

/// 후보 공개키 중 하나라도 맞으면 true (키 교체 기간 용도).
pub fn verify_bytes_any(data: &[u8], signature_hex: &str, public_pems: &[&str]) -> bool {
    public_pems
        .iter()
        .any(|p| verify_bytes(data, signature_hex, p))
}

fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_and_verify_roundtrip() {
        let (sk, pk) = generate_key_pair().unwrap();
        let sig = sign_bytes(b"hello kova", &sk).unwrap();
        assert_eq!(sig.len(), 128);
        assert!(verify_bytes(b"hello kova", &sig, &pk));
    }

    #[test]
    fn tampered_data_or_signature_fails() {
        let (sk, pk) = generate_key_pair().unwrap();
        let sig = sign_bytes(b"data", &sk).unwrap();
        assert!(!verify_bytes(b"datA", &sig, &pk));
        let mut bad = sig.clone();
        bad.replace_range(0..2, if &sig[0..2] == "00" { "01" } else { "00" });
        assert!(!verify_bytes(b"data", &bad, &pk));
    }

    #[test]
    fn wrong_key_and_garbage_inputs_fail_closed() {
        let (sk, _) = generate_key_pair().unwrap();
        let (_, other_pk) = generate_key_pair().unwrap();
        let sig = sign_bytes(b"data", &sk).unwrap();
        assert!(!verify_bytes(b"data", &sig, &other_pk));
        assert!(!verify_bytes(b"data", "zz", &other_pk));
        assert!(!verify_bytes(b"data", "", &other_pk));
        assert!(!verify_bytes(b"data", &sig, "not a pem"));
    }

    #[test]
    fn verify_any_accepts_rotated_key() {
        let (sk, pk) = generate_key_pair().unwrap();
        let (_, old_pk) = generate_key_pair().unwrap();
        let sig = sign_bytes(b"x", &sk).unwrap();
        assert!(verify_bytes_any(b"x", &sig, &[&old_pk, &pk]));
        assert!(!verify_bytes_any(b"x", &sig, &[&old_pk]));
    }
}
