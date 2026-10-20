import os
import sys
import time
import shutil
import stat
import hashlib
from typing import Dict, List, Any

# Ensure UTF-8 stdout/stderr on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.storage import BlobStorage, lock_file_immutable, unlock_file_writable
from core.hasher import calculate_sha256

def safe_rmtree(path: str):
    """Safely removes directory tree unlocking any read-only WORM files."""
    if not os.path.exists(path):
        return
    def _handle_readonly(func, p, excinfo):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_handle_readonly)

def run_disaster_recovery_simulation():
    print("=" * 70)
    print(" [*] [재해 복구 전주기 시뮬레이션] Disaster Recovery Simulation Suite")
    print("=" * 70)

    sandbox = os.path.join(BASE_DIR, "recovery_simulation_sandbox")
    safe_rmtree(sandbox)
    os.makedirs(sandbox, exist_ok=True)

    src_dir = os.path.join(sandbox, "primary_server")
    repo_dir = os.path.join(sandbox, "backup_vault")
    os.makedirs(src_dir, exist_ok=True)

    try:
        # -------------------------------------------------------------
        # [준비] 원본 운영 서버 데이터셋 구성
        # -------------------------------------------------------------
        print("\n[Step 0] 원본 시스템 데이터셋 생성 (설정, DB, 소스코드, 미디어)...")
        os.makedirs(os.path.join(src_dir, "configs"), exist_ok=True)
        os.makedirs(os.path.join(src_dir, "database"), exist_ok=True)
        os.makedirs(os.path.join(src_dir, "web", "assets"), exist_ok=True)

        conf_v1 = "SERVER_HOST=0.0.0.0\nSERVER_PORT=8080\nLOG_LEVEL=DEBUG\n"
        db_v1 = "CREATE TABLE customers (id INT PRIMARY KEY, name TEXT);\nINSERT INTO customers VALUES (1, 'Acme Corp');\n"
        code_v1 = "def start_service():\n    print('Production Server Running 2026')\n"
        media_v1 = b"\x89PNG\r\n\x1a\n" + (b"\xaa\xbb\xcc\xdd" * 2048)  # 8KB binary

        with open(os.path.join(src_dir, "configs", "server.env"), "w", encoding="utf-8") as f:
            f.write(conf_v1)
        with open(os.path.join(src_dir, "database", "schema.sql"), "w", encoding="utf-8") as f:
            f.write(db_v1)
        with open(os.path.join(src_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write(code_v1)
        with open(os.path.join(src_dir, "web", "assets", "logo.png"), "wb") as f:
            f.write(media_v1)

        # Baseline Snapshot 1 생성
        snap1 = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[src_dir],
            profile_id="prod_server",
            profile_name="Production Server"
        )
        print(f" -> Baseline 스냅샷 생성 완료 (ID: {snap1['id']}, 파일: {snap1['summary']['total_files']}개)")

        # -------------------------------------------------------------
        # [시나리오 1] 재해 & 랜섬웨어 감염 상황 전체 복원 (Disaster Recovery)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 1] 랜섬웨어 감염/데이터 파괴 재해 복구 (Disaster Recovery)")
        print("-" * 60)
        print(" -> [재해 주입] 원본 파일 랜섬웨어 감염 및 암호화 변조 시뮬레이션...")
        # 원본 파일들을 .encrypted 로 변조/파괴
        with open(os.path.join(src_dir, "configs", "server.env"), "w", encoding="utf-8") as f:
            f.write("=== YOUR FILES ARE ENCRYPTED BY RANSOMWARE ===")
        with open(os.path.join(src_dir, "database", "schema.sql"), "w", encoding="utf-8") as f:
            f.write("LOCKED_PAY_1_BITCOIN_TO_RECOVER")
        os.remove(os.path.join(src_dir, "app.py"))
        with open(os.path.join(src_dir, "app.py.locked"), "w", encoding="utf-8") as f:
            f.write("LOCKED")

        print(" -> [복구 실행] 최신 백업 스냅샷으로부터 원위치 재해 복원 시작...")
        t0 = time.time()
        res_dr = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=snap1["id"],
            target_dir=src_dir,
            overwrite=True,
            verify_hash=True
        )
        t_dr = time.time() - t0
        print(f" -> 복원 완료 소요시간: {round(t_dr, 3)}초 (복원된 파일: {res_dr['restored_files']}개, 실패: {len(res_dr['failed_files'])}개)")

        # 무결성 검증
        with open(os.path.join(src_dir, "configs", "server.env"), "r", encoding="utf-8") as f:
            assert f.read() == conf_v1, "server.env 복원 데이터 불일치!"
        with open(os.path.join(src_dir, "database", "schema.sql"), "r", encoding="utf-8") as f:
            assert f.read() == db_v1, "schema.sql 복원 데이터 불일치!"
        with open(os.path.join(src_dir, "app.py"), "r", encoding="utf-8") as f:
            assert f.read() == code_v1, "app.py 복원 데이터 불일치!"
        with open(os.path.join(src_dir, "web", "assets", "logo.png"), "rb") as f:
            assert f.read() == media_v1, "logo.png 복원 바이너리 불일치!"
        print(" [OK] [시나리오 1 성공] 랜섬웨어로 파괴된 원본이 100% 비트 단위로 복구되었습니다!")

        # -------------------------------------------------------------
        # [시나리오 2] 특정 파일/폴더 정밀 부분 복구 (Selective Granular Restore)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 2] 특정 파일/폴더 정밀 부분 복구 (Selective Restore)")
        print("-" * 60)
        # 실수로 database 폴더만 삭제됨
        shutil.rmtree(os.path.join(src_dir, "database"))
        assert not os.path.exists(os.path.join(src_dir, "database", "schema.sql"))
        print(" -> [상황 주입] 운영 중 실수로 'database/' 폴더가 완전히 삭제됨")

        # 다른 파일은 건드리지 않고 'database' 폴더만 정밀 복원
        print(" -> [복구 실행] selected_rel_paths=['database'] 로 부분 복원 요청...")
        res_sel = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=snap1["id"],
            target_dir=src_dir,
            selected_rel_paths=["database"],
            overwrite=True
        )
        print(f" -> 정밀 복원 결과: {res_sel['restored_files']}개 파일 복원 (전체 {res_sel['total_files']}개 중)")
        assert res_sel["restored_files"] == 1
        assert os.path.exists(os.path.join(src_dir, "database", "schema.sql"))
        with open(os.path.join(src_dir, "database", "schema.sql"), "r", encoding="utf-8") as f:
            assert f.read() == db_v1
        print(" [OK] [시나리오 2 성공] 다른 파일 영향 없이 타겟 폴더/파일만 초정밀 복구되었습니다!")

        # -------------------------------------------------------------
        # [시나리오 3] 읽기 전용/잠긴 파일 덮어쓰기 복원 (Read-only Overwrite)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 3] 읽기 전용(Read-only) 파일 덮어쓰기 & 스킵 복구")
        print("-" * 60)
        # 복원 대상 위치의 파일에 읽기 전용 속성(WORM/Locked) 부여
        server_env_path = os.path.join(src_dir, "configs", "server.env")
        with open(server_env_path, "w", encoding="utf-8") as f:
            f.write("CORRUPTED_READONLY_DATA")
        os.chmod(server_env_path, stat.S_IREAD)
        print(" -> 대상 파일에 읽기 전용(0444) 속성 강제 설정 완료")

        # 1) overwrite=False 일 때 스킵 확인
        res_skip = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=snap1["id"],
            target_dir=src_dir,
            selected_rel_paths=["configs/server.env"],
            overwrite=False
        )
        assert res_skip["skipped_files"] == 1
        print(" -> overwrite=False 옵션 시 기존 파일 손상 없이 안전하게 스킵 확인!")

        # 2) overwrite=True 일 때 읽기 전용 속성을 해제하고 원본 덮어쓰는지 확인
        res_ovw = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=snap1["id"],
            target_dir=src_dir,
            selected_rel_paths=["configs/server.env"],
            overwrite=True
        )
        assert res_ovw["restored_files"] == 1
        with open(server_env_path, "r", encoding="utf-8") as f:
            assert f.read() == conf_v1
        print(" [OK] [시나리오 3 성공] 읽기 전용 잠금 파일도 권한 정상 제어로 완벽 덮어쓰기 복구되었습니다!")

        # -------------------------------------------------------------
        # [시나리오 4] 시점별 타임머신 롤백 (Historical Point-in-Time Rollback)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 4] 시점별 타임머신 롤백 (V1 vs V2 Point-in-Time)")
        print("-" * 60)
        time.sleep(1.0)
        # V2 업데이트
        conf_v2 = "SERVER_HOST=10.0.0.1\nSERVER_PORT=9000\nLOG_LEVEL=INFO\nMAX_CONNECTIONS=1000\n"
        with open(os.path.join(src_dir, "configs", "server.env"), "w", encoding="utf-8") as f:
            f.write(conf_v2)
        with open(os.path.join(src_dir, "new_feature.py"), "w", encoding="utf-8") as f:
            f.write("# New feature introduced in V2\n")

        snap2 = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[src_dir],
            profile_id="prod_server",
            profile_name="Production Server"
        )
        print(f" -> V2 증분 스냅샷 생성 완료 (ID: {snap2['id']})")

        # 별도 독립 복구 디렉토리에 각각 V1, V2 복원
        target_v1 = os.path.join(sandbox, "restored_time_machine_v1")
        target_v2 = os.path.join(sandbox, "restored_time_machine_v2")

        RestoreEngine.restore_snapshot(repo_dir, snap1["id"], target_v1)
        RestoreEngine.restore_snapshot(repo_dir, snap2["id"], target_v2)

        # V1 검증: new_feature.py가 없어야 하고, server.env는 v1이어야 함
        with open(os.path.join(target_v1, "configs", "server.env"), "r", encoding="utf-8") as f:
            assert f.read() == conf_v1
        assert not os.path.exists(os.path.join(target_v1, "new_feature.py"))

        # V2 검증: new_feature.py가 존재해야 하고, server.env는 v2여야 함
        with open(os.path.join(target_v2, "configs", "server.env"), "r", encoding="utf-8") as f:
            assert f.read() == conf_v2
        assert os.path.exists(os.path.join(target_v2, "new_feature.py"))
        print(" [OK] [시나리오 4 성공] V1과 V2 각 시점의 시스템 상태로 오차 없이 롤백되었습니다!")

        # -------------------------------------------------------------
        # [시나리오 5] 데이터 손상(Bit Rot) 감지 및 비정상 복구 사전 차단
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 5] 데이터 손상(Bit-Rot) 감지 및 안전 차단 (Integrity Guard)")
        print("-" * 60)
        # 1) 정상 무결성 검증 확인
        check_before = RestoreEngine.verify_snapshot_integrity(repo_dir, snap1["id"])
        assert check_before["is_valid"]
        print(" -> 변조 전: 스냅샷 무결성 100% 정상 판정 확인")

        # 2) 저장소 내 블롭 1개를 찾아 쓰기 권한을 잠시 풀고 바이트 변조(Bit Rot) 주입
        storage = BlobStorage(repo_dir)
        snap1_data = SnapshotEngine.get_snapshot(repo_dir, snap1["id"])
        target_blob_id = snap1_data["entries"][0]["blob_id"]
        target_blob_file = storage.get_blob_abs_path(target_blob_id)

        unlock_file_writable(target_blob_file)
        with open(target_blob_file, "r+b") as fb:
            fb.seek(10)
            orig_byte = fb.read(1)
            fb.seek(10)
            fb.write(bytes([orig_byte[0] ^ 0xFF]))  # 1바이트 비트 반전 주입!
        lock_file_immutable(target_blob_file)
        print(f" -> [손상 주입] 블롭 '{target_blob_id[:12]}...' 1바이트 비트 반전 변조 완료")

        # 3) 무결성 검증 엔진이 손상을 정확히 탐지하는지 확인
        check_after = RestoreEngine.verify_snapshot_integrity(repo_dir, snap1["id"])
        assert not check_after["is_valid"], "손상된 블롭을 감지하지 못했습니다!"
        assert len(check_after["corrupted_blobs"]) > 0
        print(f" -> 무결성 엔진 감지 성공: {len(check_after['corrupted_blobs'])}개 손상 블롭 정확히 식별!")

        # 4) 복원 시 verify_hash=True에 의해 손상 데이터 배포가 차단되는지 확인
        corrupt_target = os.path.join(sandbox, "restored_corrupt_test")
        res_corrupt = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=snap1["id"],
            target_dir=corrupt_target,
            verify_hash=True
        )
        assert len(res_corrupt["failed_files"]) > 0, "손상 파일이 차단되지 않고 복원되었습니다!"
        print(f" -> 손상 데이터 복원 안전 차단 성공 (실패로 안전 격리: {res_corrupt['failed_files'][0]['error']})")
        print(" [OK] [시나리오 5 성공] Bit-rot 변조 파일은 시스템에 풀리지 않고 원천 차단됩니다!")

        # -------------------------------------------------------------
        # [시나리오 6] 경로 탈출(Path Traversal) 공격 차단 (Sandbox Security)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 6] 악의적 상대경로 탈출(Path Traversal) 공격 차단")
        print("-" * 60)
        # 조작된 스냅샷 매니페스트 생성 (상위 폴더 탈출 시도)
        evil_snap_id = "snap_evil_traversal_test"
        evil_manifest = {
            "id": evil_snap_id,
            "created_at": time.time(),
            "profile_id": "test",
            "entries": [
                {
                    "rel_path": "../../evil_escaped_file.txt",
                    "blob_id": snap2["entries"][0]["blob_id"],
                    "size": 100,
                    "mtime": time.time()
                }
            ]
        }
        evil_manifest_path = os.path.join(storage.snapshots_dir, f"{evil_snap_id}.json")
        import json
        with open(evil_manifest_path, "w", encoding="utf-8") as f:
            json.dump(evil_manifest, f)
        lock_file_immutable(evil_manifest_path)

        isolated_target = os.path.join(sandbox, "isolated_target")
        os.makedirs(isolated_target, exist_ok=True)

        res_evil = RestoreEngine.restore_snapshot(repo_dir, evil_snap_id, isolated_target)
        assert len(res_evil["failed_files"]) == 1
        assert "경로 트래버설 차단" in res_evil["failed_files"][0]["error"]
        assert not os.path.exists(os.path.join(sandbox, "evil_escaped_file.txt"))
        print(" [OK] [시나리오 6 성공] 시스템 상위 디렉터리 탈출 시도가 100% 안전하게 차단되었습니다!")

        # -------------------------------------------------------------
        # [시나리오 7] 대량 멀티스레드 병렬 복구 성능 & 타임스탬프(mtime) 보존
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 7] 대량(150개) 파일 멀티스레드 고속 병렬 복원 & mtime 보존율")
        print("-" * 60)
        bulk_src = os.path.join(sandbox, "bulk_source")
        os.makedirs(bulk_src, exist_ok=True)
        sample_mtime = 1700000000.0  # 고정 과거 시점

        for i in range(150):
            fp = os.path.join(bulk_src, f"module_{i // 20}", f"file_{i}.dat")
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(f"Bulk Content Sample Index {i} - Timestamp Preservation Test\n".encode() * 20)
            os.utime(fp, (sample_mtime, sample_mtime))

        bulk_snap = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[bulk_src],
            profile_id="bulk_test",
            profile_name="Bulk Test"
        )
        print(f" -> 대량 150개 파일 백업 완료 (스냅샷 ID: {bulk_snap['id']})")

        bulk_target = os.path.join(sandbox, "bulk_restored")
        t_start = time.time()
        res_bulk = RestoreEngine.restore_snapshot(repo_dir, bulk_snap["id"], bulk_target)
        t_elapsed = time.time() - t_start

        rate = round(res_bulk["restored_files"] / max(0.001, t_elapsed), 1)
        print(f" -> 150개 파일 복원 완료: {round(t_elapsed, 3)}초 소요 (초당 {rate}개 파일 초고속 복구)")
        assert res_bulk["restored_files"] == 150
        assert len(res_bulk["failed_files"]) == 0

        # mtime 검증
        sample_check_fp = os.path.join(bulk_target, "module_0", "file_0.dat")
        restored_mtime = os.path.getmtime(sample_check_fp)
        assert abs(restored_mtime - sample_mtime) < 1.0, f"mtime 보존 실패 (원문: {sample_mtime}, 복원: {restored_mtime})"
        print(" [OK] [시나리오 7 성공] 150개 전 파일 병렬 고속 복구 및 파일 수정일시(mtime) 완벽 보존!")

        # -------------------------------------------------------------
        # [시나리오 8] 다중 소스 원위치 복원 (in_place=True Multi-Source In-Place Disaster Recovery)
        # -------------------------------------------------------------
        print("\n" + "-" * 60)
        print(" [시나리오 8] 다중 소스(C:, D: 등) 원위치 롤백 (In-Place Restore)")
        print("-" * 60)
        src_alpha = os.path.join(sandbox, "drive_c_data")
        src_beta = os.path.join(sandbox, "drive_d_projects")
        os.makedirs(src_alpha, exist_ok=True)
        os.makedirs(src_beta, exist_ok=True)

        alpha_content = "CRITICAL_DATABASE_ON_C_DRIVE\n"
        beta_content = "PROJECT_SOURCE_CODE_ON_D_DRIVE\n"

        with open(os.path.join(src_alpha, "db.sqlite"), "w", encoding="utf-8") as f:
            f.write(alpha_content)
        with open(os.path.join(src_beta, "main.py"), "w", encoding="utf-8") as f:
            f.write(beta_content)

        multi_snap = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[src_alpha, src_beta],
            profile_id="multi_source_test",
            profile_name="Multi Source Test"
        )
        print(f" -> 2개 소스 폴더 백업 완료 (스냅샷 ID: {multi_snap['id']})")

        # 재해 시뮬레이션: 두 폴더의 파일들이 모두 랜섬웨어로 파괴됨
        with open(os.path.join(src_alpha, "db.sqlite"), "w", encoding="utf-8") as f:
            f.write("ENCRYPTED_C")
        with open(os.path.join(src_beta, "main.py"), "w", encoding="utf-8") as f:
            f.write("ENCRYPTED_D")

        # in_place=True 로 원위치 롤백 복원 실행
        print(" -> [복구 실행] in_place=True 옵션으로 원위치 자동 분배 복원 실행...")
        res_inplace = RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=multi_snap["id"],
            in_place=True,
            overwrite=True
        )
        print(f" -> 원위치 복원 완료: {res_inplace['restored_files']}개 파일 복구 (소요 {res_inplace['duration_seconds']}초)")
        assert res_inplace["restored_files"] == 2
        assert len(res_inplace["failed_files"]) == 0

        # 각각 원래의 폴더로 정확히 복구되었는지 검증
        with open(os.path.join(src_alpha, "db.sqlite"), "r", encoding="utf-8") as f:
            assert f.read() == alpha_content, "src_alpha 복원 실패!"
        with open(os.path.join(src_beta, "main.py"), "r", encoding="utf-8") as f:
            assert f.read() == beta_content, "src_beta 복원 실패!"
        print(" [OK] [시나리오 8 성공] 서로 다른 드라이브/폴더의 파일들이 각자의 원래 위치로 100% 자동 분배 복구되었습니다!")

        print("\n" + "=" * 70)
        print(" [PASS] [최종 결과] 8개 재해 복구 시뮬레이션 시나리오 전체 100% 통과!")
        print("=" * 70 + "\n")

    finally:
        safe_rmtree(sandbox)

if __name__ == "__main__":
    run_disaster_recovery_simulation()

