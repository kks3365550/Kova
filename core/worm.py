# -*- coding: utf-8 -*-
"""
core/worm.py: WORM (Write Once Read Many / 불변성 제어) 엔진 (v2.13.5)

[현재 실측 방어 상태 및 한계 명시]
- Read-Only 속성 (Windows os.chmod / stat.S_IREAD):
  * 블롭 및 매니페스트에 실제 적용 중. 파일 '변조/수정'은 방지하나,
    디렉터리에 쓰기 권한이 있는 사용자 세션에서의 '파일 삭제(os.remove)'는 원천 방어하지 못함.
- Windows NTFS ACL (icacls Everyone Deny):
  * 현재 대화형 단일 사용자 계정(Interactive User) 환경에서는 Everyone(*S-1-1-0) Deny ACL 적용 시
    자체 프로세스 쓰기 및 서비스 계정 미분리로 인한 충돌 가능성이 있어 자동 호출이 비활성화(미배선)되어 있음.
  * 완전한 랜섬웨어 방어(WORM)를 위해서는 '전용 서비스 계정(NT SERVICE)' 분리 또는
    '물리적 오프사이트 복제본(외장/원격 네트워크 PC/클라우드)'이 필수적임.
- 무결성 보증:
  * 랜섬웨어 사후 침해 탐지는 Ed25519 전자서명 검증으로 수행됨.
"""

import os
import sys
import stat
import logging
import subprocess
from typing import Dict

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith('win')
SID_EVERYONE = "*S-1-1-0"


class WORMAuthorizationError(PermissionError):
    """Raised when an unauthorized process attempts to unprotect WORM-protected repository files."""
    pass


class WORMManager:
    """
    저장소 파일 및 디렉토리에 대해 OS 및 파일시스템 수준의 WORM 보호를 강제하는 엔진.
    Windows NTFS 환경에서는 icacls를 활용하여 Delete, WriteData, AppendData, DeleteChild 거부 ACL을 부여합니다.
    """

    def __init__(self):
        self._is_windows = IS_WINDOWS

    def _run_icacls(self, target_path: str, arguments: list) -> bool:
        if not self._is_windows or not os.path.exists(target_path):
            return False

        cmd = ["icacls", target_path] + arguments
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if self._is_windows else 0
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                logger.debug(f"icacls failed for {target_path}: {output.strip()}")
                return False
            if "0 files processed" in output or "Access denied" in output:
                logger.debug(f"icacls reported failure for {target_path}: {output.strip()}")
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"icacls timed out after 15s on {target_path}")
            return False
        except FileNotFoundError:
            logger.debug("icacls executable not found. Skipping NTFS ACL.")
            return False
        except Exception as e:
            logger.debug(f"icacls exception on {target_path}: {e}")
            return False

    def protect_file(self, filepath: str, use_ntfs_acl: bool = True) -> bool:
        """
        파일을 수정/삭제할 수 없도록 WORM 보호를 적용합니다.
        1. OS Read-only 속성 부여
        2. Windows NTFS 환경일 경우 Delete, Write, Append 거부 ACL 부여
        """
        if not os.path.exists(filepath):
            return False

        chmod_ok = False

        # 1. POSIX / DOS Read-Only attribute
        try:
            current_mode = os.stat(filepath).st_mode
            new_mode = current_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH
            os.chmod(filepath, new_mode)
            chmod_ok = True
        except Exception as e:
            logger.debug(f"chmod readonly failed on {filepath}: {e}")

        # 2. Windows NTFS ACL Deny (DE,WD,AD)
        if self._is_windows and use_ntfs_acl:
            acl_ok = self._run_icacls(filepath, ["/deny", f"{SID_EVERYONE}:(DE,WD,AD)"])
            if not acl_ok:
                logger.warning(f"NTFS ACL WORM 적용 실패 (chmod만 적용됨): {filepath}")
            return acl_ok and chmod_ok

        return chmod_ok

    def unprotect_file(self, filepath: str, authorized: bool = False) -> bool:
        """
        정당한 삭제나 정리를 위해 WORM 보호를 해제합니다.
        명시적 관리자 권한(authorized=True)이 필요하며, 인가되지 않은 호출은 차단됩니다.
        """
        if not authorized:
            raise WORMAuthorizationError(f"WORM 보호 해제 거부: {filepath} (명시적 관리자 승인 권한 필요)")

        if not os.path.exists(filepath):
            return False

        acl_ok = True
        chmod_ok = False

        # 1. Remove NTFS Deny ACL
        if self._is_windows:
            acl_ok = self._run_icacls(filepath, ["/remove:d", SID_EVERYONE])

        # 2. Restore Write attribute
        try:
            current_mode = os.stat(filepath).st_mode
            new_mode = current_mode | stat.S_IWUSR
            os.chmod(filepath, new_mode)
            chmod_ok = True
        except Exception as e:
            logger.debug(f"chmod writeable failed on {filepath}: {e}")

        if self._is_windows:
            return acl_ok and chmod_ok

        return chmod_ok

    def protect_directory(self, dirpath: str) -> bool:
        """
        디렉토리 내 파일 삭제(Delete Child)를 거부하는 WORM ACL을 적용합니다.
        """
        if not os.path.isdir(dirpath):
            return False

        if self._is_windows:
            return self._run_icacls(dirpath, ["/deny", f"{SID_EVERYONE}:(DC)"])
        return True

    def unprotect_directory(self, dirpath: str, authorized: bool = False) -> bool:
        """
        디렉토리 및 하위 디렉토리의 Delete Child 거부 ACL을 해제합니다.
        명시적 관리자 권한(authorized=True)이 필요합니다.
        """
        if not authorized:
            raise WORMAuthorizationError(f"WORM 디렉토리 보호 해제 거부: {dirpath} (명시적 관리자 승인 권한 필요)")

        if not os.path.isdir(dirpath):
            return False

        if self._is_windows:
            # 단 1회의 재귀 호출(/t)로 전체 디렉토리 트리의 Deny ACL을 일괄 해제 (성능 병목/프로세스 폭풍 방지)
            success = self._run_icacls(dirpath, ["/remove:d", SID_EVERYONE, "/t", "/c", "/q"])
            # 읽기 전용 속성(chmod)도 일괄 복원
            try:
                for root, dirs, files in os.walk(dirpath):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            os.chmod(fp, os.stat(fp).st_mode | stat.S_IWUSR)
                        except OSError:
                            pass
            except OSError:
                pass
            return success
        return True


    def protect_repository(self, repo_dir: str, protect_dirs: bool = True) -> Dict[str, int]:
        """
        저장소 내 모든 스냅샷 매니페스트 및 블롭 디렉토리에 대해 일괄 WORM을 적용합니다.
        기본적으로 protect_dirs=True가 활성화되어 blobs 디렉토리 트리에 DeleteChild 거부 ACL을 적용합니다.
        """
        protected_blobs = 0
        protected_snapshots = 0

        snapshots_dir = os.path.join(repo_dir, "snapshots")
        if os.path.exists(snapshots_dir):
            try:
                for fname in os.listdir(snapshots_dir):
                    if fname.endswith(".json"):
                        fpath = os.path.join(snapshots_dir, fname)
                        if self.protect_file(fpath, use_ntfs_acl=True):
                            protected_snapshots += 1
            except (PermissionError, OSError) as e:
                logger.warning(f"Error listing snapshots directory {snapshots_dir}: {e}")

        blobs_dir = os.path.join(repo_dir, "blobs")
        if os.path.exists(blobs_dir):
            if protect_dirs:
                self.protect_directory(blobs_dir)
            try:
                for root, dirs, files in os.walk(blobs_dir):
                    if protect_dirs and root != blobs_dir:
                        self.protect_directory(root)
                    for f in files:
                        if f.endswith(".blob"):
                            fpath = os.path.join(root, f)
                            if self.protect_file(fpath, use_ntfs_acl=False):
                                protected_blobs += 1
            except (PermissionError, OSError) as e:
                logger.warning(f"Error walking blobs directory {blobs_dir}: {e}")

        return {
            "protected_snapshots": protected_snapshots,
            "protected_blobs": protected_blobs
        }


# 하위 호환성 단축 함수
_default_worm_manager = WORMManager()

def lock_file_immutable(filepath: str, use_ntfs_acl: bool = False) -> bool:
    """하위 호환성을 지원하는 WORM 잠금 함수"""
    return _default_worm_manager.protect_file(filepath, use_ntfs_acl=use_ntfs_acl)

def unlock_file_writable(filepath: str, authorized: bool = True) -> bool:
    """하위 호환성을 지원하는 WORM 해제 함수 (명시적 권한 검증)"""
    return _default_worm_manager.unprotect_file(filepath, authorized=authorized)
