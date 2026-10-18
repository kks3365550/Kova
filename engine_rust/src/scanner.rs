use rayon::prelude::*;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;
use walkdir::WalkDir;

#[derive(Debug, Clone)]
pub struct ScannedItem {
    pub path: PathBuf,
    pub rel_path: String,
    pub is_dir: bool,
    pub size: u64,
    pub modified: u64,
}

pub fn scan_directory(source_root: &Path) -> Vec<ScannedItem> {
    let entries: Vec<_> = WalkDir::new(source_root)
        .into_iter()
        .filter_map(|e| e.ok())
        .collect();

    entries
        .into_par_iter()
        .filter_map(|entry| {
            let path = entry.path().to_path_buf();
            let metadata = fs::symlink_metadata(&path).ok()?;
            let is_dir = metadata.is_dir();
            let size = if is_dir { 0 } else { metadata.len() };
            let modified = metadata
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0);

            let rel_path = path
                .strip_prefix(source_root)
                .ok()?
                .to_string_lossy()
                .replace('\\', "/");

            if rel_path.is_empty() {
                return None;
            }

            Some(ScannedItem {
                path,
                rel_path,
                is_dir,
                size,
                modified,
            })
        })
        .collect()
}
