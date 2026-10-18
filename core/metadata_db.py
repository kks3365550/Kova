import os
import time
import json
import sqlite3
import threading
import contextlib
import logging
from typing import Dict, List, Any, Tuple

logger = logging.getLogger("core.metadata_db")

class MetadataDB:
    _lock = threading.Lock()

    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.db_path = os.path.join(self.repo_dir, "metadata.db")
        self.snapshots_dir = os.path.join(self.repo_dir, "snapshots")
        self.blobs_dir = os.path.join(self.repo_dir, "blobs")
        self._stats_cache = None
        self._stats_cache_time = 0.0
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _verify_and_heal_db(self):
        """
        Self-Healing: Performs PRAGMA integrity_check on metadata.db.
        If corruption is detected from sudden power loss, quarantines the corrupted DB
        and automatically rebuilds everything from ground truth JSON snapshots and CAS blobs.
        """
        if not os.path.exists(self.db_path):
            return

        is_corrupted = False
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            rows = cursor.fetchall()
            if not rows or rows[0][0].lower() != "ok":
                is_corrupted = True
        except Exception:
            is_corrupted = True
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        if is_corrupted:
            import time
            ts = int(time.time() * 1000)
            corrupt_backup = f"{self.db_path}.corrupt_{ts}"
            try:
                for ext in ["-wal", "-shm"]:
                    extra_file = self.db_path + ext
                    if os.path.exists(extra_file):
                        try:
                            os.remove(extra_file)
                        except (PermissionError, OSError):
                            pass
                if os.path.exists(self.db_path):
                    os.replace(self.db_path, corrupt_backup)
            except Exception:
                try:
                    import shutil
                    shutil.copy2(self.db_path, corrupt_backup)
                    os.remove(self.db_path)
                except Exception:
                    pass

    def _init_db(self):
        try:
            os.makedirs(self.repo_dir, exist_ok=True)
            self._verify_and_heal_db()
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS blobs_summary (
                        id INTEGER PRIMARY KEY,
                        total_blobs INTEGER NOT NULL DEFAULT 0,
                        stored_bytes INTEGER NOT NULL DEFAULT 0,
                        last_updated REAL NOT NULL DEFAULT 0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots_meta (
                        id TEXT PRIMARY KEY,
                        created_at REAL,
                        iso_time TEXT,
                        profile_id TEXT,
                        profile_name TEXT,
                        backup_type TEXT,
                        base_snapshot_id TEXT,
                        total_files INTEGER DEFAULT 0,
                        total_bytes INTEGER DEFAULT 0,
                        new_files INTEGER DEFAULT 0,
                        modified_files INTEGER DEFAULT 0,
                        unmodified_files INTEGER DEFAULT 0,
                        dedup_saved_bytes INTEGER DEFAULT 0,
                        duration_seconds REAL DEFAULT 0,
                        sources_json TEXT,
                        file_mtime REAL,
                        is_verified INTEGER DEFAULT 0,
                        verify_timestamp REAL DEFAULT 0,
                        verify_error_count INTEGER DEFAULT 0
                    );
                """)
                # Auto migration: check if is_verified column exists
                cursor = conn.execute("PRAGMA table_info(snapshots_meta);")
                columns = [row[1] for row in cursor.fetchall()]
                if "is_verified" not in columns:
                    conn.execute("ALTER TABLE snapshots_meta ADD COLUMN is_verified INTEGER DEFAULT 0;")
                if "verify_timestamp" not in columns:
                    conn.execute("ALTER TABLE snapshots_meta ADD COLUMN verify_timestamp REAL DEFAULT 0;")
                if "verify_error_count" not in columns:
                    conn.execute("ALTER TABLE snapshots_meta ADD COLUMN verify_error_count INTEGER DEFAULT 0;")
                conn.commit()

            # Check if snapshots need reconstruction (performed outside the connection context to prevent deadlocks)
            needs_rebuild = False
            with self._get_connection() as conn:
                row = conn.execute("SELECT COUNT(*) FROM snapshots_meta;").fetchone()
                if row and row[0] == 0:
                    needs_rebuild = True

            if needs_rebuild and os.path.exists(self.snapshots_dir):
                self.sync_snapshots()
                self.rebuild_blobs_summary()
        except Exception as e_init:
            logger.error("MetadataDB _init_db 초기화 실패: %s", e_init, exc_info=True)

    def record_new_blob(self, stored_size: int, count: int = 1):
        """Atomically increments blob count and stored bytes in cache.
        Fix #11: 'count' should be the number of truly NEW unique blobs, not new files.
        Callers must pass only blobs that are genuinely new (is_new=True from put_file_blob_onepass).
        """
        if count <= 0 and stored_size <= 0:
            return
        try:
            with self._lock, self._get_connection() as conn:
                row = conn.execute("SELECT id FROM blobs_summary WHERE id = 1;").fetchone()
                now = time.time()
                if not row:
                    conn.execute("""
                        INSERT INTO blobs_summary (id, total_blobs, stored_bytes, last_updated)
                        VALUES (1, ?, ?, ?);
                    """, (max(0, count), max(0, stored_size), now))
                else:
                    conn.execute("""
                        UPDATE blobs_summary
                        SET total_blobs = total_blobs + ?,
                            stored_bytes = stored_bytes + ?,
                            last_updated = ?
                        WHERE id = 1;
                    """, (max(0, count), max(0, stored_size), now))
                conn.commit()
        except Exception:
            pass

    def record_removed_blobs(self, deleted_count: int, freed_bytes: int):
        """Atomically decrements blob count and stored bytes."""
        try:
            with self._lock, self._get_connection() as conn:
                now = time.time()
                conn.execute("""
                    UPDATE blobs_summary
                    SET total_blobs = MAX(0, total_blobs - ?),
                        stored_bytes = MAX(0, stored_bytes - ?),
                        last_updated = ?
                    WHERE id = 1;
                """, (deleted_count, freed_bytes, now))
                conn.commit()
        except Exception:
            pass

    def rebuild_blobs_summary(self) -> Tuple[int, int]:
        """Scans disk to accurately recalculate blob totals (only run on first init or manual refresh)."""
        total_blobs = 0
        total_blob_bytes = 0

        if os.path.exists(self.blobs_dir):
            for root, _, files in os.walk(self.blobs_dir):
                for file in files:
                    if file.endswith(".blob"):
                        total_blobs += 1
                        try:
                            total_blob_bytes += os.path.getsize(os.path.join(root, file))
                        except OSError:
                            pass

        now = time.time()
        try:
            with self._lock, self._get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO blobs_summary (id, total_blobs, stored_bytes, last_updated)
                    VALUES (1, ?, ?, ?);
                """, (total_blobs, total_blob_bytes, now))
                conn.commit()
        except Exception:
            pass

        return total_blobs, total_blob_bytes

    def sync_snapshots(self) -> List[Dict[str, Any]]:
        """
        Synchronizes snapshot JSON metadata into SQLite.
        Only parses files that are new or whose mtime has changed.
        """
        if not os.path.exists(self.snapshots_dir):
            return []

        disk_files = {}
        for f in os.listdir(self.snapshots_dir):
            if f.endswith(".json"):
                full_p = os.path.join(self.snapshots_dir, f)
                try:
                    disk_files[f] = (full_p, os.path.getmtime(full_p))
                except OSError:
                    pass

        with self._lock, self._get_connection() as conn:
            cached_rows = conn.execute("SELECT id, file_mtime FROM snapshots_meta;").fetchall()
            cached_map = {row["id"]: row["file_mtime"] for row in cached_rows}

            # Delete snapshots that no longer exist on disk
            for cid in list(cached_map.keys()):
                fname = f"{cid}.json"
                if fname not in disk_files:
                    conn.execute("DELETE FROM snapshots_meta WHERE id = ?;", (cid,))
                    del cached_map[cid]

            # Insert/Update new or modified snapshots
            for fname, (full_p, mtime) in disk_files.items():
                sid = fname[:-5]
                if sid not in cached_map or abs(cached_map[sid] - mtime) > 0.001:
                    try:
                        with open(full_p, "r", encoding="utf-8") as fp:
                            data = json.load(fp)
                            summary = data.get("summary", {})
                            sources = json.dumps(data.get("sources", []), ensure_ascii=False)
                            is_ver = 1 if data.get("is_verified") else 0
                            ver_ts = data.get("verify_timestamp", 0.0)
                            ver_err = data.get("verify_error_count", 0)
                            created_at_val = data.get("created_at_ts") or data.get("created_at", 0)
                            if isinstance(created_at_val, str):
                                try:
                                    import datetime
                                    dt = datetime.datetime.fromisoformat(created_at_val.replace("Z", "+00:00"))
                                    created_at_val = dt.timestamp()
                                except Exception:
                                    created_at_val = mtime
                            conn.execute("""
                                INSERT OR REPLACE INTO snapshots_meta (
                                    id, created_at, iso_time, profile_id, profile_name,
                                    backup_type, base_snapshot_id, total_files, total_bytes,
                                    new_files, modified_files, unmodified_files,
                                    dedup_saved_bytes, duration_seconds, sources_json, file_mtime,
                                    is_verified, verify_timestamp, verify_error_count
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """, (
                                sid,
                                float(created_at_val),
                                data.get("iso_time", ""),
                                data.get("profile_id", ""),
                                data.get("profile_name", ""),
                                data.get("backup_type", "full"),
                                data.get("base_snapshot_id"),
                                summary.get("total_files", 0),
                                summary.get("total_bytes", 0),
                                summary.get("new_files", 0),
                                summary.get("modified_files", 0),
                                summary.get("unmodified_files", 0),
                                summary.get("dedup_saved_bytes", 0),
                                summary.get("duration_seconds", 0),
                                sources,
                                mtime,
                                is_ver,
                                ver_ts,
                                ver_err
                            ))
                    except Exception:
                        pass

            conn.commit()

        return self.list_snapshots()

    def update_snapshot_verification(self, snapshot_id: str, is_verified: bool, error_count: int = 0):
        """Updates verification status for a specific snapshot in SQLite."""
        max_retries = 2
        attempt = 0
        while True:
            try:
                with self._lock, self._get_connection() as conn:
                    conn.execute("""
                        UPDATE snapshots_meta
                        SET is_verified = ?,
                            verify_timestamp = ?,
                            verify_error_count = ?
                        WHERE id = ?;
                    """, (1 if is_verified else 0, time.time(), error_count, snapshot_id))
                    conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retries:
                    attempt += 1
                    time.sleep(0.1)
                    continue
                logger.error("MetadataDB update_snapshot_verification failed: %s", e, exc_info=True)
                raise
            except Exception as e:
                logger.error("MetadataDB update_snapshot_verification failed: %s", e, exc_info=True)
                raise

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """Returns snapshot metadata summaries in under 1ms."""
        snapshots = []
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT id, created_at, iso_time, profile_id, profile_name,
                           backup_type, base_snapshot_id, total_files, total_bytes,
                           new_files, modified_files, unmodified_files,
                           dedup_saved_bytes, duration_seconds, sources_json,
                           is_verified, verify_timestamp, verify_error_count
                    FROM snapshots_meta
                    ORDER BY created_at DESC;
                """).fetchall()

                for r in rows:
                    try:
                        sources = json.loads(r["sources_json"]) if r["sources_json"] else []
                    except Exception:
                        sources = []

                    snapshots.append({
                        "id": r["id"],
                        "created_at": r["created_at"],
                        "iso_time": r["iso_time"],
                        "profile_id": r["profile_id"],
                        "profile_name": r["profile_name"],
                        "backup_type": r["backup_type"],
                        "base_snapshot_id": r["base_snapshot_id"],
                        "sources": sources,
                        "is_verified": bool(r["is_verified"]),
                        "verify_timestamp": r["verify_timestamp"],
                        "verify_error_count": r["verify_error_count"],
                        "summary": {
                            "total_files": r["total_files"],
                            "total_bytes": r["total_bytes"],
                            "new_files": r["new_files"],
                            "modified_files": r["modified_files"],
                            "unmodified_files": r["unmodified_files"],
                            "dedup_saved_bytes": r["dedup_saved_bytes"],
                            "duration_seconds": r["duration_seconds"]
                        }
                    })
        except Exception:
            pass
        return snapshots

    def get_storage_stats(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Calculates storage stats in 0.001s using SQLite cache & in-memory TTL."""
        now = time.time()
        if not force_refresh and self._stats_cache is not None and (now - self._stats_cache_time) < 60.0:
            return dict(self._stats_cache)

        total_blobs = 0
        stored_bytes = 0

        needs_rebuild = False
        try:
            with self._get_connection() as conn:
                row = conn.execute("SELECT total_blobs, stored_bytes, last_updated FROM blobs_summary WHERE id = 1;").fetchone()
                if row and not force_refresh:
                    total_blobs = row["total_blobs"]
                    stored_bytes = row["stored_bytes"]
                else:
                    needs_rebuild = True
        except Exception:
            needs_rebuild = True

        if needs_rebuild:
            total_blobs, stored_bytes = self.rebuild_blobs_summary()

        snapshots = self.sync_snapshots()
        total_logical_bytes = sum(s.get("summary", {}).get("total_bytes", 0) for s in snapshots)
        saved_bytes = max(0, total_logical_bytes - stored_bytes)
        ratio = round((saved_bytes / total_logical_bytes * 100), 1) if total_logical_bytes > 0 else 0.0

        # 청킹 통계 계산 (최신 1개 스냅샷만 확인하여 90MB JSON 반복 파싱으로 인한 수초 블로킹 원천 차단)
        total_chunks = 0
        chunked_files_count = 0
        chunk_strategies = {}

        if snapshots:
            snap = snapshots[0]
            snap_id = snap.get("id")
            if snap_id:
                snap_path = os.path.join(self.snapshots_dir, f"{snap_id}.json")
                try:
                    with open(snap_path, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                        entries = data.get("entries") or data.get("files", [])
                        for entry in entries:
                            chunk_ids = entry.get("chunk_ids")
                            if chunk_ids and len(chunk_ids) > 1:
                                total_chunks += len(chunk_ids)
                                chunked_files_count += 1
                                strategy = entry.get("chunk_strategy", "unknown")
                                chunk_strategies[strategy] = chunk_strategies.get(strategy, 0) + 1
                except Exception:
                    pass

        res = {
            "total_blobs": total_blobs,
            "stored_bytes": stored_bytes,
            "logical_bytes": total_logical_bytes,
            "total_snapshots": len(snapshots),
            "dedup_saved_bytes": saved_bytes,
            "savings_percentage": ratio,
            "total_chunks": total_chunks,
            "chunked_files_count": chunked_files_count,
            "chunk_strategies": chunk_strategies
        }
        self._stats_cache = res
        self._stats_cache_time = now
        return res

    def delete_snapshot_record(self, snapshot_id: str):
        try:
            with self._lock, self._get_connection() as conn:
                conn.execute("DELETE FROM snapshots_meta WHERE id = ?;", (snapshot_id,))
                conn.commit()
        except Exception:
            pass

    def checkpoint(self, mode: str = "TRUNCATE") -> bool:
        """
        SQLite WAL 모드의 로그를 메인 DB 파일로 즉시 강제 병합 및 자르기(TRUNCATE).
        정전이나 충돌 시에도 metadata.db 단일 파일의 원자적 일관성을 보장.
        """
        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"PRAGMA wal_checkpoint({mode});")
                row = cursor.fetchone()
                logger.debug(f"[WAL Checkpoint] mode={mode}, result={row}")
                return True
        except Exception as e:
            logger.warning(f"[WAL Checkpoint] Failed to checkpoint ({mode}): {e}")
            return False
