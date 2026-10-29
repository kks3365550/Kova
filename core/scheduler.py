import os
import json
import glob
import time
import threading
import datetime
from typing import Dict, Any, Optional, Callable, List
from core.config import ConfigManager
from core.snapshot import SnapshotEngine
from core.lock import BackupAlreadyRunningError

# 자동 백업 실패 시 재시도 정책 (최대 3회, 실패 후 30분 간격)
MAX_AUTO_RETRIES = 3
RETRY_COOLDOWN_SEC = 1800


class BackupScheduler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BackupScheduler, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.active_jobs: Dict[str, Dict[str, Any]] = {}
        self.log_callbacks: list = []
        self.stop_event = threading.Event()
        self.audit_thread: Optional[threading.Thread] = None
        self.audit_lock = threading.Lock()
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.audit_state_file = os.path.join(base_dir, "config", "deep_scan_state.json")

    def start(self):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="BackupSchedulerThread")
        self.thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def register_log_callback(self, callback: Callable[[str, str], None]):
        self.log_callbacks.append(callback)

    def _log(self, profile_name: str, message: str, level: str = "INFO"):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{ts}] [{level}] [{profile_name}] {message}"
        for cb in self.log_callbacks:
            try:
                cb(formatted, level)
            except Exception:
                pass

    def _should_run_profile(self, profile: Dict[str, Any]) -> bool:
        if not profile.get("auto_backup_enabled", False):
            return False

        # 실패 직후 재시도 (일정 간격, 최대 횟수 제한). 한도를 넘으면 정상 스케줄을 따른다.
        if profile.get("last_status") == "failed":
            retry_count = int(profile.get("retry_count", 0) or 0)
            failed_at = profile.get("last_run") or 0
            if retry_count < MAX_AUTO_RETRIES and (time.time() - failed_at) >= RETRY_COOLDOWN_SEC:
                return True

        last_run = profile.get("last_run")
        sched_type = profile.get("schedule_type", "interval_hours")
        sched_val = profile.get("schedule_value", "12")

        if not last_run:
            return True

        now = time.time()
        elapsed_sec = now - last_run

        if sched_type == "interval_hours":
            try:
                interval_hours = float(sched_val)
                return elapsed_sec >= (interval_hours * 3600)
            except ValueError:
                return False

        elif sched_type == "daily":
            # Format: "HH:MM" e.g. "03:00"
            try:
                hour, minute = map(int, sched_val.split(":"))
                now_dt = datetime.datetime.now()
                target_today = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # Fix #12: Prevent re-trigger loop — require at least 1h since last run to avoid
                # re-triggering on the same day after a quick failure/retry within the same minute.
                if elapsed_sec < 3600:
                    return False

                # If target time today has passed, and last_run was before target time today
                if now_dt >= target_today:
                    target_ts = target_today.timestamp()
                    return last_run < target_ts
                return False
            except Exception:
                return False

        return False

    def _run_profile_backup(self, profile: Dict[str, Any]):
        profile_id = profile["id"]
        profile_name = profile.get("name", profile_id)

        if profile_id in self.active_jobs and self.active_jobs[profile_id].get("running"):
            return  # Already running

        cancel_evt = threading.Event()
        self.active_jobs[profile_id] = {
            "profile_name": profile_name,
            "running": True,
            "start_time": time.time(),
            "cancel_event": cancel_evt,
            "progress": {}
        }

        self._log(profile_name, "스케줄 자동 백업 작업을 시작합니다...")

        try:
            repo_dir = profile.get("repo_dir")
            sources = profile.get("sources", [])
            excludes = profile.get("exclude_patterns", [])
            compress = profile.get("compression_level", 6)
            retention_count = profile.get("retention_count", 30)
            retention_days = profile.get("retention_days", 60)

            def on_progress(p_data):
                if profile_id in self.active_jobs:
                    self.active_jobs[profile_id]["progress"] = p_data

            manifest = None
            used_rust_manifest = False
            use_rust = (ConfigManager.get_settings().get("backup_engine", "rust") == "rust")

            if use_rust:
                try:
                    from core.rust_adapter import run_rust_backup, find_rust_engine_binary
                    if find_rust_engine_binary():
                        session_id = f"snap_sched_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
                        self._log(profile_name, f"[초고속 Rust 네이티브 엔진 가동] 세션: {session_id}")

                        from core.lock import BackupLock
                        with BackupLock(repo_dir, timeout_sec=1, process_desc="스케줄러 Rust 백업"):
                            res = run_rust_backup(
                                sources=sources,
                                repo_dir=repo_dir,
                                session_id=session_id,
                                zstd_level=compress,
                                progress_callback=on_progress,
                                cancel_event=cancel_evt,
                                exclude_patterns=excludes,
                                profile_name=profile_name,
                                profile_id=profile_id
                            )
                        with open(res.manifest_path, "r", encoding="utf-8") as fp:
                            manifest = json.load(fp)
                        used_rust_manifest = True
                except BackupAlreadyRunningError:
                    raise
                except Exception as e_rust:
                    self._log(profile_name, f"[경고] Rust 엔진 오류 ({e_rust}), Python 엔진으로 자동 폴백합니다.", level="WARNING")

            if not manifest:
                manifest = SnapshotEngine.create_snapshot(
                    repo_dir=repo_dir,
                    sources=sources,
                    profile_id=profile_id,
                    profile_name=profile_name,
                    exclude_patterns=excludes,
                    compress_level=compress,
                    progress_callback=on_progress,
                    cancel_event=cancel_evt
                )

            # Rust 엔진 결과에도 자동 복원 검증 적용 (Python 엔진 경로는 SnapshotEngine이 이미 수행)
            if used_rust_manifest:
                self._verify_rust_snapshot(profile_name, repo_dir, manifest)

            # Prune old snapshots
            try:
                pruned = SnapshotEngine.prune_snapshots(
                    repo_dir=repo_dir,
                    retention_count=retention_count,
                    max_age_days=retention_days,
                    authorized=True
                )
            except Exception as e_prune:
                pruned = []
                self._log(profile_name, f"[경고] 만료 스냅샷 정리를 건너뜁니다(백업은 성공): {e_prune}", level="WARNING")

            # Update profile info
            profile["last_run"] = time.time()
            profile["last_status"] = "success"
            profile["retry_count"] = 0
            profile["last_snapshot_id"] = manifest["id"]
            ConfigManager.save_profile(profile)

            summary = manifest.get("summary", {})
            self._log(
                profile_name,
                f"백업 완료: {summary.get('total_files', 0)}개 파일 ({round(summary.get('total_bytes', 0)/(1024*1024), 2)}MB), "
                f"신규/수정 {summary.get('new_files', 0) + summary.get('modified_files', 0)}개, "
                f"중복제거 절감 {round(summary.get('dedup_saved_bytes', 0)/(1024*1024), 2)}MB (소요 {summary.get('duration_seconds', 0)}초)"
            )
            if pruned:
                self._log(profile_name, f"보관 주기 만료 스냅샷 {len(pruned)}개 정리 완료.")

            # 3-2-1 오프사이트 원격 복제 자동 실행
            try:
                offsite_cfg = ConfigManager.get_offsite_config()
                if offsite_cfg.get("enabled") and offsite_cfg.get("auto_sync"):
                    provider = offsite_cfg.get("provider", "local")
                    from core.offsite import replicate_offsite

                    if provider == "gdrive":
                        creds = offsite_cfg.get("gdrive_credentials_file")
                        folder = offsite_cfg.get("gdrive_folder_id") or "BackupSystem_Offsite"
                        if creds:
                            self._log(profile_name, f"[3-2-1 오프사이트] 구글 드라이브(Google Drive/{folder})로 자동 복제 시작...")
                            off_res = replicate_offsite(
                                local_repo_dir=repo_dir,
                                provider="gdrive",
                                gdrive_credentials_file=creds,
                                gdrive_folder_id=folder
                            )
                            if off_res.get("success"):
                                self._log(profile_name, f"[3-2-1 오프사이트] 구글 드라이브 동기화 성공: 복제 {off_res.get('files_copied', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                            else:
                                self._log(profile_name, f"[3-2-1 오프사이트] 구글 드라이브 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARN")
                    else:
                        remote_path = offsite_cfg.get("remote_path")
                        if remote_path:
                            self._log(profile_name, f"[3-2-1 오프사이트] 원격 저장소({remote_path})로 자동 복제 시작...")
                            off_res = replicate_offsite(
                                local_repo_dir=repo_dir,
                                remote_repo_dir=remote_path,
                                provider="local"
                            )
                            if off_res.get("success"):
                                self._log(profile_name, f"[3-2-1 오프사이트] 동기화 성공: 복사 {off_res.get('files_copied', 0)}개 / 스킵 {off_res.get('files_skipped', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                            elif not off_res.get("skipped"):
                                self._log(profile_name, f"[3-2-1 오프사이트] 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARN")
                            else:
                                self._log(profile_name, f"[3-2-1 오프사이트] 동기화 건너뜀: {off_res.get('reason') or off_res.get('error') or '원인 불명'}", level="WARN")
            except Exception as e_off:
                self._log(profile_name, f"[3-2-1 오프사이트] 동기화 오류: {e_off}", level="WARN")

        except Exception as e:
            self._log(profile_name, f"백업 중 오류 발생: {str(e)}", level="ERROR")
            profile["last_status"] = "failed"
            profile["last_run"] = time.time()
            # 사용자 취소는 재시도하지 않고, 알림도 보내지 않는다
            if cancel_evt.is_set() or isinstance(e, InterruptedError):
                profile["retry_count"] = MAX_AUTO_RETRIES
            else:
                profile["retry_count"] = int(profile.get("retry_count", 0) or 0) + 1
                self._notify_failure(profile_name, str(e))
            ConfigManager.save_profile(profile)

        finally:
            if profile_id in self.active_jobs:
                self.active_jobs[profile_id]["running"] = False
                self.active_jobs[profile_id]["end_time"] = time.time()

    def _notify_failure(self, profile_name: str, message: str) -> None:
        try:
            from core.windows_toast import notify_backup_failed
            notify_backup_failed(profile_name, message)
        except Exception:
            pass

    def _verify_rust_snapshot(self, profile_name: str, repo_dir: str, manifest: Dict[str, Any]) -> None:
        from core.verify import IntegrityVerifier, RestoreVerificationError
        entries = manifest.get("entries")
        if not entries:
            # Rust 엔진은 files(path, chunks[{hash}]) 형식으로 기록하므로 검증용 entries로 변환한다
            entries = []
            for f in manifest.get("files") or []:
                chunks = f.get("chunks") or []
                if f.get("is_dir") or not chunks:
                    continue
                hashes = [c.get("hash") for c in chunks if c.get("hash")]
                if not hashes:
                    continue
                entries.append({
                    "rel_path": f.get("path", ""),
                    "blob_id": hashes[0],
                    "chunk_ids": hashes,
                    "size": f.get("size", -1),
                })
        if not entries:
            self._log(profile_name, "[경고] Rust 매니페스트에 검증할 파일이 없어 자동 복원 검증을 생략했습니다.", level="WARNING")
            return
        try:
            verifier = IntegrityVerifier(repo_dir)
            res = verifier.verify_restore_sampling({**manifest, "entries": entries, "verify_content_hash": False}, sample_count=20)
        except RestoreVerificationError:
            raise
        except Exception as e:
            self._log(profile_name, f"[경고] 자동 복원 검증을 수행하지 못했습니다: {e}", level="WARNING")
            return
        if res.get("status") == "skipped":
            self._log(profile_name, f"[경고] 자동 복원 검증 생략: {res.get('detail', '')}", level="WARNING")
            return
        if res.get("status") != "passed":
            raise RestoreVerificationError(
                f"자동 복원 검증 실패 (Rust 엔진 백업): {res.get('status')} {res.get('detail', '')}".strip()
            )
        self._log(profile_name, "[자동 복원 검증] 샘플 복원 대조 통과")

    def _load_audit_state(self) -> Dict[str, Any]:
        if os.path.exists(self.audit_state_file):
            try:
                with open(self.audit_state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_audit_state(self, state: Dict[str, Any]):
        try:
            os.makedirs(os.path.dirname(self.audit_state_file), exist_ok=True)
            tmp = self.audit_state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            if os.path.exists(self.audit_state_file):
                os.remove(self.audit_state_file)
            os.replace(tmp, self.audit_state_file)
        except Exception:
            pass

    def _is_backup_running(self) -> bool:
        return any(job.get("running") for job in self.active_jobs.values())

    def _run_tier2_weekly_audit(self, repo_dir: str):
        """Tier 2: 주간(Weekly) 모든 스냅샷의 Manifest 서명 및 지문 무결성 검증"""
        self._log("DeepScan", f"[Tier 2] 주간 Manifest 무결성 검사를 시작합니다 (저장소: {repo_dir})")
        snaps_dir = os.path.join(repo_dir, "snapshots")
        if not os.path.isdir(snaps_dir):
            return

        from core.crypto_sign import Ed25519Signer
        signer = Ed25519Signer(repo_dir)
        has_pubkey = os.path.exists(signer.public_key_path)

        snap_files = glob.glob(os.path.join(snaps_dir, "*.json"))
        corrupted_snaps = []

        for sf in snap_files:
            try:
                with open(sf, "r", encoding="utf-8") as fp:
                    m = json.load(fp)

                # 1. Ed25519 서명 검증
                if has_pubkey and "ed25519_signature" in m:
                    if not signer.verify_manifest(m):
                        corrupted_snaps.append(f"{os.path.basename(sf)}: Ed25519 서명 위조/불일치")
                        continue

                # 2. Manifest 파일 지문 검증
                from core.verify import generate_manifest_signature
                entries = m.get("entries", [])
                expected_sig = m.get("manifest_signature")
                if expected_sig and entries:
                    calc_sig = generate_manifest_signature(entries)
                    if calc_sig != expected_sig:
                        corrupted_snaps.append(f"{os.path.basename(sf)}: Manifest 파일 지문 불일치")
            except Exception as e:
                corrupted_snaps.append(f"{os.path.basename(sf)}: 파싱 에러 ({str(e)})")

        if corrupted_snaps:
            err_msg = f"[Tier 2 경고] {len(corrupted_snaps)}개 스냅샷 무결성 손상 감지! " + "; ".join(corrupted_snaps[:5])
            self._log("DeepScan", err_msg, level="ERROR")
        else:
            self._log("DeepScan", f"[Tier 2 통과] {len(snap_files)}개 스냅샷 Manifest 서명 및 지문 무결성 100% 정상")

    def _run_tier3_monthly_audit(self, repo_dir: str):
        """Tier 3: 월간(Monthly) 저장소 내 모든 블롭(.blob)의 Bit-Rot 전수 감사"""
        self._log("DeepScan", f"[Tier 3] 월간 심야 전체 블롭 Bit-Rot 감사를 시작합니다 (저장소: {repo_dir})")
        try:
            from disaster_recovery import audit_repository
            res = audit_repository(repo_dir)
            total = res.get("total", 0)
            corrupted = res.get("corrupted", 0)
            if corrupted > 0:
                err_msg = f"[Tier 3 비상] 저장소 내 {corrupted}개의 손상된 Bit-Rot 블롭이 발견되었습니다! (총 {total}개 중)"
                self._log("DeepScan", err_msg, level="ERROR")
            else:
                self._log("DeepScan", f"[Tier 3 통과] 총 {total}개 블롭 SHA-256 Bit-Rot 무결성 100% 정상 확인")
        except Exception as e:
            self._log("DeepScan", f"[Tier 3 감사 실패] {str(e)}", level="ERROR")

    def _check_deep_scans(self, profiles: List[Dict[str, Any]]):
        """Tier 2/Tier 3 주기 검사 및 심야 스케줄링 트리거"""
        if self._is_backup_running():
            return  # 백업 실행 중일 때는 감사 보류

        with self.audit_lock:
            if self.audit_thread and self.audit_thread.is_alive():
                return  # 이미 감사 스레드가 구동 중

            repo_dirs = set()
            for p in profiles:
                r = p.get("repo_dir")
                if r and os.path.isdir(r):
                    repo_dirs.add(os.path.abspath(r))

            if not repo_dirs:
                return

            state = self._load_audit_state()
            now = time.time()
            now_dt = datetime.datetime.now()
            is_night_window = (2 <= now_dt.hour < 5)  # 새벽 02:00 ~ 05:00

            for r in repo_dirs:
                r_key = r.lower()
                r_state = state.setdefault(r_key, {"last_weekly": 0.0, "last_monthly": 0.0})

                # Tier 2: 주간 검사 (7일 = 604,800초)
                if (now - r_state.get("last_weekly", 0.0)) >= 604800:
                    r_state["last_weekly"] = now
                    self._save_audit_state(state)
                    self.audit_thread = threading.Thread(
                        target=self._run_tier2_weekly_audit,
                        args=(r,),
                        daemon=True,
                        name=f"Tier2Audit-{os.path.basename(r)}"
                    )
                    self.audit_thread.start()
                    return

                # Tier 3: 월간 심야 Bit-Rot 검사 (30일 = 2,592,000초 & 심야 시간대)
                if (now - r_state.get("last_monthly", 0.0)) >= 2592000 and is_night_window:
                    r_state["last_monthly"] = now
                    self._save_audit_state(state)
                    self.audit_thread = threading.Thread(
                        target=self._run_tier3_monthly_audit,
                        args=(r,),
                        daemon=True,
                        name=f"Tier3Audit-{os.path.basename(r)}"
                    )
                    self.audit_thread.start()
                    return

    def _run_loop(self):
        while not self.stop_event.is_set():
            try:
                profiles = ConfigManager.get_profiles()
                for prof in profiles:
                    prof_id = prof.get("id")
                    if prof_id and self.active_jobs.get(prof_id, {}).get("running"):
                        continue
                    if self._should_run_profile(prof):
                        # Run in worker thread
                        worker = threading.Thread(
                            target=self._run_profile_backup,
                            args=(prof,),
                            daemon=True
                        )
                        worker.start()

                # Deep Scan 주기 점검
                self._check_deep_scans(profiles)
            except Exception as e:
                self._log("Scheduler", f"스케줄러 루프 오류: {e}", level="ERROR")

            # Sleep 30 seconds before next check
            self.stop_event.wait(timeout=30)
