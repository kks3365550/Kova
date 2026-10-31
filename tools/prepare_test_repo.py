# -*- coding: utf-8 -*-
"""
tools/prepare_test_repo.py - 가상머신 DR 복구 테스트용 백업 저장소 생성기
실제 SnapshotEngine을 사용하여 현실적인 테스트 파일들을 백업하고,
Ed25519 전자서명 및 SHA-256 지문이 포함된 무결성 100% 저장소를 생성합니다.
"""

import os
import sys
import shutil
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.snapshot import SnapshotEngine
from core.crypto_sign import Ed25519Signer


def create_test_repository(repo_path: str):
    print(f"[*] 테스트 백업 저장소 생성 중: {repo_path}")
    os.makedirs(repo_path, exist_ok=True)

    # 1. 테스트 소스 파일 생성
    temp_src = tempfile.mkdtemp(prefix="dr_source_")
    try:
        sample_files = {
            r"Windows\System32\drivers\etc\hosts": "# DR Test Hosts File\n127.0.0.1 localhost\n",
            r"Users\Administrator\Desktop\긴급복구_검증보고서.txt": "긴급 재해 복구(DR) 부팅 USB 자동 복원 테스트 완료 파일입니다.\n시스템이 성공적으로 복구되었습니다.",
            r"Users\Administrator\Documents\시스템_설정.json": '{"version": "2.17.37", "environment": "Hyper-V Gen2 VM", "status": "RECOVERED"}',
            r"Program Files\BackupSystem\config.ini": "[General]\nAutoBackup=True\nDR_Mode=Verified\n",
            r"Database\company_records.dat": "RECORDS_HEADER_DATA_100_PERCENT_INTEGRITY\n" * 100,
        }

        for rel, content in sample_files.items():
            full = os.path.join(temp_src, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

        # 2. SnapshotEngine을 통한 실제 스냅샷 생성
        snap = SnapshotEngine.create_snapshot(
            repo_dir=repo_path,
            sources=[temp_src],
            profile_id="vm_disaster_recovery_test",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        snap_id = snap.get("id")
        entry_count = len(snap.get("entries", []))
        print(f"[OK] 테스트 스냅샷 생성 완료! ID: {snap_id} (파일: {entry_count}개)")

        # 3. Ed25519 전자서명 날인 확인
        signer = Ed25519Signer(repo_path)
        valid = signer.verify_manifest(snap)
        print(f"[*] Ed25519 전자서명 상태: {'검증 성공' if valid else '검증 실패'}")
        return True
    finally:
        shutil.rmtree(temp_src, ignore_errors=True)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else r"C:\HyperV_USB_Test\TestRepo\MyBackup_Repository"
    create_test_repository(target)
