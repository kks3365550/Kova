# -*- coding: utf-8 -*-
"""
core/updater_v2/keyring.py: 신뢰 사슬 및 Ed25519 공개키 링 관리 모듈 (Trust Anchor & Key Rotation)

설계 원칙:
1. 'key_id'는 단순 색인일 뿐, 신뢰의 근거가 아닙니다.
2. 서명 검증은 반드시 해당 key_id에 연결된 실제 Ed25519 공개키와 상태(ACTIVE),
   유효기간(valid_from <= now <= valid_until)을 검증하여 수행합니다.
3. 키 로테이션: 기존 신뢰 키(Old Key)가 서명한 KeyRotationRecord를 통해서만 신규 키로 전환됩니다.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, Union
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey
    )
    from cryptography.hazmat.primitives.serialization import (
        load_pem_public_key,
        load_pem_private_key
    )
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    Ed25519PrivateKey = None
    Ed25519PublicKey = None
    load_pem_public_key = None
    load_pem_private_key = None
    InvalidSignature = Exception

from .jcs import canonicalize


class KeyStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUSPENDED = "suspended"


class KeyRingError(Exception):
    """키링 처리 관련 기본 예외"""
    pass


class KeyNotFoundError(KeyRingError):
    """지정된 key_id를 신뢰 저장소에서 찾을 수 없음"""
    pass


class KeyStatusError(KeyRingError):
    """키 상태가 ACTIVE가 아니거나 유효기간 만료"""
    pass


class SignatureVerificationError(KeyRingError):
    """디지털 서명 검증 실패"""
    pass


@dataclass(frozen=True)
class TrustedKey:
    """단일 Ed25519 신뢰 키 항목"""
    key_id: str
    public_key_bytes: bytes  # 32바이트 Raw 또는 PEM 바이트
    status: KeyStatus = KeyStatus.ACTIVE
    valid_from: Optional[str] = None  # ISO 8601 UTC
    valid_until: Optional[str] = None  # ISO 8601 UTC

    def is_valid_at(self, dt: Optional[datetime] = None) -> bool:
        if self.status != KeyStatus.ACTIVE:
            return False
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if self.valid_from:
            try:
                v_from = datetime.fromisoformat(self.valid_from)
                if v_from.tzinfo is None:
                    v_from = v_from.replace(tzinfo=timezone.utc)
                if dt < v_from:
                    return False
            except Exception:
                return False

        if self.valid_until:
            try:
                v_until = datetime.fromisoformat(self.valid_until)
                if v_until.tzinfo is None:
                    v_until = v_until.replace(tzinfo=timezone.utc)
                if dt > v_until:
                    return False
            except Exception:
                return False

        return True


@dataclass(frozen=True)
class KeyRotationRecord:
    """기존 신뢰 키가 새 키의 위임을 보증하는 로테이션 레코드"""
    old_key_id: str
    new_key_id: str
    new_public_key_hex: str  # 32바이트 Ed25519 공개키 hex
    valid_from: Optional[str]
    valid_until: Optional[str]
    rotation_signature_hex: str  # old_key로 서명된 레코드

    def to_canonical_dict(self) -> dict:
        return {
            "from_key_id": self.old_key_id,
            "to_key_id": self.new_key_id,
            "public_key": self.new_public_key_hex.lower(),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until
        }


class KeyRing:
    """Ed25519 공개키 저장소 및 서명 검증 엔진"""

    def __init__(self):
        self._keys: Dict[str, TrustedKey] = {}

    def add_trusted_key(self, key: TrustedKey) -> None:
        self._keys[key.key_id] = key

    def get_key(self, key_id: str) -> TrustedKey:
        if key_id not in self._keys:
            raise KeyNotFoundError(f"Key ID '{key_id}' not found in trusted keyring")
        return self._keys[key_id]

    def verify(
        self,
        key_id: str,
        message: bytes,
        signature: Union[bytes, str],
        now: Optional[datetime] = None
    ) -> bool:
        """
        1. key_id로 신뢰 키 검색
        2. 상태 및 유효기간 검증
        3. Ed25519 서명 검증
        """
        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography package is required for Ed25519 verification")

        key = self.get_key(key_id)

        if not key.is_valid_at(now):
            raise KeyStatusError(
                f"TrustedKey '{key_id}' is not currently active or has expired (status={key.status})"
            )

        if isinstance(signature, str):
            try:
                sig_bytes = bytes.fromhex(signature.strip())
            except ValueError:
                raise SignatureVerificationError("Signature string is not valid hex")
        else:
            sig_bytes = signature

        if len(sig_bytes) != 64:
            raise SignatureVerificationError(f"Ed25519 signature must be 64 bytes (128 hex), got {len(sig_bytes)}")

        # 공개키 로드 (32바이트 Raw 또는 PEM)
        try:
            if key.public_key_bytes.startswith(b"-----BEGIN"):
                pub_obj = load_pem_public_key(key.public_key_bytes)
            else:
                pub_obj = Ed25519PublicKey.from_public_bytes(key.public_key_bytes)
        except Exception as e:
            raise KeyRingError(f"Failed to load public key for '{key_id}': {e}")

        try:
            pub_obj.verify(sig_bytes, message)
            return True
        except InvalidSignature:
            raise SignatureVerificationError(f"Digital signature invalid for key_id '{key_id}'")

    def apply_rotation(self, record: KeyRotationRecord, now: Optional[datetime] = None) -> None:
        """
        키 로테이션 레코드를 검증하고, 정상 서명된 경우 신규 키를 KeyRing에 등록합니다.
        """
        # 1. 이전 키가 신뢰 저장소에 존재하며 유효한지 확인
        old_key = self.get_key(record.old_key_id)
        if not old_key.is_valid_at(now):
            raise KeyStatusError(f"Cannot rotate key: old_key '{record.old_key_id}' is not active")

        # 2. 로테이션 레코드 서명 검증 (old_key로 새 키의 정보를 서명했는지 확인)
        canonical_payload = canonicalize(record.to_canonical_dict())
        self.verify(record.old_key_id, canonical_payload, record.rotation_signature_hex, now=now)

        # 3. 신규 키 등록
        try:
            new_pub_raw = bytes.fromhex(record.new_public_key_hex.strip())
        except ValueError:
            raise KeyRingError(f"Invalid hex for new_public_key: {record.new_public_key_hex}")

        new_trusted_key = TrustedKey(
            key_id=record.new_key_id,
            public_key_bytes=new_pub_raw,
            status=KeyStatus.ACTIVE,
            valid_from=record.valid_from,
            valid_until=record.valid_until
        )
        self.add_trusted_key(new_trusted_key)
