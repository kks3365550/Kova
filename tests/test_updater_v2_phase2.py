# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_phase2.py: OTA 2단계 (KeyRing, PolicyVerifier, PolicySemantics) 정밀 공격 검증 테스트

검증 항목 (8대 필수 공격 테스트 + Key Rotation):
1. valid signature + modified policy       -> REJECT
2. valid signature + wrong key_id          -> REJECT
3. old sequence                            -> REJECT (ReplayAttackError)
4. same sequence + different policy        -> REJECT (ReplayConflictError)
5. same sequence + identical policy        -> ACCEPT (Idempotent Duplicate)
6. revoked target                          -> REJECT_TARGET
7. revoked current + invalid rollback      -> REJECT_POLICY
8. revoked current + valid rollback        -> FORCE_ROLLBACK
9. excessive version jump                  -> REJECT_TARGET
10. expired policy                         -> REJECT_POLICY
11. Key Rotation Trust Chain (2026a -> 2026b 위임 서명 검증 및 활성화)
"""

import json
import unittest
from datetime import datetime, timezone, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.updater_v2.jcs import canonicalize
from core.updater_v2.keyring import (
    KeyRing,
    TrustedKey,
    KeyStatus,
    KeyRotationRecord,
    KeyRingError,
    KeyNotFoundError,
    KeyStatusError
)
from core.updater_v2.policy_verifier import (
    PolicyVerifier,
    PolicyVerificationError,
    ReplayAttackError,
    ReplayConflictError
)
from core.updater_v2.policy_semantics import (
    PolicySemanticsEvaluator,
    PolicyDecision
)
from core.updater_v2.models import PolicyDocument


class TestPhase2KeyRingAndPolicy(unittest.TestCase):
    """Phase 2 키링, 정책 검증기 및 의미론 평가기 정밀 테스트"""

    def setUp(self):
        # 1. 2026a 테스트 키 페어 생성
        self.priv_2026a = Ed25519PrivateKey.generate()
        self.pub_bytes_2026a = self.priv_2026a.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw
        )

        # 2. 2026b 테스트 키 페어 생성 (로테이션용)
        self.priv_2026b = Ed25519PrivateKey.generate()
        self.pub_bytes_2026b = self.priv_2026b.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw
        )

        # 3. KeyRing 초기화 및 2026a 등록
        self.keyring = KeyRing()
        self.keyring.add_trusted_key(
            TrustedKey(
                key_id="ed25519_pol_2026a",
                public_key_bytes=self.pub_bytes_2026a,
                status=KeyStatus.ACTIVE,
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-12-31T23:59:59Z"
            )
        )

        self.verifier = PolicyVerifier(keyring=self.keyring)

        # 기본 유효 정책 템플릿
        self.base_policy_dict = {
            "schema_version": "1.0",
            "channel": "stable",
            "policy_sequence": 100,
            "latest_version": "2.9.9",
            "minimum_version": "2.9.8",
            "revoked_versions": [],
            "rollback_target": None,
            "force_update": False,
            "max_allowed_version_jump": {"major": 1, "minor": 5},
            "signing_key_id": "ed25519_pol_2026a",
            "valid_until": "2026-12-31T23:59:59Z"
        }

    def _sign_policy(self, policy_dict: dict, priv_key: Ed25519PrivateKey) -> tuple[bytes, str]:
        """정책을 RFC 8785 정규화 후 Ed25519 서명하여 (raw_bytes, sig_hex) 반환"""
        raw_bytes = json.dumps(policy_dict).encode("utf-8")
        # Sign over canonical bytes
        canonical_bytes = canonicalize(policy_dict)
        sig = priv_key.sign(canonical_bytes)
        return raw_bytes, sig.hex()

    def test_01_valid_policy_signature_accepted(self):
        """정상 서명된 정책 통과 검증"""
        raw_bytes, sig_hex = self._sign_policy(self.base_policy_dict, self.priv_2026a)
        res = self.verifier.verify_policy(raw_bytes, sig_hex, cached_sequence=90)
        self.assertEqual(res.sequence, 100)
        self.assertFalse(res.is_duplicate)

    def test_02_modified_policy_rejected(self):
        """1. 서명 생성 후 페이로드를 단 1바이트라도 변조한 경우 거부"""
        raw_bytes, sig_hex = self._sign_policy(self.base_policy_dict, self.priv_2026a)
        # 페이로드 내부의 latest_version을 2.9.10으로 몰래 변조
        tampered_dict = json.loads(raw_bytes.decode("utf-8"))
        tampered_dict["latest_version"] = "2.9.10"
        tampered_bytes = json.dumps(tampered_dict).encode("utf-8")

        with self.assertRaises(PolicyVerificationError):
            self.verifier.verify_policy(tampered_bytes, sig_hex, cached_sequence=90)

    def test_03_wrong_key_id_rejected(self):
        """2. 서명은 올바르나 key_id가 조작되었거나 일치하지 않는 경우 거부"""
        p_dict = dict(self.base_policy_dict)
        p_dict["signing_key_id"] = "non_existent_key_id"
        raw_bytes, sig_hex = self._sign_policy(p_dict, self.priv_2026a)

        with self.assertRaises(PolicyVerificationError):
            self.verifier.verify_policy(raw_bytes, sig_hex, cached_sequence=90)

    def test_04_old_sequence_rejected(self):
        """3. 과거 시퀀스 번호 재전송(Replay Attack) 거부"""
        # cached sequence = 100, incoming sequence = 99
        p_dict = dict(self.base_policy_dict)
        p_dict["policy_sequence"] = 99
        raw_bytes, sig_hex = self._sign_policy(p_dict, self.priv_2026a)

        with self.assertRaises(ReplayAttackError):
            self.verifier.verify_policy(raw_bytes, sig_hex, cached_sequence=100)

    def test_05_same_sequence_different_policy_rejected(self):
        """4. 동일 시퀀스 번호인데 내용이 다른 변조 정책(Conflict) 거부"""
        raw_bytes_orig, sig_orig = self._sign_policy(self.base_policy_dict, self.priv_2026a)
        res_orig = self.verifier.verify_policy(raw_bytes_orig, sig_orig, cached_sequence=90)

        # 동일한 sequence=100을 사용하지만 다른 최신 버전을 가리키는 서명된 공격 정책 생성
        attack_dict = dict(self.base_policy_dict)
        attack_dict["latest_version"] = "2.9.8"
        raw_bytes_attack, sig_attack = self._sign_policy(attack_dict, self.priv_2026a)

        with self.assertRaises(ReplayConflictError):
            self.verifier.verify_policy(
                raw_bytes_attack,
                sig_attack,
                cached_sequence=100,
                cached_canonical_bytes=res_orig.canonical_bytes
            )

    def test_06_same_sequence_identical_policy_idempotent(self):
        """5. 동일 시퀀스 번호 및 동일 내용 재수신 시 Idempotent No-op 수용"""
        raw_bytes, sig_hex = self._sign_policy(self.base_policy_dict, self.priv_2026a)
        res1 = self.verifier.verify_policy(raw_bytes, sig_hex, cached_sequence=90)

        # 재수신
        res2 = self.verifier.verify_policy(
            raw_bytes,
            sig_hex,
            cached_sequence=100,
            cached_canonical_bytes=res1.canonical_bytes
        )
        self.assertTrue(res2.is_duplicate)

    def test_07_revoked_target_rejected(self):
        """6. 배포 타겟(latest_version)이 revoked_versions에 포함된 경우 REJECT_TARGET"""
        evaluator = PolicySemanticsEvaluator(current_version="2.9.8")
        p_dict = dict(self.base_policy_dict)
        p_dict["latest_version"] = "2.9.9"
        p_dict["revoked_versions"] = ["2.9.9"]
        policy = PolicyDocument.from_dict(p_dict)

        decision, reason = evaluator.evaluate(policy)
        self.assertEqual(decision, PolicyDecision.REJECT_TARGET)
        self.assertIn("revoked", reason)

    def test_08_revoked_current_without_valid_rollback_rejected(self):
        """7. 현재 버전이 폐기되었는데 rollback_target이 없거나 부적격한 경우 REJECT_POLICY"""
        evaluator = PolicySemanticsEvaluator(current_version="2.9.9")
        p_dict = dict(self.base_policy_dict)
        p_dict["revoked_versions"] = ["2.9.9"]
        p_dict["rollback_target"] = None  # 타겟 누락
        policy = PolicyDocument.from_dict(p_dict)

        decision, reason = evaluator.evaluate(policy)
        self.assertEqual(decision, PolicyDecision.REJECT_POLICY)
        self.assertIn("no rollback_target", reason)

        # rollback_target 자체도 revoked인 경우
        p_dict["rollback_target"] = "2.9.8"
        p_dict["revoked_versions"] = ["2.9.9", "2.9.8"]
        policy_bad_rb = PolicyDocument.from_dict(p_dict)
        decision2, reason2 = evaluator.evaluate(policy_bad_rb)
        self.assertEqual(decision2, PolicyDecision.REJECT_POLICY)

    def test_09_revoked_current_with_valid_rollback_forces_rollback(self):
        """8. 현재 버전이 폐기되고 적격한 rollback_target이 있는 경우 FORCE_ROLLBACK"""
        evaluator = PolicySemanticsEvaluator(current_version="2.9.9")
        p_dict = dict(self.base_policy_dict)
        p_dict["revoked_versions"] = ["2.9.9"]
        p_dict["rollback_target"] = "2.9.8"
        policy = PolicyDocument.from_dict(p_dict)

        decision, reason = evaluator.evaluate(policy)
        self.assertEqual(decision, PolicyDecision.FORCE_ROLLBACK)
        self.assertIn("2.9.8", reason)

    def test_10_excessive_version_jump_rejected(self):
        """9. 허용 범위를 초과하는 비상식적인 메이저/마이너 버전 폭증 차단 (REJECT_TARGET)"""
        evaluator = PolicySemanticsEvaluator(current_version="2.9.8")
        # 2.9.8 -> 999.0.0 (허용 major jump: 1)
        p_dict = dict(self.base_policy_dict)
        p_dict["latest_version"] = "999.0.0"
        policy = PolicyDocument.from_dict(p_dict)

        decision, reason = evaluator.evaluate(policy)
        self.assertEqual(decision, PolicyDecision.REJECT_TARGET)
        self.assertIn("Excessive major version jump", reason)

    def test_11_expired_policy_rejected(self):
        """10. valid_until이 지난 만료된 정책 거부 (REJECT_POLICY)"""
        evaluator = PolicySemanticsEvaluator(current_version="2.9.8")
        p_dict = dict(self.base_policy_dict)
        p_dict["valid_until"] = "2025-01-01T00:00:00Z"
        policy = PolicyDocument.from_dict(p_dict)

        future_now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        decision, reason = evaluator.evaluate(policy, now=future_now)
        self.assertEqual(decision, PolicyDecision.REJECT_POLICY)
        self.assertIn("expired", reason)

    def test_12_key_rotation_trust_chain(self):
        """11. Key Rotation: 기존 신뢰 키(2026a)가 서명한 위임 레코드를 통해 2026b 키 활성화 및 서명 검증"""
        # 1. 2026a 키로 2026b 키 위임 레코드 서명
        record_dict = {
            "from_key_id": "ed25519_pol_2026a",
            "to_key_id": "ed25519_pol_2026b",
            "public_key": self.pub_bytes_2026b.hex().lower(),
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2027-12-31T23:59:59Z"
        }
        canonical_rotation = canonicalize(record_dict)
        rot_sig = self.priv_2026a.sign(canonical_rotation)

        rotation_record = KeyRotationRecord(
            old_key_id="ed25519_pol_2026a",
            new_key_id="ed25519_pol_2026b",
            new_public_key_hex=self.pub_bytes_2026b.hex(),
            valid_from="2026-09-01T00:00:00Z",
            valid_until="2027-12-31T23:59:59Z",
            rotation_signature_hex=rot_sig.hex()
        )

        # 2. KeyRing에 로테이션 적용
        self.keyring.apply_rotation(rotation_record)

        # 3. 신규 2026b 키로 서명된 신규 정책(seq=101) 검증 시도 -> 정상 통과 확인
        p_dict_new = dict(self.base_policy_dict)
        p_dict_new["policy_sequence"] = 101
        p_dict_new["signing_key_id"] = "ed25519_pol_2026b"
        raw_bytes_new, sig_hex_new = self._sign_policy(p_dict_new, self.priv_2026b)

        res = self.verifier.verify_policy(raw_bytes_new, sig_hex_new, cached_sequence=100)
        self.assertEqual(res.sequence, 101)
        self.assertEqual(res.policy.signing_key_id, "ed25519_pol_2026b")


if __name__ == "__main__":
    unittest.main()
