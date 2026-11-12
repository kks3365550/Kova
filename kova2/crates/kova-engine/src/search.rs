// =========================================================================
// [KOVA Fast Engine] Ultra-Fast Parallel Snapshot Search Module
// Rayon Multi-Threading + Zero-Allocation Chosung Decomposition + Score Ranking
// Optimized with Lightweight Manifest Deserialization (Zero-Chunk Overhead)
// =========================================================================

use chrono::{DateTime, Local};
use memmap2::Mmap;
use rayon::prelude::*;
use serde::{Deserialize, Deserializer, Serialize};
use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};

const CHOSUNG_LIST: [char; 19] = [
    'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ',
    'ㅌ', 'ㅍ', 'ㅎ',
];

/// Extracts Korean Chosung (initial consonants) from a string.
pub fn extract_chosung(text: &str) -> String {
    let mut result = String::with_capacity(text.len());
    for c in text.chars() {
        let code = c as u32;
        if (0xAC00..=0xD7A3).contains(&code) {
            let idx = ((code - 0xAC00) / (21 * 28)) as usize;
            if idx < CHOSUNG_LIST.len() {
                result.push(CHOSUNG_LIST[idx]);
                continue;
            }
        }
        result.push(c);
    }
    result
}

pub fn is_chosung_query(query: &str) -> bool {
    !query.is_empty() && query.chars().all(|c| CHOSUNG_LIST.contains(&c))
}

pub fn format_bytes(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = KB * 1024;
    const GB: u64 = MB * 1024;
    const TB: u64 = GB * 1024;

    if bytes >= TB {
        format!("{:.2} TB", bytes as f64 / TB as f64)
    } else if bytes >= GB {
        format!("{:.2} GB", bytes as f64 / GB as f64)
    } else if bytes >= MB {
        format!("{:.2} MB", bytes as f64 / MB as f64)
    } else if bytes >= KB {
        format!("{:.1} KB", bytes as f64 / KB as f64)
    } else {
        format!("{} B", bytes)
    }
}

pub fn format_mtime(mtime: u64) -> String {
    let secs = if mtime >= 100_000_000_000 {
        (mtime / 1000) as i64
    } else {
        mtime as i64
    };
    if let Some(dt) = DateTime::from_timestamp(secs, 0) {
        let local_dt: DateTime<Local> = DateTime::from(dt);
        local_dt.format("%Y-%m-%d %H:%M").to_string()
    } else {
        String::new()
    }
}

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

/// Ultra-lightweight file entry without chunks array.
/// Omitting chunks skips millions of heap allocations in 200MB JSON files.
#[derive(Deserialize, Debug, Clone, Default)]
pub struct SearchFileEntry {
    #[serde(alias = "rel_path", default)]
    pub path: String,
    #[serde(default)]
    pub size: u64,
    #[serde(alias = "mtime", deserialize_with = "deserialize_mtime", default)]
    pub modified: u64,
    #[serde(default)]
    pub is_dir: bool,
    #[serde(default)]
    pub blob_id: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
}

#[derive(Deserialize, Debug, Clone, Default)]
pub struct SearchManifest {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub created_at: Option<serde_json::Value>,
    #[serde(default)]
    pub iso_time: Option<String>,
    #[serde(default)]
    pub profile_name: Option<String>,
    #[serde(default)]
    pub source_root: Option<String>,
    #[serde(default)]
    pub sources: Option<serde_json::Value>,
    #[serde(default)]
    pub files: Option<Vec<SearchFileEntry>>,
    #[serde(default)]
    pub entries: Option<Vec<SearchFileEntry>>,
}

impl SearchManifest {
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

    pub fn take_files(&mut self) -> Vec<SearchFileEntry> {
        self.files
            .take()
            .or_else(|| self.entries.take())
            .unwrap_or_default()
    }
}

pub fn calculate_score(
    fname_lower: &str,
    norm_path: &str,
    q_lower: &str,
    query_chosung: Option<&str>,
) -> u32 {
    if fname_lower == q_lower {
        return 100;
    }
    if fname_lower.starts_with(q_lower) {
        return 80;
    }
    if let Some(qc) = query_chosung {
        let fname_chosung = extract_chosung(fname_lower);
        if fname_chosung.contains(qc) {
            return 75;
        }
    }
    if fname_lower.contains(q_lower) {
        return 60;
    }
    if norm_path.to_lowercase().contains(q_lower) {
        return 30;
    }
    0
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct VersionDetail {
    pub snapshot_id: String,
    pub snapshot_time: String,
    pub profile_name: String,
    pub size: u64,
    pub size_formatted: String,
    pub mtime: u64,
    pub mtime_formatted: String,
    pub blob_id: String,
    pub repo_dir: String,
    pub rel_path: String,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct SearchResultItem {
    pub filename: String,
    pub path: String,
    pub rel_path: String,
    pub source_root: String,
    pub relevance: u32,
    pub version_count: usize,
    pub latest_snapshot_id: String,
    pub latest_size: u64,
    pub latest_size_formatted: String,
    pub latest_time: String,
    pub latest_mtime: u64,
    pub latest_mtime_formatted: String,
    pub versions: Vec<VersionDetail>,
}

/// Executes ultra-fast search across CAS snapshot manifests using memory mapping and Rayon.
pub fn search_snapshots(
    repo_dir: &Path,
    query: &str,
    max_snapshots: usize,
    limit: usize,
    enable_chosung: bool,
) -> io::Result<Vec<SearchResultItem>> {
    let q_clean = query.trim();
    if q_clean.is_empty() {
        return Ok(Vec::new());
    }

    let q_lower = q_clean.to_lowercase();
    #[allow(clippy::if_same_then_else)]
    let query_chosung = if enable_chosung && is_chosung_query(q_clean) {
        Some(q_clean)
    } else if enable_chosung
        && q_clean
            .chars()
            .any(|c| (0xAC00..=0xD7A3).contains(&(c as u32)))
    {
        Some(q_clean)
    } else {
        None
    };

    let snapshots_dir = repo_dir.join("snapshots");
    if !snapshots_dir.is_dir() || max_snapshots == 0 {
        return Ok(Vec::new());
    }

    // Collect candidate snapshot files sorted by modification time descending
    let mut entries: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
    if let Ok(dir_entries) = std::fs::read_dir(&snapshots_dir) {
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
    let target_paths: Vec<PathBuf> = entries
        .into_iter()
        .take(max_snapshots)
        .map(|(_, p)| p)
        .collect();

    if target_paths.is_empty() {
        return Ok(Vec::new());
    }

    // Parse manifests in parallel using mmap and lightweight SearchManifest (ignoring chunks)
    let parsed_manifests: Vec<SearchManifest> = target_paths
        .par_iter()
        .filter_map(|path| {
            let file = File::open(path).ok()?;
            let mmap = unsafe { Mmap::map(&file).ok()? };
            serde_json::from_slice::<SearchManifest>(&mmap).ok()
        })
        .collect();

    let repo_str = repo_dir.to_string_lossy().to_string();
    let mut grouped: HashMap<String, SearchResultItem> = HashMap::new();

    for mut manifest in parsed_manifests {
        let sid = manifest.session_id();
        let s_time = if let Some(ref iso) = manifest.iso_time {
            iso.clone()
        } else {
            match &manifest.created_at {
                Some(serde_json::Value::String(s)) => s.clone(),
                Some(serde_json::Value::Number(n)) => {
                    let ts = if let Some(u) = n.as_u64() {
                        if u >= 100_000_000_000 {
                            (u / 1000) as i64
                        } else {
                            u as i64
                        }
                    } else if let Some(f) = n.as_f64() {
                        if f >= 100_000_000_000.0 {
                            (f / 1000.0) as i64
                        } else {
                            f as i64
                        }
                    } else {
                        0
                    };
                    if let Some(dt) = DateTime::from_timestamp(ts, 0) {
                        let local_dt: DateTime<Local> = DateTime::from(dt);
                        local_dt.format("%Y-%m-%dT%H:%M:%S").to_string()
                    } else {
                        n.to_string()
                    }
                }
                Some(v) => v.to_string().trim_matches('"').to_string(),
                None => String::new(),
            }
        };

        let profile_name = manifest.profile_name.clone().unwrap_or_else(|| {
            if sid.starts_with("snap_custom") {
                "선택 백업".to_string()
            } else {
                "일반 백업".to_string()
            }
        });

        let s_root = manifest.source_root();

        let files = manifest.take_files();

        for entry in files {
            if entry.is_dir {
                continue;
            }

            let rel_path = entry.path;
            let filename = Path::new(&rel_path)
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_else(|| rel_path.clone());

            let fname_lower = filename.to_lowercase();
            let norm_k = rel_path.replace('\\', "/").to_lowercase();

            let score = calculate_score(&fname_lower, &norm_k, &q_lower, query_chosung);
            if score == 0 {
                continue;
            }

            // Clean full path: do not concatenate if s_root contains multiple roots separated by ';'
            let full_path = if s_root.is_empty() || s_root.contains(';') {
                rel_path.clone()
            } else {
                format!(
                    "{}\\{}",
                    s_root.trim_end_matches('\\'),
                    rel_path.trim_start_matches('\\')
                )
            };

            let canonical_key = full_path.replace('\\', "/").to_lowercase();

            let blob_id = entry.blob_id.or(entry.sha256).unwrap_or_default();

            let version_item = VersionDetail {
                snapshot_id: sid.clone(),
                snapshot_time: s_time.clone(),
                profile_name: profile_name.clone(),
                size: entry.size,
                size_formatted: format_bytes(entry.size),
                mtime: entry.modified,
                mtime_formatted: format_mtime(entry.modified),
                blob_id,
                repo_dir: repo_str.clone(),
                rel_path: rel_path.clone(),
            };

            if let Some(existing) = grouped.get_mut(&canonical_key) {
                if score > existing.relevance {
                    existing.relevance = score;
                }
                existing.versions.push(version_item);
            } else {
                grouped.insert(
                    canonical_key,
                    SearchResultItem {
                        filename,
                        path: full_path,
                        rel_path,
                        source_root: s_root.clone(),
                        relevance: score,
                        version_count: 1,
                        latest_snapshot_id: sid.clone(),
                        latest_size: entry.size,
                        latest_size_formatted: format_bytes(entry.size),
                        latest_time: s_time.clone(),
                        latest_mtime: entry.modified,
                        latest_mtime_formatted: format_mtime(entry.modified),
                        versions: vec![version_item],
                    },
                );
            }
        }
    }

    let mut results: Vec<SearchResultItem> = grouped.into_values().collect();

    // Sort versions inside each item (newest snapshot first) and update latest stats
    for item in &mut results {
        item.versions
            .sort_by(|a, b| b.snapshot_time.cmp(&a.snapshot_time));
        item.version_count = item.versions.len();
        if let Some(first) = item.versions.first() {
            item.latest_snapshot_id = first.snapshot_id.clone();
            item.latest_size = first.size;
            item.latest_size_formatted = first.size_formatted.clone();
            item.latest_time = first.snapshot_time.clone();
            item.latest_mtime = first.mtime;
            item.latest_mtime_formatted = first.mtime_formatted.clone();
        }
    }

    // Sort items by relevance descending, then latest_mtime descending
    results.sort_by(|a, b| {
        b.relevance
            .cmp(&a.relevance)
            .then_with(|| b.latest_mtime.cmp(&a.latest_mtime))
    });

    if limit > 0 && results.len() > limit {
        results.truncate(limit);
    }

    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_chosung_extraction() {
        assert_eq!(extract_chosung("품질규격서"), "ㅍㅈㄱㄱㅅ");
        assert_eq!(extract_chosung("삼영데리카후레쉬"), "ㅅㅇㄷㄹㅋㅎㄹㅅ");
        assert_eq!(extract_chosung("Excel2026.xlsx"), "Excel2026.xlsx");
        assert_eq!(extract_chosung("보고서_v2"), "ㅂㄱㅅ_v2");
    }

    #[test]
    fn test_chosung_query_check() {
        assert!(is_chosung_query("ㄱㄱㅅ"));
        assert!(is_chosung_query("ㅂㄱㅅ"));
        assert!(!is_chosung_query("규격서"));
        assert!(!is_chosung_query("abc"));
    }

    #[test]
    fn test_score_calculation() {
        let score_exact = calculate_score("spec.xlsx", "doc/spec.xlsx", "spec.xlsx", None);
        assert_eq!(score_exact, 100);

        let score_prefix = calculate_score("spec_2026.xlsx", "doc/spec_2026.xlsx", "spec", None);
        assert_eq!(score_prefix, 80);

        let score_chosung =
            calculate_score("규격서.xlsx", "doc/규격서.xlsx", "ㄱㄱㅅ", Some("ㄱㄱㅅ"));
        assert_eq!(score_chosung, 75);

        let score_substr = calculate_score("2026_spec_final.xlsx", "doc/file.xlsx", "spec", None);
        assert_eq!(score_substr, 60);

        let score_path = calculate_score("data.csv", "documents/reports/data.csv", "reports", None);
        assert_eq!(score_path, 30);
    }
}
