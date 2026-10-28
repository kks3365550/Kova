# -*- coding: utf-8 -*-
"""
core/lock.py: 백업 시스템 동시성 제어 및 파일 기반 상호 배제 락 (BackupLock)
- Windows 다중 프로세스 동시 백업 충돌 방지
- 비정상 종료(Crash)된 프로세스의 Stale Lock 자동 감지 및 회수
- 컨텍스트 매니저 인터페이스 지원
"""

import os
import time
import json
import socket
import datetime
from typing import Optional, Dict, Any

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

import threading

# 재진입(Re-entrancy) 깊이 카운터.
# CRITICAL: 반드시 (repo_dir, thread_ident)로 키를 잡아야 한다.
# repo_dir만 쓰면 동일 프로세스의 '다른 스레드'(예: FastAPI BackgroundTasks)가
# 이미 진행 중인 백업을 감지하고 무혈입으로 통과해 버려 상호배제가 무력화된다.
_active_locks: Dict[tuple, int] = {}
_reentrant_gate = threading.Lock()


def _lock_key(repo_dir: str) -> tuple:
    """저장소별 '현재 스레드의 락 깊이' 키를 생성한다."""
    return (os.path.abspath(repo_dir), threading.get_ident())


class BackupAlreadyRunningError(Exception):
    """이미 다른 백업 프로세스가 실행 중일 때 발생하는 예외"""
    def __init__(self, message: str, lock_info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.lock_info = lock_info or {}


class BackupLock:
    """
    저장소(repo_dir) 레벨의 상호 배제 백업 락.
    백업 시작 시 lock 파일을 획득하고, 완료/실패 시 안전하게 해제합니다.
    """

    LOCK_FILE_NAME = "backup.lock"
    _LOCK_OFFSET = 1 << 30

    def __init__(self, repo_dir: str, timeout_sec: float = 0.0, process_desc: str = "Backup Process"):
        self.repo_dir = os.path.abspath(repo_dir)
        self.lock_path = os.path.join(self.repo_dir, self.LOCK_FILE_NAME)
        self.timeout_sec = timeout_sec
        self.process_desc = process_desc
        self.fd = None
        self._is_locked = False

    def _is_pid_alive(self, pid: int) -> bool:
        """해당 PID를 가진 프로세스가 실제로 시스템에 살아있는지 검사"""
        if pid <= 0:
            return False
        if HAS_PSUTIL:
            try:
                p = psutil.Process(pid)
                return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False
        else:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                SYNCHRONIZE = 0x00100000
                process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if process != 0:
                    kernel32.CloseHandle(process)
                    return True
                return False
            except Exception:
                return True

    def _read_lock_info(self) -> Optional[Dict[str, Any]]:
        """기존 락 파일의 메타데이터 조회"""
        if not os.path.exists(self.lock_path):
            return None
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def acquire(self) -> bool:
        """
        락 획득 시도.
        동일 프로세스 내 재진입(Re-entrancy)을 지원하며,
        이미 실행 중인 다른 프로세스가 있으면 BackupAlreadyRunningError 발생.
        만약 이전 프로세스가 비정상 종료(죽은 PID)되었다면 Stale Lock을 정리하고 새로 획득.
        """
        self.repo_dir = os.path.abspath(self.repo_dir)
        with _reentrant_gate:
            depth = _active_locks.get(_lock_key(self.repo_dir), 0)
            if depth > 0:
                _active_locks[_lock_key(self.repo_dir)] = depth + 1
                self._is_locked = True
                self._is_reentrant = True
                return True

        os.makedirs(self.repo_dir, exist_ok=True)
        start_time = time.time()

        while True:
            # 1. 기존 락 파일 상태 점검
            if os.path.exists(self.lock_path):
                lock_info = self._read_lock_info()
                lock_pid = lock_info.get("pid", -1) if lock_info else -1
                lock_host = lock_info.get("hostname", "") if lock_info else ""
                my_host = socket.gethostname()

                # 동일 호스트에서 해당 PID가 이미 사망했거나 lock_info를 읽을 수 없는 경우
                if not lock_info or ((not lock_host or lock_host == my_host) and not self._is_pid_alive(lock_pid)):
                    try:
                        if os.path.exists(self.lock_path):
                            os.remove(self.lock_path)
                    except Exception:
                        pass
                else:
                    elapsed = time.time() - start_time
                    if elapsed >= self.timeout_sec:
                        start_str = lock_info.get("start_time", "알 수 없음")
                        desc = lock_info.get("desc", "백업 프로세스")
                        err_msg = (
                            f"이미 다른 백업 작업이 실행 중입니다.\n"
                            f"- 작업명: {desc} (PID: {lock_pid})\n"
                            f"- 시작시간: {start_str}\n"
                            f"- 저장소: {self.repo_dir}"
                        )
                        raise BackupAlreadyRunningError(err_msg, lock_info)
                    time.sleep(0.5)
                    continue

            # 2. 락 파일 생성 시도 (원자적 O_CREAT | O_EXCL)
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                self.fd = os.open(self.lock_path, flags)
                
                if HAS_MSVCRT:
                    # 0번 바이트를 잠그면 다른 프로세스가 lock JSON을 못 읽으므로 데이터 영역 밖에 잠근다
                    try:
                        os.lseek(self.fd, self._LOCK_OFFSET, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    except (OSError, IOError):
                        pass
                    finally:
                        os.lseek(self.fd, 0, os.SEEK_SET)

                info = {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "start_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "created_at": time.time(),
                    "desc": self.process_desc,
                    "repo_dir": self.repo_dir
                }
                info_bytes = json.dumps(info, indent=2, ensure_ascii=False).encode("utf-8")
                os.write(self.fd, info_bytes)
                self._is_locked = True
                self._is_reentrant = False
                with _reentrant_gate:
                    _active_locks[_lock_key(self.repo_dir)] = 1
                return True

            except (FileExistsError, PermissionError):
                elapsed = time.time() - start_time
                if elapsed >= self.timeout_sec:
                    existing_info = self._read_lock_info()
                    raise BackupAlreadyRunningError(
                        f"백업 저장소가 다른 프로세스에 의해 잠겨 있습니다: {self.repo_dir}",
                        existing_info
                    )
                time.sleep(0.5)

    def release(self):
        """락 해제 및 락 파일 정리"""
        if not self._is_locked and not os.path.exists(self.lock_path):
            return

        with _reentrant_gate:
            key = _lock_key(self.repo_dir)
            depth = _active_locks.get(key, 0)
            
            # 1. depth > 1: 재진입 중이므로 카운터만 감소하고 즉시 반환 (파일 락 유지)
            if depth > 1:
                _active_locks[key] = depth - 1
                self._is_locked = False
                self._is_reentrant = False
                return
            
            # 2. depth <= 1: 최종 해제. 키를 pop하고 실제 파일 락 해제 로직을 진행
            else:
                _active_locks.pop(key, None)
                self._is_locked = False
                self._is_reentrant = False

        # 실제 파일 락 해제 및 리소스 정리 (depth <= 1일 때만 실행됨)
        if self.fd is not None:
            try:
                if HAS_MSVCRT:
                    try:
                        os.lseek(self.fd, self._LOCK_OFFSET, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                    except (OSError, IOError):
                        pass
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

        if os.path.exists(self.lock_path):
            try:
                info = self._read_lock_info()
                if not info or info.get("pid") == os.getpid() or not self._is_pid_alive(info.get("pid", 0)):
                    os.remove(self.lock_path)
            except Exception:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
