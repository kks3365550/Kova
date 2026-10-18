use std::error::Error;
use std::ffi::c_void;
use std::io::Write;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use windows::core::PCWSTR;
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

pub fn run_baremetal_clone(
    source_path: &str,
    target_path: &str,
    start_offset: u64,
    total_bytes: u64,
    queue_depth: usize,
    chunk_mb: usize,
    json: bool,
) -> Result<(), Box<dyn Error>> {
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
    let src_handle = unsafe {
        CreateFileW(
            PCWSTR(src_wide.as_ptr()),
            0x80000000, // GENERIC_READ
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_NO_BUFFERING,
            HANDLE::default(),
        )
    }.map_err(|e| format!("CreateFileW(Source) failed: {:?}", e))?;

    if src_handle == INVALID_HANDLE_VALUE {
        return Err(format!("Failed to open source device: {}", source_path).into());
    }

    // Target Open (Direct I/O + Overlapped + WriteThrough)
    let tgt_handle = unsafe {
        CreateFileW(
            PCWSTR(tgt_wide.as_ptr()),
            0x80000000 | 0x40000000, // GENERIC_READ | GENERIC_WRITE
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH | FILE_FLAG_OVERLAPPED,
            HANDLE::default(),
        )
    }.map_err(|e| format!("CreateFileW(Target) failed: {:?}", e))?;

    if tgt_handle == INVALID_HANDLE_VALUE {
        unsafe { let _ = CloseHandle(src_handle); }
        return Err(format!("Failed to open target device: {}", target_path).into());
    }

    // IOCP creation
    let iocp = unsafe {
        CreateIoCompletionPort(tgt_handle, HANDLE::default(), 1, 0)
    }?;

    if iocp == INVALID_HANDLE_VALUE {
        unsafe {
            let _ = CloseHandle(src_handle);
            let _ = CloseHandle(tgt_handle);
        }
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
        let start_time = Instant::now();

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

    while current_offset < end_offset || in_flight_count > 0 {
        // Submit writes as long as slots are available and there is data to read
        while current_offset < end_offset && !free_slots.is_empty() {
            let slot_idx = free_slots.pop().unwrap();
            let remaining = end_offset - current_offset;
            let read_req = std::cmp::min(remaining, aligned_chunk as u64) as usize;
            let aligned_read = (read_req + SECTOR_SIZE - 1) & !(SECTOR_SIZE - 1);

            let mut bytes_read = 0u32;
            let read_slice = unsafe {
                std::slice::from_raw_parts_mut(slots[slot_idx].buffer.ptr, aligned_read)
            };

            // Set file pointer for source read
            let mut read_ov = unsafe { std::mem::zeroed::<OVERLAPPED>() };
            read_ov.Anonymous.Anonymous.Offset = (current_offset & 0xFFFF_FFFF) as u32;
            read_ov.Anonymous.Anonymous.OffsetHigh = ((current_offset >> 32) & 0xFFFF_FFFF) as u32;

            let read_res = unsafe {
                ReadFile(
                    src_handle,
                    Some(read_slice),
                    Some(&mut bytes_read),
                    Some(&mut read_ov),
                )
            };

            if let Err(e) = read_res {
                let code = windows::core::Error::from_win32().code().0 as u32;
                // ERROR_IO_PENDING = 997
                if code != 997 {
                    eprintln!("[RUST IOCP ERROR] ReadFile failed at offset {}: {:?}", current_offset, e);
                    break;
                }
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
                    tgt_handle,
                    Some(write_buf),
                    None,
                    Some(&mut *slot.overlapped),
                )
            };

            if let Err(e) = write_res {
                let code = windows::core::Error::from_win32().code().0 as u32;
                if code != 997 {
                    eprintln!("[RUST IOCP ERROR] WriteFile failed at offset {}: {:?}", current_offset, e);
                    slots[slot_idx].in_flight = false;
                    free_slots.push(slot_idx);
                    break;
                }
            }

            in_flight_count += 1;
            current_offset += read_req as u64;
        }

        // Collect at least one completion from IOCP
        if in_flight_count > 0 {
            let mut bytes_transferred = 0u32;
            let mut completion_key = 0usize;
            let mut p_overlapped: *mut OVERLAPPED = std::ptr::null_mut();

            let status = unsafe {
                GetQueuedCompletionStatus(
                    iocp,
                    &mut bytes_transferred,
                    &mut completion_key,
                    &mut p_overlapped,
                    1000,
                )
            };

            if status.is_ok() && !p_overlapped.is_null() {
                // Find matching slot by overlapped pointer
                for idx in 0..qd {
                    if (&*slots[idx].overlapped as *const OVERLAPPED) == p_overlapped {
                        slots[idx].in_flight = false;
                        completed_bytes.fetch_add(slots[idx].bytes_written as u64, Ordering::Relaxed);
                        free_slots.push(idx);
                        in_flight_count = in_flight_count.saturating_sub(1);
                        break;
                    }
                }
            }
        }
    }

    // Drain remaining in-flight completions
    while in_flight_count > 0 {
        let mut bytes_transferred = 0u32;
        let mut completion_key = 0usize;
        let mut p_overlapped: *mut OVERLAPPED = std::ptr::null_mut();

        let _ = unsafe {
            GetQueuedCompletionStatus(
                iocp,
                &mut bytes_transferred,
                &mut completion_key,
                &mut p_overlapped,
                5000,
            )
        };

        if !p_overlapped.is_null() {
            for idx in 0..qd {
                if slots[idx].in_flight && (&*slots[idx].overlapped as *const OVERLAPPED) == p_overlapped {
                    slots[idx].in_flight = false;
                    completed_bytes.fetch_add(slots[idx].bytes_written as u64, Ordering::Relaxed);
                    in_flight_count = in_flight_count.saturating_sub(1);
                    break;
                }
            }
        } else {
            break;
        }
    }

    stop_monitor.store(true, Ordering::Relaxed);
    let _ = monitor_thread.join();

    unsafe {
        let _ = CloseHandle(iocp);
        let _ = CloseHandle(src_handle);
        let _ = CloseHandle(tgt_handle);
    }

    eprintln!(
        "[RUST IOCP] Baremetal clone completed successfully! Total bytes copied: {}",
        completed_bytes.load(Ordering::Relaxed)
    );
    Ok(())
}
