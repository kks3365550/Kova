# -*- coding: utf-8 -*-
"""
core/vss_manager.py: Windows Volume Shadow Copy (VSS) 스냅샷 관리자
- Windows 환경에서 볼륨 섀도 복사본(Shadow Copy) 생성 및 라이프사이클 관리
- 실행 중인 파일(Outlook PST, SQLite, 잠긴 오피스 파일 등)의 안전한 Crash-Consistent 백업 지원
- 컨텍스트 매니저 기반 자동 리소스 해제(Cleanup)로 섀도 복사본 누수 원천 차단
- strict 모드 (기본값): VSS 실패 시 Silent Fallback을 원천 차단하고 VSSRequiredError 발생 (Fail-Closed)
- optional 모드: 관리자가 명시적으로 허용한 경우에만 일반 직접 읽기로 fallback
"""

import os
import re
import sys
import ctypes
import logging
import subprocess
import threading
import sqlite3
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("BackupSystem.VSS")

# Windows VSS 서비스 충돌 방지를 위한 모듈 레벨 재진입 가능 락 (데드락 방지)
_vss_lock = threading.RLock()


def flush_sqlite_databases(paths: List[str]):
    """
    VSS 스냅샷 직전, 소스 경로 내의 SQLite 데이터베이스(-wal 파일 보유 등)에
    PRAGMA wal_checkpoint(TRUNCATE)를 실행하여 트랜잭션 정합성(Application-Consistent) 확보.
    """
    sqlite_exts = {".db", ".sqlite", ".sqlite3"}
    for p in paths:
        try:
            if not os.path.exists(p):
                continue
            # 루트 드라이브 경로(예: 'C:\', 'C:', '/')인 경우 전체 드라이브 os.walk 순회 시
            # 수십만 개 폴더 탐색으로 1~2분 정체되므로 루트는 전체 순회를 생략하고 통과
            norm_p = os.path.abspath(p).rstrip("\\/")
            if len(norm_p) <= 2 or norm_p.endswith(":"):
                logger.info(f"[VSS Pre-Hook] 루트 볼륨({p}) 무차별 전체 디렉터리 순회를 건너뜁니다.")
                continue
            if os.path.isfile(p):
                _, ext = os.path.splitext(p)
                if ext.lower() in sqlite_exts or p.lower().endswith("-wal"):
                    db_target = p[:-4] if p.lower().endswith("-wal") else p
                    _do_checkpoint(db_target)
            elif os.path.isdir(p):
                # 디렉토리 1~2 레벨 내의 SQLite DB 탐색
                for root, dirs, files in os.walk(p):
                    # 너무 깊은 디렉토리 탐색 방지 (성능 보호)
                    depth = root[len(p):].count(os.sep)
                    if depth > 3:
                        continue
                    for f in files:
                        _, ext = os.path.splitext(f)
                        if ext.lower() in sqlite_exts or f.lower().endswith("-wal"):
                            db_target = os.path.join(root, f[:-4] if f.lower().endswith("-wal") else f)
                            _do_checkpoint(db_target)
        except Exception as e:
            logger.debug(f"[VSS Pre-Hook] SQLite flush error on {p}: {e}")

def _do_checkpoint(db_path: str):
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
        logger.info(f"[VSS Pre-Hook] SQLite wal_checkpoint(TRUNCATE) executed on {db_path}")
    except Exception as e:
        logger.debug(f"[VSS Pre-Hook] wal_checkpoint skipped/failed on {db_path}: {e}")


class VSSRequiredError(Exception):
    """VSS strict mode에서 섀도 복사본 생성 실패 시 발생하는 예외 (무단 Silent Fallback 방지)."""
    pass


class VSSCreationError(Exception):
    """VSS 섀도 복사본 생성 실패 시 내부적으로 발생하는 예외."""
    pass


def is_admin() -> bool:
    """현재 프로세스가 Windows 관리자 권한으로 실행 중인지 확인."""
    try:
        if sys.platform == "win32":
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return False
    except Exception:
        return False


def extract_drive_letter(path: str) -> Optional[str]:
    """
    경로에서 드라이브 문자(예: 'C:')를 추출.
    UNC 경로, 상대 경로 또는 비-Windows 형식은 None 반환.
    """
    if not path or not isinstance(path, str):
        return None
    p = path.replace('/', '\\')
    m = re.match(r'^([A-Za-z]):', p)
    if m:
        return m.group(1).upper() + ':'
    return None


class ShadowCopyRecord:
    """단일 드라이브에 대한 VSS 섀도 복사본 정보 레코드"""
    __slots__ = ('drive', 'shadow_id', 'device_path')

    def __init__(self, drive: str, shadow_id: str, device_path: str):
        self.drive = drive
        self.shadow_id = shadow_id
        self.device_path = device_path

    def __repr__(self) -> str:
        return f"<ShadowCopy drive={self.drive} id={self.shadow_id} device={self.device_path}>"


class VSSContext:
    """
    백업 세션 동안 볼륨 섀도 복사본(VSS)을 생성하고 작업 완료/예외 시 안전하게 해제하는 컨텍스트 매니저.

    사용법:
        with VSSContext(source_paths, strict=True) as vss:
            if vss.vss_active:
                read_path = vss.get_shadow_path(original_file)
            ...
    """

    def __init__(self, source_paths: List[str], enabled: bool = True, strict: bool = True):
        self.source_paths = list(source_paths) if source_paths else []
        self.enabled = enabled
        self.strict = strict
        self.vss_active: bool = False
        self.shadows: Dict[str, ShadowCopyRecord] = {}  # 'C:' -> ShadowCopyRecord
        self.warnings: List[str] = []
        self._cleanup_done: bool = False

    def __enter__(self) -> 'VSSContext':
        if not self.enabled:
            msg = "VSS가 비활성화되었습니다."
            self.warnings.append(msg)
            return self

        # strict 모드에서 VSS 필수 조건 검사
        if self.strict:
            if sys.platform != "win32":
                msg = "VSS는 Windows 환경에서만 지원됩니다 (strict 모드: Silent Fallback 불가)."
                self.warnings.append(msg)
                raise VSSRequiredError(msg)

            if not is_admin():
                msg = "관리자 권한이 없어 VSS 볼륨 스냅샷을 생성할 수 없습니다 (strict 모드: 일관성 미보장 백업 거부)."
                self.warnings.append(msg)
                raise VSSRequiredError(msg)

        if sys.platform != "win32":
            self.warnings.append("VSS는 Windows 환경에서만 지원됩니다.")
            return self

        try:
            self._setup()
        except Exception:
            self.cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False  # 예외는 상위로 정상 전파

    def _setup(self):
        """백업 소스 경로들이 속한 모든 고유 볼륨에 대해 VSS 섀도 복사본 생성."""
        # 0. 사전 훅: 소스 경로 내 열려있는 SQLite DB들 WAL 체크포인트 수행
        flush_sqlite_databases(self.source_paths)

        unique_drives = set()
        for p in self.source_paths:
            drv = extract_drive_letter(p)
            if drv:
                unique_drives.add(drv)

        if not unique_drives:
            msg = "유효한 Windows 드라이브 경로가 없어 VSS를 건너뜁니다."
            self.warnings.append(msg)
            if self.strict:
                raise VSSRequiredError(msg)
            return

        if not is_admin():
            msg = "관리자 권한이 없어 VSS 볼륨 스냅샷을 생성할 수 없습니다. 일반 파일 직접 읽기 모드로 백업을 진행합니다."
            self.warnings.append(msg)
            logger.info(msg)
            return

        with _vss_lock:
            creation_failures = []
            for drv in sorted(unique_drives):
                try:
                    sc = self._create_shadow(drv)
                    if sc:
                        self.shadows[drv] = sc
                        logger.info("VSS 섀도 복사본 생성 완료: %s -> %s (ID: %s)", drv, sc.device_path, sc.shadow_id)
                except Exception as e:
                    msg = f"{drv} 볼륨 VSS 스냅샷 생성 실패: {e}"
                    self.warnings.append(msg)
                    logger.warning(msg)
                    creation_failures.append(drv)

        # strict 모드 검사: 하나라도 실패하거나 섀도가 비어있으면 즉시 실패 처리
        if self.strict:
            if creation_failures or not self.shadows:
                failed_drives = ", ".join(creation_failures) if creation_failures else "모든 드라이브"
                msg = f"VSS strict 모드: 섀도 복사본 생성 실패 ({failed_drives}). Crash-Consistent 일관성 보장을 위해 백업을 거부합니다."
                self.warnings.append(msg)
                raise VSSRequiredError(msg)

        if self.shadows:
            self.vss_active = True
        else:
            self.warnings.append("모든 드라이브의 VSS 생성이 실패하여 일반 직접 읽기 모드로 동작합니다.")

    @staticmethod
    def _run_cmd(cmd: List[str], timeout: int = 90) -> Tuple[int, str, str]:
        """subprocess 안전 실행 및 윈도우 인코딩 다중 디코딩"""
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creationflags
        )
        def decode_bytes(b: bytes) -> str:
            for enc in ('cp949', 'utf-8', 'euc-kr', 'latin1'):
                try:
                    return b.decode(enc)
                except UnicodeDecodeError:
                    pass
            return b.decode('utf-8', errors='replace')

        stdout = decode_bytes(proc.stdout)
        stderr = decode_bytes(proc.stderr)
        return proc.returncode, stdout, stderr

    @classmethod
    def _create_shadow(cls, drive: str) -> ShadowCopyRecord:
        """
        볼륨 섀도 복사본 생성.
        1. Windows 10/11 Client: WMI Win32_ShadowCopy.Create (vssadmin create shadow가 없는 Windows Client OS 표준)
        2. Windows Server: vssadmin create shadow 폴백
        """
        vol_path = drive + '\\' if not drive.endswith('\\') else drive

        # 1차 시도: WMI Win32_ShadowCopy (Windows 10/11 Client + Server 공통 정석 API)
        try:
            ps_code = (
                f"$vol = '{vol_path}'; "
                "$class = [wmiclass]'Win32_ShadowCopy'; "
                "$result = $class.Create($vol, 'ClientAccessible'); "
                "if ($result.ReturnValue -eq 0 -and $result.ShadowID) { "
                "  $shadowId = $result.ShadowID; "
                "  Start-Sleep -Milliseconds 300; "
                "  $obj = Get-CimInstance Win32_ShadowCopy -Filter \"ID = '$shadowId'\" -ErrorAction SilentlyContinue; "
                "  if (-not $obj) { $obj = Get-CimInstance Win32_ShadowCopy | Where-Object { $_.ID -eq $shadowId } }; "
                "  $devPath = if ($obj) { $obj.DeviceObject } else { $null }; "
                "  Write-Output ($shadowId + '|' + $devPath); "
                "} else { "
                "  Write-Output ('ERR|' + $result.ReturnValue); "
                "}"
            )
            ps_cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_code]
            rc_wmi, stdout_wmi, stderr_wmi = cls._run_cmd(ps_cmd, timeout=90)
            out_str = stdout_wmi.strip()
            if rc_wmi == 0 and "|" in out_str and not out_str.startswith("ERR|"):
                parts = out_str.split("|", 1)
                shadow_id = parts[0].strip()
                device_path = parts[1].strip()
                if shadow_id and device_path:
                    logger.info("WMI Win32_ShadowCopy 생성 성공: %s (ID: %s, Device: %s)", drive, shadow_id, device_path)
                    return ShadowCopyRecord(drive=drive, shadow_id=shadow_id, device_path=device_path)
            else:
                logger.warning("WMI Win32_ShadowCopy 반환 이상 (rc=%s, out=%s, err=%s), vssadmin 폴백 시도", rc_wmi, out_str, stderr_wmi.strip())
        except Exception as e_wmi:
            logger.warning("WMI Win32_ShadowCopy 생성 예외 (%s), vssadmin 폴백 시도", e_wmi)

        # 2차 시도: vssadmin create shadow (Windows Server 전용 폴백)
        cmd = ['vssadmin', 'create', 'shadow', f'/for={vol_path}']

        rc, stdout, stderr = cls._run_cmd(cmd, timeout=120)
        combined_output = stdout + "\n" + stderr

        if rc != 0:
            raise VSSCreationError(f"VSS 스냅샷 생성 실패 (WMI 및 vssadmin 모두 실패, vssadmin 종료 코드 {rc}): {combined_output.strip()}")

        shadow_id = None
        id_match = re.search(
            r'\{([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}',
            combined_output
        )
        if id_match:
            shadow_id = '{' + id_match.group(1) + '}'

        device_path = None
        dev_match = re.search(
            r'(\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy\d+)',
            combined_output,
            re.IGNORECASE
        )
        if dev_match:
            device_path = dev_match.group(1)

        if not shadow_id or not device_path:
            raise VSSCreationError(f"vssadmin 출력에서 섀도 복사본 정보(ID 또는 디바이스 경로)를 파싱할 수 없습니다:\n{combined_output}")

        return ShadowCopyRecord(drive=drive, shadow_id=shadow_id, device_path=device_path)

    @classmethod
    def _delete_shadow(cls, shadow_id: str) -> bool:
        """vssadmin delete shadows /shadow={id} /quiet 호출로 섀도 복사본 삭제."""
        cmd = ['vssadmin', 'delete', 'shadows', f'/shadow={shadow_id}', '/quiet']
        try:
            rc, stdout, stderr = cls._run_cmd(cmd, timeout=60)
            return rc == 0
        except Exception as e:
            logger.error("VSS 섀도 복사본 %s 삭제 중 예외: %s", shadow_id, e)
            return False

    def cleanup(self):
        """생성된 모든 섀도 복사본을 100% 누수 없이 해제."""
        with _vss_lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True

            for drv, sc in list(self.shadows.items()):
                try:
                    success = self._delete_shadow(sc.shadow_id)
                    if success:
                        logger.info("VSS 섀도 복사본 해제 완료: %s (ID: %s)", drv, sc.shadow_id)
                    else:
                        logger.warning("VSS 섀도 복사본 해제 실패: %s (ID: %s)", drv, sc.shadow_id)
                except Exception as e:
                    logger.error("VSS 클린업 예외 (%s): %s", drv, e)

            self.shadows.clear()
            self.vss_active = False

    def get_shadow_path(self, original_path: str) -> str:
        """원본 파일 경로를 섀도 복사본 장치 경로로 매핑."""
        if not self.vss_active or not self.shadows:
            return original_path

        drv = extract_drive_letter(original_path)
        if not drv or drv not in self.shadows:
            return original_path

        sc = self.shadows[drv]
        norm = original_path.replace('/', '\\')
        rel = re.sub(r'^[A-Za-z]:\\?', '', norm).lstrip('\\')
        shadow_root = sc.device_path.rstrip('\\')

        if rel:
            return f"{shadow_root}\\{rel}"
        return shadow_root
