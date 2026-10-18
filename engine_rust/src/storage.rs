use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct FileChunkInfo {
    pub hash: String,
    pub offset: u64,
    pub length: usize,
    pub compressed_length: usize,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct FileEntry {
    pub path: String, // Relative path from source root
    pub size: u64,
    pub modified: u64, // Unix timestamp in seconds
    pub is_dir: bool,
    pub chunks: Vec<FileChunkInfo>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SnapshotManifest {
    pub session_id: String,
    pub created_at: String,
    pub source_root: String,
    pub total_files: usize,
    pub total_bytes: u64,
    pub dedup_bytes: u64,
    pub compressed_bytes: u64,
    pub files: Vec<FileEntry>,
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
        let target_path = snapshots_dir.join(format!("{}.json", manifest.session_id));
        let tmp_path = target_path.with_extension("tmp");

        let json = serde_json::to_string(manifest)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        fs::write(&tmp_path, json.as_bytes())?;
        fs::rename(&tmp_path, &target_path)?;
        Ok(())
    }

    pub fn load_manifest(&self, session_id: &str) -> io::Result<SnapshotManifest> {
        let target_path = self.repo_root.join("snapshots").join(format!("{}.json", session_id));
        let data = fs::read_to_string(target_path)?;
        let manifest: SnapshotManifest = serde_json::from_str(&data)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        Ok(manifest)
    }

    pub fn get_latest_manifest(&self) -> Option<SnapshotManifest> {
        let snapshots_dir = self.repo_root.join("snapshots");
        if !snapshots_dir.is_dir() {
            return None;
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

        for (_mtime, path) in entries {
            if let Ok(data) = fs::read_to_string(&path) {
                if let Ok(manifest) = serde_json::from_str::<SnapshotManifest>(&data) {
                    return Some(manifest);
                }
            }
        }

        None
    }
}

