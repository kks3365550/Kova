# -*- coding: utf-8 -*-
"""
core/crypto_at_rest.py - 저장 데이터 암호화 엔진 (At-Rest Encryption Engine)
v2.7.0 Sprint 3: CAS Dedup 100% 보존, Envelope Key Architecture, v0/v1 하위 호환성 지원

- CAS Identity: 원본 평문 SHA-256을 블롭 키로 유지하여 중복제거(Dedup) 100% 보존.
- Blob 바이너리 구조:
  [b"ENC\x01" (4B)] + [12B Random Nonce] + [AES-256-GCM Ciphertext (Zstd)] + [16B Auth Tag]
- AAD 분리:
  * Blob AAD: {"blob_sha256": hex, "encryption_version": 1, "format_version": 1}
  * Wrapped Key AAD: {"created_at": ts, "encryption_version": 1, "snapshot_id": id}
- 하위 호환성:
  * v0 평문 블롭 (Magic: 0x28B52FFD) -> Zstd 압축 해제만 수행
  * v1 암호화 블롭 (Magic: b"ENC\x01") -> AES-256-GCM 복호화 후 Zstd 압축 해제
"""

import os
import time
import json
import secrets
import hashlib
import threading
from typing import Dict, Any, Optional, Tuple

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    zstd = None
    HAS_ZSTD = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidTag
    HAS_CRYPTO = True
except ImportError:
    AESGCM = None
    Scrypt = None
    InvalidTag = Exception
    HAS_CRYPTO = False


# 매직 헤더 상수
MAGIC_V0_ZSTD = b"\x28\xb5\x2f\xfd"
MAGIC_V1_ENC = b"ENC\x01"
ENCRYPTION_VERSION_V0 = 0
ENCRYPTION_VERSION_V1 = 1

# Scrypt 기본 파라미터 (설정 가능 메타데이터)
DEFAULT_SCRYPT_N = 32768
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
KEY_LENGTH = 32  # AES-256
SALT_LENGTH = 16


class StorageKeyDeriver:
    """Scrypt KDF 기반 스토리지 대칭키(Storage Key) 파생 및 메타데이터 관리"""

    @staticmethod
    def derive(master_key_bytes: bytes, salt: bytes, n: int = DEFAULT_SCRYPT_N, r: int = DEFAULT_SCRYPT_R, p: int = DEFAULT_SCRYPT_P) -> bytes:
        if not HAS_CRYPTO:
            raise RuntimeError("cryptography 라이브러리가 필요합니다 ('pip install cryptography')")
        kdf = Scrypt(
            salt=salt,
            length=KEY_LENGTH,
            n=n,
            r=r,
            p=p
        )
        return kdf.derive(master_key_bytes)


class CryptoAtRestEngine:
    """
    CAS Dedup 100% 보존 저장 데이터 암호화/복호화 엔진
    """

    def __init__(self, storage_key: bytes, kdf_metadata: Optional[Dict[str, Any]] = None):
        if not HAS_CRYPTO:
            raise RuntimeError("cryptography 라이브러리가 필요합니다")
        if len(storage_key) != 32:
            raise ValueError(f"Storage key는 32바이트(256비트)여야 합니다. 현재: {len(storage_key)}바이트")
        self.storage_key = storage_key
        self.kdf_metadata = kdf_metadata or {}
        self._aesgcm = AESGCM(self.storage_key)
        self._write_lock = threading.Lock()

    @classmethod
    def from_passphrase(cls, passphrase: str, repo_dir: str, create_if_missing: bool = True) -> 'CryptoAtRestEngine':
        """패스프레이즈 및 저장소 메타데이터(keys/key_metadata.json)로부터 엔진 인스턴스 초기화"""
        keys_dir = os.path.join(repo_dir, "keys")
        meta_file = os.path.join(keys_dir, "key_metadata.json")

        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            salt = bytes.fromhex(meta["kdf"]["salt"])
            n = meta["kdf"].get("n", DEFAULT_SCRYPT_N)
            r = meta["kdf"].get("r", DEFAULT_SCRYPT_R)
            p = meta["kdf"].get("p", DEFAULT_SCRYPT_P)
        else:
            if not create_if_missing:
                raise FileNotFoundError(f"키 메타데이터 파일이 없습니다: {meta_file}")
            os.makedirs(keys_dir, exist_ok=True)
            salt = secrets.token_bytes(SALT_LENGTH)
            n, r, p = DEFAULT_SCRYPT_N, DEFAULT_SCRYPT_R, DEFAULT_SCRYPT_P
            meta = {
                "version": 1,
                "cipher": "AES-256-GCM",
                "kdf": {
                    "algorithm": "scrypt",
                    "n": n,
                    "r": r,
                    "p": p,
                    "salt": salt.hex()
                }
            }
            tmp = meta_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, meta_file)

        storage_key = StorageKeyDeriver.derive(passphrase.encode("utf-8"), salt, n=n, r=r, p=p)
        return cls(storage_key=storage_key, kdf_metadata=meta)

    @classmethod
    def from_key_file(cls, key_file_path: str, repo_dir: str) -> 'CryptoAtRestEngine':
        """오프라인 백업 키 파일 또는 마스터 키 파일로부터 로드"""
        with open(key_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. key_metadata 백업본 형태
        if "kdf" in data and "salt" in data["kdf"]:
            salt = bytes.fromhex(data["kdf"]["salt"])
            n = data["kdf"].get("n", DEFAULT_SCRYPT_N)
            r = data["kdf"].get("r", DEFAULT_SCRYPT_R)
            p = data["kdf"].get("p", DEFAULT_SCRYPT_P)
            passphrase = data.get("passphrase", "")
            if not passphrase:
                raise ValueError("키 파일에 패스프레이즈가 없습니다.")
            storage_key = StorageKeyDeriver.derive(passphrase.encode("utf-8"), salt, n, r, p)
            return cls(storage_key, kdf_metadata=data)

        # 2. raw key hex 형태
        if "raw_storage_key_hex" in data:
            storage_key = bytes.fromhex(data["raw_storage_key_hex"])
            return cls(storage_key, kdf_metadata=data)

        raise ValueError("알 수 없는 키 파일 포맷입니다.")

    @staticmethod
    def build_blob_aad(plaintext_sha256: str) -> bytes:
        """
        블롭 암호화용 AAD (특정 snapshot_id에 바인딩하지 않음으로써 스냅샷 간 블롭 공유 보장)
        """
        aad_dict = {
            "format_version": 1,
            "blob_sha256": plaintext_sha256.lower(),
            "encryption_version": ENCRYPTION_VERSION_V1
        }
        return json.dumps(aad_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')

    def encrypt_blob_data(self, plaintext_data: bytes, plaintext_sha256: str, compress_level: int = 1) -> bytes:
        """
        평문 데이터를 Zstd 압축 후 AES-256-GCM 암호화
        바이너리 구조: [b"ENC\x01" 4B] + [12B Nonce] + [AES-GCM Ciphertext + 16B Tag]
        """
        if not HAS_ZSTD:
            raise RuntimeError("zstandard 라이브러리가 필요합니다")

        # 1. Zstd 압축
        cctx = zstd.ZstdCompressor(level=compress_level)
        compressed = cctx.compress(plaintext_data)

        # 2. 96-bit CSPRNG Nonce
        nonce = secrets.token_bytes(12)

        # 3. AAD 바인딩
        aad = self.build_blob_aad(plaintext_sha256)

        # 4. 암호화
        ciphertext_with_tag = self._aesgcm.encrypt(nonce, compressed, aad)

        # 5. 블롭 조립
        return MAGIC_V1_ENC + nonce + ciphertext_with_tag

    def decrypt_blob_data(self, blob_bytes: bytes, expected_plaintext_sha256: str) -> bytes:
        """
        블롭 바이트열을 복호화하고 Zstd 압축 해제하여 원본 평문 반환
        - v0 평문 블롭: Zstd 압축 해제만 수행 (하위 호환성 100%)
        - v1 암호화 블롭: AES-256-GCM 복호화 후 Zstd 압축 해제
        """
        if not HAS_ZSTD:
            raise RuntimeError("zstandard 라이브러리가 필요합니다")

        if len(blob_bytes) < 4:
            raise ValueError("손상된 블롭 파일: 길이가 너무 짧습니다.")

        magic = blob_bytes[:4]

        # 1. v0 평문 블롭 감지 (Zstd Magic 또는 b"ENC\x01"이 아닌 경우)
        if magic == MAGIC_V0_ZSTD or magic != MAGIC_V1_ENC:
            dctx = zstd.ZstdDecompressor()
            decompressed = dctx.decompress(blob_bytes)
            calc_hash = hashlib.sha256(decompressed).hexdigest()
            if calc_hash.lower() != expected_plaintext_sha256.lower():
                raise ValueError(f"v0 평문 해시 불일치 (기대: {expected_plaintext_sha256[:12]}, 실제: {calc_hash[:12]})")
            return decompressed

        # 2. v1 암호화 블롭 처리
        if len(blob_bytes) < 4 + 12 + 16:
            raise ValueError("손상된 v1 암호화 블롭: 헤더/태그 길이 부족")

        nonce = blob_bytes[4:16]
        ciphertext_with_tag = blob_bytes[16:]
        aad = self.build_blob_aad(expected_plaintext_sha256)

        try:
            compressed = self._aesgcm.decrypt(nonce, ciphertext_with_tag, aad)
        except InvalidTag as e:
            raise ValueError(f"블롭 복호화 실패 (인증 태그 불일치 / 암호화 데이터 또는 AAD 손상 탐지): {str(e)}") from e
        except Exception as e:
            raise ValueError(f"블롭 복호화 실패: {str(e)}") from e

        # 압축 해제
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(compressed)
        calc_hash = hashlib.sha256(decompressed).hexdigest()
        if calc_hash.lower() != expected_plaintext_sha256.lower():
            raise ValueError(f"복호화 후 해시 불일치 (기대: {expected_plaintext_sha256[:12]}, 실제: {calc_hash[:12]})")

        return decompressed

    def save_blob_atomic(self, repo_dir: str, plaintext_data: bytes, plaintext_sha256: str, compress_level: int = 1) -> Tuple[str, bool]:
        """
        CAS 원자적 저장 함수 (CAS Dedup 100% 보존 보장)
        Returns: (blob_path, is_newly_written: bool)
        - 이미 블롭 파일이 온전히 존재하면 신규 쓰기를 생략하고 (path, False) 반환
        - 신규 블롭일 경우 암호화하여 원자적으로 저장하고 (path, True) 반환
        """
        prefix = plaintext_sha256[:2]
        shard_dir = os.path.join(repo_dir, "blobs", prefix)
        blob_path = os.path.join(shard_dir, f"{plaintext_sha256}.blob")

        # 1. CAS 빠른 확인 (Dedup 보존)
        if os.path.exists(blob_path) and os.path.getsize(blob_path) > 0:
            return blob_path, False

        # 2. 신규 블롭 암호화
        enc_bytes = self.encrypt_blob_data(plaintext_data, plaintext_sha256, compress_level=compress_level)

        # 3. 원자적 안전 쓰기 (멀티스레드 race condition 방지)
        os.makedirs(shard_dir, exist_ok=True)
        tid = threading.get_ident()
        pid = os.getpid()
        tmp_path = f"{blob_path}.tmp_{pid}_{tid}_{secrets.token_hex(4)}"

        try:
            with open(tmp_path, "wb") as f:
                f.write(enc_bytes)
                f.flush()
                os.fsync(f.fileno())

            # WORM 잠금 (읽기 전용 부여)
            try:
                import stat
                os.chmod(tmp_path, stat.S_IREAD)
            except Exception:
                pass

            # 원자적 치환
            if os.path.exists(blob_path):
                return blob_path, False

            try:
                os.replace(tmp_path, blob_path)
                return blob_path, True
            except (FileExistsError, PermissionError, OSError):
                # 다른 워커가 찰나의 순간에 먼저 저장 및 WORM 잠금을 완료한 경우
                if os.path.exists(blob_path):
                    return blob_path, False
                raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    import stat
                    os.chmod(tmp_path, stat.S_IWRITE)
                    os.remove(tmp_path)
                except Exception:
                    pass

    def export_key_backup(self, backup_file_path: str, passphrase_hint: str = ""):
        """키 분실 방지를 위한 오프라인 키 백업 파일 생성 (Chaos 22 대응용)"""
        os.makedirs(os.path.dirname(os.path.abspath(backup_file_path)), exist_ok=True)
        backup_data = {
            "version": 1,
            "created_at": time.time(),
            "kdf_metadata": self.kdf_metadata,
            "raw_storage_key_hex": self.storage_key.hex(),
            "hint": passphrase_hint
        }
        tmp = backup_file_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)
        os.replace(tmp, backup_file_path)
