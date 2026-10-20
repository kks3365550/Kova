# -*- coding: utf-8 -*-
"""
core/updater_v2/models.py: OTA 데이터 계약 모델 정의 (Type & Structure Validation)

주의:
- 본 모듈은 데이터의 타입, 필수 필드 존재 여부, 기본 형식(Hex, 길이 등)의 구조적 유효성만 검증합니다.
- '버전이 롤백 가능한가?', '이 버전이 폐기되었는가?' 등의 정책 의미론(Semantics) 판단은
  반드시 상위 PolicySemantics 계층에서 수행하며 본 모델에 포함하지 않습니다.
"""

import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HEX_128_PATTERN = re.compile(r"^[0-9a-f]{128}$")

# Strict SemVer 2.0.0 specification (No leading zeros, no 'v' prefix)
# Group 1: Major, Group 2: Minor, Group 3: Patch
# Group 4: Prerelease (optional), Group 5: Build metadata (optional)
SEMVER_200_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class ValidationError(ValueError):
    """OTA 데이터 계약 유효성 검증 실패 예외"""
    pass


@dataclass(frozen=True)
class ManifestDocument:
    """Artifact Trust: 배포 파일(package.zip)의 신원과 무결성을 증명하는 명세"""
    schema_version: str
    version: str
    package_name: str
    package_sha256: str
    file_size: int
    signing_key_id: str
    created_at: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ManifestDocument":
        if not isinstance(data, dict):
            raise ValidationError(f"Manifest must be a dictionary, got {type(data)}")

        required = [
            "schema_version", "version", "package_name",
            "package_sha256", "file_size", "signing_key_id", "created_at"
        ]
        for field_name in required:
            if field_name not in data:
                raise ValidationError(f"Missing required manifest field: '{field_name}'")

        schema_ver = str(data["schema_version"]).strip()
        if schema_ver != "1.0":
            raise ValidationError(f"Unsupported manifest schema_version: '{schema_ver}'")

        ver = str(data["version"]).strip()
        if not SEMVER_200_PATTERN.match(ver):
            raise ValidationError(f"Invalid SemVer 2.0.0 in manifest: '{ver}' (Must not have 'v' prefix or leading zeros)")

        pkg_name = str(data["package_name"]).strip()
        if not pkg_name or "/" in pkg_name or "\\" in pkg_name:
            raise ValidationError(f"Invalid package_name (must be simple filename): '{pkg_name}'")

        sha256 = str(data["package_sha256"]).strip().lower()
        if not HEX_64_PATTERN.match(sha256):
            raise ValidationError(f"Invalid package_sha256 (must be 64 hex characters): '{sha256}'")

        try:
            file_size = int(data["file_size"])
            if file_size <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError(f"Invalid file_size (must be positive integer): {data.get('file_size')}")

        key_id = str(data["signing_key_id"]).strip()
        if not key_id:
            raise ValidationError("signing_key_id cannot be empty")

        created_at = str(data["created_at"]).strip()
        if not created_at:
            raise ValidationError("created_at cannot be empty")

        return cls(
            schema_version=schema_ver,
            version=ver,
            package_name=pkg_name,
            package_sha256=sha256,
            file_size=file_size,
            signing_key_id=key_id,
            created_at=created_at
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "package_name": self.package_name,
            "package_sha256": self.package_sha256,
            "file_size": self.file_size,
            "signing_key_id": self.signing_key_id,
            "created_at": self.created_at
        }


@dataclass(frozen=True)
class PolicyDocument:
    """Deployment Policy Trust: 버전 취급 규칙 및 강제/롤백 정책을 정의하는 명세"""
    schema_version: str
    channel: str
    policy_sequence: int
    latest_version: str
    minimum_version: str
    revoked_versions: List[str]
    rollback_target: Optional[str]
    force_update: bool
    max_allowed_version_jump: Dict[str, int]
    signing_key_id: str
    valid_until: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolicyDocument":
        if not isinstance(data, dict):
            raise ValidationError(f"Policy must be a dictionary, got {type(data)}")

        required = [
            "schema_version", "channel", "policy_sequence",
            "latest_version", "minimum_version", "signing_key_id", "valid_until"
        ]
        for field_name in required:
            if field_name not in data:
                raise ValidationError(f"Missing required policy field: '{field_name}'")

        schema_ver = str(data["schema_version"]).strip()
        if schema_ver != "1.0":
            raise ValidationError(f"Unsupported policy schema_version: '{schema_ver}'")

        channel = str(data["channel"]).strip().lower()
        if channel not in ("stable", "beta", "dev"):
            raise ValidationError(f"Unknown release channel: '{channel}'")

        try:
            seq = int(data["policy_sequence"])
            if seq < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError(f"policy_sequence must be non-negative integer, got {data.get('policy_sequence')}")

        latest_ver = str(data["latest_version"]).strip()
        min_ver = str(data["minimum_version"]).strip()
        if not SEMVER_200_PATTERN.match(latest_ver):
            raise ValidationError(f"Invalid SemVer 2.0.0 in latest_version: '{latest_ver}'")
        if not SEMVER_200_PATTERN.match(min_ver):
            raise ValidationError(f"Invalid SemVer 2.0.0 in minimum_version: '{min_ver}'")

        raw_revoked = data.get("revoked_versions", [])
        if not isinstance(raw_revoked, list):
            raise ValidationError("revoked_versions must be a list of strings")
        revoked = []
        for x in raw_revoked:
            s_ver = str(x).strip()
            if not SEMVER_200_PATTERN.match(s_ver):
                raise ValidationError(f"Invalid SemVer in revoked_versions: '{s_ver}'")
            revoked.append(s_ver)

        rb_target = data.get("rollback_target")
        if rb_target is not None:
            rb_target = str(rb_target).strip()
            if not SEMVER_200_PATTERN.match(rb_target):
                raise ValidationError(f"Invalid SemVer in rollback_target: '{rb_target}'")

        force_up = bool(data.get("force_update", False))

        raw_jump = data.get("max_allowed_version_jump", {"major": 1, "minor": 5})
        if not isinstance(raw_jump, dict):
            raw_jump = {"major": 1, "minor": 5}
        jump_dict = {
            "major": int(raw_jump.get("major", 1)),
            "minor": int(raw_jump.get("minor", 5))
        }

        key_id = str(data["signing_key_id"]).strip()
        valid_until = str(data["valid_until"]).strip()

        return cls(
            schema_version=schema_ver,
            channel=channel,
            policy_sequence=seq,
            latest_version=latest_ver,
            minimum_version=min_ver,
            revoked_versions=revoked,
            rollback_target=rb_target,
            force_update=force_up,
            max_allowed_version_jump=jump_dict,
            signing_key_id=key_id,
            valid_until=valid_until
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "channel": self.channel,
            "policy_sequence": self.policy_sequence,
            "latest_version": self.latest_version,
            "minimum_version": self.minimum_version,
            "revoked_versions": self.revoked_versions,
            "rollback_target": self.rollback_target,
            "force_update": self.force_update,
            "max_allowed_version_jump": self.max_allowed_version_jump,
            "signing_key_id": self.signing_key_id,
            "valid_until": self.valid_until
        }


@dataclass(frozen=True)
class AcquiredUpdate:
    """
    온라인 수신 또는 오프라인 번들 추출 시 획득한 원본 바이트 컨테이너.
    서명 검증은 반드시 이 원본 바이트를 JCS 정규화하여 수행하며,
    객체로 역직렬화한 후 재직렬화하여 검증하지 않습니다.
    """
    policy_bytes: bytes
    policy_sig: str
    manifest_bytes: bytes
    manifest_sig: str
    package_bytes: Optional[bytes] = None
    package_path: Optional[str] = None

    def validate_signatures_format(self) -> None:
        """서명 문자열의 128자리 소문자 Hex 형식 검증"""
        p_sig = self.policy_sig.strip().lower()
        m_sig = self.manifest_sig.strip().lower()
        if not HEX_128_PATTERN.match(p_sig):
            raise ValidationError(f"policy_sig must be 128-char hex, got '{self.policy_sig[:16]}...'")
        if not HEX_128_PATTERN.match(m_sig):
            raise ValidationError(f"manifest_sig must be 128-char hex, got '{self.manifest_sig[:16]}...'")
