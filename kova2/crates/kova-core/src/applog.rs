//! 회전 로그 파일: 서비스·엔진의 작업 이력과 오류를 `<config_dir>/logs/kova-service.log`에 남긴다.
//! 용량 상한을 넘으면 `.1` ~ `.N`으로 자동 회전해 디스크가 무한정 커지지 않게 한다.
use chrono::Local;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const LOG_DIR_NAME: &str = "logs";
pub const LOG_FILE_NAME: &str = "kova-service.log";
pub const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024; // 5 MiB
pub const MAX_BACKUPS: usize = 5;

static WRITE_GUARD: Mutex<()> = Mutex::new(());

pub fn log_dir(config_dir: &Path) -> PathBuf {
    config_dir.join(LOG_DIR_NAME)
}

pub fn log_path(config_dir: &Path) -> PathBuf {
    log_dir(config_dir).join(LOG_FILE_NAME)
}

/// 로그 파일에 한 줄 추가한다. `max_bytes`를 초과하면 최대 `max_backups`개까지 회전한다.
pub fn append_with_limits(
    config_dir: &Path,
    level: &str,
    message: &str,
    max_bytes: u64,
    max_backups: usize,
) -> std::io::Result<()> {
    let _g = WRITE_GUARD.lock().unwrap_or_else(|e| e.into_inner());
    let dir = log_dir(config_dir);
    fs::create_dir_all(&dir)?;
    let path = dir.join(LOG_FILE_NAME);
    if max_bytes > 0 {
        if let Ok(meta) = fs::metadata(&path) {
            if meta.len() >= max_bytes {
                rotate(&path, max_backups);
            }
        }
    }
    let mut f = OpenOptions::new().create(true).append(true).open(&path)?;
    let ts = Local::now().format("%Y-%m-%d %H:%M:%S");
    let clean = message.replace(['\r', '\n'], " ");
    writeln!(f, "[{ts}] [{level}] {clean}")?;
    Ok(())
}

/// 기본 제한(5MiB, 백업 5개)으로 로그를 기록한다. 로그 기록 실패가 본 작업을 깨지 않도록 무시한다.
pub fn write(config_dir: &Path, level: &str, message: &str) {
    let _ = append_with_limits(config_dir, level, message, MAX_LOG_BYTES, MAX_BACKUPS);
}

fn rotate(path: &Path, max_backups: usize) {
    if max_backups == 0 {
        let _ = fs::remove_file(path);
        return;
    }
    let oldest = backup_path(path, max_backups);
    let _ = fs::remove_file(&oldest);
    for i in (1..max_backups).rev() {
        let from = backup_path(path, i);
        let to = backup_path(path, i + 1);
        if from.exists() {
            let _ = fs::rename(&from, &to);
        }
    }
    if path.exists() {
        let _ = fs::rename(path, backup_path(path, 1));
    }
}

fn backup_path(base: &Path, index: usize) -> PathBuf {
    PathBuf::from(format!("{}.{index}", base.display()))
}

/// 진단 번들에 담을 로그 파일 목록(현재 파일 + 회전 백업 파일)을 반환한다.
pub fn collect_log_files(config_dir: &Path) -> Vec<(String, PathBuf)> {
    let mut out = Vec::new();
    let base = log_path(config_dir);
    if base.is_file() {
        out.push((format!("{LOG_DIR_NAME}/{LOG_FILE_NAME}"), base.clone()));
    }
    for i in 1..=MAX_BACKUPS {
        let p = backup_path(&base, i);
        if p.is_file() {
            out.push((format!("{LOG_DIR_NAME}/{LOG_FILE_NAME}.{i}"), p));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_and_rotates_logs_within_backup_limit() {
        let d = tempfile::tempdir().unwrap();
        for i in 0..8 {
            append_with_limits(d.path(), "INFO", &format!("line-{i}-payload"), 40, 2).unwrap();
        }
        let main = log_path(d.path());
        assert!(main.is_file());
        assert!(backup_path(&main, 1).is_file());
        assert!(backup_path(&main, 2).is_file());
        assert!(!backup_path(&main, 3).exists());

        let collected = collect_log_files(d.path());
        assert_eq!(collected.len(), 3);
        assert_eq!(collected[0].0, "logs/kova-service.log");
    }

    #[test]
    fn sanitizes_newlines_in_log_messages() {
        let d = tempfile::tempdir().unwrap();
        append_with_limits(d.path(), "ERROR", "first\nsecond\r\nthird", 1024, 1).unwrap();
        let content = fs::read_to_string(log_path(d.path())).unwrap();
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(lines.len(), 1);
        assert!(lines[0].contains("[ERROR] first second  third"));
    }
}
