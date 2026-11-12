//! 설정·프로파일 (core/config.py 이식). profiles.json / app_settings.json 과 호환.
//! 모르는 필드는 extra 로 보존해 파이썬 앱과 같은 파일을 공유해도 유실이 없다.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

fn d_sched() -> String {
    "interval_hours".into()
}
fn d_sval() -> String {
    "12".into()
}
fn d_cnt() -> u32 {
    30
}
fn d_days() -> u32 {
    60
}
fn d_comp() -> i32 {
    3
}
fn d_status() -> String {
    "never".into()
}

/// schedule_value 는 파일에 문자열("12")로 저장된다(숫자여도 읽을 수 있게 한다).
fn de_str<'de, D: serde::Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    match Value::deserialize(d)? {
        Value::String(s) => Ok(s),
        Value::Number(n) => Ok(n.to_string()),
        _ => Ok(d_sval()),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Profile {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub repo_dir: String,
    #[serde(default)]
    pub exclude_patterns: Vec<String>,
    #[serde(default = "d_sched")]
    pub schedule_type: String,
    #[serde(default = "d_sval", deserialize_with = "de_str")]
    pub schedule_value: String,
    #[serde(default)]
    pub auto_backup_enabled: bool,
    #[serde(default = "d_cnt")]
    pub retention_count: u32,
    #[serde(default = "d_days")]
    pub retention_days: u32,
    #[serde(default = "d_comp")]
    pub compression_level: i32,
    #[serde(default)]
    pub last_run: Option<f64>,
    #[serde(default = "d_status")]
    pub last_status: String,
    #[serde(default)]
    pub last_snapshot_id: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Profile {
    pub fn new(id: &str, name: &str) -> Self {
        serde_json::from_value(serde_json::json!({ "id": id, "name": name })).unwrap()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Settings {
    #[serde(default = "d_port")]
    pub server_port: u16,
    #[serde(default = "d_host")]
    pub server_host: String,
    #[serde(default = "d_true")]
    pub dark_mode: bool,
    #[serde(default = "d_log")]
    pub log_level: String,
    #[serde(default = "d_free")]
    pub min_free_disk_gb: u32,
    #[serde(default = "d_true")]
    pub enable_ransomware_protection: bool,
    /// 저장소 암호화(켠 뒤 만들어지는 블롭부터 AES-256-GCM). 키는 설정 화면에서 켤 때 저장소별로 만든다
    #[serde(default)]
    pub encrypt_backups: bool,
    /// 백업 때 원본 드라이브를 VSS 로 스냅샷해 잠긴 파일도 읽는다(못 만들면 직접 읽기로 되돌아감)
    #[serde(default = "d_true")]
    pub use_vss: bool,
    #[serde(default)]
    pub offsite_enabled: bool,
    #[serde(default = "d_prov")]
    pub offsite_provider: String,
    #[serde(default)]
    pub offsite_remote_path: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}
fn d_port() -> u16 {
    8765
}
fn d_host() -> String {
    "0.0.0.0".into()
}
fn d_true() -> bool {
    true
}
fn d_log() -> String {
    "INFO".into()
}
fn d_free() -> u32 {
    10
}
fn d_prov() -> String {
    "local".into()
}

impl Default for Settings {
    fn default() -> Self {
        serde_json::from_str("{}").unwrap()
    }
}

/// 임시 파일에 쓴 뒤 rename 하는 원자적 저장.
fn atomic_write(path: &Path, text: &str) -> io::Result<()> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, text)?;
    fs::rename(&tmp, path)
}

/// 읽기. 파일이 없으면 None, 파싱 실패면 손상 파일을 .bak 으로 보존하고 None.
fn read_json<T: for<'a> Deserialize<'a>>(path: &Path) -> Option<T> {
    let raw = fs::read(path).ok()?;
    let raw = raw.strip_prefix(&[0xEF, 0xBB, 0xBF][..]).unwrap_or(&raw);
    match serde_json::from_slice(raw) {
        Ok(v) => Some(v),
        Err(_) => {
            let mut bak: PathBuf = path.into();
            bak.set_extension("json.bak");
            let _ = fs::copy(path, bak);
            None
        }
    }
}

pub struct ConfigStore {
    pub dir: PathBuf,
}

impl ConfigStore {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }
    fn profiles_path(&self) -> PathBuf {
        self.dir.join("profiles.json")
    }
    fn settings_path(&self) -> PathBuf {
        self.dir.join("app_settings.json")
    }

    pub fn settings(&self) -> Settings {
        read_json(&self.settings_path()).unwrap_or_default()
    }
    pub fn save_settings(&self, s: &Settings) -> io::Result<()> {
        atomic_write(&self.settings_path(), &serde_json::to_string_pretty(s)?)
    }
    pub fn profiles(&self) -> Vec<Profile> {
        read_json(&self.profiles_path()).unwrap_or_default()
    }
    pub fn save_profiles(&self, p: &[Profile]) -> io::Result<()> {
        atomic_write(&self.profiles_path(), &serde_json::to_string_pretty(p)?)
    }
    pub fn get(&self, id: &str) -> Option<Profile> {
        self.profiles().into_iter().find(|p| p.id == id)
    }
    /// 같은 id 가 있으면 교체, 없으면 추가.
    pub fn upsert(&self, prof: Profile) -> io::Result<()> {
        let mut all = self.profiles();
        match all.iter_mut().find(|p| p.id == prof.id) {
            Some(slot) => *slot = prof,
            None => all.push(prof),
        }
        self.save_profiles(&all)
    }
    pub fn delete(&self, id: &str) -> io::Result<bool> {
        let mut all = self.profiles();
        let n = all.len();
        all.retain(|p| p.id != id);
        let removed = all.len() != n;
        if removed {
            self.save_profiles(&all)?;
        }
        Ok(removed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_preserves_unknown_fields_and_numeric_schedule() {
        let d = tempfile::tempdir().unwrap();
        let st = ConfigStore::new(d.path());
        fs::write(
            st.profiles_path(),
            "\u{feff}[{\"id\":\"a\",\"name\":\"한글\",\"schedule_value\":12,\"future_field\":{\"x\":1}}]",
        ).unwrap();
        let p = st.profiles();
        assert_eq!(p[0].schedule_value, "12");
        assert_eq!(p[0].retention_count, 30);
        st.upsert(p[0].clone()).unwrap();
        let txt = fs::read_to_string(st.profiles_path()).unwrap();
        assert!(txt.contains("future_field") && txt.contains("한글"));
    }

    #[test]
    fn upsert_delete_and_corrupt_backup() {
        let d = tempfile::tempdir().unwrap();
        let st = ConfigStore::new(d.path());
        st.upsert(Profile::new("a", "A")).unwrap();
        st.upsert(Profile::new("b", "B")).unwrap();
        let mut a = Profile::new("a", "A2");
        a.retention_count = 5;
        st.upsert(a).unwrap();
        assert_eq!(st.profiles().len(), 2);
        assert_eq!(st.get("a").unwrap().retention_count, 5);
        assert!(st.delete("b").unwrap() && !st.delete("b").unwrap());
        fs::write(st.settings_path(), "{broken").unwrap();
        assert_eq!(st.settings(), Settings::default());
        assert!(d.path().join("app_settings.json.bak").exists());
    }

    #[test]
    fn defaults_and_settings_roundtrip() {
        let d = tempfile::tempdir().unwrap();
        let st = ConfigStore::new(d.path());
        let s = st.settings();
        assert_eq!(s.server_port, 8765);
        st.save_settings(&s).unwrap();
        assert_eq!(st.settings(), s);
    }
}
