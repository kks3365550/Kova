# -*- coding: utf-8 -*-
"""스케줄러 로그 오류 3건 수정 검증: Rust files->entries 변환, skipped 상태, 잠금 정보 가독성"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.lock import BackupLock, BackupAlreadyRunningError
from core.scheduler import BackupScheduler
from core.verify import IntegrityVerifier, RestoreVerificationError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rust_manifest():
    return {
        "files": [
            {"path": "dir", "size": 0, "is_dir": True, "chunks": []},
            {"path": "empty.txt", "size": 0, "is_dir": False, "chunks": []},
            {"path": "a.bin", "size": 30, "is_dir": False,
             "chunks": [{"hash": "h1", "offset": 0, "length": 10}, {"hash": "h2", "offset": 10, "length": 20}]},
        ]
    }


class TestRustVerifyConversion(unittest.TestCase):
    def setUp(self):
        self.sched = BackupScheduler()
        self.logs = []
        self.sched.log_callbacks.append(lambda msg, lvl: self.logs.append(msg))

    def tearDown(self):
        self.sched.log_callbacks.clear()

    def test_files_converted_to_entries(self):
        captured = {}

        class FakeVerifier:
            def __init__(self, repo_dir):
                pass

            def verify_restore_sampling(self, manifest, sample_count=20):
                captured["entries"] = manifest["entries"]
                return {"status": "passed"}

        with patch("core.verify.IntegrityVerifier", FakeVerifier):
            self.sched._verify_rust_snapshot("p", "repo", _rust_manifest())
        self.assertEqual(len(captured["entries"]), 1)
        e = captured["entries"][0]
        self.assertEqual(e["rel_path"], "a.bin")
        self.assertEqual(e["chunk_ids"], ["h1", "h2"])
        self.assertEqual(e["size"], 30)

    def test_failed_verification_raises(self):
        class FakeVerifier:
            def __init__(self, repo_dir):
                pass

            def verify_restore_sampling(self, manifest, sample_count=20):
                return {"status": "failed", "detail": "bad"}

        with patch("core.verify.IntegrityVerifier", FakeVerifier):
            with self.assertRaises(RestoreVerificationError):
                self.sched._verify_rust_snapshot("p", "repo", _rust_manifest())

    def test_no_files_is_warning_not_error(self):
        self.sched._verify_rust_snapshot("p", "repo", {"files": []})
        self.assertTrue(any("검증할 파일이 없어" in m for m in self.logs))

    def test_empty_entries_returns_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            res = IntegrityVerifier(d).verify_restore_sampling({"entries": []}, sample_count=5)
        self.assertEqual(res["status"], "skipped")


class TestLockInfoReadable(unittest.TestCase):
    def test_other_process_sees_holder_details(self):
        with tempfile.TemporaryDirectory() as repo:
            code = (
                "import sys; sys.path.insert(0, sys.argv[2]);"
                "from core.lock import BackupLock, BackupAlreadyRunningError\n"
                "try:\n"
                "    BackupLock(sys.argv[1], timeout_sec=0.1, process_desc='x').acquire()\n"
                "    print('ACQUIRED')\n"
                "except BackupAlreadyRunningError as e:\n"
                "    print('MSG:' + str(e).replace(chr(10), ' | '))\n"
            )
            with BackupLock(repo, timeout_sec=1, process_desc="홀더 작업"):
                out = subprocess.run(
                    [sys.executable, "-c", code, repo, ROOT],
                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                ).stdout
        self.assertIn("MSG:", out, out)
        self.assertIn("홀더 작업", out)
        self.assertNotIn("ACQUIRED", out)


if __name__ == "__main__":
    unittest.main()
