# -*- coding: utf-8 -*-
"""
core/updater_v2/policy_semantics.py: Step 2 정책 의미론(Semantics) 및 액션 의사결정 엔진

책임:
1. 암호학적 서명이 통과된 PolicyDocument의 비즈니스/운영 규칙 검증
2. 검증 항목:
   - 정책 만료 여부 (valid_until vs now)
   - 릴리스 채널 일치 여부 (channel vs expected_channel)
   - 최소 버전과 최신 버전의 논리적 모순 (minimum_version <= latest_version)
   - 폐기 버전(revoked_versions) 및 롤백 타겟(rollback_target) 유효성
   - 과도한 버전 점프 방어 (max_allowed_version_jump)
3. 출력:
   - 명시적인 PolicyDecision Enum 반환 (NOOP, PROCEED_UPDATE, FORCE_UPDATE, FORCE_ROLLBACK, REJECT_POLICY, REJECT_TARGET)
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple

from .models import PolicyDocument


class PolicyDecision(str, Enum):
    NOOP = "NOOP"                          # 현재 버전 유지 (이미 최신이거나 업데이트 불필요)
    PROCEED_UPDATE = "PROCEED_UPDATE"      # 일반 권장 업데이트 진행
    FORCE_UPDATE = "FORCE_UPDATE"          # 보안 패치/최소 지원 미달에 따른 강제 업데이트
    FORCE_ROLLBACK = "FORCE_ROLLBACK"      # 현재 버전 폐기에 따른 강제 롤백
    REJECT_POLICY = "REJECT_POLICY"        # 정책 자체의 의미론적 모순/만료로 인한 정책 거부
    REJECT_TARGET = "REJECT_TARGET"        # 배포 대상 버전이 폐기되었거나 비정상적 점프로 인한 거부


def parse_semver(ver_str: str) -> Tuple[int, int, int]:
    """버전 문자열에서 (major, minor, patch) 정수 튜플 추출"""
    clean = ver_str.strip().lstrip('v').lstrip('V')
    # Prerelease / build metadata 분리
    base = clean.split('-')[0].split('+')[0]
    parts = base.split('.')
    while len(parts) < 3:
        parts.append('0')
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 0, 0, 0


class PolicySemanticsEvaluator:
    """검증된 정책 문서를 해석하여 업데이트 전략을 결정하는 평가기"""

    def __init__(self, current_version: str, expected_channel: str = "stable"):
        self.current_version = current_version.strip()
        self.expected_channel = expected_channel.strip().lower()

    def evaluate(
        self,
        policy: PolicyDocument,
        now: Optional[datetime] = None
    ) -> Tuple[PolicyDecision, str]:
        """
        정책을 의미론적으로 평가하고 (PolicyDecision, reason) 튜플을 반환합니다.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # 1. 정책 유효기간 검증 (valid_until)
        if policy.valid_until:
            try:
                v_until = datetime.fromisoformat(policy.valid_until)
                if v_until.tzinfo is None:
                    v_until = v_until.replace(tzinfo=timezone.utc)
                if now > v_until:
                    return PolicyDecision.REJECT_POLICY, f"Policy expired at {policy.valid_until} (current={now.isoformat()})"
            except Exception as e:
                return PolicyDecision.REJECT_POLICY, f"Invalid valid_until timestamp in policy: {e}"

        # 2. 릴리스 채널 검증
        if policy.channel.lower() != self.expected_channel:
            return PolicyDecision.REJECT_POLICY, f"Channel mismatch: expected '{self.expected_channel}', got '{policy.channel}'"

        cur_semver = parse_semver(self.current_version)
        min_semver = parse_semver(policy.minimum_version)
        latest_semver = parse_semver(policy.latest_version)

        # 3. 최소 버전과 최신 버전의 논리적 모순 검증 (minimum <= latest)
        if min_semver > latest_semver:
            return PolicyDecision.REJECT_POLICY, (
                f"Contradictory policy: minimum_version ({policy.minimum_version}) > latest_version ({policy.latest_version})"
            )

        # 4. 현재 버전이 폐기(Revoked)되었는지 확인
        if self.current_version in policy.revoked_versions:
            # 롤백 타겟이 지정되어 있고, 롤백 타겟 자체가 폐기 목록에 없는지 검증
            if not policy.rollback_target:
                return PolicyDecision.REJECT_POLICY, (
                    f"Current version '{self.current_version}' is revoked, but no rollback_target was specified in policy"
                )
            if policy.rollback_target in policy.revoked_versions:
                return PolicyDecision.REJECT_POLICY, (
                    f"Invalid rollback_target '{policy.rollback_target}': Target is also in revoked_versions"
                )
            return PolicyDecision.FORCE_ROLLBACK, (
                f"Current version '{self.current_version}' is revoked. Forcing rollback to '{policy.rollback_target}'"
            )

        # 5. 배포 타겟(latest_version)이 폐기 목록에 포함되어 있는지 확인
        if policy.latest_version in policy.revoked_versions:
            return PolicyDecision.REJECT_TARGET, f"Target version '{policy.latest_version}' is marked as revoked"

        # 6. 과도한 버전 점프 방어 (max_allowed_version_jump)
        # 예: 2.9.8 -> 999.0.0 과 같은 비상식적 폭증 차단
        if latest_semver > cur_semver:
            major_jump = latest_semver[0] - cur_semver[0]
            max_major = policy.max_allowed_version_jump.get("major", 1)
            max_minor = policy.max_allowed_version_jump.get("minor", 5)

            if major_jump > max_major:
                return PolicyDecision.REJECT_TARGET, (
                    f"Excessive major version jump: {self.current_version} -> {policy.latest_version} "
                    f"(jump={major_jump}, allowed={max_major})"
                )
            if major_jump == 0:
                minor_jump = latest_semver[1] - cur_semver[1]
                if minor_jump > max_minor:
                    return PolicyDecision.REJECT_TARGET, (
                        f"Excessive minor version jump: {self.current_version} -> {policy.latest_version} "
                        f"(jump={minor_jump}, allowed={max_minor})"
                    )

        # 7. 버전 비교 및 최종 액션 결정
        if latest_semver == cur_semver:
            return PolicyDecision.NOOP, f"Already at latest version ({self.current_version})"

        if latest_semver < cur_semver:
            return PolicyDecision.NOOP, f"Current version ({self.current_version}) is newer than latest ({policy.latest_version})"

        # latest_semver > cur_semver
        if policy.force_update or (cur_semver < min_semver):
            reason = "Mandatory flag set" if policy.force_update else f"Current version below minimum ({policy.minimum_version})"
            return PolicyDecision.FORCE_UPDATE, f"Force update required to {policy.latest_version} ({reason})"

        return PolicyDecision.PROCEED_UPDATE, f"Update available: {self.current_version} -> {policy.latest_version}"
