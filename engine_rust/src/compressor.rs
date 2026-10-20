use std::io;

pub fn compress(data: &[u8], level: i32) -> io::Result<Vec<u8>> {
    zstd::encode_all(data, level)
}

pub fn decompress(compressed_data: &[u8]) -> io::Result<Vec<u8>> {
    zstd::decode_all(compressed_data)
}
