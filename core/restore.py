import os
import time
import hashlib
import threading
import concurrent.futures
from typing import Dict, List, Any, Optional, Callable
from core.storage import BlobStorage
from core.snapshot import SnapshotEngine
from core.logging_setup import get_logger

log = get_logger("core.restore")

class RestoreEngine:
    @classmethod
    def restore_snapshot(
        cls,
        repo_dir: str,
        snapshot_id: str,
        target_dir: Optional[str] = None,
        selected_rel_paths: Optional[List[str]] = None,
        overwrite: bool = True,
        verify_hash: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[Any] = None,
        in_place: bool = False
    ) -> Dict[str, Any]:
        """
        Restores files from a specific snapshot.
        - If in_place=True: restores each file back to its original source_root location (multi-source safe).
        - If in_place=False: restores all files into specified target_dir.
        """
        start_time = time.time()
        storage = BlobStorage(repo_dir)
        snapshot = SnapshotEngine.get_snapshot(repo_dir, snapshot_id)

        if not snapshot:
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found.")

        if not in_place:
            if not target_dir:
                raise ValueError("target_dir is required when in_place=False")
            target_dir = os.path.abspath(target_dir)
            os.makedirs(target_dir, exist_ok=True)

        entries = snapshot.get("entries", [])
        to_restore = []

        if selected_rel_paths is not None:
            # Normalize selected paths for case-insensitive matching
            normalized_selected = [p.replace('\\', '/').strip('/').lower() for p in selected_rel_paths]
            for entry in entries:
                rel_p = entry.get("rel_path", "").replace('\\', '/').strip('/')
                rel_p_lower = rel_p.lower()
                for sel in normalized_selected:
                    if rel_p_lower == sel or rel_p_lower.startswith(sel + "/"):
                        to_restore.append(entry)
                        break
        else:
            to_restore = entries

        total_files = len(to_restore)
        restored_files = 0
        skipped_files = 0
        failed_files = []
        restored_bytes = 0
        lock = threading.Lock()

        num_workers = min(12, max(4, os.cpu_count() or 4))
        current_user_profile = os.path.normpath(os.path.expanduser('~'))
        remap_from = ""
        for entry in to_restore:
            src = entry.get("source_root", "")
            if "users" in src.lower():
                parts = src.replace('/', '\\').split('\\')
                try:
                    u_idx = [p.lower() for p in parts].index("users")
                    if u_idx + 1 < len(parts):
                        backup_user = parts[u_idx + 1]
                        drive_prefix = parts[0] if ':' in parts[0] else 'C:'
                        candidate_remap = os.path.normpath(f"{drive_prefix}\\Users\\{backup_user}")
                        if candidate_remap.lower() != current_user_profile.lower():
                            remap_from = candidate_remap
                            break
                except (ValueError, IndexError):
                    continue

        def _restore_one(entry):
            if cancel_event and cancel_event.is_set():
                return ("cancelled", 0, None, None)

            rel_path = entry.get("rel_path")
            blob_id = entry.get("blob_id") or entry.get("sha256")
            f_size = entry.get("size", 0)
            f_mtime = entry.get("mtime")

            if not rel_path or not blob_id:
                return ("skip", 0, None, None)

            if in_place:
                src_root = entry.get("source_root")
                if not src_root:
                    return ("fail", 0, rel_path, "스냅샷에 원본 경로(source_root) 정보가 없습니다.")
                file_target_dir = os.path.abspath(src_root)
                if remap_from:
                    norm_base = os.path.normpath(file_target_dir)
                    if norm_base.lower().startswith(remap_from.lower()):
                        file_target_dir = os.path.normpath(current_user_profile + norm_base[len(remap_from):])
                dest_path = os.path.abspath(os.path.join(file_target_dir, rel_path))
                # P0: ZipSlip / Path Traversal 방어 (commonpath 검증)
                try:
                    if os.path.commonpath([file_target_dir, dest_path]) != file_target_dir:
                        log.warning("경로 트래버설 차단 (in_place): %s -> %s", rel_path, dest_path)
                        return ("fail", 0, rel_path, "경로 트래버설 차단: 원본 디렉토리 밖으로 복원 시도")
                except ValueError:
                    log.warning("경로 트래버설 차단 (in_place 드라이브 불일치): %s", rel_path)
                    return ("fail", 0, rel_path, "경로 트래버설 차단: 드라이브 불일치")
            else:
                dest_path = os.path.abspath(os.path.join(target_dir, rel_path))
                # P0: ZipSlip / Path Traversal 방어 (commonpath 검증)
                try:
                    if os.path.commonpath([target_dir, dest_path]) != target_dir:
                        log.warning("경로 트래버설 차단 (target_dir): %s -> %s", rel_path, dest_path)
                        return ("fail", 0, rel_path, "경로 트래버설 차단: 대상 디렉토리 밖으로 복원 시도")
                except ValueError:
                    log.warning("경로 트래버설 차단 (target_dir 드라이브 불일치): %s", rel_path)
                    return ("fail", 0, rel_path, "경로 트래버설 차단: 드라이브 불일치")

            if os.path.exists(dest_path) and not overwrite:
                return ("skip", 0, None, None)

            try:
                chunk_ids = entry.get("chunk_ids")
                if chunk_ids and isinstance(chunk_ids, list):
                    expected_sha = entry.get("sha256")
                    storage.assemble_chunks_to_file(
                        chunk_ids=chunk_ids,
                        dest_filepath=dest_path,
                        expected_sha256=expected_sha,
                        verify_hash=verify_hash
                    )
                else:
                    storage.extract_blob_to_file(blob_id, dest_path, verify_hash=verify_hash, direct_write=False)

                # Restore original modified time if present
                if f_mtime:
                    try:
                        os.utime(dest_path, (f_mtime, f_mtime))
                    except OSError:
                        pass

                is_reg = dest_path.lower().endswith(".reg")
                return ("ok", f_size, dest_path if is_reg else None, None)
            except Exception as e:
                return ("fail", 0, rel_path, str(e))


        reg_files_to_import = []

        if to_restore:
            # Optimization #1: Sliding Window Batch Scheduler (max 2,000 active futures in memory)
            # Prevents OOM and massive GIL contention when restoring 100,000+ files
            BATCH_WINDOW_SIZE = 2000

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                items_iter = iter(to_restore)
                active_futures = {}

                # Pre-fill sliding window
                for item in items_iter:
                    fut = executor.submit(_restore_one, item)
                    active_futures[fut] = item
                    if len(active_futures) >= BATCH_WINDOW_SIZE:
                        break

                while active_futures:
                    if cancel_event and cancel_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise InterruptedError("Restore operation was cancelled by user.")

                    # Wait for at least one worker to finish
                    done, _ = concurrent.futures.wait(
                        active_futures.keys(),
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    for fut in done:
                        entry = active_futures.pop(fut)
                        try:
                            next_item = next(items_iter)
                            new_fut = executor.submit(_restore_one, next_item)
                            active_futures[new_fut] = next_item
                        except StopIteration:
                            pass

                        result = fut.result()
                        rel_path = entry.get("rel_path", "")

                        with lock:
                            if result[0] == "ok":
                                restored_files += 1
                                restored_bytes += result[1]
                                if len(result) > 2 and result[2]:
                                    reg_files_to_import.append(result[2])
                            elif result[0] == "skip":
                                skipped_files += 1
                            elif result[0] == "fail":
                                failed_files.append({
                                    "rel_path": result[2],
                                    "error": result[3]
                                })
                            # cancelled: just skip

                            done_count = restored_files + skipped_files + len(failed_files)
                            if progress_callback and (done_count % 5 == 0 or done_count == total_files):
                                pct = round((done_count / max(1, total_files)) * 100, 1)
                                try:
                                    progress_callback({
                                        "type": "progress",
                                        "current_file": rel_path,
                                        "restored_files": restored_files,
                                        "skipped_files": skipped_files,
                                        "failed_files": len(failed_files),
                                        "total_files": total_files,
                                        "percent": pct,
                                        "restored_bytes": restored_bytes
                                    })
                                except Exception:
                                    pass

        # Fix: Safely import registry files sequentially after all files are restored
        if reg_files_to_import:
            try:
                from core.registry_backup import import_registry_file
                for rf in reg_files_to_import:
                    try:
                        import_registry_file(rf)
                    except Exception:
                        pass
            except Exception:
                pass

        # Post-restore: Refresh desktop shortcuts & links to guarantee no broken links
        try:
            proj_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            shortcut_script = os.path.join(proj_dir, "create_desktop_shortcut.py")
            if os.path.exists(shortcut_script):
                import subprocess, sys
                kwargs = {"capture_output": True, "timeout": 10}
                if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.run([sys.executable, shortcut_script], **kwargs)
        except Exception:
            pass

        duration = time.time() - start_time
        return {
            "snapshot_id": snapshot_id,
            "target_dir": target_dir if not in_place else "ORIGINAL_LOCATION",
            "total_files": total_files,
            "restored_files": restored_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "restored_bytes": restored_bytes,
            "duration_seconds": round(duration, 2)
        }

    @classmethod
    def verify_snapshot_integrity(
        cls,
        repo_dir: str,
        snapshot_id: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """
        Verifies that all blobs referenced by this snapshot are present and uncorrupted.
        Fix #3: Removed 'if False:' dead code — actual_hash was always None.
        """
        storage = BlobStorage(repo_dir)
        snapshot = SnapshotEngine.get_snapshot(repo_dir, snapshot_id)
        if not snapshot:
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found.")

        entries = snapshot.get("entries", [])
        total_files = len(entries)
        verified_count = 0
        missing_blobs = []
        corrupted_blobs = []

        for i, entry in enumerate(entries):
            blob_id = entry.get("blob_id") or entry.get("sha256")
            rel_path = entry.get("rel_path")
            chunk_ids = entry.get("chunk_ids")

            # 대용량 파일 분할 청크 검증 로직
            if chunk_ids and isinstance(chunk_ids, list):
                # 1. 모든 청크의 존재 여부 확인
                missing_chunks = [cid for cid in chunk_ids if not storage.has_blob(cid)]
                if missing_chunks:
                    missing_blobs.append({
                        "rel_path": rel_path,
                        "blob_id": blob_id,
                        "missing_chunks": missing_chunks
                    })
                    continue

                # 2. 청크별 SHA-256 무결성 검증
                try:
                    is_corrupted = False
                    for cid in chunk_ids:
                        data = storage.read_blob_bytes(cid)
                        h = hashlib.sha256(data).hexdigest()
                        if h != cid:
                            log.error(f"청크 해시 불일치 감지 [{rel_path}] (청크: {cid}, 예상: {cid}, 실제: {h})")
                            is_corrupted = True
                            break
                    if is_corrupted:
                        corrupted_blobs.append({"rel_path": rel_path, "blob_id": blob_id, "type": "chunk_hash_mismatch"})
                    else:
                        verified_count += 1
                except Exception as e:
                    log.error(f"청크 검증 중 오류 발생 [{rel_path}]: {e}", exc_info=True)
                    corrupted_blobs.append({"rel_path": rel_path, "blob_id": blob_id, "error": str(e)})
            else:
                # 기존 단일 블롭 검증 로직
                if not blob_id or not storage.has_blob(blob_id):
                    missing_blobs.append({"rel_path": rel_path, "blob_id": blob_id})
                    continue

                try:
                    # Read and decompress to verify SHA-256 hash (Fix #3: removed dead 'if False:' block)
                    data = storage.read_blob_bytes(blob_id)
                    h = hashlib.sha256(data).hexdigest()
                    if h != blob_id:
                        log.error(f"블롭 해시 불일치 감지 [{rel_path}] (예상: {blob_id}, 실제: {h})")
                        corrupted_blobs.append({"rel_path": rel_path, "blob_id": blob_id})
                    else:
                        verified_count += 1
                except Exception as e:
                    log.error(f"손상된 블롭 감지 [{rel_path}] ({blob_id}): {e}", exc_info=True)
                    corrupted_blobs.append({"rel_path": rel_path, "blob_id": blob_id, "error": str(e)})

            if progress_callback and (i % 10 == 0 or i == total_files - 1):
                try:
                    progress_callback({
                        "verified": verified_count,
                        "total": total_files,
                        "percent": round(((i + 1) / max(1, total_files)) * 100, 1)
                    })
                except Exception:
                    pass

        is_valid = (len(missing_blobs) == 0 and len(corrupted_blobs) == 0)
        return {
            "snapshot_id": snapshot_id,
            "is_valid": is_valid,
            "total_files": total_files,
            "verified_files": verified_count,
            "missing_blobs": missing_blobs,
            "corrupted_blobs": corrupted_blobs
        }
