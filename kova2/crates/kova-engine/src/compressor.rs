use std::io::{self, Write};

pub fn compress(data: &[u8], level: i32) -> io::Result<Vec<u8>> {
    if data.len() >= 128 * 1024 {
        // 128KB 이상 대용량 블록은 풀코어 멀티스레드 압축 적용 (threads = 0: 라이브러리 자동 풀코어)
        let mut encoder = zstd::stream::write::Encoder::new(Vec::with_capacity(data.len()), level)?;
        let _ = encoder.multithread(0);
        encoder.write_all(data)?;
        encoder.finish()
    } else {
        zstd::encode_all(data, level)
    }
}

pub fn decompress(compressed_data: &[u8]) -> io::Result<Vec<u8>> {
    zstd::decode_all(compressed_data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_restores_original() {
        let data: Vec<u8> = (0..100_000u32).map(|i| (i % 97) as u8).collect();
        let packed = compress(&data, 3).unwrap();
        assert!(packed.len() < data.len());
        assert_eq!(decompress(&packed).unwrap(), data);
    }

    #[test]
    fn large_multithreaded_roundtrip() {
        // 128KB 이상 풀코어 멀티스레드 경로 검증
        let data: Vec<u8> = (0..500_000u32).map(|i| (i % 127) as u8).collect();
        let packed = compress(&data, 3).unwrap();
        assert!(packed.len() < data.len());
        assert_eq!(decompress(&packed).unwrap(), data);
    }

    #[test]
    fn empty_roundtrip() {
        let packed = compress(&[], 3).unwrap();
        assert!(decompress(&packed).unwrap().is_empty());
    }

    #[test]
    fn corrupt_input_is_an_error() {
        assert!(decompress(b"this is not a zstd frame").is_err());
    }
}
