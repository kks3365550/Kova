//! 스케줄 판정 (core/scheduler.py `_should_run_profile` 이식). 시각을 인자로 받는 순수 함수라
//! 서비스 틱·시험 어디서나 같은 결과를 낸다. 시스템이 꺼져 있던 동안 놓친 백업은
//! last_run 이 마지막 도래 시각보다 앞서 있으면 켜지는 즉시 따라잡는다.
use crate::config::Profile;
use chrono::{DateTime, Local, TimeZone};

pub const MAX_AUTO_RETRIES: u32 = 3;
pub const RETRY_COOLDOWN_SEC: f64 = 1800.0;

pub fn retry_count(p: &Profile) -> u32 {
    p.extra
        .get("retry_count")
        .and_then(|v| v.as_u64())
        .unwrap_or(0) as u32
}

/// `now` 시점에 이 프로파일의 자동 백업을 시작해야 하는가.
pub fn should_run(p: &Profile, now: DateTime<Local>) -> bool {
    if !p.auto_backup_enabled {
        return false;
    }
    let now_ts = now.timestamp() as f64;

    // 실패 직후 재시도 (최대 횟수 제한). 한도를 넘으면 정상 스케줄을 따른다.
    if p.last_status == "failed"
        && retry_count(p) < MAX_AUTO_RETRIES
        && now_ts - p.last_run.unwrap_or(0.0) >= RETRY_COOLDOWN_SEC
    {
        return true;
    }

    let Some(last) = p.last_run.filter(|l| *l > 0.0) else {
        return true;
    };
    let elapsed = now_ts - last;
    match p.schedule_type.as_str() {
        "interval_hours" => match p.schedule_value.trim().parse::<f64>() {
            Ok(h) if h.is_finite() && h >= 0.0 => elapsed >= h * 3600.0,
            _ => false,
        },
        "daily" => {
            let Some((h, m)) = p.schedule_value.split_once(':').and_then(|(h, m)| {
                Some((h.trim().parse::<u32>().ok()?, m.trim().parse::<u32>().ok()?))
            }) else {
                return false;
            };
            // 같은 시간 안의 빠른 실패·재시도로 재발동하는 루프 방지
            if elapsed < 3600.0 {
                return false;
            }
            let Some(target) = now
                .date_naive()
                .and_hms_opt(h, m, 0)
                .and_then(|t| Local.from_local_datetime(&t).earliest())
            else {
                return false;
            };
            now >= target && last < target.timestamp() as f64
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<Local> {
        Local
            .with_ymd_and_hms(y, mo, d, h, mi, 0)
            .earliest()
            .unwrap()
    }
    fn prof(kind: &str, val: &str, last: Option<DateTime<Local>>) -> Profile {
        let mut p = Profile::new("p", "P");
        p.auto_backup_enabled = true;
        p.schedule_type = kind.into();
        p.schedule_value = val.into();
        p.last_run = last.map(|t| t.timestamp() as f64);
        p
    }

    #[test]
    fn disabled_never_runs_and_first_run_is_immediate() {
        let mut p = prof("interval_hours", "12", None);
        assert!(should_run(&p, at(2026, 11, 11, 9, 0)));
        p.auto_backup_enabled = false;
        assert!(!should_run(&p, at(2026, 11, 11, 9, 0)));
    }

    #[test]
    fn interval_hours() {
        let last = at(2026, 11, 11, 0, 0);
        let p = prof("interval_hours", "12", Some(last));
        assert!(!should_run(&p, at(2026, 11, 11, 11, 59)));
        assert!(should_run(&p, at(2026, 11, 11, 12, 0)));
        assert!(!should_run(
            &prof("interval_hours", "abc", Some(last)),
            at(2026, 12, 1, 0, 0)
        ));
    }

    #[test]
    fn daily_runs_once_after_target_and_catches_up_missed() {
        let p = prof("daily", "03:00", Some(at(2026, 11, 10, 3, 1)));
        assert!(!should_run(&p, at(2026, 11, 11, 2, 59)), "목표 시각 전");
        assert!(should_run(&p, at(2026, 11, 11, 3, 0)));
        // 하루 종일 꺼져 있다가 저녁에 켜져도 오늘 몫을 따라잡는다
        assert!(should_run(&p, at(2026, 11, 11, 21, 0)));
        // 오늘 이미 돌았으면 다시 안 돈다
        let done = prof("daily", "03:00", Some(at(2026, 11, 11, 3, 5)));
        assert!(!should_run(&done, at(2026, 11, 11, 21, 0)));
        // 빠른 재발동 방지: 방금(1시간 미만) 돌았으면 안 돈다
        let just = prof("daily", "03:00", Some(at(2026, 11, 11, 2, 40)));
        assert!(!should_run(&just, at(2026, 11, 11, 3, 10)));
        assert!(!should_run(
            &prof("daily", "25x", Some(at(2026, 11, 1, 0, 0))),
            at(2026, 11, 11, 9, 0)
        ));
    }

    #[test]
    fn failed_backup_retries_after_cooldown_up_to_limit() {
        let last = at(2026, 11, 11, 10, 0);
        let mut p = prof("interval_hours", "24", Some(last));
        p.last_status = "failed".into();
        assert!(!should_run(&p, at(2026, 11, 11, 10, 29)));
        assert!(should_run(&p, at(2026, 11, 11, 10, 30)));
        p.extra.insert("retry_count".into(), 3.into());
        assert!(
            !should_run(&p, at(2026, 11, 11, 10, 30)),
            "한도 초과 시 정상 스케줄을 따름"
        );
        assert!(should_run(&p, at(2026, 11, 12, 10, 0)));
    }
}
