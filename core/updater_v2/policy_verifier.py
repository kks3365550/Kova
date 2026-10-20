# -*- coding: utf-8 -*-
"""
core/updater_v2/policy_verifier.py: Step 1 정책 무결성 및 리플레이 방어 검증기

검증 순서 (엄격 준수):
1. raw policy bytes 수신
2. JSON 파싱 및 기본 스키마/타입 검증 (models.PolicyDocument)
3. RFC 8785 JCS 정규화 (jcs.canonicalize)
4. key_id로 KeyRing에서 Ed25519 공개키 조회 및 상태/유효성 검증
5. Ed25519 서명 검증
6. 시퀀스 번호(policy_sequence) 안티 리플레이 및 동일 시퀀스 변조(Conflict) 검사

주의: 서명 검증이 통과하기 전에는 정책의 내용을 신뢰하거나 의사결정에 사용하지 않습니다.
"""

import json
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from .jcs import canonicalize
from .models import PolicyDocument, ValidationError
from .keyring import KeyRing, KeyRingError, SignatureVerificationError


class PolicyVerificationError(Exception):
    """정책 검증 실패 기본 예외"""
    pass


class ReplayAttackError(PolicyVerificationError):
    """과거 정책 재전송 공격 탐지"""
    pass


class ReplayConflictError(PolicyVerificationError):
    """동일 시퀀스 번호의 변조된 정책 충돌 탐지"""
    pass


@dataclass(frozen=True)
class PolicyVerificationResult:
    """정책 검증 성공 결과"""
    policy: PolicyDocument
    canonical_bytes: bytes
    sequence: int
    is_duplicate: bool = False


class PolicyVerifier:
    """배포 정책(policy.json)의 암호학적 서명 및 시퀀스 무결성을 검증하는 클래스"""

    def __init__(self, keyring: KeyRing):
        self.keyring = keyring

    def verify_policy(
        self,
        raw_policy_bytes: bytes,
        policy_signature_hex: str,
        cached_sequence: int = -1,
        cached_canonical_bytes: Optional[bytes] = None,
        now: Optional[datetime] = None
    ) -> PolicyVerificationResult:
        """
        수신된 정책 바이트를 전수 검증합니다.

        Args:
            raw_policy_bytes: 수신된 원본 policy.json 바이트
            policy_signature_hex: 128자리 hex Ed25519 서명값
            cached_sequence: 로컬에 캐시된 마지막 유효 시퀀스 번호
            cached_canonical_bytes: 로컬에 캐시된 마지막 유효 정책의 JCS 정규화 바이트
            now: 기준 시각 (테스트 주입용)

        Returns:
            PolicyVerificationResult
        """
        # 1. JSON Parse
        try:
            policy_dict = json.loads(raw_policy_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise PolicyVerificationError(f"Malformed policy JSON: {e}")

        # 2. Schema Validation (타입 및 필수 필드 형식 검증)
        try:
            policy = PolicyDocument.from_dict(policy_dict)
        except ValidationError as e:
            raise PolicyVerificationError(f"Policy schema validation failed: {e}")

        # 3. JCS Canonicalization
        canonical_bytes = canonicalize(policy.to_dict())

        # 4 & 5. KeyRing Lookup & Ed25519 Signature Verification
        try:
            self.keyring.verify(
                key_id=policy.signing_key_id,
                message=canonical_bytes,
                signature=policy_signature_hex,
                now=now
            )
        except SignatureVerificationError as e:
            raise PolicyVerificationError(f"Policy signature verification failed: {e}")
        except KeyRingError as e:
            raise PolicyVerificationError(f"Key error during policy verification: {e}")

        # 6. Sequence Anti-Replay Verification
        incoming_seq = policy.policy_sequence

        if incoming_seq > cached_sequence:
            # 정상적인 신규 정책 전진
            return PolicyVerificationResult(
                policy=policy,
                canonical_bytes=canonical_bytes,
                sequence=incoming_seq,
                is_duplicate=False
            )

        elif incoming_seq == cached_sequence:
            # 동일 시퀀스 번호 재수신 (Idempotent vs Conflict 판정)
            if cached_canonical_bytes is not None and canonical_bytes == cached_canonical_bytes:
                # 100% 동일한 정책 재수신 ➔ Idempotent No-op 허용
                return PolicyVerificationResult(
                    policy=policy,
                    canonical_bytes=canonical_bytes,
                    sequence=incoming_seq,
                    is_duplicate=True
                )
            else:
                # 동일한 시퀀스 번호인데 내용이 다른 변조 정책 ➔ 거부
                raise ReplayConflictError(
                    f"Conflict detected: Received policy sequence {incoming_seq} with different payload"
                )

        else:
            # incoming_seq < cached_sequence (과거 정책 리플레이 공격)
            raise ReplayAttackError(
                f"Replay attack detected: Incoming sequence {incoming_seq} < cached sequence {cached_sequence}"
            )
