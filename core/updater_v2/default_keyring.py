# -*- coding: utf-8 -*-
"""
core/updater_v2/default_keyring.py: 신뢰 앵커(Trust Anchor) 및 공개키 로더 (Phase 5-B Hardened)

안전 수칙:
1. 절대 경로 강제: 실행 위치(CWD)에 영향받지 않도록 BASE_DIR 절대 경로 기반 로드.
2. Fail-Closed 강제: 공개키 파일 누락, 권한 오류, 손상된 포맷일 경우 조용히 넘기지 않고 즉각 TrustAnchorError 발생.
3. Root Key Fingerprint 2중 앵커링:
   - 로컬 파일이 무단 변조되거나 공격자의 공개키로 교체되었을 때 시스템 장악을 방지하기 위해,
     배포 시 고정된 Root Public Key SHA-256 Digest를 대조 검증.
"""

import os
import hashlib
import logging
from typing import Optional

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .keyring import KeyRing, TrustedKey, KeyStatus

logger = logging.getLogger(__name__)

DEFAULT_KEY_ID = "release_2026_v2"

# kks3365550 공식 배포 Root Key 지문 (SHA-256 of keys/release_ed25519_v2.pub)
EXPECTED_ROOT_KEY_SHA256 = "b621a2b26a166b9f6b9f09f2764339eda7d07f1b44fc6a0492291dfb9b6256eb"


class TrustAnchorError(Exception):
    """신뢰 앵커 초기화 또는 무결성 검증 실패 (Fail-Closed)"""
    pass


def load_default_keyring(
    base_dir: str,
    expected_fingerprint: Optional[str] = EXPECTED_ROOT_KEY_SHA256,
    enforce_fingerprint: bool = True
) -> KeyRing:
    """
    base_dir/keys/release_ed25519_v2.pub 로부터 Root Key를 로드하여 KeyRing을 구성합니다.

    보안 보장:
    - 파일 부재 시: TrustAnchorError 발생 (Fail-Closed)
    - 파일 손상/비공개키 포맷 시: TrustAnchorError 발생 (Fail-Closed)
    - 지문 불일치(변조/바꿔치기) 시: TrustAnchorError 발생 (Fail-Closed)
    """
    abs_base = os.path.abspath(base_dir)
    pub_path = os.path.abspath(os.path.join(abs_base, "keys", "release_ed25519_v2.pub"))

    # 1. 파일 존재성 검증 (CWD 무관 절대 경로)
    if not os.path.exists(pub_path):
        raise TrustAnchorError(
            f"FAIL-CLOSED: Crucial trust anchor missing. Public key not found at '{pub_path}'"
        )

    # 2. 파일 읽기
    try:
        with open(pub_path, "rb") as f:
            pub_bytes = f.read()
    except Exception as e:
        raise TrustAnchorError(f"FAIL-CLOSED: Unable to read trust anchor at '{pub_path}': {e}") from e

    if not pub_bytes.strip():
        raise TrustAnchorError(f"FAIL-CLOSED: Trust anchor at '{pub_path}' is empty")

    # 3. Root Key 지문 2중 검증 (공개키 교체/위조 방어)
    if enforce_fingerprint and expected_fingerprint:
        actual_sha256 = hashlib.sha256(pub_bytes).hexdigest().lower()
        if actual_sha256 != expected_fingerprint.lower():
            raise TrustAnchorError(
                f"FAIL-CLOSED: Trust anchor fingerprint mismatch!\n"
                f"Expected: {expected_fingerprint}\n"
                f"Actual:   {actual_sha256}"
            )

    # 4. 공개키 포맷 및 Ed25519 유효성 사전 검증
    try:
        pub_obj = load_pem_public_key(pub_bytes)
        if not isinstance(pub_obj, Ed25519PublicKey):
            raise TrustAnchorError(f"FAIL-CLOSED: Key is not an Ed25519 public key (got {type(pub_obj)})")
    except Exception as e:
        raise TrustAnchorError(f"FAIL-CLOSED: Malformed or unparseable public key: {e}") from e

    # 5. KeyRing 구성
    keyring = KeyRing()
    trusted_key = TrustedKey(
        key_id=DEFAULT_KEY_ID,
        public_key_bytes=pub_bytes,
        status=KeyStatus.ACTIVE,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2035-12-31T23:59:59Z"
    )
    keyring.add_trusted_key(trusted_key)
    logger.info("Trust anchor successfully loaded and verified (key_id=%s, fingerprint=%s)",
                DEFAULT_KEY_ID, expected_fingerprint[:16] if expected_fingerprint else "unverified")

    return keyring
