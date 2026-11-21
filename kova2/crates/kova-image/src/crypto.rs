//! 디스크 이미지(.qblk) 스트리밍 AES-256-GCM 암호화·복호화 래퍼 (`QENC` v2, v1 읽기 호환).
//! 대용량 이미지도 1MiB 청크 단위로 스트리밍 처리해 메모리 사용량을 일정하게 유지한다.
//! 청크마다 `base_nonce XOR chunk_index`와 AAD를 바인딩해 순서 변경·잘림·변조를 즉시 거부한다.
//! - v2(새로 만드는 파일): 청크 = `flag(u8) | len(u32) | ct`, AAD = `chunk_index | flag`.
//!   마지막 청크만 `flag=1`이며 인증되므로, 청크 경계에서 잘라도·중간 청크를 끝으로 위장해도 거부된다.
//!   마지막 청크 뒤에 남는 바이트도 거부한다. 빈 입력은 평문 0바이트의 최종 청크 하나로 기록한다.
//! - v1(기존 파일, 읽기 전용): 청크 = `len(u32) | ct`, AAD = `chunk_index`, 끝 표식은 인증 없는 길이 0.
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use std::fs::{self, File};
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

/// 새로 기록하는 암호화 이미지 헤더(v2).
pub const ENC_MAGIC: &[u8; 8] = b"QENC\x02\x00\x00\x00";
/// 기존 v1 암호화 이미지 헤더(복호화·검증만 지원).
pub const ENC_MAGIC_V1: &[u8; 8] = b"QENC\x01\x00\x00\x00";
const FLAG_MORE: u8 = 0;
const FLAG_FINAL: u8 = 1;
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
    TrailingData,
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
            Self::TrailingData => write!(
                f,
                "암호화 이미지의 마지막 청크 뒤에 알 수 없는 데이터가 있습니다"
            ),
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

fn aad_v2(idx: u64, flag: u8) -> [u8; 9] {
    let mut a = [0u8; 9];
    a[..8].copy_from_slice(&idx.to_le_bytes());
    a[8] = flag;
    a
}

pub fn is_encrypted_image(path: &Path) -> bool {
    let Ok(mut f) = File::open(path) else {
        return false;
    };
    let mut magic = [0u8; 8];
    f.read_exact(&mut magic).is_ok() && (&magic == ENC_MAGIC || &magic == ENC_MAGIC_V1)
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

    // 마지막 청크 여부를 알기 위해 한 청크를 미리 읽는다(빈 입력이면 빈 최종 청크 하나).
    let mut buf = vec![0u8; chunk_size];
    let mut next = vec![0u8; chunk_size];
    let mut n = read_up_to(&mut src, &mut buf)?;
    let mut idx = 0u64;
    let mut total_plain = 0u64;
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err(ImageCryptoError::Cancelled);
        }
        let next_n = if n == chunk_size {
            read_up_to(&mut src, &mut next)?
        } else {
            0
        };
        let flag = if next_n == 0 { FLAG_FINAL } else { FLAG_MORE };
        let nonce_bytes = chunk_nonce(&base_nonce, idx);
        let aad = aad_v2(idx, flag);
        let ct = cipher
            .encrypt(
                Nonce::from_slice(&nonce_bytes),
                Payload {
                    msg: &buf[..n],
                    aad: &aad,
                },
            )
            .map_err(|_| ImageCryptoError::AuthFailed(idx))?;
        dst.write_all(&[flag])?;
        dst.write_all(&(ct.len() as u32).to_le_bytes())?;
        dst.write_all(&ct)?;
        total_plain += n as u64;
        idx += 1;
        if flag == FLAG_FINAL {
            break;
        }
        std::mem::swap(&mut buf, &mut next);
        n = next_n;
    }
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
    let v2 = &hdr[..8] == ENC_MAGIC;
    if !v2 && &hdr[..8] != ENC_MAGIC_V1 {
        return Err(ImageCryptoError::BadMagic);
    }
    let base_nonce: [u8; 12] = hdr[8..20].try_into().unwrap();
    let chunk_size = u32::from_le_bytes(hdr[20..24].try_into().unwrap()) as usize;
    if chunk_size == 0 || chunk_size > MAX_ENC_CHUNK {
        return Err(ImageCryptoError::BadChunkSize(chunk_size));
    }
    let cipher = Aes256Gcm::new_from_slice(key).unwrap();
    if v2 {
        return decrypt_chunks_v2(src, dst, &cipher, &base_nonce, chunk_size, cancel);
    }
    // v1 읽기 전용 호환 경로(기존 파일). 새 파일은 만들지 않는다.
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

/// 출력 파일을 새로 만든다. 이미 있으면 덮어쓰지 않고 오류(기존 파일은 그대로 둔다).
fn create_new(p: &Path) -> io::Result<File> {
    fs::OpenOptions::new().write(true).create_new(true).open(p)
}

fn eof_as_truncated(idx: u64) -> impl Fn(io::Error) -> ImageCryptoError {
    move |e| {
        if e.kind() == io::ErrorKind::UnexpectedEof {
            ImageCryptoError::Truncated(idx)
        } else {
            ImageCryptoError::Io(e)
        }
    }
}

/// v2 청크 스트림 복호화: 인증된 최종 청크(`flag=1`)로만 정상 종료하고, 그 뒤 데이터는 거부한다.
fn decrypt_chunks_v2<R: Read, W: Write>(
    mut src: R,
    mut dst: W,
    cipher: &Aes256Gcm,
    base_nonce: &[u8; 12],
    chunk_size: usize,
    cancel: &AtomicBool,
) -> Result<u64, ImageCryptoError> {
    let mut idx = 0u64;
    let mut total = 0u64;
    let mut frame = [0u8; 5];
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err(ImageCryptoError::Cancelled);
        }
        src.read_exact(&mut frame).map_err(eof_as_truncated(idx))?;
        let flag = frame[0];
        if flag != FLAG_MORE && flag != FLAG_FINAL {
            return Err(ImageCryptoError::AuthFailed(idx));
        }
        let ct_len = u32::from_le_bytes(frame[1..5].try_into().unwrap()) as usize;
        // 마지막이 아닌 청크는 항상 꽉 찬 크기다.
        let bad_len = ct_len < TAG_LEN
            || ct_len > chunk_size + TAG_LEN
            || (flag == FLAG_MORE && ct_len != chunk_size + TAG_LEN);
        if bad_len {
            return Err(ImageCryptoError::BadChunkSize(ct_len));
        }
        let mut ct = vec![0u8; ct_len];
        src.read_exact(&mut ct).map_err(eof_as_truncated(idx))?;
        let nonce_bytes = chunk_nonce(base_nonce, idx);
        let aad = aad_v2(idx, flag);
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
        if flag == FLAG_FINAL {
            break;
        }
    }
    let mut extra = [0u8; 1];
    loop {
        match src.read(&mut extra) {
            Ok(0) => break,
            Ok(_) => return Err(ImageCryptoError::TrailingData),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(ImageCryptoError::Io(e)),
        }
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
    let w = BufWriter::with_capacity(DEFAULT_ENC_CHUNK, create_new(dst_path)?);
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
    let w = BufWriter::with_capacity(DEFAULT_ENC_CHUNK, create_new(dst_path)?);
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

        // 최종 청크 일부 잘림 거부
        let truncated = &enc[..enc.len() - 4];
        assert!(matches!(
            decrypt_stream(Cursor::new(truncated), &mut Vec::new(), &key, &no),
            Err(ImageCryptoError::Truncated(_))
        ));
    }

    const HDR: usize = 24;
    const CS: usize = 4096;

    /// v2 파일의 청크 프레임(시작 위치, 길이) 목록.
    fn frames_v2(enc: &[u8]) -> Vec<(usize, usize)> {
        let mut out = Vec::new();
        let mut p = HDR;
        while p < enc.len() {
            let len = u32::from_le_bytes(enc[p + 1..p + 5].try_into().unwrap()) as usize;
            out.push((p, 5 + len));
            p += 5 + len;
        }
        out
    }

    /// 구버전(v1) 기록기 재현: 기존 파일 호환 시험용.
    fn encrypt_stream_v1(plain: &[u8], key: &[u8; 32], chunk_size: usize) -> Vec<u8> {
        let cipher = Aes256Gcm::new_from_slice(key).unwrap();
        let base_nonce = [0x5au8; 12];
        let mut dst = Vec::new();
        dst.extend_from_slice(ENC_MAGIC_V1);
        dst.extend_from_slice(&base_nonce);
        dst.extend_from_slice(&(chunk_size as u32).to_le_bytes());
        for (idx, c) in plain.chunks(chunk_size).enumerate() {
            let idx = idx as u64;
            let ct = cipher
                .encrypt(
                    Nonce::from_slice(&chunk_nonce(&base_nonce, idx)),
                    Payload {
                        msg: c,
                        aad: &idx.to_le_bytes(),
                    },
                )
                .unwrap();
            dst.extend_from_slice(&(ct.len() as u32).to_le_bytes());
            dst.extend_from_slice(&ct);
        }
        dst.extend_from_slice(&0u32.to_le_bytes());
        dst
    }

    fn dec(enc: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, ImageCryptoError> {
        let mut out = Vec::new();
        decrypt_stream(Cursor::new(enc), &mut out, key, &AtomicBool::new(false)).map(|_| out)
    }

    fn enc_v2(plain: &[u8], key: &[u8; 32]) -> Vec<u8> {
        let mut enc = Vec::new();
        encrypt_stream(
            Cursor::new(plain),
            &mut enc,
            key,
            CS,
            &AtomicBool::new(false),
        )
        .unwrap();
        enc
    }

    fn sample() -> Vec<u8> {
        (0..(3 * CS + 100)).map(|i| (i % 251) as u8).collect()
    }

    #[test]
    fn v2_roundtrip_edge_sizes() {
        let key = [3u8; 32];
        for len in [0usize, 1, CS - 1, CS, CS + 1, 3 * CS, 3 * CS + 7] {
            let plain: Vec<u8> = (0..len).map(|i| (i % 253) as u8).collect();
            let enc = enc_v2(&plain, &key);
            assert_eq!(&enc[..8], ENC_MAGIC);
            let fr = frames_v2(&enc);
            assert_eq!(fr.len(), len.div_ceil(CS).max(1), "len={len}");
            // 마지막 프레임만 최종 플래그
            for (i, (p, _)) in fr.iter().enumerate() {
                let want = if i + 1 == fr.len() {
                    FLAG_FINAL
                } else {
                    FLAG_MORE
                };
                assert_eq!(enc[*p], want);
            }
            assert_eq!(dec(&enc, &key).unwrap(), plain, "len={len}");
        }
    }

    #[test]
    fn v2_rejects_boundary_truncation_attacks() {
        let key = [9u8; 32];
        let enc = enc_v2(&sample(), &key);
        let fr = frames_v2(&enc);
        assert_eq!(fr.len(), 4);
        for &(p, _) in &fr {
            // 청크 경계에서 자르기만
            assert!(matches!(
                dec(&enc[..p], &key),
                Err(ImageCryptoError::Truncated(_))
            ));
            // 경계에서 자르고 v1식 길이 0 끝 표식(4바이트) 붙이기 (감사 20번 공격)
            let mut a = enc[..p].to_vec();
            a.extend_from_slice(&0u32.to_le_bytes());
            assert!(dec(&a, &key).is_err());
            // 경계에서 자르고 v2식 "빈 최종 청크" 프레임을 위조해 붙이기(태그는 위조 불가)
            let mut b = enc[..p].to_vec();
            b.push(FLAG_FINAL);
            b.extend_from_slice(&(TAG_LEN as u32).to_le_bytes());
            b.extend_from_slice(&[0u8; TAG_LEN]);
            assert!(matches!(
                dec(&b, &key),
                Err(ImageCryptoError::AuthFailed(_))
            ));
        }
    }

    #[test]
    fn v2_rejects_middle_chunk_disguised_as_final() {
        let key = [9u8; 32];
        let enc = enc_v2(&sample(), &key);
        let fr = frames_v2(&enc);
        for (i, &(p, l)) in fr.iter().enumerate().take(fr.len() - 1) {
            // 중간 청크 플래그를 최종으로 바꾸고 그 뒤를 잘라냄
            let mut a = enc[..p + l].to_vec();
            a[p] = FLAG_FINAL;
            assert!(matches!(
                dec(&a, &key),
                Err(ImageCryptoError::AuthFailed(x)) if x == i as u64
            ));
        }
        // 최종 청크 플래그를 "계속"으로 바꾸기
        let (lp, _) = *fr.last().unwrap();
        let mut b = enc.clone();
        b[lp] = FLAG_MORE;
        assert!(dec(&b, &key).is_err());
        // 정의되지 않은 플래그 값
        let mut c = enc.clone();
        c[fr[0].0] = 7;
        assert!(matches!(
            dec(&c, &key),
            Err(ImageCryptoError::AuthFailed(0))
        ));
    }

    #[test]
    fn v2_rejects_reorder_drop_trailing_and_downgrade() {
        let key = [9u8; 32];
        let enc = enc_v2(&sample(), &key);
        let fr = frames_v2(&enc);
        let frame = |i: usize| &enc[fr[i].0..fr[i].0 + fr[i].1];
        let build = |order: &[usize]| {
            let mut v = enc[..HDR].to_vec();
            for &i in order {
                v.extend_from_slice(frame(i));
            }
            v
        };
        // 중간 청크 순서 바꾸기
        assert!(matches!(
            dec(&build(&[0, 2, 1, 3]), &key),
            Err(ImageCryptoError::AuthFailed(1))
        ));
        // 중간 청크 하나 빼기
        assert!(matches!(
            dec(&build(&[0, 1, 3]), &key),
            Err(ImageCryptoError::AuthFailed(2))
        ));
        // 최종 청크를 앞으로 당겨 붙이기
        assert!(matches!(
            dec(&build(&[0, 3]), &key),
            Err(ImageCryptoError::AuthFailed(1))
        ));
        // 최종 청크 뒤에 덧붙이기
        let mut d = enc.clone();
        d.extend_from_slice(&0u32.to_le_bytes());
        assert!(matches!(dec(&d, &key), Err(ImageCryptoError::TrailingData)));
        // 헤더를 v1로 바꾸는 다운그레이드
        let mut e = enc.clone();
        e[..8].copy_from_slice(ENC_MAGIC_V1);
        assert!(dec(&e, &key).is_err());
        // 알 수 없는 버전
        let mut f = enc.clone();
        f[4] = 3;
        assert!(matches!(dec(&f, &key), Err(ImageCryptoError::BadMagic)));
    }

    #[test]
    fn v1_compat_decrypts_existing_files() {
        let key = [0x42u8; 32];
        for len in [0usize, 1, CS, 3 * CS + 100] {
            let plain: Vec<u8> = (0..len).map(|i| (i % 251) as u8).collect();
            let v1 = encrypt_stream_v1(&plain, &key, CS);
            assert_eq!(dec(&v1, &key).unwrap(), plain, "len={len}");
            // v1 기존 거부 동작 유지: 끝 표식 누락, 틀린 키, 1비트 변조
            assert!(matches!(
                dec(&v1[..v1.len() - 4], &key),
                Err(ImageCryptoError::Truncated(_))
            ));
            if len > 0 {
                assert!(matches!(
                    dec(&v1, &[1u8; 32]),
                    Err(ImageCryptoError::AuthFailed(0))
                ));
                let mut t = v1.clone();
                t[HDR + 6] ^= 1;
                assert!(matches!(
                    dec(&t, &key),
                    Err(ImageCryptoError::AuthFailed(0))
                ));
            }
        }
        // 파일 경로(검증·복원)도 v1을 그대로 처리
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("old.qblk.enc");
        let out = d.path().join("old.dec.qblk");
        let plain = b"QBLKSTRM-legacy".repeat(700);
        fs::write(&src, encrypt_stream_v1(&plain, &key, DEFAULT_ENC_CHUNK)).unwrap();
        assert!(is_encrypted_image(&src));
        decrypt_file(&src, &out, &key, &AtomicBool::new(false)).unwrap();
        assert_eq!(fs::read(&out).unwrap(), plain);
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

    #[test]
    fn existing_output_is_never_overwritten_or_deleted() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("a.qblk");
        let dst = d.path().join("a.qblk.enc");
        fs::write(&src, b"QBLKSTRM-data").unwrap();
        fs::write(&dst, b"keep me").unwrap();
        let no = AtomicBool::new(false);
        assert!(encrypt_file(&src, &dst, &[1u8; 32], &no).is_err());
        assert!(decrypt_file(&src, &dst, &[1u8; 32], &no).is_err());
        assert_eq!(fs::read(&dst).unwrap(), b"keep me");
    }
}
