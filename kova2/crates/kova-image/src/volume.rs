//! 볼륨 직접 복원(.qblk → `\\.\E:` 같은 비시스템 볼륨). 실제 디스크를 덮어쓰므로 안전장치를 모두 통과해야만 쓴다.
//!
//! 순서(어느 단계든 실패하면 볼륨에는 아무것도 쓰지 않는다): 먼저 이미지 전체 검증(`verify`),
//! 정책 검사(시스템·보호 볼륨, 크기), 사용자 확인 문구 대조, 볼륨 열기·잠금·마운트 해제
//! (잠금 실패는 사용 중이라는 뜻이므로 중단), 마지막으로 청크를 LCN 위치에 쓴다.
//! 파일 복원(`restore.rs`)과 달리 빈 구간을 0으로 채우지 않는다: NTFS 비트맵상 빈 클러스터라 정상 동작에는 영향이 없다(옛 데이터가 남을 수는 있음).
use crate::qblk::{restore_to, verify, Header, QblkError, Stats};
use std::fs::File;
use std::io::{self, BufReader, Seek, Write};
use std::path::Path;
use std::sync::atomic::AtomicBool;

#[derive(Debug)]
pub enum VolumeError {
    /// 정책·확인 문구·잠금 등으로 거부됨(볼륨에 쓰지 않음)
    Refused(String),
    Io(io::Error),
    Image(QblkError),
}
impl std::fmt::Display for VolumeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Refused(s) => write!(f, "볼륨 복원 거부: {s}"),
            Self::Io(e) => write!(f, "{e}"),
            Self::Image(e) => write!(f, "{e}"),
        }
    }
}
impl From<io::Error> for VolumeError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}
impl From<QblkError> for VolumeError {
    fn from(e: QblkError) -> Self {
        Self::Image(e)
    }
}

/// 사용자에게 보여 주고 그대로 입력하게 할 볼륨 정보
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeInfo {
    pub letter: char,
    pub label: String,
    pub serial: u32,
}

/// 사용자가 직접 입력해야 하는 확인 문구: `E: 레이블 1A2B3C4D` (드라이브·레이블·시리얼이 모두 맞아야 한다)
pub fn confirmation_phrase(v: &VolumeInfo) -> String {
    format!(
        "{}: {} {:08X}",
        v.letter.to_ascii_uppercase(),
        v.label,
        v.serial
    )
}

pub fn check_confirmation(v: &VolumeInfo, typed: &str) -> Result<(), VolumeError> {
    let norm = |s: &str| {
        s.split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase()
    };
    if norm(typed) == norm(&confirmation_phrase(v)) {
        Ok(())
    } else {
        Err(VolumeError::Refused(
            "확인 문구가 일치하지 않음(드라이브·레이블·시리얼을 그대로 입력해야 함)".into(),
        ))
    }
}

/// 드라이브 문자만으로 거부할 수 있는 검사(순수 함수). 장치를 열기 전에 먼저 호출한다.
/// `protected`: 이미지·Kova 저장소·실행 파일이 있는 드라이브 문자.
pub fn check_letter(
    letter: char,
    system_drive: char,
    protected: &[char],
) -> Result<(), VolumeError> {
    let refuse = |why: String| Err(VolumeError::Refused(why));
    if !letter.is_ascii_alphabetic() {
        return refuse(format!("드라이브 문자가 아님: {letter:?}"));
    }
    let l = letter.to_ascii_uppercase();
    if l == system_drive.to_ascii_uppercase() {
        return refuse(format!(
            "{l}: 는 시스템 볼륨이라 실행 중에는 복원할 수 없음"
        ));
    }
    if protected.iter().any(|p| p.to_ascii_uppercase() == l) {
        return refuse(format!(
            "{l}: 에 이미지·저장소·실행 파일이 있어 대상이 될 수 없음"
        ));
    }
    Ok(())
}

/// 정책 검사(순수 함수): 드라이브 문자 검사와 대상 크기 검사.
pub fn check_policy(
    letter: char,
    system_drive: char,
    protected: &[char],
    volume_bytes: u64,
    header: &Header,
) -> Result<(), VolumeError> {
    check_letter(letter, system_drive, protected)?;
    let need = header
        .total_clusters
        .saturating_mul(header.cluster_size as u64);
    if volume_bytes < need {
        return Err(VolumeError::Refused(format!(
            "대상 볼륨이 이미지보다 작음(볼륨 {volume_bytes} < 필요 {need} 바이트)"
        )));
    }
    Ok(())
}

/// 핵심 흐름. 이미지 검증이 끝나기 전에는 `open` 을 호출하지 않으므로 깨진 이미지로는 장치를 열지도 않는다.
/// `open` 은 정책·확인 문구 검사와 장치 열기·잠금을 맡고, 실패하면 아무것도 쓰지 않은 채 오류를 돌려준다.
pub fn restore_verified<W: Write + Seek, F: FnOnce(&Header) -> Result<W, VolumeError>>(
    image: &Path,
    open: F,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Header, Stats), VolumeError> {
    let (header, _) = verify(
        BufReader::with_capacity(8 << 20, File::open(image)?),
        cancel,
        &|_| {},
    )?;
    let mut dev = open(&header)?;
    let src = BufReader::with_capacity(8 << 20, File::open(image)?);
    Ok(restore_to(src, &mut dev, cancel, progress)?)
}

#[cfg(windows)]
pub use win::{restore_image_to_volume, volume_info};

#[cfg(windows)]
mod win {
    use super::*;
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;

    extern "system" {
        fn DeviceIoControl(
            h: *mut core::ffi::c_void,
            code: u32,
            inb: *const u8,
            inlen: u32,
            outb: *mut u8,
            outlen: u32,
            ret: *mut u32,
            ov: *mut core::ffi::c_void,
        ) -> i32;
        fn GetVolumeInformationW(
            root: *const u16,
            name: *mut u16,
            name_len: u32,
            serial: *mut u32,
            max_comp: *mut u32,
            flags: *mut u32,
            fs_name: *mut u16,
            fs_len: u32,
        ) -> i32;
        fn GetDriveTypeW(root: *const u16) -> u32;
    }
    const FSCTL_LOCK_VOLUME: u32 = 0x00090018;
    const FSCTL_DISMOUNT_VOLUME: u32 = 0x00090020;
    const IOCTL_DISK_GET_LENGTH_INFO: u32 = 0x0007405C;
    const DRIVE_REMOVABLE: u32 = 2;
    const DRIVE_FIXED: u32 = 3;

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn ioctl(f: &File, code: u32, out: &mut [u8]) -> bool {
        let mut ret = 0u32;
        unsafe {
            DeviceIoControl(
                f.as_raw_handle() as *mut _,
                code,
                std::ptr::null(),
                0,
                if out.is_empty() {
                    std::ptr::null_mut()
                } else {
                    out.as_mut_ptr()
                },
                out.len() as u32,
                &mut ret,
                std::ptr::null_mut(),
            ) != 0
        }
    }

    /// 드라이브 문자의 레이블·시리얼과 종류(고정/이동식만 허용)를 읽는다.
    pub fn volume_info(letter: char) -> Result<VolumeInfo, VolumeError> {
        let l = letter.to_ascii_uppercase();
        let root = wide(&format!("{l}:\\"));
        let t = unsafe { GetDriveTypeW(root.as_ptr()) };
        if t != DRIVE_FIXED && t != DRIVE_REMOVABLE {
            return Err(VolumeError::Refused(format!(
                "{l}: 는 고정·이동식 디스크가 아님(종류 코드 {t})"
            )));
        }
        let mut name = [0u16; 262];
        let mut serial = 0u32;
        let ok = unsafe {
            GetVolumeInformationW(
                root.as_ptr(),
                name.as_mut_ptr(),
                name.len() as u32,
                &mut serial,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                0,
            )
        };
        if ok == 0 {
            return Err(VolumeError::Io(io::Error::last_os_error()));
        }
        let n = name.iter().position(|&c| c == 0).unwrap_or(name.len());
        Ok(VolumeInfo {
            letter: l,
            label: String::from_utf16_lossy(&name[..n]),
            serial,
        })
    }

    fn drive_letter_of(p: &Path) -> Option<char> {
        let s = p.to_string_lossy();
        let b = s.as_bytes();
        (b.len() >= 2 && b[1] == b':' && b[0].is_ascii_alphabetic())
            .then(|| (b[0] as char).to_ascii_uppercase())
    }

    /// `letter` 볼륨에 image 를 직접 쓴다. `typed` 는 `confirmation_phrase(volume_info(letter))` 와 같아야 한다.
    /// `protected`: 대상이 될 수 없는 드라이브(Kova 저장소 등). 이미지·실행 파일·시스템 드라이브는 자동으로 보호된다.
    pub fn restore_image_to_volume(
        image: &Path,
        letter: char,
        typed: &str,
        protected: &[char],
        cancel: &AtomicBool,
        progress: &dyn Fn(&Stats),
    ) -> Result<(Header, Stats), VolumeError> {
        restore_verified(
            image,
            |header| {
                let system = std::env::var("SystemDrive")
                    .ok()
                    .and_then(|s| s.chars().next())
                    .unwrap_or('C');
                let mut prot: Vec<char> = protected.to_vec();
                prot.extend(drive_letter_of(image));
                prot.extend(
                    std::env::current_exe()
                        .ok()
                        .as_deref()
                        .and_then(drive_letter_of),
                );
                // 문자만으로 거부할 수 있는 건 장치를 열기 전에 거부한다
                check_letter(letter, system, &prot)?;
                let info = volume_info(letter)?;
                let dev_path = format!(r"\\.\{}:", info.letter);
                // 크기·확인 문구를 통과하기 전에는 쓰기 권한 핸들을 만들지 않는다(읽기 전용으로 크기만 조회)
                let probe = OpenOptionsExt::share_mode(
                    std::fs::OpenOptions::new().read(true),
                    3, // FILE_SHARE_READ | FILE_SHARE_WRITE
                )
                .open(&dev_path)?;
                let mut len = [0u8; 8];
                if !ioctl(&probe, IOCTL_DISK_GET_LENGTH_INFO, &mut len) {
                    return Err(VolumeError::Io(io::Error::last_os_error()));
                }
                check_policy(
                    info.letter,
                    system,
                    &prot,
                    i64::from_le_bytes(len) as u64,
                    header,
                )?;
                check_confirmation(&info, typed)?;
                drop(probe);
                let dev = OpenOptionsExt::share_mode(
                    std::fs::OpenOptions::new().read(true).write(true),
                    3,
                )
                .open(&dev_path)?;
                // 잠금 실패 = 파일이 열려 있거나 페이지 파일 등 사용 중 → 쓰지 않고 중단
                if !ioctl(&dev, FSCTL_LOCK_VOLUME, &mut []) {
                    return Err(VolumeError::Refused(format!(
                        "{}: 를 잠글 수 없음(사용 중인 파일이 있음): {}",
                        info.letter,
                        io::Error::last_os_error()
                    )));
                }
                if !ioctl(&dev, FSCTL_DISMOUNT_VOLUME, &mut []) {
                    return Err(VolumeError::Refused(format!(
                        "{}: 마운트 해제 실패: {}",
                        info.letter,
                        io::Error::last_os_error()
                    )));
                }
                Ok(Flush(dev))
            },
            cancel,
            progress,
        )
    }

    /// 쓰기를 마치고 닫기 전에 장치 버퍼를 비운다(`flush` 가 호출될 때마다 `sync_all`).
    struct Flush(File);
    impl Write for Flush {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            self.0.write(b)
        }
        fn flush(&mut self) -> io::Result<()> {
            self.0.sync_all()
        }
    }
    impl Seek for Flush {
        fn seek(&mut self, p: io::SeekFrom) -> io::Result<u64> {
            self.0.seek(p)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::restore::restore_image_to_file;
    use std::cell::Cell;
    use std::fs;

    const PY: &[u8] = include_bytes!("../tests/fixtures/py_small.qblk");

    fn hdr() -> Header {
        let d = tempfile::tempdir().unwrap();
        let img = d.path().join("p.qblk");
        fs::write(&img, PY).unwrap();
        verify(File::open(&img).unwrap(), &AtomicBool::new(false), &|_| {})
            .unwrap()
            .0
    }

    #[test]
    fn policy_refuses_system_protected_small_and_non_letters() {
        let h = hdr();
        let need = h.total_clusters * h.cluster_size as u64;
        assert!(check_policy('E', 'C', &['D'], need, &h).is_ok());
        assert!(check_policy('e', 'C', &['D'], need + 1, &h).is_ok());
        for (l, sys, prot, size) in [
            ('C', 'C', vec![], need),
            ('c', 'C', vec![], need),
            ('D', 'C', vec!['d'], need),
            ('E', 'C', vec![], need - 1),
            ('1', 'C', vec![], need),
            (':', 'C', vec![], need),
        ] {
            assert!(
                matches!(
                    check_policy(l, sys, &prot, size, &h),
                    Err(VolumeError::Refused(_))
                ),
                "{l} {sys} {prot:?} {size}"
            );
        }
    }

    #[test]
    fn confirmation_needs_letter_label_and_serial() {
        let v = VolumeInfo {
            letter: 'e',
            label: "백업 디스크".into(),
            serial: 0x1A2B3C4D,
        };
        assert_eq!(confirmation_phrase(&v), "E: 백업 디스크 1A2B3C4D");
        assert!(check_confirmation(&v, "E: 백업 디스크 1A2B3C4D").is_ok());
        assert!(check_confirmation(&v, "  e:  백업 디스크  1a2b3c4d ").is_ok());
        for bad in [
            "",
            "E:",
            "F: 백업 디스크 1A2B3C4D",
            "E: 백업 디스크 1A2B3C4E",
            "E: 다른 1A2B3C4D",
        ] {
            assert!(
                matches!(check_confirmation(&v, bad), Err(VolumeError::Refused(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn writes_same_bytes_as_file_restore_into_preallocated_device() {
        let d = tempfile::tempdir().unwrap();
        let img = d.path().join("p.qblk");
        fs::write(&img, PY).unwrap();
        let want = d.path().join("want.img");
        let (h, _) = restore_image_to_file(&img, &want, &AtomicBool::new(false), &|_| {}).unwrap();
        // 장치처럼 이미 같은 크기로 존재하는(옛 데이터가 든) 파일에 쓴다
        let dev_path = d.path().join("dev.img");
        let full = h.total_clusters * h.cluster_size as u64;
        fs::write(&dev_path, vec![0xAAu8; full as usize]).unwrap();
        let dev = fs::OpenOptions::new().write(true).open(&dev_path).unwrap();
        let (_, st) =
            restore_verified(&img, |_| Ok(dev), &AtomicBool::new(false), &|_| {}).unwrap();
        assert_eq!(st.clusters, 6);
        let (got, exp) = (fs::read(&dev_path).unwrap(), fs::read(&want).unwrap());
        assert_eq!(got.len(), exp.len());
        // 이미지가 쓴 클러스터는 정확히 같고, 쓰지 않은 구간은 옛 데이터(0xAA)가 그대로 남는다
        let cs = h.cluster_size as usize;
        let mut written = 0;
        for (g, e) in got.chunks(cs).zip(exp.chunks(cs)) {
            if e.iter().any(|&b| b != 0) {
                assert_eq!(g, e);
                written += 1;
            } else {
                assert!(g == e || g.iter().all(|&b| b == 0xAA));
            }
        }
        assert!(written > 0);
    }

    #[test]
    fn broken_image_never_opens_the_device_and_refusal_writes_nothing() {
        let d = tempfile::tempdir().unwrap();
        let bad = d.path().join("bad.qblk");
        let mut b = PY.to_vec();
        let n = b.len();
        b[n - 5] ^= 0xFF;
        fs::write(&bad, b).unwrap();
        let opened = Cell::new(false);
        let r = restore_verified::<fs::File, _>(
            &bad,
            |_| {
                opened.set(true);
                Err(VolumeError::Refused("x".into()))
            },
            &AtomicBool::new(false),
            &|_| {},
        );
        assert!(matches!(r, Err(VolumeError::Image(_))));
        assert!(!opened.get(), "검증 실패 전에 장치를 열면 안 됨");

        let good = d.path().join("good.qblk");
        fs::write(&good, PY).unwrap();
        let r = restore_verified::<fs::File, _>(
            &good,
            |_| Err(VolumeError::Refused("정책".into())),
            &AtomicBool::new(false),
            &|_| {},
        );
        assert!(matches!(r, Err(VolumeError::Refused(_))));
    }
}
