use std::error::Error;
use std::ffi::c_void;
use std::io::Write;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use windows::core::{HRESULT, PCWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_FLAG_NO_BUFFERING,
    FILE_FLAG_OVERLAPPED, FILE_FLAG_WRITE_THROUGH, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
    FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};
use windows::Win32::System::Memory::{
    VirtualAlloc, VirtualFree, MEM_COMMIT, MEM_RELEASE, MEM_RESERVE, PAGE_READWRITE,
};
use windows::Win32::System::IO::{
    CreateIoCompletionPort, GetQueuedCompletionStatus, OVERLAPPED,
};

const SECTOR_SIZE: usize = 4096;

pub struct CloneProgress {
    pub completed_bytes: u64,
    pub total_bytes: u64,
    pub mb_s: f64,
    pub percent: f64,
}

struct AlignedBuffer {
    ptr: *mut u8,
    size: usize,
}

unsafe impl Send for AlignedBuffer {}
unsafe impl Sync for AlignedBuffer {}

impl AlignedBuffer {
    fn new(size: usize) -> Result<Self, String> {
        let aligned_size = (size + SECTOR_SIZE - 1) & !(SECTOR_SIZE - 1);
        let ptr = unsafe {
            VirtualAlloc(
                None,
                aligned_size,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_READWRITE,
            )
        };
        if ptr.is_null() {
            return Err("VirtualAlloc failed to allocate memory".to_string());
        }
        Ok(Self {
            ptr: ptr as *mut u8,
            size: aligned_size,
        })
    }
}

impl Drop for AlignedBuffer {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            unsafe {
                let _ = VirtualFree(self.ptr as *mut c_void, 0, MEM_RELEASE);
            }
        }
    }
}

struct OverlappedSlot {
    buffer: AlignedBuffer,
    overlapped: Box<OVERLAPPED>,
    in_flight: bool,
    bytes_written: u32,
}

fn to_wide_chars(s: &str) -> Vec<u16> {
    Path::new(s).as_os_str().encode_wide().chain(std::iter::once(0)).collect()
}

/// Closes the handle on every exit path (early returns included).
struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

/// ERROR_IO_PENDING (997): the async I/O was submitted successfully.
const ERROR_IO_PENDING_HR: HRESULT = HRESULT::from_win32(997);

pub fn run_baremetal_clone(
    source_path: &str,
    target_path: &str,
    start_offset: u64,
    total_bytes: u64,
    queue_depth: usize,
    chunk_mb: usize,
    json: bool,
) -> Result<(), Box<dyn Error>> {
    // Direct I/O (NO_BUFFERING) requires sector alignment. An unaligned length would
    // overwrite past the requested range, so reject it before touching any device.
    if total_bytes == 0 {
        return Err("total_bytes must be greater than 0".into());
    }
    if start_offset % SECTOR_SIZE as u64 != 0 || total_bytes % SECTOR_SIZE as u64 != 0 {
        return Err(format!(
            "start_offset({}) and total_bytes({}) must be multiples of {} bytes",
            start_offset, total_bytes, SECTOR_SIZE
        )
        .into());
    }
    if chunk_mb == 0 {
        return Err("chunk_mb must be at least 1".into());
    }

    let chunk_bytes = chunk_mb * 1024 * 1024;
    let aligned_chunk = (chunk_bytes + SECTOR_SIZE - 1) & !(SECTOR_SIZE - 1);
    let qd = if queue_depth == 0 { 32 } else { queue_depth };

    eprintln!(
        "[RUST IOCP] Baremetal clone initialized: Source='{}' -> Target='{}'",
        source_path, target_path
    );
    eprintln!(
        "[RUST IOCP] QD={}, Chunk={}MB (Aligned {} bytes), Total={} bytes ({:.2} GB)",
        qd,
        chunk_mb,
        aligned_chunk,
        total_bytes,
        total_bytes as f64 / (1024.0 * 1024.0 * 1024.0)
    );

    let src_wide = to_wide_chars(source_path);
    let tgt_wide = to_wide_chars(target_path);

    // Source Open (Direct I/O)
    let src_handle = OwnedHandle(
        unsafe {
            CreateFileW(
                PCWSTR(src_wide.as_ptr()),
                0x80000000, // GENERIC_READ
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_NO_BUFFERING,
                HANDLE::default(),
            )
        }
        .map_err(|e| format!("CreateFileW(Source) failed: {:?}", e))?,
    );

    if src_handle.0 == INVALID_HANDLE_VALUE {
        return Err(format!("Failed to open source device: {}", source_path).into());
    }

    // Target Open (Direct I/O + Overlapped + WriteThrough)
    let tgt_handle = OwnedHandle(
        unsafe {
            CreateFileW(
                PCWSTR(tgt_wide.as_ptr()),
                0x80000000 | 0x40000000, // GENERIC_READ | GENERIC_WRITE
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH | FILE_FLAG_OVERLAPPED,
                HANDLE::default(),
            )
        }
        .map_err(|e| format!("CreateFileW(Target) failed: {:?}", e))?,
    );

    if tgt_handle.0 == INVALID_HANDLE_VALUE {
        return Err(format!("Failed to open target device: {}", target_path).into());
    }

    // IOCP creation
    let iocp = OwnedHandle(
        unsafe { CreateIoCompletionPort(tgt_handle.0, HANDLE::default(), 1, 0) }
            .map_err(|e| format!("CreateIoCompletionPort failed: {:?}", e))?,
    );

    if iocp.0 == INVALID_HANDLE_VALUE {
        return Err("Failed to create IOCP port".into());
    }

    // Slot allocation
    let mut slots: Vec<OverlappedSlot> = Vec::with_capacity(qd);
    for _ in 0..qd {
        let buf = AlignedBuffer::new(aligned_chunk).map_err(|e| e.to_string())?;
        let ov = Box::new(unsafe { std::mem::zeroed::<OVERLAPPED>() });
        slots.push(OverlappedSlot {
            buffer: buf,
            overlapped: ov,
            in_flight: false,
            bytes_written: 0,
        });
    }

    let completed_bytes = Arc::new(AtomicU64::new(0));
    let stop_monitor = Arc::new(AtomicBool::new(false));

    // Progress monitor thread
    let mon_completed = Arc::clone(&completed_bytes);
    let mon_stop = Arc::clone(&stop_monitor);
    let mon_total = total_bytes;
    let monitor_thread = thread::spawn(move || {
        let mut last_bytes = 0u64;
        let mut last_time = Instant::now();

        while !mon_stop.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(1000));
            let current = mon_completed.load(Ordering::Relaxed);
            let elapsed_sec = last_time.elapsed().as_secs_f64();
            if elapsed_sec >= 0.5 {
                let delta = current.saturating_sub(last_bytes);
                let mb_s = (delta as f64 / (1024.0 * 1024.0)) / elapsed_sec;
                let pct = if mon_total > 0 {
                    (current as f64 / mon_total as f64) * 100.0
                } else {
                    0.0
                };

                if json {
                    println!(
                        "{{\"completed_bytes\": {}, \"total_bytes\": {}, \"mb_s\": {:.2}, \"percent\": {:.2}}}",
                        current, mon_total, mb_s, pct
                    );
                    let _ = std::io::stdout().flush();
                } else {
                    eprintln!(
                        "[RUST IOCP] Progress: {:.2}% ({:.2}/{:.2} GB) - Instant Speed: {:.2} MB/s",
                        pct,
                        current as f64 / (1024.0 * 1024.0 * 1024.0),
                        mon_total as f64 / (1024.0 * 1024.0 * 1024.0),
                        mb_s
                    );
                }
                last_bytes = current;
                last_time = Instant::now();
            }
        }
    });

    let mut current_offset = start_offset;
    let end_offset = start_offset + total_bytes;
    let mut in_flight_count = 0usize;
    let mut free_slots: Vec<usize> = (0..qd).collect();
    // First failure reason. Once set, no new I/O is submitted and only in-flight I/O is reaped.
    let mut failure: Option<String> = None;

    // Buffers and OVERLAPPED structs must outlive their I/O, so keep looping until
    // in_flight_count reaches 0 even after a failure.
    while (failure.is_none() && current_offset < end_offset) || in_flight_count > 0 {
        // Submit writes as long as slots are available and there is data to read
        while failure.is_none() && current_offset < end_offset && !free_slots.is_empty() {
            let slot_idx = free_slots.pop().unwrap();
            let remaining = end_offset - current_offset;
            let read_req = std::cmp::min(remaining, aligned_chunk as u64) as usize;
            let aligned_read = (read_req + SECTOR_SIZE - 1) & !(SECTOR_SIZE - 1);

            let mut bytes_read = 0u32;
            let read_slice = unsafe {
                std::slice::from_raw_parts_mut(slots[slot_idx].buffer.ptr, aligned_read)
            };

            // Set file pointer for source read (source handle is synchronous)
            let mut read_ov = unsafe { std::mem::zeroed::<OVERLAPPED>() };
            read_ov.Anonymous.Anonymous.Offset = (current_offset & 0xFFFF_FFFF) as u32;
            read_ov.Anonymous.Anonymous.OffsetHigh = ((current_offset >> 32) & 0xFFFF_FFFF) as u32;

            let read_res = unsafe {
                ReadFile(
                    src_handle.0,
                    Some(read_slice),
                    Some(&mut bytes_read),
                    Some(&mut read_ov),
                )
            };

            // The source handle is synchronous, so any error here is a real failure.
            if let Err(e) = read_res {
                eprintln!("[RUST IOCP ERROR] ReadFile failed at offset {}: {:?}", current_offset, e);
                failure = Some(format!("ReadFile failed at offset {}: {:?}", current_offset, e));
                free_slots.push(slot_idx);
                break;
            }
            // A short read (EOF etc.) would write stale buffer contents, so fail instead.
            if bytes_read as usize != aligned_read {
                eprintln!(
                    "[RUST IOCP ERROR] Short read at offset {}: expected {}, got {}",
                    current_offset, aligned_read, bytes_read
                );
                failure = Some(format!(
                    "Short read at offset {}: expected {} bytes, got {}",
                    current_offset, aligned_read, bytes_read
                ));
                free_slots.push(slot_idx);
                break;
            }

            // Prepare Overlapped write to target
            let slot = &mut slots[slot_idx];
            slot.overlapped.Internal = 0;
            slot.overlapped.InternalHigh = 0;
            slot.overlapped.Anonymous.Anonymous.Offset = (current_offset & 0xFFFF_FFFF) as u32;
            slot.overlapped.Anonymous.Anonymous.OffsetHigh = ((current_offset >> 32) & 0xFFFF_FFFF) as u32;
            slot.in_flight = true;
            slot.bytes_written = aligned_read as u32;

            let write_buf = unsafe {
                std::slice::from_raw_parts(slot.buffer.ptr, aligned_read)
            };

            let write_res = unsafe {
                WriteFile(
                    tgt_handle.0,
                    Some(write_buf),
                    None,
                    Some(&mut *slot.overlapped),
                )
            };

            if let Err(e) = write_res {
                if e.code() != ERROR_IO_PENDING_HR {
                    eprintln!("[RUST IOCP ERROR] WriteFile failed at offset {}: {:?}", current_offset, e);
                    failure = Some(format!("WriteFile failed at offset {}: {:?}", current_offset, e));
                    slots[slot_idx].in_flight = false;
                    free_slots.push(slot_idx);
                    break;
                }
            }

            in_flight_count += 1;
            current_offset += read_req as u64;
        }

        // Collect one completion from IOCP
        if in_flight_count > 0 {
            let mut bytes_transferred = 0u32;
            let mut completion_key = 0usize;
            let mut p_overlapped: *mut OVERLAPPED = std::ptr::null_mut();

            let status = unsafe {
                GetQueuedCompletionStatus(
                    iocp.0,
                    &mut bytes_transferred,
                    &mut completion_key,
                    &mut p_overlapped,
                    1000,
                )
            };

            // A null OVERLAPPED means timeout (or a port error): just wait again.
            if !p_overlapped.is_null() {
                if let Some(idx) = (0..qd).find(|&i| {
                    slots[i].in_flight && (&*slots[i].overlapped as *const OVERLAPPED) == p_overlapped
                }) {
                    slots[idx].in_flight = false;
                    free_slots.push(idx);
                    in_flight_count = in_flight_count.saturating_sub(1);

                    if let Err(e) = status {
                        eprintln!("[RUST IOCP ERROR] Write completion failed: {:?}", e);
                        failure.get_or_insert_with(|| format!("Write completion failed: {:?}", e));
                    } else if bytes_transferred != slots[idx].bytes_written {
                        eprintln!(
                            "[RUST IOCP ERROR] Partial write: expected {}, wrote {}",
                            slots[idx].bytes_written, bytes_transferred
                        );
                        let expected = slots[idx].bytes_written;
                        failure.get_or_insert_with(|| {
                            format!(
                                "Partial write: expected {} bytes, wrote {}",
                                expected, bytes_transferred
                            )
                        });
                    } else {
                        completed_bytes.fetch_add(slots[idx].bytes_written as u64, Ordering::Relaxed);
                    }
                }
            }
        }
    }

    stop_monitor.store(true, Ordering::Relaxed);
    let _ = monitor_thread.join();

    // Handles are closed by OwnedHandle::drop (iocp, target, source).
    let copied = completed_bytes.load(Ordering::Relaxed);

    if let Some(reason) = failure {
        return Err(format!(
            "Baremetal clone failed: {} (copied {} of {} bytes)",
            reason, copied, total_bytes
        )
        .into());
    }
    if copied != total_bytes {
        return Err(format!(
            "Baremetal clone incomplete: copied {} of {} bytes",
            copied, total_bytes
        )
        .into());
    }

    eprintln!(
        "[RUST IOCP] Baremetal clone completed successfully! Total bytes copied: {}",
        copied
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    const MB: usize = 1024 * 1024;

    struct TempFiles(Vec<PathBuf>);

    impl Drop for TempFiles {
        fn drop(&mut self) {
            for p in &self.0 {
                let _ = fs::remove_file(p);
            }
        }
    }

    fn temp_path(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("kova_bm_test_{}_{}", std::process::id(), tag))
    }

    fn make_source(path: &PathBuf, size: usize) -> Vec<u8> {
        let data: Vec<u8> = (0..size).map(|i| ((i * 31 + i / 4096) % 251) as u8).collect();
        fs::write(path, &data).unwrap();
        data
    }

    fn make_target(path: &PathBuf, size: usize) {
        let f = fs::File::create(path).unwrap();
        f.set_len(size as u64).unwrap();
    }

    #[test]
    fn clones_file_exactly() {
        let src = temp_path("ok_src");
        let tgt = temp_path("ok_tgt");
        let _g = TempFiles(vec![src.clone(), tgt.clone()]);
        let data = make_source(&src, 8 * MB);
        make_target(&tgt, 8 * MB);

        run_baremetal_clone(src.to_str().unwrap(), tgt.to_str().unwrap(), 0, (8 * MB) as u64, 4, 1, false)
            .expect("clone should succeed");

        assert_eq!(fs::read(&tgt).unwrap(), data);
    }

    #[test]
    fn short_read_is_error() {
        let src = temp_path("short_src");
        let tgt = temp_path("short_tgt");
        let _g = TempFiles(vec![src.clone(), tgt.clone()]);
        make_source(&src, 4 * MB);
        make_target(&tgt, 8 * MB);

        let res = run_baremetal_clone(src.to_str().unwrap(), tgt.to_str().unwrap(), 0, (8 * MB) as u64, 4, 1, false);
        assert!(res.is_err(), "reading past source end must fail, not report success");
    }

    #[test]
    fn unaligned_length_is_rejected() {
        let src = temp_path("unal_src");
        let tgt = temp_path("unal_tgt");
        let _g = TempFiles(vec![src.clone(), tgt.clone()]);
        make_source(&src, MB);
        make_target(&tgt, MB);

        let res = run_baremetal_clone(src.to_str().unwrap(), tgt.to_str().unwrap(), 0, 5000, 4, 1, false);
        assert!(res.is_err());
    }

    #[test]
    fn zero_length_is_rejected() {
        let res = run_baremetal_clone("unused", "unused", 0, 0, 4, 1, false);
        assert!(res.is_err());
    }

    #[test]
    fn missing_source_is_error() {
        let tgt = temp_path("miss_tgt");
        let _g = TempFiles(vec![tgt.clone()]);
        make_target(&tgt, MB);

        let res = run_baremetal_clone(
            temp_path("does_not_exist").to_str().unwrap(),
            tgt.to_str().unwrap(),
            0,
            MB as u64,
            4,
            1,
            false,
        );
        assert!(res.is_err());
    }
}
