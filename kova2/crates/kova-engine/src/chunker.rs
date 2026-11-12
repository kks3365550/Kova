use std::fs::File;
use std::io;
use std::path::Path;
use fastcdc::FastCDC;
use memmap2::Mmap;

const ONE_MIB: usize = 1024 * 1024;
const MIN_CHUNK_SIZE: usize = 256 * 1024; // 256 KB
const AVG_CHUNK_SIZE: usize = 1024 * 1024; // 1 MB
const MAX_CHUNK_SIZE: usize = 4 * 1024 * 1024; // 4 MB

#[derive(Debug, Clone)]
pub struct Chunk {
    pub offset: u64,
    pub length: usize,
    pub hash: String,
    pub data: Vec<u8>,
}

pub fn chunk_slice(data: &[u8]) -> Vec<Chunk> {
    if data.is_empty() {
        return Vec::new();
    }

    if data.len() < ONE_MIB {
        let hash = blake3::hash(data).to_hex().to_string();
        return vec![Chunk {
            offset: 0,
            length: data.len(),
            hash,
            data: data.to_vec(),
        }];
    }

    let chunker = FastCDC::new(data, MIN_CHUNK_SIZE, AVG_CHUNK_SIZE, MAX_CHUNK_SIZE);
    let mut chunks = Vec::new();

    for entry in chunker {
        let slice = &data[entry.offset..entry.offset + entry.length];
        let hash = blake3::hash(slice).to_hex().to_string();
        chunks.push(Chunk {
            offset: entry.offset as u64,
            length: entry.length,
            hash,
            data: slice.to_vec(),
        });
    }

    chunks
}

pub fn chunk_file(path: &Path) -> io::Result<Vec<Chunk>> {
    let file = File::open(path)?;
    let metadata = file.metadata()?;
    let file_len = metadata.len();

    if file_len == 0 {
        return Ok(Vec::new());
    }

    // Safety: Memory mapping read-only file
    let mmap = unsafe { Mmap::map(&file)? };
    Ok(chunk_slice(&mmap))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pseudo_random(len: usize) -> Vec<u8> {
        let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
        (0..len)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x >> 24) as u8
            })
            .collect()
    }

    #[test]
    fn empty_input_has_no_chunks() {
        assert!(chunk_slice(&[]).is_empty());
    }

    #[test]
    fn small_input_is_single_chunk() {
        let data = pseudo_random(1000);
        let chunks = chunk_slice(&data);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].offset, 0);
        assert_eq!(chunks[0].data, data);
    }

    #[test]
    fn chunks_are_contiguous_and_reassemble() {
        let data = pseudo_random(12 * ONE_MIB + 12345);
        let chunks = chunk_slice(&data);
        assert!(chunks.len() > 1);

        let mut expected_offset = 0u64;
        let mut rebuilt = Vec::with_capacity(data.len());
        for c in &chunks {
            assert_eq!(c.offset, expected_offset);
            assert_eq!(c.length, c.data.len());
            assert_eq!(c.hash, blake3::hash(&c.data).to_hex().to_string());
            expected_offset += c.length as u64;
            rebuilt.extend_from_slice(&c.data);
        }
        assert_eq!(rebuilt, data);
    }

    #[test]
    fn chunking_is_deterministic() {
        let data = pseudo_random(5 * ONE_MIB);
        let a: Vec<String> = chunk_slice(&data).into_iter().map(|c| c.hash).collect();
        let b: Vec<String> = chunk_slice(&data).into_iter().map(|c| c.hash).collect();
        assert_eq!(a, b);
    }
}
