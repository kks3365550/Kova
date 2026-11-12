//! WORM(불변성) 보호 (core/worm.py 이식).
//! 1) 읽기 전용 속성, 2) Windows 에서는 icacls 로 Everyone 삭제·쓰기 거부 ACL.
//! 해제는 `authorized=true` 가 아니면 거부한다. ACL 변경은 권한·보안 영역이므로
//! 테스트는 읽기 전용 속성과 인가 게이트만 시험하고 실제 ACL 호출은 하지 않는다.
use std::fs;
use std::io;
use std::path::Path;
use std::process::Command;

pub const SID_EVERYONE: &str = "*S-1-1-0";

#[derive(Debug, PartialEq, Eq)]
pub enum WormError {
    /// 관리자 승인(authorized=true) 없이 해제를 시도함
    Unauthorized(String),
}
impl std::fmt::Display for WormError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized(p) => write!(f, "WORM 보호 해제 거부: {p} (명시적 관리자 승인 필요)"),
        }
    }
}
impl std::error::Error for WormError {}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct ProtectStats {
    pub blobs: u32,
    pub snapshots: u32,
}

fn set_readonly(p: &Path, ro: bool) -> io::Result<()> {
    let mut perm = fs::metadata(p)?.permissions();
    #[allow(clippy::permissions_set_readonly_false)]
    perm.set_readonly(ro);
    fs::set_permissions(p, perm)
}

#[cfg(windows)]
fn icacls(target: &Path, args: &[&str]) -> bool {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    if !target.exists() { return false; }
    match Command::new("icacls").arg(target).args(args).creation_flags(CREATE_NO_WINDOW).output() {
        Ok(o) => {
            let out = format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr));
            o.status.success() && !out.contains("0 files processed") && !out.contains("Access denied")
        }
        Err(_) => false,
    }
}
#[cfg(not(windows))]
fn icacls(_target: &Path, _args: &[&str]) -> bool { let _ = Command::new("true"); false }

/// 파일 보호: 읽기 전용 + (옵션) NTFS 거부 ACL. 둘 다 성공해야 true.
pub fn protect_file(path: &Path, use_acl: bool) -> bool {
    if !path.exists() { return false; }
    let ro = set_readonly(path, true).is_ok();
    if cfg!(windows) && use_acl {
        let deny = format!("{SID_EVERYONE}:(DE,WD,AD)");
        return icacls(path, &["/deny", &deny]) && ro;
    }
    ro
}

pub fn unprotect_file(path: &Path, authorized: bool) -> Result<bool, WormError> {
    if !authorized { return Err(WormError::Unauthorized(path.display().to_string())); }
    if !path.exists() { return Ok(false); }
    let acl = if cfg!(windows) { icacls(path, &["/remove:d", SID_EVERYONE]) } else { true };
    let ro = set_readonly(path, false).is_ok();
    Ok(acl && ro)
}

/// 디렉터리 보호: 하위 항목 삭제(Delete Child) 거부 ACL.
pub fn protect_directory(dir: &Path) -> bool {
    if !dir.is_dir() { return false; }
    if cfg!(windows) {
        let deny = format!("{SID_EVERYONE}:(DC)");
        return icacls(dir, &["/deny", &deny]);
    }
    true
}

pub fn unprotect_directory(dir: &Path, authorized: bool) -> Result<bool, WormError> {
    if !authorized { return Err(WormError::Unauthorized(dir.display().to_string())); }
    if !dir.is_dir() { return Ok(false); }
    let ok = if cfg!(windows) {
        // 재귀 1회로 트리 전체 Deny 해제 (프로세스 폭주 방지)
        icacls(dir, &["/remove:d", SID_EVERYONE, "/t", "/c", "/q"])
    } else {
        true
    };
    for e in walk(dir) {
        let _ = set_readonly(&e, false);
    }
    Ok(ok)
}

fn walk(dir: &Path) -> Vec<std::path::PathBuf> {
    let mut out = vec![];
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        if let Ok(rd) = fs::read_dir(&d) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() { stack.push(p) } else { out.push(p) }
            }
        }
    }
    out
}

/// 저장소의 스냅샷 매니페스트(.json)와 blobs 트리를 일괄 보호한다.
pub fn protect_repository(repo: &Path, protect_dirs: bool) -> ProtectStats {
    let mut st = ProtectStats::default();
    if let Ok(rd) = fs::read_dir(repo.join("snapshots")) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map_or(false, |x| x == "json") && protect_file(&p, true) { st.snapshots += 1; }
        }
    }
    let blobs = repo.join("blobs");
    if blobs.is_dir() {
        if protect_dirs { protect_directory(&blobs); }
        for p in walk(&blobs) {
            if p.extension().map_or(false, |x| x == "blob") && protect_file(&p, true) { st.blobs += 1; }
        }
    }
    st
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unprotect_requires_authorization() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("a.blob");
        fs::write(&f, b"x").unwrap();
        assert!(matches!(unprotect_file(&f, false), Err(WormError::Unauthorized(_))));
        assert!(matches!(unprotect_directory(d.path(), false), Err(WormError::Unauthorized(_))));
    }

    #[test]
    fn readonly_attribute_roundtrip_without_acl() {
        // ACL 은 건드리지 않고(use_acl=false) 읽기 전용 속성만 시험한다.
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("a.blob");
        fs::write(&f, b"x").unwrap();
        assert!(protect_file(&f, false));
        assert!(fs::write(&f, b"y").is_err(), "읽기 전용이면 쓰기가 거부되어야 함");
        set_readonly(&f, false).unwrap();
        assert!(fs::write(&f, b"y").is_ok());
        assert!(!protect_file(&d.path().join("none"), false));
    }

    #[test]
    fn missing_targets_are_not_errors() {
        let d = tempfile::tempdir().unwrap();
        assert_eq!(unprotect_file(&d.path().join("n"), true), Ok(false));
        assert_eq!(unprotect_directory(&d.path().join("n"), true), Ok(false));
    }
}
