//! 진단 보고서: 문제 상담용 텍스트. 설정 경로·프로파일 상태·알림만 담고 비밀값(키·토큰)은 담지 않는다.
use crate::alerts::AlertLog;
use crate::config::ConfigStore;
use crate::history::History;
use std::path::Path;

fn ts(t: f64) -> String {
    format!("{:.0} (unix)", t)
}

pub fn report(config_dir: &Path, version: &str, service_line: &str) -> String {
    let mut o = String::new();
    let mut line = |s: String| {
        o.push_str(&s);
        o.push('\n');
    };
    line(format!("Kova2 진단 보고서 (앱 {version})"));
    line(format!("생성 시각: {}", ts(now())));
    line(format!("서비스: {service_line}"));
    line(format!("설정 폴더: {}", config_dir.display()));
    let store = ConfigStore::new(config_dir);
    let s = store.settings();
    line(format!(
        "설정: 랜섬웨어 보호={} 최소 여유공간={}GB 오프사이트={}",
        s.enable_ransomware_protection, s.min_free_disk_gb, s.offsite_enabled
    ));
    line(String::new());
    let profiles = store.profiles();
    line(format!("프로파일 {}개", profiles.len()));
    for p in &profiles {
        let repo = Path::new(&p.repo_dir);
        let snaps = if repo.join("metadata.db").exists() {
            History::open(repo)
                .ok()
                .and_then(|h| h.list_snapshots().ok())
                .map(|v| v.len().to_string())
                .unwrap_or_else(|| "읽기 실패".into())
        } else {
            "이력 DB 없음".into()
        };
        line(format!("- {} ({})", p.name, p.id));
        for src in &p.sources {
            line(format!(
                "    원본: {src} [{}]",
                if Path::new(src).exists() {
                    "있음"
                } else {
                    "없음"
                }
            ));
        }
        line(format!(
            "    저장소: {} [{}] 스냅샷 {snaps}개",
            p.repo_dir,
            if repo.exists() { "있음" } else { "없음" }
        ));
        line(format!(
            "    자동 백업={} 일정={}:{} 보존 {}개/{}일 압축 {}",
            p.auto_backup_enabled,
            p.schedule_type,
            p.schedule_value,
            p.retention_count,
            p.retention_days,
            p.compression_level
        ));
        line(format!(
            "    마지막 결과: {} / {}",
            p.last_status,
            p.last_run.map(ts).unwrap_or_else(|| "기록 없음".into())
        ));
    }
    line(String::new());
    let alerts = AlertLog::new(config_dir).list();
    line(format!(
        "알림 {}건 (미확인 {}건), 최근 10건:",
        alerts.len(),
        alerts.iter().filter(|a| !a.acknowledged).count()
    ));
    for a in alerts.iter().rev().take(10) {
        line(format!(
            "- {} [{}] {}: {}",
            ts(a.at),
            a.level,
            a.profile_id,
            a.message
        ));
    }
    o
}

/// 진단 ZIP: 보고서 + 디스크·BitLocker 상태 + 알림 기록. 키·비밀번호·토큰은 담지 않는다.
pub fn bundle(
    config_dir: &Path,
    version: &str,
    service_line: &str,
    out: &Path,
) -> std::io::Result<()> {
    use std::io::Write;
    let mut z = zip::ZipWriter::new(std::fs::File::create(out)?);
    let o = zip::write::SimpleFileOptions::default();
    // 웹훅 URL(비밀 토큰 포함)이 알림·로그에 남아 있어도 ZIP 에는 가려서 담는다
    let hook = ConfigStore::new(config_dir).settings().webhook_url;
    let mut put = |name: &str, bytes: &[u8]| -> std::io::Result<()> {
        z.start_file(name, o).map_err(std::io::Error::other)?;
        if hook.trim().is_empty() {
            return z.write_all(bytes);
        }
        let text = String::from_utf8_lossy(bytes);
        z.write_all(crate::notify::redact(&text, &hook).as_bytes())
    };
    put(
        "report.txt",
        report(config_dir, version, service_line).as_bytes(),
    )?;
    put("health.txt", crate::health::report().as_bytes())?;
    if let Ok(b) = std::fs::read(config_dir.join("alerts.json")) {
        put("alerts.json", &b)?;
    }
    for (zip_name, log_path) in crate::applog::collect_log_files(config_dir) {
        if let Ok(b) = std::fs::read(&log_path) {
            put(&zip_name, &b)?;
        }
    }
    z.finish().map_err(std::io::Error::other)?;
    Ok(())
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Profile, Settings};

    #[test]
    fn report_lists_profiles_and_missing_paths_without_failing() {
        let d = tempfile::tempdir().unwrap();
        let cfg = d.path().join("cfg");
        let store = ConfigStore::new(&cfg);
        let mut p = Profile::new("p1", "샘플");
        p.sources = vec![d.path().join("없는폴더").to_string_lossy().into()];
        p.repo_dir = d.path().join("repo").to_string_lossy().into();
        store.upsert(p).unwrap();
        let r = report(&cfg, "0.0.1", "연결됨");
        assert!(r.contains("프로파일 1개") && r.contains("샘플") && r.contains("[없음]"));
        assert!(r.contains("이력 DB 없음") && r.contains("알림 0건"));
        assert!(r.contains("서비스: 연결됨"));
    }

    #[test]
    fn bundle_zip_contains_report_and_health() {
        let d = tempfile::tempdir().unwrap();
        let cfg = d.path().join("cfg");
        crate::applog::write(&cfg, "INFO", "diag log sample");
        let out = d.path().join("diag.zip");
        bundle(&cfg, "0.0.1", "연결됨", &out).unwrap();
        let mut z = zip::ZipArchive::new(std::fs::File::open(&out).unwrap()).unwrap();
        assert!(z.by_name("report.txt").is_ok());
        assert!(z.by_name("health.txt").is_ok());
        assert!(z.by_name("logs/kova-service.log").is_ok());
    }

    #[test]
    fn bundle_masks_webhook_url_in_alerts_and_logs() {
        use std::io::Read;
        let d = tempfile::tempdir().unwrap();
        let cfg = d.path().join("cfg");
        let store = ConfigStore::new(&cfg);
        let hook = "https://hooks.example.com/services/T1/B2/TOPSECRET";
        let s = Settings {
            webhook_url: hook.into(),
            ..Default::default()
        };
        store.save_settings(&s).unwrap();
        AlertLog::new(&cfg)
            .push(crate::alerts::Alert {
                at: 1.0,
                profile_id: "p".into(),
                level: "error".into(),
                message: format!("웹훅 전송 실패: {hook}: refused"),
                acknowledged: false,
            })
            .unwrap();
        crate::applog::write(&cfg, "ERROR", &format!("post {hook} failed"));
        let out = d.path().join("diag.zip");
        bundle(&cfg, "0.0.1", "연결됨", &out).unwrap();
        let mut z = zip::ZipArchive::new(std::fs::File::open(&out).unwrap()).unwrap();
        for i in 0..z.len() {
            let mut f = z.by_index(i).unwrap();
            let mut s = String::new();
            let _ = f.read_to_string(&mut s);
            assert!(!s.contains("TOPSECRET"), "{} 에 웹훅 토큰이 남음", f.name());
        }
        let mut a = String::new();
        z.by_name("alerts.json")
            .unwrap()
            .read_to_string(&mut a)
            .unwrap();
        assert!(a.contains("https://hooks.example.com/***"));
    }
}
