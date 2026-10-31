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
        
        # Test with relative alias 'Desktop'
        pf = PathFilter()
        files = SnapshotEngine._scan_sources_parallel(
            sources=["Desktop"],
            path_filter=pf,
            num_workers=2
        )
        # Should not crash and should resolve to real desktop if it exists
        if os.path.exists(desktop_dir) and os.listdir(desktop_dir):
            self.assertGreater(len(files), 0)
            self.assertTrue(files[0][0].startswith(desktop_dir) or desktop_dir in files[0][0])

if __name__ == "__main__":
    unittest.main()
