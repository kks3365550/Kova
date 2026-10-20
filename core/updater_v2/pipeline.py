# -*- coding: utf-8 -*-
"""
core/updater_v2/pipeline.py: 통합 오프라인/온라인 업데이트 파이프라인 (UnifiedUpdatePipeline)

전체 실행 흐름 (단방향 방어선):
AcquiredUpdate (policy, policy.sig, manifest, manifest.sig, package)
    │
    ▼ [1. PolicyVerifier]
    ├─ JSON 파싱 & 스키마 검증
    ├─ JCS RFC 8785 정규화
    ├─ Ed25519 서명 검증 (KeyRing)
    └─ 안티 리플레이 시퀀스 검사
    │
    ▼ [2. PolicySemanticsEvaluator]
    ├─ NOOP: 이미 최신 버전 (정상 종료)
    ├─ REJECT_POLICY / REJECT_TARGET: 배포 중단 및 보안 예외
    ├─ FORCE_ROLLBACK: 롤백 타깃 지정 시 강제 복원 분기
    └─ PROCEED_UPDATE / FORCE_UPDATE: 업데이트 계속 진행
    │
    ▼ [3. ArtifactVerifier]
    ├─ Manifest JCS 정규화 & Ed25519 서명 검증 (KeyRing)
    ├─ package.zip 파일 크기 일치 검증
    └─ package.zip SHA-256 해시 일치 검증
    │
    ▼ [4. AtomicInstaller]
    ├─ 사전 무해성 검증 (ArtifactSafetyChecker: Zip-Slip & 4축 Zip Bomb)
    ├─ 현재 설치본 백업 (backup/v{현재버전})
    ├─ 백업 서버 안전 선별 종료 (포트 8765)
    ├─ 원자적 파일 교체 (최대 3회 재시도 파일락 방어)
    ├─ start_silent.vbs 무창 백그라운드 재시작
    └─ 12초 헬스체크 및 실패 시 100% 자동 원복
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable
from datetime import datetime

from .models import AcquiredUpdate
from .keyring import KeyRing
from .policy_verifier import PolicyVerifier, PolicyVerificationError
from .policy_semantics import PolicySemanticsEvaluator, PolicyDecision
from .artifact_verifier import ArtifactVerifier, ArtifactVerificationError
from .installer import AtomicInstaller, InstallerError

logger = logging.getLogger(__name__)


class PipelineExecutionError(Exception):
    """업데이트 파이프라인 실행 실패 기본 예외"""
    pass


@dataclass(frozen=True)
class PipelineExecutionResult:
    """파이프라인 실행 최종 결과"""
    success: bool
    decision: PolicyDecision
    installed_version: str
    message: str
    details: Optional[Dict[str, Any]] = None


class UnifiedUpdatePipeline:
    """모든 OTA 및 오프라인 번들 업데이트를 통합 조율하는 단일 파이프라인"""

    def __init__(
        self,
        keyring: KeyRing,
        target_dir: str,
        server_port: int = 8765,
        policy_verifier: Optional[PolicyVerifier] = None,
        artifact_verifier: Optional[ArtifactVerifier] = None,
        installer: Optional[AtomicInstaller] = None
    ):
        self.keyring = keyring
        self.target_dir = target_dir
        self.policy_verifier = policy_verifier or PolicyVerifier(keyring=self.keyring)
        self.artifact_verifier = artifact_verifier or ArtifactVerifier(keyring=self.keyring)
        self.installer = installer or AtomicInstaller(target_dir=self.target_dir, server_port=server_port)

    def execute_update(
        self,
        acquired: AcquiredUpdate,
        cached_sequence: int = -1,
        cached_canonical_bytes: Optional[bytes] = None,
        skip_process_control: bool = False,
        now: Optional[datetime] = None,
        debug_crash_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ) -> PipelineExecutionResult:
        """
        AcquiredUpdate를 입력받아 전 과정을 엄격히 검증하고 설치합니다.
        """
        current_version = self.installer.get_current_installed_version()
        logger.info("PIPELINE_START current_version=%s", current_version)

        # 0. 서명 문자열 형식 사전 검증
        acquired.validate_signatures_format()

        if not acquired.package_bytes:
            raise PipelineExecutionError("AcquiredUpdate is missing package_bytes")

        # -------------------------------------------------------------------
        # Step 1: PolicyVerifier (정책 암호학적 서명 및 안티 리플레이)
        # -------------------------------------------------------------------
        try:
            policy_result = self.policy_verifier.verify_policy(
                raw_policy_bytes=acquired.policy_bytes,
                policy_signature_hex=acquired.policy_sig,
                cached_sequence=cached_sequence,
                cached_canonical_bytes=cached_canonical_bytes,
                now=now
            )
        except PolicyVerificationError as e:
            logger.error("PIPELINE_POLICY_VERIFICATION_FAILED: %s", e)
            raise PipelineExecutionError(f"Policy verification failed: {e}") from e

        policy = policy_result.policy

        # -------------------------------------------------------------------
        # Step 2: PolicySemanticsEvaluator (정책 의미론 평가)
        # -------------------------------------------------------------------
        evaluator = PolicySemanticsEvaluator(
            current_version=current_version,
            expected_channel=policy.channel
        )
        decision, reason = evaluator.evaluate(
            policy=policy,
            now=now
        )
        logger.info("PIPELINE_POLICY_DECISION: %s (reason=%s)", decision.name, reason)

        if decision == PolicyDecision.NOOP:
            return PipelineExecutionResult(
                success=True,
                decision=decision,
                installed_version=current_version,
                message=f"Already running target version {current_version}. No update needed.",
                details={"policy_sequence": policy.policy_sequence}
            )

        if decision in (PolicyDecision.REJECT_POLICY, PolicyDecision.REJECT_TARGET):
            raise PipelineExecutionError(f"Policy semantics rejected update: {reason}")

        # 강제 롤백 처리
        if decision == PolicyDecision.FORCE_ROLLBACK:
            rollback_ver = policy.rollback_target
            backup_ver_dir = os.path.join(self.target_dir, "backup", f"v{rollback_ver}")
            logger.warning("PIPELINE_TRIGGER_FORCE_ROLLBACK to %s using %s", rollback_ver, backup_ver_dir)
            if not os.path.exists(backup_ver_dir):
                raise PipelineExecutionError(f"Rollback target directory does not exist: {backup_ver_dir}")

            rb_success = self.installer.rollback(backup_ver_dir)
            if not rb_success:
                raise PipelineExecutionError("Forced rollback execution failed")

            return PipelineExecutionResult(
                success=True,
                decision=decision,
                installed_version=rollback_ver,
                message=f"Successfully forced rollback to {rollback_ver}",
                details={"rollback_target": rollback_ver}
            )

        # -------------------------------------------------------------------
        # Step 3: ArtifactVerifier (아티팩트 서명, 크기, SHA-256)
        # -------------------------------------------------------------------
        try:
            artifact_result = self.artifact_verifier.verify_artifact(
                raw_manifest_bytes=acquired.manifest_bytes,
                manifest_signature_hex=acquired.manifest_sig,
                raw_package_bytes=acquired.package_bytes,
                now=now
            )
        except ArtifactVerificationError as e:
            logger.error("PIPELINE_ARTIFACT_VERIFICATION_FAILED: %s", e)
            raise PipelineExecutionError(f"Artifact verification failed: {e}") from e

        manifest = artifact_result.manifest

        # 정책 상의 최신 버전과 매니페스트 상의 버전 일치 확인
        if manifest.version != policy.latest_version:
            raise PipelineExecutionError(
                f"Version mismatch: policy requests {policy.latest_version}, but manifest contains {manifest.version}"
            )

        # -------------------------------------------------------------------
        # Step 4: AtomicInstaller (사전 안전성 검사, 백업, 설치, 헬스체크)
        # -------------------------------------------------------------------
        try:
            self.installer.install(
                raw_package_bytes=acquired.package_bytes,
                new_version=manifest.version,
                skip_process_control=skip_process_control,
                debug_crash_hook=debug_crash_hook
            )
        except InstallerError as e:
            logger.critical("PIPELINE_INSTALL_FAILED: %s", e)
            raise PipelineExecutionError(f"Installation failed: {e}") from e

        logger.info("PIPELINE_SUCCESS: updated from %s to %s", current_version, manifest.version)
        return PipelineExecutionResult(
            success=True,
            decision=decision,
            installed_version=manifest.version,
            message=f"Successfully updated from {current_version} to {manifest.version}",
            details={
                "policy_sequence": policy.policy_sequence,
                "package_sha256": artifact_result.package_sha256
            }
        )
