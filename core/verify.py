# -*- coding: utf-8 -*-
"""
core/verify.py: 백업 무결성 자동 검증 및 복원 시뮬레이션 엔진 (v2.3.9)
- zstandard / zlib 압축 블롭 스트림 및 해시 무결성 검증
- Automated Restore Verification: 계층화 샘플링(소형/대형/유니코드/신규) 기반 엔드투엔드 실제 복원 대조
- Merkle/Hash 기반 Manifest SHA-256 서명 생성 및 위변조 검증
"""

import os
import json
import time
import hashlib
import random
import logging
from typing import Dict, Any, List, Optional, Tuple
from core.storage import BlobStorage, HAS_ZSTD

logger = logging.getLogger("core.verify")

# zstd 는 하위 모듈이 on-demand 로 참조하는 가용성 신호다.
# pyflakes 는 사용으로 보지 못하므로 유지한다.
if HAS_ZSTD:
    import zstandard as zstd
else:
    zstd = None

_HAS_ZSTD_MODULE = zstd  # 가용성 플래그로 외부에서 참조


class RestoreVerificationError(Exception):
    """자동 복원 검증 실패 시 발생하는 커스텀 예외 (Crash-Consistent / Data Integrity 위반)."""
    pass


def generate_manifest_signature(entries: List[Dict[str, Any]]) -> str:
    """
    전체 파일 엔트리의 sha256들을 정렬 결합하여 최종 SHA-256 서명 해시를 생성.
    스냅샷 메타데이터(manifest.json)의 위변조나 잘림(Truncation)을 즉각 탐지.
    """
    if not entries:
        return hashlib.sha256(b"").hexdigest()

    hashes = []
    for entry in entries:
        h = entry.get("sha256") or entry.get("blob_id")
        if h:
            hashes.append(h.lower())

    hashes.sort()
    combined = "\n".join(hashes).encode('utf-8')
    return hashlib.sha256(combined).hexdigest()


def verify_manifest_signature(manifest: Dict[str, Any]) -> bool:
    """manifest의 manifest_signature와 엔트리들로부터 재계산한 서명이 일치하는지 검증."""
    stored_sig = manifest.get("manifest_signature")
    if not stored_sig:
        return False

    entries = manifest.get("entries", [])
    calculated_sig = generate_manifest_signature(entries)
    return stored_sig.lower() == calculated_sig.lower()


class IntegrityVerifier:
    """스냅샷 및 블롭 저장소의 물리적 무결성을 검증하는 엔진"""

    def __init__(self, repo_dir: str, crypto_engine: Optional[Any] = None):
        self.repo_dir = os.path.abspath(repo_dir)
        self.crypto_engine = crypto_engine
        self.storage = BlobStorage(self.repo_dir, crypto_engine=crypto_engine)

    def verify_blob(self, sha256_hash: str) -> Tuple[bool, Optional[str]]:
        """
        단일 블롭의 압축 해제 및 SHA-256 일치 여부를 검증 (저장소 내부 verify_blob 위임).
        반환: (성공 여부, 오류 메시지)
        """
        ok, err, _ = self.storage.verify_blob(sha256_hash)
        return ok, err


    def verify_snapshot(
        self,
        snapshot_manifest: Dict[str, Any],
        sample_ratio: float = 0.1,
        max_samples: int = 100,
        verify_all_new: bool = True
    ) -> Dict[str, Any]:
        """
        스냅샷 내 블롭들의 물리적 무결성 검증.
        - verify_all_new=True: 이번 백업에서 신규/수정 생성된 블롭은 100% 전수 검증
        - 기존 블롭은 sample_ratio 무작위 표본 검증
        """
        entries = snapshot_manifest.get("entries", [])
        if not entries:
            return {
                "success": True,
                "verified_count": 0,
                "error_count": 0,
                "errors": [],
                "detail": "검증할 파일 항목 없음"
            }

        new_hashes = set()
        existing_hashes = set()

        for entry in entries:
            h = entry.get("blob_id") or entry.get("sha256")
            if not h:
                continue
            status = entry.get("status", "")
            if status in ("new", "modified"):
                new_hashes.add(h)
            else:
                existing_hashes.add(h)

        targets_to_verify = set()
        if verify_all_new:
            targets_to_verify.update(new_hashes)

        if existing_hashes:
            sample_size = min(len(existing_hashes), max(10, int(len(existing_hashes) * sample_ratio)))
            sample_size = min(sample_size, max_samples)
            sampled = random.sample(list(existing_hashes), sample_size)
            targets_to_verify.update(sampled)

        verified_count = 0
        errors = []

        for blob_hash in targets_to_verify:
            ok, err = self.verify_blob(blob_hash)
            if ok:
                verified_count += 1
            else:
                errors.append({"blob_id": blob_hash, "error": err})

        success = (len(errors) == 0)
        return {
            "success": success,
            "total_candidates": len(targets_to_verify),
            "verified_count": verified_count,
            "error_count": len(errors),
            "errors": errors,
            "new_verified": len(new_hashes),
            "sample_verified": len(targets_to_verify) - len(new_hashes)
        }

    def _decompress_and_hash_blob(self, blob_path: str) -> Tuple[int, str]:
        """블롭 파일을 스트리밍 압축 해제하며 크기와 SHA-256을 실시간 계산 (저장소 위임)."""
        fname = os.path.basename(blob_path)
        blob_id = fname[:-5].lower() if fname.endswith(".blob") else fname.lower()
        ok, err_msg, total_size = self.storage.verify_blob(blob_id)
        if not ok:
            raise RuntimeError(err_msg or f"블롭 검증 실패: {blob_id}")
        return total_size, blob_id


    def _has_special_chars(self, path: str) -> bool:
        """경로에 한글, 공백, 유니코드 문자가 포함되어 있는지 검사."""
        if not path:
            return False
        if ' ' in path or '\t' in path:
            return True
        for ch in path:
            if ord(ch) > 127:
                return True
        return False

    def verify_restore_sampling(
        self,
        snapshot_manifest: Dict[str, Any],
        sample_count: int = 20,
        self_heal: bool = True
    ) -> Dict[str, Any]:
        """
        🔴 Automated Restore Verification (자동 복원 검증):
        - 계층화 샘플링:
          * 소형 파일 (< 100KB) 최대 5개
          * 대형 파일 (>= 5MB) 최대 5개
          * 한글/공백/유니코드 경로 파일 최대 5개
          * 신규/수정된 파일 최대 5개
          * 부족분은 전체 항목에서 무작위 표본 보충 (최대 sample_count개)
        - 각 샘플 파일의 블롭을 실제로 스트리밍 디컴프레스하여 크기와 SHA-256을 바이트 단위로 전수 대조.
        - 단 1개 파일이라도 불일치/누락 시 RestoreVerificationError 발생 (Fail-Closed).
        """
        entries = snapshot_manifest.get("entries", [])
        if not entries:
            return {
                "status": "passed",
                "samples_verified": 0,
                "total_verified_bytes": 0,
                "sample_paths": []
            }

        small_files = []
        large_files = []
        special_path_files = []
        new_modified_files = []

        SMALL_THRESHOLD = 100 * 1024       # 100KB
        LARGE_THRESHOLD = 5 * 1024 * 1024  # 5MB

        for entry in entries:
            size = entry.get("size", 0)
            rel_path = entry.get("rel_path", "")
            status = entry.get("status", "")

            if size < SMALL_THRESHOLD:
                small_files.append(entry)
            elif size >= LARGE_THRESHOLD:
                large_files.append(entry)

            if self._has_special_chars(rel_path):
                special_path_files.append(entry)

            if status in ("new", "modified"):
                new_modified_files.append(entry)

        selected_entries = []
        selected_keys = set()

        def _add_sample(pool: List[Dict[str, Any]], max_count: int):
            if not pool:
                return
            actual_count = min(len(pool), max_count)
            sampled = random.sample(pool, actual_count)
            for entry in sampled:
                key = (entry.get("rel_path", ""), entry.get("blob_id") or entry.get("sha256", ""))
                if key not in selected_keys:
                    selected_keys.add(key)
                    selected_entries.append(entry)

        _add_sample(small_files, 5)
        _add_sample(large_files, 5)
        _add_sample(special_path_files, 5)
        _add_sample(new_modified_files, 5)

        # 목표 수량 부족 시 전체 항목에서 보충
        if len(selected_entries) < sample_count:
            remaining_needed = sample_count - len(selected_entries)
            unselected = [
                e for e in entries
                if (e.get("rel_path", ""), e.get("blob_id") or e.get("sha256", "")) not in selected_keys
            ]
            if unselected:
                actual_fill = min(len(unselected), remaining_needed)
                filled = random.sample(unselected, actual_fill)
                for entry in filled:
                    key = (entry.get("rel_path", ""), entry.get("blob_id") or entry.get("sha256", ""))
                    if key not in selected_keys:
                        selected_keys.add(key)
                        selected_entries.append(entry)

        total_verified_bytes = 0
        healed_count = 0

        for entry in selected_entries:
            rel_path = entry.get("rel_path", "unknown")
            blob_id = entry.get("blob_id") or entry.get("sha256")
            expected_size = entry.get("size", -1)
            expected_sha256 = entry.get("sha256")

            if not blob_id:
                raise RestoreVerificationError(
                    f"자동 복원 검증 실패: {rel_path} (블롭 ID 또는 SHA-256 누락)"
                )

            corrupted = False
            corruption_reason = ""
            actual_size = 0

            chunk_ids = entry.get("chunk_ids")
            if chunk_ids and isinstance(chunk_ids, list):

                # Multi-chunk verification: verify all chunks exist and are valid
                for cid in chunk_ids:
                    if not self.storage.has_blob(cid):
                        raise RestoreVerificationError(
                            f"자동 복원 검증 실패: {rel_path} (청크 누락: {cid})"
                        )
                    c_ok, c_err, c_sz = self.storage.verify_blob(cid)
                    if not c_ok:
                        corrupted = True
                        corruption_reason = f"청크 손상 ({cid}): {c_err}"
                        break
                    actual_size += c_sz

                if not corrupted and expected_size >= 0 and actual_size != expected_size:
                    corrupted = True
                    corruption_reason = f"다중 청크 합산 크기 불일치 (기대값: {expected_size}, 복원값: {actual_size})"
            else:
                if not self.storage.has_blob(blob_id):
                    raise RestoreVerificationError(
                        f"자동 복원 검증 실패: {rel_path} (저장소 내 블롭 파일 누락: {blob_id})"
                    )

                # 저장소 논리 API(verify_blob)로 검증 수행
                ok, err_msg, actual_size = self.storage.verify_blob(blob_id)
                if not ok:
                    corrupted = True
                    corruption_reason = err_msg or "블롭 무결성 검증 실패"
                elif expected_size >= 0 and actual_size != expected_size:
                    corrupted = True
                    corruption_reason = f"크기 불일치 (기대값: {expected_size}, 복원값: {actual_size})"


            if corrupted:
                healed = False
                # 🔴 자가 치유(Self-Healing): self_heal=True이고 원본 소스 파일이 존재하는 경우 정상 블롭 재생성
                if self_heal:
                    source_root = entry.get("source_root")
                    if source_root and os.path.isdir(source_root) and expected_sha256:
                        candidate_src = os.path.join(source_root, rel_path)
                        if os.path.isfile(candidate_src) and os.access(candidate_src, os.R_OK):
                            try:
                                # 원본 파일 무결성 우선 확인
                                with open(candidate_src, "rb") as f_src:
                                    src_bytes = f_src.read()
                                if hashlib.sha256(src_bytes).hexdigest().lower() == expected_sha256.lower():
                                    self.storage.delete_blob(blob_id)
                                    self.storage.put_file_blob_onepass(candidate_src)
                                    # 재검증
                                    ok_re, _, re_size = self.storage.verify_blob(blob_id)
                                    if ok_re:
                                        healed = True
                                        actual_size = re_size
                                        logger.info("손상 블롭 자가 치유(Self-Healing) 성공: %s", blob_id)
                                    else:
                                        logger.warning("손상 블롭 자가 치유 실패 (재검증 불합격): %s", blob_id)
                                else:
                                    logger.warning("자가 치유 건너뜀 (원본 파일 해시 불일치): %s", candidate_src)
                            except Exception as heal_err:
                                logger.error("자가 치유 중 예외 발생 (블롭 ID: %s): %s", blob_id, heal_err)


                if healed:
                    healed_count += 1
                    total_verified_bytes += actual_size
                    continue

                raise RestoreVerificationError(
                    f"자동 복원 검증 실패: {rel_path} ({corruption_reason})"
                )

            total_verified_bytes += actual_size

        return {
            "status": "passed",
            "samples_verified": len(selected_entries),
            "healed_count": healed_count,
            "total_verified_bytes": total_verified_bytes,
            "sample_paths": [e.get("rel_path") for e in selected_entries]
        }

    def audit_entire_repository(self, progress_callback=None) -> Dict[str, Any]:
        """
        저장소 전체에 대한 심층 무결성 전수 검증 (Deep Scan & Bit Rot Audit).
        1. 모든 스냅샷 매니페스트의 JSON 유효성, Ed25519 서명, SHA-256 지문 및 누락 블롭(Missing) 전수 검사.
        2. 모든 물리적 블롭 파일의 압축 해제 스트림 및 언팩 SHA-256 해시 대조 (Bit Rot / 물리적 비트 손상 탐지).
        3. 어떤 매니페스트에도 참조되지 않는 고아 블롭(Orphaned Blobs) 집계.
        """
        start_time = time.time()
        snapshots_dir = os.path.join(self.repo_dir, "snapshots")
        blobs_dir = os.path.join(self.repo_dir, "blobs")

        snapshot_files = []
        if os.path.exists(snapshots_dir):
            for fname in os.listdir(snapshots_dir):
                if fname.endswith(".json"):
                    snapshot_files.append(os.path.join(snapshots_dir, fname))

        blob_files = []
        if os.path.exists(blobs_dir):
            for root, _, files in os.walk(blobs_dir):
                for f in files:
                    if f.endswith(".blob"):
                        blob_files.append(os.path.join(root, f))

        total_tasks = len(snapshot_files) + len(blob_files)
        current_task = 0

        def _report(msg: str):
            if progress_callback:
                progress_callback({
                    "type": "audit_progress",
                    "current": current_task,
                    "total": total_tasks,
                    "percent": round((current_task / max(1, total_tasks)) * 100, 1),
                    "message": msg
                })

        results = {
            "status": "healthy",
            "scanned_snapshots": len(snapshot_files),
            "valid_snapshots": 0,
            "corrupted_snapshots": [],
            "total_blobs": len(blob_files),
            "valid_blobs": 0,
            "corrupted_blobs": [],
            "missing_blobs": [],
            "orphaned_blobs": [],
            "total_scanned_bytes": 0,
            "duration_seconds": 0.0
        }

        referenced_blob_ids = set()
        missing_blob_set = set()

        # Phase 1: 스냅샷 매니페스트 검증
        from core.crypto_sign import Ed25519Signer
        signer = Ed25519Signer(self.repo_dir)

        for snap_path in snapshot_files:
            current_task += 1
            snap_name = os.path.basename(snap_path)
            if current_task % 50 == 0 or current_task == total_tasks:
                _report(f"스냅샷 검증 중: {snap_name}")

            try:
                with open(snap_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)

                # 1. Ed25519 비대칭키 전자서명 검증 (서명이 존재할 경우)
                if manifest.get("ed25519_signature"):
                    if not signer.verify_manifest(manifest):
                        results["corrupted_snapshots"].append({
                            "snapshot": snap_name,
                            "error": "Ed25519 비대칭키 디지털 전자서명 위변조 감지"
                        })
                        continue

                # 2. Manifest SHA-256 서명 지문 검증
                if not verify_manifest_signature(manifest):
                    results["corrupted_snapshots"].append({
                        "snapshot": snap_name,
                        "error": "Manifest SHA-256 결합 지문 불일치 (엔트리 변조)"
                    })
                    continue

                # 3. 매니페스트 내 참조 블롭 수집 및 누락 여부 확인
                for entry in manifest.get("entries", []):
                    chunk_ids = entry.get("chunk_ids")
                    if chunk_ids and isinstance(chunk_ids, list):
                        for cid in chunk_ids:
                            cid_lower = cid.lower()
                            referenced_blob_ids.add(cid_lower)
                            if not self.storage.has_blob(cid_lower):
                                if cid_lower not in missing_blob_set:
                                    missing_blob_set.add(cid_lower)
                                    results["missing_blobs"].append(cid_lower)
                    else:
                        b_id = entry.get("blob_id") or entry.get("sha256")
                        if b_id:
                            b_id_lower = b_id.lower()
                            referenced_blob_ids.add(b_id_lower)
                            if not self.storage.has_blob(b_id_lower):
                                if b_id_lower not in missing_blob_set:
                                    missing_blob_set.add(b_id_lower)
                                    results["missing_blobs"].append(b_id_lower)

                results["valid_snapshots"] += 1


            except Exception as e:
                results["corrupted_snapshots"].append({
                    "snapshot": snap_name,
                    "error": f"매니페스트 파싱/검증 오류: {str(e)}"
                })

        # Phase 2: 블롭 파일 전수 바이트 검증 (Bit Rot 탐지)
        for blob_path in blob_files:
            current_task += 1
            fname = os.path.basename(blob_path)
            blob_id = fname[:-5].lower() if fname.endswith(".blob") else fname.lower()

            if current_task % 100 == 0 or current_task == total_tasks:
                _report(f"블롭 바이트 검증 중: {fname}")

            ok, err_msg, actual_size = self.storage.verify_blob(blob_id)
            if not ok:
                results["corrupted_blobs"].append({
                    "blob_id": blob_id,
                    "error": err_msg or "압축 스트림 손상"
                })
            else:
                results["valid_blobs"] += 1
                results["total_scanned_bytes"] += actual_size

            if blob_id not in referenced_blob_ids:
                results["orphaned_blobs"].append(blob_id)


        results["duration_seconds"] = round(time.time() - start_time, 2)

        if results["corrupted_snapshots"] or results["corrupted_blobs"]:
            results["status"] = "corrupted"
        elif results["missing_blobs"]:
            results["status"] = "warning"
        else:
            results["status"] = "healthy"

        _report(f"감사 완료 (상태: {results['status']})")
        return results

