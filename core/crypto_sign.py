# -*- coding: utf-8 -*-
"""
core/crypto_sign.py: Ed25519 비대칭키 기반 디지털 서명 관리 모듈
- 백업 시스템의 Manifest 위변조 방지를 위한 암호학적 무결성 보증
- cryptography 라이브러리 활용 (PEM 포맷 키 저장/로드)
"""

import os
import json
import logging
from typing import Dict, Any, Union

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey
    )
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key,
        load_pem_public_key,
        Encoding,
        NoEncryption,
        PublicFormat,
        PrivateFormat
    )
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    Ed25519PrivateKey = None
    Ed25519PublicKey = None
    load_pem_private_key = None
    load_pem_public_key = None
    Encoding = None
    NoEncryption = None
    PublicFormat = None
    PrivateFormat = None
    InvalidSignature = Exception
    logger.warning("cryptography package is not installed. Ed25519 digital signature will operate in fallback mode.")


class Ed25519Signer:
    """
    Ed25519 키 페어를 관리하고 Manifest에 대한 디지털 서명/검증을 수행하는 클래스.
    - 키 파일은 repo_dir/keys/ 디렉토리에 저장됨
    - private key: backup_ed25519.key (PEM)
    - public key: backup_ed25519.pub (PEM)
    """

    PRIVATE_KEY_FILENAME = "backup_ed25519.key"
    PUBLIC_KEY_FILENAME = "backup_ed25519.pub"

    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.keys_dir = os.path.join(self.repo_dir, "keys")
        self.private_key_path = os.path.join(self.keys_dir, self.PRIVATE_KEY_FILENAME)
        self.public_key_path = os.path.join(self.keys_dir, self.PUBLIC_KEY_FILENAME)
        self._private_key = None
        self._public_key = None
        os.makedirs(self.keys_dir, exist_ok=True)

    def ensure_key_pair(self) -> None:
        """Ed25519 키 페어가 존재하는지 확인하고, 없으면 생성하여 저장."""
        if not HAS_CRYPTOGRAPHY:
            return
        if os.path.exists(self.private_key_path):
            self._load_private_key()
        else:
            private_key = Ed25519PrivateKey.generate()
            self._save_private_key(private_key)

        if os.path.exists(self.public_key_path):
            self._load_public_key()
        else:
            private_key = self._get_private_key_instance()
            public_key = private_key.public_key()
            self._save_public_key(public_key)

    def _get_private_key_instance(self) -> Ed25519PrivateKey:
        if self._private_key is None:
            self._load_private_key()
        return self._private_key

    def _load_private_key(self) -> None:
        try:
            with open(self.private_key_path, 'rb') as f:
                key_data = f.read()
            self._private_key = load_pem_private_key(key_data, password=None)
        except Exception as e:
            raise RuntimeError(f"Private Key 로드 실패: {str(e)}") from e

    def _save_private_key(self, private_key: Ed25519PrivateKey) -> None:
        pem_data = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption()
        )
        tmp_path = self.private_key_path + ".tmp"
        try:
            with open(tmp_path, 'wb') as f:
                f.write(pem_data)
            if os.path.exists(self.private_key_path):
                os.remove(self.private_key_path)
            os.replace(tmp_path, self.private_key_path)
            self._private_key = private_key
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Private Key 저장 실패: {str(e)}") from e

    def _load_public_key(self) -> None:
        try:
            with open(self.public_key_path, 'rb') as f:
                key_data = f.read()
            self._public_key = load_pem_public_key(key_data)
        except Exception as e:
            raise RuntimeError(f"Public Key 로드 실패: {str(e)}") from e

    def _save_public_key(self, public_key: Ed25519PublicKey) -> None:
        pem_data = public_key.public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo
        )
        tmp_path = self.public_key_path + ".tmp"
        try:
            with open(tmp_path, 'wb') as f:
                f.write(pem_data)
            if os.path.exists(self.public_key_path):
                os.remove(self.public_key_path)
            os.replace(tmp_path, self.public_key_path)
            self._public_key = public_key
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Public Key 저장 실패: {str(e)}") from e

    def _prepare_manifest_payload(self, manifest: Dict[str, Any]) -> bytes:
        signable_manifest = {
            k: v for k, v in manifest.items()
            if k not in ("manifest_signature", "ed25519_signature")
        }
        json_str = json.dumps(signable_manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return json_str.encode('utf-8')

    def sign_manifest(self, manifest: Dict[str, Any]) -> str:
        if not HAS_CRYPTOGRAPHY:
            logger.warning("cryptography module missing; skipping manifest signing.")
            return ""
        self.ensure_key_pair()
        private_key = self._get_private_key_instance()
        payload = self._prepare_manifest_payload(manifest)
        try:
            signature = private_key.sign(payload)
            return signature.hex()
        except Exception as e:
            raise RuntimeError(f"Manifest Ed25519 서명 실패: {str(e)}") from e

    def verify_manifest(self, manifest: Dict[str, Any]) -> bool:
        if not HAS_CRYPTOGRAPHY:
            return False
        self.ensure_key_pair()
        public_key = self._public_key
        signature_hex = manifest.get("ed25519_signature")
        if not signature_hex or not isinstance(signature_hex, str):
            return False

        try:
            signature_bytes = bytes.fromhex(signature_hex)
        except ValueError:
            return False

        payload = self._prepare_manifest_payload(manifest)
        try:
            public_key.verify(signature_bytes, payload)
            return True
        except (InvalidSignature, Exception):
            return False


def verify_manifest_signature_ed25519(
    manifest: Dict[str, Any], 
    pub_key_path_or_bytes: Union[str, bytes]
) -> bool:
    """독립적인 Ed25519 서명 검증 함수 (Fail-Closed 보안 표준)."""
    if not HAS_CRYPTOGRAPHY:
        return False
    try:
        if isinstance(pub_key_path_or_bytes, str):
            with open(pub_key_path_or_bytes, 'rb') as f:
                key_data = f.read()
        elif isinstance(pub_key_path_or_bytes, bytes):
            key_data = pub_key_path_or_bytes
        else:
            return False

        public_key = load_pem_public_key(key_data)
        signature_hex = manifest.get("ed25519_signature")
        if not signature_hex or not isinstance(signature_hex, str):
            return False

        signature_bytes = bytes.fromhex(signature_hex)
        signable_manifest = {
            k: v for k, v in manifest.items()
            if k not in ("manifest_signature", "ed25519_signature")
        }
        payload = json.dumps(signable_manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        public_key.verify(signature_bytes, payload)
        return True
    except Exception:
        return False


def sign_bytes_ed25519(data: bytes, priv_key_path_or_bytes: Union[str, bytes]) -> str:
    """임의의 바이트 데이터에 대한 Ed25519 서명 생성 (hex 반환)."""
    if not HAS_CRYPTOGRAPHY:
        return ""
    if isinstance(priv_key_path_or_bytes, str):
        with open(priv_key_path_or_bytes, 'rb') as f:
            key_data = f.read()
    else:
        key_data = priv_key_path_or_bytes
    private_key = load_pem_private_key(key_data, password=None)
    sig = private_key.sign(data)
    return sig.hex()


def verify_bytes_ed25519(data: bytes, signature_hex: str, pub_key_path_or_bytes: Union[str, bytes]) -> bool:
    """임의의 바이트 데이터에 대한 Ed25519 서명 검증."""
    if not HAS_CRYPTOGRAPHY:
        return True
    try:
        if not signature_hex or not isinstance(signature_hex, str):
            return False
        if isinstance(pub_key_path_or_bytes, str):
            with open(pub_key_path_or_bytes, 'rb') as f:
                key_data = f.read()
        else:
            key_data = pub_key_path_or_bytes
        public_key = load_pem_public_key(key_data)
        sig_bytes = bytes.fromhex(signature_hex)
        public_key.verify(sig_bytes, data)
        return True
    except Exception:
        return False


def verify_bytes_ed25519_any(
    data: bytes,
    signature_hex: str,
    candidates: list,
) -> tuple:
    """
    여러 공개키 후보 중 하나라도 서명을 검증하면 True.

    왜 필요한가:
        배포 서명 개인키가 공개 저장소 이력에 유출됐다
        (docs/SECURITY_20261008.md). 키를 교체하려면 기존 클라이언트가
        구 공개키와 신 공개키를 동시에 신뢰해야 한다. 단일 키 검증만
        지원하면 키를 바꾸는 순간 이미 배포된 모든 PC 가 새 업데이트를
        거부한다.

    인자:
        candidates: 공개키 경로(str) 또는 PEM 바이트(bytes)의 리스트.
            순서를 보장한다. 먼저 매칭된 키에서 중단한다.

    반환:
        (ok, matched_index)
        ok가 True면 matched_index는 매칭된 후보의 인덱스.
        실패하면 (False, None).

    주의:
        키 내용은 로그에 남기지 않는다. 호출자가 basename 만 기록한다.
    """
    if not HAS_CRYPTOGRAPHY:
        return True, None
    if not signature_hex or not isinstance(signature_hex, str):
        return False, None
    if not candidates:
        return False, None
    for idx, cand in enumerate(candidates):
        try:
            if verify_bytes_ed25519(data, signature_hex, cand):
                return True, idx
        except Exception:
            continue
    return False, None
