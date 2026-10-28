//! Extent copy throughput benchmark (prototype, not wired into Kova).
//!
//! Reads the allocated clusters of an NTFS volume (normally a VSS snapshot device)
//! with several parallel Direct I/O readers and packs them into one target file.
//! Used only to measure whether a parallel writer beats wbadmin's ~130 MB/s.
//! It does not produce a restorable image.

use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::os::windows::fs::{FileExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use windows::core::HRESULT;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::IO::DeviceIoControl;

const ALIGN: usize = 4096;
const FILE_FLAG_NO_BUFFERING: u32 = 0x2000_0000;
const FSCTL_GET_VOLUME_BITMAP: u32 = 0x0009_006F;
const ERROR_MORE_DATA: HRESULT = HRESULT::from_win32(234);

#[derive(Parser)]
#[command(name = "extent_bench", about = "Parallel allocated-extent copy benchmark")]
struct Args {
    /// Volume device, e.g. \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7
    #[arg(long)]
    source: String,
    /// Output file. Omit for a read-only run (upper bound of the read side).
    #[arg(long)]
    target: Option<String>,
    /// Stop after this many GB of allocated data (0 = everything)
    #[arg(long, default_value_t = 0)]
    limit_gb: u64,
    #[arg(long, default_value_t = 8)]
    threads: usize,
    #[arg(long, default_value_t = 16)]
    chunk_mb: usize,
    /// Only read the bitmap and print allocation statistics
    #[arg(long)]
    extents_only: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Extent {
    start: u64,
    len: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Job {
    src: u64,
    dst: u64,
    len: u64,
}

struct AlignedBuf {
    ptr: *mut u8,
    layout: Layout,
}

unsafe impl Send for AlignedBuf {}

impl AlignedBuf {
    fn new(size: usize) -> Self {
        let size = (size + ALIGN - 1) & !(ALIGN - 1);
        let layout = Layout::from_size_align(size, ALIGN).expect("layout");
        let ptr = unsafe { alloc_zeroed(layout) };
        assert!(!ptr.is_null(), "allocation failed");
        Self { ptr, layout }
    }
    fn slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.layout.size()) }
    }
}

impl Drop for AlignedBuf {
    fn drop(&mut self) {
        unsafe { dealloc(self.ptr, self.layout) };
    }
}

/// Turns a volume allocation bitmap (1 bit per cluster) into byte extents.
fn bitmap_to_extents(bitmap: &[u8], total_clusters: u64, cluster: u64) -> Vec<Extent> {
    let mut out = Vec::new();
    let mut run_start: Option<u64> = None;
    let mut c = 0u64;
    while c < total_clusters {
        let byte = bitmap[(c / 8) as usize];
        // Fast paths for whole bytes that are fully free or fully used.
        if c.is_multiple_of(8) && c + 8 <= total_clusters {
            if byte == 0x00 {
                if let Some(s) = run_start.take() {
                    out.push(Extent { start: s * cluster, len: (c - s) * cluster });
                }
                c += 8;
                continue;
            }
            if byte == 0xFF {
                if run_start.is_none() {
                    run_start = Some(c);
                }
                c += 8;
                continue;
            }
        }
        let used = (byte >> (c % 8)) & 1 == 1;
        match (used, run_start) {
            (true, None) => run_start = Some(c),
            (false, Some(s)) => {
                out.push(Extent { start: s * cluster, len: (c - s) * cluster });
                run_start = None;
            }
            _ => {}
        }
        c += 1;
    }
    if let Some(s) = run_start {
        out.push(Extent { start: s * cluster, len: (total_clusters - s) * cluster });
    }
    out
}

/// Aligns extents to 4 KiB (Direct I/O), clamps to the volume end and merges neighbours.
fn align_extents(extents: &[Extent], volume_bytes: u64) -> Vec<Extent> {
    let limit = volume_bytes & !(ALIGN as u64 - 1);
    let mut out: Vec<Extent> = Vec::new();
    for e in extents {
        let start = e.start & !(ALIGN as u64 - 1);
        let end = ((e.start + e.len + ALIGN as u64 - 1) & !(ALIGN as u64 - 1)).min(limit);
        if end <= start {
            continue;
        }
        match out.last_mut() {
            Some(last) if start <= last.start + last.len => {
                let new_end = (last.start + last.len).max(end);
                last.len = new_end - last.start;
            }
            _ => out.push(Extent { start, len: end - start }),
        }
    }
    out
}

/// Splits extents into chunk-sized jobs; the target is a packed stream of the extents.
fn make_jobs(extents: &[Extent], chunk: u64, limit_bytes: u64) -> Vec<Job> {
    let mut jobs = Vec::new();
    let mut dst = 0u64;
    'outer: for e in extents {
        let mut off = 0u64;
        while off < e.len {
            let mut len = chunk.min(e.len - off);
            if limit_bytes > 0 {
                if dst >= limit_bytes {
                    break 'outer;
                }
                len = len.min(limit_bytes - dst);
            }
            jobs.push(Job { src: e.start + off, dst, len });
            dst += len;
            off += len;
        }
    }
    jobs
}

fn open_direct(path: &str, write: bool) -> std::io::Result<File> {
    OpenOptions::new()
        .read(!write)
        .write(write)
        .share_mode(7)
        .custom_flags(FILE_FLAG_NO_BUFFERING)
        .open(path)
}

fn read_full(f: &File, buf: &mut [u8], mut off: u64) -> std::io::Result<()> {
    let mut done = 0usize;
    while done < buf.len() {
        let n = f.seek_read(&mut buf[done..], off)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!("unexpected EOF at offset {}", off),
            ));
        }
        done += n;
        off += n as u64;
    }
    Ok(())
}

fn write_full(f: &File, buf: &[u8], mut off: u64) -> std::io::Result<()> {
    let mut done = 0usize;
    while done < buf.len() {
        let n = f.seek_write(&buf[done..], off)?;
        if n == 0 {
            return Err(std::io::Error::new(std::io::ErrorKind::WriteZero, "write returned 0"));
        }
        done += n;
        off += n as u64;
    }
    Ok(())
}

/// Copies every job from `source` to the packed `target` (or just reads when None).
fn copy_jobs(
    source: &str,
    target: Option<&str>,
    jobs: &[Job],
    threads: usize,
    chunk: usize,
    done_bytes: &AtomicU64,
) -> Result<(), String> {
    let total: u64 = jobs.iter().map(|j| j.len).sum();
    if let Some(t) = target {
        let f = File::create(t).map_err(|e| format!("create target: {e}"))?;
        f.set_len(total).map_err(|e| format!("preallocate target: {e}"))?;
    }
    let next = AtomicUsize::new(0);
    let abort = AtomicBool::new(false);
    let first_err: Mutex<Option<String>> = Mutex::new(None);

    thread::scope(|s| {
        for _ in 0..threads.max(1) {
            s.spawn(|| {
                let src = match open_direct(source, false) {
                    Ok(f) => f,
                    Err(e) => {
                        abort.store(true, Ordering::SeqCst);
                        first_err.lock().unwrap().get_or_insert(format!("open source: {e}"));
                        return;
                    }
                };
                let dst = match target.map(|t| open_direct(t, true)).transpose() {
                    Ok(f) => f,
                    Err(e) => {
                        abort.store(true, Ordering::SeqCst);
                        first_err.lock().unwrap().get_or_insert(format!("open target: {e}"));
                        return;
                    }
                };
                let mut buf = AlignedBuf::new(chunk);
                loop {
                    if abort.load(Ordering::Relaxed) {
                        return;
                    }
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    let Some(job) = jobs.get(i) else { return };
                    let n = job.len as usize;
                    let res = read_full(&src, &mut buf.slice()[..n], job.src).and_then(|_| {
                        match &dst {
                            Some(d) => write_full(d, &buf.slice()[..n], job.dst),
                            None => Ok(()),
                        }
                    });
                    match res {
                        Ok(()) => {
                            done_bytes.fetch_add(job.len, Ordering::Relaxed);
                        }
                        Err(e) => {
                            abort.store(true, Ordering::SeqCst);
                            first_err
                                .lock()
                                .unwrap()
                                .get_or_insert(format!("job at src {}: {e}", job.src));
                            return;
                        }
                    }
                }
            });
        }

        // Progress reporting on the calling thread.
        let t0 = Instant::now();
        let mut last = 0u64;
        let mut last_t = t0;
        while !abort.load(Ordering::Relaxed) && done_bytes.load(Ordering::Relaxed) < total {
            thread::sleep(Duration::from_millis(100));
            if last_t.elapsed() < Duration::from_secs(5) {
                continue;
            }
            let cur = done_bytes.load(Ordering::Relaxed);
            let dt = last_t.elapsed().as_secs_f64();
            eprintln!(
                "[{:>6.0}s] {:>7.1} GB / {:.1} GB  {:>6.0} MB/s",
                t0.elapsed().as_secs_f64(),
                cur as f64 / 1e9 * 1.073741824,
                total as f64 / 1e9 * 1.073741824,
                (cur - last) as f64 / 1048576.0 / dt
            );
            last = cur;
            last_t = Instant::now();
        }
    });

    match first_err.into_inner().unwrap() {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

fn read_boot_sector(f: &File) -> Result<(u64, u64), String> {
    let mut buf = AlignedBuf::new(ALIGN);
    read_full(f, &mut buf.slice()[..ALIGN], 0).map_err(|e| format!("read boot sector: {e}"))?;
    let b = buf.slice();
    if &b[3..7] != b"NTFS" {
        return Err("source is not an NTFS volume".into());
    }
    let bps = u16::from_le_bytes([b[0x0B], b[0x0C]]) as u64;
    let spc = b[0x0D] as u64;
    let total_sectors = u64::from_le_bytes(b[0x28..0x30].try_into().unwrap());
    if bps == 0 || spc == 0 {
        return Err("invalid NTFS boot sector".into());
    }
    Ok((bps * spc, bps * total_sectors))
}

fn read_bitmap(f: &File, total_clusters: u64) -> Result<Vec<u8>, String> {
    let h = HANDLE(f.as_raw_handle());
    let mut bitmap: Vec<u8> = Vec::with_capacity(total_clusters.div_ceil(8) as usize);
    let mut out = vec![0u8; 16 + 4 * 1024 * 1024];
    let mut start: i64 = 0;
    loop {
        let mut returned = 0u32;
        let res = unsafe {
            DeviceIoControl(
                h,
                FSCTL_GET_VOLUME_BITMAP,
                Some(&start as *const i64 as *const c_void),
                8,
                Some(out.as_mut_ptr() as *mut c_void),
                out.len() as u32,
                Some(&mut returned),
                None,
            )
        };
        let more = match res {
            Ok(()) => false,
            Err(e) if e.code() == ERROR_MORE_DATA => true,
            Err(e) => return Err(format!("FSCTL_GET_VOLUME_BITMAP failed: {e}")),
        };
        let got_start = i64::from_le_bytes(out[0..8].try_into().unwrap());
        let size = i64::from_le_bytes(out[8..16].try_into().unwrap()) as u64;
        if got_start != start {
            return Err(format!("bitmap start mismatch: asked {start}, got {got_start}"));
        }
        let data_bits = (returned as u64).saturating_sub(16) * 8;
        let bits = size.min(data_bits);
        bitmap.extend_from_slice(&out[16..16 + bits.div_ceil(8) as usize]);
        start += bits as i64;
        if !more || bits >= size {
            break;
        }
    }
    Ok(bitmap)
}

fn main() {
    let a = Args::parse();
    if let Err(e) = run(a) {
        eprintln!("ERROR: {e}");
        std::process::exit(1);
    }
}

fn run(a: Args) -> Result<(), String> {
    if a.chunk_mb == 0 {
        return Err("chunk_mb must be at least 1".into());
    }
    let chunk = a.chunk_mb * 1024 * 1024;

    let vol = OpenOptions::new()
        .read(true)
        .share_mode(7)
        .open(&a.source)
        .map_err(|e| format!("open source volume: {e}"))?;
    let (cluster, volume_bytes) = {
        let direct = open_direct(&a.source, false).map_err(|e| format!("open source (direct): {e}"))?;
        read_boot_sector(&direct)?
    };
    let total_clusters = volume_bytes / cluster;
    let t = Instant::now();
    let bitmap = read_bitmap(&vol, total_clusters)?;
    let extents = align_extents(&bitmap_to_extents(&bitmap, total_clusters, cluster), volume_bytes);
    let used: u64 = extents.iter().map(|e| e.len).sum();
    eprintln!(
        "volume {:.1} GB, cluster {} B, allocated {:.1} GB ({:.1}%), {} extents (bitmap in {:.1}s)",
        volume_bytes as f64 / 1073741824.0,
        cluster,
        used as f64 / 1073741824.0,
        used as f64 * 100.0 / volume_bytes as f64,
        extents.len(),
        t.elapsed().as_secs_f64()
    );
    if a.extents_only {
        return Ok(());
    }

    let limit = a.limit_gb * 1024 * 1024 * 1024;
    let jobs = make_jobs(&extents, chunk as u64, limit);
    let total: u64 = jobs.iter().map(|j| j.len).sum();
    eprintln!(
        "copying {:.1} GB with {} threads x {} MB chunks -> {}",
        total as f64 / 1073741824.0,
        a.threads,
        a.chunk_mb,
        a.target.as_deref().unwrap_or("(read-only)")
    );

    let done = AtomicU64::new(0);
    let t0 = Instant::now();
    copy_jobs(&a.source, a.target.as_deref(), &jobs, a.threads, chunk, &done)?;
    let secs = t0.elapsed().as_secs_f64();
    let bytes = done.load(Ordering::Relaxed);
    println!(
        "{{\"copied_bytes\": {}, \"seconds\": {:.1}, \"avg_mb_s\": {:.0}}}",
        bytes,
        secs,
        bytes as f64 / 1048576.0 / secs
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> String {
        let p = std::env::temp_dir().join(format!("kova_extent_{}_{}", std::process::id(), name));
        p.to_string_lossy().into_owned()
    }

    #[test]
    fn bitmap_runs_become_extents() {
        // clusters: 0..3 used, 3..11 free, 11..16 used (cluster = 4096)
        // bit i of byte i/8; used bits: 0,1,2, 11,12,13,14,15
        let bitmap = [0b0000_0111u8, 0b1111_1000u8];
        let e = bitmap_to_extents(&bitmap, 16, 4096);
        assert_eq!(
            e,
            vec![
                Extent { start: 0, len: 3 * 4096 },
                Extent { start: 11 * 4096, len: 5 * 4096 }
            ]
        );
    }

    #[test]
    fn run_spanning_fast_path_bytes_is_merged() {
        let bitmap = [0xFFu8, 0xFFu8, 0x01u8];
        let e = bitmap_to_extents(&bitmap, 20, 4096);
        assert_eq!(e, vec![Extent { start: 0, len: 17 * 4096 }]);
    }

    #[test]
    fn align_merges_and_clamps() {
        let e = vec![
            Extent { start: 100, len: 4000 },
            Extent { start: 4096, len: 4096 },
            Extent { start: 40_000, len: 100_000 },
        ];
        let a = align_extents(&e, 65_536);
        assert_eq!(
            a,
            vec![Extent { start: 0, len: 8192 }, Extent { start: 36_864, len: 65_536 - 36_864 }]
        );
    }

    #[test]
    fn jobs_are_packed_and_limited() {
        let e = vec![Extent { start: 0, len: 12288 }, Extent { start: 65536, len: 8192 }];
        let j = make_jobs(&e, 8192, 0);
        assert_eq!(
            j,
            vec![
                Job { src: 0, dst: 0, len: 8192 },
                Job { src: 8192, dst: 8192, len: 4096 },
                Job { src: 65536, dst: 12288, len: 8192 },
            ]
        );
        let limited = make_jobs(&e, 8192, 16384);
        assert_eq!(limited.iter().map(|x| x.len).sum::<u64>(), 16384);
    }

    #[test]
    fn copy_packs_extents_exactly() {
        let src = tmp("src.bin");
        let dst = tmp("dst.bin");
        let data: Vec<u8> = (0..(2 * 1024 * 1024u32)).map(|i| (i.wrapping_mul(2654435761) >> 24) as u8).collect();
        std::fs::write(&src, &data).unwrap();

        let extents = vec![
            Extent { start: 0, len: 8192 },
            Extent { start: 16384, len: 12288 },
            Extent { start: 1024 * 1024, len: 512 * 1024 },
        ];
        let jobs = make_jobs(&extents, 8192, 0);
        let done = AtomicU64::new(0);
        copy_jobs(&src, Some(&dst), &jobs, 4, 8192, &done).unwrap();

        let mut expected = Vec::new();
        for e in &extents {
            expected.extend_from_slice(&data[e.start as usize..(e.start + e.len) as usize]);
        }
        assert_eq!(std::fs::read(&dst).unwrap(), expected);
        assert_eq!(done.load(Ordering::Relaxed), expected.len() as u64);
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&dst);
    }

    #[test]
    fn read_past_end_is_error() {
        let src = tmp("short.bin");
        let dst = tmp("short_dst.bin");
        std::fs::write(&src, vec![7u8; 8192]).unwrap();
        let jobs = vec![Job { src: 0, dst: 0, len: 16384 }];
        let done = AtomicU64::new(0);
        assert!(copy_jobs(&src, Some(&dst), &jobs, 2, 16384, &done).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&dst);
    }

    #[test]
    fn missing_source_is_error() {
        let jobs = vec![Job { src: 0, dst: 0, len: 4096 }];
        let done = AtomicU64::new(0);
        assert!(copy_jobs("Z:\\no\\such\\file", None, &jobs, 1, 4096, &done).is_err());
    }
}
