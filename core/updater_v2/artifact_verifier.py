# -*- coding: utf-8 -*-
"""
core/updater_v2/artifact_verifier.py: 아티팩트(Manifest & Package) 무결성 및 암호학적 검증기

검증 순서:
1. raw_manifest_bytes 파싱 및 ManifestDocument 스키마 검증
2. JCS(RFC 8785) 정규화 후 KeyRing을 통한 Ed25519 서명 검증
3. raw_package_bytes의 크기가 manifest.file_size와 정확히 일치하는지 검증
4. raw_package_bytes의 SHA-256 해시를 계산하여 manifest.package_sha256와 일치하는지 검증 (대소문자 무관)

주의:
- manifest_signature가 유효하지 않거나 SHA-256/크기가 불일치하면 패키지 압축 해제나 디스크 기록을 일체 진행하지 않습니다.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from .jcs import canonicalize
from .models import ManifestDocument, ValidationError
from .keyring import KeyRing, SignatureVerificationError


class ArtifactVerificationError(Exception):
    """아티팩트 검증 실패 기본 예외"""
    pass


class ManifestParseError(ArtifactVerificationError):
    """Manifest JSON 구문 오류 또는 스키마 위반"""
    pass


class ManifestSignatureError(ArtifactVerificationError):
    """Manifest 서명 검증 실패 (위조, 키 불일치, 만료 등)"""
    pass


class PackageSizeMismatchError(ArtifactVerificationError):
    """패키지 파일 크기 불일치"""
    pass


class PackageHashMismatchError(ArtifactVerificationError):
    """패키지 SHA-256 체크섬 불일치"""
    pass


@dataclass(frozen=True)
class ArtifactVerificationResult:
    """아티팩트 검증 성공 결과"""
    manifest: ManifestDocument
    canonical_manifest_bytes: bytes
    package_sha256: str
    package_size: int


class ArtifactVerifier:
    """배포 아티팩트(manifest.json, manifest.sig, package.zip)의 무결성을 전수 검증하는 클래스"""

    def __init__(self, keyring: KeyRing):
        self.keyring = keyring

    def verify_artifact(
        self,
        raw_manifest_bytes: bytes,
        manifest_signature_hex: str,
        raw_package_bytes: bytes,
        now: Optional[datetime] = None
    ) -> ArtifactVerificationResult:
        """
        수신된 아티팩트의 매니페스트 서명 및 패키지 바이트 무결성을 전수 검증합니다.

        Args:
            raw_manifest_bytes: 수신된 manifest.json의 원본 바이트
            manifest_signature_hex: 128자리 hex Ed25519 서명값
            raw_package_bytes: 수신된 package.zip의 원본 바이트
            now: 기준 시각 (테스트 주입용)

        Returns:
            ArtifactVerificationResult
        """
        # 1. Manifest JSON Parse
        try:
            manifest_dict = json.loads(raw_manifest_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ManifestParseError(f"Malformed manifest JSON: {e}")

        # 2. Manifest Schema Validation
        try:
            manifest = ManifestDocument.from_dict(manifest_dict)
        except ValidationError as e:
            raise ManifestParseError(f"Manifest schema validation failed: {e}")

        # 3. JCS Canonicalization
        canonical_bytes = canonicalize(manifest.to_dict())

        # 4. KeyRing Ed25519 Signature Verification
        try:
            self.keyring.verify(
                key_id=manifest.signing_key_id,
                message=canonical_bytes,
                signature=manifest_signature_hex,
                now=now
            )
        except SignatureVerificationError as e:
            raise ManifestSignatureError(f"Manifest signature verification failed: {e}")
        except Exception as e:
            raise ManifestSignatureError(f"Unexpected signature verification error: {e}")

        # 5. Package Size Check
        actual_size = len(raw_package_bytes)
        if actual_size != manifest.file_size:
            raise PackageSizeMismatchError(
                f"Package size mismatch: expected {manifest.file_size} bytes, got {actual_size} bytes"
            )

        # 6. Package SHA-256 Check (대소문자 무관)
        calc_sha256 = hashlib.sha256(raw_package_bytes).hexdigest().lower()
        expected_sha256 = manifest.package_sha256.lower()

        if calc_sha256 != expected_sha256:
            raise PackageHashMismatchError(
                f"Package SHA-256 mismatch: expected {expected_sha256}, got {calc_sha256}"
            )

        return ArtifactVerificationResult(
            manifest=manifest,
            canonical_manifest_bytes=canonical_bytes,
            package_sha256=calc_sha256,
            package_size=actual_size
        )
