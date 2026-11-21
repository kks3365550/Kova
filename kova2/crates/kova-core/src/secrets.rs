//! 비밀 파일(구글 드라이브 토큰 `gdrive_token.bin`, 저장소 키 폴더 `keys\`) 접근 제한.
//! DPAPI(PC 범위)만으로는 이 PC의 다른 일반 사용자도 풀 수 있으므로, 파일 자체를 SYSTEM·Administrators 만
//! 열 수 있게 잠근다. 일반 권한 앱은 이 파일을 직접 열지 않고 서비스(SYSTEM)에 IPC 로 맡긴다.
//! 이 모듈은 경로만 다루며 내용(토큰·키)을 기록하지 않는다.
use std::fs;
use std::path::Path;

const SYSTEM_SID: &str = "*S-1-5-18";
const ADMINS_SID: &str = "*S-1-5-32-544";

/// 이 프로세스가 SYSTEM 이거나 Administrators 그룹이 켜진(권한 상승된) 토큰으로 도는가.
#[cfg(windows)]
pub fn process_is_privileged() -> bool {
    #[link(name = "advapi32")]
    extern "system" {
        fn CreateWellKnownSid(
            kind: i32,
            domain: *mut core::ffi::c_void,
            sid: *mut core::ffi::c_void,
            size: *mut u32,
        ) -> i32;
        fn CheckTokenMembership(
            token: *mut core::ffi::c_void,
            sid: *mut core::ffi::c_void,
            is_member: *mut i32,
        ) -> i32;
    }
    const WIN_LOCAL_SYSTEM_SID: i32 = 22;
    const WIN_BUILTIN_ADMINISTRATORS_SID: i32 = 26;
    let has = |kind: i32| {
        let mut sid = [0u32; 17]; // SECURITY_MAX_SID_SIZE(68바이트), 4바이트 정렬
        let mut size = std::mem::size_of_val(&sid) as u32;
        let mut member = 0i32;
        // SAFETY: 크기를 알려 준 버퍼에 SID 를 만들고, 널 토큰(현재 스레드/프로세스)으로 소속을 확인한다.
        unsafe {
            CreateWellKnownSid(
                kind,
                std::ptr::null_mut(),
                sid.as_mut_ptr() as *mut _,
                &mut size,
            ) != 0
                && CheckTokenMembership(
                    std::ptr::null_mut(),
                    sid.as_mut_ptr() as *mut _,
                    &mut member,
                ) != 0
                && member != 0
        }
    };
    has(WIN_LOCAL_SYSTEM_SID) || has(WIN_BUILTIN_ADMINISTRATORS_SID)
}
#[cfg(not(windows))]
pub fn process_is_privileged() -> bool {
    false
}

#[cfg(windows)]
fn icacls(path: &Path, args: &[&str]) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    let o = std::process::Command::new("icacls.exe")
        .arg(path)
        .args(args)
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output()
        .map_err(|e| format!("icacls 실행 실패: {e}"))?;
    if !o.status.success() {
        return Err(format!(
            "icacls 실패({:?}) {}: {}",
            o.status.code(),
            path.display(),
            String::from_utf8_lossy(&o.stdout).trim()
        ));
    }
    Ok(())
}
#[cfg(not(windows))]
fn icacls(_: &Path, _: &[&str]) -> Result<(), String> {
    Ok(())
}

/// ACL 만 바꾼다: 상속을 끊고 SYSTEM·Administrators 전체 권한만 남긴다(폴더면 하위에 상속).
/// 폴더 안의 기존 파일은 명시 ACE 를 지우고 이 폴더의 제한된 ACL 을 상속하게 한다.
pub fn restrict_acl(path: &Path) -> Result<(), String> {
    let dir = path.is_dir();
    let (sy, ba) = if dir {
        (
            format!("{SYSTEM_SID}:(OI)(CI)F"),
            format!("{ADMINS_SID}:(OI)(CI)F"),
        )
    } else {
        (format!("{SYSTEM_SID}:F"), format!("{ADMINS_SID}:F"))
    };
    icacls(path, &["/inheritance:r", "/grant:r", &sy, &ba, "/Q"])?;
    if dir {
        for e in fs::read_dir(path).map_err(|e| format!("{}: {e}", path.display()))? {
            let p = e.map_err(|e| e.to_string())?.path();
            if p.is_file() {
                icacls(&p, &["/reset", "/Q"])?;
            }
        }
    }
    Ok(())
}

/// SYSTEM·관리자 프로세스(서비스)에서만: 소유자를 Administrators 로 바꾸고(예전 앱이 만든 파일의 소유자가
/// 권한을 되돌리지 못하게) ACL 을 제한한다. 일반 권한 프로세스(시험 포함)에서는 아무것도 하지 않는다.
pub fn harden(path: &Path) -> Result<(), String> {
    if !process_is_privileged() || !path.exists() {
        return Ok(());
    }
    if path.is_dir() {
        icacls(path, &["/setowner", ADMINS_SID, "/T", "/Q"])?;
    } else {
        icacls(path, &["/setowner", ADMINS_SID, "/Q"])?;
    }
    restrict_acl(path)
}

/// 비밀 파일 쓰기: 빈 임시 파일을 먼저 잠근 뒤 내용을 쓰고 이름을 바꾼다(잠기기 전 내용이 보이는 틈이 없다).
pub fn write_private(path: &Path, data: &[u8]) -> Result<(), String> {
    use std::io::Write;
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let tmp = path.with_file_name(format!("{name}.{}.tmp", std::process::id()));
    let _ = fs::remove_file(&tmp);
    let write = || -> Result<(), String> {
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp)
            .map_err(|e| e.to_string())?;
        harden(&tmp)?;
        let mut f = fs::OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|e| e.to_string())?;
        f.write_all(data).map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
        drop(f);
        fs::rename(&tmp, path).map_err(|e| e.to_string())
    };
    write().inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

/// 시작할 때마다 `setup` 을 부르는 전용 스레드 풀 안에서 `f` 를 돌린다(그 안의 병렬 작업도 이 풀에서 돈다).
/// 서비스가 복원 파일 쓰기를 요청한 사용자 권한(가장)으로 하게 할 때 쓴다. `setup` 이 false 면 그 스레드가
/// 서비스(SYSTEM) 권한으로 일하는 일이 없도록 프로세스를 즉시 끝낸다(장애 재시작 설정으로 다시 뜬다).
pub fn run_in_setup_pool<R: Send>(
    setup: impl Fn() -> bool + Send + Sync + 'static,
    f: impl FnOnce() -> R + Send,
) -> Result<R, String> {
    let pool = rayon::ThreadPoolBuilder::new()
        .thread_name(|i| format!("kova-as-caller-{i}"))
        .start_handler(move |_| {
            if !setup() {
                std::process::abort();
            }
        })
        .build()
        .map_err(|e| format!("작업 스레드 풀 만들기 실패: {e}"))?;
    Ok(pool.install(f))
}

/// 서비스 시작 시: 이전 버전이 남긴 토큰 파일·키 폴더를 제한 ACL 로 옮긴다(내용은 그대로라 업그레이드 뒤에도 읽힌다).
/// 돌려주는 문구에는 경로만 있다.
pub fn migrate(config_dir: &Path) -> Vec<Result<String, String>> {
    let mut out = Vec::new();
    let token = config_dir.join(crate::gdrive::TOKEN_FILE);
    let keys = crate::repocrypto::key_dir();
    for p in [token, keys] {
        if p.exists() {
            out.push(harden(&p).map(|()| format!("비밀 파일 접근 제한 적용: {}", p.display())));
        }
    }
    out
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    /// 제한하면 일반 권한(이 시험 프로세스)으로는 내용을 읽을 수 없다. 부모 폴더 권한으로 지울 수는 있다.
    #[test]
    fn restricted_file_is_unreadable_for_unprivileged_process() {
        if process_is_privileged() {
            return; // 권한 상승된 실행에서는 관리자 ACE 로 읽히므로 이 확인은 일반 권한에서만 한다
        }
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("secret.bin");
        fs::write(&f, b"x").unwrap();
        assert_eq!(fs::read(&f).unwrap(), b"x");
        restrict_acl(&f).unwrap();
        let e = fs::read(&f).unwrap_err();
        assert_eq!(e.kind(), std::io::ErrorKind::PermissionDenied);
        fs::remove_file(&f).unwrap();
    }

    /// 풀 안의 병렬 작업은 모두 setup 을 거친 스레드에서 돈다.
    #[test]
    fn setup_pool_runs_all_parallel_work_on_prepared_threads() {
        use rayon::prelude::*;
        thread_local!(static READY: std::cell::Cell<bool> = const { std::cell::Cell::new(false) });
        let all = run_in_setup_pool(
            || {
                READY.with(|r| r.set(true));
                true
            },
            || (0..2000).into_par_iter().all(|_| READY.with(|r| r.get())),
        )
        .unwrap();
        assert!(all);
        assert!(!READY.with(|r| r.get()), "호출한 스레드는 바뀌지 않는다");
    }

    /// 일반 권한 프로세스에서는 harden 이 아무것도 바꾸지 않고, write_private 는 그대로 쓴다.
    #[test]
    fn write_private_writes_atomically_without_leftover_temp() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("t.bin");
        write_private(&f, b"one").unwrap();
        write_private(&f, b"two").unwrap();
        if !process_is_privileged() {
            assert_eq!(fs::read(&f).unwrap(), b"two");
        }
        let names: Vec<_> = fs::read_dir(d.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from("t.bin")]);
    }
}
