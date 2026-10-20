# -*- coding: utf-8 -*-
"""
tests/benchmarks/prototypes/pack_consistency/test_pack_crash_consistency.py
Track 2-10: Automated Test Suite for Crash Consistency, Torn Write, and Atomic Recovery
"""

import os
import shutil
import tempfile
import unittest
import hashlib
import random
from pathlib import Path

from tests.benchmarks.prototypes.pack_consistency.pack_format import (
    PackContainerWriter,
    PackContainerReader,
    PackRecoveryEngine,
    PackConsistencyError,
    PackFormatError,
    HEADER_SIZE,
    FOOTER_SIZE
)


class TestPackCrashConsistency(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="pack_crash_test_")
        self.pack_path = Path(self.tmp_dir) / "data.pack"
        self.idx_path = Path(self.tmp_dir) / "data.idx"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _generate_deterministic_chunks(self, count: int, chunk_size: int = 16384):
        chunks = []
        for i in range(count):
            # Seeded deterministic content
            rng = random.Random(i + 42)
            payload = rng.randbytes(chunk_size)
            h = hashlib.sha256(payload).hexdigest()
            chunks.append((h, payload))
        return chunks

    def test_scenario_0_baseline_clean_commit(self):
        """기본 정상 경로: N개 청크 작성 및 원자적 커밋 후 100% 온전성 검증"""
        chunks = self._generate_deterministic_chunks(50, 8192)
        writer = PackContainerWriter(self.pack_path, self.idx_path)
        for h, payload in chunks:
            writer.write_chunk(h, payload)
        writer.commit_index()
        writer.close()

        reader = PackContainerReader(self.pack_path, self.idx_path)
        self.assertEqual(len(reader.index), 50)
        for h, payload in chunks:
            self.assertTrue(reader.has_chunk(h))
            read_payload = reader.read_chunk(h, verify=True)
            self.assertEqual(read_payload, payload)

    def test_scenario_1_partial_uncommitted_write(self):
        """
        시나리오 1: 50개 작성 중 commit_index() 호출 직전 강제 종료/크래시.
        - 기존 인덱스가 없거나 미완성인 상태.
        - RecoveryEngine이 Pack 프레임 전수 검사를 통해 온전한 50개 청크를 100% 원자적 재구축.
        """
        chunks = self._generate_deterministic_chunks(50, 8192)
        writer = PackContainerWriter(self.pack_path, self.idx_path)
        for h, payload in chunks:
            writer.write_chunk(h, payload)
        # commit_index() 호출 없이 프로세스 사망 모의 (close만 수행)
        writer.close()

        # 인덱스가 없으므로 일반 Reader는 비어 있음
        reader_before = PackContainerReader(self.pack_path, self.idx_path)
        self.assertEqual(len(reader_before.index), 0)

        # 자동 복구 엔진 가동
        rebuilt_count, valid_offset = PackRecoveryEngine.rebuild_index_from_pack(
            self.pack_path, self.idx_path
        )
        self.assertEqual(rebuilt_count, 50)
        self.assertEqual(valid_offset, self.pack_path.stat().st_size)

        # 복구된 인덱스로 모든 청크 바이트 일치 검증
        reader_after = PackContainerReader(self.pack_path, self.idx_path)
        self.assertEqual(len(reader_after.index), 50)
        for h, payload in chunks:
            self.assertEqual(reader_after.read_chunk(h, verify=True), payload)

    def test_scenario_2_torn_write_truncation(self):
        """
        시나리오 2: 50개 청크를 쓴 후, 51번째 청크를 쓰던 중 중간 바이트에서 전원 차단/잘림(Truncation).
        - 51번째 청크의 헤더 일부 또는 페이로드만 기록된 상태.
        - Reader는 인덱스 불일치 탐지 or RecoveryEngine이 마지막 51번째 쓰레기를 정확히 잘라내고(Rollback)
          50개 청크만 원자적으로 유지.
        """
        chunks = self._generate_deterministic_chunks(50, 8192)
        writer = PackContainerWriter(self.pack_path, self.idx_path)
        for h, payload in chunks:
            writer.write_chunk(h, payload)
        writer.commit_index()
        writer.close()

        # 고의로 51번째 불완전 청크 쓰기 (반쪽짜리 헤더 15바이트만 기록)
        with open(self.pack_path, "ab") as f:
            f.write(b"CHNK\x00\x00\x00\x33\x00\x10\x00") # 11 bytes incomplete frame

        # 복구 엔진 실행
        rebuilt_count, valid_offset = PackRecoveryEngine.rebuild_index_from_pack(
            self.pack_path, self.idx_path
        )
        self.assertEqual(rebuilt_count, 50)

        # 불완전한 trailing 바이트를 깨끗이 롤백 자르기
        PackRecoveryEngine.truncate_to_valid_offset(self.pack_path, valid_offset)

        # 다시 51번째 정상 청크 추가 쓰기가 가능한지 검증 (Append-Only 복구 후 계속 쓰기)
        new_chunk_payload = b"NEW_CHUNK_AFTER_RECOVERY" * 100
        new_h = hashlib.sha256(new_chunk_payload).hexdigest()

        writer2 = PackContainerWriter(self.pack_path, self.idx_path)
        # 이전 인덱스 로드 후 추가
        reader_tmp = PackContainerReader(self.pack_path, self.idx_path)
        writer2.index_entries = reader_tmp.index.copy()
        writer2.write_chunk(new_h, new_chunk_payload)
        writer2.commit_index()
        writer2.close()

        # 총 51개 정상 검증
        final_reader = PackContainerReader(self.pack_path, self.idx_path)
        self.assertEqual(len(final_reader.index), 51)
        self.assertEqual(final_reader.read_chunk(new_h, verify=True), new_chunk_payload)

    def test_scenario_3_index_pack_desync_detection(self):
        """
        시나리오 3: 인덱스 파일(.idx)은 50개를 기대하는데, Pack 파일 끝이 강제로 잘려 작아진 경우.
        - Reader 초기화 시 PackConsistencyError를 즉각 발생시켜 Fail-Closed 보장.
        - RecoveryEngine을 통해 실존하는 유효 바이트 범위까지만 자동 Re-index 수행.
        """
        chunks = self._generate_deterministic_chunks(50, 8192)
        writer = PackContainerWriter(self.pack_path, self.idx_path)
        for h, payload in chunks:
            writer.write_chunk(h, payload)
        writer.commit_index()
        writer.close()

        # Pack 파일의 마지막 2개 청크를 물리적으로 잘라버림
        orig_size = self.pack_path.stat().st_size
        cut_size = orig_size - (8192 + HEADER_SIZE + FOOTER_SIZE) * 2
        with open(self.pack_path, "r+b") as f:
            f.truncate(cut_size)

        # Reader는 기대 크기 > 실제 크기 불일치로 즉각 예외 발생
        with self.assertRaises(PackConsistencyError):
            PackContainerReader(self.pack_path, self.idx_path)

        # RecoveryEngine으로 인덱스 자동 재구축 -> 48개로 안전 복원
        rebuilt_count, valid_offset = PackRecoveryEngine.rebuild_index_from_pack(
            self.pack_path, self.idx_path
        )
        self.assertEqual(rebuilt_count, 48)

        recovered_reader = PackContainerReader(self.pack_path, self.idx_path)
        self.assertEqual(len(recovered_reader.index), 48)

    def test_scenario_4_bit_rot_isolation(self):
        """
        시나리오 4: 특정 청크(예: 청크 25)의 페이로드 내부 1바이트가 변조(Bit Rot)된 경우.
        - 손상된 청크 25 읽기 시 PackConsistencyError (CRC/SHA256 불일치) 발생.
        - 다른 정상 청크 0~24, 26~49는 전혀 영향 없이 100% 정상 복원 (완벽한 오류 격리).
        """
        chunks = self._generate_deterministic_chunks(50, 8192)
        writer = PackContainerWriter(self.pack_path, self.idx_path)
        for h, payload in chunks:
            writer.write_chunk(h, payload)
        writer.commit_index()
        writer.close()

        reader = PackContainerReader(self.pack_path, self.idx_path)
        target_hash = chunks[25][0]
        offset, length, _ = reader.index[target_hash]

        # 청크 25의 페이로드 중간 1바이트를 반전
        with open(self.pack_path, "r+b") as f:
            f.seek(offset + 100)
            b = f.read(1)
            corrupted_byte = bytes([b[0] ^ 0xFF])
            f.seek(offset + 100)
            f.write(corrupted_byte)

        reader_after = PackContainerReader(self.pack_path, self.idx_path)

        # 1. 오염된 청크 25는 CRC/SHA 불일치로 에러 발생
        with self.assertRaises(PackConsistencyError):
            reader_after.read_chunk(target_hash, verify=True)

        # 2. 나머지 모든 49개 청크는 정상 동작 (완전 격리)
        for i, (h, payload) in enumerate(chunks):
            if i == 25:
                continue
            self.assertEqual(reader_after.read_chunk(h, verify=True), payload)


if __name__ == "__main__":
    unittest.main()
