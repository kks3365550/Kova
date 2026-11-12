//! 파일 백업용 VSS(볼륨 섀도 복사): 원본이 있는 드라이브를 잠깐 스냅샷해 열려 있거나 잠긴 파일도 같은 시점으로 읽는다.
//! 관리자(서비스) 권한과 NTFS 같은 VSS 지원 드라이브가 필요하다. 만들 수 없으면 호출자가 직접 읽기로 되돌아간다.
use std::path::{Path, PathBuf};

/// 만들어 둔 섀도 복사본 묶음. 지워질 때(Drop) 모두 삭제한다.
pub struct ShadowSet {
    /// (드라이브 "X:" 대문자, 섀도 ID, 장치 경로)
    shadows: Vec<(String, String, String)>,
}

/// `C:\dir\x` → "C:" (드라이브 문자 경로가 아니면 None).
fn drive_of(p: &Path) -> Option<String> {
    let s = p.to_string_lossy();
    let s = s.strip_prefix("\\\\?\\").unwrap_or(&s);
    let b = s.as_bytes();
    if b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':' {
        Some(format!("{}:", (b[0] as char).to_ascii_uppercase()))
    } else {
        None
    }
}

/// 장치 경로 + 원래 경로(드라이브 뒤 부분) → 섀도 안의 경로.
fn join_device(device: &str, p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    let s = s.strip_prefix("\\\\?\\").unwrap_or(&s);
    let rest = &s[2..];
    PathBuf::from(format!(
        "{}{}{}",
        device.trim_end_matches('\\'),
        if rest.starts_with(['\\', '/']) {
            ""
        } else {
            "\\"
        },
        rest
    ))
}

impl ShadowSet {
    /// 경로들이 있는 드라이브마다 섀도 복사본을 하나씩 만든다. 하나라도 실패하면 만든 것을 지우고 오류를 돌려준다.
    pub fn create_for(paths: &[PathBuf]) -> Result<ShadowSet, String> {
        let mut drives: Vec<String> = paths.iter().filter_map(|p| drive_of(p)).collect();
        drives.sort();
        drives.dedup();
        if drives.is_empty() {
            return Err("드라이브 문자 경로가 아니어서 섀도 복사를 쓸 수 없음".into());
        }
        let mut set = ShadowSet { shadows: vec![] };
        for d in drives {
            let (id, device) = imp::create(&d)?; // 실패하면 set 이 Drop 되며 앞서 만든 것도 지워진다
            set.shadows.push((d, id, device));
        }
        Ok(set)
    }

    /// 원래 경로를 섀도 안의 경로로 바꾼다. 섀도가 없는 드라이브면 그대로 돌려준다.
    pub fn map(&self, p: &Path) -> PathBuf {
        match drive_of(p).and_then(|d| self.shadows.iter().find(|s| s.0 == d)) {
            Some((_, _, device)) => join_device(device, p),
            None => p.to_path_buf(),
        }
    }

    pub fn count(&self) -> usize {
        self.shadows.len()
    }
}

impl Drop for ShadowSet {
    fn drop(&mut self) {
        for (_, id, _) in &self.shadows {
            imp::delete(id);
        }
    }
}

#[cfg(windows)]
mod imp {
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    fn ps(script: &str) -> Result<String, String> {
        let o = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
            .output()
            .map_err(|e| format!("PowerShell 실행 실패: {e}"))?;
        if !o.status.success() {
            return Err(format!(
                "섀도 복사 실패: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            ));
        }
        Ok(String::from_utf8_lossy(&o.stdout).trim().to_string())
    }

    /// drive 는 호출 전에 "X:" 로 검증된 값이라 스크립트에 넣어도 안전하다.
    pub fn create(drive: &str) -> Result<(String, String), String> {
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
            .ok_or_else(|| format!("섀도 복사본 응답을 해석할 수 없음: {line}"))?;
        let (id, device) = (id.trim(), device.trim());
        // 삭제 스크립트에 들어가므로 GUID 모양만 허용한다
        let guid_ok = id.len() >= 36
            && id
                .chars()
                .all(|c| c.is_ascii_hexdigit() || matches!(c, '-' | '{' | '}'));
        if !guid_ok || !device.starts_with("\\\\?\\GLOBALROOT\\Device\\") {
            return Err(format!("섀도 복사본 정보가 이상함: {line}"));
        }
        Ok((id.to_string(), device.to_string()))
    }

    pub fn delete(id: &str) {
        let _ = ps(&format!(
            "Get-CimInstance Win32_ShadowCopy -Filter \"ID='{id}'\" | Remove-CimInstance"
        ));
    }
}

#[cfg(not(windows))]
mod imp {
    pub fn create(_: &str) -> Result<(String, String), String> {
        Err("VSS 는 Windows 전용".into())
    }
    pub fn delete(_: &str) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drive_and_path_mapping() {
        assert_eq!(drive_of(Path::new("c:\\a\\b")).as_deref(), Some("C:"));
        assert_eq!(drive_of(Path::new("\\\\?\\D:\\x")).as_deref(), Some("D:"));
        assert_eq!(drive_of(Path::new("\\\\server\\share")), None);
        let dev = "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7";
        assert_eq!(
            join_device(dev, Path::new("C:\\data\\a.txt")),
            PathBuf::from("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\\data\\a.txt")
        );
        assert_eq!(
            join_device(dev, Path::new("C:")),
            PathBuf::from("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\\")
        );
        let set = ShadowSet {
            shadows: vec![("C:".into(), "x".into(), dev.into())],
        };
        assert_eq!(set.map(Path::new("D:\\q")), PathBuf::from("D:\\q"));
        assert!(ShadowSet::create_for(&[PathBuf::from("\\\\srv\\s")]).is_err());
        std::mem::forget(set); // 가짜 ID 를 지우려고 PowerShell 을 부르지 않게 한다
    }

    /// 실제 섀도 복사본을 만든다(관리자 권한 필요): `cargo test -p kova-core vss -- --ignored`
    #[test]
    #[ignore]
    #[cfg(windows)]
    fn real_shadow_reads_a_file_held_open_exclusively() {
        use std::io::Write;
        use std::os::windows::fs::OpenOptionsExt;
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("locked.txt");
        std::fs::write(&f, b"before-lock").unwrap();
        let set = ShadowSet::create_for(std::slice::from_ref(&f)).expect("관리자 권한/VSS 필요");
        // 섀도를 만든 뒤 독점으로 열어 내용을 바꾼다: 직접 읽기는 막히고 섀도는 이전 시점을 돌려준다
        let mut w = std::fs::OpenOptions::new()
            .write(true)
            .truncate(true)
            .share_mode(0)
            .open(&f)
            .unwrap();
        w.write_all(b"after-change").unwrap();
        w.flush().unwrap();
        assert!(
            std::fs::read(&f).is_err(),
            "독점으로 열려 직접 읽기는 실패해야 함"
        );
        assert_eq!(std::fs::read(set.map(&f)).unwrap(), b"before-lock");
        drop(w);
        drop(set);
    }
}
