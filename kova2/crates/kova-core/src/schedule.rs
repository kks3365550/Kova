//! 스케줄 판정 (core/scheduler.py `_should_run_profile` 이식). 시각을 인자로 받는 순수 함수라
//! 서비스 틱·시험 어디서나 같은 결과를 낸다. 시스템이 꺼져 있던 동안 놓친 백업은
//! last_run 이 마지막 도래 시각보다 앞서 있으면 켜지는 즉시 따라잡는다.
use crate::config::Profile;
use chrono::{DateTime, Datelike, Local, TimeZone, Weekday};

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
        "weekly" => {
            if elapsed < 3600.0 {
                return false;
            }
            let (days_part, time_part) = match p.schedule_value.split_once('@') {
                Some((d, t)) => (d, t),
                None => ("MON", p.schedule_value.as_str()),
            };
            let days = parse_weekdays(days_part);
            if days.is_empty() || !days.contains(&now.weekday()) {
                return false;
            }
            let Some((h, m)) = parse_hm(time_part) else {
                return false;
            };
            let Some(target) = now
                .date_naive()
                .and_hms_opt(h, m, 0)
                .and_then(|t| Local.from_local_datetime(&t).earliest())
            else {
                return false;
            };
            now >= target && last < target.timestamp() as f64
        }
        "monthly" => {
            if elapsed < 3600.0 {
                return false;
            }
            let (day_part, time_part) = match p.schedule_value.split_once('@') {
                Some((d, t)) => (d, t),
                None => ("1", p.schedule_value.as_str()),
            };
            let Ok(day) = day_part.trim().parse::<u32>() else {
                return false;
            };
            if !(1..=28).contains(&day) {
                return false;
            }
            let Some((h, m)) = parse_hm(time_part) else {
                return false;
            };
            let Some(target_date) = now.date_naive().with_day(day) else {
                return false;
            };
            let Some(target) = target_date
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

fn parse_hm(s: &str) -> Option<(u32, u32)> {
    let (h, m) = s.split_once(':')?;
    Some((h.trim().parse().ok()?, m.trim().parse().ok()?))
}

fn parse_weekdays(s: &str) -> Vec<Weekday> {
    s.split(',')
        .filter_map(|tok| match tok.trim().to_ascii_uppercase().as_str() {
            "MON" | "월" => Some(Weekday::Mon),
            "TUE" | "화" => Some(Weekday::Tue),
            "WED" | "수" => Some(Weekday::Wed),
            "THU" | "목" => Some(Weekday::Thu),
            "FRI" | "금" => Some(Weekday::Fri),
            "SAT" | "토" => Some(Weekday::Sat),
            "SUN" | "일" => Some(Weekday::Sun),
            _ => None,
        })
        .collect()
}

/// 노트북이 AC 전원 없이 배터리로 구동 중이면 true. 데스크톱이거나 확인 실패면 false.
pub fn is_on_battery() -> bool {
    #[cfg(windows)]
    {
        #[repr(C)]
        struct SystemPowerStatus {
            ac_line_status: u8,
            battery_flag: u8,
            battery_life_percent: u8,
            system_status_flag: u8,
            battery_life_time: u32,
            battery_full_life_time: u32,
        }
        extern "system" {
            fn GetSystemPowerStatus(lp: *mut SystemPowerStatus) -> i32;
        }
        let mut st = SystemPowerStatus {
            ac_line_status: 255,
            battery_flag: 0,
            battery_life_percent: 255,
            system_status_flag: 0,
            battery_life_time: 0,
            battery_full_life_time: 0,
        };
        let ok = unsafe { GetSystemPowerStatus(&mut st) };
        ok != 0 && st.ac_line_status == 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}

/// 정기 유지보수 작업(Deep Scrub, GC)을 `now` 시점에 실행해야 하는가.
/// `interval_days == 0`이면 비활성. `last_at`이 없으면 기준 시각(`baseline`)으로부터 경과를 계산한다.
pub fn should_run_maintenance(
    last_at: Option<f64>,
    baseline: Option<f64>,
    interval_days: u32,
    now: DateTime<Local>,
) -> bool {
    if interval_days == 0 {
        return false;
    }
    let Some(ref_ts) = last_at.or(baseline).filter(|t| *t > 0.0) else {
        return false;
    };
    let elapsed = now.timestamp() as f64 - ref_ts;
    elapsed >= (interval_days as f64) * 86_400.0
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

    #[test]
    fn maintenance_runs_after_interval_days_and_respects_zero() {
        let base = at(2026, 11, 1, 3, 0).timestamp() as f64;
        assert!(!should_run_maintenance(
            None,
            None,
            7,
            at(2026, 11, 10, 3, 0)
        ));
        assert!(!should_run_maintenance(
            None,
            Some(base),
            0,
            at(2026, 11, 20, 3, 0)
        ));
        assert!(!should_run_maintenance(
            None,
            Some(base),
            7,
            at(2026, 11, 7, 23, 59)
        ));
        assert!(should_run_maintenance(
            None,
            Some(base),
            7,
            at(2026, 11, 8, 3, 0)
        ));
        let verified = at(2026, 11, 8, 3, 5).timestamp() as f64;
        assert!(!should_run_maintenance(
            Some(verified),
            Some(base),
            7,
            at(2026, 11, 14, 23, 0)
        ));
        assert!(should_run_maintenance(
            Some(verified),
            Some(base),
            7,
            at(2026, 11, 15, 3, 5)
        ));
    }

    #[test]
    fn weekly_and_monthly_schedules_match_expected_times() {
        // 2026-11-16 은 월요일(Mon), 2026-11-17 은 화요일(Tue)
        let prev = at(2026, 11, 9, 3, 5);
        let w = prof("weekly", "MON,WED@03:00", Some(prev));
        assert!(!should_run(&w, at(2026, 11, 16, 2, 59)));
        assert!(should_run(&w, at(2026, 11, 16, 3, 0)));
        assert!(!should_run(&w, at(2026, 11, 17, 10, 0)), "화요일은 제외");

        let w_kr = prof("weekly", "월,수@03:00", Some(prev));
        assert!(should_run(&w_kr, at(2026, 11, 16, 4, 0)));

        let m = prof("monthly", "15@02:30", Some(at(2026, 10, 15, 2, 35)));
        assert!(!should_run(&m, at(2026, 11, 14, 23, 59)));
        assert!(should_run(&m, at(2026, 11, 15, 2, 30)));
        // 15일에 꺼져 있다가 18일에 켜져도 이번 달 몫을 따라잡는다
        assert!(should_run(&m, at(2026, 11, 18, 9, 0)));
        let m_done = prof("monthly", "15@02:30", Some(at(2026, 11, 15, 2, 35)));
        assert!(!should_run(&m_done, at(2026, 11, 18, 9, 0)));
    }
}
