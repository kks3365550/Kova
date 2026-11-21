//! 백업·복원 작업을 라이브러리 함수로 제공한다 (서비스가 서브프로세스 없이 직접 호출).
//! - 진행 상황은 콜백(`on_event`)으로 전달하고, 취소는 `cancel` 플래그로 받는다.
//! - 취소되면 매니페스트를 쓰지 않는다(반쪽짜리 스냅샷이 생기지 않는다).
//! - 복원은 청크마다 BLAKE3 해시를 다시 확인한다(변조·손상된 데이터를 조용히 복원하지 않는다).
use crate::storage::{
    manifest_sources, norm_source, valid_session_id, CasStorage, FileChunkInfo, FileEntry,
    SnapshotManifest,
};
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
    /// 기본 제외 규칙(temp/logs/cache 등 이름)으로 건너뛴 폴더(전체 경로)
    pub excluded_dirs: Vec<String>,
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

/// 새 스냅샷 ID: 밀리초까지의 시각 + 난수 4자리(16진). 이미 있으면 다시 만든다.
/// 공유 저장소에서 두 프로파일이 같은 초에 돌아도 앞 스냅샷이 덮어써지지 않는다.
fn new_session_id(snapshots_dir: &Path) -> String {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    loop {
        let now = chrono::Local::now();
        let nanos = now.timestamp_subsec_nanos() as u64;
        let mix = nanos
            ^ ((std::process::id() as u64) << 20)
            ^ SEQ
                .fetch_add(1, Ordering::Relaxed)
                .wrapping_mul(0x9E37_79B9);
        let mix = mix ^ (mix >> 16);
        let id = format!("{}_{:04x}", now.format("%Y%m%d_%H%M%S_%3f"), mix & 0xFFFF);
        if !snapshots_dir.join(format!("{id}.json")).exists() {
            return id;
        }
    }
}

pub fn run_backup(
    opts: &BackupOptions,
    on_event: &(dyn Fn(Event) + Sync),
    cancel: &AtomicBool,
) -> Result<BackupSummary, EngineError> {
    run_backup_with_threads(opts, 0, on_event, cancel)
}

/// `max_threads > 0` 이면 해당 스레드 수의 전용 Rayon 풀 안에서 병렬 백업을 수행한다(CPU 스로틀링).
pub fn run_backup_with_threads(
    opts: &BackupOptions,
    max_threads: usize,
    on_event: &(dyn Fn(Event) + Sync),
    cancel: &AtomicBool,
) -> Result<BackupSummary, EngineError> {
    if max_threads > 0 {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(max_threads)
            .build()
            .map_err(|e| msg(format!("thread pool init failed: {e}")))?;
        return pool.install(|| run_backup_inner(opts, on_event, cancel));
    }
    run_backup_inner(opts, on_event, cancel)
}

fn run_backup_inner(
    opts: &BackupOptions,
    on_event: &(dyn Fn(Event) + Sync),
    cancel: &AtomicBool,
) -> Result<BackupSummary, EngineError> {
    let start_time = Instant::now();
    let sources = &opts.sources;
    if sources.is_empty() {
        return Err(msg("no source paths given"));
    }
    // 세션 ID 는 매니페스트 파일 이름이 되므로 허용 문자만 쓴다
    if let Some(s) = &opts.session_id {
        if !valid_session_id(s) {
            return Err(msg(format!("invalid session id: {}", s)));
        }
    }
    let cas = CasStorage::new(&opts.repo)?;
    let snapshots_dir = opts.repo.join("snapshots");
    let mut sid = match &opts.session_id {
        Some(s) => s.clone(),
        None => new_session_id(&snapshots_dir),
    };
    // 이미 있는 스냅샷을 덮어쓰지 않는다(지정한 ID 가 겹치면 시작 전에 거부)
    if snapshots_dir.join(format!("{sid}.json")).exists() {
        return Err(msg(format!("snapshot id already exists: {sid}")));
    }

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
    let scanned: Vec<io::Result<(Vec<scanner::ScannedItem>, scanner::ScanReport)>> = scan_roots
        .par_iter()
        .enumerate()
        .map(|(idx, src)| {
            let (mut v, rep) = scanner::scan_directory_report(src, &exclude_filter)?;
            for it in v.iter_mut() {
                it.src = idx as u32;
            }
            Ok((v, rep))
        })
        .collect();
    let mut items: Vec<scanner::ScannedItem> = Vec::new();
    let mut scan_errors: Vec<String> = Vec::new();
    let mut excluded_dirs: Vec<String> = Vec::new();
    for (i, r) in scanned.into_iter().enumerate() {
        // 원본 자체를 읽지 못하면(접근 거부 등) 빈 스냅샷을 "성공"으로 남기지 않고 실패한다
        let (v, rep) = r.map_err(|e| {
            msg(format!(
                "원본 경로를 읽을 수 없음: {} ({})",
                sources[i].display(),
                e
            ))
        })?;
        items.extend(v);
        let label = |p: String| {
            sources[i]
                .join(p.replace('/', std::path::MAIN_SEPARATOR_STR))
                .display()
                .to_string()
        };
        scan_errors.extend(rep.errors.into_iter().map(|e| format!("{e} (scan)")));
        excluded_dirs.extend(rep.default_excluded.into_iter().map(label));
    }
    if !excluded_dirs.is_empty() {
        on_event(Event::Phase {
            phase: "scanning",
            message: format!(
                "기본 제외 폴더 {}개를 건너뜀(temp/logs/cache 등): {}",
                excluded_dirs.len(),
                excluded_dirs
                    .iter()
                    .take(5)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        });
    }
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
    // 스캔 중 읽지 못한 항목(접근 거부 등)도 빠진 파일로 집계한다
    let skipped: Mutex<Vec<String>> = Mutex::new(scan_errors);

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

    // 증분 빠른 경로 지도: 원본 경로 집합이 같고 같은 프로파일(또는 프로파일 기록이 없는 구형)인
    // 가장 최근 스냅샷만 기준으로 쓴다. 키는 (원본 경로, 상대 경로)라 원본 목록 순서가 바뀌어도
    // 다른 원본의 청크를 재사용하지 않는다.
    let src_strs: Vec<String> = sources
        .iter()
        .map(|s| s.to_string_lossy().to_string())
        .collect();
    let src_keys: Vec<String> = src_strs.iter().map(|s| norm_source(s)).collect();
    let file_key =
        |src: &str, rel: &str| format!("{}|{}", src, rel.replace('\\', "/").to_lowercase());
    let mut prev_files_map: HashMap<String, FileEntry> = HashMap::new();
    if let Some(manifest) = cas.baseline_manifest(opts.profile_id.as_deref(), &src_strs) {
        let prev_srcs: Vec<String> = manifest_sources(&manifest)
            .iter()
            .map(|s| norm_source(s))
            .collect();
        for entry in manifest.files() {
            // 구형 매니페스트의 blob_id/sha256(청크 정보 없음)은 해시 종류를 알 수 없으므로 재사용하지 않고 다시 청킹한다
            if entry.is_dir || (entry.chunks.is_empty() && entry.size > 0) {
                continue;
            }
            // 원본 번호가 없는 구형 항목은 원본이 하나일 때만 어느 원본인지 확정할 수 있다
            let src = match entry.src {
                Some(i) => prev_srcs.get(i as usize),
                None if prev_srcs.len() == 1 => prev_srcs.first(),
                None => None,
            };
            let Some(src) = src else {
                continue;
            };
            let key = file_key(src, &entry.path);
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
            let key = file_key(&src_keys[item.src as usize], &item.rel_path);
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

            // 큰 파일도 통째로 메모리에 올리지 않도록 청크 묶음 단위로 읽어 바로 압축·저장한다
            let mut file_chunks: Vec<FileChunkInfo> = Vec::new();
            let mut file_size: u64 = 0;
            let streamed = chunker::chunk_file_batches(&item.path, |chunks_raw| {
                file_size += chunks_raw.iter().map(|c| c.length as u64).sum::<u64>();
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
                        cas.stage_blob(&c.hash, compressed)
                            .map_err(|e| format!("save blob: {}", e))?;
                        Ok(FileChunkInfo {
                            hash: c.hash,
                            offset: c.offset,
                            length: c.length,
                            compressed_length: comp_len,
                        })
                    })
                    .collect();
                file_chunks.extend(stored?);
                Ok(())
            });
            if let Err(why) = streamed {
                return skip(why);
            }
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

    // 모아 둔 새 블롭을 팩으로 모두 쓴 뒤에야 매니페스트를 쓴다(취소돼도 쓴 만큼은 남겨 이어 하기가 빠르다)
    let flushed = cas.flush_staged();

    // 취소되면 매니페스트를 쓰지 않는다
    if cancel.load(Ordering::Relaxed) {
        return Err(EngineError::Cancelled);
    }
    flushed?;

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

    let mut manifest = SnapshotManifest {
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
    // 같은 ID 가 그 사이에 생겼으면(동시 실행) 자동 ID 는 새로 만들어 다시 쓰고, 지정 ID 는 거부한다
    loop {
        match cas.save_manifest(&manifest) {
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists && opts.session_id.is_none() => {
                sid = new_session_id(&snapshots_dir);
                manifest.id = Some(sid.clone());
                manifest.session_id = Some(sid.clone());
            }
            r => break r?,
        }
    }

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
        excluded_dirs,
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
    if !valid_session_id(session_id) {
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
    if !valid_session_id(&opts.session_id) {
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

/// 블롭을 읽고 풀어서 해시까지 검증한 평문을 돌려준다.
fn load_chunk(cas: &CasStorage, c: &crate::storage::FileChunkInfo) -> Result<Vec<u8>, String> {
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
    Ok(raw)
}

/// 파일 위치를 지정해 쓴다(핸들을 공유하는 여러 스레드가 서로 다른 위치에 동시에 써도 안전하다).
fn write_at(f: &File, buf: &[u8], offset: u64) -> io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileExt;
        let mut done = 0usize;
        while done < buf.len() {
            let n = f.seek_write(&buf[done..], offset + done as u64)?;
            if n == 0 {
                return Err(io::Error::new(io::ErrorKind::WriteZero, "쓰기 0바이트"));
            }
            done += n;
        }
        Ok(())
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileExt;
        f.write_all_at(buf, offset)
    }
}

fn restore_one(cas: &CasStorage, base: &RestoreBase, entry: &FileEntry) -> Result<(), String> {
    let file_path = resolve_restore_path(base, entry)?;
    // 내용이 있어야 할 파일(size>0)에 청크 정보가 없으면 Rust 형식이 아닌 매니페스트다.
    // 빈 파일을 만들고 성공으로 끝내면 조용한 데이터 유실이므로 실패로 처리한다.
    if entry.size > 0 && entry.chunks.is_empty() {
        return Err(format!(
            "No chunk info for {} (size {}): not a Rust-format manifest",
            file_path.display(),
            entry.size
        ));
    }
    // 기존 파일(원위치 복원의 원본)을 먼저 비우지 않는다: 같은 폴더의 임시 파일에 쓰고 모든 청크를
    // 검증한 뒤에만 이름 바꾸기로 바꿔 놓는다. 실패하면 임시 파일만 지우고 원본은 그대로 둔다.
    static TMP_SEQ: AtomicU64 = AtomicU64::new(0);
    let tmp_name = format!(
        ".{}.kova-restore-{}-{}.tmp",
        file_path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default(),
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed)
    );
    let tmp_path = file_path.with_file_name(tmp_name);
    let result = write_restored(cas, entry, &file_path, &tmp_path).and_then(|()| {
        fs::rename(&tmp_path, &file_path)
            .map_err(|e| format!("Cannot replace file {}: {}", file_path.display(), e))
    });
    if result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    result
}

/// 복원 내용을 `tmp_path` 에 쓰고 청크 해시·길이·전체 크기를 검증한다(성공하면 파일은 닫힌 상태).
fn write_restored(
    cas: &CasStorage,
    entry: &FileEntry,
    file_path: &Path,
    tmp_path: &Path,
) -> Result<(), String> {
    // 폴더는 복원 시작 때 이미 만들어 두었으니, 없을 때(일부만 복원 등)에만 만든다. 파일마다 폴더를 확인하면
    // 같은 폴더에 동시에 접근하는 스레드끼리 서로 기다려 크게 느려진다
    let mut file = match File::create(tmp_path) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            if let Some(parent) = tmp_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            File::create(tmp_path)
                .map_err(|e| format!("Cannot create file {}: {}", file_path.display(), e))?
        }
        Err(e) => return Err(format!("Cannot create file {}: {}", file_path.display(), e)),
    };
    // 청크가 여럿이고 길이 합이 파일 크기와 맞으면 청크를 묶음 단위로 병렬 복원한다(큰 파일 하나도 여러 코어 사용)
    let total_len: u64 = entry.chunks.iter().map(|c| c.length as u64).sum();
    if entry.chunks.len() >= 4 && total_len == entry.size && file.set_len(entry.size).is_ok() {
        let mut offs = Vec::with_capacity(entry.chunks.len());
        let mut off = 0u64;
        for c in &entry.chunks {
            offs.push(off);
            off += c.length as u64;
        }
        // 묶음마다 끝나야 다음 묶음을 시작하므로 쓰기 위치가 크게 앞서 나가지 않는다
        // (파일 끝보다 멀리 쓰면 NTFS가 그 앞 구간을 0으로 채우느라 느려진다)
        const BATCH: usize = 64;
        for (ci, co) in entry.chunks.chunks(BATCH).zip(offs.chunks(BATCH)) {
            ci.par_iter().zip(co.par_iter()).try_for_each(|(c, &o)| {
                let raw = load_chunk(cas, c)?;
                if raw.len() != c.length {
                    return Err(format!("Chunk length mismatch for blob {}", c.hash));
                }
                write_at(&file, &raw, o)
                    .map_err(|e| format!("Write failed for {}: {}", file_path.display(), e))
            })?;
        }
    } else {
        let mut written = 0u64;
        for c in &entry.chunks {
            let raw = load_chunk(cas, c)?;
            if c.length != 0 && raw.len() != c.length {
                return Err(format!("Chunk length mismatch for blob {}", c.hash));
            }
            file.write_all(&raw)
                .map_err(|e| format!("Write failed for {}: {}", file_path.display(), e))?;
            written += raw.len() as u64;
        }
        if written != entry.size {
            return Err(format!(
                "Size mismatch for {}: restored {} bytes, expected {}",
                file_path.display(),
                written,
                entry.size
            ));
        }
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

    fn noise(len: usize, seed: u64) -> Vec<u8> {
        let mut x = seed | 1;
        (0..len)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x >> 24) as u8
            })
            .collect()
    }

    #[test]
    fn large_multi_chunk_files_roundtrip_and_dedup() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        // 청크 수십 개짜리 파일 + 내용이 같은 복사본(같은 청크를 동시에 저장) + 작은 파일
        let big = noise(40 * 1024 * 1024, 7);
        fs::write(src.join("big.bin"), &big).unwrap();
        fs::write(src.join("big_copy.bin"), &big).unwrap();
        fs::write(src.join("other.bin"), noise(9 * 1024 * 1024, 99)).unwrap();
        fs::write(src.join("tiny.txt"), "작은 파일").unwrap();
        let repo = d.path().join("repo");
        let first = backup(&src, &repo, "t1");
        assert!(first.skipped_files.is_empty());

        let out = d.path().join("out");
        restore_only(&repo, &out, None);
        assert_eq!(tree_hashes(&src), tree_hashes(&out));

        // 큰 파일 한가운데를 조금만 바꾸면 대부분의 청크를 다시 쓰지 않는다
        let mut edited = big.clone();
        for b in &mut edited[20 * 1024 * 1024..20 * 1024 * 1024 + 100] {
            *b ^= 0xFF;
        }
        fs::write(src.join("big.bin"), &edited).unwrap();
        let second = backup(&src, &repo, "t2");
        assert_eq!(second.modified_files, 1);
        assert!(
            second.dedup_bytes > 30 * 1024 * 1024,
            "dedup {}",
            second.dedup_bytes
        );
        let out2 = d.path().join("out2");
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t2".into(),
                only: None,
                target: Some(out2.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(tree_hashes(&src), tree_hashes(&out2));
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
        // 정상 압축본이지만 다른 내용으로 바꿔치기. 낱개 파일만 남도록 팩은 지운다(읽을 때는 팩이 먼저다)
        fs::remove_dir_all(repo.join("packs")).unwrap();
        fs::create_dir_all(cas.blob_path(&hash).parent().unwrap()).unwrap();
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

    /// 팩 안의 바이트가 망가지면 복원이 조용히 넘어가지 않고 실패한다.
    #[test]
    fn corrupted_pack_fails_restore() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.bin"), noise(300_000, 5)).unwrap();
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        let ids = crate::pack::pack_ids(&repo);
        assert_eq!(ids.len(), 1, "백업이 팩을 만들지 않음");
        let pf = crate::pack::pack_file(&repo, &ids[0]);
        let mut bytes = fs::read(&pf).unwrap();
        for b in bytes.iter_mut().skip(10).take(50) {
            *b ^= 0xFF;
        }
        fs::write(&pf, bytes).unwrap();
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
        assert!(r.is_err(), "망가진 팩인데 복원이 성공함");
    }

    /// 청크가 많아 병렬로 복원되는 큰 파일에서도 변조된 청크는 복원 실패로 잡힌다.
    #[test]
    fn tampered_chunk_in_parallel_restore_fails() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("big.bin"), noise(24 * 1024 * 1024, 11)).unwrap();
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        let ids = crate::pack::pack_ids(&repo);
        let pf = crate::pack::pack_file(&repo, ids.last().unwrap());
        let mut bytes = fs::read(&pf).unwrap();
        let mid = bytes.len() / 2;
        for b in &mut bytes[mid..mid + 64] {
            *b ^= 0xFF;
        }
        fs::write(&pf, bytes).unwrap();
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
        assert!(r.is_err(), "변조된 청크인데 복원이 성공함");
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

    fn opts(
        sources: Vec<PathBuf>,
        repo: &Path,
        sid: Option<&str>,
        pid: Option<&str>,
    ) -> BackupOptions {
        BackupOptions {
            sources,
            repo: repo.to_path_buf(),
            session_id: sid.map(String::from),
            zstd_level: 3,
            exclude: vec![],
            profile_id: pid.map(String::from),
            scan_from: None,
        }
    }

    fn run(o: &BackupOptions) -> BackupSummary {
        run_backup(o, &no_events, &AtomicBool::new(false)).unwrap()
    }

    fn set_mtime(p: &Path, secs: u64) {
        File::options()
            .write(true)
            .open(p)
            .unwrap()
            .set_modified(UNIX_EPOCH + Duration::from_secs(secs))
            .unwrap();
    }

    /// 원위치 복원이 실패하면(변조된 블롭) 현재 파일을 자르거나 바꾸지 않고, 임시 파일도 남기지 않는다.
    #[test]
    fn failed_in_place_restore_keeps_original_file() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        let f = src.join("doc.txt");
        fs::write(&f, "backed up version").unwrap();
        let repo = d.path().join("repo");
        backup(&src, &repo, "t1");
        fs::write(&f, "current precious version").unwrap();
        let cas = CasStorage::new(&repo).unwrap();
        let hash = cas.load_manifest("t1").unwrap().files()[0].chunks[0]
            .hash
            .clone();
        fs::remove_dir_all(repo.join("packs")).unwrap();
        fs::create_dir_all(cas.blob_path(&hash).parent().unwrap()).unwrap();
        fs::write(
            cas.blob_path(&hash),
            compressor::compress(b"evil data", 3).unwrap(),
        )
        .unwrap();
        let r = run_restore(
            &RestoreOptions {
                repo: repo.clone(),
                session_id: "t1".into(),
                only: None,
                target: None,
            },
            &no_events,
            &AtomicBool::new(false),
        );
        assert!(r.is_err());
        assert_eq!(fs::read_to_string(&f).unwrap(), "current precious version");
        let names: Vec<String> = fs::read_dir(&src)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec!["doc.txt"], "임시 파일이 남음");
        // 블롭이 정상이면 원위치 복원이 기존 파일을 백업 내용으로 바꾼다
        fs::remove_file(cas.blob_path(&hash)).unwrap();
        let o = opts(vec![src.clone()], &repo, Some("t2"), None);
        fs::write(&f, "backed up version").unwrap();
        run(&o);
        fs::write(&f, "changed again").unwrap();
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t2".into(),
                only: None,
                target: None,
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(fs::read_to_string(&f).unwrap(), "backed up version");
    }

    /// 자동 ID 는 같은 초에 연달아 돌아도 겹치지 않고, 이미 있는 ID 를 지정하면 덮어쓰지 않고 거부한다.
    #[test]
    fn snapshot_ids_never_collide_or_overwrite() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        make_tree(&src);
        let repo = d.path().join("repo");
        let o = opts(vec![src.clone()], &repo, None, Some("p1"));
        let ids: Vec<String> = (0..3).map(|_| run(&o).session_id).collect();
        assert!(
            ids[0] != ids[1] && ids[1] != ids[2] && ids[0] != ids[2],
            "{ids:?}"
        );
        for id in &ids {
            assert!(valid_session_id(id), "{id}");
            assert!(repo.join("snapshots").join(format!("{id}.json")).is_file());
        }
        let before = fs::read(repo.join("snapshots").join(format!("{}.json", ids[0]))).unwrap();
        let dup = opts(vec![src], &repo, Some(&ids[0]), Some("p2"));
        assert!(run_backup(&dup, &no_events, &AtomicBool::new(false)).is_err());
        let after = fs::read(repo.join("snapshots").join(format!("{}.json", ids[0]))).unwrap();
        assert_eq!(before, after, "기존 스냅샷이 바뀜");
        // 저장 계층도 같은 ID 덮어쓰기를 거부한다
        let cas = CasStorage::new(&repo).unwrap();
        let m = cas.load_manifest(&ids[0]).unwrap();
        let e = cas.save_manifest(&m).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::AlreadyExists);
        assert!(!valid_session_id("a/b") && !valid_session_id("..") && !valid_session_id("x:y"));
    }

    /// 원본 순서를 바꿔도(같은 상대경로·크기·수정시각) 다른 원본의 청크를 재사용하지 않는다.
    #[test]
    fn incremental_key_uses_source_path_not_index() {
        let d = tempfile::tempdir().unwrap();
        let (a, b) = (d.path().join("A"), d.path().join("B"));
        fs::create_dir_all(&a).unwrap();
        fs::create_dir_all(&b).unwrap();
        fs::write(a.join("same.txt"), "AAAA").unwrap();
        fs::write(b.join("same.txt"), "BBBB").unwrap();
        set_mtime(&a.join("same.txt"), 1_700_000_000);
        set_mtime(&b.join("same.txt"), 1_700_000_000);
        let repo = d.path().join("repo");
        run(&opts(
            vec![a.clone(), b.clone()],
            &repo,
            Some("t1"),
            Some("p"),
        ));
        let s = run(&opts(vec![b, a], &repo, Some("t2"), Some("p")));
        assert_eq!(s.unmodified_files, 2, "원본 경로가 같으면 재사용해야 함");
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo,
                session_id: "t2".into(),
                only: None,
                target: Some(out.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(out.join("001_B/same.txt")).unwrap(),
            "BBBB"
        );
        assert_eq!(
            fs::read_to_string(out.join("002_A/same.txt")).unwrap(),
            "AAAA"
        );
    }

    /// 프로파일이 없으면 원본 경로가 같은 스냅샷만 기준으로 쓰고, 프로파일이 있으면
    /// 업그레이드 전(프로파일 기록 없음) 스냅샷도 원본이 같으면 기준으로 인정한다.
    #[test]
    fn baseline_selection_by_sources_and_legacy_profile() {
        let d = tempfile::tempdir().unwrap();
        let (x, y) = (d.path().join("X"), d.path().join("Y"));
        fs::create_dir_all(&x).unwrap();
        fs::create_dir_all(&y).unwrap();
        fs::write(x.join("f.txt"), "xxxx").unwrap();
        fs::write(y.join("f.txt"), "yyyy").unwrap();
        set_mtime(&x.join("f.txt"), 1_700_000_000);
        set_mtime(&y.join("f.txt"), 1_700_000_000);
        let repo = d.path().join("repo");
        run(&opts(vec![x.clone()], &repo, Some("t1"), None));
        // 원본이 다르면 기준 없음 → 새 파일로 다시 읽는다
        let s = run(&opts(vec![y.clone()], &repo, Some("t2"), None));
        assert_eq!((s.unmodified_files, s.new_files), (0, 1));
        let out = d.path().join("out");
        run_restore(
            &RestoreOptions {
                repo: repo.clone(),
                session_id: "t2".into(),
                only: None,
                target: Some(out.clone()),
            },
            &no_events,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(fs::read_to_string(out.join("f.txt")).unwrap(), "yyyy");
        // 업그레이드 후 첫 백업(프로파일 있음): 프로파일 없는 구형 스냅샷(t1, 같은 원본)을 기준으로 쓴다
        let s = run(&opts(vec![x.clone()], &repo, Some("t3"), Some("p1")));
        assert_eq!(s.unmodified_files, 1);
        // 다른 프로파일의 스냅샷은 원본이 같아도 기준으로 쓰지 않는다
        let s = run(&opts(vec![x], &repo, Some("t4"), Some("p2")));
        assert_eq!(s.unmodified_files, 1, "구형 t1 은 여전히 인정");
        let cas = CasStorage::new(&repo).unwrap();
        let src_x = vec![d.path().join("X").to_string_lossy().to_string()];
        let base = cas.baseline_manifest(Some("p1"), &src_x).unwrap();
        assert!(matches!(base.profile_id.as_deref(), Some("p1") | None));
        assert_ne!(base.session_id(), "t4");
    }

    /// 청크 정보 없이 blob_id/sha256 만 있는 구형 매니페스트는 빠른 경로로 재사용하지 않고 다시 청킹한다.
    #[test]
    fn legacy_blob_id_entries_are_rechunked() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.txt"), "legacy").unwrap();
        set_mtime(&src.join("a.txt"), 1_700_000_000);
        let repo = d.path().join("repo");
        let cas = CasStorage::new(&repo).unwrap();
        // 같은 이름의 블롭이 저장소에 있어도(해시 종류 미상) 쓰지 않는다
        let fake = "ab".repeat(32);
        cas.save_blob(&fake, &compressor::compress(b"wrong!", 3).unwrap())
            .unwrap();
        fs::write(
            repo.join("snapshots").join("old.json"),
            format!(
                r#"{{"id":"old","sources":[{src:?}],"files":[{{"path":"a.txt","size":6,"mtime":1700000000,"blob_id":"{fake}"}}]}}"#,
                src = src.to_string_lossy()
            ),
        )
        .unwrap();
        let s = run(&opts(vec![src.clone()], &repo, Some("t1"), None));
        assert_eq!((s.unmodified_files, s.new_files + s.modified_files), (0, 1));
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
        assert_eq!(fs::read_to_string(out.join("a.txt")).unwrap(), "legacy");
    }

    /// 해시가 16진수가 아닌 손상 매니페스트는 패닉 없이 복원 실패로 끝난다.
    #[test]
    fn non_hex_hash_in_manifest_fails_without_panic() {
        let d = tempfile::tempdir().unwrap();
        let repo = d.path().join("repo");
        let cas = CasStorage::new(&repo).unwrap();
        assert!(cas.save_blob("한글해시", b"x").is_err());
        assert!(!cas.has_blob("..\\..\\x"));
        fs::write(
            repo.join("snapshots").join("bad.json"),
            r#"{"id":"bad","files":[{"path":"a.txt","size":3,"chunks":[{"hash":"한글","length":3}]}]}"#,
        )
        .unwrap();
        let r = run_restore(
            &RestoreOptions {
                repo,
                session_id: "bad".into(),
                only: None,
                target: Some(d.path().join("out")),
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
