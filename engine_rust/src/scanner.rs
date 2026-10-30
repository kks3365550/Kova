use rayon::prelude::*;
use std::collections::HashSet;
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

/// Name-based exclude filter mirroring core/filter.py (PathFilter).
#[derive(Debug, Default)]
pub struct ExcludeFilter {
    exts: HashSet<String>,
    substrs: Vec<String>,
    exact: HashSet<String>,
    complex: Vec<Vec<char>>,
}

const VOLATILE_DIRS: [&str; 11] = [
    "cache", "caches", "gpucache", "shadercache", "code cache", "temp", "tmp", "crashpad",
    "crashreporting", "logs", "log",
];

fn glob_match(p: &[char], s: &[char]) -> bool {
    if p.is_empty() {
        return s.is_empty();
    }
    match p[0] {
        '*' => (0..=s.len()).any(|i| glob_match(&p[1..], &s[i..])),
        '?' => !s.is_empty() && glob_match(&p[1..], &s[1..]),
        c => !s.is_empty() && s[0] == c && glob_match(&p[1..], &s[1..]),
    }
}

impl ExcludeFilter {
    pub fn new(patterns: &[String]) -> Self {
        let mut f = ExcludeFilter::default();
        for raw in patterns {
            let p = raw.trim().to_lowercase();
            if p.is_empty() || p.starts_with('#') {
                continue;
            }
            if p.contains('/') || p.contains('\\') {
                // Path-style patterns are not matched by the Python filter either.
                continue;
            }
            if p.starts_with("*.") && !p[2..].contains(|c| c == '*' || c == '?' || c == '[') {
                f.exts.insert(p[1..].to_string());
            } else if p.len() > 2
                && p.starts_with('*')
                && p.ends_with('*')
                && !p[1..p.len() - 1].contains(|c| c == '*' || c == '?' || c == '[')
            {
                f.substrs.push(p[1..p.len() - 1].to_string());
            } else if p.contains(|c| c == '*' || c == '?' || c == '[') {
                f.complex.push(p.chars().collect());
            } else {
                f.exact.insert(p);
            }
        }
        f
    }

    fn matches_common(&self, name: &str) -> bool {
        if self.exact.contains(name) {
            return true;
        }
        if self.substrs.iter().any(|s| name.contains(s.as_str())) {
            return true;
        }
        let chars: Vec<char> = name.chars().collect();
        self.complex.iter().any(|cp| glob_match(cp, &chars))
    }

    fn ext_excluded(&self, name: &str) -> bool {
        match name.rfind('.') {
            Some(i) => self.exts.contains(&name[i..]),
            None => false,
        }
    }

    pub fn is_dir_excluded(&self, name: &str) -> bool {
        let n = name.to_lowercase();
        if VOLATILE_DIRS.contains(&n.as_str()) || n == "deliveryoptimization" {
            return true;
        }
        self.matches_common(&n)
    }

    pub fn is_file_excluded(&self, name: &str) -> bool {
        let n = name.to_lowercase();
        if self.ext_excluded(&n) {
            return true;
        }
        if n.ends_with(".py") {
            // Never drop Python sources through substring/glob matches.
            return self.exact.contains(&n);
        }
        self.matches_common(&n)
    }
}

pub fn scan_directory(source_root: &Path, filter: &ExcludeFilter) -> Vec<ScannedItem> {
    // A source that is a single file: use the file name as its relative path.
    if source_root.is_file() {
        let name = match source_root.file_name() {
            Some(n) => n.to_string_lossy().to_string(),
            None => return Vec::new(),
        };
        return match fs::symlink_metadata(source_root) {
            Ok(m) => vec![ScannedItem {
                path: source_root.to_path_buf(),
                rel_path: name,
                is_dir: false,
                size: m.len(),
                modified: mtime_ms(&m),
            }],
            Err(_) => Vec::new(),
        };
    }

    if !source_root.is_dir() {
        return Vec::new();
    }

    // Read top-level children to branch subdirectories across Rayon thread pool
    let mut top_files = Vec::new();
    let mut top_subdirs = Vec::new();

    if let Ok(rd) = fs::read_dir(source_root) {
        for entry in rd.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            let file_type = match entry.file_type() {
                Ok(ft) => ft,
                Err(_) => continue,
            };

            if file_type.is_dir() {
                if !filter.is_dir_excluded(&name) {
                    top_subdirs.push(path);
                }
            } else if !filter.is_file_excluded(&name) {
                if let Ok(metadata) = entry.metadata() {
                    if let Ok(rel) = path.strip_prefix(source_root) {
                        let rel_path = rel.to_string_lossy().replace('\\', "/");
                        if !rel_path.is_empty() {
                            top_files.push(ScannedItem {
                                path,
                                rel_path,
                                is_dir: false,
                                size: metadata.len(),
                                modified: mtime_ms(&metadata),
                            });
                        }
                    }
                }
            }
        }
    }

    // Scan each subdirectory in parallel using Rayon full-core worker pool
    let subdir_items: Vec<ScannedItem> = top_subdirs
        .into_par_iter()
        .flat_map(|subdir| {
            let mut items = Vec::new();

            // Include the subdir itself if metadata is available
            if let Ok(m) = fs::symlink_metadata(&subdir) {
                if let Ok(rel) = subdir.strip_prefix(source_root) {
                    let rel_path = rel.to_string_lossy().replace('\\', "/");
                    if !rel_path.is_empty() {
                        items.push(ScannedItem {
                            path: subdir.clone(),
                            rel_path,
                            is_dir: true,
                            size: 0,
                            modified: mtime_ms(&m),
                        });
                    }
                }
            }

            let entries: Vec<_> = WalkDir::new(&subdir)
                .into_iter()
                .filter_entry(|e| {
                    if e.depth() == 0 {
                        return true;
                    }
                    let name = e.file_name().to_string_lossy();
                    if e.file_type().is_dir() {
                        !filter.is_dir_excluded(&name)
                    } else {
                        !filter.is_file_excluded(&name)
                    }
                })
                .filter_map(|e| e.ok())
                .collect();

            let mut scanned: Vec<ScannedItem> = entries
                .into_iter()
                .filter_map(|entry| {
                    if entry.depth() == 0 {
                        // Already recorded the root subdir above
                        return None;
                    }
                    let metadata = entry.metadata().ok()?;
                    let is_dir = metadata.is_dir();
                    let size = if is_dir { 0 } else { metadata.len() };
                    let modified = mtime_ms(&metadata);

                    let path = entry.into_path();
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
                .collect();

            items.append(&mut scanned);
            items
        })
        .collect();

    top_files.extend(subdir_items);
    top_files
}

fn mtime_ms(m: &fs::Metadata) -> u64 {
    m.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pats(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn filter_matches_like_python() {
        let f = ExcludeFilter::new(&pats(&["node_modules", "*.tmp", "*cache*", "~$*", ".git"]));
        assert!(f.is_dir_excluded("node_modules"));
        assert!(f.is_dir_excluded("Node_Modules"));
        assert!(f.is_dir_excluded("temp"));
        assert!(f.is_file_excluded("a.TMP"));
        assert!(f.is_file_excluded("~$doc.docx"));
        assert!(!f.is_file_excluded("linecache.py"));
        assert!(!f.is_file_excluded("report.xlsx"));
    }

    #[test]
    fn scan_prunes_and_handles_single_file() {
        let root = std::env::temp_dir().join(format!("kova_scan_test_{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("node_modules/x")).unwrap();
        fs::create_dir_all(root.join("src")).unwrap();
        fs::write(root.join("node_modules/x/a.js"), b"1").unwrap();
        fs::write(root.join("src/b.txt"), b"2").unwrap();
        fs::write(root.join("c.tmp"), b"3").unwrap();
        let f = ExcludeFilter::new(&pats(&["node_modules", "*.tmp"]));
        let items = scan_directory(&root, &f);
        let rels: Vec<_> = items.iter().map(|i| i.rel_path.clone()).collect();
        assert!(rels.contains(&"src/b.txt".to_string()));
        assert!(!rels.iter().any(|r| r.contains("node_modules") || r.ends_with(".tmp")));
        let single = scan_directory(&root.join("src/b.txt"), &f);
        assert_eq!(single.len(), 1);
        assert_eq!(single[0].rel_path, "b.txt");
        let _ = fs::remove_dir_all(&root);
    }
}
