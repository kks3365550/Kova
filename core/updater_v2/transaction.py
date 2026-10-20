# -*- coding: utf-8 -*-
"""
core/updater_v2/transaction.py: OTA 트랜잭션 상태 머신 및 Fail-Closed Self-Healing 복구 엔진

상태 전이:
    STABLE
      ↓
    BACKUP_READY
      ↓
    UPDATE_IN_PROGRESS (SWAP)
      ↓
    SWAP_COMPLETE
      ↓
    HEALTHCHECK_OK
      ↓
    COMMITTED
      ↓
    marker 삭제

복구 전략 (Preflight):
    - marker 없음: 정상 기동 (Fast-path)
    - marker 있음 + 백업 무결성 통과:
        rollback 수행 -> 복원 검증 -> marker 제거 -> 이전 버전 정상 기동 (Self-Healing)
    - marker 있음 + 백업 손상/부재:
        marker 삭제 금지 -> FAIL-CLOSED (기동 차단 및 치명적 오류 로깅)
"""

from __future__ import annotations

import os
import json
import shutil
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Optional, Tuple, Any

logger = logging.getLogger("updater_v2.transaction")

MARKER_FILENAME = ".update_transaction.json"
BACKUP_MANIFEST_FILENAME = "backup_manifest.json"

UPDATABLE_DIRS = ["core", "static", "templates", "web"]
UPDATABLE_FILES = ["app.py", "VERSION", "requirements.txt", "run.py"]


class UpdatePhase(str, Enum):
    """트랜잭션 순차 상태 정의"""
    STABLE = "STABLE"
    BACKUP_READY = "BACKUP_READY"
    UPDATE_IN_PROGRESS = "UPDATE_IN_PROGRESS"
    SWAP_COMPLETE = "SWAP_COMPLETE"
    HEALTHCHECK_OK = "HEALTHCHECK_OK"
    COMMITTED = "COMMITTED"


class FailClosedRecoveryError(Exception):
    """백업 손상 또는 불일치로 인한 Fail-Closed 기동 차단 예외"""
    pass


def compute_file_sha256(file_path: str) -> str:
    """파일의 SHA-256 해시를 64KB 단위로 계산"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def compute_bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


class UpdateTransactionManager:
    """
    원자적 소프트웨어 업데이트 트랜잭션 관리자
    """

    def __init__(
        self,
        target_dir: str,
        debug_crash_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ):
        self.target_dir = os.path.abspath(target_dir)
        self.marker_path = os.path.join(self.target_dir, MARKER_FILENAME)
        self.debug_crash_hook = debug_crash_hook or (lambda step, ctx: None)

    def _invoke_hook(self, step_id: str, context: Optional[Dict[str, Any]] = None) -> None:
        """디버그 크래시 주입 훅 호출"""
        try:
            self.debug_crash_hook(step_id, context or {})
        except Exception as e:
            # 훅 자체에서 크래시/종료를 일으키는 것이 아닌 일반 예외는 로깅
            logger.debug("Crash hook %s executed (%s)", step_id, e)
            raise

    def _write_marker_atomic(self, marker_dict: Dict[str, Any]) -> None:
        """원자적 임시파일 교체 방식으로 마커 파일 쓰기"""
        tmp_path = self.marker_path + f".tmp_{uuid.uuid4().hex[:8]}"
        content = json.dumps(marker_dict, indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.marker_path)

    def build_and_save_backup_manifest(self, backup_dir: str) -> str:
        """
        백업 디렉터리 내의 모든 파일 목록 및 SHA-256 해시 매니페스트 생성
        반환값: backup_manifest.json의 SHA-256 해시
        """
        files_manifest: Dict[str, str] = {}
        for root, _, filenames in os.walk(backup_dir):
            for fname in filenames:
                if fname == BACKUP_MANIFEST_FILENAME:
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, backup_dir).replace("\\", "/")
                files_manifest[rel_path] = compute_file_sha256(fpath)

        manifest_data = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files_manifest),
            "files": files_manifest
        }
        manifest_bytes = json.dumps(manifest_data, indent=2, sort_keys=True).encode("utf-8")
        manifest_path = os.path.join(backup_dir, BACKUP_MANIFEST_FILENAME)

        with open(manifest_path, "wb") as f:
            f.write(manifest_bytes)
            f.flush()
            os.fsync(f.fileno())

        return compute_bytes_sha256(manifest_bytes)

    def verify_backup_integrity(self, backup_dir: str, expected_manifest_sha256: str) -> Tuple[bool, str]:
        """
        백업 디렉터리의 무결성 검증:
        1. backup_dir 존재 여부
        2. backup_manifest.json 존재 및 해시 일치
        3. 매니페스트 내 모든 파일의 존재 및 SHA-256 일치
        """
        if not os.path.exists(backup_dir) or not os.path.isdir(backup_dir):
            return False, f"Backup directory does not exist: {backup_dir}"

        manifest_path = os.path.join(backup_dir, BACKUP_MANIFEST_FILENAME)
        if not os.path.exists(manifest_path):
            return False, f"Backup manifest missing: {manifest_path}"

        try:
            with open(manifest_path, "rb") as f:
                manifest_bytes = f.read()
            actual_manifest_sha = compute_bytes_sha256(manifest_bytes)
            if actual_manifest_sha != expected_manifest_sha256:
                return False, f"Manifest hash mismatch: expected {expected_manifest_sha256}, got {actual_manifest_sha}"

            manifest_data = json.loads(manifest_bytes.decode("utf-8"))
            files_dict = manifest_data.get("files", {})
            if not files_dict:
                return False, "Backup manifest contains no files"

            for rel_path, expected_file_sha in files_dict.items():
                target_f = os.path.join(backup_dir, rel_path.replace("/", os.sep))
                if not os.path.exists(target_f):
                    return False, f"Missing backup file: {rel_path}"
                actual_file_sha = compute_file_sha256(target_f)
                if actual_file_sha != expected_file_sha:
                    return False, f"Hash mismatch for {rel_path}: expected {expected_file_sha}, got {actual_file_sha}"

            return True, "Backup integrity verified"
        except Exception as e:
            return False, f"Backup verification exception: {e}"

    def start_transaction(
        self,
        previous_version: str,
        target_version: str,
        backup_dir: str
    ) -> Dict[str, Any]:
        """
        트랜잭션 시작: 백업 매니페스트 빌드 및 BACKUP_READY 마커 파일 생성
        """
        # 백업 무결성 매니페스트 생성 및 해시 계산
        manifest_sha = self.build_and_save_backup_manifest(backup_dir)

        marker_dict = {
            "transaction_id": uuid.uuid4().hex,
            "previous_version": previous_version,
            "target_version": target_version,
            "backup_dir": os.path.abspath(backup_dir),
            "phase": UpdatePhase.BACKUP_READY.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backup_manifest_sha256": manifest_sha
        }

        self._write_marker_atomic(marker_dict)
        logger.info(
            "TRANSACTION_STARTED id=%s prev=%s target=%s phase=BACKUP_READY",
            marker_dict["transaction_id"], previous_version, target_version
        )
        self._invoke_hook("KILL-01", marker_dict)
        return marker_dict

    def transition_to(self, marker_dict: Dict[str, Any], new_phase: UpdatePhase) -> Dict[str, Any]:
        """트랜잭션 상태 전이 및 마커 파일 원자적 갱신"""
        marker_dict["phase"] = new_phase.value
        marker_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_marker_atomic(marker_dict)
        logger.info("TRANSACTION_TRANSITION id=%s -> %s", marker_dict["transaction_id"], new_phase.value)
        return marker_dict

    def cleanup_marker(self, marker_dict: Dict[str, Any]) -> None:
        """정상 커밋 후 마커 파일 삭제"""
        if os.path.exists(self.marker_path):
            os.remove(self.marker_path)
            logger.info("TRANSACTION_CLEANUP_SUCCESS marker removed (id=%s)", marker_dict.get("transaction_id"))

    def recover_interrupted_update(self) -> Tuple[bool, str]:
        """
        비정상 중단된 업데이트 복구 (Self-Healing)
        반환값: (성공 여부, 설명 메시지)
        - 마커 없음 -> (True, "No pending transaction")
        - 마커 있음 + 백업 유효 -> 롤백 복원 -> 복원 검증 -> 마커 삭제 -> (True, "Recovery successful")
        - 마커 있음 + 백업 손상 -> Fail-Closed (마커 유지, 기동 차단) -> (False, "Fail-Closed...")
        """
        if not os.path.exists(self.marker_path):
            return True, "No pending update transaction"

        logger.warning("INTERRUPTED_TRANSACTION_DETECTED at %s", self.marker_path)

        try:
            with open(self.marker_path, "r", encoding="utf-8") as f:
                marker = json.load(f)
        except Exception as e:
            logger.critical("TRANSACTION_MARKER_CORRUPT: %s", e)
            return False, f"Transaction marker is corrupted and unreadable: {e}"

        txn_id = marker.get("transaction_id", "unknown")
        prev_ver = marker.get("previous_version", "unknown")
        backup_dir = marker.get("backup_dir", "")
        manifest_sha = marker.get("backup_manifest_sha256", "")
        phase = marker.get("phase", "UNKNOWN")

        logger.info("RECOVERY_ANALYZING txn_id=%s phase=%s target_prev_ver=%s", txn_id, phase, prev_ver)

        # 1. 백업 무결성 엄격 검증 (Fail-Closed 핵심)
        is_valid, reason = self.verify_backup_integrity(backup_dir, manifest_sha)
        if not is_valid:
            error_msg = f"FAIL-CLOSED: Backup integrity verification failed ({reason}). Refusing to start to prevent data loss."
            logger.critical("TRANSACTION_RECOVERY_ABORTED %s", error_msg)
            # 마커를 절대 지우지 않음!
            return False, error_msg

        # 2. 파일 롤백 복원 수행
        try:
            manifest_path = os.path.join(backup_dir, BACKUP_MANIFEST_FILENAME)
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)

            files_dict = manifest_data.get("files", {})

            for rel_path in files_dict.keys():
                src_f = os.path.join(backup_dir, rel_path.replace("/", os.sep))
                dst_f = os.path.join(self.target_dir, rel_path.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst_f), exist_ok=True)
                shutil.copy2(src_f, dst_f)

            # 3. 복원 결과 검증 (VERSION 확인)
            version_file = os.path.join(self.target_dir, "VERSION")
            if os.path.exists(version_file):
                with open(version_file, "r", encoding="utf-8") as vf:
                    restored_ver = vf.read().strip()
                if restored_ver != prev_ver:
                    logger.critical("RECOVERY_VERIFICATION_FAILED restored_version=%s expected=%s", restored_ver, prev_ver)
                    return False, f"VERSION file mismatch after restore: got {restored_ver}, expected {prev_ver}"

            # 4. 복구 완결: 마커 안전 제거
            self.cleanup_marker(marker)
            msg = f"Self-Healing recovery successful: restored to v{prev_ver} from interrupted phase {phase}"
            logger.info("TRANSACTION_RECOVERY_SUCCESS %s", msg)
            return True, msg

        except Exception as e:
            logger.critical("TRANSACTION_RECOVERY_EXCEPTION error=%s", str(e))
            return False, f"Exception during rollback recovery: {e}"


def check_and_recover_preflight(base_dir: str) -> Tuple[bool, str]:
    """
    run.py 엔트리포인트 최우선 단계에서 호출되는 Preflight 복구 래퍼
    """
    mgr = UpdateTransactionManager(target_dir=base_dir)
    return mgr.recover_interrupted_update()
