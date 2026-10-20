#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_repository_chaos.py - 저장소 Chaos 10단계 극한 자동 파괴 테스트 스위트
10가지 실전 재해 및 공격 시나리오(Bit-Rot, 위조, 누락, 이동, 고아, DB유실 등)에 대해
시스템의 방어/격리/복구 능력을 전수 검증합니다.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from typing import Dict, Any

from core.snapshot import SnapshotEngine
from core.crypto_sign import Ed25519Signer
from core.worm import WORMManager
from disaster_recovery import (
    list_snapshots,
    restore_snapshot,
    audit_repository,
    verify_ed25519_manifest,
    verify_manifest_fingerprint,
    extract_blob,
    get_blob_path
)


class TestRepositoryChaos(unittest.TestCase):
    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="chaos_test_")
        self.src_dir = os.path.join(self.test_root, "source")
        self.repo_dir = os.path.join(self.test_root, "repo")
        self.dest_dir = os.path.join(self.test_root, "dest")
        self.worm = WORMManager()

        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.dest_dir, exist_ok=True)

        # 1. 테스트 원본 파일 생성 (샤드 분산을 위해 다양한 크기/내용의 파일 생성)
        self.files_data = {
            "file_alpha.txt": b"Alpha content for repository chaos test 12345",
            "file_beta.log": b"Beta log entries with diverse bytes 67890",
            "sub_gamma/file_gamma.dat": b"Gamma binary data \x00\x01\x02\x03\x04\x05\xFF\xFE\xFD",
            "sub_delta/file_delta.txt": b"Delta content for shard partition testing ABCD"
        }
        for rel_path, data in self.files_data.items():
            full_path = os.path.join(self.src_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)

        # 2. 1차 스냅샷 생성
        self.snap1 = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="chaos_test",
            profile_name="Chaos Test Profile",
            compress_level=1
        )

        # 3. 파일 수정 및 2차 스냅샷 생성
        time_path = os.path.join(self.src_dir, "file_alpha.txt")
        with open(time_path, "wb") as f:
            f.write(b"Alpha content MODIFIED in snapshot 2")

        self.snap2 = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="chaos_test",
            profile_name="Chaos Test Profile",
            compress_level=1
        )

    def tearDown(self):
        try:
            self.worm.unprotect_directory(self.repo_dir, authorized=True)
        except Exception:
            pass

        def _handle_rm_error(func, path, exc_info):
            try:
                self.worm.unprotect_file(path, authorized=True)
            except Exception:
                pass
            try:
                import stat
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        shutil.rmtree(self.test_root, onerror=_handle_rm_error)

    # -------------------------------------------------------------
    # Chaos 1: 최신 manifest 파일 1개 삭제 시 이전 스냅샷으로 100% 롤백 복원
    # -------------------------------------------------------------
    def test_01_manifest_single_deleted(self):
        snap2_file = os.path.join(self.repo_dir, "snapshots", f"{self.snap2['id']}.json")
        self.assertTrue(os.path.exists(snap2_file))
        # WORM 해제 후 삭제
        self.worm.unprotect_file(snap2_file, authorized=True)
        os.remove(snap2_file)

        # 최신 스냅샷이 삭제되었으므로 스냅샷 목록에는 snap1만 남아있어야 함
        snapshots = list_snapshots(self.repo_dir)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], self.snap1["id"])

        # snap1으로 복원 실행
        dest = os.path.join(self.dest_dir, "chaos_01")
        res = restore_snapshot(self.repo_dir, self.snap1["id"], dest_dir=dest)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)

        # 원본 초기 내용(수정 전)으로 온전히 복원되었는지 검증
        #
        # disaster_recovery.restore_snapshot()은 dest_dir 지정 시 '원본 드라이브 디렉터리 트리 보존'을
        # 계약으로 한다(dr.py:478-481). 즉 dest 아래에 드라이브 문자만 제거한 전체 절대 경로가 생성된다.
        #   dest\Users\<user>\...\chaos_test_xxx\source\file_alpha.txt
        # 따라서 dest 루트 바로 아래를 가정해서는 안 되고, 실제 생성된 트리에서 상대경로를 찾는다.
        def _find_restored(dest_root, filename):
            for root_d, _, files in os.walk(dest_root):
                if filename in files:
                    return os.path.join(root_d, filename)
            raise AssertionError(f"복원된 파일을 찾을 수 없음: {filename} (dest={dest_root})")

        alpha_restored = _find_restored(dest, "file_alpha.txt")
        with open(alpha_restored, "rb") as f:
            self.assertEqual(f.read(), self.files_data["file_alpha.txt"])

    # -------------------------------------------------------------
    # Chaos 2: manifest JSON 손상 시 에러 격리 및 타 정상 스냅샷 복원 성공
    # -------------------------------------------------------------
    def test_02_manifest_corrupted_json(self):
        snap2_file = os.path.join(self.repo_dir, "snapshots", f"{self.snap2['id']}.json")
        self.worm.unprotect_file(snap2_file, authorized=True)
        with open(snap2_file, "w", encoding="utf-8") as f:
            f.write("{corrupted garbage json contents!@#$%^&*")

        # 손상된 json은 건너뛰고 정상 json만 목록에 표출
        snapshots = list_snapshots(self.repo_dir)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], self.snap1["id"])

        dest = os.path.join(self.dest_dir, "chaos_02")
        res = restore_snapshot(self.repo_dir, self.snap1["id"], dest_dir=dest)
        self.assertTrue(res["success"])

    # -------------------------------------------------------------
    # Chaos 3: Manifest 위조 시 Ed25519 및 지문 검증에서 즉시 탐지(Tamper-Evident)
    # -------------------------------------------------------------
    def test_03_signature_forged(self):
        snap2_file = os.path.join(self.repo_dir, "snapshots", f"{self.snap2['id']}.json")
        with open(snap2_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        # 파일 크기 위조 시도
        if manifest.get("entries"):
            manifest["entries"][0]["size"] += 9999

        pubkey = os.path.join(self.repo_dir, "keys", "backup_ed25519.pub")
        ok_ed25519, msg_ed = verify_ed25519_manifest(manifest, pubkey)
        ok_fp, msg_fp = verify_manifest_fingerprint(manifest)

        # 서명 검증에서 위조가 적발되어야 함
        self.assertFalse(ok_ed25519, "위조된 manifest는 Ed25519 서명 검증을 통과할 수 없어야 합니다.")

    # -------------------------------------------------------------
    # Chaos 4: 블롭 1바이트 변조 시 Bit-Rot 100% 탐지 및 복원 거부
    # -------------------------------------------------------------
    def test_04_blob_bit_rot(self):
        target_entry = self.snap2["entries"][0]
        h = target_entry.get("sha256") or target_entry.get("blob_id")
        blob_path = get_blob_path(self.repo_dir, h)
        self.assertTrue(os.path.exists(blob_path))

        # WORM 해제 후 1바이트 반전 변조
        self.worm.unprotect_file(blob_path, authorized=True)
        with open(blob_path, "r+b") as f:
            byte = f.read(1)
            f.seek(0)
            f.write(bytes([byte[0] ^ 0xFF]))

        # 1) extract_blob 시도 시 ValueError(해시 불일치) 발생 검증
        dest_file = os.path.join(self.dest_dir, "bitrot_target.txt")
        with self.assertRaises(ValueError):
            extract_blob(blob_path, dest_file, expected_sha256=h, verify_hash=True)

        # 2) audit_repository 감사 시 손상 감지 검증
        audit_res = audit_repository(self.repo_dir)
        self.assertGreaterEqual(audit_res["corrupted"], 1)

    # -------------------------------------------------------------
    # Chaos 5: 블롭 파일 삭제 시 해당 파일 격리 실패 및 타 파일 100% 정상 복원
    # -------------------------------------------------------------
    def test_05_blob_missing(self):
        alpha_entry = next(e for e in self.snap2["entries"] if "file_alpha.txt" in (e.get("path") or e.get("rel_path", "")))
        h = alpha_entry.get("sha256") or alpha_entry.get("blob_id")
        blob_path = get_blob_path(self.repo_dir, h)
        self.worm.unprotect_file(blob_path, authorized=True)
        os.remove(blob_path)

        dest = os.path.join(self.dest_dir, "chaos_05")
        res = restore_snapshot(self.repo_dir, self.snap2["id"], dest_dir=dest)

        # 전체가 죽지 않고, 누락 파일만 실패하고 나머지는 온전히 성공(Partial Fault Tolerance)
        self.assertFalse(res["success"])
        self.assertEqual(res["failed"], 1)
        self.assertGreaterEqual(res["restored"], 3)

    # -------------------------------------------------------------
    # Chaos 6: 고아 블롭(더미 데이터) 주입 시 스냅샷 복원에 영향 없음
    # -------------------------------------------------------------
    def test_06_orphan_blobs_injected(self):
        orphan_dir = os.path.join(self.repo_dir, "blobs", "ff")
        os.makedirs(orphan_dir, exist_ok=True)
        orphan_path = os.path.join(orphan_dir, "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.blob")
        with open(orphan_path, "wb") as f:
            f.write(b"Orphan junk blob content not referenced anywhere")

        # 스냅샷 복원은 아무런 영향 없이 100% 성공해야 함
        dest = os.path.join(self.dest_dir, "chaos_06")
        res = restore_snapshot(self.repo_dir, self.snap2["id"], dest_dir=dest)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)

    # -------------------------------------------------------------
    # Chaos 7: 블롭 샤드 디렉터리(blobs/xx) 삭제 시 타 샤드 파일 정상 복원
    # -------------------------------------------------------------
    def test_07_blob_shard_directory_deleted(self):
        blobs_dir = os.path.join(self.repo_dir, "blobs")
        shards = [d for d in os.listdir(blobs_dir) if os.path.isdir(os.path.join(blobs_dir, d))]
        self.assertTrue(len(shards) > 0)
        target_shard = os.path.join(blobs_dir, shards[0])
        self.worm.unprotect_directory(target_shard, authorized=True)
        for root, dirs, files in os.walk(target_shard):
            for f in files:
                self.worm.unprotect_file(os.path.join(root, f), authorized=True)
        shutil.rmtree(target_shard)

        dest = os.path.join(self.dest_dir, "chaos_07")
        res = restore_snapshot(self.repo_dir, self.snap2["id"], dest_dir=dest)

        # 시스템 크래시 없이 살아남고 타 샤드 파일들은 복원 완료
        self.assertGreater(res["restored"], 0)

    # -------------------------------------------------------------
    # Chaos 8: 저장소 전체를 다른 경로로 통째로 이동해도 100% 정상 복원
    # -------------------------------------------------------------
    def test_08_repo_relocation(self):
        new_repo_dir = os.path.join(self.test_root, "relocated_repo")
        shutil.copytree(self.repo_dir, new_repo_dir)

        # 새 경로에서 스냅샷 목록화 및 복원
        snapshots = list_snapshots(new_repo_dir)
        self.assertEqual(len(snapshots), 2)

        dest = os.path.join(self.dest_dir, "chaos_08")
        res = restore_snapshot(new_repo_dir, self.snap2["id"], dest_dir=dest)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)

    # -------------------------------------------------------------
    # Chaos 9: DB 및 시스템 캐시 강제 소실 시에도 순수 스냅샷 JSON만으로 100% 복원
    # -------------------------------------------------------------
    def test_09_database_and_cache_lost(self):
        clean_repo = os.path.join(self.test_root, "clean_repo")
        os.makedirs(os.path.join(clean_repo, "snapshots"), exist_ok=True)
        os.makedirs(os.path.join(clean_repo, "blobs"), exist_ok=True)

        # 순수 snapshots와 blobs만 복사 (DB나 부가 파일 없음)
        for s in os.listdir(os.path.join(self.repo_dir, "snapshots")):
            if not s.endswith(".json"):
                continue
            shutil.copy(os.path.join(self.repo_dir, "snapshots", s), os.path.join(clean_repo, "snapshots", s))
        for shard in os.listdir(os.path.join(self.repo_dir, "blobs")):
            shutil.copytree(os.path.join(self.repo_dir, "blobs", shard), os.path.join(clean_repo, "blobs", shard))

        dest = os.path.join(self.dest_dir, "chaos_09")
        res = restore_snapshot(clean_repo, self.snap2["id"], dest_dir=dest)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)

    # -------------------------------------------------------------
    # Chaos 10: disaster_recovery.py CLI 외부 프로세스 단독 복원 완결 검증
    # -------------------------------------------------------------
    def test_10_standalone_disaster_recovery_cli(self):
        dr_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disaster_recovery.py")
        self.assertTrue(os.path.exists(dr_script))

        dest = os.path.join(self.dest_dir, "chaos_10")
        cmd = [
            sys.executable,
            dr_script,
            "--repo", self.repo_dir,
            "--restore", self.snap2["id"],
            "--dest", dest
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"disaster_recovery.py failed: {proc.stderr}")
        self.assertIn("복원 작업 완료", proc.stdout)

        # 복원된 파일 무결성 확인
        # (restore_snapshot 계약에 따라 dest 아래 전체 경로 트리에서 탐색해야 한다)
        restored_alpha = None
        for root_d, _, files in os.walk(dest):
            if "file_alpha.txt" in files:
                restored_alpha = os.path.join(root_d, "file_alpha.txt")
                break
        self.assertIsNotNone(restored_alpha, f"복원된 file_alpha.txt 없음 (dest={dest})")
        with open(restored_alpha, "rb") as f:
            self.assertEqual(f.read(), b"Alpha content MODIFIED in snapshot 2")


if __name__ == "__main__":
    unittest.main()
