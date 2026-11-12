//! 저장 데이터 암호화 (core/crypto_at_rest.py 이식, 블롭 포맷 호환).
//! 블롭: b"ENC\x01" + 12B nonce + AES-256-GCM(zstd(평문)) + 16B tag.
//! AAD 는 {"blob_sha256","encryption_version","format_version"} 정렬 JSON. 평문 해시를 블롭 키로 유지해 중복제거를 보존한다.
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use sha2::{Digest, Sha256};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub const MAGIC_V1: &[u8; 4] = b"ENC\x01";
const ZSTD_MAGIC: [u8; 4] = [0x28, 0xB5, 0x2F, 0xFD];
pub const SCRYPT_N: u32 = 32768;
pub const SCRYPT_R: u32 = 8;
pub const SCRYPT_P: u32 = 1;

#[derive(Debug)]
pub enum CryptoError {
    Corrupt(String),
    Auth,
    HashMismatch { expected: String, actual: String },
    Io(io::Error),
    Kdf(String),
}
impl std::fmt::Display for CryptoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Corrupt(m) => write!(f, "손상된 블롭: {m}"),
            Self::Auth => write!(
                f,
                "블롭 복호화 실패 (인증 태그 불일치: 키 오류 또는 데이터·AAD 손상)"
            ),
            Self::HashMismatch { expected, actual } => {
                write!(f, "해시 불일치 (기대 {expected}, 실제 {actual})")
            }
            Self::Io(e) => write!(f, "입출력 오류: {e}"),
            Self::Kdf(m) => write!(f, "키 파생 실패: {m}"),
        }
    }
}
impl std::error::Error for CryptoError {}
impl From<io::Error> for CryptoError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
pub fn unhex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

/// scrypt 로 32바이트 저장 키를 만든다 (파이썬 StorageKeyDeriver.derive 와 같은 값).
pub fn derive_key(
    passphrase: &str,
    salt: &[u8],
    n: u32,
    r: u32,
    p: u32,
) -> Result<[u8; 32], CryptoError> {
    if n < 2 || !n.is_power_of_two() {
        return Err(CryptoError::Kdf("n 은 2의 거듭제곱이어야 함".into()));
    }
    let params = scrypt::Params::new(n.trailing_zeros() as u8, r, p, 32)
        .map_err(|e| CryptoError::Kdf(e.to_string()))?;
    let mut out = [0u8; 32];
    scrypt::scrypt(passphrase.as_bytes(), salt, &params, &mut out)
        .map_err(|e| CryptoError::Kdf(e.to_string()))?;
    Ok(out)
}

pub fn random_bytes<const N: usize>() -> [u8; N] {
    let mut b = [0u8; N];
    getrandom::getrandom(&mut b).expect("OS 난수 사용 불가");
    b
}

pub fn blob_aad(plain_hash: &str) -> Vec<u8> {
    format!(
        "{{\"blob_sha256\":\"{}\",\"encryption_version\":1,\"format_version\":1}}",
        plain_hash.to_lowercase()
    )
    .into_bytes()
}

pub struct CryptoEngine {
    cipher: Aes256Gcm,
}

impl CryptoEngine {
    pub fn new(key: &[u8; 32]) -> Self {
        Self {
            cipher: Aes256Gcm::new(key.into()),
        }
    }

    pub fn from_passphrase(pass: &str, salt: &[u8]) -> Result<Self, CryptoError> {
        Ok(Self::new(&derive_key(
            pass, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P,
        )?))
    }

    /// 평문 → zstd → AES-GCM. plain_hash 는 중복제거 키(평문 SHA-256 hex)이며 AAD 에 묶인다.
    pub fn encrypt_blob(
        &self,
        plain: &[u8],
        plain_hash: &str,
        level: i32,
    ) -> Result<Vec<u8>, CryptoError> {
        let comp = zstd::encode_all(plain, level)?;
        let nonce: [u8; 12] = random_bytes();
        let aad = blob_aad(plain_hash);
        let ct = self
            .cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: &comp,
                    aad: &aad,
                },
            )
            .map_err(|_| CryptoError::Auth)?;
        let mut out = Vec::with_capacity(16 + ct.len());
        out.extend_from_slice(MAGIC_V1);
        out.extend_from_slice(&nonce);
        out.extend_from_slice(&ct);
        Ok(out)
    }

    /// v0(평문 zstd)·v1(암호화) 모두 복호화하고 해시를 재검증한다.
    pub fn decrypt_blob(&self, blob: &[u8], expected_hash: &str) -> Result<Vec<u8>, CryptoError> {
        if blob.len() < 4 {
            return Err(CryptoError::Corrupt("길이가 너무 짧음".into()));
        }
        let comp = if &blob[..4] == MAGIC_V1 {
            if blob.len() < 4 + 12 + 16 {
                return Err(CryptoError::Corrupt("헤더/태그 길이 부족".into()));
            }
            let aad = blob_aad(expected_hash);
            self.cipher
                .decrypt(
                    Nonce::from_slice(&blob[4..16]),
                    Payload {
                        msg: &blob[16..],
                        aad: &aad,
                    },
                )
                .map_err(|_| CryptoError::Auth)?
        } else {
            if blob[..4] != ZSTD_MAGIC {
                return Err(CryptoError::Corrupt("알 수 없는 매직".into()));
            }
            blob.to_vec()
        };
        let plain = zstd::decode_all(&comp[..]).map_err(|e| CryptoError::Corrupt(e.to_string()))?;
        let actual = hex(&Sha256::digest(&plain));
        if !actual.eq_ignore_ascii_case(expected_hash) {
            return Err(CryptoError::HashMismatch {
                expected: expected_hash.into(),
                actual,
            });
        }
        Ok(plain)
    }

    /// CAS 원자적 저장. 이미 있으면 (경로, false). 임시 파일 → fsync → 읽기 전용 → rename.
    pub fn save_blob_atomic(
        &self,
        repo: &Path,
        plain: &[u8],
        plain_hash: &str,
        level: i32,
    ) -> Result<(PathBuf, bool), CryptoError> {
        let dir = repo.join("blobs").join(&plain_hash[..2]);
        let path = dir.join(format!("{plain_hash}.blob"));
        if fs::metadata(&path).map(|m| m.len() > 0).unwrap_or(false) {
            return Ok((path, false));
        }
        let enc = self.encrypt_blob(plain, plain_hash, level)?;
        fs::create_dir_all(&dir)?;
        let tmp = dir.join(format!(
            "{plain_hash}.blob.tmp_{}_{}",
            std::process::id(),
            hex(&random_bytes::<4>())
        ));
        let res = (|| -> io::Result<bool> {
            use std::io::Write;
            let mut f = fs::File::create(&tmp)?;
            f.write_all(&enc)?;
            f.sync_all()?;
            drop(f);
            if path.exists() {
                return Ok(false);
            }
            let mut perm = fs::metadata(&tmp)?.permissions();
            perm.set_readonly(true);
            fs::set_permissions(&tmp, perm)?;
            match fs::rename(&tmp, &path) {
                Ok(()) => Ok(true),
                Err(_) if path.exists() => Ok(false),
                Err(e) => Err(e),
            }
        })();
        if tmp.exists() {
            if let Ok(m) = fs::metadata(&tmp) {
                let mut p = m.permissions();
                #[allow(clippy::permissions_set_readonly_false)]
                p.set_readonly(false);
                let _ = fs::set_permissions(&tmp, p);
            }
            let _ = fs::remove_file(&tmp);
        }
        Ok((path, res?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sha(d: &[u8]) -> String {
        hex(&Sha256::digest(d))
    }

    #[test]
    fn scrypt_matches_python_cryptography() {
        // python: Scrypt(salt=bytes(range(16)),length=32,n=32768,r=8,p=1).derive("한글 pass".encode()).hex()
        // 알려진 RFC 7914 벡터로 구현 정확성 확인 (n=16, r=1, p=1, 빈 입력)
        let k = derive_key("", b"", 16, 1, 1).unwrap();
        assert_eq!(&hex(&k)[..32], "77d6576238657b203b19ca42c18a0497");
    }

    #[test]
    fn roundtrip_and_aad_binding() {
        let e = CryptoEngine::from_passphrase("암호", &[7u8; 16]).unwrap();
        let data = b"hello kova ".repeat(1000);
        let h = sha(&data);
        let blob = e.encrypt_blob(&data, &h, 1).unwrap();
        assert_eq!(&blob[..4], MAGIC_V1);
        assert_eq!(e.decrypt_blob(&blob, &h).unwrap(), data);
        // 다른 해시(AAD)로는 복호화 불가
        assert!(matches!(
            e.decrypt_blob(&blob, &sha(b"x")),
            Err(CryptoError::Auth)
        ));
        // 다른 키 불가
        let e2 = CryptoEngine::from_passphrase("다른", &[7u8; 16]).unwrap();
        assert!(matches!(e2.decrypt_blob(&blob, &h), Err(CryptoError::Auth)));
    }

    #[test]
    fn bit_flip_and_truncation_are_detected() {
        let e = CryptoEngine::new(&[1u8; 32]);
        let h = sha(b"abc");
        let mut blob = e.encrypt_blob(b"abc", &h, 1).unwrap();
        let last = blob.len() - 1;
        blob[last] ^= 1;
        assert!(e.decrypt_blob(&blob, &h).is_err());
        assert!(e.decrypt_blob(&blob[..10], &h).is_err());
        assert!(e.decrypt_blob(&[1, 2], &h).is_err());
    }

    #[test]
    fn v0_plain_zstd_blob_still_reads_and_verifies_hash() {
        let e = CryptoEngine::new(&[1u8; 32]);
        let data = b"legacy".to_vec();
        let v0 = zstd::encode_all(&data[..], 1).unwrap();
        assert_eq!(e.decrypt_blob(&v0, &sha(&data)).unwrap(), data);
        assert!(matches!(
            e.decrypt_blob(&v0, &sha(b"other")),
            Err(CryptoError::HashMismatch { .. })
        ));
    }

    #[test]
    fn save_blob_atomic_dedups_and_locks_readonly() {
        let d = tempfile::tempdir().unwrap();
        let e = CryptoEngine::new(&[9u8; 32]);
        let data = b"payload".to_vec();
        let h = sha(&data);
        let (p, new1) = e.save_blob_atomic(d.path(), &data, &h, 1).unwrap();
        let (_, new2) = e.save_blob_atomic(d.path(), &data, &h, 1).unwrap();
        assert!(new1 && !new2);
        assert!(fs::metadata(&p).unwrap().permissions().readonly());
        assert_eq!(e.decrypt_blob(&fs::read(&p).unwrap(), &h).unwrap(), data);
        let leftovers = fs::read_dir(p.parent().unwrap()).unwrap().count();
        assert_eq!(leftovers, 1);
    }

    /// 파이썬(core/crypto_at_rest.py)이 만든 실제 블롭과 키 파생값을 Rust 가 그대로 읽는다.
    #[test]
    fn python_made_blob_and_key_are_compatible() {
        let salt: Vec<u8> = (0u8..16).collect();
        let key = derive_key("한글 pass", &salt, SCRYPT_N, SCRYPT_R, SCRYPT_P).unwrap();
        assert_eq!(
            hex(&key),
            "b42bd0d36575380adf82620949a9a30a53099f9c777b45007dcf05276e019506"
        );
        let h = "e81a54d87adae860c120b83890cfd4fa1d6b8aca6b7fc3648c19ca69ff3837e2";
        let blob = unhex("454e430196f322604aeda5195cbc4c7360ae23da47e27ad43253f44bf528723ef79a6e33bb13bd5580caf7c835805fee0c5951e5c6f9f80c831f5e1fef4609588048").unwrap();
        let plain = CryptoEngine::new(&key).decrypt_blob(&blob, h).unwrap();
        assert_eq!(plain, "호환성 시험 ".repeat(50).into_bytes());
    }
}
