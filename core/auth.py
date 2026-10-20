"""
core/auth.py
웹 대시보드 마스터 비밀번호 인증 및 세션 관리 모듈
- PBKDF2-HMAC-SHA256 (100,000 iterations) 기반 암호화
- 64바이트 URL-Safe 세션 토큰 발행 및 만료 관리
- 로컬 루프백(127.0.0.1) 바이패스 옵션 지원
"""

import os
import json
import time
import secrets
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict

AUTH_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "auth_config.json"
)

# 인메모리 세션 저장소 (token -> expiry_datetime)
_sessions: Dict[str, datetime] = {}
_lock = threading.Lock()

# IP 기반 로그인 브루트포스 방지 상태
# client_ip -> {"failures": int, "locked_until": float, "last_attempt": float}
_login_rate_limits: Dict[str, dict] = {}
MAX_LOGIN_FAILURES = 5
LOCKOUT_DURATION_SEC = 300  # 5분


def _get_auth_config_path() -> str:
    os.makedirs(os.path.dirname(AUTH_CONFIG_FILE), exist_ok=True)
    return AUTH_CONFIG_FILE


def _load_auth_config() -> dict:
    path = _get_auth_config_path()
    if not os.path.exists(path):
        return {
            "configured": False,
            "corrupted": False,
            "password_hash": "",
            "salt": "",
            "allow_localhost_bypass": True,
            "session_ttl_hours": 24,
            "created_at": None,
            "updated_at": None
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("auth_config root must be a dict")
            cfg["corrupted"] = False
            return cfg
    except Exception as e:
        import logging
        logging.getLogger("BackupSystem").critical(
            f"[SECURITY FAIL-CLOSED] auth_config.json 손상 감지: {e}. 모든 변경/삭제/실행 API를 차단합니다."
        )
        return {
            "configured": True,  # Fail-Closed: 미설정으로 다운그레이드하지 않음
            "corrupted": True,
            "password_hash": "",
            "salt": "",
            "allow_localhost_bypass": False,
            "session_ttl_hours": 0,
            "created_at": None,
            "updated_at": None
        }


def _save_auth_config(config: dict) -> None:
    path = _get_auth_config_path()
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    # NTFS 원자적 교체
    os.replace(temp_path, path)


def _hash_password(password: str, salt_hex: str) -> str:
    salt_bytes = bytes.fromhex(salt_hex)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt_bytes,
        100000
    )
    return key.hex()


def is_auth_corrupted() -> bool:
    """인증 설정 파일이 손상되었는지 확인"""
    with _lock:
        config = _load_auth_config()
        return bool(config.get("corrupted", False))


def is_auth_configured() -> bool:
    """마스터 비밀번호가 설정되어 있는지 확인 (Fail-Closed: 손상된 경우도 True로 취급하여 무인증 통과 차단)"""
    with _lock:
        config = _load_auth_config()
        if config.get("corrupted"):
            return True
        return bool(config.get("configured") and config.get("password_hash"))


def get_auth_status(client_ip: Optional[str] = None) -> dict:
    """인증 상태 요약 정보 반환"""
    with _lock:
        config = _load_auth_config()
        corrupted = bool(config.get("corrupted", False))
        configured = bool(config.get("configured") and config.get("password_hash"))
        allow_bypass = config.get("allow_localhost_bypass", True) and not corrupted
        is_local = client_ip in ("127.0.0.1", "::1", "localhost", "testclient") if client_ip else False
        return {
            "configured": configured,
            "corrupted": corrupted,
            "allow_localhost_bypass": allow_bypass,
            "is_localhost": is_local,
            "bypassed": is_local and allow_bypass
        }


def check_login_rate_limit(client_ip: str) -> tuple[bool, int]:
    """
    해당 IP가 로그인 실패 락아웃 상태인지 확인.
    반환: (is_locked: bool, remaining_seconds: int)
    """
    with _lock:
        now = time.time()
        record = _login_rate_limits.get(client_ip)
        if not record:
            return False, 0
        locked_until = record.get("locked_until", 0.0)
        if now < locked_until:
            return True, max(1, int(locked_until - now))
        elif locked_until > 0.0:
            _login_rate_limits.pop(client_ip, None)
            return False, 0
        return False, 0


def record_login_failure(client_ip: str) -> tuple[int, bool]:
    """
    로그인 실패 기록.
    반환: (current_failures: int, is_locked: bool)
    """
    with _lock:
        now = time.time()
        record = _login_rate_limits.get(client_ip, {"failures": 0, "locked_until": 0.0, "last_attempt": now})
        record["failures"] += 1
        record["last_attempt"] = now
        is_locked = False
        if record["failures"] >= MAX_LOGIN_FAILURES:
            record["locked_until"] = now + LOCKOUT_DURATION_SEC
            is_locked = True
        _login_rate_limits[client_ip] = record
        return record["failures"], is_locked


def record_login_success(client_ip: str) -> None:
    """로그인 성공 시 실패 기록 초기화"""
    with _lock:
        _login_rate_limits.pop(client_ip, None)


def reset_login_rate_limits_for_test() -> None:
    """테스트용 레이트 리밋 상태 초기화"""
    with _lock:
        _login_rate_limits.clear()


def setup_master_password(password: str, allow_localhost_bypass: bool = True) -> bool:
    """최초 마스터 비밀번호 설정 (최소 8자리 이상)"""
    if not password or len(password) < 8:
        raise ValueError("비밀번호는 최소 8자리 이상이어야 합니다.")
    
    with _lock:
        config = _load_auth_config()
        if config.get("configured") and config.get("password_hash") and not config.get("corrupted"):
            raise ValueError("이미 마스터 비밀번호가 설정되어 있습니다. 암호 변경을 사용하세요.")
        
        salt = secrets.token_hex(16)
        p_hash = _hash_password(password, salt)
        now_str = datetime.now().isoformat()
        
        config.update({
            "configured": True,
            "corrupted": False,
            "password_hash": p_hash,
            "salt": salt,
            "allow_localhost_bypass": allow_localhost_bypass,
            "session_ttl_hours": config.get("session_ttl_hours", 24) or 24,
            "created_at": now_str,
            "updated_at": now_str
        })
        _save_auth_config(config)
        return True


def verify_master_password(password: str) -> bool:
    """마스터 비밀번호 일치 여부 검증"""
    if not password:
        return False
    with _lock:
        config = _load_auth_config()
        if not config.get("configured") or not config.get("password_hash") or config.get("corrupted"):
            return False
        salt = config.get("salt", "")
        expected_hash = config.get("password_hash", "")
        test_hash = _hash_password(password, salt)
        return secrets.compare_digest(test_hash, expected_hash)


def change_master_password(old_password: str, new_password: str) -> bool:
    """마스터 비밀번호 변경 (새 비밀번호 최소 8자리 이상)"""
    if not new_password or len(new_password) < 8:
        raise ValueError("새 비밀번호는 최소 8자리 이상이어야 합니다.")
    
    with _lock:
        config = _load_auth_config()
        if not config.get("configured") or not config.get("password_hash"):
            raise ValueError("마스터 비밀번호가 아직 설정되지 않았습니다.")
        
        salt = config.get("salt", "")
        expected_hash = config.get("password_hash", "")
        test_hash = _hash_password(old_password, salt)
        if not secrets.compare_digest(test_hash, expected_hash):
            raise ValueError("현재 비밀번호가 일치하지 않습니다.")
        
        new_salt = secrets.token_hex(16)
        new_p_hash = _hash_password(new_password, new_salt)
        now_str = datetime.now().isoformat()
        
        config.update({
            "password_hash": new_p_hash,
            "salt": new_salt,
            "updated_at": now_str
        })
        _save_auth_config(config)
        # 비밀번호 변경 시 기존 모든 활성 세션 만료
        _sessions.clear()
        return True


def set_localhost_bypass(enabled: bool) -> None:
    """로컬 루프백 접속 시 인증 자동 우회 여부 설정"""
    with _lock:
        config = _load_auth_config()
        config["allow_localhost_bypass"] = enabled
        _save_auth_config(config)


def create_session() -> str:
    """유효한 세션 토큰을 생성하고 저장"""
    with _lock:
        _cleanup_expired_sessions()
        token = secrets.token_urlsafe(32)
        config = _load_auth_config()
        ttl_hours = config.get("session_ttl_hours", 24)
        _sessions[token] = datetime.now() + timedelta(hours=ttl_hours)
        return token


def validate_session(token: Optional[str]) -> bool:
    """세션 토큰의 유효성을 검사"""
    if not token:
        return False
    with _lock:
        _cleanup_expired_sessions()
        expiry = _sessions.get(token)
        if not expiry:
            return False
        if datetime.now() > expiry:
            _sessions.pop(token, None)
            return False
        return True


def revoke_session(token: Optional[str]) -> None:
    """세션 토큰 폐기(로그아웃)"""
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def _cleanup_expired_sessions() -> None:
    """만료된 세션 정리 (내부용, _lock 보유 상태에서 호출)"""
    now = datetime.now()
    expired = [t for t, exp in _sessions.items() if now > exp]
    for t in expired:
        _sessions.pop(t, None)
