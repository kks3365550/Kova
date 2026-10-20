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
