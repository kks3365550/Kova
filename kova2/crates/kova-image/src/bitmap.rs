//! NTFS $Bitmap → 청크 구간 분할 (core/block_stream.py `get_allocated_cluster_runs` 이식).
//! 결과가 파이썬과 같아야 하므로 구조와 특이 동작(갭 병합 시 빈 클러스터 포함, 최소 청크 바닥 보정)을 그대로 따른다.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunParams {
    pub cluster_size: u64,
    pub max_chunk_bytes: u64,
    pub max_coalesce_gap_bytes: u64,
    pub min_chunk_bytes: u64,
    /// 0 이면 2단계 변속 없음
    pub two_stage_threshold_bytes: u64,
}

impl Default for RunParams {
    fn default() -> Self {
        Self {
            cluster_size: 4096,
            max_chunk_bytes: 16 << 20,
            max_coalesce_gap_bytes: 0,
            min_chunk_bytes: 0,
            two_stage_threshold_bytes: 0,
        }
    }
}

/// 할당된 클러스터를 (lcn, cluster_count) 구간 목록으로 묶는다. 빈 입력이면 빈 목록.
pub fn allocated_runs(bitmap: &[u8], p: RunParams) -> Vec<(u64, u64)> {
    if bitmap.is_empty() || p.cluster_size == 0 {
        return vec![];
    }
    let cs = p.cluster_size;
    let threshold = if p.two_stage_threshold_bytes > 0 {
        p.two_stage_threshold_bytes / cs
    } else {
        0
    };
    let def_max = (p.max_chunk_bytes / cs).max(1);
    let def_gap = p.max_coalesce_gap_bytes / cs;
    let def_min = def_max.min(p.min_chunk_bytes / cs);
    let s2_max = ((8u64 << 20) / cs).max(1);
    let (s2_gap, s2_min) = (0u64, 0u64);
    let stage2 = |lcn: u64| threshold > 0 && lcn >= threshold;

    let split = |raw: &mut Vec<(u64, u64)>, start: u64, end: u64, max: u64| {
        let (mut rem, mut c) = (end - start, start);
        while rem > 0 {
            let take = rem.min(max);
            raw.push((c, take));
            c += take;
            rem -= take;
        }
    };

    let mut raw: Vec<(u64, u64)> = vec![];
    let mut cur: Option<(u64, u64)> = None; // (start, end exclusive)
    for (byte_idx, &b) in bitmap.iter().enumerate() {
        if b == 0 {
            continue;
        }
        for bit in 0..8u64 {
            if (b >> bit) & 1 == 0 {
                continue;
            }
            let lcn = byte_idx as u64 * 8 + bit;
            let (max, gap_lim) = if stage2(lcn) {
                (s2_max, s2_gap)
            } else {
                (def_max, def_gap)
            };
            match cur {
                None => cur = Some((lcn, lcn + 1)),
                Some((start, end)) => {
                    let gap = lcn - end;
                    if (gap == 0 || (gap_lim > 0 && gap <= gap_lim)) && lcn + 1 - start <= max {
                        cur = Some((start, lcn + 1));
                    } else {
                        split(&mut raw, start, end, max);
                        cur = Some((lcn, lcn + 1));
                    }
                }
            }
        }
    }
    if let Some((start, end)) = cur {
        let max = if stage2(start) { s2_max } else { def_max };
        split(&mut raw, start, end, max);
    }

    // 2차 통합 패스 (최소 청크 바닥 / 갭 병합)
    let mut runs: Vec<(u64, u64)> = vec![];
    for (lcn, count) in raw {
        let Some(&(pl, pc)) = runs.last() else {
            runs.push((lcn, count));
            continue;
        };
        let gap = lcn - (pl + pc); // 파이썬과 같이 정수(원래 오름차순이라 음수 아님)
        let (max, gap_lim, min) = if stage2(lcn) {
            (s2_max, s2_gap, s2_min)
        } else {
            (def_max, def_gap, def_min)
        };
        let merged = pc + gap + count;
        #[allow(clippy::if_same_then_else)] // 병합 조건을 구 Python 구현과 1:1 로 유지
        if (gap == 0 || (gap_lim > 0 && gap <= gap_lim)) && merged <= max {
            *runs.last_mut().unwrap() = (pl, merged);
        } else if min > 0 && pc < min && merged <= max {
            *runs.last_mut().unwrap() = (pl, merged);
        } else if min > 0 && count < min && merged <= max {
            *runs.last_mut().unwrap() = (pl, merged);
        } else {
            runs.push((lcn, count));
        }
    }

    // 잔여 초미세 청크 최종 바닥 보정
    let limit = bitmap.len() as u64 * 8;
    runs.into_iter()
        .map(|(lcn, count)| {
            let min = if stage2(lcn) { s2_min } else { def_min };
            if min > 0 && count < min {
                (lcn, min.min(limit - lcn))
            } else {
                (lcn, count)
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bm(bits: &[u64], len: usize) -> Vec<u8> {
        let mut v = vec![0u8; len];
        for &b in bits {
            v[(b / 8) as usize] |= 1 << (b % 8);
        }
        v
    }

    #[test]
    fn contiguous_and_split_runs() {
        let b = bm(&[0, 1, 2, 10, 11], 4);
        assert_eq!(
            allocated_runs(&b, RunParams::default()),
            vec![(0, 3), (10, 2)]
        );
        // 최대 청크(2클러스터)로 쪼갠다
        let p = RunParams {
            max_chunk_bytes: 2 * 4096,
            ..Default::default()
        };
        assert_eq!(
            allocated_runs(&bm(&[0, 1, 2, 3, 4], 2), p),
            vec![(0, 2), (2, 2), (4, 1)]
        );
        assert!(allocated_runs(&[], RunParams::default()).is_empty());
        assert!(allocated_runs(&[0, 0], RunParams::default()).is_empty());
    }

    #[test]
    fn gap_coalescing_includes_the_gap() {
        let p = RunParams {
            max_coalesce_gap_bytes: 3 * 4096,
            ..Default::default()
        };
        // 0..1 과 4 사이 갭 3 → 한 구간(0..5)으로 합치며 빈 클러스터 3개를 포함
        assert_eq!(allocated_runs(&bm(&[0, 4], 2), p), vec![(0, 5)]);
        // 갭이 한도(3)보다 크면 분리
        assert_eq!(allocated_runs(&bm(&[0, 5], 2), p), vec![(0, 1), (5, 1)]);
    }

    /// 파이썬 구현이 만든 정답(tests/fixtures/bitmap_cases.json)과 무작위 입력 전체에서 같아야 한다.
    #[test]
    fn matches_python_on_random_cases() {
        let raw = include_str!("../tests/fixtures/bitmap_cases.json");
        let cases: Vec<serde_json::Value> = serde_json::from_str(raw).unwrap();
        assert!(cases.len() >= 100);
        for (i, c) in cases.iter().enumerate() {
            let g = |k: &str| c[k].as_u64().unwrap();
            let p = RunParams {
                cluster_size: g("cluster_size"),
                max_chunk_bytes: g("max_chunk_bytes"),
                max_coalesce_gap_bytes: g("max_coalesce_gap_bytes"),
                min_chunk_bytes: g("min_chunk_bytes"),
                two_stage_threshold_bytes: g("two_stage_threshold_bytes"),
            };
            let bytes: Vec<u8> = (0..c["bitmap_hex"].as_str().unwrap().len() / 2)
                .map(|k| {
                    u8::from_str_radix(&c["bitmap_hex"].as_str().unwrap()[2 * k..2 * k + 2], 16)
                        .unwrap()
                })
                .collect();
            let want: Vec<(u64, u64)> = c["runs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| (r[0].as_u64().unwrap(), r[1].as_u64().unwrap()))
                .collect();
            assert_eq!(allocated_runs(&bytes, p), want, "케이스 {i}: {p:?}");
        }
    }
}
