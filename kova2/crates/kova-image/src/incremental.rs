//! `.qblk` 블록 단위 증분 이미지 & 체인 복원.
//! - 커널 드라이버 없이 사이드카 블록 해시 맵(`.qmap.json`)과 체인 메타(`.qchain.json`)로 증분 캡처·복원을 구현한다.
//! - 개별 증분 파일도 표준 `QBLKSTRM` v1 포맷을 유지하므로 기존 검증기와 100% 호환된다.
use crate::bitmap::{allocated_runs, RunParams};
use crate::qblk::{restore_to, verify, Header, QblkError, Stats, Writer};
use crate::restore::{check_target, RestoreError};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

pub const DEFAULT_BLOCK_CLUSTERS: u64 = 256; // 4KiB 클러스터 기준 1MiB 단위
pub const MAX_CHAIN_DEPTH: usize = 64;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BlockMap {
    pub version: u32,
    pub cluster_size: u32,
    pub total_clusters: u64,
    pub block_clusters: u64,
    /// 블록 시작 LCN -> CRC32 해시
    pub hashes: BTreeMap<u64, u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ChainMeta {
    pub version: u32,
    /// 같은 폴더 내 부모 `.qblk` 파일명
    pub parent: String,
    pub changed_clusters: u64,
    pub skipped_clusters: u64,
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct IncrementalStats {
    pub changed_clusters: u64,
    pub skipped_clusters: u64,
    pub chunks_written: u64,
}

pub fn map_path_for(image: &Path) -> PathBuf {
    PathBuf::from(format!("{}.qmap.json", image.display()))
}

pub fn chain_path_for(image: &Path) -> PathBuf {
    PathBuf::from(format!("{}.qchain.json", image.display()))
}

pub fn save_block_map(image: &Path, map: &BlockMap) -> Result<(), QblkError> {
    let json = serde_json::to_string_pretty(map)
        .map_err(|e| QblkError::Io(std::io::Error::other(e.to_string())))?;
    fs::write(map_path_for(image), json)?;
    Ok(())
}

pub fn load_block_map(image: &Path) -> Result<BlockMap, QblkError> {
    let raw = fs::read_to_string(map_path_for(image))?;
    serde_json::from_str(&raw).map_err(|e| QblkError::Io(std::io::Error::other(e.to_string())))
}

pub fn save_chain_meta(image: &Path, meta: &ChainMeta) -> Result<(), QblkError> {
    let json = serde_json::to_string_pretty(meta)
        .map_err(|e| QblkError::Io(std::io::Error::other(e.to_string())))?;
    fs::write(chain_path_for(image), json)?;
    Ok(())
}

pub fn load_chain_meta(image: &Path) -> Result<Option<ChainMeta>, QblkError> {
    let p = chain_path_for(image);
    if !p.exists() {
        return Ok(None);
    }
    let raw = fs::read_to_string(p)?;
    let meta = serde_json::from_str(&raw)
        .map_err(|e| QblkError::Io(std::io::Error::other(e.to_string())))?;
    Ok(Some(meta))
}

/// `base_map`이 None이면 전체 풀 이미지를 쓰고 블록 해시 맵을 만든다.
/// `base_map`이 Some이면 해시가 달라진 블록만 `.qblk`에 기록(증분)하고 갱신된 블록 맵을 돌려준다.
#[allow(clippy::too_many_arguments)]
pub fn write_incremental_image<R: Read + Seek, W: Write + Seek>(
    src: &mut R,
    out: W,
    header: &mut Header,
    bitmap: &[u8],
    base_map: Option<&BlockMap>,
    level: i32,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Stats, IncrementalStats, BlockMap, W), QblkError> {
    let cs = header.cluster_size as u64;
    let total = header.total_clusters;
    let block_clusters = base_map
        .map(|m| m.block_clusters)
        .unwrap_or(DEFAULT_BLOCK_CLUSTERS)
        .max(1);

    // 블록 단위로 분할된 할당 런 목록
    let runs = allocated_runs(
        bitmap,
        RunParams {
            cluster_size: cs,
            max_chunk_bytes: block_clusters * cs,
            max_coalesce_gap_bytes: 0,
            ..RunParams::default()
        },
    );

    let mut w = Writer::new(BufWriter::new(out), header, level)?;
    let mut st = Stats {
        total_raw_bytes: runs
            .iter()
            .filter(|(lcn, _)| *lcn < total)
            .map(|(lcn, count)| (*count).min(total - *lcn) * cs)
            .sum(),
        ..Stats::default()
    };
    let mut inc = IncrementalStats::default();
    let mut new_hashes = BTreeMap::new();
    let mut buf = Vec::new();

    for (lcn, count) in runs {
        if cancel.load(Ordering::Relaxed) {
            return Err(QblkError::Cancelled);
        }
        if lcn >= total {
            continue;
        }
        let count = count.min(total - lcn);
        buf.resize((count * cs) as usize, 0);
        src.seek(SeekFrom::Start(lcn * cs))?;
        src.read_exact(&mut buf)?;
        let hash = crc32fast::hash(&buf);
        new_hashes.insert(lcn, hash);

        let unchanged = base_map
            .and_then(|m| {
                if m.cluster_size == header.cluster_size && m.total_clusters == total {
                    m.hashes.get(&lcn).copied()
                } else {
                    None
                }
            })
            .is_some_and(|prev| prev == hash);

        if unchanged {
            inc.skipped_clusters += count;
        } else {
            w.write_chunk(lcn, &buf)?;
            inc.changed_clusters += count;
            inc.chunks_written += 1;
            st.chunks += 1;
            st.clusters += count;
            st.raw_bytes += count * cs;
        }
        progress(&st);
    }

    header.used_clusters = w.clusters_written();
    let bw = w.finish()?;
    let mut f = bw.into_inner().map_err(|e| e.into_error())?;
    f.seek(SeekFrom::Start(0))?;
    f.write_all(&header.to_bytes())?;
    f.flush()?;

    let new_map = BlockMap {
        version: 1,
        cluster_size: header.cluster_size,
        total_clusters: total,
        block_clusters,
        hashes: new_hashes,
    };
    Ok((st, inc, new_map, f))
}

/// `.qchain.json`을 거슬러 올라가 `[base.qblk, inc1.qblk, ..., target.qblk]` 복원 순서를 반환한다.
pub fn resolve_chain(image: &Path) -> Result<Vec<PathBuf>, QblkError> {
    let mut chain = Vec::new();
    let mut visited = HashSet::new();
    let mut cur = image.to_path_buf();
    loop {
        if chain.len() >= MAX_CHAIN_DEPTH {
            return Err(QblkError::Io(std::io::Error::other(
                "증분 체인 깊이가 상한(64)을 초과했습니다",
            )));
        }
        if !cur.is_file() {
            return Err(QblkError::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("체인 이미지 파일이 없습니다: {}", cur.display()),
            )));
        }
        let canon = fs::canonicalize(&cur).unwrap_or_else(|_| cur.clone());
        if !visited.insert(canon) {
            return Err(QblkError::Io(std::io::Error::other(
                "증분 체인에 순환 참조가 있습니다",
            )));
        }
        chain.push(cur.clone());
        let Some(meta) = load_chain_meta(&cur)? else {
            break;
        };
        if meta.parent.is_empty()
            || meta.parent.contains(['/', '\\'])
            || meta.parent == "."
            || meta.parent == ".."
        {
            return Err(QblkError::Io(std::io::Error::other(format!(
                "잘못된 부모 이미지 이름: {}",
                meta.parent
            ))));
        }
        let parent_dir = cur.parent().unwrap_or_else(|| Path::new("."));
        cur = parent_dir.join(meta.parent);
    }
    chain.reverse();
    Ok(chain)
}

/// 체인의 모든 `.qblk` 파일이 손상되지 않았는지 순서대로 검증한다.
pub fn verify_chain(
    image: &Path,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<Vec<(PathBuf, Header, Stats)>, QblkError> {
    let chain = resolve_chain(image)?;
    let mut results = Vec::with_capacity(chain.len());
    for p in chain {
        let src = BufReader::with_capacity(8 << 20, File::open(&p)?);
        let (h, s) = verify(src, cancel, progress)?;
        results.push((p, h, s));
    }
    Ok(results)
}

/// 풀+증분 체인을 `base`부터 최신까지 순서대로 새 파일(`target`)에 복원한다.
pub fn restore_chain_to_file(
    image: &Path,
    target: &Path,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), RestoreError> {
    check_target(target)?;
    let chain = resolve_chain(image)?;
    let f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    let mut out = BufWriter::with_capacity(8 << 20, f);
    let mut last_header = None;
    let mut total_stats = Stats::default();
    for p in &chain {
        let src = BufReader::with_capacity(8 << 20, File::open(p)?);
        match restore_to(src, &mut out, cancel, progress) {
            Ok((h, s)) => {
                total_stats.chunks += s.chunks;
                total_stats.clusters += s.clusters;
                total_stats.raw_bytes += s.raw_bytes;
                total_stats.compressed_bytes += s.compressed_bytes;
                last_header = Some(h);
            }
            Err(e) => {
                drop(out);
                let _ = fs::remove_file(target);
                return Err(e.into());
            }
        }
    }
    out.flush()?;
    let h = last_header.expect("체인은 최소 1개 이상");
    let full = h.total_clusters * h.cluster_size as u64;
    let f = out.into_inner().map_err(|e| e.into_error())?;
    if f.metadata()?.len() < full {
        f.set_len(full)?;
    }
    Ok((h, total_stats))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn incremental_chain_captures_only_changed_blocks_and_restores_exact_bytes() {
        let d = tempfile::tempdir().unwrap();
        let cs = 4096usize;
        let total_clusters = 8u64;
        // 클러스터 0..8 모두 할당됨
        let bitmap = [0xFFu8];
        let mut disk = vec![0u8; cs * total_clusters as usize];
        for i in 0..8 {
            disk[i * cs..(i + 1) * cs].fill((i as u8) + 1);
        }
        let no = AtomicBool::new(false);

        // 1) Base 풀 이미지 (블록 단위 = 2클러스터씩)
        let base_path = d.path().join("base.qblk");
        let mut h1 = Header {
            version: 1,
            cluster_size: cs as u32,
            total_clusters,
            used_clusters: 0,
            vsn: "AABB-CCDD".into(),
            source_label: "DATA".into(),
        };
        {
            let f = File::create(&base_path).unwrap();
            let (_, inc1, mut map1, _) = write_incremental_image(
                &mut Cursor::new(&disk),
                f,
                &mut h1,
                &bitmap,
                None,
                3,
                &no,
                &|_| {},
            )
            .unwrap();
            assert_eq!(inc1.changed_clusters, 8);
            assert_eq!(inc1.skipped_clusters, 0);
            map1.block_clusters = 2;
            save_block_map(&base_path, &map1).unwrap();
        }

        // 2) 블록 단위(2클러스터)로 다시 base를 생성해 정확한 증분 비교 테스트
        let base2_path = d.path().join("base2.qblk");
        let base2_map = {
            let f = File::create(&base2_path).unwrap();
            let dummy = BlockMap {
                version: 1,
                cluster_size: cs as u32,
                total_clusters,
                block_clusters: 2,
                hashes: BTreeMap::new(),
            };
            let (_, inc, map, _) = write_incremental_image(
                &mut Cursor::new(&disk),
                f,
                &mut h1.clone(),
                &bitmap,
                Some(&dummy),
                3,
                &no,
                &|_| {},
            )
            .unwrap();
            assert_eq!(inc.changed_clusters, 8);
            save_block_map(&base2_path, &map).unwrap();
            map
        };

        // 3) 클러스터 4..6 (1개 블록=2클러스터)만 수정 후 1차 증분
        disk[4 * cs..6 * cs].fill(0xEE);
        let inc1_path = d.path().join("inc1.qblk");
        let inc1_map = {
            let f = File::create(&inc1_path).unwrap();
            let (_, inc, map, _) = write_incremental_image(
                &mut Cursor::new(&disk),
                f,
                &mut h1.clone(),
                &bitmap,
                Some(&base2_map),
                3,
                &no,
                &|_| {},
            )
            .unwrap();
            assert_eq!(inc.changed_clusters, 2);
            assert_eq!(inc.skipped_clusters, 6);
            save_block_map(&inc1_path, &map).unwrap();
            save_chain_meta(
                &inc1_path,
                &ChainMeta {
                    version: 1,
                    parent: "base2.qblk".into(),
                    changed_clusters: inc.changed_clusters,
                    skipped_clusters: inc.skipped_clusters,
                },
            )
            .unwrap();
            map
        };

        // 4) 클러스터 0..2 수정 후 2차 증분
        disk[0..2 * cs].fill(0xAA);
        let inc2_path = d.path().join("inc2.qblk");
        {
            let f = File::create(&inc2_path).unwrap();
            let (_, inc, map, _) = write_incremental_image(
                &mut Cursor::new(&disk),
                f,
                &mut h1.clone(),
                &bitmap,
                Some(&inc1_map),
                3,
                &no,
                &|_| {},
            )
            .unwrap();
            assert_eq!(inc.changed_clusters, 2);
            assert_eq!(inc.skipped_clusters, 6);
            save_block_map(&inc2_path, &map).unwrap();
            save_chain_meta(
                &inc2_path,
                &ChainMeta {
                    version: 1,
                    parent: "inc1.qblk".into(),
                    changed_clusters: inc.changed_clusters,
                    skipped_clusters: inc.skipped_clusters,
                },
            )
            .unwrap();
        }

        // 체인 검증 & 복원 → 최신 disk 바이트와 100% 일치
        let verified = verify_chain(&inc2_path, &no, &|_| {}).unwrap();
        assert_eq!(verified.len(), 3);
        let out_raw = d.path().join("restored.img");
        restore_chain_to_file(&inc2_path, &out_raw, &no, &|_| {}).unwrap();
        assert_eq!(fs::read(&out_raw).unwrap(), disk);
    }

    #[test]
    fn resolve_chain_rejects_missing_parent_and_cycles() {
        let d = tempfile::tempdir().unwrap();
        let a = d.path().join("a.qblk");
        let b = d.path().join("b.qblk");
        fs::write(&a, b"x").unwrap();
        fs::write(&b, b"y").unwrap();
        // 누락 부모
        save_chain_meta(
            &a,
            &ChainMeta {
                version: 1,
                parent: "missing.qblk".into(),
                changed_clusters: 0,
                skipped_clusters: 0,
            },
        )
        .unwrap();
        assert!(resolve_chain(&a).is_err());

        // 순환 참조 a -> b -> a
        save_chain_meta(
            &a,
            &ChainMeta {
                version: 1,
                parent: "b.qblk".into(),
                changed_clusters: 0,
                skipped_clusters: 0,
            },
        )
        .unwrap();
        save_chain_meta(
            &b,
            &ChainMeta {
                version: 1,
                parent: "a.qblk".into(),
                changed_clusters: 0,
                skipped_clusters: 0,
            },
        )
        .unwrap();
        assert!(resolve_chain(&a).is_err());
    }
}
