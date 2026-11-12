use kova_engine::{baremetal, chunker, compressor, ops, search};

use clap::{Parser, Subcommand};
use rayon::prelude::*;
use std::path::{Path, PathBuf};
use std::time::Instant;


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

        /// Exclude patterns (same semantics as core/filter.py)
        #[arg(long, num_args = 1..)]
        exclude: Vec<String>,
    },
    /// Restore files from a specific snapshot session
    Restore {
        #[arg(short, long)]
        session_id: String,

        #[arg(short, long)]
        repo: PathBuf,

        /// 복원 대상 폴더(--in-place 가 아니면 필수)
        #[arg(short, long)]
        target: Option<PathBuf>,

        /// 백업 당시의 원래 위치로 복원(format_version 2 스냅샷만 가능)
        #[arg(long)]
        in_place: bool,
    },
    /// Search files across historical snapshots with ultra-fast parallel matching
    Search {
        #[arg(short, long)]
        repo: PathBuf,

        #[arg(short, long)]
        query: String,

        #[arg(long, default_value_t = 10)]
        max_snapshots: usize,

        #[arg(long, default_value_t = 50)]
        limit: usize,

        #[arg(long, default_value_t = true)]
        chosung: bool,

        #[arg(long)]
        json: bool,
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
            exclude,
        } => run_backup(&source, &repo, session_id, zstd_level, json, &exclude)?,

        Commands::Restore {
            session_id,
            repo,
            target,
            in_place,
        } => run_restore(&session_id, &repo, target.as_deref(), in_place)?,

        Commands::Search {
            repo,
            query,
            max_snapshots,
            limit,
            chosung,
            json,
        } => run_search(&repo, &query, max_snapshots, limit, chosung, json)?,

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
    exclude: &[String],
) -> Result<(), Box<dyn std::error::Error>> {
    let opts = ops::BackupOptions {
        sources: sources.to_vec(),
        repo: repo.to_path_buf(),
        session_id,
        zstd_level,
        exclude: exclude.to_vec(),
        profile_id: None,
    };
    let on_event = move |ev: ops::Event| match ev {
        ops::Event::Phase { phase, message } => {
            if json_mode {
                println!("{}", serde_json::json!({"event": "phase", "phase": phase, "message": message}));
            } else {
                println!("[KOVA-FAST] {}", message);
            }
        }
        ops::Event::Progress(p) => {
            if json_mode {
                println!(
                    "{}",
                    serde_json::json!({
                        "type": "progress", "status": "running", "percent": p.percent,
                        "processed_files": p.processed_files, "total_files": p.total_files,
                        "processed_size": p.processed_size, "speed": p.speed,
                        "current_file": p.current_file, "new_files": p.new_files,
                        "modified_files": p.modified_files, "unmodified_files": p.unmodified_files
                    })
                );
            }
        }
        ops::Event::RestoreProgress { .. } => {}
    };
    let cancel = std::sync::atomic::AtomicBool::new(false);
    let s = ops::run_backup(&opts, &on_event, &cancel)?;
    for f in &s.skipped_files {
        eprintln!("[WARN] skipped: {}", f);
    }
    if json_mode {
        println!(
            "{}",
            serde_json::json!({
                "type": "summary", "session_id": s.session_id, "total_files": s.total_files,
                "total_bytes": s.total_bytes, "dedup_bytes": s.dedup_bytes,
                "compressed_bytes": s.compressed_bytes, "elapsed_seconds": s.elapsed_seconds,
                "throughput_mb": s.throughput_mb, "new_files": s.new_files,
                "modified_files": s.modified_files, "unmodified_files": s.unmodified_files,
                "skipped_files": s.skipped_files.len()
            })
        );
    } else {
        println!("Session ID       : {}", s.session_id);
        println!("Total Items      : {}", s.total_files);
        println!("Source Raw Size  : {:.2} MB", s.total_bytes as f64 / 1048576.0);
        println!("Deduplicated     : {:.2} MB", s.dedup_bytes as f64 / 1048576.0);
        println!("Compressed Stored: {:.2} MB", s.compressed_bytes as f64 / 1048576.0);
        println!("Skipped files    : {}", s.skipped_files.len());
        println!("Elapsed Time     : {:.3}s ({:.2} MB/s)", s.elapsed_seconds, s.throughput_mb);
    }
    Ok(())
}

fn run_restore(
    session_id: &str,
    repo: &Path,
    target: Option<&Path>,
    in_place: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    if !in_place && target.is_none() {
        return Err("--target is required unless --in-place is set".into());
    }
    let opts = ops::RestoreOptions {
        repo: repo.to_path_buf(),
        session_id: session_id.to_string(),
        target: if in_place { None } else { target.map(|t| t.to_path_buf()) },
    };
    let on_event = |ev: ops::Event| {
        if let ops::Event::RestoreProgress { done, total } = ev {
            println!("[PROGRESS] {}/{}", done, total);
        }
    };
    let cancel = std::sync::atomic::AtomicBool::new(false);
    let s = ops::run_restore(&opts, &on_event, &cancel)?;
    println!("[KOVA-FAST] Restore completed in {:.3}s", s.elapsed_seconds);
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

fn run_search(
    repo: &Path,
    query: &str,
    max_snapshots: usize,
    limit: usize,
    chosung: bool,
    json_mode: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let start = Instant::now();
    let results = search::search_snapshots(repo, query, max_snapshots, limit, chosung)?;
    let elapsed = start.elapsed().as_secs_f64();

    if json_mode {
        let json_str = serde_json::to_string(&results)?;
        println!("{}", json_str);
    } else {
        println!("========================================================");
        println!("       KOVA FAST ENGINE SNAPSHOT SEARCH RESULT          ");
        println!("========================================================");
        println!("Query        : {}", query);
        println!("Max Snapshots: {}", max_snapshots);
        println!("Found Items  : {}", results.len());
        println!("Elapsed Time : {:.3}s ({:.1}ms)", elapsed, elapsed * 1000.0);
        println!("========================================================");

        for (idx, item) in results.iter().enumerate() {
            println!(
                "[{}] {} (점수: {}) - {}개 버전 (최신: {}, {})",
                idx + 1,
                item.path,
                item.relevance,
                item.version_count,
                item.latest_size_formatted,
                item.latest_time
            );
            for v in &item.versions {
                println!(
                    "    -> [{}] {} | {} | {}",
                    v.snapshot_id, v.snapshot_time, v.size_formatted, v.mtime_formatted
                );
            }
        }
    }

    Ok(())
}
