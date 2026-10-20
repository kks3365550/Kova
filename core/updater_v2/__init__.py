# -*- coding: utf-8 -*-
"""
core/updater_v2: 차세대 오프라인/온라인 통합 OTA 패키지

Phase 1 모듈:
- jcs: RFC 8785 JSON Canonicalization Scheme 구현
- models: ManifestDocument, PolicyDocument, AcquiredUpdate
- artifact_safety: Step 4B 시스템 안전성/무해성 검증기

Phase 2 모듈:
- keyring: Trust Anchor, Ed25519 공개키 저장소, Key Rotation 위임
- policy_verifier: Step 1 정책 무결성 및 시퀀스 리플레이/변조 방어 검증기
- policy_semantics: Step 2 정책 의미론 평가 및 액션 의사결정 엔진
"""

from .jcs import canonicalize
from .models import (
    ManifestDocument,
    PolicyDocument,
    AcquiredUpdate,
    ValidationError
)
from .artifact_safety import (
    ArtifactSafetyChecker,
    assert_safe_destination_path,
    SafetyViolationError
)
from .keyring import (
    KeyRing,
    TrustedKey,
    KeyStatus,
    KeyRotationRecord,
    KeyRingError,
    KeyNotFoundError,
    KeyStatusError,
    SignatureVerificationError
)
from .policy_verifier import (
    PolicyVerifier,
    PolicyVerificationResult,
    PolicyVerificationError,
    ReplayAttackError,
    ReplayConflictError
)
from .policy_semantics import (
    PolicySemanticsEvaluator,
    PolicyDecision,
    parse_semver
)

# 하위 호환성 별칭
ArtifactPayload = AcquiredUpdate

__all__ = [
    # Phase 1
    "canonicalize",
    "ManifestDocument",
    "PolicyDocument",
    "AcquiredUpdate",
    "ArtifactPayload",
    "ValidationError",
    "ArtifactSafetyChecker",
    "assert_safe_destination_path",
    "SafetyViolationError",
    # Phase 2
    "KeyRing",
    "TrustedKey",
    "KeyStatus",
    "KeyRotationRecord",
    "KeyRingError",
    "KeyNotFoundError",
    "KeyStatusError",
    "SignatureVerificationError",
    "PolicyVerifier",
    "PolicyVerificationResult",
    "PolicyVerificationError",
    "ReplayAttackError",
    "ReplayConflictError",
    "PolicySemanticsEvaluator",
    "PolicyDecision",
    "parse_semver"
]
