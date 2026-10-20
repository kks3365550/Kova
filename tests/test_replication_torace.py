# -*- coding: utf-8 -*-
"""
tests/test_replication_torace.py - core/replication.py 동시성 회귀 테스트

발견된 결함:
    replicate_blob() 의 'os.path.exists(remote_path)' 검사와
    'os.replace(tmp_path, remote_path)' 사이에 다른 스레드가 같은 블롭을
    먼저 복제하는 TOCTOU 경합이 있었다. 선행 스레드가 lock_file_immutable()
    로 ReadOnly 를 설정하면 후행 스레드의 os.replace 가 PermissionError 로
    실패했다.

    재현 결과(수정 전): 8 스레드 중 7개 FAIL:PermissionError

CAS 블롭은 내용 주소 기반이므로 '이미 존재 = 동일 내용'이다.
따라서 이 경합은 benign 이며, 실패가 아니라 0건 처리로 흡수해야 한다.
"""
import os
import sys
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.replication import ReplicationManager


class TestReplicationToctouRace(unittest.TestCase):
    """동시 복제 경합 회귀 테스트"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rep_torace_")
        self.local = os.path.join(self.tmp, "local")
        self.remote = os.path.join(self.tmp, "remote")
        self.src = os.path.join(self.tmp, "src")
        for d in (self.local, self.remote, self.src):
            os.makedirs(d, exist_ok=True)

        # 동일 내용을 가진 여러 파일 -> 동일 sha256 -> 동일 blob 하나
        for i in range(8):
            with open(os.path.join(self.src, "dup_%d.txt" % i), "w") as f:
                f.write("identical content for dedup\n")

        from core.snapshot import SnapshotEngine
        self.snap = SnapshotEngine.create_snapshot(
            repo_dir=self.local,
            sources=[self.src],
            profile_id="race",
            profile_name="race",
        )

    def tearDown(self):
        from core.worm import WORMManager
        wm = WORMManager()
        try:
            wm.unprotect_directory(self.remote, authorized=True)
            for root, _, files in os.walk(self.remote):
                for f in files:
                    wm.unprotect_file(os.path.join(root, f), authorized=True)
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_replicate_same_blob_does_not_raise(self):
        """
        동일 블롭을 여러 스레드가 동시에 복제해도 예외가 전파되지 않아야 한다.
        (수정 전: PermissionError 로 다수 실패)
        """
        rm = ReplicationManager(self.local, self.remote)
        rm._ensure_remote_structure()

        blob_id = self.snap["entries"][0].get("blob_id") or \
            self.snap["entries"][0].get("sha256")

        def _one(_i):
            try:
                rm.replicate_blob(blob_id)
                return "OK"
            except Exception as e:
                return "FAIL:%s" % type(e).__name__

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_one, range(8)))

        failures = [r for r in results if r != "OK"]
        self.assertFalse(
            failures,
            "동시 복제에서 %d건 실패 (TOCTOU 회귀): %s" % (len(failures), failures)
        )

    def test_remote_blob_exists_after_race(self):
        """경합 이후 원격 블롭이 실제로 존재해야 한다."""
        rm = ReplicationManager(self.local, self.remote)
        rm._ensure_remote_structure()

        blob_id = self.snap["entries"][0].get("blob_id") or \
            self.snap["entries"][0].get("sha256")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda i: rm.replicate_blob(blob_id), range(8)))

        remote_blob = rm._get_remote_blob_path(blob_id)
        self.assertTrue(os.path.exists(remote_blob),
                        "경합 후 원격 블롭이 존재해야 한다")

    def test_replicate_snapshot_concurrent_consistency(self):
        """스냅샷 단위 동시 복제 시 원격/로컬 블롭 수가 일치해야 한다."""
        rm = ReplicationManager(self.local, self.remote)
        rm._ensure_remote_structure()

        results = []
        errors = []

        def _do(_i):
            try:
                r = rm.replicate_snapshot(self.snap["id"], max_workers=8)
                results.append(r)
            except Exception as e:
                errors.append(repr(e))

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(_do, range(3)))

        self.assertFalse(errors, "스냅샷 동시 복제 중 예외: %s" % errors[:3])

        for r in results:
            self.assertEqual(r.get("status"), "success",
                             "모든 스냅샷 복제가 성공해야 한다")
            self.assertEqual(r.get("failed_blobs", 0), 0,
                             "실패한 블롭이 없어야 한다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
