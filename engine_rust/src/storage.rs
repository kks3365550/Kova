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
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct SnapshotManifest {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub created_at: Option<serde_json::Value>,
    #[serde(default)]
    pub source_root: Option<String>,
    #[serde(default)]
    pub sources: Option<serde_json::Value>,
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
        self.session_id.clone().or_else(|| self.id.clone()).unwrap_or_default()
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
}

impl CasStorage {
    pub fn new<P: AsRef<Path>>(repo_root: P) -> io::Result<Self> {
        let root = repo_root.as_ref().to_path_buf();
        fs::create_dir_all(root.join("blobs"))?;
        fs::create_dir_all(root.join("snapshots"))?;
        Ok(Self { repo_root: root })
    }

    pub fn blob_path(&self, hash: &str) -> PathBuf {
        let prefix = if hash.len() >= 2 { &hash[0..2] } else { "misc" };
        self.repo_root.join("blobs").join(prefix).join(format!("{}.blob", hash))
    }

    pub fn has_blob(&self, hash: &str) -> bool {
        self.blob_path(hash).exists()
    }

    /// Stores compressed blob data atomically. Returns true if newly written, false if deduplicated.
    pub fn save_blob(&self, hash: &str, compressed_data: &[u8]) -> io::Result<bool> {
        let target_path = self.blob_path(hash);
        if target_path.exists() {
            return Ok(false);
        }

        if let Some(parent) = target_path.parent() {
            fs::create_dir_all(parent)?;
        }

        let tmp_path = target_path.with_extension("tmp");
        {
            let mut file = File::create(&tmp_path)?;
            file.write_all(compressed_data)?;
            file.flush()?;
        }

        fs::rename(&tmp_path, &target_path)?;
        Ok(true)
    }

    pub fn read_blob(&self, hash: &str) -> io::Result<Vec<u8>> {
        let target_path = self.blob_path(hash);
        let mut file = File::open(target_path)?;
        let mut data = Vec::new();
        file.read_to_end(&mut data)?;
        Ok(data)
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
        let target_path = self.repo_root.join("snapshots").join(format!("{}.json", session_id));
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
                if path.is_file() && path.extension().map_or(false, |ext| ext == "json") {
                    if let Ok(meta) = entry.metadata() {
                        if let Ok(mtime) = meta.modified() {
                            entries.push((mtime, path));
                        }
                    }
                }
            }
        }

        entries.sort_by(|a, b| b.0.cmp(&a.0));

        let mut manifests: Vec<SnapshotManifest> = Vec::new();
        for (_mtime, path) in entries {
            match File::open(&path) {
                Ok(file) => {
                    let reader = BufReader::new(file);
                    match serde_json::from_reader::<_, SnapshotManifest>(reader) {
                        Ok(manifest) => {
                            let count = manifest.files.as_ref().map_or(0, |f| f.len())
                                + manifest.entries.as_ref().map_or(0, |e| e.len());
                            if count > 0 {
                                manifests.push(manifest);
                                if manifests.len() >= limit {
                                    break;
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!("[KOVA-FAST-DEBUG] Failed to parse manifest {}: {}", path.display(), e);
                        }
                    }
                }
                Err(e) => {
                    eprintln!("[KOVA-FAST-DEBUG] Failed to open manifest file {}: {}", path.display(), e);
                }
            }
        }

        manifests
    }

    pub fn get_latest_manifest(&self) -> Option<SnapshotManifest> {
        self.get_recent_manifests(1).into_iter().next()
    }
}

