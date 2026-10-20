# -*- coding: utf-8 -*-
"""
core/updater_v2/installer.py: 원자적 설치(Atomic Installation) 및 롤백 제어기

원칙 및 방어선:
1. 대상 디렉토리 오염 방지:
   - blobs, snapshots, D:\\ 백업 리포지토리 등 데이터 디렉토리는 절대 건드리지 않습니다.
   - 코드 대상 디렉토리: ["core", "web", "keys"], 스크립트: ["run.py", "start_silent.vbs", "VERSION"]
2. 무창(Ghost/Silent) 백그라운드 프로세스 제어:
   - 포트 8765를 점유하는 백업 서버만 선별 종료 (PowerShell Get-NetTCPConnection 기반)
   - Windows cmd창 깜빡임 영구 금지 (CREATE_NO_WINDOW 플래그 필수)
   - 재시작 시 start_silent.vbs 호출 (wscript.exe, 창 스타일 0)
3. 파일 잠금(Lock) 및 TOCTOU 방어:
   - 교체 전 임시 Staging 디렉토리에 우선 검증 해제
   - ArtifactSafetyChecker(5대 안전 수칙 & 4축 Zip Bomb 검사) 사전 전수 검사
   - assert_safe_destination_path로 모든 압축 해제 대상 경로의 Traversal 및 Reparse Point 차단
   - 파일 복사 시 최대 3회 재시도(Retry) 및 백오프(0.5s, 1.0s, 2.0s)
4. 실패 시 100% 자동 원복:
   - 헬스체크(포트 8765 소켓 연결) 실패 시 즉시 backup/v{현재버전}에서 원복 후 재기동
"""

import io
import os
import sys
import time
import shutil
import socket
import logging
import tempfile
import zipfile
import subprocess
from typing import Optional, Callable, Dict, Any

from .artifact_safety import (
    ArtifactSafetyChecker,
    assert_safe_destination_path
)
from .transaction import UpdateTransactionManager, UpdatePhase

logger = logging.getLogger(__name__)

# 업데이트 대상 디렉토리 및 파일 (화이트리스트)
UPDATABLE_DIRS = ["core", "web", "keys"]
UPDATABLE_FILES = ["run.py", "start_silent.vbs", "VERSION"]


class InstallerError(Exception):
    """설치 실패 기본 예외"""
    pass


class HealthCheckTimeoutError(InstallerError):
    """업데이트 후 서비스 헬스체크 타임아웃"""
    pass


class AtomicInstaller:
    """안전한 백업 및 원자적 파일 교체, 무창 재기동, 롤백을 수행하는 설치 엔진"""

    def __init__(self, target_dir: str, server_port: int = 8765):
        self.target_dir = os.path.abspath(target_dir)
        self.server_port = server_port
        self.safety_checker = ArtifactSafetyChecker()

    def get_current_installed_version(self) -> str:
        """VERSION 파일에서 현재 버전 문자열을 읽음"""
        ver_file = os.path.join(self.target_dir, "VERSION")
        if os.path.exists(ver_file):
            try:
                with open(ver_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return "0.0.0"

    def stop_server(self, timeout_sec: int = 8) -> bool:
        """
        포트 8765 점유 백업 서버 프로세스만 선별 종료.
        Windows 환경에서 창 깜빡임 없이 무창(CREATE_NO_WINDOW) 실행.
        """
        if not sys.platform.startswith("win"):
            return True

        ps_script = (
            f"$conn = Get-NetTCPConnection -LocalPort {self.server_port} -ErrorAction SilentlyContinue; "
            "if ($conn) { foreach ($c in $conn) { "
            "  $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
            "  if ($p) { "
            "    $cmd = (Get-CimInstance Win32_Process -Filter \"ProcessId = $($p.Id)\").CommandLine; "
            "    if ($cmd -like '*백업시스템*' -or $cmd -like '*run.py*') { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } "
            "  } "
            "} }; "
            "Start-Sleep -Seconds 1"
        )
        no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], timeout=timeout_sec, **no_win)
            return True
        except Exception as e:
            logger.warning("Installer stop_server warning: %s", e)
            return False

    def health_check(self, timeout_sec: int = 12) -> bool:
        """
        로컬 포트 8765 소켓 연결을 주기적으로 시도하여 서버 기동 여부 확인.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.server_port), timeout=1):
                    logger.info("INSTALLER_HEALTHCHECK_OK port=%d responding", self.server_port)
                    return True
            except OSError:
                time.sleep(0.5)

        logger.error("INSTALLER_HEALTHCHECK_FAILED port=%d did not respond within %ds", self.server_port, timeout_sec)
        return False

    def restart_server(self) -> bool:
        """
        start_silent.vbs 또는 pythonw를 통해 백업 서버를 백그라운드 무창 기동.
        """
        vbs_path = os.path.join(self.target_dir, "start_silent.vbs")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)

        try:
            if os.path.exists(vbs_path):
                subprocess.Popen(
                    ["wscript.exe", vbs_path],
                    cwd=self.target_dir,
                    creationflags=flags,
                    close_fds=True
                )
            else:
                pyw = os.path.join(self.target_dir, ".venv", "Scripts", "pythonw.exe")
                if not os.path.exists(pyw):
                    pyw = "pythonw.exe"
                subprocess.Popen(
                    [pyw, os.path.join(self.target_dir, "run.py")],
                    cwd=self.target_dir,
                    creationflags=flags,
                    close_fds=True
                )
            return True
        except Exception as e:
            logger.error("Failed to restart server: %s", e)
            return False

    def backup_current_installation(self, backup_dir: str) -> None:
        """
        현재 설치된 코드와 스크립트만 backup_dir로 안전 복사.
        데이터 폴더(blobs, snapshots, data)는 일체 복사하지 않음.
        """
        os.makedirs(backup_dir, exist_ok=True)
        for folder in UPDATABLE_DIRS:
            src_f = os.path.join(self.target_dir, folder)
            dst_f = os.path.join(backup_dir, folder)
            if os.path.exists(src_f):
                if os.path.exists(dst_f):
                    shutil.rmtree(dst_f, ignore_errors=True)
                shutil.copytree(src_f, dst_f)

        for script in UPDATABLE_FILES:
            src_s = os.path.join(self.target_dir, script)
            dst_s = os.path.join(backup_dir, script)
            if os.path.exists(src_s):
                shutil.copy2(src_s, dst_s)

    def rollback(self, backup_dir: str, skip_process_control: bool = False) -> bool:
        """
        백업 보존 디렉토리에서 이전 버전 파일 원복 및 서비스 재기동.
        skip_process_control=True일 경우 프로세스 제어(종료/재시작/헬스체크)를 건너뛰고 파일 원복만 수행합니다.
        """
        logger.warning("INSTALLER_ROLLBACK initiated from %s to %s (skip_process_control=%s)", backup_dir, self.target_dir, skip_process_control)
        if not os.path.exists(backup_dir):
            logger.error("INSTALLER_ROLLBACK_FAILED reason=backup_dir_missing")
            return False

        try:
            if not skip_process_control:
                self.stop_server(timeout_sec=5)

            # 이전 버전 파일 덮어쓰기 (재시도 로직 포함)
            for folder in UPDATABLE_DIRS:
                src_f = os.path.join(backup_dir, folder)
                dst_f = os.path.join(self.target_dir, folder)
                if os.path.exists(src_f):
                    self._copy_with_retry(src_f, dst_f)

            for script in UPDATABLE_FILES:
                src_s = os.path.join(backup_dir, script)
                dst_s = os.path.join(self.target_dir, script)
                if os.path.exists(src_s):
                    self._copy_with_retry(src_s, dst_s)

            if not skip_process_control:
                # 서비스 재시작
                self.restart_server()
                self.health_check(timeout_sec=10)

            logger.info("INSTALLER_ROLLBACK_SUCCESS restored to previous version")
            return True
        except Exception as e:
            logger.critical("INSTALLER_ROLLBACK_FAILED error=%s", str(e))
            return False

    def _copy_with_retry(self, src: str, dst: str, retries: int = 3) -> None:
        """
        Windows 파일 잠금 대응 재시도 복사
        """
        last_err = None
        for attempt in range(retries):
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                return
            except (PermissionError, FileExistsError, shutil.Error, OSError) as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
            except Exception as e:
                last_err = e
                break
        raise InstallerError(f"Failed to copy '{src}' to '{dst}' after {retries} retries: {last_err}")

    def install(
        self,
        raw_package_bytes: bytes,
        new_version: str,
        skip_process_control: bool = False,
        debug_crash_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ) -> bool:
        """
        패키지 바이트를 안전하게 설치합니다 (UpdateTransactionManager Self-Healing 연동).

        단계:
        0. 이전 미완료 트랜잭션 자가치유 점검 (Pre-install Recovery)
        1. 사전 안전성 검사 (ArtifactSafetyChecker: 5대 안전 수칙 & 4축 Zip Bomb 검사)
        2. Staging 임시 디렉토리에 안전 압축 해제 (각 경로 assert_safe_destination_path 검증)
        3. 현재 버전 백업 (backup/v{현재버전}) 및 트랜잭션 마커 생성 (BACKUP_READY)
        4. 백업 서버 프로세스 안전 종료 및 UPDATE_IN_PROGRESS 전이
        5. Staging -> target_dir 원자적 파일 교체 (재시도 로직 포함) 및 SWAP_COMPLETE 전이
        6. 백업 서버 백그라운드 재기동 및 헬스체크 (HEALTHCHECK_OK)
        7. 트랜잭션 커밋(COMMITTED) 및 마커 파일 정리
        """
        txn_mgr = UpdateTransactionManager(target_dir=self.target_dir, debug_crash_hook=debug_crash_hook)

        # 0. 기동 전 미완료 트랜잭션 자가치유 점검
        recovery_ok, recovery_msg = txn_mgr.recover_interrupted_update()
        if not recovery_ok:
            raise InstallerError(f"Pre-install recovery failed: {recovery_msg}")

        current_ver = self.get_current_installed_version()
        logger.info("INSTALL_START current=%s target_version=%s", current_ver, new_version)

        # 1. 아티팩트 무해성 및 시스템 안전 검사 (압축 해제 전)
        is_safe, errors = self.safety_checker.check_archive(raw_package_bytes)
        if not is_safe:
            raise InstallerError(f"Package safety verification failed: {'; '.join(errors)}")

        staging_dir = tempfile.mkdtemp(prefix="backup_stage_")
        backup_ver_dir = os.path.join(self.target_dir, "backup", f"v{current_ver}")
        marker = None

        try:
            # 2. 안전한 Staging 추출 (각 파일 경로 assert_safe_destination_path 검증)
            with zipfile.ZipFile(io.BytesIO(raw_package_bytes), "r") as zf:
                for member in zf.infolist():
                    dest_path = assert_safe_destination_path(
                        target_base_dir=os.path.join(staging_dir, "extracted"),
                        rel_path=member.filename
                    )

                    if member.is_dir() or member.filename.endswith("/"):
                        os.makedirs(dest_path, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        with zf.open(member) as s_file, open(dest_path, "wb") as d_file:
                            shutil.copyfileobj(s_file, d_file)

            extracted_root = os.path.join(staging_dir, "extracted")

            # 3. 현재 설치본 백업 및 트랜잭션 마커 생성 (BACKUP_READY)
            self.backup_current_installation(backup_ver_dir)
            logger.info("CURRENT_INSTALLATION_BACKED_UP to %s", backup_ver_dir)

            marker = txn_mgr.start_transaction(
                previous_version=current_ver,
                target_version=new_version,
                backup_dir=backup_ver_dir
            )
            txn_mgr._invoke_hook("KILL-02", marker)

            # 4. 서버 프로세스 종료 및 UPDATE_IN_PROGRESS 전이
            if not skip_process_control:
                self.stop_server()
                time.sleep(1)

            marker = txn_mgr.transition_to(marker, UpdatePhase.UPDATE_IN_PROGRESS)
            txn_mgr._invoke_hook("KILL-03", marker)

            # 5. 파일 교체 적용 (extracted_root -> target_dir)
            items = os.listdir(extracted_root)
            for idx, item in enumerate(items):
                s_item = os.path.join(extracted_root, item)
                d_item = os.path.join(self.target_dir, item)
                self._copy_with_retry(s_item, d_item)
                if idx == 0:
                    txn_mgr._invoke_hook("KILL-04", {"item": item, "index": idx})

            logger.info("FILES_SWAPPED_SUCCESSFULLY")
            marker = txn_mgr.transition_to(marker, UpdatePhase.SWAP_COMPLETE)
            txn_mgr._invoke_hook("KILL-05", marker)

            # 6. 재기동 및 헬스체크
            txn_mgr._invoke_hook("KILL-06", marker)
            if not skip_process_control:
                self.restart_server()
                if not self.health_check(timeout_sec=12):
                    raise HealthCheckTimeoutError("Server failed health check after update")

            marker = txn_mgr.transition_to(marker, UpdatePhase.HEALTHCHECK_OK)
            txn_mgr._invoke_hook("KILL-07", marker)

            # 7. 트랜잭션 커밋 및 마커 정리
            txn_mgr._invoke_hook("KILL-08", marker)
            marker = txn_mgr.transition_to(marker, UpdatePhase.COMMITTED)
            txn_mgr._invoke_hook("KILL-09", marker)
            txn_mgr.cleanup_marker(marker)

            logger.info("INSTALL_SUCCESS target_version=%s", new_version)
            return True

        except Exception as e:
            logger.critical("INSTALL_FAILED error=%s -> starting rollback", str(e))
            if os.path.exists(backup_ver_dir):
                self.rollback(backup_ver_dir, skip_process_control=skip_process_control)
                if marker:
                    txn_mgr.cleanup_marker(marker)
            raise InstallerError(f"Installation failed: {e}") from e

        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
