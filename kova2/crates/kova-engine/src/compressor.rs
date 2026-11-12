use std::io;

pub fn compress(data: &[u8], level: i32) -> io::Result<Vec<u8>> {
    zstd::encode_all(data, level)
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
    fn empty_roundtrip() {
        let packed = compress(&[], 3).unwrap();
        assert!(decompress(&packed).unwrap().is_empty());
    }

    #[test]
    fn corrupt_input_is_an_error() {
        assert!(decompress(b"this is not a zstd frame").is_err());
    }
}
