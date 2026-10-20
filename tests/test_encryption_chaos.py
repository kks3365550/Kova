#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_encryption_chaos.py - 암호화 저장소 Chaos 11~22단계 극한 파괴 테스트 스위트
v2.7.0: AES-256-GCM At-Rest 암호화, Key Loss, Key Backup Restore, Nonce/AAD 위변조 거부 전수 검증
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
import hashlib
from typing import Dict, Any

from core.snapshot import SnapshotEngine
from core.crypto_at_rest import CryptoAtRestEngine
from core.worm import WORMManager
from disaster_recovery import (
    list_snapshots,
    restore_snapshot,
    audit_repository,
    extract_blob,
    get_blob_path
)


class TestEncryptionChaos(unittest.TestCase):
    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="enc_chaos_")
        self.src_dir = os.path.join(self.test_root, "source")
        self.repo_dir = os.path.join(self.test_root, "repo")
        self.dest_dir = os.path.join(self.test_root, "dest")
        self.key_backup_dir = os.path.join(self.test_root, "offline_keys")
        self.worm = WORMManager()

        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.dest_dir, exist_ok=True)
        os.makedirs(self.key_backup_dir, exist_ok=True)

        self.passphrase = "UltraMasterPassphrase2026!#"

        # 1. 테스트 원본 파일 생성
        self.files_data = {
            "secret_doc.txt": b"Confidential financial statement and business plans 2026",
            "db_dump.sql": b"CREATE TABLE users (id INT, pw_hash VARCHAR(64)); INSERT INTO users VALUES (1, 'hash');",
            "sub/credentials.env": b"DATABASE_URL=postgres://user:pass@localhost/db\nAPI_KEY=xyz987"
        }
        for rel_path, data in self.files_data.items():
            full_path = os.path.join(self.src_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)

        # 2. 암호화 활성화 스냅샷 생성
        self.snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="encrypted_chaos",
            profile_name="Encrypted Chaos Profile",
            compress_level=1,
            encryption_enabled=True,
            passphrase=self.passphrase
        )

        # 3. 암호화 엔진 및 오프라인 키 백업본 저장
        self.engine = CryptoAtRestEngine.from_passphrase(self.passphrase, self.repo_dir)
        self.offline_key_file = os.path.join(self.key_backup_dir, "master_key_backup.json")
        self.engine.export_key_backup(self.offline_key_file, passphrase_hint="2026Master")

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
    # Chaos 11: 암호화 블롭 1바이트 변조 시 GCM Auth Tag 불일치로 즉시 거부
    # -------------------------------------------------------------
    def test_11_encrypted_blob_bit_rot(self):
        target_entry = self.snap["entries"][0]
        h = target_entry.get("sha256") or target_entry.get("blob_id")
        blob_path = get_blob_path(self.repo_dir, h)
        self.assertTrue(os.path.exists(blob_path))

        # WORM 해제 후 1바이트 변조
        self.worm.unprotect_file(blob_path, authorized=True)
        with open(blob_path, "r+b") as f:
            f.seek(20)  # ciphertext 영역 변조
            byte = f.read(1)
            f.seek(20)
            f.write(bytes([byte[0] ^ 0xFF]))

        dest = os.path.join(self.dest_dir, "chaos_11")
        # 복호화 시도시 GCM 태그 불일치 에러 발생 검증
        with self.assertRaises(ValueError):
            extract_blob(blob_path, dest, expected_sha256=h, verify_hash=True, crypto_engine=self.engine)

    # -------------------------------------------------------------
    # Chaos 12: 특정 암호화 블롭 삭제 시 해당 파일 격리 실패 및 타 파일 정상 복원
    # -------------------------------------------------------------
    def test_12_encrypted_blob_missing(self):
        doc_entry = next(e for e in self.snap["entries"] if "secret_doc.txt" in (e.get("path") or e.get("rel_path", "")))
        h = doc_entry.get("sha256") or doc_entry.get("blob_id")
        blob_path = get_blob_path(self.repo_dir, h)
        self.worm.unprotect_file(blob_path, authorized=True)
        os.remove(blob_path)

        dest = os.path.join(self.dest_dir, "chaos_12")
        res = restore_snapshot(self.repo_dir, self.snap["id"], dest_dir=dest, crypto_engine=self.engine)

        self.assertFalse(res["success"])
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["restored"], len(self.files_data) - 1)

    # -------------------------------------------------------------
    # Chaos 13: 잘못된 마스터키 입력 시 복호화 단계에서 즉시 복원 차단
    # -------------------------------------------------------------
    def test_13_wrong_master_key(self):
        wrong_engine = CryptoAtRestEngine.from_passphrase("TotallyWrongPassphrase!@#$", self.repo_dir)
        dest = os.path.join(self.dest_dir, "chaos_13")
        res = restore_snapshot(self.repo_dir, self.snap["id"], dest_dir=dest, crypto_engine=wrong_engine)
        self.assertFalse(res["success"])
        self.assertEqual(res["restored"], 0)
        self.assertEqual(res["failed"], len(self.files_data))

    # -------------------------------------------------------------
    # Chaos 14: Manifest 내 AAD / 메타데이터 변조 시 복원 즉시 거부
    # -------------------------------------------------------------
    def test_14_corrupted_encryption_metadata(self):
        snap_file = os.path.join(self.repo_dir, "snapshots", f"{self.snap['id']}.json")
        self.worm.unprotect_file(snap_file, authorized=True)
        with open(snap_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        # 해시값을 변조하여 AAD 불일치 유도
        if manifest.get("entries"):
            manifest["entries"][0]["sha256"] = "f" * 64
        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        dest = os.path.join(self.dest_dir, "chaos_14")
        res = restore_snapshot(self.repo_dir, self.snap["id"], dest_dir=dest, crypto_engine=self.engine)
        self.assertFalse(res["success"])

    # -------------------------------------------------------------
    # Chaos 15: 블롭 헤더의 12B Nonce 변조 시 복호화 실패 격리
    # -------------------------------------------------------------
    def test_15_nonce_corruption(self):
        target_entry = self.snap["entries"][0]
        h = target_entry.get("sha256") or target_entry.get("blob_id")
        blob_path = get_blob_path(self.repo_dir, h)

        self.worm.unprotect_file(blob_path, authorized=True)
        with open(blob_path, "r+b") as f:
            f.seek(4)  # 매직 헤더(4B) 직후 Nonce 영역(12B)
            byte = f.read(1)
            f.seek(4)
            f.write(bytes([byte[0] ^ 0xAA]))

        dest = os.path.join(self.dest_dir, "chaos_15_file.txt")
        with self.assertRaises(ValueError):
            extract_blob(blob_path, dest, expected_sha256=h, verify_hash=True, crypto_engine=self.engine)

    # -------------------------------------------------------------
    # Chaos 16: 암호화 저장소 전체 경로 이동 시에도 100% 온전 복원
    # -------------------------------------------------------------
    def test_16_encrypted_repo_relocation(self):
        new_repo_dir = os.path.join(self.test_root, "relocated_encrypted_repo")
        shutil.copytree(self.repo_dir, new_repo_dir)

        dest = os.path.join(self.dest_dir, "chaos_16")
        new_engine = CryptoAtRestEngine.from_passphrase(self.passphrase, new_repo_dir)
        res = restore_snapshot(new_repo_dir, self.snap["id"], dest_dir=dest, crypto_engine=new_engine)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["restored"], len(self.files_data))

    # -------------------------------------------------------------
    # Chaos 17: DB/캐시 소실 상태에서 마스터키 + 저장소만으로 복원 완결
    # -------------------------------------------------------------
    def test_17_db_cache_lost_encrypted_dr(self):
        clean_repo = os.path.join(self.test_root, "clean_encrypted_repo")
        os.makedirs(os.path.join(clean_repo, "snapshots"), exist_ok=True)
        os.makedirs(os.path.join(clean_repo, "blobs"), exist_ok=True)
        os.makedirs(os.path.join(clean_repo, "keys"), exist_ok=True)

        for s in os.listdir(os.path.join(self.repo_dir, "snapshots")):
            if not s.endswith(".json"):
                continue
            shutil.copy(os.path.join(self.repo_dir, "snapshots", s), os.path.join(clean_repo, "snapshots", s))
        for shard in os.listdir(os.path.join(self.repo_dir, "blobs")):
            shutil.copytree(os.path.join(self.repo_dir, "blobs", shard), os.path.join(clean_repo, "blobs", shard))
        for k in os.listdir(os.path.join(self.repo_dir, "keys")):
            shutil.copy(os.path.join(self.repo_dir, "keys", k), os.path.join(clean_repo, "keys", k))

        dest = os.path.join(self.dest_dir, "chaos_17")
        res = restore_snapshot(clean_repo, self.snap["id"], dest_dir=dest, passphrase=self.passphrase)
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)

    # -------------------------------------------------------------
    # Chaos 18: disaster_recovery.py CLI 외부 프로세스 단독 암호화 복원 완결
    # -------------------------------------------------------------
    def test_18_standalone_dr_encrypted_cli(self):
        dr_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disaster_recovery.py")
        dest = os.path.join(self.dest_dir, "chaos_18")
        cmd = [
            sys.executable,
            dr_script,
            "--repo", self.repo_dir,
            "--restore", self.snap["id"],
            "--dest", dest,
            "--passphrase", self.passphrase
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"CLI execution failed: {proc.stderr}")
        self.assertIn("복원 작업 완료", proc.stdout)

        # 복원 파일 내용 일치 확인
        #
        # disaster_recovery.restore_snapshot()은 dest_dir 지정 시
        # '드라이브 문자만 제거한 전체 절대 경로'를 dest 아래에 재현한다
        # (disaster_recovery.py:478-481). 즉 dest 루트 바로 아래를 가정하면 안 된다.
        #   dest\Users\<user>\...\chaos_test_xxx\source\file_alpha.txt
        def _find_restored(dest_root, filename):
            for root_d, _, files in os.walk(dest_root):
                if filename in files:
                    return os.path.join(root_d, filename)
            raise AssertionError(f"복원된 파일을 찾을 수 없음: {filename} (dest={dest_root})")

        for rel_path, data in self.files_data.items():
            restored_path = _find_restored(dest, os.path.basename(rel_path))
            with open(restored_path, "rb") as f:
                self.assertEqual(f.read(), data)

    # -------------------------------------------------------------
    # Chaos 19: 원격 복제본에서 마스터키 없이 무결성 검증, 키 주입 시에만 복원
    # -------------------------------------------------------------
    def test_19_client_side_replication_only(self):
        # 원격지에는 키 없이 snapshots와 blobs만 복제된 상황
        remote_repo = os.path.join(self.test_root, "remote_repo_no_key")
        os.makedirs(os.path.join(remote_repo, "snapshots"), exist_ok=True)
        os.makedirs(os.path.join(remote_repo, "blobs"), exist_ok=True)

        for s in os.listdir(os.path.join(self.repo_dir, "snapshots")):
            if not s.endswith(".json"):
                continue
            shutil.copy(os.path.join(self.repo_dir, "snapshots", s), os.path.join(remote_repo, "snapshots", s))
        for shard in os.listdir(os.path.join(self.repo_dir, "blobs")):
            shutil.copytree(os.path.join(self.repo_dir, "blobs", shard), os.path.join(remote_repo, "blobs", shard))

        dest = os.path.join(self.dest_dir, "chaos_19")
        # 1) 키 없이 복원 시도 -> 복호화 불가로 실패
        res_no_key = restore_snapshot(remote_repo, self.snap["id"], dest_dir=dest)
        self.assertFalse(res_no_key["success"])

        # 2) 클라이언트 마스터키 주입 시 복원 성공
        res_with_key = restore_snapshot(remote_repo, self.snap["id"], dest_dir=dest, crypto_engine=self.engine)
        self.assertTrue(res_with_key["success"])

    # -------------------------------------------------------------
    # Chaos 20: 키 미입력 시 시스템 크래시 없이 명확한 에러 반환
    # -------------------------------------------------------------
    def test_20_key_unavailable_graceful_fail(self):
        dest = os.path.join(self.dest_dir, "chaos_20")
        res = restore_snapshot(self.repo_dir, self.snap["id"], dest_dir=dest)
        self.assertFalse(res["success"])
        self.assertEqual(res["restored"], 0)
        self.assertEqual(res["failed"], len(self.files_data))

    # -------------------------------------------------------------
    # Chaos 21: 마스터키 파괴 시 복원 절대 실패 격리 (Encryption Key Loss)
    # -------------------------------------------------------------
    def test_21_encryption_key_loss(self):
        # 저장소와 블롭은 100% 무결하지만, 키 메타데이터 및 사용자의 키가 영구 소실된 상황
        keys_dir = os.path.join(self.repo_dir, "keys")
        self.worm.unprotect_directory(keys_dir, authorized=True)
        for f in os.listdir(keys_dir):
            self.worm.unprotect_file(os.path.join(keys_dir, f), authorized=True)
        shutil.rmtree(keys_dir)

        dest = os.path.join(self.dest_dir, "chaos_21")
        # 키가 없으므로 복호화 절대 불가능 (RESTORE MUST FAIL)
        res = restore_snapshot(self.repo_dir, self.snap["id"], dest_dir=dest)
        self.assertFalse(res["success"], "키 소실 상태에서는 복원이 절대 성공하면 안 됩니다.")
        self.assertEqual(res["restored"], 0)

    # -------------------------------------------------------------
    # Chaos 22: 오프라인 백업된 키 파일 + 깨끗한 새 디렉터리에서 100% 완전 복원
    # -------------------------------------------------------------
    def test_22_key_backup_restore(self):
        self.assertTrue(os.path.exists(self.offline_key_file))

        # 완전히 새로운 머신/경로 가정
        fresh_dest = os.path.join(self.dest_dir, "fresh_machine_restore")
        res = restore_snapshot(
            repo_dir=self.repo_dir,
            snapshot_id=self.snap["id"],
            dest_dir=fresh_dest,
            key_file=self.offline_key_file
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["restored"], len(self.files_data))

        # 복원된 모든 파일 바이트 일치 확인
        for rel_path, expected_data in self.files_data.items():
            out_file = os.path.join(fresh_dest, rel_path)
        # 신메신(新 machines)에서 키 파일만으로 전체 복원 가능 확인
        # (restore_snapshot 은 드라이브 문자만 제거한 전체 경로를 dest 아래에 생성한다)
        def _find_restored(dest_root, filename):
            for root_d, _, files in os.walk(dest_root):
                if filename in files:
                    return os.path.join(root_d, filename)
            raise AssertionError(f"복원된 파일을 찾을 수 없음: {filename} (dest={dest_root})")

        for rel_path, expected_data in self.files_data.items():
            out_file = _find_restored(fresh_dest, os.path.basename(rel_path))
            with open(out_file, "rb") as f:
                self.assertEqual(f.read(), expected_data)
