# -*- coding: utf-8 -*-
"""
core/snapshot_cache.py - Invalidation-Driven Snapshot Metadata Cache (v2.10.0-rc)
================================================================================
Architecture Principles:
1. P0 (Zero-Drift Consistency): Exact byte/field parity with disk-scanned manifests.
2. P1 (Single Source of Truth):
   - Snapshot manifest metadata is authoritative from filesystem/CAS.
   - Replication status is authoritative from SQLite ReplicationQueue (NOT cached here).
   - This cache NEVER mutates or owns replication states.
3. P2 (Fail-Safe & Eviction-First):
   - Cache errors trigger immediate eviction and fallback to disk scan.
   - NO silent swallowing of exceptions ('except Exception: pass' prohibited).
   - Warning + traceback logged; next read executes a clean rebuild.
4. P3 (External Change Safeguard - Best-effort Safety Net):
   - Dual fingerprint check (snapshots_dir mtime + snapshot file count).
   - Best-effort safety net for external file changes, not an absolute cryptographic guarantee.
   - Authoritative CAS/WORM and manifest verification remains with the core storage tier.
"""

import os
import time
import logging
import threading
from typing import Dict, List, Any, Optional, Callable, Tuple

logger = logging.getLogger("BackupSystem.SnapshotCache")


class SnapshotMetadataCache:
    """
    Thread-safe, eviction-first in-memory cache for snapshot metadata.
    Does NOT store replication queue status (delegated to SQLite).
    """

    def __init__(self):
        self._lock = threading.RLock()
        # repo_dir -> {
        #   "dir_mtime": float,
        #   "snap_count": int,
        #   "cached_at": float,
        #   "snapshots": List[Dict[str, Any]]
        # }
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "fingerprint_invalidations": 0,
            "errors": 0
        }

    def _get_fingerprint(self, repo_dir: str) -> Tuple[Optional[float], int]:
        """
        Retrieves directory mtime and snapshot JSON count in ~0.05ms.
        Serves as an external change detection heuristic.
        """
        snap_dir = os.path.join(repo_dir, "snapshots")
        try:
            st = os.stat(snap_dir)
            dir_mtime = st.st_mtime
            # Quick count of .json files in snapshots directory
            count = sum(1 for f in os.listdir(snap_dir) if f.endswith(".json"))
            return dir_mtime, count
        except (OSError, FileNotFoundError):
            return None, 0

    def list_snapshots(
        self,
        repo_dir: str,
        fallback_scanner: Callable[[str], List[Dict[str, Any]]],
        force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Retrieves snapshot metadata through the cache layer.
        If cache is missing, stale, or encounters an error, it safely falls back to fallback_scanner.
        """
        repo_key = os.path.abspath(repo_dir)

        with self._lock:
            if not force_refresh and repo_key in self._cache:
                try:
                    entry = self._cache[repo_key]
                    dir_mtime, snap_count = self._get_fingerprint(repo_key)

                    # Fingerprint check: both dir_mtime and file count must match exactly
                    if (dir_mtime is not None and 
                        entry["dir_mtime"] == dir_mtime and 
                        entry["snap_count"] == snap_count):
                        self._stats["hits"] += 1
                        # Defensive shallow copy to prevent caller mutation
                        return [s.copy() for s in entry["snapshots"]]
                    else:
                        self._stats["fingerprint_invalidations"] += 1
                        self.evict(repo_key)
                except Exception as e:
                    self._stats["errors"] += 1
                    logger.warning(
                        f"Snapshot cache verification error for '{repo_key}'; evicting cache: {e}",
                        exc_info=True
                    )
                    self.evict(repo_key)

            # Cache Miss, Stale, or Evicted: Fallback to Disk Scanner
            self._stats["misses"] += 1
            try:
                fresh_snapshots = fallback_scanner(repo_key)
                dir_mtime, snap_count = self._get_fingerprint(repo_key)

                self._cache[repo_key] = {
                    "dir_mtime": dir_mtime,
                    "snap_count": snap_count,
                    "cached_at": time.time(),
                    "snapshots": fresh_snapshots
                }
                return [s.copy() for s in fresh_snapshots]
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(
                    f"Fallback disk scanner failed for repo '{repo_key}': {e}",
                    exc_info=True
                )
                self.evict(repo_key)
                raise

    def evict(self, repo_dir: str):
        """
        Evicts all cached metadata for the given repository.
        Called on snapshot creation, deletion, or any failure.
        """
        repo_key = os.path.abspath(repo_dir)
        with self._lock:
            if repo_key in self._cache:
                del self._cache[repo_key]
                self._stats["evictions"] += 1

    def clear(self):
        """Completely clears all cached repositories (e.g., on restart)."""
        with self._lock:
            self._cache.clear()

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return self._stats.copy()


# Global Singleton Instance
_GLOBAL_SNAPSHOT_CACHE = SnapshotMetadataCache()

def get_snapshot_cache() -> SnapshotMetadataCache:
    """Returns the singleton instance of SnapshotMetadataCache."""
    return _GLOBAL_SNAPSHOT_CACHE
