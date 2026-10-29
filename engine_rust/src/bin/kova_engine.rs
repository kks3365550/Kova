//! KOVA Engine: prototype image writer/verifier (phase 1-2, not wired into Kova).
//!
//! File layout (all integers little-endian, every region 4096-aligned for Direct I/O):
//!   [0, 4096)        header
//!   [4096, index)    chunk records: each chunk is stored raw or zstd-compressed, padded to 4096
//!   [index, +n*64)   index entries, padded to 4096
//! Header: magic "KOVAENG1" (legacy "KIMG0001" readable), version u32, flags u32, volume_bytes u64, cluster u64,
//!         chunk_bytes u64, index_off u64, index_count u64, raw_bytes u64, index_hash [32].
//! Index entry (64 B): src_off u64, raw_len u32, flags u32 (bit0 = zstd), file_off u64,
//!         comp_len u32, reserved u32, blake3(raw chunk) [32].
//! Free clusters have no chunk; a restorer leaves those gaps untouched.

use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::os::windows::fs::{FileExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use clap::{Parser, Subcommand};
use windows::core::HRESULT;
use windows::Win32::Foundation::HANDLE;
use windows::core::w;
use windows::Win32::Foundation::CloseHandle;
use windows::Win32::Security::{
    AdjustTokenPrivileges, LookupPrivilegeValueW, LUID_AND_ATTRIBUTES, SE_PRIVILEGE_ENABLED,
    TOKEN_ADJUST_PRIVILEGES, TOKEN_PRIVILEGES, TOKEN_QUERY,
};
use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
use windows::Win32::System::Ioctl::{GET_LENGTH_INFORMATION, IOCTL_DISK_GET_LENGTH_INFO};
use windows::Win32::System::IO::DeviceIoControl;

const ALIGN: usize = 4096;
const FILE_FLAG_NO_BUFFERING: u32 = 0x2000_0000;
const FSCTL_GET_VOLUME_BITMAP: u32 = 0x0009_006F;
const FSCTL_GET_RETRIEVAL_POINTERS: u32 = 0x0009_0073;
const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
/// VSS diff-area files in "System Volume Information" end with this GUID.
const VSS_STORE_SUFFIX: &str = "{3808876b-c176-4e48-b7ae-04046e6cc752}";
const FSCTL_SET_SPARSE: u32 = 0x0009_00C4;
const FSCTL_LOCK_VOLUME: u32 = 0x0009_0018;
const FSCTL_DISMOUNT_VOLUME: u32 = 0x0009_0020;
const ERROR_MORE_DATA: HRESULT = HRESULT::from_win32(234);
const MAGIC: &[u8; 8] = b"KOVAENG1";
const LEGACY_MAGIC: &[u8; 8] = b"KIMG0001"; // 구 .kimg 이미지 읽기 호환
const VERSION: u32 = 1;
const ENTRY_SIZE: usize = 64;
const FLAG_ZSTD: u32 = 1;

#[derive(Parser)]
#[command(name = "kova_engine", about = "Kova image prototype: write and verify")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Write an image of the allocated clusters of an NTFS volume (normally a VSS snapshot)
    Write {
        /// Volume device, e.g. \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7
        #[arg(long)]
        source: String,
        #[arg(long)]
        target: String,
        /// Stop after this many GiB of allocated data (0 = everything)
        #[arg(long, default_value_t = 0)]
        limit_gb: u64,
        #[arg(long, default_value_t = 8)]
        threads: usize,
        #[arg(long, default_value_t = 16)]
        chunk_mb: usize,
        /// zstd level; 0 stores chunks raw
        #[arg(long, default_value_t = 1)]
        level: i32,
    },
    /// Re-read an image, decompress every chunk and check its BLAKE3 hash
    Verify {
        #[arg(long)]
        image: String,
        #[arg(long, default_value_t = 8)]
        threads: usize,
    },
    /// Write every chunk back at its original offset. A plain path becomes a sparse file
    /// of the volume size; a \\.\ or \\?\ device path is opened as is.
    Restore {
        #[arg(long)]
        image: String,
        #[arg(long)]
        target: String,
        #[arg(long, default_value_t = 8)]
        threads: usize,
    },
    /// Byte-compare every chunk range of two volumes/files (e.g. snapshot vs restored file)
    Compare {
        #[arg(long)]
        image: String,
        #[arg(long)]
        source: String,
        #[arg(long)]
        target: String,
        #[arg(long, default_value_t = 8)]
        threads: usize,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Extent {
    start: u64,
    len: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Job {
    src: u64,
    len: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Entry {
    src_off: u64,
    raw_len: u32,
    flags: u32,
    file_off: u64,
    comp_len: u32,
    hash: [u8; 32],
}

impl Entry {
    fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.src_off.to_le_bytes());
        out.extend_from_slice(&self.raw_len.to_le_bytes());
        out.extend_from_slice(&self.flags.to_le_bytes());
        out.extend_from_slice(&self.file_off.to_le_bytes());
        out.extend_from_slice(&self.comp_len.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&self.hash);
    }
    fn decode(b: &[u8]) -> Self {
        Self {
            src_off: u64::from_le_bytes(b[0..8].try_into().unwrap()),
            raw_len: u32::from_le_bytes(b[8..12].try_into().unwrap()),
            flags: u32::from_le_bytes(b[12..16].try_into().unwrap()),
            file_off: u64::from_le_bytes(b[16..24].try_into().unwrap()),
            comp_len: u32::from_le_bytes(b[24..28].try_into().unwrap()),
            hash: b[32..64].try_into().unwrap(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Header {
    volume_bytes: u64,
    cluster: u64,
    chunk_bytes: u64,
    index_off: u64,
    index_count: u64,
    raw_bytes: u64,
    index_hash: [u8; 32],
}

impl Header {
    fn encode(&self) -> Vec<u8> {
        let mut b = vec![0u8; ALIGN];
        b[0..8].copy_from_slice(MAGIC);
        b[8..12].copy_from_slice(&VERSION.to_le_bytes());
        b[16..24].copy_from_slice(&self.volume_bytes.to_le_bytes());
        b[24..32].copy_from_slice(&self.cluster.to_le_bytes());
        b[32..40].copy_from_slice(&self.chunk_bytes.to_le_bytes());
        b[40..48].copy_from_slice(&self.index_off.to_le_bytes());
        b[48..56].copy_from_slice(&self.index_count.to_le_bytes());
        b[56..64].copy_from_slice(&self.raw_bytes.to_le_bytes());
        b[64..96].copy_from_slice(&self.index_hash);
        b
    }
    fn decode(b: &[u8]) -> Result<Self, String> {
        if b.len() < 96 || (&b[0..8] != MAGIC && &b[0..8] != LEGACY_MAGIC) {
            return Err("not a KOVA Engine image (bad magic)".into());
        }
        let ver = u32::from_le_bytes(b[8..12].try_into().unwrap());
        if ver != VERSION {
            return Err(format!("unsupported KOVA Engine version {ver}"));
        }
        let u = |r: std::ops::Range<usize>| u64::from_le_bytes(b[r].try_into().unwrap());
        Ok(Self {
            volume_bytes: u(16..24),
            cluster: u(24..32),
            chunk_bytes: u(32..40),
            index_off: u(40..48),
            index_count: u(48..56),
            raw_bytes: u(56..64),
            index_hash: b[64..96].try_into().unwrap(),
        })
    }
}

struct AlignedBuf {
    ptr: *mut u8,
    layout: Layout,
}

unsafe impl Send for AlignedBuf {}

impl AlignedBuf {
    fn new(size: usize) -> Self {
        let size = round_up(size as u64) as usize;
        let layout = Layout::from_size_align(size.max(ALIGN), ALIGN).expect("layout");
        let ptr = unsafe { alloc_zeroed(layout) };
        assert!(!ptr.is_null(), "allocation failed: {} bytes", layout.size());
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

fn round_up(v: u64) -> u64 {
    (v + ALIGN as u64 - 1) & !(ALIGN as u64 - 1)
}

/// Turns a volume allocation bitmap (1 bit per cluster) into byte extents.
fn bitmap_to_extents(bitmap: &[u8], total_clusters: u64, cluster: u64) -> Vec<Extent> {
    let mut out = Vec::new();
    let mut run_start: Option<u64> = None;
    let mut c = 0u64;
    while c < total_clusters {
        let byte = bitmap[(c / 8) as usize];
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
        let end = round_up(e.start + e.len).min(limit);
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

/// Splits extents into chunk-sized jobs, optionally stopping after `limit_bytes`.
fn make_jobs(extents: &[Extent], chunk: u64, limit_bytes: u64) -> Vec<Job> {
    let mut jobs = Vec::new();
    let mut total = 0u64;
    'outer: for e in extents {
        let mut off = 0u64;
        while off < e.len {
            let mut len = chunk.min(e.len - off);
            if limit_bytes > 0 {
                if total >= limit_bytes {
                    break 'outer;
                }
                len = len.min(limit_bytes - total);
            }
            jobs.push(Job { src: e.start + off, len });
            total += len;
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
                format!("unexpected EOF at offset {off}"),
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

struct Shared {
    next: AtomicUsize,
    abort: AtomicBool,
    first_err: Mutex<Option<String>>,
}

impl Shared {
    fn new() -> Self {
        Self { next: AtomicUsize::new(0), abort: AtomicBool::new(false), first_err: Mutex::new(None) }
    }
    fn fail(&self, msg: String) {
        self.abort.store(true, Ordering::SeqCst);
        self.first_err.lock().unwrap().get_or_insert(msg);
    }
    fn result(self) -> Result<(), String> {
        match self.first_err.into_inner().unwrap() {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

/// Prints progress every 5 s until all `total` bytes are done or a worker failed.
fn progress_loop(sh: &Shared, done: &AtomicU64, total: u64, extra: &AtomicU64, label: &str) {
    let t0 = Instant::now();
    let mut last = 0u64;
    let mut last_t = t0;
    while !sh.abort.load(Ordering::Relaxed) && done.load(Ordering::Relaxed) < total {
        thread::sleep(Duration::from_millis(100));
        if last_t.elapsed() < Duration::from_secs(5) {
            continue;
        }
        let cur = done.load(Ordering::Relaxed);
        let dt = last_t.elapsed().as_secs_f64();
        eprintln!(
            "[{:>6.0}s] {label} {:>7.1} / {:.1} GiB  {:>6.0} MB/s  (stored {:.1} GiB)",
            t0.elapsed().as_secs_f64(),
            cur as f64 / 1073741824.0,
            total as f64 / 1073741824.0,
            (cur - last) as f64 / 1048576.0 / dt,
            extra.load(Ordering::Relaxed) as f64 / 1073741824.0
        );
        last = cur;
        last_t = Instant::now();
    }
}

struct WriteOpts {
    threads: usize,
    chunk: usize,
    level: i32,
}

/// Reads every job from `source`, hashes and compresses it, appends it to `target`.
/// Returns the finished header (also written to the file) and the stored byte count.
fn write_image(
    source: &str,
    target: &str,
    jobs: &[Job],
    volume_bytes: u64,
    cluster: u64,
    o: &WriteOpts,
    done: &AtomicU64,
) -> Result<(Header, u64), String> {
    let total: u64 = jobs.iter().map(|j| j.len).sum();
    File::create(target).map_err(|e| format!("create target: {e}"))?;
    let end = AtomicU64::new(ALIGN as u64);
    let stored = AtomicU64::new(0);
    let entries: Mutex<Vec<Entry>> = Mutex::new(Vec::with_capacity(jobs.len()));
    let sh = Shared::new();

    thread::scope(|s| {
        for _ in 0..o.threads.max(1) {
            s.spawn(|| {
                let src = match open_direct(source, false) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open source: {e}")),
                };
                let dst = match open_direct(target, true) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open target: {e}")),
                };
                let mut rbuf = AlignedBuf::new(o.chunk);
                let mut wbuf = AlignedBuf::new(o.chunk);
                loop {
                    if sh.abort.load(Ordering::Relaxed) {
                        return;
                    }
                    let i = sh.next.fetch_add(1, Ordering::SeqCst);
                    let Some(job) = jobs.get(i) else { return };
                    let n = job.len as usize;
                    let res = (|| -> Result<(), String> {
                        rbuf.slice()[..n].fill(0);
                        read_full(&src, &mut rbuf.slice()[..n], job.src)
                            .map_err(|e| format!("read src {}: {e}", job.src))?;
                        let raw = &rbuf.slice()[..n];
                        let hash = *blake3::hash(raw).as_bytes();
                        let comp = if o.level > 0 {
                            zstd::bulk::compress(raw, o.level).map_err(|e| format!("zstd: {e}"))?
                        } else {
                            Vec::new()
                        };
                        let (payload, flags): (&[u8], u32) = if !comp.is_empty() && comp.len() < n {
                            (&comp, FLAG_ZSTD)
                        } else {
                            (raw, 0)
                        };
                        let padded = round_up(payload.len() as u64) as usize;
                        let w = &mut wbuf.slice()[..padded];
                        w[..payload.len()].copy_from_slice(payload);
                        w[payload.len()..].fill(0);
                        let file_off = end.fetch_add(padded as u64, Ordering::SeqCst);
                        write_full(&dst, w, file_off).map_err(|e| format!("write image at {file_off}: {e}"))?;
                        stored.fetch_add(padded as u64, Ordering::Relaxed);
                        entries.lock().unwrap().push(Entry {
                            src_off: job.src,
                            raw_len: n as u32,
                            flags,
                            file_off,
                            comp_len: payload.len() as u32,
                            hash,
                        });
                        Ok(())
                    })();
                    match res {
                        Ok(()) => {
                            done.fetch_add(job.len, Ordering::Relaxed);
                        }
                        Err(e) => return sh.fail(e),
                    }
                }
            });
        }
        progress_loop(&sh, done, total, &stored, "write");
    });
    sh.result()?;

    let mut list = entries.into_inner().unwrap();
    list.sort_by_key(|e| e.src_off);
    if list.len() != jobs.len() {
        return Err(format!("internal: {} entries for {} jobs", list.len(), jobs.len()));
    }
    let mut idx = Vec::with_capacity(list.len() * ENTRY_SIZE);
    for e in &list {
        e.encode(&mut idx);
    }
    let index_hash = *blake3::hash(&idx).as_bytes();
    let index_off = end.load(Ordering::SeqCst);
    let mut ibuf = AlignedBuf::new(idx.len().max(1));
    ibuf.slice()[..idx.len()].copy_from_slice(&idx);
    let dst = open_direct(target, true).map_err(|e| format!("reopen target: {e}"))?;
    let ilen = round_up(idx.len() as u64) as usize;
    write_full(&dst, &ibuf.slice()[..ilen], index_off).map_err(|e| format!("write index: {e}"))?;

    let hdr = Header {
        volume_bytes,
        cluster,
        chunk_bytes: o.chunk as u64,
        index_off,
        index_count: list.len() as u64,
        raw_bytes: total,
        index_hash,
    };
    let mut hbuf = AlignedBuf::new(ALIGN);
    hbuf.slice()[..ALIGN].copy_from_slice(&hdr.encode());
    write_full(&dst, &hbuf.slice()[..ALIGN], 0).map_err(|e| format!("write header: {e}"))?;
    dst.set_len(index_off + ilen as u64).map_err(|e| format!("trim image: {e}"))?;
    dst.sync_all().map_err(|e| format!("flush image: {e}"))?;
    Ok((hdr, stored.load(Ordering::Relaxed)))
}

fn read_header_and_index(f: &File) -> Result<(Header, Vec<Entry>), String> {
    let mut hb = AlignedBuf::new(ALIGN);
    read_full(f, &mut hb.slice()[..ALIGN], 0).map_err(|e| format!("read header: {e}"))?;
    let hdr = Header::decode(&hb.slice()[..ALIGN])?;
    let ilen = hdr.index_count as usize * ENTRY_SIZE;
    let mut ib = AlignedBuf::new(ilen.max(1));
    let padded = round_up(ilen as u64) as usize;
    read_full(f, &mut ib.slice()[..padded], hdr.index_off).map_err(|e| format!("read index: {e}"))?;
    let raw = &ib.slice()[..ilen];
    if blake3::hash(raw).as_bytes() != &hdr.index_hash {
        return Err("index hash mismatch (image is damaged)".into());
    }
    let entries = raw.as_chunks::<ENTRY_SIZE>().0.iter().map(|c| Entry::decode(c)).collect();
    Ok((hdr, entries))
}

/// Decodes one chunk from `image` into `out[..raw_len]` and checks its hash.
fn decode_chunk(image: &File, e: &Entry, rbuf: &mut AlignedBuf, out: &mut Vec<u8>) -> Result<(), String> {
    let padded = round_up(e.comp_len as u64) as usize;
    read_full(image, &mut rbuf.slice()[..padded], e.file_off)
        .map_err(|er| format!("read chunk at {}: {er}", e.file_off))?;
    let payload = &rbuf.slice()[..e.comp_len as usize];
    out.clear();
    if e.flags & FLAG_ZSTD != 0 {
        let d = zstd::bulk::decompress(payload, e.raw_len as usize).map_err(|er| format!("zstd chunk {}: {er}", e.src_off))?;
        out.extend_from_slice(&d);
    } else {
        out.extend_from_slice(payload);
    }
    if out.len() != e.raw_len as usize {
        return Err(format!("chunk {} size {} != {}", e.src_off, out.len(), e.raw_len));
    }
    if blake3::hash(out).as_bytes() != &e.hash {
        return Err(format!("chunk {} hash mismatch", e.src_off));
    }
    Ok(())
}

/// Checks every chunk of an image. Returns (chunks, raw bytes).
fn verify_image(path: &str, threads: usize) -> Result<(u64, u64), String> {
    let f = open_direct(path, false).map_err(|e| format!("open image: {e}"))?;
    let (hdr, entries) = read_header_and_index(&f)?;
    let total: u64 = entries.iter().map(|e| e.raw_len as u64).sum();
    if total != hdr.raw_bytes {
        return Err(format!("header says {} raw bytes, index has {}", hdr.raw_bytes, total));
    }
    if hdr.chunk_bytes == 0 || hdr.chunk_bytes > (1 << 30) {
        return Err(format!("implausible chunk size {}", hdr.chunk_bytes));
    }
    eprintln!("kova_engine: {} chunks, chunk {} bytes, {} threads", entries.len(), hdr.chunk_bytes, threads);
    let done = AtomicU64::new(0);
    let none = AtomicU64::new(0);
    let sh = Shared::new();
    thread::scope(|s| {
        for _ in 0..threads.max(1) {
            s.spawn(|| {
                let img = match open_direct(path, false) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open image: {e}")),
                };
                let mut rbuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
                let mut out = Vec::new();
                loop {
                    if sh.abort.load(Ordering::Relaxed) {
                        return;
                    }
                    let i = sh.next.fetch_add(1, Ordering::SeqCst);
                    let Some(e) = entries.get(i) else { return };
                    match decode_chunk(&img, e, &mut rbuf, &mut out) {
                        Ok(()) => {
                            done.fetch_add(e.raw_len as u64, Ordering::Relaxed);
                        }
                        Err(er) => return sh.fail(er),
                    }
                }
            });
        }
        progress_loop(&sh, &done, total, &none, "verify");
    });
    sh.result()?;
    Ok((entries.len() as u64, total))
}

fn is_device_path(p: &str) -> bool {
    p.starts_with("\\\\.\\") || p.starts_with("\\\\?\\")
}

fn mark_sparse(f: &File) -> Result<(), String> {
    let mut returned = 0u32;
    unsafe {
        DeviceIoControl(
            HANDLE(f.as_raw_handle()),
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            Some(&mut returned),
            None,
        )
    }
    .map_err(|e| format!("FSCTL_SET_SPARSE failed: {e}"))
}

/// Length in bytes of a volume/disk device (`\\.\W:`), via IOCTL_DISK_GET_LENGTH_INFO.
fn device_length(path: &str) -> Result<u64, String> {
    let f = OpenOptions::new()
        .read(true)
        .share_mode(7)
        .open(path)
        .map_err(|e| format!("open target for size: {e}"))?;
    let mut info = GET_LENGTH_INFORMATION::default();
    let mut returned = 0u32;
    unsafe {
        DeviceIoControl(
            HANDLE(f.as_raw_handle()),
            IOCTL_DISK_GET_LENGTH_INFO,
            None,
            0,
            Some(&mut info as *mut _ as *mut c_void),
            std::mem::size_of::<GET_LENGTH_INFORMATION>() as u32,
            Some(&mut returned),
            None,
        )
    }
    .map_err(|e| format!("IOCTL_DISK_GET_LENGTH_INFO failed: {e}"))?;
    u64::try_from(info.Length).map_err(|_| "negative device length".to_string())
}

/// Refuses a restore target that is smaller than the imaged volume.
fn check_target_size(target_len: u64, volume_bytes: u64) -> Result<(), String> {
    if target_len < volume_bytes {
        return Err(format!(
            "target is too small: {target_len} bytes < image volume {volume_bytes} bytes"
        ));
    }
    Ok(())
}

/// Locks and dismounts a mounted target volume so raw writes are not denied.
/// The returned handle must stay open until the restore finishes.
fn lock_target_volume(path: &str) -> Result<File, String> {
    let f = OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(7)
        .custom_flags(FILE_FLAG_NO_BUFFERING)
        .open(path)
        .map_err(|e| format!("open target for lock: {e}"))?;
    let mut returned = 0u32;
    for ctl in [FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME] {
        unsafe {
            DeviceIoControl(HANDLE(f.as_raw_handle()), ctl, None, 0, None, 0, Some(&mut returned), None)
        }
        .map_err(|e| format!("lock/dismount target volume failed (ctl {ctl:#x}): {e}"))?;
    }
    Ok(f)
}

/// Writes every chunk of `image` back at its original offset in `target`.
fn restore_image(image: &str, target: &str, threads: usize) -> Result<(u64, u64), String> {
    let f = open_direct(image, false).map_err(|e| format!("open image: {e}"))?;
    let (hdr, entries) = read_header_and_index(&f)?;
    if let Some(bad) = entries
        .iter()
        .find(|e| !e.src_off.is_multiple_of(ALIGN as u64) || !(e.raw_len as usize).is_multiple_of(ALIGN))
    {
        return Err(format!("chunk at {} is not 4096-aligned", bad.src_off));
    }
    let mut lock: Option<File> = None;
    if is_device_path(target) {
        check_target_size(device_length(target)?, hdr.volume_bytes)?;
        lock = Some(lock_target_volume(target)?);
    } else {
        let t = File::create(target).map_err(|e| format!("create target: {e}"))?;
        mark_sparse(&t)?;
        t.set_len(hdr.volume_bytes).map_err(|e| format!("size target: {e}"))?;
    }
    let total: u64 = entries.iter().map(|e| e.raw_len as u64).sum();
    let done = AtomicU64::new(0);
    let none = AtomicU64::new(0);
    let sh = Shared::new();
    thread::scope(|s| {
        for _ in 0..threads.max(1) {
            s.spawn(|| {
                let img = match open_direct(image, false) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open image: {e}")),
                };
                // A locked+dismounted volume cannot be reopened: reuse the lock handle.
                let own: File;
                let dst: &File = match &lock {
                    Some(l) => l,
                    None => {
                        own = match open_direct(target, true) {
                            Ok(f) => f,
                            Err(e) => return sh.fail(format!("open target: {e}")),
                        };
                        &own
                    }
                };
                let mut rbuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
                let mut wbuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
                let mut out = Vec::new();
                loop {
                    if sh.abort.load(Ordering::Relaxed) {
                        return;
                    }
                    let i = sh.next.fetch_add(1, Ordering::SeqCst);
                    let Some(e) = entries.get(i) else { return };
                    let res = decode_chunk(&img, e, &mut rbuf, &mut out).and_then(|_| {
                        let n = out.len();
                        wbuf.slice()[..n].copy_from_slice(&out);
                        write_full(dst, &wbuf.slice()[..n], e.src_off)
                            .map_err(|er| format!("write target at {}: {er}", e.src_off))
                    });
                    match res {
                        Ok(()) => {
                            done.fetch_add(e.raw_len as u64, Ordering::Relaxed);
                        }
                        Err(er) => return sh.fail(er),
                    }
                }
            });
        }
        progress_loop(&sh, &done, total, &none, "restore");
    });
    sh.result()?;
    Ok((entries.len() as u64, total))
}

/// Compares the byte range of every chunk between `source` and `target`.
fn compare_volumes(image: &str, source: &str, target: &str, threads: usize) -> Result<(u64, u64), String> {
    let f = open_direct(image, false).map_err(|e| format!("open image: {e}"))?;
    let (hdr, entries) = read_header_and_index(&f)?;
    let total: u64 = entries.iter().map(|e| e.raw_len as u64).sum();
    let done = AtomicU64::new(0);
    let none = AtomicU64::new(0);
    let sh = Shared::new();
    let mismatches: Mutex<(u64, Vec<String>)> = Mutex::new((0, Vec::new()));
    thread::scope(|s| {
        for _ in 0..threads.max(1) {
            s.spawn(|| {
                let a = match open_direct(source, false) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open source: {e}")),
                };
                let b = match open_direct(target, false) {
                    Ok(f) => f,
                    Err(e) => return sh.fail(format!("open target: {e}")),
                };
                let mut abuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
                let mut bbuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
                loop {
                    if sh.abort.load(Ordering::Relaxed) {
                        return;
                    }
                    let i = sh.next.fetch_add(1, Ordering::SeqCst);
                    let Some(e) = entries.get(i) else { return };
                    let n = e.raw_len as usize;
                    abuf.slice()[..n].fill(0);
                    bbuf.slice()[..n].fill(0);
                    let res = read_full(&a, &mut abuf.slice()[..n], e.src_off)
                        .map_err(|er| format!("read source {}: {er}", e.src_off))
                        .and_then(|_| {
                            read_full(&b, &mut bbuf.slice()[..n], e.src_off)
                                .map_err(|er| format!("read target {}: {er}", e.src_off))
                        })
                        .map(|_| {
                            let (x, y) = (&abuf.slice()[..n], &bbuf.slice()[..n]);
                            let src_ok = blake3::hash(x).as_bytes() == &e.hash;
                            if x != y || !src_ok {
                                let first = x.iter().zip(y).position(|(p, q)| p != q).unwrap_or(0);
                                let diff = x.iter().zip(y).filter(|(p, q)| p != q).count();
                                let dst_ok = blake3::hash(y).as_bytes() == &e.hash;
                                let mut m = mismatches.lock().unwrap();
                                m.0 += 1;
                                if m.1.len() < 50 {
                                    m.1.push(format!(
                                        "MISMATCH {}..{} first_diff_at={} diff_bytes={} source_matches_write_hash={} restored_matches_write_hash={}",
                                        e.src_off, e.src_off + n as u64, e.src_off + first as u64, diff, src_ok, dst_ok
                                    ));
                                }
                            }
                        });
                    match res {
                        Ok(()) => {
                            done.fetch_add(e.raw_len as u64, Ordering::Relaxed);
                        }
                        Err(er) => return sh.fail(er),
                    }
                }
            });
        }
        progress_loop(&sh, &done, total, &none, "compare");
    });
    sh.result()?;
    let (count, details) = mismatches.into_inner().unwrap();
    if count > 0 {
        for d in &details {
            eprintln!("{d}");
        }
        return Err(format!("{count} of {} chunks differ", entries.len()));
    }
    Ok((entries.len() as u64, total))
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

/// Removes `cut` ranges from `extents` (both sorted/unsorted byte ranges).
fn subtract_extents(extents: &[Extent], cut: &[Extent]) -> Vec<Extent> {
    let mut cut: Vec<Extent> = cut.to_vec();
    cut.sort_by_key(|e| e.start);
    let mut out = Vec::new();
    for e in extents {
        let (mut pos, end) = (e.start, e.start + e.len);
        for c in &cut {
            let (cs, ce) = (c.start, c.start + c.len);
            if ce <= pos || cs >= end {
                continue;
            }
            if cs > pos {
                out.push(Extent { start: pos, len: cs - pos });
            }
            pos = pos.max(ce);
            if pos >= end {
                break;
            }
        }
        if pos < end {
            out.push(Extent { start: pos, len: end - pos });
        }
    }
    out
}

fn enable_backup_privilege() {
    unsafe {
        let mut token = HANDLE::default();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &mut token).is_err() {
            return;
        }
        let mut tp = TOKEN_PRIVILEGES { PrivilegeCount: 1, ..Default::default() };
        if LookupPrivilegeValueW(None, w!("SeBackupPrivilege"), &mut tp.Privileges[0].Luid).is_ok() {
            tp.Privileges[0] = LUID_AND_ATTRIBUTES { Luid: tp.Privileges[0].Luid, Attributes: SE_PRIVILEGE_ENABLED };
            let _ = AdjustTokenPrivileges(token, false, Some(&tp), 0, None, None);
        }
        let _ = CloseHandle(token);
    }
}

/// Byte ranges (cluster aligned) occupied by one file, via FSCTL_GET_RETRIEVAL_POINTERS.
fn file_extents(f: &File, cluster: u64) -> Result<Vec<Extent>, String> {
    let h = HANDLE(f.as_raw_handle());
    let mut out = Vec::new();
    let mut start: i64 = 0;
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let mut returned = 0u32;
        let res = unsafe {
            DeviceIoControl(
                h,
                FSCTL_GET_RETRIEVAL_POINTERS,
                Some(&start as *const i64 as *const c_void),
                8,
                Some(buf.as_mut_ptr() as *mut c_void),
                buf.len() as u32,
                Some(&mut returned),
                None,
            )
        };
        let more = match res {
            Ok(()) => false,
            Err(e) if e.code() == ERROR_MORE_DATA => true,
            // 0x80070026 = end of file (no clusters allocated, e.g. resident data)
            Err(e) if e.code() == HRESULT::from_win32(38) => return Ok(out),
            Err(e) => return Err(format!("FSCTL_GET_RETRIEVAL_POINTERS failed: {e}")),
        };
        let count = u32::from_le_bytes(buf[0..4].try_into().unwrap()) as usize;
        let mut vcn = i64::from_le_bytes(buf[8..16].try_into().unwrap());
        for i in 0..count {
            let o = 16 + i * 16;
            let next = i64::from_le_bytes(buf[o..o + 8].try_into().unwrap());
            let lcn = i64::from_le_bytes(buf[o + 8..o + 16].try_into().unwrap());
            if lcn >= 0 {
                out.push(Extent { start: lcn as u64 * cluster, len: (next - vcn) as u64 * cluster });
            }
            vcn = next;
        }
        if !more || count == 0 {
            return Ok(out);
        }
        start = vcn;
    }
}

/// Finds the VSS diff-area files in the volume's "System Volume Information" and returns their
/// clusters. Windows keeps rewriting them while a snapshot exists, so an image that includes them
/// can never match the snapshot. A restored volume does not need them.
fn shadow_store_extents(source: &str, cluster: u64) -> Result<Vec<Extent>, String> {
    enable_backup_privilege();
    let dir = format!("{}\\System Volume Information", source.trim_end_matches('\\'));
    let mut out = Vec::new();
    let rd = std::fs::read_dir(&dir).map_err(|e| format!("list System Volume Information: {e}"))?;
    for ent in rd.flatten() {
        let name = ent.file_name().to_string_lossy().to_lowercase();
        if !name.ends_with(VSS_STORE_SUFFIX) {
            continue;
        }
        let f = OpenOptions::new()
            .access_mode(0x80) // FILE_READ_ATTRIBUTES
            .share_mode(7)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
            .open(ent.path())
            .map_err(|e| format!("open {name}: {e}"))?;
        out.extend(file_extents(&f, cluster)?);
    }
    Ok(out)
}

fn main() {
    let cli = Cli::parse();
    let r = match cli.cmd {
        Cmd::Write { source, target, limit_gb, threads, chunk_mb, level } => {
            cmd_write(&source, &target, limit_gb, threads, chunk_mb, level)
        }
        Cmd::Verify { image, threads } => cmd_verify(&image, threads),
        Cmd::Restore { image, target, threads } => {
            report("restored", restore_image(&image, &target, threads))
        }
        Cmd::Compare { image, source, target, threads } => {
            report("compared_equal", compare_volumes(&image, &source, &target, threads))
        }
    };
    if let Err(e) = r {
        eprintln!("ERROR: {e}");
        std::process::exit(1);
    }
}

fn cmd_write(source: &str, target: &str, limit_gb: u64, threads: usize, chunk_mb: usize, level: i32) -> Result<(), String> {
    if chunk_mb == 0 {
        return Err("chunk_mb must be at least 1".into());
    }
    let chunk = chunk_mb * 1024 * 1024;
    let vol = OpenOptions::new()
        .read(true)
        .share_mode(7)
        .open(source)
        .map_err(|e| format!("open source volume: {e}"))?;
    let (cluster, volume_bytes) = {
        let direct = open_direct(source, false).map_err(|e| format!("open source (direct): {e}"))?;
        read_boot_sector(&direct)?
    };
    let total_clusters = volume_bytes / cluster;
    let bitmap = read_bitmap(&vol, total_clusters)?;
    let mut used = bitmap_to_extents(&bitmap, total_clusters, cluster);
    match shadow_store_extents(source, cluster) {
        Ok(cut) => {
            let skipped: u64 = cut.iter().map(|e| e.len).sum();
            eprintln!("skipping {:.1} MiB of snapshot storage (VSS diff area)", skipped as f64 / 1048576.0);
            used = subtract_extents(&used, &cut);
        }
        Err(e) => eprintln!("warning: could not locate VSS storage ({e}); image may not match the snapshot"),
    }
    let extents = align_extents(&used, volume_bytes);
    let jobs = make_jobs(&extents, chunk as u64, limit_gb * 1024 * 1024 * 1024);
    let total: u64 = jobs.iter().map(|j| j.len).sum();
    eprintln!(
        "volume {:.1} GiB, writing {:.1} GiB ({} chunks), {} threads, zstd level {} -> {}",
        volume_bytes as f64 / 1073741824.0,
        total as f64 / 1073741824.0,
        jobs.len(),
        threads,
        level,
        target
    );
    let done = AtomicU64::new(0);
    let t0 = Instant::now();
    let opts = WriteOpts { threads, chunk, level };
    let (hdr, stored) = write_image(source, target, &jobs, volume_bytes, cluster, &opts, &done)?;
    let secs = t0.elapsed().as_secs_f64();
    println!(
        "{{\"raw_bytes\": {}, \"stored_bytes\": {}, \"ratio\": {:.2}, \"chunks\": {}, \"seconds\": {:.1}, \"raw_mb_s\": {:.0}, \"stored_mb_s\": {:.0}}}",
        hdr.raw_bytes,
        stored,
        hdr.raw_bytes as f64 / stored.max(1) as f64,
        hdr.index_count,
        secs,
        hdr.raw_bytes as f64 / 1048576.0 / secs,
        stored as f64 / 1048576.0 / secs
    );
    Ok(())
}

/// Prints a one-line JSON result for a (chunks, bytes) outcome.
fn report(label: &str, r: Result<(u64, u64), String>) -> Result<(), String> {
    let (chunks, bytes) = r?;
    println!("{{\"{label}_chunks\": {chunks}, \"raw_bytes\": {bytes}}}");
    Ok(())
}

fn cmd_verify(image: &str, threads: usize) -> Result<(), String> {
    let t0 = Instant::now();
    let (chunks, bytes) = verify_image(image, threads)?;
    let secs = t0.elapsed().as_secs_f64();
    println!(
        "{{\"verified_chunks\": {}, \"raw_bytes\": {}, \"seconds\": {:.1}, \"raw_mb_s\": {:.0}}}",
        chunks,
        bytes,
        secs,
        bytes as f64 / 1048576.0 / secs
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_size_check() {
        assert!(check_target_size(100, 100).is_ok());
        assert!(check_target_size(101, 100).is_ok());
        assert!(check_target_size(99, 100).unwrap_err().contains("too small"));
    }

    #[test]
    fn device_length_of_c_volume_is_positive() {
        // read-only query; skipped silently when the volume cannot be opened (non-admin)
        if let Ok(n) = device_length(r"\\.\C:") {
            assert!(n > 0);
        }
    }

    fn tmp(name: &str) -> String {
        std::env::temp_dir()
            .join(format!("kova_engine_{}_{}", std::process::id(), name))
            .to_string_lossy()
            .into_owned()
    }

    /// Half compressible, half pseudo-random data.
    fn sample(len: usize) -> Vec<u8> {
        (0..len)
            .map(|i| if i < len / 2 { (i / 4096) as u8 } else { (i as u32).wrapping_mul(2654435761).wrapping_shr(24) as u8 })
            .collect()
    }

    fn build(name: &str, level: i32) -> (String, String, Vec<u8>, Vec<Job>) {
        let src = tmp(&format!("{name}_src"));
        let img = tmp(&format!("{name}_img"));
        let data = sample(2 * 1024 * 1024);
        std::fs::write(&src, &data).unwrap();
        let extents = vec![
            Extent { start: 0, len: 8192 },
            Extent { start: 16384, len: 12288 },
            Extent { start: 1024 * 1024, len: 768 * 1024 },
        ];
        let jobs = make_jobs(&extents, 65536, 0);
        let done = AtomicU64::new(0);
        let o = WriteOpts { threads: 4, chunk: 65536, level };
        let (hdr, _) = write_image(&src, &img, &jobs, data.len() as u64, 4096, &o, &done).unwrap();
        assert_eq!(hdr.index_count as usize, jobs.len());
        (src, img, data, jobs)
    }

    #[test]
    fn roundtrip_compressed_and_raw() {
        for level in [0, 1, 3] {
            let (src, img, data, jobs) = build(&format!("rt{level}"), level);
            let (n, bytes) = verify_image(&img, 3).unwrap();
            assert_eq!(n as usize, jobs.len());
            assert_eq!(bytes, jobs.iter().map(|j| j.len).sum::<u64>());

            // decode and compare with the source bytes
            let f = open_direct(&img, false).unwrap();
            let (hdr, entries) = read_header_and_index(&f).unwrap();
            assert_eq!(hdr.volume_bytes, data.len() as u64);
            let mut rbuf = AlignedBuf::new(hdr.chunk_bytes as usize + ALIGN);
            let mut out = Vec::new();
            for e in &entries {
                decode_chunk(&f, e, &mut rbuf, &mut out).unwrap();
                assert_eq!(out, data[e.src_off as usize..e.src_off as usize + e.raw_len as usize]);
            }
            let _ = std::fs::remove_file(&src);
            let _ = std::fs::remove_file(&img);
        }
    }

    #[test]
    fn compression_actually_shrinks_compressible_data() {
        let (src, img, _data, _jobs) = build("shrink", 1);
        let raw = std::fs::metadata(&img).unwrap().len();
        // total raw payload is 8192+12288+786432 bytes; half of the sample is compressible
        assert!(raw < 2 * 1024 * 1024);
        let f = open_direct(&img, false).unwrap();
        let (_, entries) = read_header_and_index(&f).unwrap();
        assert!(entries.iter().any(|e| e.flags & FLAG_ZSTD != 0));
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
    }

    #[test]
    fn corrupted_chunk_is_detected() {
        let (src, img, _data, _jobs) = build("corrupt", 0);
        let mut bytes = std::fs::read(&img).unwrap();
        bytes[ALIGN + 10] ^= 0xFF; // first data byte region
        std::fs::write(&img, &bytes).unwrap();
        assert!(verify_image(&img, 2).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
    }

    #[test]
    fn corrupted_index_is_detected() {
        let (src, img, _data, _jobs) = build("badidx", 1);
        let f = open_direct(&img, false).unwrap();
        let (hdr, _) = read_header_and_index(&f).unwrap();
        drop(f);
        let mut bytes = std::fs::read(&img).unwrap();
        bytes[hdr.index_off as usize + 3] ^= 0x01;
        std::fs::write(&img, &bytes).unwrap();
        assert!(verify_image(&img, 2).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
    }

    #[test]
    fn restore_then_compare_matches_source() {
        let (src, img, data, _jobs) = build("restore", 1);
        let out = tmp("restore_out");
        let (n, bytes) = restore_image(&img, &out, 3).unwrap();
        assert!(n > 0 && bytes > 0);
        assert_eq!(std::fs::metadata(&out).unwrap().len(), data.len() as u64);
        let (cn, cb) = compare_volumes(&img, &src, &out, 3).unwrap();
        assert_eq!((cn, cb), (n, bytes));
        // restored bytes inside the chunk ranges equal the original
        let restored = std::fs::read(&out).unwrap();
        assert_eq!(restored[16384..28672], data[16384..28672]);
        // a flipped byte in a covered range must be reported
        let mut bad = restored.clone();
        bad[20000] ^= 0x55;
        std::fs::write(&out, &bad).unwrap();
        assert!(compare_volumes(&img, &src, &out, 2).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
        let _ = std::fs::remove_file(&out);
    }

    #[test]
    fn restore_rejects_damaged_chunk() {
        let (src, img, _data, _jobs) = build("restore_bad", 0);
        let mut bytes = std::fs::read(&img).unwrap();
        bytes[ALIGN + 5] ^= 0xFF;
        std::fs::write(&img, &bytes).unwrap();
        let out = tmp("restore_bad_out");
        assert!(restore_image(&img, &out, 2).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
        let _ = std::fs::remove_file(&out);
    }

    #[test]
    fn bad_magic_is_rejected() {
        let img = tmp("magic");
        std::fs::write(&img, vec![0u8; 8192]).unwrap();
        assert!(verify_image(&img, 1).is_err());
        let _ = std::fs::remove_file(&img);
    }

    #[test]
    fn read_past_end_is_error() {
        let src = tmp("short_src");
        let img = tmp("short_img");
        std::fs::write(&src, vec![7u8; 8192]).unwrap();
        let jobs = vec![Job { src: 0, len: 16384 }];
        let done = AtomicU64::new(0);
        let o = WriteOpts { threads: 2, chunk: 16384, level: 1 };
        assert!(write_image(&src, &img, &jobs, 8192, 4096, &o, &done).is_err());
        let _ = std::fs::remove_file(&src);
        let _ = std::fs::remove_file(&img);
    }

    #[test]
    fn entry_and_header_roundtrip() {
        let e = Entry { src_off: 5, raw_len: 4096, flags: 1, file_off: 8192, comp_len: 100, hash: [9; 32] };
        let mut v = Vec::new();
        e.encode(&mut v);
        assert_eq!(v.len(), ENTRY_SIZE);
        assert_eq!(Entry::decode(&v), e);
        let h = Header { volume_bytes: 1, cluster: 4096, chunk_bytes: 65536, index_off: 8192, index_count: 3, raw_bytes: 99, index_hash: [1; 32] };
        assert_eq!(Header::decode(&h.encode()).unwrap(), h);
    }

    #[test]
    fn subtract_removes_ranges() {
        let a = [Extent { start: 0, len: 100 }, Extent { start: 200, len: 100 }];
        let c = [Extent { start: 50, len: 20 }, Extent { start: 90, len: 130 }];
        assert_eq!(
            subtract_extents(&a, &c),
            vec![Extent { start: 0, len: 50 }, Extent { start: 70, len: 20 }, Extent { start: 220, len: 80 }]
        );
    }

    #[test]
    fn bitmap_runs_become_extents() {
        let bitmap = [0b0000_0111u8, 0b1111_1000u8];
        assert_eq!(
            bitmap_to_extents(&bitmap, 16, 4096),
            vec![Extent { start: 0, len: 3 * 4096 }, Extent { start: 11 * 4096, len: 5 * 4096 }]
        );
    }
}
