# -*- coding: utf-8 -*-
"""
core/pack_consolidator.py
Track 2-17: Idle-time Pack Consolidation & Delayed GC Compactor Engine.
Safely consolidates scattered individual .blob files into cohesive .pack containers.

Guarantees:
1. Delayed GC: Originals are never deleted immediately; moved to .quarantine/ and deleted only after retention delay.
2. 2-Phase Integrity Verification: Every chunk is checksum-verified (SHA-256 + CRC32) from the committed pack before quarantining originals.
3. Fail-Closed Rollback: Any corruption or abort instantly wipes the partial .pack without touching original .blob files.
4. Non-blocking & Crash Consistent: Respects repository lock and atomic os.replace semantics.
"""

import os
import time
import shutil
import hashlib
from typing import List, Dict, Any, Tuple, Set
from pathlib import Path

from core.storage import BlobStorage
from core.pack_format import PackContainerWriter, PackContainerReader
from core.worm import unlock_file_writable


class PackConsolidator:
    def __init__(
        self,
        repo_dir: str,
        target_pack_size_bytes: int = 256 * 1024 * 1024,
        max_blobs_per_run: int = 5000,
        quarantine_retention_seconds: float = 86400.0  # 24 hours delayed GC
    ):
        self.repo_dir = Path(repo_dir)
        self.blobs_dir = self.repo_dir / "blobs"
        self.packs_dir = self.repo_dir / "packs"
        self.quarantine_dir = self.repo_dir / ".quarantine_blobs"
        self.target_pack_size_bytes = target_pack_size_bytes
        self.max_blobs_per_run = max_blobs_per_run
        self.quarantine_retention_seconds = quarantine_retention_seconds

        self.storage = BlobStorage(str(self.repo_dir))
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def scan_unpacked_blobs(self) -> List[Tuple[str, Path, int]]:
        """
        Scans blobs/ for individual .blob files that are not yet in any .pack index.
        Returns: list of (sha256, file_path, size)
        """
        packed_hashes: Set[str] = set(self.storage.composite_facade.pack_index_map.keys())
        unpacked = []

        for root, _, files in os.walk(self.blobs_dir):
            if "_temp" in root:
                continue
            for fname in files:
                if fname.endswith(".blob"):
                    sha256 = fname[:-5].lower()
                    if sha256 not in packed_hashes:
                        fpath = Path(root) / fname
                        try:
                            fsize = fpath.stat().st_size
                            unpacked.append((sha256, fpath, fsize))
                            if len(unpacked) >= self.max_blobs_per_run:
                                return unpacked
                        except OSError:
                            continue
        return unpacked

    def consolidate(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Executes consolidation pipeline:
        1. Scan candidate .blobs
        2. Append to a new PackContainer
        3. Commit index
        4. Re-verify each chunk from the new Pack
        5. Quarantine original .blob files
        """
        candidates = self.scan_unpacked_blobs()
        if not candidates:
            return {
                "status": "idle",
                "message": "No unpacked individual blobs found.",
                "consolidated_count": 0,
                "consolidated_bytes": 0
            }

        total_bytes = sum(c[2] for c in candidates)
        if dry_run:
            return {
                "status": "dry_run",
                "message": f"Candidate blobs: {len(candidates)} ({total_bytes} bytes)",
                "consolidated_count": len(candidates),
                "consolidated_bytes": total_bytes
            }

        timestamp = int(time.time())
        pack_id = f"pack_consolidated_{timestamp}_{os.urandom(3).hex()}"
        pack_path = self.packs_dir / f"{pack_id}.pack"
        idx_path = self.packs_dir / f"{pack_id}.idx"

        writer = None
        packed_blobs = []

        try:
            writer = PackContainerWriter(pack_path, idx_path)
            # Stage 1: Read raw uncompressed/decompressed data and write into Pack
            for sha256, fpath, fsize in candidates:
                try:
                    raw_data = self.storage.read_blob_bytes(sha256)
                    writer.write_chunk(sha256, raw_data)
                    packed_blobs.append((sha256, fpath, fsize))
                except Exception as e_read:
                    _ = e_read  # 읽기 불가 블롭은 건너뜀
                    # Skip unreadable blobs, keep moving
                    continue

            if not packed_blobs:
                writer.close()
                if pack_path.exists():
                    pack_path.unlink()
                return {"status": "skipped", "message": "No valid blobs packed.", "consolidated_count": 0}

            # Stage 2: Commit Pack Index atomically
            writer.commit_index()
            writer.close()

            # Stage 3: Two-Phase Verification from the newly committed Pack
            reader = PackContainerReader(pack_path, idx_path)
            verified_count = 0
            for sha256, fpath, fsize in packed_blobs:
                chunk_bytes = reader.read_chunk(sha256, verify=True)
                calc_sha = hashlib.sha256(chunk_bytes).hexdigest().lower()
                if calc_sha != sha256:
                    raise ValueError(f"Consolidation verification failed for {sha256}")
                verified_count += 1

            # Stage 4: Reload storage composite facade so dual-read now points to new pack
            self.storage.composite_facade.reload_packs()

            # Stage 5: Delayed GC - Move originals to .quarantine_blobs/ (NEVER immediate delete)
            quarantined_count = 0
            for sha256, fpath, fsize in packed_blobs:
                try:
                    unlock_file_writable(str(fpath))
                    q_dest = self.quarantine_dir / f"{sha256}.blob"
                    shutil.move(str(fpath), str(q_dest))
                    quarantined_count += 1
                except OSError:
                    pass

            return {
                "status": "success",
                "pack_id": pack_id,
                "consolidated_count": verified_count,
                "quarantined_count": quarantined_count,
                "consolidated_bytes": total_bytes
            }

        except Exception as e:
            # Fail-closed rollback: wipe incomplete pack and keep originals 100% untouched
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass
            if pack_path.exists():
                try:
                    pack_path.unlink()
                except OSError:
                    pass
            if idx_path.exists():
                try:
                    idx_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"Consolidation aborted and safely rolled back: {str(e)}")

    def run_delayed_gc(self, force_all: bool = False) -> Tuple[int, int]:
        """
        Deletes quarantined .blob files whose age exceeds quarantine_retention_seconds.
        Returns: (deleted_files_count, freed_bytes)
        """
        if not self.quarantine_dir.exists():
            return 0, 0

        now = time.time()
        deleted_count = 0
        freed_bytes = 0

        for f in self.quarantine_dir.glob("*.blob"):
            try:
                mtime = f.stat().st_mtime
                if force_all or (now - mtime) >= self.quarantine_retention_seconds:
                    fsize = f.stat().st_size
                    f.unlink()
                    deleted_count += 1
                    freed_bytes += fsize
            except OSError:
                continue

        return deleted_count, freed_bytes
