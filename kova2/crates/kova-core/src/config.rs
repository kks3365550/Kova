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
    /// 백업 뒤 오래된 낱개 블롭을 팩 컨테이너로 묶는다(파일 수 감소). 팩을 모르는 옛 버전은 읽지 못한다
    #[serde(default)]
    pub pack_blobs: bool,
    #[serde(default)]
    pub offsite_enabled: bool,
    #[serde(default = "d_prov")]
    pub offsite_provider: String,
    #[serde(default)]
    pub offsite_remote_path: String,
    /// 정기 무결성 검증(Deep Scrub) 주기(일). 0이면 자동 검증 안 함.
    #[serde(default = "d_maint_days")]
    pub auto_verify_days: u32,
    /// 정기 고아 블롭 정리(GC) 주기(일). 0이면 자동 정리 안 함.
    #[serde(default = "d_maint_days")]
    pub auto_gc_days: u32,
    /// 저장소 검증 시 청크 존재 여부뿐 아니라 실제 복호화·해시 일치까지 검사(Deep Scrub).
    #[serde(default = "d_true")]
    pub verify_deep: bool,
    /// 원격 실패·경고 알림 웹훅 URL (비어 있으면 비활성).
    #[serde(default)]
    pub webhook_url: String,
    /// 백업 성공 시에도 웹훅 알림 발송 여부 (기본 false: 실패·경고만 발송).
    #[serde(default)]
    pub notify_on_success: bool,
    /// 백업 엔진 최대 CPU 스레드 수 (0이면 자동/무제한).
    #[serde(default)]
    pub max_cpu_threads: u32,
    /// 오프사이트 복제 대역폭 상한(MB/s). 0이면 무제한.
    #[serde(default)]
    pub offsite_bw_limit_mbps: u32,
    /// 노트북 배터리 구동 시 자동 스케줄 백업 일시정지.
    #[serde(default)]
    pub pause_on_battery: bool,
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
fn d_maint_days() -> u32 {
    7
}

impl Default for Settings {
    fn default() -> Self {
        serde_json::from_str("{}").unwrap()
    }
}

/// 서비스(SYSTEM)와 일반 사용자 앱이 함께 쓰는 설정 파일에 Users 수정 권한을 준다(비밀 파일에는 쓰지 않는다).
/// 서비스가 새로 만든 파일은 폴더 상속(Users 읽기만)을 받아 앱이 저장하지 못하게 되기 때문이다.
/// 프로세스마다 경로별로 한 번만 시도한다(이후 저장은 ACL 을 보존하는 교체로 유지된다).
pub(crate) fn grant_users_modify_once(path: &Path) {
    use std::collections::HashSet;
    use std::sync::Mutex;
    static DONE: Mutex<Option<HashSet<PathBuf>>> = Mutex::new(None);
    let mut g = DONE.lock().unwrap_or_else(|e| e.into_inner());
    if !g
        .get_or_insert_with(HashSet::new)
        .insert(path.to_path_buf())
    {
        return;
    }
    drop(g);
    if cfg!(windows) {
        let _ = crate::worm::icacls(path, &["/grant", "*S-1-5-32-545:(M)", "/q"]);
    }
}

/// `replacement` 로 `target` 을 바꾼다. 기존 파일이 있으면 ReplaceFileW 로 기존 파일의 ACL·속성을 유지하고,
/// 실패하거나 없으면 rename 한다.
fn replace_preserving_acl(replacement: &Path, target: &Path) -> io::Result<()> {
    #[cfg(windows)]
    if target.exists() {
        use std::os::windows::ffi::OsStrExt;
        extern "system" {
            fn ReplaceFileW(
                replaced: *const u16,
                replacement: *const u16,
                backup: *const u16,
                flags: u32,
                exclude: *mut std::ffi::c_void,
                reserved: *mut std::ffi::c_void,
            ) -> i32;
        }
        const REPLACEFILE_IGNORE_MERGE_ERRORS: u32 = 0x2;
        let w = |p: &Path| -> Vec<u16> { p.as_os_str().encode_wide().chain([0]).collect() };
        let (t, r) = (w(target), w(replacement));
        let ok = unsafe {
            ReplaceFileW(
                t.as_ptr(),
                r.as_ptr(),
                std::ptr::null(),
                REPLACEFILE_IGNORE_MERGE_ERRORS,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        if ok != 0 {
            return Ok(());
        }
    }
    fs::rename(replacement, target)
}

/// 공유 설정 파일의 원자적 저장: 고유한 임시 이름에 쓰고 디스크에 내려쓴 뒤 교체한다(동시 저장끼리 임시 파일을 밟지 않게).
pub(crate) fn atomic_write_shared(path: &Path, bytes: &[u8]) -> io::Result<()> {
    use std::io::Write;
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let tmp = path.with_file_name(format!(
        "{name}.{}.{}.tmp",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let r = (|| {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        drop(f);
        replace_preserving_acl(&tmp, path)
    })();
    if r.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    r?;
    grant_users_modify_once(path);
    Ok(())
}

/// 프로세스 안팎(서비스·앱·여러 스레드) 읽기-수정-쓰기를 직렬화하는 파일 잠금. 놓으면(drop) 풀린다.
/// 잠금 파일을 열 수 없으면(권한 등) None — 이 경우 저장 자체도 대개 실패하므로 잠금 없이 진행한다.
pub(crate) fn lock_shared(lock_path: &Path) -> Option<fs::File> {
    if let Some(p) = lock_path.parent() {
        let _ = fs::create_dir_all(p);
    }
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .or_else(|_| fs::File::open(lock_path))
        .ok()?;
    grant_users_modify_once(lock_path);
    f.lock().ok()?;
    Some(f)
}

/// 손상 파일을 `.json.bak` 으로 보존한다. 이미 다른 내용의 .bak 이 있으면 시각을 붙인 이름으로 남긴다.
fn preserve_corrupt(path: &Path, raw: &[u8]) -> PathBuf {
    let mut bak: PathBuf = path.into();
    bak.set_extension("json.bak");
    if fs::read(&bak).is_ok_and(|b| b != raw) {
        let t = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        bak.set_extension(format!("json.bak.{t}"));
    }
    let _ = fs::write(&bak, raw);
    bak
}

/// 읽기. 파일이 없으면 Ok(None). 읽기 실패(권한·잠김 등)는 Err, 파싱 실패는 손상 파일을 .bak 으로 보존하고 Err.
pub(crate) fn try_read_json<T: for<'a> Deserialize<'a>>(path: &Path) -> io::Result<Option<T>> {
    let raw = match fs::read(path) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io::Error::new(e.kind(), format!("{}: {e}", path.display()))),
    };
    let body = raw.strip_prefix(&[0xEF, 0xBB, 0xBF][..]).unwrap_or(&raw);
    match serde_json::from_slice(body) {
        Ok(v) => Ok(Some(v)),
        Err(e) => {
            let bak = preserve_corrupt(path, &raw);
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "{} 손상({e}). 덮어쓰지 않고 거부합니다(사본: {})",
                    path.display(),
                    bak.display()
                ),
            ))
        }
    }
}

/// 읽기(표시용). 없거나 읽지 못하면 None(손상 파일은 .bak 으로 보존).
fn read_json<T: for<'a> Deserialize<'a>>(path: &Path) -> Option<T> {
    try_read_json(path).ok().flatten()
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

    fn lock(&self) -> Option<fs::File> {
        lock_shared(&self.dir.join(".config.lock"))
    }

    pub fn settings(&self) -> Settings {
        read_json(&self.settings_path()).unwrap_or_default()
    }
    /// 설정 읽기. 파일이 없으면 기본값, 읽기 실패·손상이면 오류.
    pub fn try_settings(&self) -> io::Result<Settings> {
        Ok(try_read_json(&self.settings_path())?.unwrap_or_default())
    }
    /// 저장. 기존 파일을 읽지 못하거나 손상돼 있으면 덮어쓰지 않고 거부한다(손상 파일은 .bak 으로 보존).
    pub fn save_settings(&self, s: &Settings) -> io::Result<()> {
        let _g = self.lock();
        try_read_json::<Settings>(&self.settings_path())?;
        atomic_write_shared(
            &self.settings_path(),
            serde_json::to_string_pretty(s)?.as_bytes(),
        )
    }
    pub fn profiles(&self) -> Vec<Profile> {
        read_json(&self.profiles_path()).unwrap_or_default()
    }
    /// 프로파일 읽기. 파일이 없으면 빈 목록, 읽기 실패·손상이면 오류(빈 목록으로 착각해 덮어쓰지 않게).
    pub fn try_profiles(&self) -> io::Result<Vec<Profile>> {
        Ok(try_read_json(&self.profiles_path())?.unwrap_or_default())
    }
    fn write_profiles(&self, p: &[Profile]) -> io::Result<()> {
        atomic_write_shared(
            &self.profiles_path(),
            serde_json::to_string_pretty(p)?.as_bytes(),
        )
    }
    /// 저장. 기존 파일을 읽지 못하거나 손상돼 있으면 덮어쓰지 않고 거부한다.
    pub fn save_profiles(&self, p: &[Profile]) -> io::Result<()> {
        let _g = self.lock();
        self.try_profiles()?;
        self.write_profiles(p)
    }
    pub fn get(&self, id: &str) -> Option<Profile> {
        self.profiles().into_iter().find(|p| p.id == id)
    }
    /// 같은 id 가 있으면 교체, 없으면 추가. 읽기-수정-쓰기를 파일 잠금으로 직렬화한다.
    pub fn upsert(&self, prof: Profile) -> io::Result<()> {
        let _g = self.lock();
        let mut all = self.try_profiles()?;
        match all.iter_mut().find(|p| p.id == prof.id) {
            Some(slot) => *slot = prof,
            None => all.push(prof),
        }
        self.write_profiles(&all)
    }
    pub fn delete(&self, id: &str) -> io::Result<bool> {
        let _g = self.lock();
        let mut all = self.try_profiles()?;
        let n = all.len();
        all.retain(|p| p.id != id);
        let removed = all.len() != n;
        if removed {
            self.write_profiles(&all)?;
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
    fn corrupt_or_unreadable_profiles_refuse_to_save_and_keep_the_file() {
        let d = tempfile::tempdir().unwrap();
        let st = ConfigStore::new(d.path());
        st.upsert(Profile::new("a", "A")).unwrap();
        // 손상: 저장을 거부하고 원본·사본을 남긴다
        fs::write(st.profiles_path(), b"[{\"id\":\"a\",broken").unwrap();
        assert!(st.try_profiles().is_err());
        assert!(st.profiles().is_empty(), "표시용 읽기는 빈 목록");
        let e = st.upsert(Profile::new("b", "B")).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::InvalidData);
        assert!(st.delete("a").is_err());
        assert!(st.save_profiles(&[]).is_err());
        assert_eq!(
            fs::read(st.profiles_path()).unwrap(),
            b"[{\"id\":\"a\",broken"
        );
        assert!(d.path().join("profiles.json.bak").exists());
        // 읽기 실패(파일 자리에 폴더)는 '없음'과 구분돼 오류다
        fs::remove_file(st.profiles_path()).unwrap();
        fs::create_dir(st.profiles_path()).unwrap();
        assert!(st.try_profiles().is_err());
        assert!(st.upsert(Profile::new("c", "C")).is_err());
        // 파일이 아예 없으면 빈 목록에서 시작한다
        fs::remove_dir(st.profiles_path()).unwrap();
        assert!(st.try_profiles().unwrap().is_empty());
        st.upsert(Profile::new("c", "C")).unwrap();
        // 손상된 설정도 저장 거부
        fs::write(st.settings_path(), "{broken").unwrap();
        assert!(st.save_settings(&Settings::default()).is_err());
        assert_eq!(fs::read(st.settings_path()).unwrap(), b"{broken");
    }

    #[test]
    fn concurrent_upserts_from_threads_keep_every_profile() {
        let d = tempfile::tempdir().unwrap();
        let dir = d.path().to_path_buf();
        let hs: Vec<_> = (0..8)
            .map(|t| {
                let dir = dir.clone();
                std::thread::spawn(move || {
                    let st = ConfigStore::new(&dir);
                    for i in 0..5 {
                        st.upsert(Profile::new(&format!("p{t}_{i}"), "x")).unwrap();
                    }
                })
            })
            .collect();
        for h in hs {
            h.join().unwrap();
        }
        assert_eq!(ConfigStore::new(&dir).try_profiles().unwrap().len(), 40);
        assert!(!fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .any(|e| e.file_name().to_string_lossy().ends_with(".tmp")));
    }

    #[test]
    #[cfg(windows)]
    fn rewritten_config_keeps_users_modify_permission() {
        use std::os::windows::process::CommandExt;
        let d = tempfile::tempdir().unwrap();
        let st = ConfigStore::new(d.path());
        st.upsert(Profile::new("a", "A")).unwrap();
        // 두 번째 저장(교체)에서도 Users 수정 권한이 유지돼야 한다
        st.upsert(Profile::new("b", "B")).unwrap();
        let o = std::process::Command::new("icacls")
            .arg(st.profiles_path())
            .creation_flags(0x0800_0000)
            .output()
            .unwrap();
        let out = String::from_utf8_lossy(&o.stdout).to_string();
        assert!(
            out.lines()
                .any(|l| l.contains("Users") && l.contains("(M)")),
            "{out}"
        );
        assert_eq!(st.try_profiles().unwrap().len(), 2);
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
