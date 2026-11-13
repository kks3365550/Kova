//! 프로파일 입력 화면의 검사: 입력 문자열을 검증해 Profile 로 만든다. 새 프로파일은 자동 백업이 꺼진 채로 시작한다.
use crate::config::Profile;
use std::path::Path;

fn abs_ok(p: &str) -> bool {
    let b = p.as_bytes();
    !p.starts_with("\\\\.\\")
        && !p.starts_with("\\\\?\\")
        && ((b.len() >= 3 && b[1] == b':' && b[2] == b'\\') || p.starts_with("\\\\"))
}

fn norm(p: &str) -> String {
    p.trim()
        .trim_matches('"')
        .trim()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn inside(child: &str, parent: &str) -> bool {
    child == parent || child.starts_with(&format!("{parent}\\"))
}

/// `base` 가 있으면 수정(id·일정·마지막 실행 기록 등은 그대로), 없으면 새로 만든다. `others` 는 이름 중복 검사용 다른 프로파일들.
#[allow(clippy::too_many_arguments)]
pub fn build(
    base: Option<&Profile>,
    new_id: &str,
    name: &str,
    sources: &str,
    repo_dir: &str,
    count: &str,
    days: &str,
    others: &[Profile],
) -> Result<Profile, String> {
    let name = name.trim();
    if name.is_empty() {
        return Err("이름을 입력하세요".into());
    }
    let me = base.map(|b| b.id.as_str()).unwrap_or(new_id);
    if others.iter().any(|o| o.id != me && o.name == name) {
        return Err("같은 이름의 프로파일이 이미 있습니다".into());
    }
    let srcs: Vec<String> = sources
        .split(';')
        .map(|s| s.trim().trim_matches('"').trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    if srcs.is_empty() {
        return Err("백업할 폴더를 입력하세요 (여러 개는 ; 로 구분)".into());
    }
    for s in &srcs {
        let s = s.replace('/', "\\");
        if !abs_ok(&s) {
            return Err(format!("백업 대상은 전체 경로여야 합니다: {s}"));
        }
        if !Path::new(&s).exists() {
            return Err(format!("백업 대상이 없습니다: {s}"));
        }
    }
    let repo = repo_dir.trim().trim_matches('"').trim().replace('/', "\\");
    if repo.is_empty() {
        return Err("백업을 저장할 폴더를 입력하세요".into());
    }
    if !abs_ok(&repo) {
        return Err("저장 폴더는 전체 경로(예: D:\\백업저장소)여야 합니다".into());
    }
    let rp = Path::new(&repo);
    if rp.parent().is_none() || rp.file_name().is_none() {
        return Err("드라이브 루트는 저장 폴더로 쓸 수 없습니다 — 폴더를 지정하세요".into());
    }
    if rp.is_file() {
        return Err("저장 폴더 자리에 같은 이름의 파일이 있습니다".into());
    }
    let rn = norm(&repo);
    for s in &srcs {
        let sn = norm(s);
        if inside(&rn, &sn) || inside(&sn, &rn) {
            return Err("저장 폴더와 백업 대상이 서로 겹칩니다".into());
        }
    }
    let count: u32 = count
        .trim()
        .parse()
        .ok()
        .filter(|n| (1..=1000).contains(n))
        .ok_or("보존 개수는 1~1000 사이의 숫자여야 합니다")?;
    let days: u32 = days
        .trim()
        .parse()
        .ok()
        .filter(|n| (1..=3650).contains(n))
        .ok_or("보존 일수는 1~3650 사이의 숫자여야 합니다")?;
    let mut p = base.cloned().unwrap_or_else(|| Profile::new(new_id, name));
    p.name = name.into();
    p.sources = srcs;
    p.repo_dir = repo;
    p.retention_count = count;
    p.retention_days = days;
    Ok(p)
}

/// 주간·월간 보관 개수(0=끔)를 프로파일에 저장한다. 파이썬 앱과 같은 파일을 쓰므로 extra 필드로 둔다.
pub fn apply_keep(mut p: Profile, weekly: &str, monthly: &str) -> Result<Profile, String> {
    let parse = |s: &str, what: &str| -> Result<u32, String> {
        let s = s.trim();
        if s.is_empty() {
            return Ok(0);
        }
        s.parse::<u32>()
            .ok()
            .filter(|n| *n <= 520)
            .ok_or(format!("{what} 보관은 0~520 사이의 숫자여야 합니다"))
    };
    let (w, m) = (parse(weekly, "주간")?, parse(monthly, "월간")?);
    p.extra.insert("keep_weekly".into(), w.into());
    p.extra.insert("keep_monthly".into(), m.into());
    Ok(p)
}

/// 자동 백업 켜기·끄기, 스케줄(N시간마다 / 매일 시:분), 제외 패턴을 프로파일에 반영한다.
/// `daily` 가 참이면 `value` 는 "시:분", 아니면 시간 간격(0보다 크고 8760 이하).
pub fn apply_schedule(
    mut p: Profile,
    auto: bool,
    daily: bool,
    value: &str,
    exclude: &str,
) -> Result<Profile, String> {
    let value = value.trim();
    if daily {
        let (h, m) = value
            .split_once(':')
            .and_then(|(h, m)| Some((h.trim().parse::<u32>().ok()?, m.trim().parse::<u32>().ok()?)))
            .filter(|(h, m)| *h < 24 && *m < 60)
            .ok_or("매일 실행 시각은 시:분 형식이어야 합니다 (예: 02:30)")?;
        p.schedule_type = "daily".into();
        p.schedule_value = format!("{h:02}:{m:02}");
    } else {
        let h: f64 = value
            .parse()
            .ok()
            .filter(|h: &f64| h.is_finite() && *h > 0.0 && *h <= 8760.0)
            .ok_or("실행 간격은 0보다 크고 8760 이하의 시간 숫자여야 합니다")?;
        p.schedule_type = "interval_hours".into();
        p.schedule_value = h.to_string();
    }
    p.auto_backup_enabled = auto;
    p.exclude_patterns = exclude
        .split(';')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    Ok(p)
}

/// 목록에 보일 자동 백업 설명.
pub fn schedule_text(p: &Profile) -> String {
    if !p.auto_backup_enabled {
        return "자동 백업 꺼짐".into();
    }
    match p.schedule_type.as_str() {
        "daily" => format!("매일 {}", p.schedule_value),
        _ => format!("{}시간마다", p.schedule_value),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_and_keeps_schedule_on_edit() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        std::fs::create_dir_all(&src).unwrap();
        let (src, repo) = (
            src.to_str().unwrap().to_string(),
            d.path().join("repo").to_str().unwrap().to_string(),
        );
        let p = build(None, "n1", "신규", &src, &repo, "10", "30", &[]).unwrap();
        assert_eq!((p.id.as_str(), p.retention_count), ("n1", 10));
        assert!(!p.auto_backup_enabled);
        let mut base = p.clone();
        base.auto_backup_enabled = true;
        base.last_status = "success".into();
        let e = build(Some(&base), "x", "신규2", &src, &repo, "5", "7", &[]).unwrap();
        assert!(e.auto_backup_enabled && e.last_status == "success" && e.id == "n1");
        // 같은 이름은 다른 프로파일과만 충돌한다
        assert!(build(
            Some(&base),
            "x",
            "신규",
            &src,
            &repo,
            "5",
            "7",
            std::slice::from_ref(&p)
        )
        .is_ok());
        assert!(build(None, "n2", "신규", &src, &repo, "5", "7", &[p]).is_err());
        let bad =
            |n: &str, s: &str, r: &str, c: &str, dd: &str| build(None, "z", n, s, r, c, dd, &[]);
        assert!(bad("", &src, &repo, "5", "7").is_err());
        assert!(bad("a", "", &repo, "5", "7").is_err());
        assert!(bad("a", "rel\\x", &repo, "5", "7").is_err());
        assert!(bad("a", &format!("{src}\\없음"), &repo, "5", "7").is_err());
        assert!(bad("a", &src, "D:\\", "5", "7").is_err());
        assert!(bad("a", &src, "relative", "5", "7").is_err());
        assert!(bad("a", &src, &format!("{src}\\sub"), "5", "7").is_err());
        assert!(bad("a", &src, &repo, "0", "7").is_err());
        assert!(bad("a", &src, &repo, "5", "abc").is_err());
        assert!(bad("a", &src, r"\\.\PhysicalDrive0", "5", "7").is_err());
    }

    #[test]
    fn schedule_and_exclude_are_validated_and_saved() {
        let p = Profile::new("p", "P");
        let a = apply_schedule(p.clone(), true, false, " 6 ", "*.tmp; node_modules ;;").unwrap();
        assert!(a.auto_backup_enabled);
        assert_eq!(
            (a.schedule_type.as_str(), a.schedule_value.as_str()),
            ("interval_hours", "6")
        );
        assert_eq!(a.exclude_patterns, vec!["*.tmp", "node_modules"]);
        assert_eq!(schedule_text(&a), "6시간마다");
        let d = apply_schedule(p.clone(), true, true, "2:5", "").unwrap();
        assert_eq!(
            (d.schedule_type.as_str(), d.schedule_value.as_str()),
            ("daily", "02:05")
        );
        assert!(d.exclude_patterns.is_empty());
        assert_eq!(schedule_text(&d), "매일 02:05");
        let off = apply_schedule(p.clone(), false, false, "12", "").unwrap();
        assert_eq!(schedule_text(&off), "자동 백업 꺼짐");
        for bad in ["0", "-1", "abc", "9000", "", "nan"] {
            assert!(
                apply_schedule(p.clone(), true, false, bad, "").is_err(),
                "{bad}"
            );
        }
        for bad in ["24:00", "12:60", "12", "ab:cd", ""] {
            assert!(
                apply_schedule(p.clone(), true, true, bad, "").is_err(),
                "{bad}"
            );
        }
    }

    #[test]
    fn keep_counts_are_validated_and_stored_in_extra() {
        let p = Profile::new("p", "P");
        let a = apply_keep(p.clone(), " 4 ", "").unwrap();
        assert_eq!(a.extra["keep_weekly"], 4);
        assert_eq!(a.extra["keep_monthly"], 0);
        assert!(apply_keep(p.clone(), "x", "0").is_err());
        assert!(apply_keep(p, "0", "9999").is_err());
    }
}
