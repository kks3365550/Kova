"""
Track 2-9: Pack Container Unit & Crash/Atomicity Tests.
Verifies:
1. Ingest & Lookup
2. Deduplication on double ingest
3. Missing chunk error
4. Truncated record / container (fail-closed)
5. Corrupted record CRC32 mismatch (fail-closed)
6. Corrupted payload SHA-256 mismatch (fail-closed)
7. Integration with Chunking Adapters (Strategy != Layout)
"""

import os
import sys
import shutil
import hashlib
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from tests.benchmarks.prototypes.pack_container.format import (
    StorageCorruptionError, ChunkNotFoundError
)
from tests.benchmarks.prototypes.pack_container.store import (
    PackContainerStore, IndividualFileStore
)
from tests.benchmarks.prototypes.adapter_contract.adapters import FixedBlockAdapter


class TestPackContainer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="pack_test_")
        self.store = PackContainerStore(self.test_dir, max_pack_size=10 * 1024 * 1024)

    def tearDown(self):
        if self.store._current_pack_file:
            self.store._current_pack_file.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_ingest_and_lookup(self):
        data = b"HELLO_PACK_CONTAINER_PAYLOAD_12345"
        cid = hashlib.sha256(data).hexdigest()

        is_new, stored = self.store.put_chunk(cid, data)
        self.assertTrue(is_new)
        self.assertGreater(stored, len(data))
        self.assertTrue(self.store.has_chunk(cid))

        fetched = self.store.get_chunk(cid)
        self.assertEqual(fetched, data)

    def test_02_deduplication_double_ingest(self):
        data = b"DEDUP_TEST_CHUNK_PAYLOAD"
        cid = hashlib.sha256(data).hexdigest()

        is_new1, stored1 = self.store.put_chunk(cid, data)
        is_new2, stored2 = self.store.put_chunk(cid, data)

        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(stored2, 0)

        stats = self.store.get_stats()
        self.assertEqual(stats["total_chunks"], 1)

    def test_03_missing_chunk_error(self):
        fake_cid = hashlib.sha256(b"NON_EXISTENT").hexdigest()
        with self.assertRaises(ChunkNotFoundError):
            self.store.get_chunk(fake_cid)

    def test_04_truncated_record_fail_closed(self):
        data = b"TRUNCATED_RECORD_DATA_TEST" * 100
        cid = hashlib.sha256(data).hexdigest()
        self.store.put_chunk(cid, data)
        self.store.flush()
        self.store._current_pack_file.close()
        self.store._current_pack_file = None

        # Pack 파일의 마지막 10바이트를 잘라냄
        pack_path = os.path.join(self.store.packs_dir, "pack_0000.bin")
        orig_sz = os.path.getsize(pack_path)
        with open(pack_path, "r+b") as f:
            f.truncate(orig_sz - 10)

        with self.assertRaises(StorageCorruptionError):
            self.store.get_chunk(cid)

    def test_05_corrupted_crc32_fail_closed(self):
        data = b"CRC_CORRUPTED_RECORD_DATA" * 100
        cid = hashlib.sha256(data).hexdigest()
        self.store.put_chunk(cid, data)
        self.store.flush()
        self.store._current_pack_file.close()
        self.store._current_pack_file = None

        # Pack 파일의 CRC 바이트 변조
        pack_path = os.path.join(self.store.packs_dir, "pack_0000.bin")
        orig_sz = os.path.getsize(pack_path)
        with open(pack_path, "r+b") as f:
            f.seek(orig_sz - 2)
            f.write(b"\xFF\xFF")

        with self.assertRaises(StorageCorruptionError):
            self.store.get_chunk(cid)

    def test_06_corrupted_payload_sha256_fail_closed(self):
        data = b"PAYLOAD_SHA_CORRUPTED_DATA" * 100
        cid = hashlib.sha256(data).hexdigest()
        self.store.put_chunk(cid, data)
        self.store.flush()
        self.store._current_pack_file.close()
        self.store._current_pack_file = None

        # 페이로드 중간 1바이트 변조 (헤더+페이로드 CRC도 맞지 않겠지만 우선 감지 보장)
        pack_path = os.path.join(self.store.packs_dir, "pack_0000.bin")
        with open(pack_path, "r+b") as f:
            f.seek(50)  # 페이로드 내부
            f.write(b"\xAA")

        with self.assertRaises(StorageCorruptionError):
            self.store.get_chunk(cid)

    def test_07_adapter_integration_roundtrip(self):
        # Chunking Strategy != Storage Layout 검증
        adp = FixedBlockAdapter(block_size=1024)
        sample_file = os.path.join(self.test_dir, "sample.bin")
        raw_data = os.urandom(5000)
        with open(sample_file, "wb") as f:
            f.write(raw_data)

        # 1. 청킹 및 PackContainerStore에 sink
        manifest = adp.chunk_file(sample_file, chunk_sink_cb=lambda item, b: self.store.put_chunk(item.chunk_id, b))
        self.assertEqual(len(manifest.chunks), 5)

        # 2. PackContainerStore에서 fetch하여 복원
        restored_file = os.path.join(self.test_dir, "restored.bin")
        ok = adp.restore_file(manifest, chunk_fetch_cb=self.store.get_chunk, output_filepath=restored_file)
        self.assertTrue(ok)

        with open(restored_file, "rb") as rf:
            self.assertEqual(rf.read(), raw_data)


if __name__ == "__main__":
    unittest.main()
