//! 저장소 드라이브의 남은 공간 확인(백업 시작 전 검사용).
use std::path::Path;

#[cfg(windows)]
extern "system" {
    fn GetDiskFreeSpaceExW(
        dir: *const u16,
        avail_to_caller: *mut u64,
        total: *mut u64,
        total_free: *mut u64,
    ) -> i32;
}

/// 경로가 있는 드라이브에서 현재 사용자가 쓸 수 있는 바이트 수. 경로가 아직 없으면 가장 가까운 상위 폴더를 쓴다.
/// 알 수 없으면 None(검사를 건너뛰고 백업은 막지 않는다).
pub fn free_bytes(path: &Path) -> Option<u64> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let existing = path.ancestors().find(|p| p.exists())?;
        let wide: Vec<u16> = existing
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let (mut avail, mut total, mut free) = (0u64, 0u64, 0u64);
        // SAFETY: wide 는 NUL 로 끝나는 유효한 UTF-16 문자열이고, 세 출력 포인터는 이 함수 안의 지역 변수다.
        let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut avail, &mut total, &mut free) };
        (ok != 0).then_some(avail)
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        None
    }
}

/// 남은 공간이 최소 기준(GB) 아래면 사용자에게 보여 줄 사유를 돌려준다. 기준이 0이거나 남은 공간을 모르면 통과.
pub fn check_min_free(free: Option<u64>, min_gb: u32) -> Result<(), String> {
    let (Some(free), true) = (free, min_gb > 0) else {
        return Ok(());
    };
    let need = u64::from(min_gb) * 1_073_741_824;
    if free < need {
        return Err(format!(
            "저장소 드라이브의 여유 공간이 부족합니다 (남음 {:.1}GB, 최소 {}GB 필요). 공간을 확보하거나 설정의 최소 여유 공간을 조정하세요",
            free as f64 / 1_073_741_824.0,
            min_gb
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn min_free_threshold() {
        const GB: u64 = 1_073_741_824;
        assert!(check_min_free(Some(10 * GB), 5).is_ok());
        assert!(check_min_free(Some(5 * GB), 5).is_ok()); // 딱 기준만큼이면 통과
        let e = check_min_free(Some(4 * GB), 5).unwrap_err();
        assert!(e.contains("4.0GB") && e.contains("최소 5GB"));
        assert!(check_min_free(Some(0), 0).is_ok()); // 0 은 검사 끔
        assert!(check_min_free(None, 50).is_ok()); // 모르면 막지 않는다
    }

    #[test]
    #[cfg(windows)]
    fn free_bytes_of_existing_and_missing_path() {
        let d = tempfile::tempdir().unwrap();
        assert!(free_bytes(d.path()).is_some_and(|b| b > 0));
        // 아직 없는 하위 경로는 상위 폴더 기준
        assert!(free_bytes(&d.path().join("a").join("b")).is_some());
    }
}
