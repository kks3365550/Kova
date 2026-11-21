use serde::{Deserialize, Deserializer, Serialize};
use std::fs::{self, File};
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};

fn deserialize_mtime<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let v = serde_json::Value::deserialize(deserializer)?;
    match v {
        serde_json::Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                if u >= 100_000_000_000 {
                    Ok(u)
                } else {
                    Ok(u * 1000)
                }
            } else if let Some(f) = n.as_f64() {
                if f >= 100_000_000_000.0 {
                    Ok(f.round() as u64)
                } else {
                    Ok((f * 1000.0).round() as u64)
                }
            } else {
                Ok(0)
            }
        }
        _ => Ok(0),
    }
}

/// 블롭 해시 문자열이 16진수인지(경로 조각·`&hash[0..2]` 로 써도 안전한지) 확인한다.
pub fn is_valid_hash(hash: &str) -> bool {
    (2..=128).contains(&hash.len()) && hash.bytes().all(|b| b.is_ascii_hexdigit())
}

/// 세션(스냅샷) ID 는 매니페스트 파일 이름이 되므로 허용 문자(글자·숫자·`_`·`-`·`.`)만 쓴다.
/// `.` 으로 시작하는 이름(`.`, `..` 포함)과 경로 구분자·`:` 등은 거부한다.
pub fn valid_session_id(sid: &str) -> bool {
    !sid.is_empty()
        && sid.len() <= 128
        && !sid.starts_with('.')
        && sid
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

/// 매니페스트의 원본 경로 목록(sources 배열, 없으면 source_root 를 `;` 로 나눈 것).
pub fn manifest_sources(m: &SnapshotManifest) -> Vec<String> {
    match &m.sources {
        Some(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect(),
        Some(serde_json::Value::String(s)) => s
            .split(';')
            .filter(|x| !x.trim().is_empty())
            .map(|x| x.to_string())
            .collect(),
        _ => m
            .source_root
            .as_deref()
            .unwrap_or("")
            .split(';')
            .filter(|x| !x.trim().is_empty())
            .map(|x| x.to_string())
            .collect(),
    }
}

/// 원본 경로 비교용 정규화(구분자 통일, 끝 구분자 제거, 대소문자 무시).
pub fn norm_source(s: &str) -> String {
    s.trim()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

/// 두 원본 목록이 (순서와 무관하게) 같은 경로 집합인지.
pub fn same_sources(a: &[String], b: &[String]) -> bool {
    let set = |v: &[String]| {
        v.iter()
            .map(|s| norm_source(s))
            .collect::<std::collections::BTreeSet<_>>()
    };
    !a.is_empty() && set(a) == set(b)
}

/// 증분 기준으로 쓸 수 있는 매니페스트인지: 원본 경로 집합이 같아야 하고,
/// 프로파일이 있으면 같은 프로파일이거나 프로파일 기록이 없는 구형(업그레이드 전) 매니페스트여야 한다.
pub fn is_baseline_for(m: &SnapshotManifest, profile_id: Option<&str>, sources: &[String]) -> bool {
    let pid_ok = match (
        profile_id.filter(|p| !p.is_empty()),
        m.profile_id.as_deref(),
    ) {
        (Some(want), Some(have)) => want == have,
        _ => true,
    };
    pid_ok && same_sources(sources, &manifest_sources(m))
}

/// 매니페스트 파일 목록(최신 수정 시각 순).
fn manifest_paths_newest_first(snapshots_dir: &Path) -> Vec<PathBuf> {
    let mut entries: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
    if let Ok(dir_entries) = fs::read_dir(snapshots_dir) {
        for entry in dir_entries.flatten() {
            let path = entry.path();
            if path.is_file() && path.extension().is_some_and(|ext| ext == "json") {
                let mtime = entry
                    .metadata()
                    .and_then(|m| m.modified())
                    .unwrap_or(std::time::UNIX_EPOCH);
                entries.push((mtime, path));
            }
        }
    }
    entries.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    entries.into_iter().map(|(_, p)| p).collect()
}

/// `snapshots_dir` 에서 `pred` 를 만족하는 가장 최근 매니페스트(파일이 하나 이상 있는 것)를 찾는다.
/// `exclude` 세션은 건너뛴다.
pub fn find_recent_manifest(
    snapshots_dir: &Path,
    exclude: Option<&str>,
    pred: &(dyn Fn(&SnapshotManifest) -> bool + Sync),
) -> Option<SnapshotManifest> {
    use rayon::prelude::*;
    let paths: Vec<PathBuf> = manifest_paths_newest_first(snapshots_dir)
        .into_iter()
        .filter(|p| exclude.is_none_or(|x| p.file_stem().is_none_or(|s| s.to_string_lossy() != x)))
        .collect();
    for batch in paths.chunks(16) {
        let parsed: Vec<Option<SnapshotManifest>> = batch
            .par_iter()
            .map(|path| {
                let m: SnapshotManifest =
                    serde_json::from_reader(BufReader::new(File::open(path).ok()?)).ok()?;
                let count = m.files.as_ref().map_or(0, |f| f.len())
                    + m.entries.as_ref().map_or(0, |e| e.len());
                (count > 0 && pred(&m)).then_some(m)
            })
            .collect();
        if let Some(m) = parsed.into_iter().flatten().next() {
            return Some(m);
        }
    }
    None
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct FileChunkInfo {
    #[serde(default)]
    pub hash: String,
    #[serde(default)]
    pub offset: u64,
    #[serde(default)]
    pub length: usize,
    #[serde(default)]
    pub compressed_length: usize,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct FileEntry {
    #[serde(alias = "rel_path", default)]
    pub path: String, // Relative path from source root
    #[serde(default)]
    pub size: u64,
    #[serde(alias = "mtime", deserialize_with = "deserialize_mtime", default)]
    pub modified: u64, // Unix timestamp (ms or sec)
    #[serde(default)]
    pub is_dir: bool,
    #[serde(default)]
    pub chunks: Vec<FileChunkInfo>,
    #[serde(default)]
    pub blob_id: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
    /// 원본 경로 번호(매니페스트 sources 배열의 인덱스). 원위치 복원용. 구형 스냅샷에는 없다.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub src: Option<u32>,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct SnapshotManifest {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
    /// 이 스냅샷을 만든 프로파일(보존 정책을 프로파일별로 적용하기 위함). 구형 스냅샷에는 없다.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub profile_id: Option<String>,
    #[serde(default)]
    pub created_at: Option<serde_json::Value>,
    #[serde(default)]
    pub source_root: Option<String>,
    #[serde(default)]
    pub sources: Option<serde_json::Value>,
    /// 각 원본 경로의 종류("file"/"dir"). sources 배열과 같은 순서.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_kinds: Option<Vec<String>>,
    /// 2 = 파일별 원본 번호(src)를 기록하는 형식(원위치 복원 지원)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub format_version: Option<u32>,
    #[serde(default)]
    pub total_files: usize,
    #[serde(default)]
    pub total_bytes: u64,
    #[serde(default)]
    pub dedup_bytes: u64,
    #[serde(default)]
    pub compressed_bytes: u64,
    #[serde(default)]
    pub files: Option<Vec<FileEntry>>,
    #[serde(default)]
    pub entries: Option<Vec<FileEntry>>,
}

impl SnapshotManifest {
    pub fn session_id(&self) -> String {
        self.session_id
            .clone()
            .or_else(|| self.id.clone())
            .unwrap_or_default()
    }

    pub fn source_root(&self) -> String {
        if let Some(ref s) = self.source_root {
            return s.clone();
        }

        match &self.sources {
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(serde_json::Value::Array(arr)) => {
                let strs: Vec<String> = arr
                    .iter()
                    .filter_map(|x| match x {
                        serde_json::Value::String(s) => Some(s.clone()),
                        _ => None,
                    })
                    .collect();
                strs.join(";")
            }
            Some(v) => v.to_string(),
            None => String::new(),
        }
    }

    pub fn files(self) -> Vec<FileEntry> {
        self.files.or(self.entries).unwrap_or_default()
    }
}

pub struct CasStorage {
    repo_root: PathBuf,
    codec: Option<std::sync::Arc<dyn crate::codec::BlobCodec>>,
    /// 팩 컨테이너 색인(처음 쓸 때 한 번 읽는다)
    packs: std::sync::OnceLock<std::collections::HashMap<String, crate::pack::PackEntry>>,
    /// 백업 중 새로 만든 블롭을 팩으로 쓰기 전까지 모아 두는 곳
    stage: std::sync::Mutex<Stage>,
    /// 이번 실행에서 이미 모아 둔(또는 팩으로 쓴) 블롭 해시 — 같은 내용의 중복 저장을 막는다
    staged: std::sync::RwLock<std::collections::HashSet<String>>,
    /// 팩 쓰기가 한 번이라도 실패했는지(실패하면 `flush_staged` 가 오류를 돌려준다)
    stage_err: std::sync::Mutex<Option<String>>,
    /// 복원 중 같은 팩을 청크마다 다시 열지 않도록 열어 둔 팩 파일 핸들
    open_packs: std::sync::RwLock<std::collections::HashMap<String, std::sync::Arc<File>>>,
}

/// 열어 둘 팩 핸들 수의 상한(넘으면 비우고 다시 연다)
const OPEN_PACKS_MAX: usize = 256;

/// 모은 블롭이 이만큼 쌓이면 팩 파일 하나로 쓴다
const STAGE_LIMIT: usize = 64 * 1024 * 1024;

#[derive(Default)]
struct Stage {
    items: Vec<(String, Vec<u8>)>,
    bytes: usize,
}

impl CasStorage {
    pub fn new<P: AsRef<Path>>(repo_root: P) -> io::Result<Self> {
        let root = repo_root.as_ref().to_path_buf();
        fs::create_dir_all(root.join("blobs"))?;
        fs::create_dir_all(root.join("snapshots"))?;
        let codec = crate::codec::resolve(&root).map_err(io::Error::other)?;
        Ok(Self {
            repo_root: root,
            codec,
            packs: std::sync::OnceLock::new(),
            stage: Default::default(),
            staged: Default::default(),
            stage_err: Default::default(),
            open_packs: Default::default(),
        })
    }

    fn pack_index(&self) -> &std::collections::HashMap<String, crate::pack::PackEntry> {
        self.packs
            .get_or_init(|| crate::pack::load_index(&self.repo_root))
    }

    /// 평문 청크를 저장 형식으로(암호화 저장소면 암호화, 아니면 zstd).
    pub fn encode_blob(&self, plain: &[u8], hash: &str, level: i32) -> Result<Vec<u8>, String> {
        match &self.codec {
            Some(c) => c.encode(plain, hash, level),
            None => crate::compressor::compress(plain, level).map_err(|e| e.to_string()),
        }
    }

    /// 저장 형식 → 평문. 호출자가 해시를 다시 검증한다.
    pub fn decode_blob(&self, blob: &[u8], hash: &str) -> Result<Vec<u8>, String> {
        match &self.codec {
            Some(c) => c.decode(blob, hash),
            None => crate::compressor::decompress(blob).map_err(|e| e.to_string()),
        }
    }

    pub fn blob_path(&self, hash: &str) -> PathBuf {
        // 16진수가 아닌 해시(손상·변조된 매니페스트)는 슬라이싱 패닉이나 경로 탈출을 막기 위해
        // 존재하지 않는 고정 자리를 가리키게 한다(읽으면 "없음", 저장은 save_blob 이 거부)
        if !is_valid_hash(hash) {
            return self
                .repo_root
                .join("blobs")
                .join("misc")
                .join("invalid.blob");
        }
        self.repo_root
            .join("blobs")
            .join(&hash[0..2])
            .join(format!("{}.blob", hash))
    }

    pub fn has_blob(&self, hash: &str) -> bool {
        // 메모리 조회를 먼저 하고, 파일 시스템 조회는 마지막에 한다
        self.staged
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .contains(hash)
            || self.pack_index().contains_key(hash)
            || self.blob_path(hash).exists()
    }

    /// 새 블롭을 모아 두었다가 `STAGE_LIMIT` 이 차면 팩 하나로 쓴다(블롭마다 파일을 만들지 않아 빠르다).
    /// 반드시 백업 끝에서 `flush_staged` 를 불러야 하고, 그 전에는 매니페스트를 쓰면 안 된다.
    pub fn stage_blob(&self, hash: &str, data: Vec<u8>) -> io::Result<()> {
        if !is_valid_hash(hash) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid blob hash",
            ));
        }
        let fresh = self
            .staged
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(hash.to_string());
        if !fresh {
            return Ok(());
        }
        let batch = {
            let mut st = self.stage.lock().unwrap_or_else(|e| e.into_inner());
            st.bytes += data.len();
            st.items.push((hash.to_string(), data));
            if st.bytes >= STAGE_LIMIT {
                st.bytes = 0;
                Some(std::mem::take(&mut st.items))
            } else {
                None
            }
        };
        match batch {
            Some(items) => self.write_stage_batch(&items),
            None => Ok(()),
        }
    }

    fn write_stage_batch(&self, items: &[(String, Vec<u8>)]) -> io::Result<()> {
        crate::pack::write_pack(&self.repo_root, items)
            .map(|_| ())
            .inspect_err(|e| {
                *self.stage_err.lock().unwrap_or_else(|x| x.into_inner()) = Some(e.to_string());
            })
    }

    /// 모아 둔 블롭을 모두 팩으로 쓴다. 이번 실행 중 팩 쓰기가 한 번이라도 실패했다면 오류를 돌려준다.
    pub fn flush_staged(&self) -> io::Result<()> {
        let items = {
            let mut st = self.stage.lock().unwrap_or_else(|e| e.into_inner());
            st.bytes = 0;
            std::mem::take(&mut st.items)
        };
        if !items.is_empty() {
            let _ = self.write_stage_batch(&items);
        }
        match self
            .stage_err
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        {
            Some(msg) => Err(io::Error::other(format!("팩 쓰기 실패: {msg}"))),
            None => Ok(()),
        }
    }

    /// Loads all existing blob hashes into an in-memory HashSet using parallel directory scan.
    /// This eliminates hundreds of thousands of GetFileAttributesW disk syscalls during incremental checks.
    pub fn get_all_blob_hashes(&self) -> std::collections::HashSet<String> {
        use rayon::prelude::*;

        let blobs_dir = self.repo_root.join("blobs");
        let packed: Vec<String> = self.pack_index().keys().cloned().collect();
        if !blobs_dir.is_dir() {
            return packed.into_iter().collect();
        }

        let prefix_dirs: Vec<PathBuf> = match fs::read_dir(&blobs_dir) {
            Ok(rd) => rd
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| p.is_dir())
                .collect(),
            Err(_) => Vec::new(),
        };

        let hashes: Vec<String> = prefix_dirs
            .par_iter()
            .flat_map(|dir| {
                let mut dir_hashes = Vec::new();
                if let Ok(rd) = fs::read_dir(dir) {
                    for entry in rd.flatten() {
                        let p = entry.path();
                        if p.extension().is_some_and(|ext| ext == "blob") {
                            if let Some(stem) = p.file_stem() {
                                dir_hashes.push(stem.to_string_lossy().to_string());
                            }
                        }
                    }
                }
                dir_hashes
            })
            .collect();

        hashes.into_iter().chain(packed).collect()
    }

    /// Stores compressed blob data atomically. Returns true if newly written, false if deduplicated.
    pub fn save_blob(&self, hash: &str, compressed_data: &[u8]) -> io::Result<bool> {
        if !is_valid_hash(hash) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid blob hash",
            ));
        }
        let target_path = self.blob_path(hash);
        if target_path.exists() || self.pack_index().contains_key(hash) {
            return Ok(false);
        }

        if let Some(parent) = target_path.parent() {
            fs::create_dir_all(parent)?;
        }

        // 같은 내용(같은 해시)을 여러 스레드가 동시에 저장할 수 있으므로 임시 파일 이름은 호출마다 다르게 한다
        static TMP_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let tmp_path = target_path.with_extension(format!(
            "{}.{}.tmp",
            std::process::id(),
            TMP_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        {
            let mut file = File::create(&tmp_path)?;
            file.write_all(compressed_data)?;
            file.flush()?;
        }

        if let Err(e) = fs::rename(&tmp_path, &target_path) {
            // 다른 스레드가 먼저 같은 블롭을 놓았다면(내용이 같으므로) 그걸로 충분하다
            if target_path.exists() {
                let _ = fs::remove_file(&tmp_path);
                return Ok(false);
            }
            let _ = fs::remove_file(&tmp_path);
            return Err(e);
        }
        Ok(true)
    }

    /// 팩 안의 블롭을 읽는다(핸들 재사용). 핸들 읽기가 실패하면 새로 열어 한 번 더 읽는다.
    fn read_packed(&self, entry: &crate::pack::PackEntry) -> io::Result<Vec<u8>> {
        let cached = self
            .open_packs
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(&entry.pack)
            .cloned();
        let handle = match cached {
            Some(h) => h,
            None => {
                let h = std::sync::Arc::new(File::open(crate::pack::pack_file(
                    &self.repo_root,
                    &entry.pack,
                ))?);
                let mut m = self.open_packs.write().unwrap_or_else(|e| e.into_inner());
                if m.len() >= OPEN_PACKS_MAX {
                    m.clear();
                }
                m.insert(entry.pack.clone(), h.clone());
                h
            }
        };
        let mut buf = vec![0u8; entry.len as usize];
        match crate::pack::read_at(&handle, &mut buf, entry.offset) {
            Ok(()) => Ok(buf),
            Err(_) => {
                self.open_packs
                    .write()
                    .unwrap_or_else(|e| e.into_inner())
                    .remove(&entry.pack);
                crate::pack::read_entry(&self.repo_root, entry)
            }
        }
    }

    pub fn read_blob(&self, hash: &str) -> io::Result<Vec<u8>> {
        // 팩에 있는 블롭은 낱개 파일이 없으므로, 없는 파일을 먼저 열어 보는 헛걸음을 하지 않는다
        if let Some(entry) = self.pack_index().get(hash) {
            return self.read_packed(entry);
        }
        let target_path = self.blob_path(hash);
        match File::open(target_path) {
            Ok(mut file) => {
                let mut data = Vec::new();
                file.read_to_end(&mut data)?;
                Ok(data)
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                // 캐시된 색인 이후에 묶인 블롭일 수 있으니, 없으면 색인을 새로 읽어 한 번 더 찾는다
                let hit = self
                    .pack_index()
                    .get(hash)
                    .cloned()
                    .or_else(|| crate::pack::load_index(&self.repo_root).remove(hash));
                match hit {
                    Some(entry) => self.read_packed(&entry),
                    None => Err(e),
                }
            }
            Err(e) => Err(e),
        }
    }

    pub fn save_manifest(&self, manifest: &SnapshotManifest) -> io::Result<()> {
        let snapshots_dir = self.repo_root.join("snapshots");
        fs::create_dir_all(&snapshots_dir)?;
        let sid = manifest.session_id();
        if !valid_session_id(&sid) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("invalid session id: {sid}"),
            ));
        }
        let target_path = snapshots_dir.join(format!("{sid}.json"));
        static TMP_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let tmp_path = snapshots_dir.join(format!(
            "{sid}.json.{}.{}.tmp",
            std::process::id(),
            TMP_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));

        let json = serde_json::to_string(manifest)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        // 내용을 디스크까지 내린 뒤에 이름을 놓는다(정전 시 빈/잘린 매니페스트 방지)
        let written = File::create(&tmp_path).and_then(|mut f| {
            f.write_all(json.as_bytes())?;
            f.sync_all()
        });
        if let Err(e) = written {
            let _ = fs::remove_file(&tmp_path);
            return Err(e);
        }
        let exists = || {
            io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!("snapshot id already exists: {sid}"),
            )
        };
        // 같은 ID 의 기존 스냅샷은 절대 덮어쓰지 않는다. 하드 링크는 대상이 있으면 원자적으로 실패한다.
        let placed = match fs::hard_link(&tmp_path, &target_path) {
            Ok(()) => Ok(()),
            Err(_) if target_path.exists() => Err(exists()),
            // 하드 링크를 못 쓰는 파일 시스템(FAT 등): 확인 후 이름 바꾸기
            Err(_) => fs::rename(&tmp_path, &target_path),
        };
        let _ = fs::remove_file(&tmp_path);
        placed
    }

    pub fn load_manifest(&self, session_id: &str) -> io::Result<SnapshotManifest> {
        let target_path = self
            .repo_root
            .join("snapshots")
            .join(format!("{}.json", session_id));
        let file = File::open(target_path)?;
        let reader = BufReader::new(file);
        let manifest: SnapshotManifest = serde_json::from_reader(reader)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        Ok(manifest)
    }

    /// 증분 기준 스냅샷: 원본 경로 집합이 같고 같은 프로파일(또는 프로파일 기록이 없는 구형)인 가장 최근 매니페스트.
    pub fn baseline_manifest(
        &self,
        profile_id: Option<&str>,
        sources: &[String],
    ) -> Option<SnapshotManifest> {
        find_recent_manifest(&self.repo_root.join("snapshots"), None, &|m| {
            is_baseline_for(m, profile_id, sources)
        })
    }

    pub fn get_recent_manifests(&self, limit: usize) -> Vec<SnapshotManifest> {
        self.get_recent_manifests_for_profile(limit, None)
    }

    pub fn get_recent_manifests_for_profile(
        &self,
        limit: usize,
        profile_id: Option<&str>,
    ) -> Vec<SnapshotManifest> {
        let snapshots_dir = self.repo_root.join("snapshots");
        if !snapshots_dir.is_dir() || limit == 0 {
            return Vec::new();
        }

        let mut entries: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
        if let Ok(dir_entries) = fs::read_dir(snapshots_dir) {
            for entry in dir_entries.flatten() {
                let path = entry.path();
                if path.is_file() && path.extension().is_some_and(|ext| ext == "json") {
                    let mtime = entry
                        .metadata()
                        .and_then(|m| m.modified())
                        .unwrap_or(std::time::UNIX_EPOCH);
                    entries.push((mtime, path));
                }
            }
        }

        entries.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| b.1.cmp(&a.1)));

        // Parse candidate manifests in parallel batches (newest first, order preserved).
        use rayon::prelude::*;
        let paths: Vec<PathBuf> = entries.into_iter().map(|(_, p)| p).collect();
        let mut manifests: Vec<SnapshotManifest> = Vec::new();
        let batch_size = if profile_id.is_some() {
            limit.max(16)
        } else {
            limit
        };
        for batch in paths.chunks(batch_size) {
            let parsed: Vec<Option<SnapshotManifest>> = batch
                .par_iter()
                .map(|path| match File::open(path) {
                    Ok(file) => {
                        match serde_json::from_reader::<_, SnapshotManifest>(BufReader::new(file)) {
                            Ok(m) => {
                                let count = m.files.as_ref().map_or(0, |f| f.len())
                                    + m.entries.as_ref().map_or(0, |e| e.len());
                                if count > 0 {
                                    if let Some(pid) = profile_id {
                                        if !pid.is_empty() {
                                            if m.profile_id.as_deref() == Some(pid) {
                                                Some(m)
                                            } else {
                                                None
                                            }
                                        } else {
                                            Some(m)
                                        }
                                    } else {
                                        Some(m)
                                    }
                                } else {
                                    None
                                }
                            }
                            Err(e) => {
                                eprintln!(
                                    "[KOVA-FAST-DEBUG] Failed to parse manifest {}: {}",
                                    path.display(),
                                    e
                                );
                                None
                            }
                        }
                    }
                    Err(e) => {
                        eprintln!(
                            "[KOVA-FAST-DEBUG] Failed to open manifest file {}: {}",
                            path.display(),
                            e
                        );
                        None
                    }
                })
                .collect();
            for m in parsed.into_iter().flatten() {
                manifests.push(m);
                if manifests.len() >= limit {
                    break;
                }
            }
            if manifests.len() >= limit {
                break;
            }
        }

        manifests
    }

    pub fn get_latest_manifest(&self) -> Option<SnapshotManifest> {
        self.get_recent_manifests(1).into_iter().next()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 같은 해시를 여러 스레드가 동시에 저장해도 전부 성공하고 블롭은 하나만 남는다.
    #[test]
    fn concurrent_save_of_same_blob_never_fails() {
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        let data = vec![9u8; 4096];
        for round in 0..200 {
            let hash = format!("{round:064x}");
            let wrote: usize = std::thread::scope(|s| {
                let hs: Vec<_> = (0..8)
                    .map(|_| s.spawn(|| cas.save_blob(&hash, &data).unwrap()))
                    .collect();
                hs.into_iter().map(|h| h.join().unwrap() as usize).sum()
            });
            assert!(wrote >= 1, "아무도 저장하지 않음");
            assert_eq!(cas.read_blob(&hash).unwrap(), data);
        }
        // 임시 파일이 남지 않는다
        let leftovers = walkdir_tmp(d.path());
        assert!(leftovers.is_empty(), "{leftovers:?}");
    }

    fn h(n: usize) -> String {
        format!("{n:064x}")
    }

    /// 모아 둔 블롭은 비우기 전에도 있는 것으로 보이고, 비운 뒤에는 팩에서 그대로 읽힌다(낱개 파일은 만들지 않는다).
    #[test]
    fn staged_blobs_are_visible_then_readable_after_flush() {
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        assert!(!cas.has_blob(&h(1)));
        cas.stage_blob(&h(1), vec![1u8; 1000]).unwrap();
        cas.stage_blob(&h(2), vec![2u8; 2000]).unwrap();
        assert!(cas.has_blob(&h(1)) && cas.has_blob(&h(2)));
        cas.flush_staged().unwrap();

        // 새 인스턴스(= 다음 실행)가 색인을 읽어 같은 내용을 돌려준다
        let again = CasStorage::new(d.path()).unwrap();
        assert!(again.has_blob(&h(1)));
        assert_eq!(again.read_blob(&h(1)).unwrap(), vec![1u8; 1000]);
        assert_eq!(again.read_blob(&h(2)).unwrap(), vec![2u8; 2000]);
        assert!(!again.blob_path(&h(1)).exists(), "낱개 파일이 생김");
        // 팩 하나, 임시 파일 없음
        assert_eq!(crate::pack::pack_ids(d.path()).len(), 1);
        assert!(walkdir_tmp(d.path()).is_empty());
    }

    /// 한도(64MB)를 넘으면 중간에 팩이 쓰이고, 나머지는 flush 때 쓰인다. 같은 해시를 여러 번·여러 스레드가 모아도 한 번만 들어간다.
    #[test]
    fn staging_rolls_over_packs_and_dedups_same_hash() {
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        let block = vec![7u8; 8 * 1024 * 1024];
        std::thread::scope(|sc| {
            for t in 0..4 {
                let (cas, block) = (&cas, &block);
                sc.spawn(move || {
                    for i in 0..10 {
                        // 번호 0..10 은 4개 스레드가 모두 같은 해시를 모은다
                        cas.stage_blob(&h(i), block.clone()).unwrap();
                        let _ = t;
                    }
                });
            }
        });
        cas.flush_staged().unwrap();
        let again = CasStorage::new(d.path()).unwrap();
        for i in 0..10 {
            assert_eq!(again.read_blob(&h(i)).unwrap().len(), block.len());
        }
        let idx = crate::pack::load_index(d.path());
        assert_eq!(idx.len(), 10, "같은 해시가 중복으로 들어감");
        assert!(
            crate::pack::pack_ids(d.path()).len() >= 2,
            "한도에서 팩이 나뉘지 않음"
        );
    }

    /// 팩을 쓸 수 없으면(여기서는 packs 자리에 파일이 있음) 백업이 성공으로 끝나지 않도록 flush 가 오류를 돌려준다.
    #[test]
    fn flush_reports_pack_write_failure() {
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        fs::write(d.path().join("packs"), b"not a dir").unwrap();
        cas.stage_blob(&h(1), vec![1u8; 100]).unwrap();
        assert!(cas.flush_staged().is_err());
        assert!(
            cas.flush_staged().is_err(),
            "한 번 실패하면 계속 오류여야 함"
        );
    }

    fn walkdir_tmp(dir: &Path) -> Vec<PathBuf> {
        let mut out = vec![];
        let mut st = vec![dir.to_path_buf()];
        while let Some(d) = st.pop() {
            for e in fs::read_dir(&d).into_iter().flatten().flatten() {
                let p = e.path();
                if p.is_dir() {
                    st.push(p)
                } else if p.extension().is_some_and(|x| x == "tmp") {
                    out.push(p)
                }
            }
        }
        out
    }

    #[test]
    fn get_recent_manifests_for_profile_filtering() {
        use std::time::Duration;
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        let snaps = d.path().join("snapshots");
        fs::create_dir_all(&snaps).unwrap();
        fs::write(
            snaps.join("s1.json"),
            r#"{"id":"s1","profile_id":"prof_a","files":[{"path":"a.txt","size":1}]}"#,
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(20));
        fs::write(
            snaps.join("s2.json"),
            r#"{"id":"s2","profile_id":"prof_b","files":[{"path":"b.txt","size":1}]}"#,
        )
        .unwrap();

        let a = cas.get_recent_manifests_for_profile(10, Some("prof_a"));
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].id.as_deref(), Some("s1"));

        let b = cas.get_recent_manifests_for_profile(10, Some("prof_b"));
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].id.as_deref(), Some("s2"));

        let all = cas.get_recent_manifests_for_profile(10, None);
        assert_eq!(all.len(), 2);
    }
}
