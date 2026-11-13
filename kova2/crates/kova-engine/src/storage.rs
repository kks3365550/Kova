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
}

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
        let prefix = if hash.len() >= 2 { &hash[0..2] } else { "misc" };
        self.repo_root
            .join("blobs")
            .join(prefix)
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

    pub fn read_blob(&self, hash: &str) -> io::Result<Vec<u8>> {
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
                    Some(entry) => crate::pack::read_entry(&self.repo_root, &entry),
                    None => Err(e),
                }
            }
            Err(e) => Err(e),
        }
    }

    pub fn save_manifest(&self, manifest: &SnapshotManifest) -> io::Result<()> {
        let snapshots_dir = self.repo_root.join("snapshots");
        fs::create_dir_all(&snapshots_dir)?;
        let target_path = snapshots_dir.join(format!("{}.json", manifest.session_id()));
        let tmp_path = target_path.with_extension("tmp");

        let json = serde_json::to_string(manifest)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        fs::write(&tmp_path, json.as_bytes())?;
        fs::rename(&tmp_path, &target_path)?;
        Ok(())
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

    pub fn get_recent_manifests(&self, limit: usize) -> Vec<SnapshotManifest> {
        let snapshots_dir = self.repo_root.join("snapshots");
        if !snapshots_dir.is_dir() || limit == 0 {
            return Vec::new();
        }

        let mut entries: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
        if let Ok(dir_entries) = fs::read_dir(snapshots_dir) {
            for entry in dir_entries.flatten() {
                let path = entry.path();
                if path.is_file() && path.extension().is_some_and(|ext| ext == "json") {
                    if let Ok(meta) = entry.metadata() {
                        if let Ok(mtime) = meta.modified() {
                            entries.push((mtime, path));
                        }
                    }
                }
            }
        }

        entries.sort_by_key(|e| std::cmp::Reverse(e.0));

        // Parse candidate manifests in parallel batches (newest first, order preserved).
        use rayon::prelude::*;
        let paths: Vec<PathBuf> = entries.into_iter().map(|(_, p)| p).collect();
        let mut manifests: Vec<SnapshotManifest> = Vec::new();
        for batch in paths.chunks(limit) {
            let parsed: Vec<Option<SnapshotManifest>> = batch
                .par_iter()
                .map(|path| match File::open(path) {
                    Ok(file) => {
                        match serde_json::from_reader::<_, SnapshotManifest>(BufReader::new(file)) {
                            Ok(m) => {
                                let count = m.files.as_ref().map_or(0, |f| f.len())
                                    + m.entries.as_ref().map_or(0, |e| e.len());
                                if count > 0 {
                                    Some(m)
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
}
