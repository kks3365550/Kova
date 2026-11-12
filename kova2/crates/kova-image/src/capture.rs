//! 파일(raw/img/bin/vhd 등)을 .qblk 이미지로 만든다. 원본은 읽기 전용으로 열고, 장치·볼륨 경로는 받지 않는다(실제 디스크 캡처는 별도 단계).
//! 원본의 "사용 중" 기준은 값이 전부 0이 아닌 클러스터다. 전부 0인 구간은 기록하지 않아 복원 시 희소하게 남는다.
use crate::qblk::{Header, QblkError, Stats, Writer};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

pub const CHUNK_CLUSTERS: usize = 2048;

fn is_device(p: &Path) -> bool {
    let s = p.to_string_lossy();
    s.starts_with("\\\\.\\") || s.starts_with("\\\\?\\") || s.starts_with("//./")
}

/// 출력 경로 검사: .qblk 확장자, 장치 경로 아님, 이미 있는 파일 아님, 부모 폴더 존재, 원본과 같지 않음.
pub fn check_output(source: &Path, out: &Path) -> Result<(), QblkError> {
    let bad = |m: &str| {
        QblkError::Io(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            m.to_string(),
        ))
    };
    if is_device(out) {
        return Err(bad("장치 경로에는 이미지를 만들 수 없습니다"));
    }
    if out
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("qblk"))
        != Some(true)
    {
        return Err(bad("출력 파일 확장자는 .qblk 여야 합니다"));
    }
    if out.exists() {
        return Err(bad("같은 이름의 파일이 이미 있습니다"));
    }
    match out.parent() {
        Some(p) if !p.as_os_str().is_empty() && !p.is_dir() => {
            return Err(bad("출력 폴더가 없습니다"))
        }
        _ => {}
    }
    if let (Ok(a), Ok(b)) = (
        source.canonicalize(),
        out.parent().unwrap_or(Path::new(".")).canonicalize(),
    ) {
        if a.parent() == Some(b.as_path()) && a.file_name() == out.file_name() {
            return Err(bad("원본과 같은 파일입니다"));
        }
    }
    Ok(())
}

/// `source` 파일을 읽어 `out_path` 에 .qblk 를 만든다. 실패하거나 취소하면 만들던 파일을 지운다.
pub fn capture_file(
    source: &Path,
    out_path: &Path,
    cluster_size: u32,
    level: i32,
    label: &str,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), QblkError> {
    if is_device(source) {
        return Err(QblkError::Io(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "장치 경로는 아직 지원하지 않습니다(파일만 가능)",
        )));
    }
    check_output(source, out_path)?;
    let mut src = File::open(source)?;
    let len = src.metadata()?.len();
    let cs = cluster_size as u64;
    if cluster_size == 0 || !cluster_size.is_power_of_two() || len == 0 || len % cs != 0 {
        return Err(QblkError::BadHeaderField("source length / cluster_size"));
    }
    let total = len / cs;
    let out = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(out_path)?;
    let res = (|| -> Result<(Header, Stats), QblkError> {
        let mut header = Header {
            version: crate::qblk::VERSION,
            cluster_size,
            total_clusters: total,
            used_clusters: 0,
            vsn: String::new(),
            source_label: label.chars().take(16).collect(),
        };
        let mut w = Writer::new(std::io::BufWriter::new(out), &header, level)?;
        let mut st = Stats::default();
        let mut buf = vec![0u8; CHUNK_CLUSTERS * cluster_size as usize];
        let mut lcn = 0u64;
        while lcn < total {
            if cancel.load(Ordering::Relaxed) {
                return Err(QblkError::Cancelled);
            }
            let n = (CHUNK_CLUSTERS as u64).min(total - lcn) as usize;
            let bytes = &mut buf[..n * cluster_size as usize];
            src.read_exact(bytes)?;
            let cl = cluster_size as usize;
            let mut i = 0;
            while i < n {
                if bytes[i * cl..(i + 1) * cl].iter().all(|&b| b == 0) {
                    i += 1;
                    continue;
                }
                let start = i;
                while i < n && !bytes[i * cl..(i + 1) * cl].iter().all(|&b| b == 0) {
                    i += 1;
                }
                w.write_chunk(lcn + start as u64, &bytes[start * cl..i * cl])?;
                st.chunks += 1;
                st.clusters += (i - start) as u64;
                st.raw_bytes += ((i - start) * cl) as u64;
            }
            lcn += n as u64;
            progress(&st);
        }
        header.used_clusters = w.clusters_written();
        let mut f = w.finish()?.into_inner().map_err(|e| e.into_error())?;
        f.seek(SeekFrom::Start(0))?;
        f.write_all(&header.to_bytes())?;
        f.flush()?;
        f.sync_all()?;
        Ok((header, st))
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(out_path);
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::qblk::{restore_to, verify};
    use std::io::Cursor;

    fn nocancel() -> AtomicBool {
        AtomicBool::new(false)
    }

    #[test]
    fn roundtrip_preserves_bytes_and_skips_zero_clusters() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("disk.img");
        // 6000 클러스터(4096): 앞·중간·끝에 데이터, 나머지는 0. 청크 경계(2048)를 넘는 연속 구간도 포함
        let cs = 4096usize;
        let total = 6000usize;
        let mut data = vec![0u8; total * cs];
        let mut put = |from: usize, to: usize, v: u8| {
            for c in from..to {
                for (j, b) in data[c * cs..(c + 1) * cs].iter_mut().enumerate() {
                    *b = v.wrapping_add((j % 251) as u8);
                }
            }
        };
        put(0, 3, 1);
        put(2040, 2060, 7);
        put(5990, 6000, 9);
        std::fs::write(&src, &data).unwrap();
        let out = d.path().join("disk.qblk");
        let (h, st) = capture_file(&src, &out, 4096, 1, "테스트", &nocancel(), &|_| {}).unwrap();
        assert_eq!((h.total_clusters, h.used_clusters), (6000, 33));
        assert_eq!(st.clusters, 33);
        let f = File::open(&out).unwrap();
        let (_, vs) = verify(std::io::BufReader::new(f), &nocancel(), &|_| {}).unwrap();
        assert_eq!(vs.clusters, 33);
        assert_eq!(vs.order_violations, 0);
        let mut cur = Cursor::new(vec![0u8; total * cs]);
        restore_to(File::open(&out).unwrap(), &mut cur, &nocancel(), &|_| {}).unwrap();
        assert!(cur.into_inner() == data);
        // 압축되어 원본보다 훨씬 작다
        assert!(std::fs::metadata(&out).unwrap().len() < 33 * 4096);
    }

    #[test]
    fn rejects_bad_inputs_and_cleans_up() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("a.img");
        std::fs::write(&src, vec![1u8; 4096 * 2]).unwrap();
        let ok = d.path().join("a.qblk");
        let c = nocancel();
        let run = |s: &Path, o: &Path| capture_file(s, o, 4096, 1, "", &c, &|_| {});
        assert!(run(&src, &d.path().join("a.txt")).is_err());
        assert!(run(&src, Path::new(r"\\.\PhysicalDrive0")).is_err());
        assert!(run(Path::new(r"\\.\PhysicalDrive0"), &ok).is_err());
        assert!(run(&src, &d.path().join("nodir").join("a.qblk")).is_err());
        let odd = d.path().join("odd.img");
        std::fs::write(&odd, vec![1u8; 4096 + 5]).unwrap();
        assert!(run(&odd, &ok).is_err());
        assert!(!ok.exists());
        assert!(run(&src, &ok).is_ok());
        assert!(run(&src, &ok).is_err()); // 이미 있음
        let cancelled = AtomicBool::new(true);
        let o2 = d.path().join("b.qblk");
        assert!(matches!(
            capture_file(&src, &o2, 4096, 1, "", &cancelled, &|_| {}),
            Err(QblkError::Cancelled)
        ));
        assert!(!o2.exists());
    }
}
