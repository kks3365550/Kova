//! 디스크 건강·BitLocker 점검(읽기 전용). 결과는 그대로 문장으로 돌려주며, 실패해도 진단을 막지 않는다.

#[cfg(windows)]
fn ps(script: &str) -> String {
    use std::os::windows::process::CommandExt;
    let out = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output();
    match out {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        Ok(o) => format!(
            "(조회 실패: {})",
            String::from_utf8_lossy(&o.stderr)
                .trim()
                .lines()
                .next()
                .unwrap_or("")
        ),
        Err(e) => format!("(PowerShell 실행 실패: {e})"),
    }
}

#[cfg(not(windows))]
fn ps(_script: &str) -> String {
    "(Windows에서만 조회합니다)".into()
}

/// 물리 디스크의 건강·운영 상태.
pub fn disks() -> String {
    ps("Get-PhysicalDisk | ForEach-Object { '{0} | {1} | 건강={2} | 상태={3} | {4:N0} GB' -f $_.FriendlyName, $_.MediaType, $_.HealthStatus, $_.OperationalStatus, ($_.Size/1GB) }")
}

/// 볼륨별 BitLocker 보호 상태(관리자 권한이 아니면 조회가 거부될 수 있다).
pub fn bitlocker() -> String {
    ps("Get-BitLockerVolume | ForEach-Object { '{0} | 보호={1} | 암호화={2}%' -f $_.MountPoint, $_.ProtectionStatus, $_.EncryptionPercentage }")
}

pub fn report() -> String {
    format!(
        "[디스크 건강]\n{}\n\n[BitLocker]\n{}\n",
        disks(),
        bitlocker()
    )
}

#[cfg(test)]
mod tests {
    #[test]
    fn report_has_both_sections_and_never_panics() {
        let r = super::report();
        assert!(r.contains("[디스크 건강]") && r.contains("[BitLocker]"));
    }
}
