//! 볼륨(NTFS)을 VSS 섀도 복사본으로 잠깐 떠서 읽기 전용으로 .qblk 를 만든다.
//! 안전 원칙: 원본 볼륨에는 아무것도 쓰지 않는다. 섀도 복사본은 이 함수가 만들고, 어떤 경우에도(성공·실패·취소·패닉)
//! 끝나면 바로 삭제한다. 출력은 새 .qblk 파일 하나뿐이며 실패·취소 시 지운다.
use crate::bitmap::{allocated_runs, RunParams};
use crate::capture::check_output;
use crate::qblk::{Header, QblkError, Stats, Writer};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

fn io_err(m: impl Into<String>) -> QblkError {
    QblkError::Io(std::io::Error::other(m.into()))
}

/// 비트맵(할당 클러스터)과 읽기 소스로 이미지를 쓴다. 장치 접근과 분리해 메모리 소스로 시험할 수 있게 한 핵심 루프.
#[allow(clippy::too_many_arguments)]
pub fn write_image<R: Read + Seek, W: Write + Seek>(
    src: &mut R,
    out: W,
    header: &mut Header,
    bitmap: &[u8],
    level: i32,
    cancel: &AtomicBool,
    progress: &dyn Fn(&Stats),
) -> Result<(Stats, W), QblkError> {
    let cs = header.cluster_size as u64;
    let total = header.total_clusters;
    let runs = allocated_runs(
        bitmap,
        RunParams {
            cluster_size: cs,
            ..RunParams::default()
        },
    );
    let mut w = Writer::new(std::io::BufWriter::new(out), header, level)?;
    let mut st = Stats::default();
    let mut buf: Vec<u8> = Vec::new();
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
        w.write_chunk(lcn, &buf)?;
        st.chunks += 1;
        st.clusters += count;
        st.raw_bytes += count * cs;
        progress(&st);
    }
    header.used_clusters = w.clusters_written();
    let bw = w.finish()?;
    let mut f = bw.into_inner().map_err(|e| e.into_error())?;
    f.seek(SeekFrom::Start(0))?;
    f.write_all(&header.to_bytes())?;
    f.flush()?;
    Ok((st, f))
}

#[cfg(windows)]
pub use win::capture_volume;

#[cfg(windows)]
mod win {
    use super::*;
    use std::fs::OpenOptions;
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const FILE_FLAG_SEQUENTIAL_SCAN: u32 = 0x0800_0000;
    const FSCTL_GET_NTFS_VOLUME_DATA: u32 = 0x0009_0064;
    const FSCTL_GET_VOLUME_BITMAP: u32 = 0x0009_006F;

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
    }

    fn ps(script: &str) -> Result<String, QblkError> {
        let o = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .creation_flags(CREATE_NO_WINDOW)
            .output()?;
        if !o.status.success() {
            return Err(io_err(format!(
                "PowerShell 실패: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            )));
        }
        Ok(String::from_utf8_lossy(&o.stdout).trim().to_string())
    }

    /// 만들어 둔 섀도 복사본. 지워질 때(Drop) 삭제한다.
    struct Shadow {
        id: String,
        device: String,
    }
    impl Shadow {
        fn create(drive: &str) -> Result<Shadow, QblkError> {
            // drive 는 호출 전에 "X:" 형태로 검증된 값이라 스크립트에 넣어도 안전하다.
            let script = format!(
                "$r = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create -Arguments @{{Volume='{drive}\\';Context='ClientAccessible'}}; \
                 if ($r.ReturnValue -ne 0) {{ throw ('VSS create failed: ' + $r.ReturnValue) }}; \
                 $s = Get-CimInstance Win32_ShadowCopy -Filter (\"ID='\" + $r.ShadowID + \"'\"); \
                 Write-Output ($r.ShadowID + '|' + $s.DeviceObject)"
            );
            let out = ps(&script)?;
            let line = out.lines().last().unwrap_or("");
            let (id, device) = line
                .split_once('|')
                .ok_or_else(|| io_err(format!("섀도 복사본 응답을 해석할 수 없음: {line}")))?;
            let id = id.trim();
            // 아래 삭제 스크립트에 들어가므로 GUID 모양만 허용한다.
            let guid_ok = id.len() >= 36
                && id
                    .chars()
                    .all(|c| c.is_ascii_hexdigit() || matches!(c, '-' | '{' | '}'));
            if !guid_ok || !device.trim().starts_with("\\\\?\\GLOBALROOT\\Device\\") {
                return Err(io_err(format!("섀도 복사본 정보가 이상함: {line}")));
            }
            Ok(Shadow {
                id: id.to_string(),
                device: device.trim().to_string(),
            })
        }
    }
    impl Drop for Shadow {
        fn drop(&mut self) {
            let _ = ps(&format!(
                "Get-CimInstance Win32_ShadowCopy -Filter \"ID='{}'\" | Remove-CimInstance",
                self.id
            ));
        }
    }

    fn ioctl(
        f: &std::fs::File,
        code: u32,
        input: &[u8],
        out: &mut [u8],
    ) -> Result<u32, std::io::Error> {
        let mut ret = 0u32;
        let ok = unsafe {
            DeviceIoControl(
                f.as_raw_handle() as *mut _,
                code,
                if input.is_empty() {
                    std::ptr::null()
                } else {
                    input.as_ptr()
                },
                input.len() as u32,
                out.as_mut_ptr(),
                out.len() as u32,
                &mut ret,
                std::ptr::null_mut(),
            )
        };
        if ok == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(ret)
    }

    /// "C:" 또는 "C:\" 형태만 허용하고 "C:" 로 정규화.
    fn norm_drive(d: &str) -> Result<String, QblkError> {
        let b = d.as_bytes();
        if (b.len() == 2 || (b.len() == 3 && b[2] == b'\\'))
            && b[0].is_ascii_alphabetic()
            && b[1] == b':'
        {
            Ok(format!("{}:", (b[0] as char).to_ascii_uppercase()))
        } else {
            Err(io_err("드라이브는 C: 형태로만 지정할 수 있습니다"))
        }
    }

    /// `drive`(예: "C:")의 섀도 복사본을 읽어 `out_path` 에 .qblk 를 만든다. 관리자 권한 필요.
    pub fn capture_volume(
        drive: &str,
        out_path: &Path,
        level: i32,
        cancel: &AtomicBool,
        progress: &dyn Fn(&Stats),
    ) -> Result<(Header, Stats), QblkError> {
        let drive = norm_drive(drive)?;
        {
            extern "system" {
                fn GetDriveTypeW(root: *const u16) -> u32;
            }
            let w: Vec<u16> = format!("{drive}\\").encode_utf16().chain(Some(0)).collect();
            if unsafe { GetDriveTypeW(w.as_ptr()) } != 3 {
                return Err(io_err("내장(고정) 드라이브만 이미지로 만들 수 있습니다"));
            }
        }
        check_output(Path::new(&format!("{drive}\\")), out_path)?;
        let shadow = Shadow::create(&drive)?;
        let res = (|| -> Result<(Header, Stats), QblkError> {
            // 섀도 장치는 읽기 전용으로만 연다(쓰기 권한 요청 없음).
            let mut dev = OpenOptions::new()
                .read(true)
                .share_mode(7)
                .custom_flags(FILE_FLAG_SEQUENTIAL_SCAN)
                .open(&shadow.device)?;
            let mut vd = [0u8; 128];
            ioctl(&dev, FSCTL_GET_NTFS_VOLUME_DATA, &[], &mut vd).map_err(|e| {
                io_err(format!(
                    "NTFS 볼륨 정보 조회 실패(NTFS 가 아닐 수 있음): {e}"
                ))
            })?;
            let serial = u64::from_le_bytes(vd[0..8].try_into().unwrap());
            let total = u64::from_le_bytes(vd[16..24].try_into().unwrap());
            let cs = u32::from_le_bytes(vd[44..48].try_into().unwrap());
            if cs == 0 || !cs.is_power_of_two() || total == 0 {
                return Err(io_err("볼륨 클러스터 정보가 이상함"));
            }
            // 비트맵: StartingLcn(8)+BitmapSize(8)+bits. 한 번에 받고, 모자라면(ERROR_MORE_DATA) 오류로 중단한다.
            let mut out = vec![0u8; 16 + (total as usize).div_ceil(8) + 64];
            ioctl(&dev, FSCTL_GET_VOLUME_BITMAP, &0u64.to_le_bytes(), &mut out)
                .map_err(|e| io_err(format!("볼륨 비트맵 조회 실패: {e}")))?;
            let start = u64::from_le_bytes(out[0..8].try_into().unwrap());
            if start != 0 {
                return Err(io_err("비트맵 시작 위치가 0이 아님"));
            }
            let bytes = (total as usize).div_ceil(8);
            let bitmap = out[16..16 + bytes].to_vec();
            let file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(out_path)?;
            let mut header = Header {
                version: crate::qblk::VERSION,
                cluster_size: cs,
                total_clusters: total,
                used_clusters: 0,
                vsn: format!("{serial:016X}"),
                source_label: drive.clone(),
            };
            let (st, f) = write_image(
                &mut dev,
                file,
                &mut header,
                &bitmap,
                level,
                cancel,
                progress,
            )?;
            f.sync_all()?;
            Ok((header, st))
        })();
        drop(shadow); // 섀도 복사본 삭제
        if res.is_err() {
            let _ = std::fs::remove_file(out_path);
        }
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::qblk::{restore_to, verify};
    use std::io::Cursor;

    #[test]
    fn writes_only_allocated_runs_and_roundtrips() {
        let cs = 4096usize;
        let total = 64usize;
        let mut img = vec![0u8; cs * total];
        // 할당 클러스터: 1,2,3 과 40
        let used = [1usize, 2, 3, 40];
        for &c in &used {
            img[c * cs..(c + 1) * cs].fill(c as u8 + 1);
        }
        let mut bitmap = vec![0u8; total / 8];
        for &c in &used {
            bitmap[c / 8] |= 1 << (c % 8);
        }
        let mut header = Header {
            version: crate::qblk::VERSION,
            cluster_size: cs as u32,
            total_clusters: total as u64,
            used_clusters: 0,
            vsn: "0123456789ABCDEF".into(),
            source_label: "C:".into(),
        };
        let cancel = AtomicBool::new(false);
        let (st, out) = write_image(
            &mut Cursor::new(img.clone()),
            Cursor::new(Vec::new()),
            &mut header,
            &bitmap,
            3,
            &cancel,
            &|_| {},
        )
        .unwrap();
        assert_eq!(st.clusters, 4);
        assert_eq!(header.used_clusters, 4);
        let bytes = out.into_inner();
        let v = verify(Cursor::new(&bytes), &cancel, &|_| {}).unwrap();
        assert_eq!(v.1.clusters, 4);
        let mut restored = Cursor::new(vec![0u8; cs * total]);
        restore_to(Cursor::new(&bytes), &mut restored, &cancel, &|_| {}).unwrap();
        assert_eq!(restored.into_inner(), img);
    }

    #[test]
    fn cancel_stops_before_writing() {
        let cancel = AtomicBool::new(true);
        let mut header = Header {
            version: crate::qblk::VERSION,
            cluster_size: 4096,
            total_clusters: 8,
            used_clusters: 0,
            vsn: String::new(),
            source_label: String::new(),
        };
        let r = write_image(
            &mut Cursor::new(vec![1u8; 4096 * 8]),
            Cursor::new(Vec::new()),
            &mut header,
            &[0xFF],
            3,
            &cancel,
            &|_| {},
        );
        assert!(matches!(r, Err(QblkError::Cancelled)));
    }
}
