mod baremetal;
mod chunker;
mod compressor;
mod hasher;
mod scanner;
mod storage;

use clap::{Parser, Subcommand};
use rayon::prelude::*;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::Instant;

use storage::{CasStorage, FileChunkInfo, FileEntry, SnapshotManifest};

#[derive(Parser)]
#[command(name = "kova_fast_engine")]
#[command(about = "Kova Ultra-Fast Native Backup Engine (Rust + FastCDC + Zstd + BLAKE3)", version = "0.1.0")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Perform full or incremental backup of source directories into CAS repository
    Backup {
        #[arg(short, long, num_args = 1..)]
        source: Vec<PathBuf>,

        #[arg(short, long)]
        repo: PathBuf,

        #[arg(long)]
        session_id: Option<String>,

        #[arg(long, default_value_t = 3)]
        zstd_level: i32,

        #[arg(long)]
        json: bool,
    },
    /// Restore files from a specific snapshot session
    Restore {
        #[arg(short, long)]
        session_id: String,

        #[arg(short, long)]
        repo: PathBuf,

        #[arg(short, long)]
        target: PathBuf,
    },
    /// Run in-memory pipeline throughput benchmark (FastCDC + BLAKE3 + Zstd)
    Benchmark {
        #[arg(long, default_value_t = 256)]
        size_mb: usize,
    },
    /// High-performance Windows Direct I/O and IOCP baremetal block clone
    BaremetalClone {
        #[arg(long)]
        source: String,

        #[arg(long)]
        target: String,

        #[arg(long, default_value_t = 0)]
        start_offset: u64,

        #[arg(long)]
        total_bytes: u64,

        #[arg(long, default_value_t = 32)]
        queue_depth: usize,

        #[arg(long, default_value_t = 16)]
        chunk_mb: usize,

        #[arg(long)]
        json: bool,
    },
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Backup {
            source,
            repo,
            session_id,
            zstd_level,
            json,
        } => run_backup(&source, &repo, session_id, zstd_level, json)?,

        Commands::Restore {
            session_id,
            repo,
            target,
        } => run_restore(&session_id, &repo, &target)?,

        Commands::Benchmark { size_mb } => run_benchmark(size_mb)?,

        Commands::BaremetalClone {
            source,
            target,
            start_offset,
            total_bytes,
            queue_depth,
            chunk_mb,
            json,
        } => baremetal::run_baremetal_clone(
            &source,
            &target,
            start_offset,
            total_bytes,
            queue_depth,
            chunk_mb,
            json,
        )?,
    }

    Ok(())
}

fn run_backup(
    sources: &[PathBuf],
    repo: &Path,
    session_id: Option<String>,
    zstd_level: i32,
    json_mode: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let start_time = Instant::now();
    let sid = session_id.unwrap_or_else(|| {
        chrono::Local::now().format("%Y%m%d_%H%M%S").to_string()
    });

    if !json_mode {
        println!("[KOVA-FAST] Initializing backup session: {}", sid);
        for s in sources {
            println!("[KOVA-FAST] Source: {}", s.display());
        }
        println!("[KOVA-FAST] Repository: {}", repo.display());
    }

    let cas = CasStorage::new(repo)?;

    if !json_mode {
        println!("[KOVA-FAST] Scanning source tree in parallel...");
    }
    let scan_start = Instant::now();
    let mut items = Vec::new();
    for src in sources {
        items.extend(scanner::scan_directory(src));
    }
    let scan_elapsed = scan_start.elapsed().as_secs_f64();
    if !json_mode {
        println!(
            "[KOVA-FAST] Found {} items in {:.3}s",
            items.len(),
            scan_elapsed
        );
    }

    let total_bytes = AtomicU64::new(0);
    let dedup_bytes = AtomicU64::new(0);
    let compressed_bytes = AtomicU64::new(0);
    let processed_files = AtomicUsize::new(0);
    let new_files = AtomicUsize::new(0);
    let modified_files = AtomicUsize::new(0);
    let unmodified_files = AtomicUsize::new(0);

    // Load latest snapshot manifest to build incremental fast-path map
    use std::collections::HashMap;
    let mut prev_files_map: HashMap<String, FileEntry> = HashMap::new();
    let recent_manifests = cas.get_recent_manifests(5);
    for manifest in recent_manifests {
        let prev_sid = manifest.session_id();
        let prev_files = manifest.files();
        if !json_mode {
            println!(
                "[KOVA-FAST] Loaded base snapshot for incremental fast-path: {} ({} files)",
                prev_sid,
                prev_files.len()
            );
        }
        for mut entry in prev_files {
            if !entry.is_dir {
                if entry.chunks.is_empty() {
                    let hash_opt = entry.blob_id.as_ref().or(entry.sha256.as_ref());
                    if let Some(bid) = hash_opt {
                        entry.chunks.push(FileChunkInfo {
                            hash: bid.clone(),
                            offset: 0,
                            length: entry.size as usize,
                            compressed_length: 0,
                        });
                    }
                }
                let norm_path = entry.path.replace('\\', "/");
                prev_files_map.entry(norm_path).or_insert(entry);
            }
        }
    }

    // Filter directories and files
    let (dirs, files): (Vec<_>, Vec<_>) = items.into_iter().partition(|i| i.is_dir);
    let total_file_count = files.len();

    // Process files in parallel with Rayon
    let file_entries: Vec<FileEntry> = files
        .into_par_iter()
        .filter_map(|item| {
            // Incremental Fast-Path Check:
            // If file path, size, and modified time match previous snapshot,
            // skip reading from disk completely and reuse existing chunk metadata!
            let norm_rel = item.rel_path.replace('\\', "/");
            if let Some(prev) = prev_files_map.get(&norm_rel) {
                let mtime_diff = if prev.modified > item.modified {
                    prev.modified - item.modified
                } else {
                    item.modified - prev.modified
                };
                let mtime_match = if mtime_diff <= 5 {
                    true
                } else if prev.modified % 1000 == 0 && (item.modified / 1000) == (prev.modified / 1000) {
                    true
                } else {
                    false
                };

                if prev.size == item.size && mtime_match && !prev.chunks.is_empty() {
                    total_bytes.fetch_add(prev.size, Ordering::Relaxed);
                    dedup_bytes.fetch_add(prev.size, Ordering::Relaxed);
                    unmodified_files.fetch_add(1, Ordering::Relaxed);

                    let done = processed_files.fetch_add(1, Ordering::Relaxed) + 1;
                    if json_mode && (done % 100 == 0 || done == total_file_count) {
                        let cur_bytes = total_bytes.load(Ordering::Relaxed);
                        let pct = if total_file_count > 0 {
                            (done as f64 / total_file_count as f64) * 100.0
                        } else {
                            100.0
                        };
                        let elapsed = start_time.elapsed().as_secs_f64();
                        let cur_speed = (cur_bytes as f64 / (1024.0 * 1024.0)) / elapsed.max(0.001);
                        println!(
                            "{}",
                            serde_json::json!({
                                "type": "progress",
                                "status": "running",
                                "percent": pct.round() as u64,
                                "processed_files": done,
                                "total_files": total_file_count,
                                "processed_size": cur_bytes,
                                "speed": cur_speed,
                                "current_file": item.rel_path,
                                "new_files": new_files.load(Ordering::Relaxed),
                                "modified_files": modified_files.load(Ordering::Relaxed),
                                "unmodified_files": unmodified_files.load(Ordering::Relaxed)
                            })
                        );
                    }

                    return Some(FileEntry {
                        path: item.rel_path,
                        size: prev.size,
                        modified: item.modified,
                        is_dir: false,
                        chunks: prev.chunks.clone(),
                        blob_id: prev.blob_id.clone(),
                        sha256: prev.sha256.clone(),
                    });
                } else {
                    modified_files.fetch_add(1, Ordering::Relaxed);
                }
            } else {
                new_files.fetch_add(1, Ordering::Relaxed);
            }

            let chunks_raw = match chunker::chunk_file(&item.path) {
                Ok(c) => c,
                Err(e) => {
                    if !json_mode {
                        eprintln!("[WARN] Failed to read {}: {}", item.path.display(), e);
                    }
                    return None;
                }
            };

            let mut file_chunks = Vec::new();
            let mut file_size: u64 = 0;

            for c in chunks_raw {
                file_size += c.length as u64;
                total_bytes.fetch_add(c.length as u64, Ordering::Relaxed);

                // Check if blob already exists in CAS repository
                if cas.has_blob(&c.hash) {
                    dedup_bytes.fetch_add(c.length as u64, Ordering::Relaxed);
                    file_chunks.push(FileChunkInfo {
                        hash: c.hash,
                        offset: c.offset,
                        length: c.length,
                        compressed_length: 0, // Deduplicated
                    });
                } else {
                    // Compress and save blob
                    let compressed = match compressor::compress(&c.data, zstd_level) {
                        Ok(data) => data,
                        Err(e) => {
                            if !json_mode {
                                eprintln!("[ERROR] Compression failed for chunk {}: {}", c.hash, e);
                            }
                            return None;
                        }
                    };

                    let comp_len = compressed.len();
                    compressed_bytes.fetch_add(comp_len as u64, Ordering::Relaxed);

                    if let Err(e) = cas.save_blob(&c.hash, &compressed) {
                        if !json_mode {
                            eprintln!("[ERROR] Failed to save blob {}: {}", c.hash, e);
                        }
                        return None;
                    }

                    file_chunks.push(FileChunkInfo {
                        hash: c.hash,
                        offset: c.offset,
                        length: c.length,
                        compressed_length: comp_len,
                    });
                }
            }

            let done = processed_files.fetch_add(1, Ordering::Relaxed) + 1;
            if json_mode && (done % 50 == 0 || done == total_file_count) {
                let cur_bytes = total_bytes.load(Ordering::Relaxed);
                let pct = if total_file_count > 0 {
                    (done as f64 / total_file_count as f64) * 100.0
                } else {
                    100.0
                };
                let elapsed = start_time.elapsed().as_secs_f64();
                let cur_speed = (cur_bytes as f64 / (1024.0 * 1024.0)) / elapsed.max(0.001);
                println!(
                    "{}",
                    serde_json::json!({
                        "type": "progress",
                        "status": "running",
                        "percent": pct.round() as u64,
                        "processed_files": done,
                        "total_files": total_file_count,
                        "processed_size": cur_bytes,
                        "speed": cur_speed,
                        "current_file": item.rel_path,
                        "new_files": new_files.load(Ordering::Relaxed),
                        "modified_files": modified_files.load(Ordering::Relaxed),
                        "unmodified_files": unmodified_files.load(Ordering::Relaxed)
                    })
                );
            }

            Some(FileEntry {
                path: item.rel_path,
                size: file_size,
                modified: item.modified,
                is_dir: false,
                chunks: file_chunks,
                blob_id: None,
                sha256: None,
            })
        })
        .collect();

    // Add directory entries
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
        });
    }

    let tot_b = total_bytes.load(Ordering::Relaxed);
    let ded_b = dedup_bytes.load(Ordering::Relaxed);
    let cmp_b = compressed_bytes.load(Ordering::Relaxed);
    let total_elapsed = start_time.elapsed().as_secs_f64();
    let throughput_mb = (tot_b as f64 / (1024.0 * 1024.0)) / total_elapsed.max(0.001);

    let src_str = sources
        .iter()
        .map(|s| s.to_string_lossy().to_string())
        .collect::<Vec<_>>()
        .join(";");

    let manifest = SnapshotManifest {
        id: Some(sid.clone()),
        session_id: Some(sid.clone()),
        created_at: Some(serde_json::Value::String(chrono::Local::now().to_rfc3339())),
        source_root: Some(src_str),
        sources: None,
        total_files: all_entries.len(),
        total_bytes: tot_b,
        dedup_bytes: ded_b,
        compressed_bytes: cmp_b,
        files: Some(all_entries),
        entries: None,
    };

    cas.save_manifest(&manifest)?;

    if json_mode {
        println!(
            "{}",
            serde_json::json!({
                "type": "summary",
                "session_id": sid,
                "total_files": manifest.total_files,
                "total_bytes": tot_b,
                "dedup_bytes": ded_b,
                "compressed_bytes": cmp_b,
                "elapsed_seconds": total_elapsed,
                "throughput_mb": throughput_mb,
                "new_files": new_files.load(Ordering::Relaxed),
                "modified_files": modified_files.load(Ordering::Relaxed),
                "unmodified_files": unmodified_files.load(Ordering::Relaxed)
            })
        );
    } else {
        println!("\n========================================================");
        println!("             KOVA FAST ENGINE BACKUP RESULT            ");
        println!("========================================================");
        println!("Session ID       : {}", sid);
        println!("Total Items      : {}", manifest.total_files);
        println!("Source Raw Size  : {:.2} MB ({} bytes)", tot_b as f64 / 1048576.0, tot_b);
        println!("Deduplicated     : {:.2} MB ({} bytes)", ded_b as f64 / 1048576.0, ded_b);
        println!("Compressed Stored: {:.2} MB ({} bytes)", cmp_b as f64 / 1048576.0, cmp_b);
        println!("Elapsed Time     : {:.3}s", total_elapsed);
        println!("Effective Speed  : {:.2} MB/s", throughput_mb);
        println!("========================================================\n");
    }

    Ok(())
}

fn run_restore(
    session_id: &str,
    repo: &Path,
    target: &Path,
) -> Result<(), Box<dyn std::error::Error>> {
    let start_time = Instant::now();
    println!("[KOVA-FAST] Restoring session: {}", session_id);
    println!("[KOVA-FAST] Target path: {}", target.display());

    let cas = CasStorage::new(repo)?;
    let manifest = cas.load_manifest(session_id)?;

    fs::create_dir_all(target)?;

    let manifest_files = manifest.files();

    // Create directories first
    for entry in &manifest_files {
        if entry.is_dir {
            let dir_path = target.join(&entry.path);
            fs::create_dir_all(&dir_path)?;
        }
    }

    // Restore files
    manifest_files.par_iter().filter(|e| !e.is_dir).for_each(|entry| {
        let file_path = target.join(&entry.path);
        if let Some(parent) = file_path.parent() {
            let _ = fs::create_dir_all(parent);
        }

        let mut file = match File::create(&file_path) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("[ERROR] Cannot create file {}: {}", file_path.display(), e);
                return;
            }
        };

        for chunk_info in &entry.chunks {
            let comp_data = match cas.read_blob(&chunk_info.hash) {
                Ok(data) => data,
                Err(e) => {
                    eprintln!("[ERROR] Missing blob {}: {}", chunk_info.hash, e);
                    return;
                }
            };

            let raw_data = match compressor::decompress(&comp_data) {
                Ok(data) => data,
                Err(e) => {
                    eprintln!("[ERROR] Decompress failed for {}: {}", chunk_info.hash, e);
                    return;
                }
            };

            if let Err(e) = file.write_all(&raw_data) {
                eprintln!("[ERROR] Write failed for {}: {}", file_path.display(), e);
                return;
            }
        }
    });

    println!(
        "[KOVA-FAST] Restore completed in {:.3}s",
        start_time.elapsed().as_secs_f64()
    );
    Ok(())
}

fn run_benchmark(size_mb: usize) -> Result<(), Box<dyn std::error::Error>> {
    println!("[KOVA-FAST] Generating {} MB synthetic test buffer...", size_mb);
    let bytes_len = size_mb * 1024 * 1024;
    let mut data = vec![0u8; bytes_len];

    // Populate with pseudo-random pattern (high-speed LCG)
    let mut seed = 0x12345678u64;
    for chunk in data.chunks_mut(8) {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
        let b = seed.to_le_bytes();
        let copy_len = chunk.len().min(8);
        chunk[..copy_len].copy_from_slice(&b[..copy_len]);
    }

    println!("[KOVA-FAST] Starting Pipeline Benchmark (FastCDC + BLAKE3 + Zstd-3)...");
    let start = Instant::now();

    let chunks = chunker::chunk_slice(&data);
    let chunk_time = start.elapsed().as_secs_f64();
    println!(
        "[BENCH] FastCDC Chunking: {} chunks created in {:.3}s ({:.2} MB/s)",
        chunks.len(),
        chunk_time,
        (size_mb as f64) / chunk_time
    );

    let comp_start = Instant::now();
    let total_comp_bytes: usize = chunks
        .par_iter()
        .map(|c| {
            let comp = compressor::compress(&c.data, 3).expect("compression");
            comp.len()
        })
        .sum();
    let comp_time = comp_start.elapsed().as_secs_f64();

    let total_elapsed = start.elapsed().as_secs_f64();
    println!(
        "[BENCH] Rayon Zstd-3 Compression: {} bytes in {:.3}s ({:.2} MB/s)",
        total_comp_bytes,
        comp_time,
        (size_mb as f64) / comp_time
    );

    println!("\n========================================================");
    println!("             KOVA PIPELINE BENCHMARK SUMMARY           ");
    println!("========================================================");
    println!("Dataset Size     : {} MB", size_mb);
    println!("Total Elapsed    : {:.3}s", total_elapsed);
    println!("Total Throughput : {:.2} MB/s", (size_mb as f64) / total_elapsed);
    println!("Compression Ratio: {:.2}%", (total_comp_bytes as f64 / bytes_len as f64) * 100.0);
    println!("========================================================\n");

    Ok(())
}
