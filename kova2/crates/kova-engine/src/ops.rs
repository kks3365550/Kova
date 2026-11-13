//! 백업·복원 작업을 라이브러리 함수로 제공한다 (서비스가 서브프로세스 없이 직접 호출).
//! - 진행 상황은 콜백(`on_event`)으로 전달하고, 취소는 `cancel` 플래그로 받는다.
//! - 취소되면 매니페스트를 쓰지 않는다(반쪽짜리 스냅샷이 생기지 않는다).
//! - 복원은 청크마다 BLAKE3 해시를 다시 확인한다(변조·손상된 데이터를 조용히 복원하지 않는다).
use crate::storage::{CasStorage, FileChunkInfo, FileEntry, SnapshotManifest};
use crate::{chunker, hasher, scanner};
use rayon::prelude::*;
use std::collections::HashMap;
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, UNIX_EPOCH};

#[derive(Debug)]
pub enum EngineError {
    Io(io::Error),
    Cancelled,
    Msg(String),
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EngineError::Io(e) => write!(f, "{}", e),
            EngineError::Cancelled => write!(f, "cancelled"),
            EngineError::Msg(m) => write!(f, "{}", m),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<io::Error> for EngineError {
    fn from(e: io::Error) -> Self {
        EngineError::Io(e)
    }
}

fn msg<S: Into<String>>(s: S) -> EngineError {
    EngineError::Msg(s.into())
}

#[derive(Debug, Clone)]
pub struct Progress {
    pub percent: u64,
    pub processed_files: usize,
    pub total_files: usize,
    pub processed_size: u64,
    /// MB/s
    pub speed: f64,
    pub current_file: String,
    pub new_files: usize,
    pub modified_files: usize,
    pub unmodified_files: usize,
}

#[derive(Debug, Clone)]
pub enum Event {
    Phase {
        phase: &'static str,
        message: String,
    },
    Progress(Progress),
    RestoreProgress {
        done: usize,
        total: usize,
    },
}

#[derive(Debug, Clone)]
pub struct BackupOptions {
    pub sources: Vec<PathBuf>,
    pub repo: PathBuf,
    pub session_id: Option<String>,
    pub zstd_level: i32,
    pub exclude: Vec<String>,
    /// 매니페스트에 기록할 프로파일 ID(없으면 기록 안 함)
    pub profile_id: Option<String>,
    /// Some: `sources` 와 같은 순서로, 실제로 읽을 경로(VSS 섀도 복사본 안의 같은 경로). 매니페스트에는 `sources` 가 기록된다
    pub scan_from: Option<Vec<PathBuf>>,
}

#[derive(Debug, Clone)]
pub struct BackupSummary {
    pub session_id: String,
    /// 매니페스트에 기록된 항목 수(폴더 포함)
    pub total_files: usize,
    pub total_bytes: u64,
    pub dedup_bytes: u64,
    pub compressed_bytes: u64,
    pub elapsed_seconds: f64,
    pub throughput_mb: f64,
    pub new_files: usize,
    pub modified_files: usize,
    pub unmodified_files: usize,
    /// 읽기·압축·저장에 실패해 스냅샷에서 빠진 파일(상대 경로)
    pub skipped_files: Vec<String>,
}

#[allow(clippy::too_many_arguments)]
fn now_progress(
    start: &Instant,
    done: usize,
    total: usize,
    bytes: u64,
    cur: &str,
    n: usize,
    m: usize,
    u: usize,
) -> Progress {
    let pct = if total > 0 {
        (done as f64 / total as f64) * 100.0
    } else {
        100.0
    };
    let elapsed = start.elapsed().as_secs_f64();
    Progress {
        percent: pct.round() as u64,
        processed_files: done,
        total_files: total,
        processed_size: bytes,
        speed: (bytes as f64 / (1024.0 * 1024.0)) / elapsed.max(0.001),
        current_file: cur.to_string(),
        new_files: n,
        modified_files: m,
        unmodified_files: u,
    }
}

pub fn run_backup(
    opts: &BackupOptions,
    on_event: &(dyn Fn(Event) + Sync),
    cancel: &AtomicBool,
) -> Result<BackupSummary, EngineError> {
    let start_time = Instant::now();
    let sources = &opts.sources;
    if sources.is_empty() {
        return Err(msg("no source paths given"));
    }
    let sid = opts
        .session_id
        .clone()
        .unwrap_or_else(|| chrono::Local::now().format("%Y%m%d_%H%M%S").to_string());
    // 세션 ID 는 매니페스트 파일 이름이 되므로 경로 조각이어야 한다
    if sid.is_empty() || sid.contains(['/', '\\', ':']) || sid == "." || sid == ".." {
        return Err(msg(format!("invalid session id: {}", sid)));
    }
    let cas = CasStorage::new(&opts.repo)?;

    on_event(Event::Phase {
        phase: "scanning",
        message: "[1/3] 대상 경로 파일 병렬 스캔 중...".into(),
    });
    let scan_start = Instant::now();
    let exclude_filter = scanner::ExcludeFilter::new(&opts.exclude);
    let scan_roots = match &opts.scan_from {
        Some(v) if v.len() == sources.len() => v,
        Some(_) => return Err(msg("scan_from 길이가 sources 와 다릅니다")),
        None => sources,
    };
    let items: Vec<scanner::ScannedItem> = scan_roots
        .par_iter()
        .enumerate()
        .map(|(idx, src)| {
            let mut v = scanner::scan_directory(src, &exclude_filter);
            for it in v.iter_mut() {
                it.src = idx as u32;
            }
            v
        })
        .collect::<Vec<_>>()
        .into_iter()
        .flatten()
        .collect();
    on_event(Event::Phase {
        phase: "indexing",
        message: format!(
            "[2/3] 파일 스캔 완료 (총 {}개 항목, {:.2}s). 인메모리 Blob 캐시 로딩 중...",
            items.len(),
            scan_start.elapsed().as_secs_f64()
        ),
    });
    if cancel.load(Ordering::Relaxed) {
        return Err(EngineError::Cancelled);
    }

    let total_bytes = AtomicU64::new(0);
    let dedup_bytes = AtomicU64::new(0);
    let compressed_bytes = AtomicU64::new(0);
    let processed_files = AtomicUsize::new(0);
    let new_files = AtomicUsize::new(0);
    let modified_files = AtomicUsize::new(0);
    let unmodified_files = AtomicUsize::new(0);
    let skipped: Mutex<Vec<String>> = Mutex::new(Vec::new());

    let blob_start = Instant::now();
    let existing_blobs = cas.get_all_blob_hashes();
    on_event(Event::Phase {
        phase: "indexing",
        message: format!(
            "[2/3] 기존 블록 {}개 메모리 인덱싱 완료 ({:.2}s). 최신 스냅샷 대조 중...",
            existing_blobs.len(),
            blob_start.elapsed().as_secs_f64()
        ),
    });

    // 가장 최근 스냅샷으로 증분 빠른 경로 지도를 만든다
    let mut prev_files_map: HashMap<String, FileEntry> = HashMap::new();
    for manifest in cas.get_recent_manifests(1) {
        for mut entry in manifest.files() {
            if entry.is_dir {
                continue;
            }
            if entry.chunks.is_empty() {
                if let Some(bid) = entry.blob_id.as_ref().or(entry.sha256.as_ref()) {
                    entry.chunks.push(FileChunkInfo {
                        hash: bid.clone(),
                        offset: 0,
                        length: entry.size as usize,
                        compressed_length: 0,
                    });
                }
            }
            // 같은 상대경로가 서로 다른 원본에 있을 수 있으므로 (원본 번호, 경로)를 키로 쓴다
            let key = format!(
                "{}|{}",
                entry.src.unwrap_or(0),
                entry.path.replace('\\', "/").to_lowercase()
            );
            prev_files_map.entry(key).or_insert(entry);
        }
    }

    let (dirs, files): (Vec<_>, Vec<_>) = items.into_iter().partition(|i| i.is_dir);
    let total_file_count = files.len();
    on_event(Event::Phase {
        phase: "processing",
        message: format!(
            "[3/3] 초고속 증분 백업 가동 (대상: {}개 파일)",
            total_file_count
        ),
    });

    let report = |done: usize, cur: &str| {
        if done.is_multiple_of(50) || done == total_file_count {
            on_event(Event::Progress(now_progress(
                &start_time,
                done,
                total_file_count,
                total_bytes.load(Ordering::Relaxed),
                cur,
                new_files.load(Ordering::Relaxed),
                modified_files.load(Ordering::Relaxed),
                unmodified_files.load(Ordering::Relaxed),
            )));
        }
    };

    let file_entries: Vec<FileEntry> = files
        .into_par_iter()
        .filter_map(|item| {
            if cancel.load(Ordering::Relaxed) {
                return None;
            }
            let key = format!(
                "{}|{}",
                item.src,
                item.rel_path.replace('\\', "/").to_lowercase()
            );
            if let Some(prev) = prev_files_map.get(&key) {
                let diff = prev.modified.abs_diff(item.modified);
                let mtime_match = diff <= 5
                    || (prev.modified % 1000 == 0
                        && (item.modified / 1000) == (prev.modified / 1000));
                // 이전 청크의 blob 이 저장소에 실제로 있을 때만 재사용한다
                if prev.size == item.size
                    && mtime_match
                    && (!prev.chunks.is_empty() || prev.size == 0)
                    && prev.chunks.iter().all(|c| existing_blobs.contains(&c.hash))
                {
                    total_bytes.fetch_add(prev.size, Ordering::Relaxed);
                    dedup_bytes.fetch_add(prev.size, Ordering::Relaxed);
                    unmodified_files.fetch_add(1, Ordering::Relaxed);
                    report(
                        processed_files.fetch_add(1, Ordering::Relaxed) + 1,
                        &item.rel_path,
                    );
                    return Some(FileEntry {
                        path: item.rel_path,
                        size: prev.size,
                        modified: item.modified,
                        is_dir: false,
                        chunks: prev.chunks.clone(),
                        blob_id: prev.blob_id.clone(),
                        sha256: prev.sha256.clone(),
                        src: Some(item.src),
                    });
                }
                modified_files.fetch_add(1, Ordering::Relaxed);
            } else {
                new_files.fetch_add(1, Ordering::Relaxed);
            }

            let skip = |why: String| {
                skipped
                    .lock()
                    .unwrap()
                    .push(format!("{} ({})", item.rel_path, why));
                report(
                    processed_files.fetch_add(1, Ordering::Relaxed) + 1,
                    &item.rel_path,
                );
                None
            };

            let chunks_raw = match chunker::chunk_file(&item.path) {
                Ok(c) => c,
                Err(e) => return skip(format!("read: {}", e)),
            };
            let file_size: u64 = chunks_raw.iter().map(|c| c.length as u64).sum();
            // 청크마다 압축·저장이 독립이라 병렬 처리(큰 파일 하나도 여러 코어 사용). 순서는 유지된다
            let stored: Result<Vec<FileChunkInfo>, String> = chunks_raw
                .into_par_iter()
                .map(|c| {
                    total_bytes.fetch_add(c.length as u64, Ordering::Relaxed);
                    if cas.has_blob(&c.hash) {
                        dedup_bytes.fetch_add(c.length as u64, Ordering::Relaxed);
                        return Ok(FileChunkInfo {
                            hash: c.hash,
                            offset: c.offset,
                            length: c.length,
                            compressed_length: 0,
                        });
                    }
                    let compressed = cas
                        .encode_blob(&c.data, &c.hash, opts.zstd_level)
                        .map_err(|e| format!("compress: {}", e))?;
                    let comp_len = compressed.len();
                    compressed_bytes.fetch_add(comp_len as u64, Ordering::Relaxed);
                    cas.save_blob(&c.hash, &compressed)
                        .map_err(|e| format!("save blob: {}", e))?;
                    Ok(FileChunkInfo {
                        hash: c.hash,
                        offset: c.offset,
                        length: c.length,
                        compressed_length: comp_len,
                    })
                })
                .collect();
            let file_chunks = match stored {
                Ok(v) => v,
                Err(why) => return skip(why),
            };
            report(
                processed_files.fetch_add(1, Ordering::Relaxed) + 1,
                &item.rel_path,
            );
            Some(FileEntry {
                path: item.rel_path,
                size: file_size,
                modified: item.modified,
                is_dir: false,
                chunks: file_chunks,
                blob_id: None,
                sha256: None,
                src: Some(item.src),
            })
        })
        .collect();

    // 취소되면 매니페스트를 쓰지 않는다
    if cancel.load(Ordering::Relaxed) {
        return Err(EngineError::Cancelled);
    }

    let mut all_entries = file_entries;
    for dir in dirs {
        all_entries.push(FileEntry {
            path: dir.rel_path,
            size: 0,
            modified: dir.modified,
            is_dir: true,
            chunks: Vec::new(),
            blob_id: None,
            sha256: None,
            src: Some(dir.src),
        });
    }

    let tot_b = total_bytes.load(Ordering::Relaxed);
    let ded_b = dedup_bytes.load(Ordering::Relaxed);
    let cmp_b = compressed_bytes.load(Ordering::Relaxed);
    let elapsed = start_time.elapsed().as_secs_f64();
    let src_strs: Vec<String> = sources
        .iter()
        .map(|s| s.to_string_lossy().to_string())
        .collect();

    let manifest = SnapshotManifest {
        id: Some(sid.clone()),
        session_id: Some(sid.clone()),
        profile_id: opts.profile_id.clone(),
        created_at: Some(serde_json::Value::String(chrono::Local::now().to_rfc3339())),
        source_root: Some(src_strs.join(";")),
        sources: Some(serde_json::Value::Array(
            src_strs
                .iter()
                .cloned()
                .map(serde_json::Value::String)
                .collect(),
        )),
        source_kinds: Some(
            sources
                .iter()
                .map(|s| {
                    if s.is_file() {
                        "file".to_string()
                    } else {
                        "dir".to_string()
                    }
                })
                .collect(),
        ),
        format_version: Some(2),
        total_files: all_entries.len(),
        total_bytes: tot_b,
        dedup_bytes: ded_b,
        compressed_bytes: cmp_b,
        files: Some(all_entries),
        entries: None,
    };
    cas.save_manifest(&manifest)?;

    Ok(BackupSummary {
        session_id: sid,
        total_files: manifest.total_files,
        total_bytes: tot_b,
        dedup_bytes: ded_b,
        compressed_bytes: cmp_b,
        elapsed_seconds: elapsed,
        throughput_mb: (tot_b as f64 / (1024.0 * 1024.0)) / elapsed.max(0.001),
        new_files: new_files.load(Ordering::Relaxed),
        modified_files: modified_files.load(Ordering::Relaxed),
        unmodified_files: unmodified_files.load(Ordering::Relaxed),
        skipped_files: skipped.into_inner().unwrap(),
    })
}

// ───────────────────────────── 복원 ─────────────────────────────

#[derive(Debug, Clone)]
pub struct RestoreOptions {
    pub repo: PathBuf,
    pub session_id: String,
    /// Some: 해당 폴더 아래로 복원. None: 백업 당시 원래 위치로 복원(format_version 2 스냅샷만)
    pub target: Option<PathBuf>,
    /// Some: 이 가상 경로(`browse_snapshot` 이 돌려주는 경로)의 파일·폴더만 복원한다. 폴더는 하위 전체. None: 전체.
    pub only: Option<Vec<String>>,
}

/// 스냅샷 탐색 결과 한 줄.
#[derive(Debug, Clone, PartialEq)]
pub struct BrowseItem {
    pub name: String,
    /// 복원 폴더 안에서의 상대 경로(`/` 구분). 원본이 여러 곳이면 "001_폴더명/..." 로 시작한다.
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
    /// 폴더면 하위 파일 수, 파일이면 1
    pub files: usize,
}

fn virtual_path(base: &RestoreBase, e: &FileEntry) -> Option<String> {
    let p = resolve_restore_path(base, e).ok()?;
    Some(
        p.components()
            .filter_map(|c| match c {
                Component::Normal(s) => Some(s.to_string_lossy().to_string()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("/"),
    )
}

fn browse_base(m: &SnapshotManifest) -> RestoreBase {
    let sources: Vec<String> = match &m.sources {
        Some(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect(),
        _ => Vec::new(),
    };
    RestoreBase {
        target: Some(PathBuf::new()),
        sources,
        kinds: m.source_kinds.clone().unwrap_or_default(),
    }
}

fn is_under(path: &str, prefix: &str) -> bool {
    prefix.is_empty() || path == prefix || path.starts_with(&format!("{prefix}/"))
}

/// 스냅샷 안을 한 폴더씩 보여준다. `query` 가 있으면 폴더 구분 없이 파일 이름으로 찾고 `limit` 건까지만 돌려준다.
/// 폴더가 먼저, 이름순.
pub fn browse_snapshot(
    repo: &Path,
    session_id: &str,
    dir: &str,
    query: &str,
    limit: usize,
) -> Result<Vec<BrowseItem>, EngineError> {
    if session_id.is_empty() || session_id.contains(['/', '\\', ':']) {
        return Err(msg(format!("invalid session id: {session_id}")));
    }
    let cas = CasStorage::new(repo)?;
    let manifest = cas.load_manifest(session_id)?;
    let base = browse_base(&manifest);
    let dir = dir.trim_matches('/');
    let q = query.trim().to_lowercase();
    let mut dirs: std::collections::BTreeMap<String, BrowseItem> = Default::default();
    let mut files: Vec<BrowseItem> = vec![];
    for e in manifest.files().iter().filter(|e| !e.is_dir) {
        let Some(vp) = virtual_path(&base, e) else {
            continue;
        };
        if !q.is_empty() {
            let name = vp.rsplit('/').next().unwrap_or(&vp);
            if name.to_lowercase().contains(&q) {
                files.push(BrowseItem {
                    name: vp.clone(),
                    path: vp,
                    is_dir: false,
                    size: e.size,
                    files: 1,
                });
            }
            continue;
        }
        if !dir.is_empty() && !vp.starts_with(&format!("{dir}/")) {
            continue;
        }
        let rest = if dir.is_empty() {
            vp.as_str()
        } else {
            &vp[dir.len() + 1..]
        };
        match rest.split_once('/') {
            Some((first, _)) => {
                let it = dirs.entry(first.to_string()).or_insert_with(|| BrowseItem {
                    name: first.to_string(),
                    path: if dir.is_empty() {
                        first.to_string()
                    } else {
                        format!("{dir}/{first}")
                    },
                    is_dir: true,
                    size: 0,
                    files: 0,
                });
                it.size += e.size;
                it.files += 1;
            }
            None => files.push(BrowseItem {
                name: rest.to_string(),
                path: vp.clone(),
                is_dir: false,
                size: e.size,
                files: 1,
            }),
        }
    }
    files.sort_by_key(|a| a.name.to_lowercase());
    let mut out: Vec<BrowseItem> = dirs.into_values().collect();
    out.sort_by_key(|a| a.name.to_lowercase());
    out.extend(files);
    out.truncate(limit);
    Ok(out)
}

#[derive(Debug, Clone)]
pub struct RestoreSummary {
    pub total_files: usize,
    pub elapsed_seconds: f64,
}

struct RestoreBase {
    target: Option<PathBuf>,
    sources: Vec<String>,
    kinds: Vec<String>,
}

/// 별도 폴더 복원 시 원본별 하위 폴더 이름: "001_폴더명" (Windows 에서 쓸 수 없는 문자는 _ 로 바꾼다)
fn source_label(idx: usize, root: &str) -> String {
    let base = Path::new(root)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| root.chars().filter(|c| c.is_alphanumeric()).collect());
    let clean: String = base
        .chars()
        .map(|c| {
            if matches!(c, '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*') {
                '_'
            } else {
                c
            }
        })
        .collect();
    format!("{:03}_{}", idx + 1, clean)
}

fn resolve_restore_path(base: &RestoreBase, entry: &FileEntry) -> Result<PathBuf, String> {
    let rel = Path::new(&entry.path);
    // 매니페스트 경로가 상위 폴더(..)나 절대경로로 빠져나가는 것을 막는다
    if rel
        .components()
        .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        return Err(format!("unsafe path in manifest: {}", entry.path));
    }
    if let Some(t) = &base.target {
        if base.sources.len() > 1 {
            if let Some(i) = entry.src {
                let i = i as usize;
                if let Some(root) = base.sources.get(i) {
                    let label = source_label(i, root);
                    if base.kinds.get(i).map(|k| k == "file").unwrap_or(false) {
                        let name = Path::new(root)
                            .file_name()
                            .map(|n| n.to_os_string())
                            .unwrap_or_default();
                        return Ok(t.join(label).join(name));
                    }
                    return Ok(t.join(label).join(rel));
                }
            }
        }
        return Ok(t.join(rel));
    }
    let idx = entry
        .src
        .ok_or_else(|| format!("no source index for {}", entry.path))? as usize;
    let root = base
        .sources
        .get(idx)
        .ok_or_else(|| format!("source index {} out of range for {}", idx, entry.path))?;
    if base.kinds.get(idx).map(|k| k == "file").unwrap_or(false) {
        Ok(PathBuf::from(root))
    } else {
        Ok(Path::new(root).join(rel))
    }
}

pub fn run_restore(
    opts: &RestoreOptions,
    on_event: &(dyn Fn(Event) + Sync),
    cancel: &AtomicBool,
) -> Result<RestoreSummary, EngineError> {
    let start = Instant::now();
    let cas = CasStorage::new(&opts.repo)?;
    if opts.session_id.is_empty() || opts.session_id.contains(['/', '\\', ':']) {
        return Err(msg(format!("invalid session id: {}", opts.session_id)));
    }
    let manifest = cas.load_manifest(&opts.session_id)?;
    let sources: Vec<String> = match &manifest.sources {
        Some(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect(),
        _ => Vec::new(),
    };
    let kinds: Vec<String> = manifest.source_kinds.clone().unwrap_or_default();

    let base = match &opts.target {
        None => {
            if manifest.format_version.unwrap_or(0) < 2 || sources.is_empty() {
                return Err(msg(
                    "in-place restore is not supported for this snapshot (no per-file source info); restore to a target folder instead",
                ));
            }
            RestoreBase {
                target: None,
                sources,
                kinds,
            }
        }
        Some(t) => {
            fs::create_dir_all(t)?;
            RestoreBase {
                target: Some(t.clone()),
                sources,
                kinds,
            }
        }
    };

    let mut entries = manifest.files();
    if let Some(only) = &opts.only {
        let vbase = RestoreBase {
            target: Some(PathBuf::new()),
            sources: base.sources.clone(),
            kinds: base.kinds.clone(),
        };
        let only: Vec<&str> = only
            .iter()
            .map(|s| s.trim_matches('/'))
            .filter(|s| !s.is_empty())
            .collect();
        entries.retain(|e| {
            virtual_path(&vbase, e).is_some_and(|vp| only.iter().any(|p| is_under(&vp, p)))
        });
    }
    for e in entries.iter().filter(|e| e.is_dir) {
        fs::create_dir_all(resolve_restore_path(&base, e).map_err(msg)?)?;
    }

    let total = entries.iter().filter(|e| !e.is_dir).count();
    let done = AtomicUsize::new(0);
    let failed = AtomicUsize::new(0);
    let errors: Mutex<Vec<String>> = Mutex::new(Vec::new());
    on_event(Event::RestoreProgress { done: 0, total });

    entries.par_iter().filter(|e| !e.is_dir).for_each(|entry| {
        let fail = |why: String| {
            errors.lock().unwrap().push(why);
            failed.fetch_add(1, Ordering::Relaxed);
        };
        if cancel.load(Ordering::Relaxed) {
            return;
        }
        match restore_one(&cas, &base, entry) {
            Ok(()) => {}
            Err(why) => fail(why),
        }
        let d = done.fetch_add(1, Ordering::Relaxed) + 1;
        if d.is_multiple_of(200) || d == total {
            on_event(Event::RestoreProgress { done: d, total });
        }
    });

    if cancel.load(Ordering::Relaxed) {
        return Err(EngineError::Cancelled);
    }
    let failed = failed.load(Ordering::Relaxed);
    if failed > 0 {
        let errs = errors.into_inner().unwrap();
        return Err(msg(format!(
            "{} of {} files failed to restore: {}",
            failed,
            total,
            errs.iter().take(5).cloned().collect::<Vec<_>>().join("; ")
        )));
    }
    Ok(RestoreSummary {
        total_files: total,
        elapsed_seconds: start.elapsed().as_secs_f64(),
    })
}

fn restore_one(cas: &CasStorage, base: &RestoreBase, entry: &FileEntry) -> Result<(), String> {
    let file_path = resolve_restore_path(base, entry)?;
    if let Some(parent) = file_path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    // 내용이 있어야 할 파일(size>0)에 청크 정보가 없으면 Rust 형식이 아닌 매니페스트다.
    // 빈 파일을 만들고 성공으로 끝내면 조용한 데이터 유실이므로 실패로 처리한다.
    if entry.size > 0 && entry.chunks.is_empty() {
        return Err(format!(
            "No chunk info for {} (size {}): not a Rust-format manifest",
            file_path.display(),
            entry.size
        ));
    }
    let mut file = File::create(&file_path)
        .map_err(|e| format!("Cannot create file {}: {}", file_path.display(), e))?;
    for c in &entry.chunks {
        let comp = cas
            .read_blob(&c.hash)
            .map_err(|e| format!("Missing blob {}: {}", c.hash, e))?;
        let raw = cas
            .decode_blob(&comp, &c.hash)
            .map_err(|e| format!("Decompress failed for {}: {}", c.hash, e))?;
        if hasher::hash_bytes(&raw) != c.hash {
            return Err(format!(
                "Hash mismatch for blob {} (corrupt or tampered)",
                c.hash
            ));
        }
        file.write_all(&raw)
            .map_err(|e| format!("Write failed for {}: {}", file_path.display(), e))?;
    }
    // 원래 수정 시각 복원 (실패해도 복원 자체는 성공)
    if entry.modified > 0 {
        let _ = file.set_modified(UNIX_EPOCH + Duration::from_millis(entry.modified));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compressor;
    use std::collections::BTreeMap;

    fn no_events(_: Event) {}

    fn make_tree(root: &Path) {
        fs::create_dir_all(root.join("하위 폴더")).unwrap();
        fs::write(root.join("가나다.txt"), "한글 내용\n".repeat(500)).unwrap();
        let big: Vec<u8> = (0..3_000_000u32)
            .map(|i| (i.wrapping_mul(2654435761) >> 24) as u8)
            .collect();
        fs::write(root.join("하위 폴더").join("big.bin"), big).unwrap();
        fs::write(root.join("empty.txt"), b"").unwrap();
    }

    fn tree_hashes(root: &Path) -> BTreeMap<String, String> {
        let mut m = BTreeMap::new();
        for e in walk(root) {
            let rel = e
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            m.insert(rel, hasher::hash_file(&e).unwrap());
        }
        m
    }

    fn walk(dir: &Path) -> Vec<PathBuf> {
        let mut out = Vec::new();
        for e in fs::read_dir(dir).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                out.extend(walk(&p));
            } else {
                out.push(p);
            }
        }
        out
    }

    fn backup(src: &Path, repo: &Path, sid: &str) -> BackupSummary {
        run_backup(
            &BackupOptions {
                sources: vec![src.to_path_buf()],
                repo: repo.to_path_buf(),
                session_id: Some(sid.into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap()
    }

    fn restore_only(repo: &Path, out: &Path, only: Option<Vec<&str>>) {
        run_restore(
            &RestoreOptions {
                repo: repo.to_path_buf(),
                session_id: "t1".into(),
                target: Some(out.to_path_buf()),
                only: only.map(|v| v.into_iter().map(String::from).collect()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
    }

    #[test]
    fn browse_lists_folders_first_and_searches_by_name() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");

        let root = browse_snapshot(&repo, "t1", "", "", 100).unwrap();
        let names: Vec<(&str, bool)> = root.iter().map(|i| (i.name.as_str(), i.is_dir)).collect();
        assert_eq!(
            names,
            vec![
                ("하위 폴더", true),
                ("empty.txt", false),
                ("가나다.txt", false)
            ]
        );
        assert_eq!(root[0].files, 1);
        assert_eq!(root[0].size, 3_000_000);

        let sub = browse_snapshot(&repo, "t1", "하위 폴더", "", 100).unwrap();
        assert_eq!(sub.len(), 1);
        assert_eq!(sub[0].path, "하위 폴더/big.bin");

        let found = browse_snapshot(&repo, "t1", "", "BIG", 100).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].path, "하위 폴더/big.bin");
        assert_eq!(browse_snapshot(&repo, "t1", "", "", 2).unwrap().len(), 2);
        assert!(browse_snapshot(&repo, "../x", "", "", 5).is_err());
    }

    #[test]
    fn restore_only_selected_files_and_folders() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");

        let one = d.path().join("one");
        restore_only(&repo, &one, Some(vec!["가나다.txt"]));
        assert!(one.join("가나다.txt").is_file());
        assert!(!one.join("하위 폴더").exists() && !one.join("empty.txt").exists());

        let dirsel = d.path().join("dirsel");
        restore_only(&repo, &dirsel, Some(vec!["하위 폴더/"]));
        assert!(dirsel.join("하위 폴더").join("big.bin").is_file());
        assert!(!dirsel.join("가나다.txt").exists());

        // 이름이 비슷해도 폴더 경계를 지킨다("하위" 는 "하위 폴더" 를 고르지 않음), 빈 선택은 아무것도 복원하지 않는다
        let none = d.path().join("none");
        restore_only(&repo, &none, Some(vec!["하위", ""]));
        assert!(!none.join("하위 폴더").exists() && !none.join("가나다.txt").exists());

        let all = d.path().join("all");
        restore_only(&repo, &all, None);
        assert_eq!(tree_hashes(&src), tree_hashes(&all));
    }

    #[test]
    fn browse_and_restore_use_source_labels_for_multiple_sources() {
        let d = tempfile::tempdir().unwrap();
        let (a, b) = (d.path().join("A"), d.path().join("B"));
        fs::create_dir_all(&a).unwrap();
        fs::create_dir_all(&b).unwrap();
        fs::write(a.join("same.txt"), "from A").unwrap();
        fs::write(b.join("same.txt"), "from B").unwrap();
        let repo = d.path().join("repo");
        run_backup(
            &BackupOptions {
                sources: vec![a, b],
                repo: repo.clone(),
                session_id: Some("t1".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        let root = browse_snapshot(&repo, "t1", "", "", 10).unwrap();
        let names: Vec<&str> = root.iter().map(|i| i.name.as_str()).collect();
        assert_eq!(names, vec!["001_A", "002_B"]);
        let out = d.path().join("out");
        restore_only(&repo, &out, Some(vec!["002_B/same.txt"]));
        assert_eq!(
            fs::read_to_string(out.join("002_B").join("same.txt")).unwrap(),
            "from B"
        );
        assert!(!out.join("001_A").exists());
    }

    #[test]
    fn backup_then_restore_to_folder_is_identical() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        let s = backup(&src, &repo, "t1");
        assert!(s.skipped_files.is_empty());
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo: repo.clone(),
                session_id: "t1".into(),
                only: None,
                target: Some(out.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(tree_hashes(&src), tree_hashes(&out));
    }

    #[test]
    fn restore_keeps_modification_time() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let f = src.join("가나다.txt");
        let t = UNIX_EPOCH + Duration::from_secs(1_600_000_000);
        File::options()
            .write(true)
            .open(&f)
            .unwrap()
            .set_modified(t)
            .unwrap();
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t1".into(),
                only: None,
                target: Some(out.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        let got = fs::metadata(out.join("가나다.txt"))
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(got, t);
    }

    #[test]
    fn second_backup_reuses_unchanged_files() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        let first = backup(&src, &repo, "t1");
        assert_eq!(first.new_files, 3);
        fs::write(src.join("가나다.txt"), "바뀐 내용").unwrap();
        let second = backup(&src, &repo, "t2");
        assert_eq!(second.unmodified_files, 2);
        assert_eq!(second.modified_files, 1);
        assert!(second.dedup_bytes > 0);
    }

    #[test]
    fn in_place_restore_recreates_deleted_files() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let before = tree_hashes(&src);
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        fs::remove_dir_all(&src).unwrap();
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t1".into(),
                only: None,
                target: None,
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(before, tree_hashes(&src));
    }

    #[test]
    fn cancelled_backup_writes_no_manifest() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        let r = run_backup(
            &BackupOptions {
                sources: vec![src],
                repo: repo.clone(),
                session_id: Some("t1".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &no_events,
            &AtomicBool::new(true),
        );
        assert!(matches!(r, Err(EngineError::Cancelled)));
        assert!(!repo.join("snapshots").join("t1.json").exists());
    }

    #[test]
    fn tampered_blob_fails_restore() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.txt"), "original data").unwrap();
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        let cas = CasStorage::new(&repo).unwrap();
        let m = cas.load_manifest("t1").unwrap();
        let hash = m.files()[0].chunks[0].hash.clone();
        // 정상 압축본이지만 다른 내용으로 바꿔치기
        fs::write(
            cas.blob_path(&hash),
            compressor::compress(b"evil data!!!!", 3).unwrap(),
        )
        .unwrap();
        let r = run_restore(
            &RestoreOptions {
                repo,
                session_id: "t1".into(),
                only: None,
                target: Some(d.path().join("out")),
            },
            &no_events,
            &AtomicBool::new(false),
        );
        let e = r.unwrap_err().to_string();
        assert!(e.contains("Hash mismatch"), "{}", e);
    }

    #[test]
    fn unsafe_manifest_path_is_rejected() {
        let base = RestoreBase {
            target: Some(PathBuf::from("C:/out")),
            sources: vec![],
            kinds: vec![],
        };
        let e = FileEntry {
            path: "../evil.txt".into(),
            ..Default::default()
        };
        assert!(resolve_restore_path(&base, &e).is_err());
        let e = FileEntry {
            path: "C:/evil.txt".into(),
            ..Default::default()
        };
        assert!(resolve_restore_path(&base, &e).is_err());
    }

    #[test]
    fn invalid_session_id_is_rejected() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let r = run_backup(
            &BackupOptions {
                sources: vec![src],
                repo: d.path().join("repo"),
                session_id: Some("../x".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &no_events,
            &AtomicBool::new(false),
        );
        assert!(r.is_err());
    }

    #[test]
    fn multiple_sources_restore_into_separate_folders() {
        let d = tempfile::tempdir().unwrap();
        let a = d.path().join("A");
        let b = d.path().join("B");
        fs::create_dir_all(&a).unwrap();
        fs::create_dir_all(&b).unwrap();
        fs::write(a.join("same.txt"), "from A").unwrap();
        fs::write(b.join("same.txt"), "from B").unwrap();
        let repo = d.path().join("repo");
        run_backup(
            &BackupOptions {
                sources: vec![a, b],
                repo: repo.clone(),
                session_id: Some("t1".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t1".into(),
                only: None,
                target: Some(out.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(out.join("001_A").join("same.txt")).unwrap(),
            "from A"
        );
        assert_eq!(
            fs::read_to_string(out.join("002_B").join("same.txt")).unwrap(),
            "from B"
        );
    }
}
