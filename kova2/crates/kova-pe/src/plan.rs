//! WinPE 복원용 순수 로직: diskpart 출력 파싱, 스크립트 생성, 안전 검사. (디스크를 직접 건드리지 않는다)

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiskEntry {
    pub num: u32,
    pub status: String,
    pub bytes: u64,
}

pub fn parse_size(num: &str, unit: &str) -> Option<u64> {
    let n: u64 = num.parse().ok()?;
    let mul: u64 = match unit.to_ascii_uppercase().as_str() {
        "B" => 1,
        "KB" => 1 << 10,
        "MB" => 1 << 20,
        "GB" => 1 << 30,
        "TB" => 1 << 40,
        _ => return None,
    };
    Some(n * mul)
}

/// `list disk` 출력에서 디스크 목록을 읽는다. (영문 diskpart 기준)
pub fn parse_list_disk(out: &str) -> Vec<DiskEntry> {
    let mut v = Vec::new();
    for line in out.lines() {
        let t: Vec<&str> = line
            .trim_start_matches([' ', '*'])
            .split_whitespace()
            .collect();
        if t.len() >= 5 && t[0].eq_ignore_ascii_case("disk") {
            if let (Ok(num), Some(bytes)) = (t[1].parse::<u32>(), parse_size(t[3], t[4])) {
                v.push(DiskEntry {
                    num,
                    status: t[2].to_string(),
                    bytes,
                });
            }
        }
    }
    v
}

/// `select volume X` + `detail volume` 출력에서 그 볼륨이 놓인 디스크 번호들을 읽는다.
pub fn parse_detail_volume_disks(out: &str) -> Vec<u32> {
    let mut v = Vec::new();
    for line in out.lines() {
        let t: Vec<&str> = line
            .trim_start_matches([' ', '*'])
            .split_whitespace()
            .collect();
        if t.len() >= 2 && t[0].eq_ignore_ascii_case("disk") {
            if let Ok(n) = t[1].parse::<u32>() {
                v.push(n);
            }
        }
    }
    v
}

pub fn disk_selected_ok(
    disk: &DiskEntry,
    image_disks: &[u32],
    image_bytes: u64,
) -> Result<(), String> {
    // 이미지가 놓인 디스크를 못 알아냈으면 어느 디스크도 지우지 않는다(이미지 디스크를 지우는 것을 막을 수 없으므로)
    if image_disks.is_empty() {
        return Err(
            "could not determine which disk holds the restore image; refusing to erase any disk"
                .into(),
        );
    }
    if image_disks.contains(&disk.num) {
        return Err(format!(
            "disk {} holds the restore image and cannot be erased",
            disk.num
        ));
    }
    if !disk.status.eq_ignore_ascii_case("online") {
        return Err(format!("disk {} is not online ({})", disk.num, disk.status));
    }
    // EFI 260MB + MSR 16MB + 여유 = 512MB
    let need = image_bytes + (512 << 20);
    if disk.bytes < need {
        return Err(format!(
            "disk {} size ({} MB) is smaller than image ({} MB) + boot area",
            disk.num,
            disk.bytes >> 20,
            image_bytes >> 20
        ));
    }
    Ok(())
}

pub fn confirm_phrase(disk: u32) -> String {
    format!("ERASE DISK {disk}")
}

/// 크기(MB)가 정확히 같은 디스크가 하나뿐일 때만 돌려준다.
pub fn find_disk_by_mb(disks: &[DiskEntry], mb: u64) -> Option<&DiskEntry> {
    let mut it = disks.iter().filter(|d| d.bytes >> 20 == mb);
    let first = it.next()?;
    it.next().is_none().then_some(first)
}

pub fn confirm_phrase_mb(mb: u64) -> String {
    format!("ERASE DISK {mb} MB")
}

/// `detail disk` 출력에서 디스크 고유 ID(GPT GUID 또는 MBR 서명)를 읽는다. (영문 diskpart 기준)
pub fn parse_disk_id(out: &str) -> Option<String> {
    out.lines().find_map(|l| {
        let (k, v) = l.split_once(':')?;
        k.trim()
            .eq_ignore_ascii_case("disk id")
            .then(|| v.trim().trim_matches(['{', '}']).to_uppercase())
            .filter(|s| !s.is_empty())
    })
}

pub fn confirm_phrase_id(id: &str) -> String {
    format!(
        "ERASE DISK ID {}",
        id.trim_matches(['{', '}']).to_uppercase()
    )
}

pub fn norm(s: &str) -> String {
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_uppercase()
}

/// UEFI/GPT: EFI(S:) + MSR + NTFS(T:)
pub fn script_gpt(disk: u32, efi: char, os: char) -> String {
    format!(
        "select disk {disk}\nclean\nconvert gpt\ncreate partition efi size=260\nformat quick fs=fat32 label=\"EFI\"\nassign letter={efi}\ncreate partition msr size=16\ncreate partition primary\nformat quick fs=ntfs label=\"KOVA_TARGET\"\nassign letter={os}\nexit\n"
    )
}

/// BIOS/MBR: 시스템 예약(S:, active) + NTFS(T:)
pub fn script_mbr(disk: u32, sys: char, os: char) -> String {
    format!(
        "select disk {disk}\nclean\nconvert mbr\ncreate partition primary size=500\nformat quick fs=ntfs label=\"SYSTEM\"\nassign letter={sys}\nactive\ncreate partition primary\nformat quick fs=ntfs label=\"KOVA_TARGET\"\nassign letter={os}\nexit\n"
    )
}

pub fn script_detail_volume(letter: char) -> String {
    format!("select volume {letter}\ndetail volume\nexit\n")
}

/// 복원 후 NTFS 를 파티션 끝까지 넓힌다(이미지가 파티션보다 작을 때). 실패해도 무방.
pub fn script_extend(os: char) -> String {
    format!("select volume {os}\nextend filesystem\nexit\n")
}

pub fn bcdboot_args(os: char, sys: char, uefi: bool) -> Vec<String> {
    vec![
        format!("{os}:\\Windows"),
        "/s".into(),
        format!("{sys}:"),
        "/f".into(),
        if uefi { "UEFI".into() } else { "BIOS".into() },
    ]
}

/// 한 번 시도한 자동 모드 파일을 바꿔 둘 이름(재부팅마다 디스크를 다시 지우지 않게).
pub const AUTO_DONE_NAME: &str = "KOVA_AUTO.done.txt";

/// 자동 모드의 대상 디스크: 디스크 고유 ID(`disk_id=`)만 받는다. 번호(`disk=`)·크기(`disk_mb=`)는
/// 디스크 순서·구성이 바뀌면 다른 디스크를 지울 수 있어 거부한다.
pub fn auto_disk_id(m: &std::collections::HashMap<String, String>) -> Result<String, String> {
    let id = m
        .get("disk_id")
        .map(|s| s.trim().trim_matches(['{', '}']).to_uppercase())
        .unwrap_or_default();
    if id.is_empty() {
        return Err(if m.contains_key("disk") || m.contains_key("disk_mb") {
            "auto mode accepts only disk_id=<unique disk ID>; disk= and disk_mb= are refused".into()
        } else {
            "auto mode needs disk_id=<unique disk ID> (see diskpart 'detail disk')".into()
        });
    }
    if !id.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err(format!("auto mode disk_id is not a disk ID: {id}"));
    }
    Ok(id)
}

/// 자동 모드 파일(KOVA_AUTO.txt)의 `key=value` 줄을 읽는다.
pub fn parse_auto(text: &str) -> std::collections::HashMap<String, String> {
    text.lines()
        .filter_map(|l| {
            let l = l.trim().trim_start_matches('\u{feff}');
            if l.starts_with('#') {
                return None;
            }
            let (k, v) = l.split_once('=')?;
            Some((k.trim().to_ascii_lowercase(), v.trim().to_string()))
        })
        .collect()
}

/// dism.exe 명령 인자 목록 생성
#[allow(dead_code)]
pub fn dism_export_driver_args(dest: &std::path::Path) -> Vec<String> {
    vec![
        "/online".into(),
        "/export-driver".into(),
        format!("/destination:{}", dest.display()),
    ]
}

/// 기본 드라이버 자동 수집 대상 폴더 (%ProgramData%\Kova2\drivers)
#[allow(dead_code)]
pub fn default_driver_export_dir() -> std::path::PathBuf {
    let base = std::env::var_os("ProgramData")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("C:\\ProgramData"));
    base.join("Kova2").join("drivers")
}

/// 수집된 드라이버 폴더에 .inf 파일이 존재하는지 검사하여 유효한 드라이버 경로를 반환한다.
#[allow(dead_code)]
pub fn find_collected_drivers(dir: &std::path::Path) -> Option<std::path::PathBuf> {
    if !dir.is_dir() {
        return None;
    }
    // 폴더 내 직접 또는 1단계 하위 폴더에 .inf 파일이 있는지 확인
    let has_inf = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok())
        .any(|entry| {
            let p = entry.path();
            if p.extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("inf"))
            {
                return true;
            }
            if p.is_dir() {
                if let Ok(sub) = std::fs::read_dir(&p) {
                    return sub.filter_map(|e| e.ok()).any(|se| {
                        se.path()
                            .extension()
                            .is_some_and(|ext| ext.eq_ignore_ascii_case("inf"))
                    });
                }
            }
            false
        });

    if has_inf {
        Some(dir.to_path_buf())
    } else {
        None
    }
}

/// dism.exe /online /export-driver /destination:<폴더> 명령으로
/// 현재 PC의 서드파티(NVMe, 네트워크 등) 드라이버를 자동 덤프한다.
#[allow(dead_code)]
#[cfg(windows)]
pub fn export_drivers(dest_dir: &std::path::Path) -> Result<String, String> {
    use std::os::windows::process::CommandExt;
    std::fs::create_dir_all(dest_dir).map_err(|e| format!("폴더 생성 실패: {e}"))?;

    let dest_str = dest_dir.to_string_lossy();
    let out = std::process::Command::new("dism.exe")
        .args([
            "/online",
            "/export-driver",
            &format!("/destination:{dest_str}"),
        ])
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output()
        .map_err(|e| format!("dism.exe 실행 실패: {e}"))?;

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    if !out.status.success() {
        let msg = if stderr.trim().is_empty() {
            stdout.trim().to_string()
        } else {
            stderr.trim().to_string()
        };
        if msg.contains("740") || msg.contains("권한") || msg.contains("administrative") {
            return Err("드라이버 수집 실패: 관리자 권한으로 실행해야 합니다".into());
        }
        return Err(format!(
            "dism 드라이버 수집 실패 (종료 코드 {:?}): {}",
            out.status.code(),
            msg
        ));
    }

    Ok(format!("드라이버 자동 수집 완료: {}", dest_dir.display()))
}

#[cfg(not(windows))]
pub fn export_drivers(dest_dir: &std::path::Path) -> Result<String, String> {
    std::fs::create_dir_all(dest_dir).map_err(|e| format!("폴더 생성 실패: {e}"))?;
    Ok(format!(
        "(모의) 드라이버 자동 수집 완료: {}",
        dest_dir.display()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIST: &str = "\n  Disk ###  Status         Size     Free     Dyn  Gpt\n  --------  -------------  -------  -------  ---  ---\n  Disk 0    Online          931 GB  1024 KB        *\n* Disk 1    Online           60 GB      0 B        *\n  Disk 2    Offline         500 MB      0 B\n";

    #[test]
    fn list_disk() {
        let d = parse_list_disk(LIST);
        assert_eq!(d.len(), 3);
        assert_eq!(
            d[1],
            DiskEntry {
                num: 1,
                status: "Online".into(),
                bytes: 60 << 30
            }
        );
        assert_eq!(d[2].bytes, 500 << 20);
    }

    #[test]
    fn detail_volume() {
        let o = "Volume 3\n\n  Disk ###  Status   Size  Free\n  --------  -------\n* Disk 2    Online\n";
        assert_eq!(parse_detail_volume_disks(o), vec![2]);
        assert!(parse_detail_volume_disks("Disk ###").is_empty());
    }

    #[test]
    fn disk_checks() {
        let d = DiskEntry {
            num: 1,
            status: "Online".into(),
            bytes: 60 << 30,
        };
        assert!(disk_selected_ok(&d, &[0], 20 << 30).is_ok());
        assert!(disk_selected_ok(&d, &[1], 20 << 30).is_err());
        assert!(disk_selected_ok(&d, &[], 60 << 30).is_err());
        let off = DiskEntry {
            status: "Offline".into(),
            ..d.clone()
        };
        assert!(disk_selected_ok(&off, &[], 1 << 30).is_err());
        // 이미지 디스크를 못 알아내면(빈 목록) 정상 디스크라도 거부한다
        assert!(disk_selected_ok(&d, &[], 20 << 30)
            .unwrap_err()
            .contains("could not determine"));
    }

    #[test]
    fn auto_mode_accepts_only_disk_id() {
        let ok = parse_auto("image=a.qblk\ndisk_id={919f469d-68f8-4433-9d35-34531b929926}\n");
        assert_eq!(
            auto_disk_id(&ok).as_deref(),
            Ok("919F469D-68F8-4433-9D35-34531B929926")
        );
        assert_eq!(
            auto_disk_id(&parse_auto("disk_id=1A2B3C4D")).as_deref(),
            Ok("1A2B3C4D")
        );
        for bad in [
            "disk=1\nconfirm=ERASE DISK 1",
            "disk_mb=61440\nconfirm=ERASE DISK 61440 MB",
            "image=a.qblk",
            "disk_id=",
            "disk_id=1 or 2",
        ] {
            assert!(auto_disk_id(&parse_auto(bad)).is_err(), "{bad}");
        }
        assert!(auto_disk_id(&parse_auto("disk=1"))
            .unwrap_err()
            .contains("refused"));
    }

    #[test]
    fn scripts_and_phrase() {
        let s = script_gpt(1, 'S', 'T');
        assert!(s.contains("select disk 1\nclean") && s.contains("assign letter=T"));
        assert!(script_mbr(1, 'S', 'T').contains("active"));
        assert_eq!(norm("  erase   disk 1 "), confirm_phrase(1));
        assert_eq!(
            bcdboot_args('T', 'S', true),
            vec!["T:\\Windows", "/s", "S:", "/f", "UEFI"]
        );
    }

    #[test]
    fn disk_by_mb() {
        let d = parse_list_disk(LIST);
        assert_eq!(find_disk_by_mb(&d, 60 << 10).map(|x| x.num), Some(1));
        assert!(find_disk_by_mb(&d, 123).is_none());
        let mut two = d.clone();
        two.push(DiskEntry {
            num: 9,
            status: "Online".into(),
            bytes: 60 << 30,
        });
        assert!(find_disk_by_mb(&two, 60 << 10).is_none());
        assert_eq!(norm("erase disk 61440 mb"), confirm_phrase_mb(61440));
    }

    #[test]
    fn disk_id() {
        let o = "Netac NVMe SSD 1TB
Disk ID: {919F469D-68F8-4433-9D35-34531B929926}
Type   : NVMe
";
        assert_eq!(
            parse_disk_id(o).as_deref(),
            Some("919F469D-68F8-4433-9D35-34531B929926")
        );
        assert_eq!(parse_disk_id("Type : NVMe"), None);
        assert_eq!(
            norm("erase disk id 919f469d-68f8-4433-9d35-34531b929926"),
            confirm_phrase_id("{919F469D-68F8-4433-9D35-34531B929926}")
        );
    }

    #[test]
    fn auto_file() {
        let m = parse_auto("\u{feff}image=a.qblk\n# c\ndisk = 1\nconfirm=ERASE DISK 1\n");
        assert_eq!(m["image"], "a.qblk");
        assert_eq!(m["disk"], "1");
        assert_eq!(m["confirm"], "ERASE DISK 1");
    }

    #[test]
    fn test_dism_export_driver_args() {
        let p = std::path::Path::new("C:\\TestDrivers");
        let args = dism_export_driver_args(p);
        assert_eq!(args[0], "/online");
        assert_eq!(args[1], "/export-driver");
        assert!(args[2].contains("C:\\TestDrivers"));
    }

    #[test]
    fn test_default_driver_export_dir() {
        let d = default_driver_export_dir();
        assert!(d.ends_with("Kova2\\drivers") || d.ends_with("Kova2/drivers"));
    }

    #[test]
    fn test_find_collected_drivers() {
        let tmp = tempfile::tempdir().unwrap();
        // 비어있는 폴더 -> None
        assert_eq!(find_collected_drivers(tmp.path()), None);

        // 하위 폴더에 inf 파일 생성 -> Some
        let sub = tmp.path().join("nvme_driver");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(sub.join("nvme.inf"), "mock inf").unwrap();

        assert_eq!(
            find_collected_drivers(tmp.path()),
            Some(tmp.path().to_path_buf())
        );
    }
}
