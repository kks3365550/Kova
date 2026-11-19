//! 디스크 이미지(.qblk) 스트리밍 AES-256-GCM 암호화·복호화 래퍼 (`QENC` v1).
//! 대용량 이미지도 1MiB 청크 단위로 스트리밍 처리해 메모리 사용량을 일정하게 유지한다.
//! 청크마다 `base_nonce XOR chunk_index`와 AAD(`chunk_index`)를 바인딩해 순서 변경·잘림·변조를 즉시 거부한다.
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use std::fs::{self, File};
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

pub const ENC_MAGIC: &[u8; 8] = b"QENC\x01\x00\x00\x00";
pub const DEFAULT_ENC_CHUNK: usize = 1024 * 1024; // 1 MiB
const TAG_LEN: usize = 16;
const MAX_ENC_CHUNK: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum ImageCryptoError {
    Io(io::Error),
    BadMagic,
    BadChunkSize(usize),
    AuthFailed(u64),
    Truncated(u64),
    Cancelled,
}

impl std::fmt::Display for ImageCryptoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "입출력 오류: {e}"),
            Self::BadMagic => write!(f, "암호화된 Kova 이미지(QENC)가 아닙니다"),
            Self::BadChunkSize(n) => write!(f, "비정상적인 암호화 청크 크기: {n}"),
            Self::AuthFailed(idx) => {
                write!(
                    f,
                    "이미지 복호화 인증 실패(청크 {idx}): 키 불일치 또는 데이터 변조"
                )
            }
            Self::Truncated(idx) => write!(f, "암호화 이미지가 중간에 잘렸습니다(청크 {idx})"),
            Self::Cancelled => write!(f, "취소됨"),
        }
    }
}

impl std::error::Error for ImageCryptoError {}
impl From<io::Error> for ImageCryptoError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

fn chunk_nonce(base: &[u8; 12], idx: u64) -> [u8; 12] {
    let mut n = *base;
    let ib = idx.to_le_bytes();
    for i in 0..8 {
        n[4 + i] ^= ib[i];
    }
    n
}

pub fn is_encrypted_image(path: &Path) -> bool {
    let Ok(mut f) = File::open(path) else {
        return false;
    };
    let mut magic = [0u8; 8];
    f.read_exact(&mut magic).is_ok() && &magic == ENC_MAGIC
}

/// 평문 `.qblk` 스트림을 AES-256-GCM 청크 스트림으로 암호화한다.
pub fn encrypt_stream<R: Read, W: Write>(
    mut src: R,
    mut dst: W,
    key: &[u8; 32],
    chunk_size: usize,
    cancel: &AtomicBool,
) -> Result<u64, ImageCryptoError> {
    if chunk_size == 0 || chunk_size > MAX_ENC_CHUNK {
        return Err(ImageCryptoError::BadChunkSize(chunk_size));
    }
    let cipher = Aes256Gcm::new_from_slice(key).unwrap();
    let mut base_nonce = [0u8; 12];
    getrandom::getrandom(&mut base_nonce).map_err(|e| io::Error::other(e.to_string()))?;

    dst.write_all(ENC_MAGIC)?;
    dst.write_all(&base_nonce)?;
    dst.write_all(&(chunk_size as u32).to_le_bytes())?;

    let mut buf = vec![0u8; chunk_size];
    let mut idx = 0u64;
    let mut total_plain = 0u64;
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err(ImageCryptoError::Cancelled);
        }
        let n = read_up_to(&mut src, &mut buf)?;
        if n == 0 {
            break;
        }
        let nonce_bytes = chunk_nonce(&base_nonce, idx);
        let aad = idx.to_le_bytes();
        let ct = cipher
            .encrypt(
                Nonce::from_slice(&nonce_bytes),
                Payload {
                    msg: &buf[..n],
                    aad: &aad,
                },
            )
            .map_err(|_| ImageCryptoError::AuthFailed(idx))?;
        dst.write_all(&(ct.len() as u32).to_le_bytes())?;
        dst.write_all(&ct)?;
        total_plain += n as u64;
        idx += 1;
    }
    // EOF 마커: 길이 0 (정상 종료 확인용 — 중간 잘림과 구분)
    dst.write_all(&0u32.to_le_bytes())?;
    dst.flush()?;
    Ok(total_plain)
}

/// `QENC` 스트림을 검증·복호화해 평문 `.qblk`로 복원한다.
pub fn decrypt_stream<R: Read, W: Write>(
    mut src: R,
    mut dst: W,
    key: &[u8; 32],
    cancel: &AtomicBool,
) -> Result<u64, ImageCryptoError> {
    let mut hdr = [0u8; 24];
    src.read_exact(&mut hdr).map_err(|e| {
        if e.kind() == io::ErrorKind::UnexpectedEof {
            ImageCryptoError::BadMagic
        } else {
            ImageCryptoError::Io(e)
        }
    })?;
    if &hdr[..8] != ENC_MAGIC {
        return Err(ImageCryptoError::BadMagic);
    }
    let base_nonce: [u8; 12] = hdr[8..20].try_into().unwrap();
    let chunk_size = u32::from_le_bytes(hdr[20..24].try_into().unwrap()) as usize;
    if chunk_size == 0 || chunk_size > MAX_ENC_CHUNK {
        return Err(ImageCryptoError::BadChunkSize(chunk_size));
    }
    let cipher = Aes256Gcm::new_from_slice(key).unwrap();
    let mut idx = 0u64;
    let mut total = 0u64;
    let mut len_buf = [0u8; 4];
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err(ImageCryptoError::Cancelled);
        }
        match src.read_exact(&mut len_buf) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
                return Err(ImageCryptoError::Truncated(idx));
            }
            Err(e) => return Err(ImageCryptoError::Io(e)),
        }
        let ct_len = u32::from_le_bytes(len_buf) as usize;
        if ct_len == 0 {
            break;
        }
        if ct_len < TAG_LEN || ct_len > chunk_size + TAG_LEN {
            return Err(ImageCryptoError::BadChunkSize(ct_len));
        }
        let mut ct = vec![0u8; ct_len];
        src.read_exact(&mut ct).map_err(|e| {
            if e.kind() == io::ErrorKind::UnexpectedEof {
                ImageCryptoError::Truncated(idx)
            } else {
                ImageCryptoError::Io(e)
            }
        })?;
        let nonce_bytes = chunk_nonce(&base_nonce, idx);
        let aad = idx.to_le_bytes();
        let plain = cipher
            .decrypt(
                Nonce::from_slice(&nonce_bytes),
                Payload {
                    msg: &ct,
                    aad: &aad,
                },
            )
            .map_err(|_| ImageCryptoError::AuthFailed(idx))?;
        dst.write_all(&plain)?;
        total += plain.len() as u64;
        idx += 1;
    }
    dst.flush()?;
    Ok(total)
}

/// 파일 단위 암호화. 실패·취소 시 불완전한 출력 파일을 삭제한다.
pub fn encrypt_file(
    src_path: &Path,
    dst_path: &Path,
    key: &[u8; 32],
    cancel: &AtomicBool,
) -> Result<u64, ImageCryptoError> {
    let r = BufReader::with_capacity(DEFAULT_ENC_CHUNK, File::open(src_path)?);
    let w = BufWriter::with_capacity(DEFAULT_ENC_CHUNK, File::create(dst_path)?);
    match encrypt_stream(r, w, key, DEFAULT_ENC_CHUNK, cancel) {
        Ok(n) => Ok(n),
        Err(e) => {
            let _ = fs::remove_file(dst_path);
            Err(e)
        }
    }
}

/// 파일 단위 복호화. 실패·취소 시 불완전한 출력 파일을 삭제한다.
pub fn decrypt_file(
    src_path: &Path,
    dst_path: &Path,
    key: &[u8; 32],
    cancel: &AtomicBool,
) -> Result<u64, ImageCryptoError> {
    let r = BufReader::with_capacity(DEFAULT_ENC_CHUNK, File::open(src_path)?);
    let w = BufWriter::with_capacity(DEFAULT_ENC_CHUNK, File::create(dst_path)?);
    match decrypt_stream(r, w, key, cancel) {
        Ok(n) => Ok(n),
        Err(e) => {
            let _ = fs::remove_file(dst_path);
            Err(e)
        }
    }
}

fn read_up_to<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match r.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn roundtrip_and_tamper_detection() {
        let key = [0x42u8; 32];
        let wrong_key = [0x99u8; 32];
        let plain: Vec<u8> = (0..150_000u32).map(|i| (i % 251) as u8).collect();
        let no = AtomicBool::new(false);

        let mut enc = Vec::new();
        let n = encrypt_stream(Cursor::new(&plain), &mut enc, &key, 4096, &no).unwrap();
        assert_eq!(n as usize, plain.len());
        assert!(&enc[..8] == ENC_MAGIC);

        // 정상 복호화
        let mut dec = Vec::new();
        decrypt_stream(Cursor::new(&enc), &mut dec, &key, &no).unwrap();
        assert_eq!(dec, plain);

        // 틀린 키 거부
        let mut bad_dec = Vec::new();
        assert!(matches!(
            decrypt_stream(Cursor::new(&enc), &mut bad_dec, &wrong_key, &no),
            Err(ImageCryptoError::AuthFailed(0))
        ));

        // 1비트 변조 거부
        let mut tampered = enc.clone();
        tampered[40] ^= 1;
        assert!(matches!(
            decrypt_stream(Cursor::new(&tampered), &mut Vec::new(), &key, &no),
            Err(ImageCryptoError::AuthFailed(_))
        ));

        // EOF 마커 잘림 거부
        let truncated = &enc[..enc.len() - 4];
        assert!(matches!(
            decrypt_stream(Cursor::new(truncated), &mut Vec::new(), &key, &no),
            Err(ImageCryptoError::Truncated(_))
        ));
    }

    #[test]
    fn file_roundtrip_and_detection() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("disk.qblk");
        let enc = d.path().join("disk.qblk.enc");
        let dec = d.path().join("disk.dec.qblk");
        let payload = b"QBLKSTRM-dummy-volume-bytes".repeat(500);
        fs::write(&src, &payload).unwrap();
        let key = [7u8; 32];
        let no = AtomicBool::new(false);

        assert!(!is_encrypted_image(&src));
        encrypt_file(&src, &enc, &key, &no).unwrap();
        assert!(is_encrypted_image(&enc));
        decrypt_file(&enc, &dec, &key, &no).unwrap();
        assert_eq!(fs::read(&dec).unwrap(), payload);
    }
}
