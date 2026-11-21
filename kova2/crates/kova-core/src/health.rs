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

/// 48자리 BitLocker 숫자 복구 암호 형식 검사 (6자리 숫자 8블록, '-' 구분, 총 55자)
pub fn is_bitlocker_password(token: &str) -> bool {
    let t = token.trim();
    if t.len() != 55 {
        return false;
    }
    let parts: Vec<&str> = t.split('-').collect();
    if parts.len() != 8 {
        return false;
    }
    parts
        .iter()
        .all(|p| p.len() == 6 && p.chars().all(|c| c.is_ascii_digit()))
}

/// BitLocker 복구 키 정보 (암호 및 선택적 GUID)
/// 보안 강화: Debug 출력 시 48자리 민감 복구 키는 [REDACTED]로 마스킹하여 로그 누출을 원천 차단한다.
#[derive(Clone, PartialEq, Eq)]
pub struct BitLockerKeyInfo {
    pub key: String,
    pub id: Option<String>,
}

impl std::fmt::Debug for BitLockerKeyInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BitLockerKeyInfo")
            .field("key", &"[REDACTED]")
            .field("id", &self.id)
            .finish()
    }
}

/// manage-bde.exe 출력 문자열에서 48자리 BitLocker 복구 키를 파싱한다.
pub fn parse_bitlocker_recovery_key(text: &str) -> Option<String> {
    for token in text.split_whitespace() {
        let clean = token.trim_matches(['"', '\'', '(', ')', '[', ']', '.', ',', ';']);
        if is_bitlocker_password(clean) {
            return Some(clean.to_string());
        }
    }
    None
}

/// manage-bde.exe 출력에서 키와 식별자(ID) 정보를 함께 파싱한다.
pub fn parse_bitlocker_recovery_info(text: &str) -> Option<BitLockerKeyInfo> {
    let mut id = None;
    let mut key = None;

    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("ID:") || trimmed.starts_with("식별자:") {
            let val = trimmed
                .split_once(':')
                .map(|(_, v)| v.trim().trim_matches(['{', '}']).to_string());
            if let Some(guid) = val {
                if !guid.is_empty() {
                    id = Some(guid);
                }
            }
        }
        for token in trimmed.split_whitespace() {
            let clean = token.trim_matches(['"', '\'', '(', ')', '[', ']', '.', ',', ';']);
            if is_bitlocker_password(clean) && key.is_none() {
                key = Some(clean.to_string());
            }
        }
    }

    key.map(|k| BitLockerKeyInfo { key: k, id })
}

/// Windows OS C: 드라이브의 BitLocker 48자리 복구 암호 조회
///
/// 내부적으로 `manage-bde.exe -protectors -get C:` 실행 및 파싱.
/// 보안 원칙: 실패 및 성공 시 로그에 48자리 민감 복구 키를 불필요하게 노출하지 않는다.
pub fn bitlocker_recovery_key_c() -> Result<String, String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let out = std::process::Command::new("manage-bde.exe")
            .args(["-protectors", "-get", "C:"])
            .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
            .output()
            .map_err(|e| format!("manage-bde 실행 실패: {e}"))?;

        let stdout = String::from_utf8_lossy(&out.stdout);
        let stderr = String::from_utf8_lossy(&out.stderr);

        if let Some(key) = parse_bitlocker_recovery_key(&stdout) {
            return Ok(key);
        }

        if !out.status.success() {
            if stderr.contains("denied") || stdout.contains("denied") || stdout.contains("거부") {
                return Err("BitLocker 복구 키 조회 실패: 관리자 권한이 필요합니다".into());
            }
            return Err(format!(
                "manage-bde 조회 실패 (종료 코드 {:?})",
                out.status.code()
            ));
        }

        if stdout.contains("not turned on")
            || stdout.contains("보호기를 찾을 수 없습니다")
            || stdout.contains("No protectors")
        {
            return Err(
                "BitLocker가 C: 드라이브에 활성화되어 있지 않거나 숫자 암호 보호기가 없습니다"
                    .into(),
            );
        }

        Err("BitLocker 48자리 복구 키를 찾을 수 없습니다".into())
    }
    #[cfg(not(windows))]
    {
        Err("Windows에서만 BitLocker 복구 키를 조회할 수 있습니다".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn report_has_both_sections_and_never_panics() {
        let r = super::report();
        assert!(r.contains("[디스크 건강]") && r.contains("[BitLocker]"));
    }

    #[test]
    fn is_bitlocker_password_validates_48_digits() {
        let valid = "123456-234567-345678-456789-567890-678901-789012-890123";
        assert!(is_bitlocker_password(valid));

        // 자릿수 부족
        assert!(!is_bitlocker_password(
            "12345-234567-345678-456789-567890-678901-789012-890123"
        ));
        // 블록 부족
        assert!(!is_bitlocker_password("123456-234567-345678-456789-567890"));
        // 문자 포함
        assert!(!is_bitlocker_password(
            "12345A-234567-345678-456789-567890-678901-789012-890123"
        ));
        // 빈 문자열
        assert!(!is_bitlocker_password(""));
    }

    #[test]
    fn parse_bitlocker_recovery_key_english_output() {
        let text = r#"
BitLocker Drive Encryption: Configuration Tool version 10.0.26100
Volume C: [Windows]
All Key Protectors

    Numerical Password:
      ID: {12345678-1234-1234-1234-1234567890AB}
      Password:
        654321-111111-222222-333333-444444-555555-666666-777777
"#;
        let key = parse_bitlocker_recovery_key(text);
        assert_eq!(
            key.as_deref(),
            Some("654321-111111-222222-333333-444444-555555-666666-777777")
        );

        let info = parse_bitlocker_recovery_info(text).unwrap();
        assert_eq!(
            info.key,
            "654321-111111-222222-333333-444444-555555-666666-777777"
        );
        assert_eq!(
            info.id.as_deref(),
            Some("12345678-1234-1234-1234-1234567890AB")
        );
    }

    #[test]
    fn parse_bitlocker_recovery_key_korean_output() {
        let text = r#"
BitLocker 드라이브 암호화: 구성 도구 버전 10.0.26100
볼륨 C: [Windows]
모든 키 보호기

    숫자 암호:
      ID: {ABCDEF01-2345-6789-ABCD-EF0123456789}
      암호:
        999999-888888-777777-666666-555555-444444-333333-222222
"#;
        let key = parse_bitlocker_recovery_key(text);
        assert_eq!(
            key.as_deref(),
            Some("999999-888888-777777-666666-555555-444444-333333-222222")
        );

        let info = parse_bitlocker_recovery_info(text).unwrap();
        assert_eq!(
            info.key,
            "999999-888888-777777-666666-555555-444444-333333-222222"
        );
        assert_eq!(
            info.id.as_deref(),
            Some("ABCDEF01-2345-6789-ABCD-EF0123456789")
        );
    }

    #[test]
    fn parse_bitlocker_recovery_key_none_when_unencrypted() {
        let text = "BitLocker Drive Encryption is not turned on for volume C:.";
        assert_eq!(parse_bitlocker_recovery_key(text), None);
        assert_eq!(parse_bitlocker_recovery_info(text), None);
    }

    #[test]
    fn bitlocker_key_info_debug_redacts_sensitive_key() {
        let info = BitLockerKeyInfo {
            key: "123456-234567-345678-456789-567890-678901-789012-890123".into(),
            id: Some("11223344-5566-7788-9900-aabbccddeeff".into()),
        };
        let debug_str = format!("{info:?}");
        assert!(
            !debug_str.contains("123456"),
            "로그/디버그 출력에 평문 키가 노출되면 안 됩니다"
        );
        assert!(
            debug_str.contains("[REDACTED]"),
            "평문 키 대신 [REDACTED] 마스킹이 적용되어야 합니다"
        );
        assert!(debug_str.contains("11223344-5566-7788-9900-aabbccddeeff"));
    }
}
