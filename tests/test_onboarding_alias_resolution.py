# -*- coding: utf-8 -*-
import os
import unittest
from core.snapshot import SnapshotEngine
from core.filter import PathFilter
from web.api_backup import RunBackupRequest


class TestOnboardingAliasResolution(unittest.TestCase):
    def test_run_backup_request_include_drivers(self):
        req = RunBackupRequest(include_drivers=True)
        self.assertTrue(req.include_drivers)

        req_false = RunBackupRequest(include_drivers=False)
        self.assertFalse(req_false.include_drivers)

    def test_scan_sources_alias_resolution(self):
        user_home = os.path.expanduser("~")
        desktop_dir = os.path.join(user_home, "Desktop")
        os.makedirs(desktop_dir, exist_ok=True)

        # Create a temporary test file in Desktop to guarantee scan discovers it
        test_file = os.path.join(desktop_dir, "_ci_test_sample.txt")
        created = False
        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ci test alias resolution")
            created = True
        except (PermissionError, OSError):
            pass

        try:
            pf = PathFilter()
            files = SnapshotEngine._scan_sources_parallel(
                sources=["Desktop"],
                path_filter=pf,
                num_workers=2
            )
            if created:
                self.assertGreater(len(files), 0)
                matched = any(f[0] == test_file or test_file in f[0] for f in files)
                self.assertTrue(matched)
                norm_desk = os.path.normpath(desktop_dir).lower()
                self.assertTrue(any(norm_desk in os.path.normpath(f[0]).lower() for f in files))
        finally:
            if created and os.path.exists(test_file):
                try:
                    os.remove(test_file)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
