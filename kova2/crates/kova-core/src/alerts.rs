//! 알림 기록: 실패·경고를 `alerts.json` 에 남겨 앱(트레이·화면)이 읽어 보여 준다.
//! 서비스는 화면이 없으므로 "남기기"만 하고, 표시와 확인(읽음) 처리는 앱이 한다. 최근 100건만 유지.
use serde::{Deserialize, Serialize};
use std::io;
use std::path::{Path, PathBuf};

const MAX_ALERTS: usize = 100;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Alert {
    pub at: f64,
    pub profile_id: String,
    /// "error" | "warning"
    pub level: String,
    pub message: String,
    #[serde(default)]
    pub acknowledged: bool,
}

pub struct AlertLog(PathBuf);

impl AlertLog {
    pub fn new(config_dir: &Path) -> Self { Self(config_dir.join("alerts.json")) }

    pub fn list(&self) -> Vec<Alert> {
        std::fs::read(&self.0).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    fn save(&self, v: &[Alert]) -> io::Result<()> {
        if let Some(p) = self.0.parent() { std::fs::create_dir_all(p)?; }
        let tmp = self.0.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(v).map_err(io::Error::other)?)?;
        std::fs::rename(&tmp, &self.0)
    }

    pub fn push(&self, a: Alert) -> io::Result<()> {
        let mut v = self.list();
        v.push(a);
        let n = v.len();
        if n > MAX_ALERTS { v.drain(0..n - MAX_ALERTS); }
        self.save(&v)
    }

    pub fn unacknowledged(&self) -> usize { self.list().iter().filter(|a| !a.acknowledged).count() }

    pub fn acknowledge_all(&self) -> io::Result<()> {
        let mut v = self.list();
        v.iter_mut().for_each(|a| a.acknowledged = true);
        self.save(&v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(i: usize) -> Alert { Alert { at: i as f64, profile_id: "p".into(), level: "error".into(), message: format!("m{i}"), acknowledged: false } }

    #[test]
    fn push_list_ack_and_cap() {
        let d = tempfile::tempdir().unwrap();
        let l = AlertLog::new(d.path());
        assert!(l.list().is_empty());
        for i in 0..105 { l.push(a(i)).unwrap(); }
        let v = l.list();
        assert_eq!(v.len(), 100);
        assert_eq!(v[0].message, "m5", "오래된 것부터 버린다");
        assert_eq!(l.unacknowledged(), 100);
        l.acknowledge_all().unwrap();
        assert_eq!(l.unacknowledged(), 0);
    }

    #[test]
    fn corrupt_file_reads_as_empty_and_recovers() {
        let d = tempfile::tempdir().unwrap();
        std::fs::write(d.path().join("alerts.json"), b"{broken").unwrap();
        let l = AlertLog::new(d.path());
        assert!(l.list().is_empty());
        l.push(a(1)).unwrap();
        assert_eq!(l.list().len(), 1);
    }
}
