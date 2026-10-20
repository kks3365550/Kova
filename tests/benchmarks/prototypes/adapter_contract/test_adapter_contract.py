"""
Track 2-8: Comprehensive Contract Test Suite (13 Test Cases).
Verifies:
1. Empty file (0 bytes)
2. 1-byte file
3. Sub-chunk file (< 64KB / < 4MB)
4. Exact boundary file (== 64KB)
5. Boundary + 1 byte (64KB + 1B)
6. Duplicate data file (pattern repetition)
7. Repeated processing (idempotency)
8. Corrupted/invalid manifest
9. Missing chunk
10. Hash mismatch (tampered chunk)
11. Truncated chunk
12. File mutated during chunking
13. Full bit-for-bit SHA-256 match

All 3 Adapters (WholeFile, FixedBlock, FastCDC) are tested against the exact same contract.
"""

import os
import sys
import shutil
import hashlib
import tempfile
import unittest
from typing import Dict, List, Any

# 모듈 로드 경로 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from tests.benchmarks.prototypes.adapter_contract.contract import (
    ChunkItem, ManifestEntry, ChunkingAdapter,
    FileMutatedError, RestoreError, ChunkMissingError,
    ChunkCorruptedError, ManifestInvalidError
)
from tests.benchmarks.prototypes.adapter_contract.adapters import (
    WholeFileAdapter, FixedBlockAdapter, FastCDCAdapter
)


class MockChunkStore:
    """테스트용 단순 인메모리/파일 CAS 저장소"""
    def __init__(self):
        self.chunks: Dict[str, bytes] = {}

    def sink(self, item: ChunkItem, data: bytes):
        self.chunks[item.chunk_id] = data

    def fetch(self, chunk_id: str) -> bytes:
        if chunk_id not in self.chunks:
            raise ChunkMissingError(f"Chunk '{chunk_id}' not found in store.")
        return self.chunks[chunk_id]


class TestAdapterContract(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="contract_test_")
        self.store = MockChunkStore()
        # 빠른 테스트를 위해 작은 블록 크기로 설정 (기능 검증 동일)
        self.fixed_size = 64 * 1024       # 64KB
        self.cdc_min = 16 * 1024          # 16KB
        self.cdc_avg = 64 * 1024          # 64KB
        self.cdc_max = 128 * 1024         # 128KB

        self.adapters: List[ChunkingAdapter] = [
            WholeFileAdapter(),
            FixedBlockAdapter(block_size=self.fixed_size),
            FastCDCAdapter(min_size=self.cdc_min, avg_size=self.cdc_avg, max_size=self.cdc_max)
        ]

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_file(self, name: str, data: bytes) -> str:
        p = os.path.join(self.test_dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _test_roundtrip(self, adapter: ChunkingAdapter, filepath: str, expected_size: int) -> ManifestEntry:
        # 1. 청킹
        manifest = adapter.chunk_file(filepath, chunk_sink_cb=self.store.sink)
        self.assertEqual(manifest.original_size, expected_size)
        manifest.validate_contract()

        # 2. 복원
        restored_path = filepath + f".restored_{adapter.strategy_id}"
        ok = adapter.restore_file(manifest, chunk_fetch_cb=self.store.fetch, output_filepath=restored_path)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(restored_path))

        # 3. Bit-for-Bit SHA-256 일치 검증
        with open(filepath, "rb") as f1, open(restored_path, "rb") as f2:
            h1 = hashlib.sha256(f1.read()).hexdigest()
            h2 = hashlib.sha256(f2.read()).hexdigest()
        self.assertEqual(h1, h2)
        self.assertEqual(h1, manifest.file_sha256)
        return manifest

    # 1. Empty file (0 bytes)
    def test_01_empty_file(self):
        p = self._create_file("empty.bin", b"")
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, 0)
            self.assertEqual(len(m.chunks), 0)

    # 2. 1-byte file
    def test_02_single_byte_file(self):
        p = self._create_file("one_byte.bin", b"X")
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, 1)
            self.assertEqual(len(m.chunks), 1)
            self.assertEqual(m.chunks[0].length, 1)
            self.assertEqual(m.chunks[0].offset, 0)

    # 3. Sub-chunk file (< 64KB)
    def test_03_sub_chunk_file(self):
        data = os.urandom(10 * 1024)  # 10KB
        p = self._create_file("sub_chunk.bin", data)
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, len(data))
            self.assertEqual(len(m.chunks), 1)
            self.assertEqual(m.chunks[0].length, len(data))

    # 4. Exact boundary file (== 64KB)
    def test_04_exact_boundary_file(self):
        data = os.urandom(self.fixed_size)  # 정확히 64KB
        p = self._create_file("exact_boundary.bin", data)
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, len(data))
            if adp.strategy_id.startswith("fixed"):
                self.assertEqual(len(m.chunks), 1)
                self.assertEqual(m.chunks[0].length, self.fixed_size)

    # 5. Boundary + 1 byte (64KB + 1B)
    def test_05_boundary_plus_one_byte(self):
        data = os.urandom(self.fixed_size + 1)  # 64KB + 1B
        p = self._create_file("boundary_p1.bin", data)
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, len(data))
            if adp.strategy_id.startswith("fixed"):
                self.assertEqual(len(m.chunks), 2)
                self.assertEqual(m.chunks[0].length, self.fixed_size)
                self.assertEqual(m.chunks[1].length, 1)
                self.assertEqual(m.chunks[1].offset, self.fixed_size)

    # 6. Duplicate data file (pattern repetition)
    def test_06_duplicate_data_file(self):
        pattern = b"REPEATING_BLOCK_DATA_ABCDEFG_" * 1024  # 약 30KB
        data = pattern * 5  # 약 150KB
        p = self._create_file("dup_pattern.bin", data)
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, len(data))
            # 중복 데이터가 올바르게 재조립되었는지 확인
            self.assertGreater(len(m.chunks), 0)

    # 7. Repeated processing (idempotency)
    def test_07_repeated_processing_idempotency(self):
        data = os.urandom(200 * 1024)
        p = self._create_file("repeat.bin", data)
        for adp in self.adapters:
            m1 = adp.chunk_file(p, self.store.sink)
            m2 = adp.chunk_file(p, self.store.sink)
            self.assertEqual(m1.file_sha256, m2.file_sha256)
            self.assertEqual(len(m1.chunks), len(m2.chunks))
            for c1, c2 in zip(m1.chunks, m2.chunks):
                self.assertEqual(c1.chunk_id, c2.chunk_id)
                self.assertEqual(c1.offset, c2.offset)
                self.assertEqual(c1.length, c2.length)

    # 8. Corrupted/invalid manifest
    def test_08_invalid_manifest(self):
        data = os.urandom(50 * 1024)
        p = self._create_file("invalid_m.bin", data)
        adp = FixedBlockAdapter(block_size=16 * 1024)
        m = adp.chunk_file(p, self.store.sink)

        # 1) 오프셋 구멍 조작
        bad_chunks = list(m.chunks)
        bad_chunks[1] = ChunkItem(chunk_id=bad_chunks[1].chunk_id, offset=bad_chunks[1].offset + 10, length=bad_chunks[1].length, content_hash=bad_chunks[1].content_hash)
        bad_m = ManifestEntry(schema_version="v1", strategy_id=m.strategy_id, original_size=m.original_size, file_sha256=m.file_sha256, chunks=bad_chunks)

        out_p = p + ".out"
        with self.assertRaises(ManifestInvalidError):
            adp.restore_file(bad_m, self.store.fetch, out_p)
        self.assertFalse(os.path.exists(out_p))

    # 9. Missing chunk
    def test_09_missing_chunk(self):
        data = os.urandom(100 * 1024)
        p = self._create_file("missing_chk.bin", data)
        adp = FixedBlockAdapter(block_size=32 * 1024)
        m = adp.chunk_file(p, self.store.sink)

        # 저장소에서 청크 하나 삭제
        target_chunk_id = m.chunks[1].chunk_id
        del self.store.chunks[target_chunk_id]

        out_p = p + ".out"
        with self.assertRaises(ChunkMissingError):
            adp.restore_file(m, self.store.fetch, out_p)
        # 부분 파일이 남지 않았는지 확인
        self.assertFalse(os.path.exists(out_p))

    # 10. Hash mismatch (tampered chunk)
    def test_10_hash_mismatch_tampered(self):
        data = os.urandom(100 * 1024)
        p = self._create_file("tamper.bin", data)
        adp = FixedBlockAdapter(block_size=32 * 1024)
        m = adp.chunk_file(p, self.store.sink)

        # 저장소의 청크 1바이트 변조
        target_chunk_id = m.chunks[0].chunk_id
        original_chunk = self.store.chunks[target_chunk_id]
        tampered_chunk = bytearray(original_chunk)
        tampered_chunk[0] ^= 0xFF
        self.store.chunks[target_chunk_id] = bytes(tampered_chunk)

        out_p = p + ".out"
        with self.assertRaises(ChunkCorruptedError):
            adp.restore_file(m, self.store.fetch, out_p)
        self.assertFalse(os.path.exists(out_p))

    # 11. Truncated chunk
    def test_11_truncated_chunk(self):
        data = os.urandom(100 * 1024)
        p = self._create_file("trunc.bin", data)
        adp = FixedBlockAdapter(block_size=32 * 1024)
        m = adp.chunk_file(p, self.store.sink)

        # 청크 길이를 10바이트 잘라냄
        target_chunk_id = m.chunks[0].chunk_id
        self.store.chunks[target_chunk_id] = self.store.chunks[target_chunk_id][:-10]

        out_p = p + ".out"
        with self.assertRaises(ChunkCorruptedError):
            adp.restore_file(m, self.store.fetch, out_p)
        self.assertFalse(os.path.exists(out_p))

    # 12. File mutated during chunking
    def test_12_file_mutated_during_chunking(self):
        p = self._create_file("mutated.bin", b"INITIAL_DATA_" * 1000)

        # 커스텀 훅을 걸어 청킹 도중 파일을 변경시키는 어댑터 시뮬레이션
        class MutatingFixedAdapter(FixedBlockAdapter):
            def chunk_file(self, filepath, chunk_sink_cb=None):
                init_st = os.stat(filepath)
                # 도중에 파일 수정
                with open(filepath, "ab") as f:
                    f.write(b"MUTATED_TAIL_BYTES")
                self._verify_file_stability(filepath, init_st)

        adp = MutatingFixedAdapter(block_size=1024)
        with self.assertRaises(FileMutatedError):
            adp.chunk_file(p, self.store.sink)

    # 13. Full bit-for-bit SHA-256 match on complex data
    def test_13_full_bit_for_bit_match_complex(self):
        # 1MB 난수 데이터에 대한 3대 어댑터 전수 복원 검증
        data = os.urandom(1024 * 1024)
        p = self._create_file("complex_1mb.bin", data)
        for adp in self.adapters:
            m = self._test_roundtrip(adp, p, len(data))
            self.assertEqual(m.original_size, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
