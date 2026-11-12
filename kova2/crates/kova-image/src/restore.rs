//! 안전한 복원 진입점: 이미지를 "새 파일"에만 푼다. 실제 디스크·볼륨·장치로는 절대 쓰지 않는다.
//! 복원 시험은 VM/VHD 용 raw 이미지 파일로만 하며, 실제 디스크 쓰기는 별도 승인 절차 없이는 이 코드에서 불가능하다.
use crate::qblk::{restore_to, Header, QblkError, Stats};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter};
use std::path::Path;
use std::sync::atomic::AtomicBool;

#[derive(Debug)]
pub enum RestoreError {
    /// 장치 경로(`\\.\PhysicalDrive0`, `\\?\Volume{..}`, `C:` 같은 드라이브 문자)·이미 있는 파일 등 허용되지 않는 대상
    UnsafeTarget(String),
    /// 대상 폴더의 여유 공간이 부족
    Io(std::io::Error),
    Image(QblkError),
}
impl std::fmt::Display for RestoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsafeTarget(s) => write!(f, "복원 대상으로 허용되지 않음: {s}"),
            Self::Io(e) => write!(f, "{e}"),
            Self::Image(e) => write!(f, "{e}"),
        }
    }
}
impl From<std::io::Error> for RestoreError { fn from(e: std::io::Error) -> Self { Self::Io(e) } }
impl From<QblkError> for RestoreError { fn from(e: QblkError) -> Self { Self::Image(e) } }

/// NTFS 희소 파일로 표시한다(안 하면 LCN 사이 빈 구간이 실제로 0으로 채워져 볼륨 전체 크기만큼 쓰게 된다). 실패해도 복원은 계속된다.
#[cfg(windows)]
fn mark_sparse(f: &File) {
    use std::os::windows::io::AsRawHandle;
    extern "system" {
        fn DeviceIoControl(h: *mut core::ffi::c_void, code: u32, inb: *const u8, inlen: u32, outb: *mut u8, outlen: u32, ret: *mut u32, ov: *mut core::ffi::c_void) -> i32;
    }
    const FSCTL_SET_SPARSE: u32 = 0x000900C4;
    let mut ret = 0u32;
    unsafe {
        DeviceIoControl(f.as_raw_handle() as *mut _, FSCTL_SET_SPARSE, std::ptr::null(), 0, std::ptr::null_mut(), 0, &mut ret, std::ptr::null_mut());
    }
}
#[cfg(not(windows))]
fn mark_sparse(_f: &File) {}

/// 대상 경로 검사: 장치 네임스페이스·드라이브 루트·UNC 루트·이미 존재하는 항목을 거부한다.
pub fn check_target(target: &Path) -> Result<(), RestoreError> {
    let s = target.to_string_lossy().replace('/', "\\");
    let bad = |why: &str| Err(RestoreError::UnsafeTarget(format!("{s} ({why})")));
    if s.starts_with("\\\\.\\") || s.starts_with("\\\\?\\") { return bad("장치/확장 경로"); }
    let b = s.as_bytes();
    if b.len() <= 3 && b.len() >= 2 && b[1] == b':' { return bad("드라이브 루트"); }
    if target.file_name().is_none() { return bad("파일 이름이 없음"); }
    if target.exists() { return bad("이미 존재함 — 덮어쓰지 않음"); }
    match target.extension().and_then(|e| e.to_str()).map(|e| e.to_lowercase()) {
        Some(e) if ["img", "raw", "vhd", "vhdx", "bin"].contains(&e.as_str()) => Ok(()),
        _ => bad("확장자는 img/raw/vhd/vhdx/bin 만 허용"),
    }
}

/// image 를 target(새 파일)로 푼다. 실패·취소 시 불완전한 대상 파일은 지운다. 읽는 쪽은 읽기 전용으로만 연다.
pub fn restore_image_to_file(
    image: &Path,
    target: &Path,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), RestoreError> {
    check_target(target)?;
    let src = BufReader::with_capacity(8 << 20, File::open(image)?);
    // create_new: 검사와 생성 사이에 같은 이름이 생겨도 덮어쓰지 않는다
    let f = OpenOptions::new().write(true).create_new(true).open(target)?;
    mark_sparse(&f);
    let mut out = BufWriter::with_capacity(8 << 20, f);
    match restore_to(src, &mut out, cancel, progress) {
        Ok(r) => {
            use std::io::Write;
            out.flush()?;
            // 마지막 클러스터들이 비어 있어도 파일 크기는 볼륨 전체 크기로 맞춘다(안 그러면 NTFS 가 RAW 로 보인다)
            let full = r.0.total_clusters * r.0.cluster_size as u64;
            let f = out.into_inner().map_err(|e| e.into_error())?;
            if f.metadata()?.len() < full { f.set_len(full)?; }
            Ok(r)
        }
        Err(e) => {
            drop(out);
            let _ = fs::remove_file(target);
            Err(e.into())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PY: &[u8] = include_bytes!("../tests/fixtures/py_small.qblk");

    #[test]
    fn device_drive_and_existing_targets_are_refused() {
        for t in [r"\\.\PhysicalDrive0", r"\\?\Volume{1234}\x.img", r"C:", r"C:\", "D:", r"\\.\C:"] {
            assert!(matches!(check_target(Path::new(t)), Err(RestoreError::UnsafeTarget(_))), "{t}");
        }
        let d = tempfile::tempdir().unwrap();
        let existing = d.path().join("a.img");
        fs::write(&existing, b"x").unwrap();
        assert!(matches!(check_target(&existing), Err(RestoreError::UnsafeTarget(_))));
        assert!(matches!(check_target(&d.path().join("a.exe")), Err(RestoreError::UnsafeTarget(_))));
        assert!(check_target(&d.path().join("new.vhd")).is_ok());
    }

    #[test]
    fn restores_python_image_to_a_new_file_and_cleans_up_on_failure() {
        let d = tempfile::tempdir().unwrap();
        let img = d.path().join("py.qblk");
        fs::write(&img, PY).unwrap();
        let out = d.path().join("restored.img");
        let (h, st) = restore_image_to_file(&img, &out, &AtomicBool::new(false), &|_| {}).unwrap();
        assert_eq!(st.clusters, 6);
        assert!(fs::metadata(&out).unwrap().len() >= 101 * h.cluster_size as u64);
        assert_eq!(fs::metadata(&out).unwrap().len(), h.total_clusters * h.cluster_size as u64);
        // 같은 대상으로 다시 → 덮어쓰지 않고 거부
        assert!(matches!(restore_image_to_file(&img, &out, &AtomicBool::new(false), &|_| {}), Err(RestoreError::UnsafeTarget(_))));
        // 깨진 이미지 → 대상 파일이 남지 않는다
        let bad = d.path().join("bad.qblk");
        let mut b = PY.to_vec();
        let n = b.len();
        b[n - 5] ^= 0xFF;
        fs::write(&bad, b).unwrap();
        let out2 = d.path().join("r2.img");
        assert!(restore_image_to_file(&bad, &out2, &AtomicBool::new(false), &|_| {}).is_err());
        assert!(!out2.exists());
        // 취소 → 남지 않는다
        let out3 = d.path().join("r3.img");
        assert!(matches!(restore_image_to_file(&img, &out3, &AtomicBool::new(true), &|_| {}), Err(RestoreError::Image(QblkError::Cancelled))));
        assert!(!out3.exists());
    }
}
