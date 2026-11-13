//! WORM(불변성) 보호 (core/worm.py 이식).
//! 1) 읽기 전용 속성, 2) Windows 에서는 icacls 로 Everyone 삭제·쓰기 거부 ACL.
//!    해제는 `authorized=true` 가 아니면 거부한다. ACL 변경은 권한·보안 영역이므로
//!    테스트는 읽기 전용 속성과 인가 게이트만 시험하고 실제 ACL 호출은 하지 않는다.
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
            Self::Unauthorized(p) => {
                write!(f, "WORM 보호 해제 거부: {p} (명시적 관리자 승인 필요)")
            }
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
    if !target.exists() {
        return false;
    }
    match Command::new("icacls")
        .arg(target)
        .args(args)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        Ok(o) => {
            let out = format!(
                "{}{}",
                String::from_utf8_lossy(&o.stdout),
                String::from_utf8_lossy(&o.stderr)
            );
            o.status.success()
                && !out.contains("0 files processed")
                && !out.contains("Access denied")
        }
        Err(_) => false,
    }
}
#[cfg(not(windows))]
fn icacls(_target: &Path, _args: &[&str]) -> bool {
    let _ = Command::new("true");
    false
}

/// 파일 보호: 읽기 전용 + (옵션) NTFS 거부 ACL. 둘 다 성공해야 true.
pub fn protect_file(path: &Path, use_acl: bool) -> bool {
    if !path.exists() {
        return false;
    }
    let ro = set_readonly(path, true).is_ok();
    if cfg!(windows) && use_acl {
        let deny = format!("{SID_EVERYONE}:(DE,WD,AD)");
        return icacls(path, &["/deny", &deny]) && ro;
    }
    ro
}

pub fn unprotect_file(path: &Path, authorized: bool) -> Result<bool, WormError> {
    if !authorized {
        return Err(WormError::Unauthorized(path.display().to_string()));
    }
    if !path.exists() {
        return Ok(false);
    }
    let acl = if cfg!(windows) {
        icacls(path, &["/remove:d", SID_EVERYONE])
    } else {
        true
    };
    let ro = set_readonly(path, false).is_ok();
    Ok(acl && ro)
}

/// 디렉터리 보호: 하위 항목 삭제(Delete Child) 거부 ACL.
pub fn protect_directory(dir: &Path) -> bool {
    if !dir.is_dir() {
        return false;
    }
    if cfg!(windows) {
        let deny = format!("{SID_EVERYONE}:(DC)");
        return icacls(dir, &["/deny", &deny]);
    }
    true
}

pub fn unprotect_directory(dir: &Path, authorized: bool) -> Result<bool, WormError> {
    if !authorized {
        return Err(WormError::Unauthorized(dir.display().to_string()));
    }
    if !dir.is_dir() {
        return Ok(false);
    }
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
                if p.is_dir() {
                    stack.push(p)
                } else {
                    out.push(p)
                }
            }
        }
    }
    out
}

/// 보호 파일 삭제 전용 승인 경로. 보존 정책·고아 블롭 정리처럼 Kova 서비스가 스스로 지울 때만 쓴다.
/// 먼저 읽기 전용 속성만 풀어 지워 보고(블롭 대부분은 이것으로 끝나 외부 프로세스를 띄우지 않는다),
/// 그래도 안 되면 ACL 까지 승인 해제한 뒤 지운다. 보호 여부와 무관하게 쓸 수 있다.
pub fn remove_protected(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => return Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Err(e),
        Err(_) => {}
    }
    let _ = set_readonly(path, false);
    if fs::remove_file(path).is_ok() {
        return Ok(());
    }
    let _ = unprotect_file(path, true);
    fs::remove_file(path)
}

/// 아직 보호되지 않은 매니페스트·블롭만 보호한다(백업 직후 호출). 이미 읽기 전용인 파일은 건너뛰므로 반복 호출해도 싸다.
/// 매니페스트는 읽기 전용 + 거부 ACL, 블롭은 읽기 전용 속성만 건다(블롭마다 icacls 를 띄우면 너무 느리고,
/// 디렉터리 ACL 은 임시 파일 이름 바꾸기를 막아 백업을 깨뜨릴 수 있어 쓰지 않는다).
pub fn protect_new_files(repo: &Path) -> ProtectStats {
    let mut st = ProtectStats::default();
    if let Ok(rd) = fs::read_dir(repo.join("snapshots")) {
        for e in rd.flatten() {
            let p = e.path();
            let ro = e
                .metadata()
                .map(|m| m.permissions().readonly())
                .unwrap_or(true);
            if !ro && p.extension().is_some_and(|x| x == "json") && protect_file(&p, true) {
                st.snapshots += 1;
            }
        }
    }
    if let Ok(dirs) = fs::read_dir(repo.join("blobs")) {
        for d in dirs.flatten().filter(|d| d.path().is_dir()) {
            let Ok(files) = fs::read_dir(d.path()) else {
                continue;
            };
            for e in files.flatten() {
                let p = e.path();
                let ro = e
                    .metadata()
                    .map(|m| m.permissions().readonly())
                    .unwrap_or(true);
                if !ro
                    && p.extension().is_some_and(|x| x == "blob")
                    && set_readonly(&p, true).is_ok()
                {
                    st.blobs += 1;
                }
            }
        }
    }
    if let Ok(files) = fs::read_dir(repo.join("packs")) {
        for e in files.flatten() {
            let p = e.path();
            let ro = e
                .metadata()
                .map(|m| m.permissions().readonly())
                .unwrap_or(true);
            if !ro
                && p.extension().is_some_and(|x| x == "pack" || x == "idx")
                && set_readonly(&p, true).is_ok()
            {
                st.blobs += 1;
            }
        }
    }
    st
}

/// 저장소의 스냅샷 매니페스트(.json)와 blobs 트리를 일괄 보호한다.
pub fn protect_repository(repo: &Path, protect_dirs: bool) -> ProtectStats {
    let mut st = ProtectStats::default();
    if let Ok(rd) = fs::read_dir(repo.join("snapshots")) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().is_some_and(|x| x == "json") && protect_file(&p, true) {
                st.snapshots += 1;
            }
        }
    }
    let blobs = repo.join("blobs");
    if blobs.is_dir() {
        if protect_dirs {
            protect_directory(&blobs);
        }
        for p in walk(&blobs) {
            if p.extension().is_some_and(|x| x == "blob") && protect_file(&p, true) {
                st.blobs += 1;
            }
        }
    }
    for p in walk(&repo.join("packs")) {
        if p.extension().is_some_and(|x| x == "pack" || x == "idx") && protect_file(&p, true) {
            st.blobs += 1;
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
        assert!(matches!(
            unprotect_file(&f, false),
            Err(WormError::Unauthorized(_))
        ));
        assert!(matches!(
            unprotect_directory(d.path(), false),
            Err(WormError::Unauthorized(_))
        ));
    }

    #[test]
    fn readonly_attribute_roundtrip_without_acl() {
        // ACL 은 건드리지 않고(use_acl=false) 읽기 전용 속성만 시험한다.
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("a.blob");
        fs::write(&f, b"x").unwrap();
        assert!(protect_file(&f, false));
        assert!(
            fs::write(&f, b"y").is_err(),
            "읽기 전용이면 쓰기가 거부되어야 함"
        );
        set_readonly(&f, false).unwrap();
        assert!(fs::write(&f, b"y").is_ok());
        assert!(!protect_file(&d.path().join("none"), false));
    }

    #[test]
    fn protect_new_files_marks_only_unprotected_blobs_and_removal_still_works() {
        let d = tempfile::tempdir().unwrap();
        let b = d.path().join("blobs").join("ab");
        fs::create_dir_all(&b).unwrap();
        let blob = b.join("ab01.blob");
        fs::write(&blob, b"x").unwrap();
        fs::write(b.join("ab01.tmp"), b"t").unwrap(); // 임시 파일은 건드리지 않는다
        let st = protect_new_files(d.path());
        assert_eq!((st.blobs, st.snapshots), (1, 0));
        assert!(
            fs::write(&blob, b"y").is_err(),
            "보호된 블롭은 덮어쓸 수 없다"
        );
        assert!(fs::write(b.join("ab01.tmp"), b"u").is_ok());
        // 두 번째 호출은 이미 보호된 파일을 다시 세지 않는다
        assert_eq!(protect_new_files(d.path()).blobs, 0);
        // 승인된 삭제 경로는 보호된 파일도 지운다
        assert!(remove_protected(&blob).is_ok() && !blob.exists());
        // 없는 파일은 오류를 그대로 돌려준다
        assert!(remove_protected(&blob).is_err());
    }

    /// 실제 icacls 를 한 번 시험한다. 임시 폴더의 파일 하나에만 적용하고 끝에 해제해 지운다. `cargo test -- --ignored` 로만 실행.
    #[test]
    #[ignore]
    #[cfg(windows)]
    fn acl_protect_and_authorized_remove_on_temp_file() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("m.json");
        fs::write(&f, b"{}").unwrap();
        assert!(protect_file(&f, true));
        // 실측: 파일 ACL 의 삭제 거부는 부모 폴더가 삭제를 허용하면 지워진다(디렉터리 DC 거부만으로도 기존 파일은 지워짐).
        // 그래서 이 보호의 효과는 "덮어쓰기·내용 변조 방지"이고, 의도적인 삭제까지 막는 것은 아니다.
        assert!(
            fs::write(&f, b"x").is_err(),
            "ACL 보호 중에는 쓸 수 없어야 함"
        );
        assert!(remove_protected(&f).is_ok() && !f.exists());
    }

    #[test]
    fn missing_targets_are_not_errors() {
        let d = tempfile::tempdir().unwrap();
        assert_eq!(unprotect_file(&d.path().join("n"), true), Ok(false));
        assert_eq!(unprotect_directory(&d.path().join("n"), true), Ok(false));
    }
}
