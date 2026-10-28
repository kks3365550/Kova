import os
import stat as stat_mod
import zlib
import json
import hashlib
import shutil
import threading
from typing import Optional, Tuple, Dict, Any, Set, List
from core.hasher import calculate_sha256, calculate_bytes_sha256

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

class InsufficientDiskSpaceError(Exception):
    """Raised when repository disk free space is below the safety threshold (Fail-Closed safeguard)."""
    pass

from core.worm import lock_file_immutable, unlock_file_writable
from core.logging_setup import get_logger

log = get_logger("core.storage")

def get_disk_free_gb(path: str) -> float:
    """Returns free disk space in gigabytes for the volume containing path."""
    try:
        # If path does not exist, traverse up to find the nearest existing parent directory
        # to ensure accurate disk usage measurement for the target volume.
        check_path = path
        while not os.path.exists(check_path):
            parent = os.path.dirname(check_path)
            if parent == check_path:  # Reached root
                break
            check_path = parent
        
        total, used, free = shutil.disk_usage(check_path)
        return round(free / (1024 ** 3), 2)
    except Exception:
        # Fail-Closed: Return 0.0 to trigger InsufficientDiskSpaceError and prevent unsafe operations
        return 0.0

def verify_disk_space_or_fail(path: str, min_free_gb: float = 10.0) -> float:
    """
    Fail-Closed Disk Space Verification:
    Ensures that the destination volume has at least min_free_gb remaining.
    If free space is insufficient, raises InsufficientDiskSpaceError immediately to prevent
    partial backups and NEVER deletes existing snapshots.
    """
    free_gb = get_disk_free_gb(path)
    if free_gb < min_free_gb:
        try:
            from core.windows_toast import notify_disk_space_warning
            notify_disk_space_warning(free_gb, min_free_gb, path)
        except Exception:
            pass
        raise InsufficientDiskSpaceError(
            f"저장소 여유 공간 부족 (현재: {free_gb:.2f}GB < 최소 안전 여유량: {min_free_gb:.2f}GB). "
            f"기존 백업 체인을 안전하게 보존하기 위해 백업 작업을 즉시 거부/중단(Fail-Closed)합니다."
        )
    return free_gb

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Performance optimization: Level 1 for 2x faster compression with <3% ratio trade-off
DEFAULT_COMPRESS_LEVEL = 1
# 4MB streaming buffer to reduce Windows I/O syscalls by 75%
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
ONEPASS_MEMORY_THRESHOLD = 16 * 1024 * 1024

class BlobStorage:
    # Fix #8: Class-level per-repo-dir cache so all BlobStorage instances in the same process share it.
    # Previously, each new BlobStorage() started with an empty cache — defeating the purpose.
    _class_blob_caches: Dict[str, Set[str]] = {}
    _class_cache_lock: threading.Lock = threading.Lock()

    def __init__(self, repo_dir: str, crypto_engine: Optional[Any] = None):
        self.repo_dir = os.path.abspath(repo_dir)
        self.blobs_dir = os.path.join(self.repo_dir, "blobs")
        self.snapshots_dir = os.path.join(self.repo_dir, "snapshots")
        self.packs_dir = os.path.join(self.repo_dir, "packs")
        self.meta_file = os.path.join(self.repo_dir, "repo_meta.json")
        self.crypto_engine = crypto_engine
        self._composite_facade = None
        self.init_repo()
        from core.metadata_db import MetadataDB
        self.db = MetadataDB(self.repo_dir)
        # Use shared class-level cache for this repo
        with BlobStorage._class_cache_lock:
            if self.repo_dir not in BlobStorage._class_blob_caches:
                BlobStorage._class_blob_caches[self.repo_dir] = set()
        self._blob_cache = BlobStorage._class_blob_caches[self.repo_dir]
        self._cache_lock = BlobStorage._class_cache_lock
        self._pack_writer_lock = threading.RLock()

    # Class-level facade cache so all BlobStorage and PackConsolidator instances share pack index map
    _class_facades: Dict[str, Any] = {}

    @property
    def composite_facade(self):
        with BlobStorage._class_cache_lock:
            if self.repo_dir not in BlobStorage._class_facades or BlobStorage._class_facades[self.repo_dir] is None:
                from core.composite_storage import CompositeStorageFacade
                BlobStorage._class_facades[self.repo_dir] = CompositeStorageFacade(
                    self.repo_dir, crypto_engine=self.crypto_engine, blob_storage=self
                )
            return BlobStorage._class_facades[self.repo_dir]

    def init_repo(self):
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(self.packs_dir, exist_ok=True)
        os.makedirs(os.path.join(self.blobs_dir, "_temp"), exist_ok=True)
        # Pre-create 256 hex prefix directories (00..ff) once
        # Eliminates 220,000 os.makedirs system calls during full backups on Windows NTFS
        for i in range(256):
            os.makedirs(os.path.join(self.blobs_dir, f"{i:02x}"), exist_ok=True)

        # Self-Healing: Clean up orphaned temporary files left behind by power loss or crashes
        try:
            self.cleanup_orphaned_tmp_files(min_age_seconds=60.0)
        except Exception:
            pass

        if not os.path.exists(self.meta_file):
            meta = {
                "version": "2.0.0",
                "compression": "zstd" if HAS_ZSTD else "zlib",
                "format": "content-addressable-snapshots"
            }
            with open(self.meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

    # Class-level throttle for orphan cleanup so repo scans don't occur on every BlobStorage instantiation
    _last_cleanup_times: Dict[str, float] = {}

    def cleanup_orphaned_tmp_files(self, min_age_seconds: float = 60.0, force: bool = False) -> int:
        """
        Self-Healing: Detects and removes orphaned temporary files (.tmp_*, _temp/*)
        left behind by power loss, SIGKILL, or system crash.
        Throttled to run at most once every 300 seconds per repository unless force=True.
        Returns the count of cleaned up files.
        """
        import glob
        import time
        now = time.time()

        if not force:
            with BlobStorage._class_cache_lock:
                last_time = BlobStorage._last_cleanup_times.get(self.repo_dir, 0.0)
                if (now - last_time) < 300.0:
                    return 0
                BlobStorage._last_cleanup_times[self.repo_dir] = now

        cleaned_count = 0

        # 1. Clean up _temp directory
        temp_dir = os.path.join(self.blobs_dir, "_temp")
        if os.path.exists(temp_dir):
            for file_path in glob.glob(os.path.join(temp_dir, "*")):
                if os.path.isfile(file_path):
                    try:
                        if (now - os.path.getmtime(file_path)) >= min_age_seconds:
                            unlock_file_writable(file_path)
                            os.remove(file_path)
                            cleaned_count += 1
                    except OSError:
                        pass

        # 2. Clean up .tmp_* files in blobs/xx/ directories
        for prefix_dir in glob.glob(os.path.join(self.blobs_dir, "[0-9a-f][0-9a-f]")):
            if not os.path.isdir(prefix_dir):
                continue
            for file_path in glob.glob(os.path.join(prefix_dir, "*.tmp_*")):
                if os.path.isfile(file_path):
                    try:
                        if (now - os.path.getmtime(file_path)) >= min_age_seconds:
                            unlock_file_writable(file_path)
                            os.remove(file_path)
                            cleaned_count += 1
                    except OSError:
                        pass

        # 3. Clean up .tmp_* in snapshots directory
        if os.path.exists(self.snapshots_dir):
            for file_path in glob.glob(os.path.join(self.snapshots_dir, "*.tmp_*")):
                if os.path.isfile(file_path):
                    try:
                        if (now - os.path.getmtime(file_path)) >= min_age_seconds:
                            unlock_file_writable(file_path)
                            os.remove(file_path)
                            cleaned_count += 1
                    except OSError:
                        pass

        return cleaned_count

    def get_blob_rel_path(self, sha256_hash: str) -> str:
        if not sha256_hash:
            return ""
        prefix = sha256_hash[:2]
        return os.path.join(prefix, f"{sha256_hash}.blob")

    def get_blob_abs_path(self, sha256_hash: str) -> str:
        if not sha256_hash:
            return ""
        return os.path.join(self.blobs_dir, self.get_blob_rel_path(sha256_hash))

    def has_blob(self, sha256_hash: str) -> bool:
        if not sha256_hash:
            return False
        # Dual-Read: Fast Pack in-memory index check first
        if sha256_hash.lower() in self.composite_facade.pack_index_map:
            return True
        # Fast lock-free lookup for existing in-memory cache
        if sha256_hash in self._blob_cache:
            return True
        blob_path = self.get_blob_abs_path(sha256_hash)
        if blob_path and os.path.exists(blob_path):
            with self._cache_lock:
                self._blob_cache.add(sha256_hash)
            return True
        return False

    def bulk_add_blob_cache(self, blob_ids) -> None:
        """Pre-populates the in-memory blob cache to eliminate disk syscalls (os.path.exists) during diffing."""
        valid_ids = {b for b in blob_ids if b}
        if not valid_ids:
            return
        # Only cache ids that really exist (pack map or blob file); a stale id
        # would make has_blob() skip re-writing a missing blob forever.
        pack_map = self.composite_facade.pack_index_map
        confirmed = {b for b in valid_ids if b.lower() in pack_map}
        by_prefix = {}
        for b in valid_ids - confirmed:
            by_prefix.setdefault(b[:2], []).append(b)
        for prefix, ids in by_prefix.items():
            try:
                with os.scandir(os.path.join(self.blobs_dir, prefix)) as it:
                    names = {e.name[:-5] for e in it if e.name.endswith(".blob")}
            except OSError:
                continue
            confirmed.update(b for b in ids if b in names)
        if confirmed:
            with self._cache_lock:
                self._blob_cache.update(confirmed)

    def put_file_blob(self, filepath: str, sha256_hash: Optional[str] = None, compress_level: int = DEFAULT_COMPRESS_LEVEL, read_path: Optional[str] = None) -> Tuple[str, int, int, bool]:
        """
        Compresses and saves a file as a content-addressed blob.
        Uses Zstandard if available (high speed & high compression), otherwise zlib.
        Returns (sha256_hash, original_size, stored_size, is_new_blob).
        """
        actual_path = read_path if read_path else filepath
        orig_size = os.path.getsize(actual_path)
        if not sha256_hash:
            sha256_hash = calculate_sha256(actual_path)

        blob_path = self.get_blob_abs_path(sha256_hash)

        # Deduplication check: if blob already exists, skip writing!
        if self.has_blob(sha256_hash):
            stored_size = os.path.getsize(blob_path)
            return sha256_hash, orig_size, stored_size, False

        temp_blob_path = blob_path + f".tmp_{os.getpid()}_{os.urandom(3).hex()}"

        stored_size = 0
        try:
            with open(actual_path, "rb") as fin, open(temp_blob_path, "wb") as fout:
                if HAS_ZSTD:
                    cctx = zstd.ZstdCompressor(level=compress_level)
                    with cctx.stream_writer(fout, closefd=False) as compressor:
                        while True:
                            chunk = fin.read(DEFAULT_CHUNK_SIZE)
                            if not chunk:
                                break
                            compressor.write(chunk)
                else:
                    compressor = zlib.compressobj(level=compress_level if compress_level in range(1, 10) else 6)
                    while True:
                        chunk = fin.read(DEFAULT_CHUNK_SIZE)
                        if not chunk:
                            break
                        compressed_chunk = compressor.compress(chunk)
                        if compressed_chunk:
                            fout.write(compressed_chunk)
                    tail = compressor.flush()
                    if tail:
                        fout.write(tail)
        except FileNotFoundError:
            os.makedirs(os.path.dirname(blob_path), exist_ok=True)
            with open(actual_path, "rb") as fin, open(temp_blob_path, "wb") as fout:
                if HAS_ZSTD:
                    cctx = zstd.ZstdCompressor(level=compress_level)
                    with cctx.stream_writer(fout, closefd=False) as compressor:
                        while True:
                            chunk = fin.read(DEFAULT_CHUNK_SIZE)
                            if not chunk:
                                break
                            compressor.write(chunk)
                else:
                    compressor = zlib.compressobj(level=compress_level if compress_level in range(1, 10) else 6)
                    while True:
                        chunk = fin.read(DEFAULT_CHUNK_SIZE)
                        if not chunk:
                            break
                        compressed_chunk = compressor.compress(chunk)
                        if compressed_chunk:
                            fout.write(compressed_chunk)
                    tail = compressor.flush()
                    if tail:
                        fout.write(tail)

        stored_size = os.path.getsize(temp_blob_path)
        os.replace(temp_blob_path, blob_path)
        lock_file_immutable(blob_path)
        with self._cache_lock:
            self._blob_cache.add(sha256_hash)
        return sha256_hash, orig_size, stored_size, True

    def put_file_blob_onepass(self, filepath: str, compress_level: int = DEFAULT_COMPRESS_LEVEL, cancel_event=None, read_path: Optional[str] = None) -> Tuple[str, int, int, bool]:
        """
        High-Performance Single-Pass (One-Pass) hashing & deduplication:
        - Files <= 16MB: RAM read + RAM SHA-256 first. Duplicate files return in ~0ms with zero disk I/O and zero compression CPU.
        - Large files: Streaming one-pass compression directly to blob.
        """
        actual_path = read_path if read_path else filepath
        orig_size = os.path.getsize(actual_path)

        # Fast path for small/medium files (<= 16MB, covers 99% of system files)
        if orig_size <= 16 * 1024 * 1024:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Operation cancelled")
            with open(actual_path, "rb") as fin:
                data = fin.read()
            sha256_hash = hashlib.sha256(data).hexdigest()
            blob_path = self.get_blob_abs_path(sha256_hash)

            # Instant deduplication return (0 disk write, 0 compression CPU)
            if self.has_blob(sha256_hash):
                stored_size = os.path.getsize(blob_path)
                return sha256_hash, orig_size, stored_size, False

            # Genuinely new blob: compress/encrypt in RAM and write directly to disk
            temp_file = blob_path + f".tmp_{os.getpid()}_{os.urandom(4).hex()}"
            try:
                if self.crypto_engine:
                    compressed = self.crypto_engine.encrypt_blob_data(data, sha256_hash, compress_level=compress_level)
                elif HAS_ZSTD:
                    cctx = zstd.ZstdCompressor(level=compress_level)
                    compressed = cctx.compress(data)
                else:
                    compressed = zlib.compress(data, level=compress_level if compress_level in range(1, 10) else 6)
                
                try:
                    with open(temp_file, "wb") as fout:
                        fout.write(compressed)
                except FileNotFoundError:
                    # Fallback only if prefix dir was somehow removed
                    os.makedirs(os.path.dirname(blob_path), exist_ok=True)
                    with open(temp_file, "wb") as fout:
                        fout.write(compressed)

                # Concurrency double check: if another parallel worker just finished storing this blob
                if self.has_blob(sha256_hash):
                    stored_size = os.path.getsize(blob_path)
                    return sha256_hash, orig_size, stored_size, False

                try:
                    os.replace(temp_file, blob_path)
                    lock_file_immutable(blob_path)
                    stored_size = len(compressed)
                    with self._cache_lock:
                        self._blob_cache.add(sha256_hash)
                    return sha256_hash, orig_size, stored_size, True
                except (FileExistsError, PermissionError, OSError):
                    if self.has_blob(sha256_hash):
                        stored_size = os.path.getsize(blob_path)
                        return sha256_hash, orig_size, stored_size, False
                    raise
            finally:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass

        # Streaming path for large files (> 16MB)
        sha256 = hashlib.sha256()
        temp_dir = os.path.join(self.blobs_dir, "_temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_file = os.path.join(temp_dir, f"tmp_{os.getpid()}_{hashlib.md5(filepath.encode('utf-8', 'replace')).hexdigest()}_{os.urandom(4).hex()}.blob")

        try:
            with open(actual_path, "rb") as fin, open(temp_file, "wb") as fout:
                if HAS_ZSTD:
                    cctx = zstd.ZstdCompressor(level=compress_level)
                    with cctx.stream_writer(fout, closefd=False) as compressor:
                        while True:
                            if cancel_event and cancel_event.is_set():
                                raise InterruptedError("Operation cancelled")
                            chunk = fin.read(DEFAULT_CHUNK_SIZE) # 4MB buffer for large files
                            if not chunk:
                                break
                            sha256.update(chunk)
                            compressor.write(chunk)
                else:
                    compressor = zlib.compressobj(level=compress_level if compress_level in range(1, 10) else 6)
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise InterruptedError("Operation cancelled")
                        chunk = fin.read(DEFAULT_CHUNK_SIZE)
                        if not chunk:
                            break
                        sha256.update(chunk)
                        compressed_chunk = compressor.compress(chunk)
                        if compressed_chunk:
                            fout.write(compressed_chunk)
                    tail = compressor.flush()
                    if tail:
                        fout.write(tail)

            sha256_hash = sha256.hexdigest()
            blob_path = self.get_blob_abs_path(sha256_hash)

            # Deduplication: If already stored, remove temp and return existing
            if self.has_blob(sha256_hash):
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                stored_size = os.path.getsize(blob_path)
                return sha256_hash, orig_size, stored_size, False

            # Concurrency double check before replacing
            if self.has_blob(sha256_hash):
                stored_size = os.path.getsize(blob_path)
                return sha256_hash, orig_size, stored_size, False

            # New blob
            os.makedirs(os.path.dirname(blob_path), exist_ok=True)
            try:
                os.replace(temp_file, blob_path)
                lock_file_immutable(blob_path)
                stored_size = os.path.getsize(blob_path)
                with self._cache_lock:
                    self._blob_cache.add(sha256_hash)
                return sha256_hash, orig_size, stored_size, True
            except (FileExistsError, PermissionError, OSError):
                if self.has_blob(sha256_hash):
                    stored_size = os.path.getsize(blob_path)
                    return sha256_hash, orig_size, stored_size, False
                raise

        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    def get_or_create_active_pack_writer(self, max_pack_size_bytes: int = 512 * 1024 * 1024):
        """Returns or creates the active append-only PackContainerWriter."""
        from pathlib import Path
        from core.pack_format import PackContainerWriter
        with self._pack_writer_lock:
            if not hasattr(self, "_active_pack_writer") or self._active_pack_writer is None:
                import time
                pack_id = f"pack_{int(time.time())}_{os.urandom(3).hex()}"
                pack_path = Path(self.packs_dir) / f"{pack_id}.pack"
                idx_path = Path(self.packs_dir) / f"{pack_id}.idx"
                self._active_pack_writer = PackContainerWriter(pack_path, idx_path)
                self._active_pack_id = pack_id
            elif self._active_pack_writer.current_offset >= max_pack_size_bytes:
                self.commit_active_pack()
                import time
                pack_id = f"pack_{int(time.time())}_{os.urandom(3).hex()}"
                pack_path = Path(self.packs_dir) / f"{pack_id}.pack"
                idx_path = Path(self.packs_dir) / f"{pack_id}.idx"
                self._active_pack_writer = PackContainerWriter(pack_path, idx_path)
                self._active_pack_id = pack_id
            return self._active_pack_writer

    def commit_active_pack(self):
        """Atomically commits index of active pack writer and reloads composite lookup."""
        with self._pack_writer_lock:
            writer = getattr(self, "_active_pack_writer", None)
            if writer is None:
                return
            try:
                writer.commit_index()
            finally:
                # Always release the writer and resync the map from on-disk idx
                # files so chunks of a failed commit are not reported as present.
                try:
                    writer.close()
                finally:
                    self._active_pack_writer = None
                    self.composite_facade.reload_packs()

    def put_chunk_to_pack(self, data: bytes, compress_level: int = 3) -> Tuple[str, int, int, bool]:
        """
        Stores chunk into an active Pack Container with payload verification.
        Returns: (sha256_hash, orig_size, stored_size, is_new)
        """
        sha256_hash = calculate_bytes_sha256(data)
        orig_size = len(data)

        # Check existing in pack or individual blob
        if self.has_blob(sha256_hash):
            return sha256_hash, orig_size, orig_size, False

        with self._pack_writer_lock:
            # Re-check under lock in case another thread just committed it
            if self.has_blob(sha256_hash):
                return sha256_hash, orig_size, orig_size, False

            # Store raw chunk bytes directly so Pack frame payload matches chunk SHA-256
            writer = self.get_or_create_active_pack_writer()
            writer.write_chunk(sha256_hash, data)

            # Update in-memory composite map immediately
            if self.composite_facade:
                self.composite_facade.pack_index_map[sha256_hash.lower()] = self._active_pack_id

            return sha256_hash, orig_size, orig_size, True

    def put_bytes_blob(self, data: bytes, compress_level: int = 3, use_pack: bool = False) -> Tuple[str, int, int, bool]:
        if use_pack:
            return self.put_chunk_to_pack(data, compress_level=compress_level)

        sha256_hash = calculate_bytes_sha256(data)
        blob_path = self.get_blob_abs_path(sha256_hash)
        orig_size = len(data)

        if self.has_blob(sha256_hash):
            stored_size = os.path.getsize(blob_path) if os.path.exists(blob_path) else orig_size
            return sha256_hash, orig_size, stored_size, False

        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        if self.crypto_engine:
            compressed = self.crypto_engine.encrypt_blob_data(data, sha256_hash, compress_level=compress_level)
        elif HAS_ZSTD:
            cctx = zstd.ZstdCompressor(level=compress_level)
            compressed = cctx.compress(data)
        else:
            compressed = zlib.compress(data, level=compress_level if compress_level in range(1, 10) else 6)

        temp_blob_path = blob_path + f".tmp_{os.getpid()}_{os.urandom(3).hex()}"
        with open(temp_blob_path, "wb") as f:
            f.write(compressed)
        try:
            os.replace(temp_blob_path, blob_path)
            lock_file_immutable(blob_path)
            # Fix #16: Don't call record_new_blob here — caller (snapshot.py) handles batch update for consistency
            with self._cache_lock:
                self._blob_cache.add(sha256_hash)
            return sha256_hash, orig_size, len(compressed), True
        except (FileExistsError, PermissionError, OSError):
            if os.path.exists(temp_blob_path):
                try:
                    os.remove(temp_blob_path)
                except OSError:
                    pass
            if os.path.exists(blob_path):
                return sha256_hash, orig_size, os.path.getsize(blob_path), False
            raise

    def extract_blob_to_file(self, sha256_hash: str, dest_filepath: str, verify_hash: bool = False, direct_write: bool = True) -> bool:
        """
        Decompresses blob directly to dest_filepath with HYBRID automatic format detection.
        Supports v1 AES-256-GCM encrypted blobs (magic b'ENC\\x01') and v0 Zstd/zlib plaintext blobs.
        Dual-Read: Pack 컨테이너 우선 추출 -> Individual Blob 폴백.
        """
        # Dual-Read: Fast Pack extraction if present
        if self._composite_facade and sha256_hash.lower() in self._composite_facade.pack_index_map:
            try:
                from pathlib import Path
                return self.composite_facade.extract_blob_to_file(sha256_hash, Path(dest_filepath), verify_hash=verify_hash)
            except Exception:
                pass  # Fallback to individual blob if available

        blob_path = self.get_blob_abs_path(sha256_hash)
        if not os.path.exists(blob_path):
            raise FileNotFoundError(f"Blob not found in repository: {sha256_hash}")

        os.makedirs(os.path.dirname(dest_filepath), exist_ok=True)
        target_dest = dest_filepath if direct_write else (dest_filepath + ".restore.tmp")
        if os.path.exists(target_dest):
            try:
                os.chmod(target_dest, stat_mod.S_IWRITE)
            except Exception:
                pass

        # Check first 4 magic bytes
        with open(blob_path, "rb") as f_head:
            magic_header = f_head.read(4)

        # v1 Encrypted blob branch
        if magic_header == b"ENC\x01":
            if not self.crypto_engine:
                raise RuntimeError(f"암호화된 블롭입니다 ({sha256_hash[:12]}). 복호화를 위해 마스터 키가 필요합니다.")
            with open(blob_path, "rb") as f_in:
                blob_bytes = f_in.read()
            decompressed = self.crypto_engine.decrypt_blob_data(blob_bytes, sha256_hash)
            with open(target_dest, "wb") as f_out:
                f_out.write(decompressed)
            if not direct_write:
                os.replace(target_dest, dest_filepath)
            return True

        is_zstd = (magic_header == ZSTD_MAGIC)

        # Decompress with appropriate engine + fallback
        success = False
        sha256 = hashlib.sha256() if verify_hash else None
        buf_size = 262144

        # Attempt 1: primary detected engine
        for engine in ("zstd" if is_zstd else "zlib", "zlib" if is_zstd else "zstd"):
            if engine == "zstd" and not HAS_ZSTD:
                continue
            try:
                if sha256:
                    sha256 = hashlib.sha256()

                with open(blob_path, "rb") as fin, open(target_dest, "wb") as fout:
                    if engine == "zstd":
                        dctx = zstd.ZstdDecompressor()
                        with dctx.stream_reader(fin) as reader:
                            while True:
                                chunk = reader.read(buf_size)
                                if not chunk:
                                    break
                                fout.write(chunk)
                                if sha256:
                                    sha256.update(chunk)
                    else:
                        decompressor = zlib.decompressobj()
                        while True:
                            chunk = fin.read(buf_size)
                            if not chunk:
                                break
                            decompressed_chunk = decompressor.decompress(chunk)
                            if decompressed_chunk:
                                fout.write(decompressed_chunk)
                                if sha256:
                                    sha256.update(decompressed_chunk)
                        tail = decompressor.flush()
                        if tail:
                            fout.write(tail)
                            if sha256:
                                sha256.update(tail)
                success = True
                break
            except Exception as e_engine:
                log.debug(f"블롭 엔진 압축 해제 실패 시도 ({sha256_hash}): {e_engine}")
                if not direct_write and os.path.exists(target_dest):
                    try:
                        os.remove(target_dest)
                    except OSError:
                        pass
                continue

        if not success:
            raise RuntimeError(f"Failed to decompress blob '{sha256_hash}' using both zstd and zlib engines.")

        if verify_hash and sha256.hexdigest() != sha256_hash:
            if os.path.exists(target_dest):
                try:
                    os.remove(target_dest)
                except OSError:
                    pass
            raise ValueError(f"Hash verification failed for {dest_filepath} (Expected: {sha256_hash}, got {sha256.hexdigest()})")

        if not direct_write:
            import stat as stat_mod
            if os.path.exists(dest_filepath):
                try:
                    os.chmod(dest_filepath, stat_mod.S_IWRITE)
                    os.remove(dest_filepath)
                except OSError:
                    pass
            try:
                os.replace(target_dest, dest_filepath)
            except PermissionError:
                try:
                    os.chmod(dest_filepath, stat_mod.S_IWRITE)
                    os.replace(target_dest, dest_filepath)
                except Exception:
                    raise
        return True

    def assemble_chunks_to_file(
        self,
        chunk_ids: List[str],
        dest_filepath: str,
        expected_sha256: Optional[str] = None,
        verify_hash: bool = True
    ) -> bool:
        """
        다중 청크(Multi-chunk) 목록을 순차적으로 스트리밍 결합 복원하여 목적지 파일에 기록.
        원자성 보장: 임시 파일에 먼저 복원 후 SHA-256 검증 완료 시 원자적 치환.
        """
        dest_filepath = os.path.normpath(dest_filepath)
        os.makedirs(os.path.dirname(dest_filepath), exist_ok=True)
        temp_dest = dest_filepath + f".assemble.tmp_{os.getpid()}_{os.urandom(3).hex()}"
        sha = hashlib.sha256() if (verify_hash or expected_sha256) else None

        try:
            with open(temp_dest, "wb") as fout:
                for cid in chunk_ids:
                    chunk_bytes = self.read_blob_bytes(cid)
                    fout.write(chunk_bytes)
                    if sha:
                        sha.update(chunk_bytes)

            if sha and expected_sha256:
                calc = sha.hexdigest().lower()
                if calc != expected_sha256.lower():
                    raise ValueError(
                        f"다중 청크 복원 해시 불일치 (기대값: {expected_sha256}, 복원값: {calc})"
                    )

            if os.path.exists(dest_filepath):
                try:
                    os.chmod(dest_filepath, stat_mod.S_IWRITE)
                    os.remove(dest_filepath)
                except OSError:
                    pass

            os.replace(temp_dest, dest_filepath)
            return True
        finally:
            if os.path.exists(temp_dest):
                try:
                    os.remove(temp_dest)
                except OSError:
                    pass

    def read_blob_bytes(self, sha256_hash: str) -> bytes:
        # Dual-Read: Fast Pack read if present
        if sha256_hash.lower() in self.composite_facade.pack_index_map:
            try:
                return self.composite_facade.read_blob_bytes(sha256_hash)
            except Exception:
                pass  # Fallback to individual blob

        blob_path = self.get_blob_abs_path(sha256_hash)
        if not os.path.exists(blob_path):
            raise FileNotFoundError(f"Blob not found: {sha256_hash}")

        with open(blob_path, "rb") as f:
            compressed = f.read()

        if compressed.startswith(ZSTD_MAGIC) and HAS_ZSTD:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(compressed)
        try:
            return zlib.decompress(compressed)
        except Exception:
            if HAS_ZSTD:
                dctx = zstd.ZstdDecompressor()
                return dctx.decompress(compressed)
            raise

    def verify_blob(self, sha256_hash: str) -> Tuple[bool, Optional[str], int]:
        """
        단일 블롭의 압축 해제, 복호화 및 SHA-256 일치 여부를 검증.
        물리 경로, ENC\x01 헤더, AES-256-GCM, ZSTD/zlib 스트리밍을 저장소 내부에서 완전 캡슐화.
        Dual-Read: Pack 컨테이너 인덱스 우선 검증 -> Individual Blob 폴백.
        반환: (성공 여부, 오류 메시지, 검증된 언팩 바이트 크기)
        """
        # Dual-Read: Fast Pack verification if present
        if sha256_hash.lower() in self.composite_facade.pack_index_map:
            return self.composite_facade.verify_blob(sha256_hash)

        blob_path = self.get_blob_abs_path(sha256_hash)
        if not os.path.exists(blob_path):
            return False, f"블롭 파일이 존재하지 않음: {sha256_hash}", 0

        try:
            with open(blob_path, "rb") as f:
                magic = f.read(4)

            # v1 암호화 블롭 처리
            if magic == b"ENC\x01":
                if not self.crypto_engine:
                    return False, "암호화된 블롭입니다 (복호화 키 필요)", 0
                with open(blob_path, "rb") as f_in:
                    raw_blob = f_in.read()
                decompressed = self.crypto_engine.decrypt_blob_data(raw_blob, sha256_hash)
                calc_hash = hashlib.sha256(decompressed).hexdigest()
                if calc_hash.lower() != sha256_hash.lower():
                    return False, f"해시 불일치 (기록: {sha256_hash}, 계산: {calc_hash})", 0
                return True, None, len(decompressed)

            is_zstd = (magic == ZSTD_MAGIC)
            decompressed_hasher = hashlib.sha256()
            total_size = 0

            with open(blob_path, "rb") as f_in:
                if is_zstd and HAS_ZSTD:
                    dctx = zstd.ZstdDecompressor()
                    with dctx.stream_reader(f_in) as reader:
                        while True:
                            chunk = reader.read(262144)
                            if not chunk:
                                break
                            decompressed_hasher.update(chunk)
                            total_size += len(chunk)
                else:
                    decomp = zlib.decompressobj()
                    while True:
                        raw = f_in.read(262144)
                        if not raw:
                            break
                        chunk = decomp.decompress(raw)
                        if chunk:
                            decompressed_hasher.update(chunk)
                            total_size += len(chunk)
                    tail = decomp.flush()
                    if tail:
                        decompressed_hasher.update(tail)
                        total_size += len(tail)

            calculated_hash = decompressed_hasher.hexdigest()
            if calculated_hash.lower() != sha256_hash.lower():
                return False, f"해시 불일치 (기록: {sha256_hash}, 계산: {calculated_hash})", 0

            return True, None, total_size

        except Exception as e:
            log.error(f"블롭 무결성 검증 예외 ({sha256_hash}): {e}", exc_info=True)
            return False, f"압축 해제 또는 데이터 무결성 오류: {str(e)}", 0

    def delete_blob(self, sha256_hash: str) -> bool:
        """단일 블롭을 WORM 해제 후 삭제하고 캐시에서 제거."""
        blob_path = self.get_blob_abs_path(sha256_hash)
        if not os.path.exists(blob_path):
            with self._cache_lock:
                self._blob_cache.discard(sha256_hash)
            return False
        try:
            unlock_file_writable(blob_path)
            os.remove(blob_path)
            with self._cache_lock:
                self._blob_cache.discard(sha256_hash)
            return True
        except OSError:
            return False

    def prune_unreferenced_blobs(self, active_hashes: Set[str]) -> Tuple[int, int]:

        """Deletes blobs that are not present in active_hashes set. Returns (deleted_count, freed_bytes)."""
        deleted_count = 0
        freed_bytes = 0

        for root, _, files in os.walk(self.blobs_dir):
            for file in files:
                if file.endswith(".blob"):
                    blob_hash = file[:-5]
                    if blob_hash not in active_hashes:
                        blob_path = os.path.join(root, file)
                        try:
                            fsize = os.path.getsize(blob_path)
                            unlock_file_writable(blob_path)
                            os.remove(blob_path)
                            deleted_count += 1
                            freed_bytes += fsize
                            with self._cache_lock:
                                self._blob_cache.discard(blob_hash)
                        except OSError:
                            pass
        if deleted_count > 0:
            self.db.record_removed_blobs(deleted_count, freed_bytes)
        return deleted_count, freed_bytes

    def get_storage_stats(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Returns storage stats instantly via SQLite metadata cache."""
        return self.db.get_storage_stats(force_refresh=force_refresh)
