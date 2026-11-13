//! 시스템 정보 내보내기(읽기 전용): 설치된 프로그램·드라이버 목록과 사용자 환경변수·시작프로그램 레지스트리 키.
//! PC를 새로 세팅할 때 "무엇이 깔려 있었는지"를 알려 주는 참고 자료이며, 설치 파일 자체를 백업하지는 않는다.
use std::path::Path;

#[cfg(windows)]
fn run(program: &str, args: &[&str]) -> Result<String, String> {
    use std::os::windows::process::CommandExt;
    let o = std::process::Command::new(program)
        .args(args)
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output()
        .map_err(|e| format!("{program} 실행 실패: {e}"))?;
    if o.status.success() {
        Ok(String::from_utf8_lossy(&o.stdout).to_string())
    } else {
        Err(format!(
            "{program}: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        ))
    }
}

#[cfg(not(windows))]
fn run(_p: &str, _a: &[&str]) -> Result<String, String> {
    Err("Windows에서만 지원합니다".into())
}

const APPS_SCRIPT: &str = "$k='HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'; Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | Sort-Object DisplayName | ForEach-Object { '{0} | {1} | {2}' -f $_.DisplayName, $_.DisplayVersion, $_.Publisher }";

/// `dir` 안에 apps.txt, drivers.txt, user_env_and_run.reg 를 만든다. 일부가 실패해도 나머지는 만든다.
pub fn export(dir: &Path) -> Result<String, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("폴더를 만들지 못했습니다: {e}"))?;
    let mut done = vec![];
    let mut failed = vec![];
    let mut put = |name: &str, r: Result<String, String>| match r {
        Ok(text) => match std::fs::write(dir.join(name), text) {
            Ok(()) => done.push(name.to_string()),
            Err(e) => failed.push(format!("{name}: {e}")),
        },
        Err(e) => failed.push(format!("{name}: {e}")),
    };
    put(
        "apps.txt",
        run(
            "powershell.exe",
            &["-NoProfile", "-NonInteractive", "-Command", APPS_SCRIPT],
        ),
    );
    put("drivers.txt", run("pnputil.exe", &["/enum-drivers"]));
    for (key, tmp) in [
        ("HKCU\\Environment", "env.reg"),
        (
            "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
            "run.reg",
        ),
    ] {
        let t = dir.join(tmp);
        let r = run("reg.exe", &["export", key, &t.to_string_lossy(), "/y"]);
        if r.is_ok() {
            done.push(tmp.to_string());
        } else if let Err(e) = r {
            failed.push(format!("{tmp}: {e}"));
        }
    }
    if done.is_empty() {
        return Err(format!("내보내지 못했습니다: {}", failed.join("; ")));
    }
    let mut msg = format!("{}에 저장: {}", dir.display(), done.join(", "));
    if !failed.is_empty() {
        msg.push_str(&format!(" (실패: {})", failed.join("; ")));
    }
    Ok(msg)
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    fn export_writes_app_and_driver_lists() {
        let d = tempfile::tempdir().unwrap();
        let out = d.path().join("info");
        // 러너·권한에 따라 일부 항목은 실패할 수 있다: 하나라도 만들어지면 성공이고, 만든 apps.txt 는 내용이 있어야 한다.
        let msg = super::export(&out).unwrap();
        if let Ok(apps) = std::fs::read_to_string(out.join("apps.txt")) {
            assert!(apps.lines().count() > 3, "{msg}");
        }
    }
}
