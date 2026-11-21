use fastcdc::FastCDC;
use memmap2::Mmap;
use rayon::prelude::*;
use std::fs::File;
use std::io;
use std::path::Path;

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

/// 한 번에 메모리에 올리는 청크 묶음의 최대 크기(큰 파일도 이 정도만 쓴다)
const BATCH_BYTES: usize = 64 * 1024 * 1024;

/// 먼저 복사한 뒤 그 복사본으로 해시를 낸다. 원본(메모리 맵)이 그 사이에 바뀌어도
/// 저장되는 바이트와 해시가 어긋나지 않는다(해시가 틀린 블롭이 중복제거로 퍼지는 것을 막는다).
fn make_chunk(data: &[u8], offset: usize, length: usize) -> Chunk {
    let copy = data[offset..offset + length].to_vec();
    Chunk {
        offset: offset as u64,
        length: copy.len(),
        hash: blake3::hash(&copy).to_hex().to_string(),
        data: copy,
    }
}

fn cut_points(data: &[u8]) -> Vec<(usize, usize)> {
    if data.is_empty() {
        return Vec::new();
    }
    if data.len() < ONE_MIB {
        return vec![(0, data.len())];
    }
    FastCDC::new(data, MIN_CHUNK_SIZE, AVG_CHUNK_SIZE, MAX_CHUNK_SIZE)
        .map(|e| (e.offset, e.length))
        .collect()
}

pub fn chunk_slice(data: &[u8]) -> Vec<Chunk> {
    // 경계 탐색은 순서대로(가벼움), 복사·해시는 청크끼리 독립이라 병렬로 한다.
    // 큰 파일 하나도 여러 코어를 쓰며 결과 순서(offset 오름차순)는 유지된다.
    cut_points(data)
        .par_iter()
        .map(|&(offset, length)| make_chunk(data, offset, length))
        .collect()
}

/// 파일을 청크로 나눠 묶음(최대 `BATCH_BYTES`)마다 `f` 에 건넨다. 파일 전체를 메모리에 올리지 않는다.
/// 읽기 오류는 `"read: ..."`, `f` 의 오류는 그대로 돌려준다.
pub fn chunk_file_batches(
    path: &Path,
    f: impl FnMut(Vec<Chunk>) -> Result<(), String>,
) -> Result<(), String> {
    batches_with_limit(path, BATCH_BYTES, f)
}

fn batches_with_limit(
    path: &Path,
    limit: usize,
    mut f: impl FnMut(Vec<Chunk>) -> Result<(), String>,
) -> Result<(), String> {
    let file = File::open(path).map_err(|e| format!("read: {e}"))?;
    let file_len = file.metadata().map_err(|e| format!("read: {e}"))?.len();
    if file_len == 0 {
        return Ok(());
    }
    // Safety: Memory mapping read-only file
    let mmap = unsafe { Mmap::map(&file) }.map_err(|e| format!("read: {e}"))?;
    let cuts = cut_points(&mmap);
    let mut start = 0;
    while start < cuts.len() {
        let mut end = start;
        let mut bytes = 0usize;
        while end < cuts.len() && (end == start || bytes + cuts[end].1 <= limit) {
            bytes += cuts[end].1;
            end += 1;
        }
        let batch: Vec<Chunk> = cuts[start..end]
            .par_iter()
            .map(|&(offset, length)| make_chunk(&mmap, offset, length))
            .collect();
        f(batch)?;
        start = end;
    }
    Ok(())
}

pub fn chunk_file(path: &Path) -> io::Result<Vec<Chunk>> {
    let mut out = Vec::new();
    chunk_file_batches(path, |b| {
        out.extend(b);
        Ok(())
    })
    .map_err(io::Error::other)?;
    Ok(out)
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

    /// 파일 청킹은 묶음 크기 한도를 지키며(큰 파일을 통째로 메모리에 올리지 않음),
    /// 결과는 통째 청킹과 같고, 모든 청크의 해시는 그 청크가 실제로 담은 바이트의 해시다.
    #[test]
    fn file_batches_are_bounded_and_hash_matches_copied_data() {
        let data = pseudo_random(12 * ONE_MIB + 777);
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("f.bin");
        std::fs::write(&p, &data).unwrap();
        let limit = 3 * ONE_MIB;
        let mut got = Vec::new();
        let mut batches = 0;
        batches_with_limit(&p, limit, |b| {
            let bytes: usize = b.iter().map(|c| c.length).sum();
            assert!(b.len() == 1 || bytes <= limit, "묶음이 한도 초과: {bytes}");
            batches += 1;
            got.extend(b);
            Ok(())
        })
        .unwrap();
        assert!(batches > 1);
        for c in &got {
            assert_eq!(c.length, c.data.len());
            assert_eq!(c.hash, blake3::hash(&c.data).to_hex().to_string());
        }
        let whole: Vec<String> = chunk_slice(&data).into_iter().map(|c| c.hash).collect();
        let streamed: Vec<String> = got.into_iter().map(|c| c.hash).collect();
        assert_eq!(whole, streamed);
        // 콜백 오류는 그대로 전파되고 이후 묶음은 처리하지 않는다
        let mut calls = 0;
        let r = batches_with_limit(&p, limit, |_| {
            calls += 1;
            Err("stop".into())
        });
        assert_eq!((r, calls), (Err("stop".to_string()), 1));
        assert!(chunk_file(&d.path().join("missing")).is_err());
    }

    #[test]
    fn chunking_is_deterministic() {
        let data = pseudo_random(5 * ONE_MIB);
        let a: Vec<String> = chunk_slice(&data).into_iter().map(|c| c.hash).collect();
        let b: Vec<String> = chunk_slice(&data).into_iter().map(|c| c.hash).collect();
        assert_eq!(a, b);
    }
}
