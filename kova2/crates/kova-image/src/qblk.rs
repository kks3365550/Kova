use std::fmt;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::sync::atomic::{AtomicBool, Ordering};

pub const MAGIC: &[u8; 8] = b"QBLKSTRM";
pub const VERSION: u16 = 1;
pub const HEADER_SIZE: usize = 82;
pub const CHUNK_HEADER_SIZE: usize = 24;
/// 청크 하나의 압축 해제 크기 상한(손상된 크기 필드로 메모리를 폭주시키지 않기 위함). 파이썬 기본 청크는 8MiB.
pub const MAX_CHUNK_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug)]
pub enum QblkError {
    Io(io::Error),
    BadMagic,
    BadHeaderCrc {
        stored: u32,
        calc: u32,
    },
    UnsupportedVersion(u16),
    BadHeaderField(&'static str),
    TruncatedChunkHeader {
        offset: u64,
        got: usize,
    },
    TruncatedChunkData {
        offset: u64,
        want: u64,
        got: u64,
    },
    ChunkTooLarge {
        offset: u64,
        size: u64,
    },
    ChunkSizeMismatch {
        offset: u64,
        cluster_count: u32,
        uncompressed: u32,
        cluster_size: u32,
    },
    Decompress {
        offset: u64,
        reason: String,
    },
    CrcMismatch {
        offset: u64,
        lcn: u64,
    },
    UsedClustersMismatch {
        header: u64,
        found: u64,
    },
    OutOfRange {
        lcn: u64,
        cluster_count: u32,
        total_clusters: u64,
    },
    Cancelled,
}
impl fmt::Display for QblkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        use QblkError::*;
        match self {
            Io(e) => write!(f, "입출력 오류: {e}"),
            BadMagic => write!(f, "QBLK 이미지가 아님(매직 불일치)"),
            BadHeaderCrc { stored, calc } => write!(f, "헤더 CRC32 불일치(저장 {stored:#x}, 계산 {calc:#x})"),
            UnsupportedVersion(v) => write!(f, "지원하지 않는 버전: {v}"),
            BadHeaderField(n) => write!(f, "헤더 값이 올바르지 않음: {n}"),
            TruncatedChunkHeader { offset, got } => write!(f, "파일이 청크 헤더 중간에서 끊김(오프셋 {offset}, {got}바이트만 있음)"),
            TruncatedChunkData { offset, want, got } => write!(f, "청크 데이터가 끊김(오프셋 {offset}, 기대 {want}, 실제 {got})"),
            ChunkTooLarge { offset, size } => write!(f, "청크 크기가 비정상(오프셋 {offset}, {size}바이트)"),
            ChunkSizeMismatch { offset, cluster_count, uncompressed, cluster_size } => write!(
                f, "청크 크기 불일치(오프셋 {offset}: {cluster_count}클러스터×{cluster_size} ≠ {uncompressed})"),
            Decompress { offset, reason } => write!(f, "압축 해제 실패(오프셋 {offset}): {reason}"),
            CrcMismatch { offset, lcn } => write!(f, "청크 CRC32 불일치(오프셋 {offset}, LCN {lcn})"),
            UsedClustersMismatch { header, found } => write!(f, "클러스터 합계 불일치(헤더 {header}, 실제 {found}): 끝부분 유실 가능"),
            OutOfRange { lcn, cluster_count, total_clusters } => write!(f, "청크가 볼륨 범위를 벗어남(LCN {lcn}+{cluster_count} > {total_clusters})"),
            Cancelled => write!(f, "취소됨"),
        }
    }
}
impl std::error::Error for QblkError {}
impl From<io::Error> for QblkError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub version: u16,
    pub cluster_size: u32,
    pub total_clusters: u64,
    pub used_clusters: u64,
    pub vsn: String,
    pub source_label: String,
}

fn fixed(s: &str, n: usize) -> Vec<u8> {
    let mut b = s.as_bytes().to_vec();
    b.truncate(n);
    b.resize(n, 0);
    b
}
fn unfixed(b: &[u8]) -> String {
    let end = b.iter().rposition(|&x| x != 0).map_or(0, |i| i + 1);
    String::from_utf8_lossy(&b[..end]).into_owned()
}

impl Header {
    pub fn to_bytes(&self) -> [u8; HEADER_SIZE] {
        let mut b = Vec::with_capacity(HEADER_SIZE);
        b.extend_from_slice(MAGIC);
        b.extend_from_slice(&self.version.to_le_bytes());
        b.extend_from_slice(&self.cluster_size.to_le_bytes());
        b.extend_from_slice(&self.total_clusters.to_le_bytes());
        b.extend_from_slice(&self.used_clusters.to_le_bytes());
        b.extend_from_slice(&fixed(&self.vsn, 16));
        b.extend_from_slice(&fixed(&self.source_label, 32));
        let crc = crc32fast::hash(&b);
        b.extend_from_slice(&crc.to_le_bytes());
        b.try_into().unwrap()
    }

    pub fn parse(b: &[u8; HEADER_SIZE]) -> Result<Header, QblkError> {
        if &b[..8] != MAGIC {
            return Err(QblkError::BadMagic);
        }
        let stored = u32::from_le_bytes(b[78..82].try_into().unwrap());
        let calc = crc32fast::hash(&b[..78]);
        if stored != calc {
            return Err(QblkError::BadHeaderCrc { stored, calc });
        }
        let version = u16::from_le_bytes(b[8..10].try_into().unwrap());
        if version != VERSION {
            return Err(QblkError::UnsupportedVersion(version));
        }
        let cluster_size = u32::from_le_bytes(b[10..14].try_into().unwrap());
        if cluster_size == 0
            || !cluster_size.is_power_of_two()
            || cluster_size as u64 > MAX_CHUNK_BYTES
        {
            return Err(QblkError::BadHeaderField("cluster_size"));
        }
        let total_clusters = u64::from_le_bytes(b[14..22].try_into().unwrap());
        let used_clusters = u64::from_le_bytes(b[22..30].try_into().unwrap());
        if used_clusters > total_clusters {
            return Err(QblkError::BadHeaderField("used_clusters"));
        }
        Ok(Header {
            version,
            cluster_size,
            total_clusters,
            used_clusters,
            vsn: unfixed(&b[30..46]),
            source_label: unfixed(&b[46..78]),
        })
    }
}

/// 한 청크(압축 해제·CRC 검증 완료).
#[derive(Debug)]
pub struct Chunk {
    pub lcn: u64,
    pub cluster_count: u32,
    /// 파일 안에서 이 청크 헤더가 시작하는 위치(오류 보고용)
    pub offset: u64,
    pub compressed_len: u32,
    pub raw: Vec<u8>,
}

pub struct Reader<R: Read> {
    r: R,
    pub header: Header,
    pos: u64,
}

fn read_full<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
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

impl<R: Read> Reader<R> {
    pub fn new(mut r: R) -> Result<Self, QblkError> {
        let mut hb = [0u8; HEADER_SIZE];
        let n = read_full(&mut r, &mut hb)?;
        if n < HEADER_SIZE {
            return Err(if n >= 8 && &hb[..8] != MAGIC {
                QblkError::BadMagic
            } else {
                QblkError::TruncatedChunkHeader { offset: 0, got: n }
            });
        }
        Ok(Self {
            header: Header::parse(&hb)?,
            r,
            pos: HEADER_SIZE as u64,
        })
    }

    /// 다음 청크. 청크 경계에서 정확히 끝났으면 None, 중간에서 끊겼으면 오류.
    pub fn next_chunk(&mut self) -> Result<Option<Chunk>, QblkError> {
        let offset = self.pos;
        let mut hb = [0u8; CHUNK_HEADER_SIZE];
        let n = read_full(&mut self.r, &mut hb)?;
        if n == 0 {
            return Ok(None);
        }
        if n < CHUNK_HEADER_SIZE {
            return Err(QblkError::TruncatedChunkHeader { offset, got: n });
        }
        let lcn = u64::from_le_bytes(hb[0..8].try_into().unwrap());
        let cluster_count = u32::from_le_bytes(hb[8..12].try_into().unwrap());
        let uncomp = u32::from_le_bytes(hb[12..16].try_into().unwrap());
        let comp = u32::from_le_bytes(hb[16..20].try_into().unwrap());
        let crc = u32::from_le_bytes(hb[20..24].try_into().unwrap());
        let cs = self.header.cluster_size;
        if uncomp as u64 > MAX_CHUNK_BYTES || comp as u64 > MAX_CHUNK_BYTES + 1024 * 1024 {
            return Err(QblkError::ChunkTooLarge {
                offset,
                size: uncomp.max(comp) as u64,
            });
        }
        // uncomp==0 은 "읽기에 실패해 데이터가 없는 청크"(파이썬이 볼륨 맨 끝 클러스터에서 만든다). 아래서 따로 다룬다.
        if uncomp != 0 && cluster_count as u64 * cs as u64 != uncomp as u64 {
            return Err(QblkError::ChunkSizeMismatch {
                offset,
                cluster_count,
                uncompressed: uncomp,
                cluster_size: cs,
            });
        }
        // 빈 청크(uncompressed==0)는 쓸 데이터가 없으므로 범위 검사 대상이 아니다(실제 이미지에서 LCN==total_clusters 로 나타남)
        if uncomp != 0
            && lcn
                .checked_add(cluster_count as u64)
                .is_none_or(|e| e > self.header.total_clusters)
        {
            return Err(QblkError::OutOfRange {
                lcn,
                cluster_count,
                total_clusters: self.header.total_clusters,
            });
        }
        let mut data = vec![0u8; comp as usize];
        let got = read_full(&mut self.r, &mut data)?;
        if got < comp as usize {
            return Err(QblkError::TruncatedChunkData {
                offset,
                want: comp as u64,
                got: got as u64,
            });
        }
        self.pos += CHUNK_HEADER_SIZE as u64 + comp as u64;
        let raw =
            zstd::bulk::decompress(&data, uncomp as usize).map_err(|e| QblkError::Decompress {
                offset,
                reason: e.to_string(),
            })?;
        if raw.len() != uncomp as usize {
            return Err(QblkError::Decompress {
                offset,
                reason: format!("길이 {} ≠ {}", raw.len(), uncomp),
            });
        }
        if crc32fast::hash(&raw) != crc {
            return Err(QblkError::CrcMismatch { offset, lcn });
        }
        Ok(Some(Chunk {
            lcn,
            cluster_count,
            offset,
            compressed_len: comp,
            raw,
        }))
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub chunks: u64,
    pub clusters: u64,
    pub raw_bytes: u64,
    pub compressed_bytes: u64,
    /// 데이터가 비어 있는 청크 수와 그 클러스터 수(백업 당시 읽기에 실패한 구간). 0 이 아니면 그 구간은 복원되지 않는다.
    pub empty_chunks: u64,
    pub empty_clusters: u64,
    /// LCN 이 직전 청크 끝보다 앞서는 청크 수. 파이썬 쓰기는 항상 오름차순이라 0 이어야 정상이다.
    /// (청크 CRC 는 LCN 을 덮지 않아 위치 필드 변조는 CRC 로 못 잡는다 — 이 값이 보조 신호)
    pub order_violations: u64,
}

/// 이미지 전체를 읽어 무결성을 검증한다(청크 CRC + 클러스터 합계). 첫 오류에서 중단한다.
pub fn verify<R: Read>(
    r: R,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), QblkError> {
    let mut rd = Reader::new(r)?;
    let mut st = Stats::default();
    let mut prev_end = 0u64;
    while let Some(c) = rd.next_chunk()? {
        if cancel.load(Ordering::Relaxed) {
            return Err(QblkError::Cancelled);
        }
        if c.lcn < prev_end {
            st.order_violations += 1;
        }
        prev_end = c.lcn + c.cluster_count as u64;
        st.chunks += 1;
        if c.raw.is_empty() {
            st.empty_chunks += 1;
            st.empty_clusters += c.cluster_count as u64;
        } else {
            st.clusters += c.cluster_count as u64;
        }
        st.raw_bytes += c.raw.len() as u64;
        st.compressed_bytes += c.compressed_len as u64;
        if st.chunks % 64 == 0 {
            progress(&st);
        }
    }
    if st.clusters + st.empty_clusters != rd.header.used_clusters {
        return Err(QblkError::UsedClustersMismatch {
            header: rd.header.used_clusters,
            found: st.clusters + st.empty_clusters,
        });
    }
    progress(&st);
    Ok((rd.header, st))
}

/// 이미지를 대상(파일·VHD 등 Write+Seek)에 LCN 위치대로 푼다. 실제 디스크가 아니라 이미지 파일을 만드는 용도.
/// 대상 길이는 total_clusters×cluster_size 로 맞추고(희소), 청크 CRC 를 모두 검증하며 쓴다.
pub fn restore_to<R: Read, W: Write + Seek>(
    r: R,
    out: &mut W,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), QblkError> {
    let mut rd = Reader::new(r)?;
    let cs = rd.header.cluster_size as u64;
    let mut st = Stats::default();
    while let Some(c) = rd.next_chunk()? {
        if cancel.load(Ordering::Relaxed) {
            return Err(QblkError::Cancelled);
        }
        if !c.raw.is_empty() {
            out.seek(SeekFrom::Start(c.lcn * cs))?;
            out.write_all(&c.raw)?;
        }
        st.chunks += 1;
        if c.raw.is_empty() {
            st.empty_chunks += 1;
            st.empty_clusters += c.cluster_count as u64;
        } else {
            st.clusters += c.cluster_count as u64;
        }
        st.raw_bytes += c.raw.len() as u64;
        st.compressed_bytes += c.compressed_len as u64;
        if st.chunks % 64 == 0 {
            progress(&st);
        }
    }
    if st.clusters + st.empty_clusters != rd.header.used_clusters {
        return Err(QblkError::UsedClustersMismatch {
            header: rd.header.used_clusters,
            found: st.clusters + st.empty_clusters,
        });
    }
    out.flush()?;
    progress(&st);
    Ok((rd.header, st))
}

pub struct Writer<W: Write> {
    w: W,
    cluster_size: u32,
    level: i32,
    clusters: u64,
}

impl<W: Write> Writer<W> {
    pub fn new(mut w: W, header: &Header, level: i32) -> io::Result<Self> {
        w.write_all(&header.to_bytes())?;
        Ok(Self {
            w,
            cluster_size: header.cluster_size,
            level,
            clusters: 0,
        })
    }
    pub fn write_chunk(&mut self, lcn: u64, raw: &[u8]) -> Result<(), QblkError> {
        let cs = self.cluster_size as usize;
        if raw.is_empty() || !raw.len().is_multiple_of(cs) || raw.len() as u64 > MAX_CHUNK_BYTES {
            return Err(QblkError::BadHeaderField("chunk length"));
        }
        let comp = zstd::bulk::compress(raw, self.level)?;
        let count = (raw.len() / cs) as u32;
        self.w.write_all(&lcn.to_le_bytes())?;
        self.w.write_all(&count.to_le_bytes())?;
        self.w.write_all(&(raw.len() as u32).to_le_bytes())?;
        self.w.write_all(&(comp.len() as u32).to_le_bytes())?;
        self.w.write_all(&crc32fast::hash(raw).to_le_bytes())?;
        self.w.write_all(&comp)?;
        self.clusters += count as u64;
        Ok(())
    }
    /// 지금까지 쓴 클러스터 수(헤더의 used_clusters 와 같아야 한다)
    pub fn clusters_written(&self) -> u64 {
        self.clusters
    }
    pub fn finish(mut self) -> io::Result<W> {
        self.w.flush()?;
        Ok(self.w)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    const PY: &[u8] = include_bytes!("../tests/fixtures/py_small.qblk");
    fn nocancel() -> AtomicBool {
        AtomicBool::new(false)
    }

    fn make(chunks: &[(u64, Vec<u8>)]) -> Vec<u8> {
        let used: u64 = chunks.iter().map(|(_, d)| d.len() as u64 / 4096).sum();
        let h = Header {
            version: 1,
            cluster_size: 4096,
            total_clusters: 1000,
            used_clusters: used,
            vsn: "ABCD-1234".into(),
            source_label: "라벨".into(),
        };
        let mut w = Writer::new(Vec::new(), &h, 1).unwrap();
        for (lcn, d) in chunks {
            w.write_chunk(*lcn, d).unwrap();
        }
        w.finish().unwrap()
    }

    #[test]
    fn python_made_image_verifies_and_restores() {
        let (h, st) = verify(Cursor::new(PY), &nocancel(), &|_| {}).unwrap();
        assert_eq!(
            (h.cluster_size, h.total_clusters, h.used_clusters),
            (4096, 1000, 6)
        );
        assert_eq!(
            (h.vsn.as_str(), h.source_label.as_str()),
            ("5AEF-9960", "시험")
        );
        assert_eq!((st.chunks, st.clusters, st.raw_bytes), (3, 6, 6 * 4096));
        let mut out = Cursor::new(Vec::new());
        restore_to(Cursor::new(PY), &mut out, &nocancel(), &|_| {}).unwrap();
        let v = out.into_inner();
        assert_eq!(
            &v[10 * 4096..10 * 4096 + 256],
            &(0u8..=255).collect::<Vec<_>>()[..]
        );
        assert!(v[100 * 4096..101 * 4096].iter().all(|&b| b == 0));
        assert_eq!(&v[7 * 4096..7 * 4096 + 5], b"kova!");
    }

    #[test]
    fn header_roundtrip_matches_python_bytes() {
        let (h, _) = verify(Cursor::new(PY), &nocancel(), &|_| {}).unwrap();
        assert_eq!(
            &h.to_bytes()[..],
            &PY[..HEADER_SIZE],
            "Rust 가 만든 헤더가 파이썬과 바이트 단위로 같아야 함"
        );
    }

    #[test]
    fn rust_written_image_roundtrips() {
        let img = make(&[(5, vec![7u8; 8192]), (50, vec![9u8; 4096])]);
        let (_, st) = verify(Cursor::new(&img), &nocancel(), &|_| {}).unwrap();
        assert_eq!(st.clusters, 3);
    }

    #[test]
    fn truncation_at_every_kind_of_point_is_an_error() {
        // 청크 데이터 중간, 청크 헤더 중간, 청크 경계(클러스터 합계 불일치) 모두 오류여야 한다
        for cut in [
            PY.len() - 3,
            HEADER_SIZE + 10,
            HEADER_SIZE + CHUNK_HEADER_SIZE + 5,
        ] {
            assert!(
                verify(Cursor::new(&PY[..cut]), &nocancel(), &|_| {}).is_err(),
                "cut={cut}"
            );
        }
        // 마지막 청크 하나를 통째로 잘라낸 경우(경계에서 끊김) — 헤더의 used_clusters 로 잡는다
        let img = make(&[(1, vec![1u8; 4096]), (2, vec![2u8; 4096])]);
        let one =
            HEADER_SIZE + CHUNK_HEADER_SIZE + zstd::bulk::compress(&[1u8; 4096], 1).unwrap().len();
        let r = verify(Cursor::new(&img[..one]), &nocancel(), &|_| {});
        assert!(
            matches!(
                r,
                Err(QblkError::UsedClustersMismatch {
                    header: 2,
                    found: 1
                })
            ),
            "{r:?}"
        );
    }

    #[test]
    fn bit_flips_are_detected_in_header_and_body() {
        let mut b = PY.to_vec();
        b[12] ^= 1; // 헤더(cluster_size) → CRC 불일치
        assert!(matches!(
            verify(Cursor::new(&b), &nocancel(), &|_| {}),
            Err(QblkError::BadHeaderCrc { .. })
        ));
        // 본문 어디를 뒤집어도 "복원될 데이터"가 조용히 바뀌어서는 안 된다.
        // 포맷 한계: 청크 CRC 가 LCN(위치)을 덮지 않아 LCN 비트 변조는 통과할 수 있다(그 경우 데이터 내용은 그대로).
        let orig = chunks_of(PY);
        for i in HEADER_SIZE..PY.len() {
            let mut b = PY.to_vec();
            b[i] ^= 0x10;
            if verify(Cursor::new(&b), &nocancel(), &|_| {}).is_ok() {
                let got = chunks_of(&b);
                let same: Vec<_> = got.iter().map(|(_, c, r)| (*c, r.clone())).collect();
                let want: Vec<_> = orig.iter().map(|(_, c, r)| (*c, r.clone())).collect();
                assert_eq!(
                    same, want,
                    "바이트 {i} 변조가 데이터를 바꿨는데 검증을 통과함"
                );
            }
        }
    }

    fn chunks_of(b: &[u8]) -> Vec<(u64, u32, Vec<u8>)> {
        let mut r = Reader::new(Cursor::new(b)).unwrap();
        let mut v = vec![];
        while let Some(c) = r.next_chunk().unwrap() {
            v.push((c.lcn, c.cluster_count, c.raw));
        }
        v
    }

    #[test]
    fn out_of_order_lcn_is_reported() {
        let img = make(&[(50, vec![1u8; 4096]), (10, vec![2u8; 4096])]);
        let (_, st) = verify(Cursor::new(&img), &nocancel(), &|_| {}).unwrap();
        assert_eq!(st.order_violations, 1);
        let (_, ok) = verify(Cursor::new(PY), &nocancel(), &|_| {}).unwrap();
        // 파이썬 픽스처는 의도적으로 LCN 10,100,7 이라 위반 1건
        assert_eq!(ok.order_violations, 1);
    }

    #[test]
    fn garbage_and_limits_fail_closed() {
        assert!(verify(
            Cursor::new(b"not an image at all".to_vec()),
            &nocancel(),
            &|_| {}
        )
        .is_err());
        assert!(verify(Cursor::new(Vec::<u8>::new()), &nocancel(), &|_| {}).is_err());
        // 청크 크기 필드를 터무니없이 키워도 메모리를 잡지 않고 거부
        let mut b = PY.to_vec();
        let o = HEADER_SIZE + 12;
        b[o..o + 4].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(matches!(
            verify(Cursor::new(&b), &nocancel(), &|_| {}),
            Err(QblkError::ChunkTooLarge { .. })
        ));
        // 볼륨 범위를 벗어난 LCN
        let mut b = PY.to_vec();
        b[HEADER_SIZE..HEADER_SIZE + 8].copy_from_slice(&999_999u64.to_le_bytes());
        assert!(matches!(
            verify(Cursor::new(&b), &nocancel(), &|_| {}),
            Err(QblkError::OutOfRange { .. })
        ));
    }

    #[test]
    fn cancel_stops_verification() {
        let c = AtomicBool::new(true);
        assert!(matches!(
            verify(Cursor::new(PY), &c, &|_| {}),
            Err(QblkError::Cancelled)
        ));
    }

    #[test]
    fn empty_chunk_is_reported_not_fatal_and_counts_toward_used() {
        // 파이썬이 실제 이미지 끝에 남긴 형태: 마지막 클러스터, uncompressed=0, 빈 zstd 프레임, crc=0
        // 실제 이미지에서는 빈 청크 LCN 이 total_clusters 와 같았다(아래 1000) — 데이터 없는 청크는 범위 검사 제외
        let h = Header {
            version: 1,
            cluster_size: 4096,
            total_clusters: 1000,
            used_clusters: 2,
            vsn: String::new(),
            source_label: String::new(),
        };
        let mut w = Writer::new(Vec::new(), &h, 1).unwrap();
        w.write_chunk(5, &vec![3u8; 4096]).unwrap();
        let mut img = w.finish().unwrap();
        img.extend_from_slice(&1000u64.to_le_bytes());
        img.extend_from_slice(&1u32.to_le_bytes());
        img.extend_from_slice(&0u32.to_le_bytes());
        img.extend_from_slice(&9u32.to_le_bytes());
        img.extend_from_slice(&0u32.to_le_bytes());
        img.extend_from_slice(&[0x28, 0xb5, 0x2f, 0xfd, 0x20, 0x00, 0x01, 0x00, 0x00]);
        let (_, st) = verify(Cursor::new(&img), &nocancel(), &|_| {}).unwrap();
        assert_eq!(
            (st.chunks, st.clusters, st.empty_chunks, st.empty_clusters),
            (2, 1, 1, 1)
        );
        let mut out = Cursor::new(Vec::new());
        restore_to(Cursor::new(&img), &mut out, &nocancel(), &|_| {}).unwrap();
        assert_eq!(
            out.into_inner().len(),
            6 * 4096,
            "빈 청크는 쓰지 않으므로 파일 길이가 늘지 않아야 함"
        );
        // 데이터가 0 인데 crc 가 0 이 아니면 오류
        let n = img.len();
        img[n - 9 - 4..n - 9].copy_from_slice(&7u32.to_le_bytes());
        assert!(matches!(
            verify(Cursor::new(&img), &nocancel(), &|_| {}),
            Err(QblkError::CrcMismatch { .. })
        ));
    }
}
