# -*- coding: utf-8 -*-
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from core.filter import PathFilter
from core.pack_format import PackContainerWriter, PackFormatError


def test_path_filter_excludes_windows_apps_and_locks():
    pf = PathFilter(use_defaults=True)
    assert pf.is_dir_excluded("WindowsApps", full_path="C:\\Users\\user\\AppData\\Local\\Microsoft\\WindowsApps") is True
    assert pf.is_dir_excluded("Temporary Internet Files", full_path="C:\\Users\\user\\AppData\\Local\\Microsoft\\Windows\\Temporary Internet Files") is True
    assert pf.is_file_excluded("lockfile") is True
    assert pf.is_file_excluded("WebCacheLock.dat") is True
    assert pf.is_file_excluded("UsrClass.dat") is True
    assert pf.is_file_excluded("UsrClass.dat.LOG1") is True


def test_pack_format_commit_index_retry_success(tmp_path):
    pack_path = tmp_path / "test.pck"
    idx_path = tmp_path / "test.idx"
    writer = PackContainerWriter(pack_path, idx_path)
    writer.write_chunk("a" * 64, b"testdata")

    # Mock os.replace to fail twice with PermissionError(WinError 32), then succeed
    original_replace = os.replace
    call_count = 0

    def mock_replace(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        writer.commit_index()

    assert call_count == 3
    assert idx_path.exists()
    writer.close()
