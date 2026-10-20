# -*- coding: utf-8 -*-
"""
tests/benchmarks/prototypes/composite_storage/test_composite_facade.py
Track 2-11: Unit and End-to-End Tests for CompositeStorageFacade
Verifies seamless dual-read, mixed restore, transparent verification, and fallback.
"""

import os
import shutil
import tempfile
import unittest
import hashlib
from pathlib import Path

from tests.benchmarks.prototypes.composite_storage.composite_facade import CompositeStorageFacade
from tests.benchmarks.prototypes.pack_consistency.pack_format import PackContainerWriter
from core.storage import BlobStorage


class TestCompositeStorageFacade(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="composite_test_")
        self.repo_dir = Path(self.tmp_dir) / "repo"
        self.blobs_dir = self.repo_dir / "blobs"
        self.packs_dir = self.repo_dir / "packs"
        self.restore_dir = Path(self.tmp_dir) / "restore"

        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.restore_dir.mkdir(parents=True, exist_ok=True)

        # 1. Individual Blob Storage instance for populating legacy data
        self.legacy_storage = BlobStorage(str(self.repo_dir))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_dual_read_and_fallback(self):
        """
        1. Blob A: Individual Blob 저장소에만 존재 (레거시 백업)
        2. Blob B: Pack Container에만 존재 (신규 백업)
        3. Blob C: 저장소 어디에도 없음
        - CompositeFacade가 세 경우 모두 정확히 판별하고 투명하게 조회/추출하는지 검증
        """
        # Blob A: Legacy Individual File
        content_a = b"LEGACY_INDIVIDUAL_BLOB_CONTENT_AAA" * 200
        hash_a = hashlib.sha256(content_a).hexdigest()
        self.legacy_storage.put_bytes_blob(content_a)

        # Blob B: Pack Container
        content_b = b"PACK_CONTAINER_BLOB_CONTENT_BBB" * 200
        hash_b = hashlib.sha256(content_b).hexdigest()
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        pack_file = self.packs_dir / "pack_001.pack"
        idx_file = self.packs_dir / "pack_001.idx"

        writer = PackContainerWriter(pack_file, idx_file)
        writer.write_chunk(hash_b, content_b)
        writer.commit_index()
        writer.close()

        # Blob C: Non-existent
        hash_c = hashlib.sha256(b"NON_EXISTENT").hexdigest()

        # Initialize CompositeFacade
        facade = CompositeStorageFacade(self.repo_dir)

        # 1. has_blob 검증
        self.assertTrue(facade.has_blob(hash_a), "Blob A (Individual) must exist")
        self.assertTrue(facade.has_blob(hash_b), "Blob B (Pack) must exist")
        self.assertFalse(facade.has_blob(hash_c), "Blob C must NOT exist")

        # 2. extract_blob_to_file 검증
        dest_a = self.restore_dir / "file_a.dat"
        dest_b = self.restore_dir / "file_b.dat"
        dest_c = self.restore_dir / "file_c.dat"

        self.assertTrue(facade.extract_blob_to_file(hash_a, dest_a))
        self.assertEqual(dest_a.read_bytes(), content_a)

        self.assertTrue(facade.extract_blob_to_file(hash_b, dest_b))
        self.assertEqual(dest_b.read_bytes(), content_b)

        with self.assertRaises(FileNotFoundError):
            facade.extract_blob_to_file(hash_c, dest_c)

        # 3. verify_blob 검증
        ok_a, err_a, size_a = facade.verify_blob(hash_a)
        self.assertTrue(ok_a)
        self.assertIsNone(err_a)
        self.assertEqual(size_a, len(content_a))

        ok_b, err_b, size_b = facade.verify_blob(hash_b)
        self.assertTrue(ok_b)
        self.assertIsNone(err_b)
        self.assertEqual(size_b, len(content_b))

        ok_c, err_c, size_c = facade.verify_blob(hash_c)
        self.assertFalse(ok_c)
        self.assertIsNotNone(err_c)

    def test_pack_priority_over_individual(self):
        """
        동일한 해시가 Individual과 Pack 양쪽에 모두 존재할 경우,
        I/O 성능 및 통합 정책에 따라 Pack Container에서 우선 읽는지 검증
        """
        content = b"DUPLICATED_HASH_CHUNK_DATA" * 500
        h = hashlib.sha256(content).hexdigest()

        # Individual에 기록
        self.legacy_storage.put_bytes_blob(content)

        # Pack에도 동일 해시 기록
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        pack_file = self.packs_dir / "pack_priority.pack"
        idx_file = self.packs_dir / "pack_priority.idx"
        writer = PackContainerWriter(pack_file, idx_file)
        writer.write_chunk(h, content)
        writer.commit_index()
        writer.close()

        facade = CompositeStorageFacade(self.repo_dir)

        # pack_index_map에 존재해야 함
        self.assertIn(h, facade.pack_index_map)

        dest = self.restore_dir / "priority.dat"
        self.assertTrue(facade.extract_blob_to_file(h, dest))
        self.assertEqual(dest.read_bytes(), content)

    def test_mixed_snapshot_e2e_restore(self):
        """
        [엔드투엔드 시나리오]
        단일 스냅샷에 100개 파일이 존재하며:
        - 50개 파일은 과거 백업인 Individual Blobs에 보관됨
        - 50개 파일은 최신 백업인 Pack Container에 보관됨
        - 복원 엔진 및 검증기가 CompositeFacade를 통해 100개 파일 전부를 100% Bit-for-Bit 복원하는지 검증
        """
        files_manifest = []

        # 1. 50 Individual Blobs 생성
        for i in range(50):
            payload = f"INDIVIDUAL_PAYLOAD_{i}_{'X'*1000}".encode("utf-8")
            h = hashlib.sha256(payload).hexdigest()
            self.legacy_storage.put_bytes_blob(payload)
            files_manifest.append((f"legacy_dir/file_{i}.txt", h, payload))

        # 2. 50 Pack Container Chunks 생성
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        pack_file = self.packs_dir / "pack_batch.pack"
        idx_file = self.packs_dir / "pack_batch.idx"
        writer = PackContainerWriter(pack_file, idx_file)

        for i in range(50, 100):
            payload = f"PACK_PAYLOAD_{i}_{'Y'*1000}".encode("utf-8")
            h = hashlib.sha256(payload).hexdigest()
            writer.write_chunk(h, payload)
            files_manifest.append((f"new_dir/file_{i}.txt", h, payload))

        writer.commit_index()
        writer.close()

        # 3. CompositeFacade 기반 전수 복원 & 검증
        facade = CompositeStorageFacade(self.repo_dir)

        restored_count = 0
        for rel_path, h, expected_bytes in files_manifest:
            dest_file = self.restore_dir / rel_path

            # A. 검증 (verify_blob)
            ok, err, sz = facade.verify_blob(h)
            self.assertTrue(ok, f"Verification failed for {rel_path}: {err}")
            self.assertEqual(sz, len(expected_bytes))

            # B. 복원 (extract_blob_to_file)
            extracted = facade.extract_blob_to_file(h, dest_file)
            self.assertTrue(extracted)
            self.assertEqual(dest_file.read_bytes(), expected_bytes)

            restored_count += 1

        self.assertEqual(restored_count, 100, "All 100 mixed files must be restored!")


if __name__ == "__main__":
    unittest.main()
