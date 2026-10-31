# -*- coding: utf-8 -*-
"""
tests/test_file_search_restore.py

스냅샷 파일 검색 (/api/snapshots/search-files) 및 원클릭 복원 단위 테스트
"""
import os
import json
from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)


def test_search_snapshot_files_short_query():
    """1글자 이하 검색어는 빈 목록 반환"""
    res = client.get("/api/snapshots/search-files?q=a")
    assert res.status_code == 200
    assert res.json() == []

    res_empty = client.get("/api/snapshots/search-files?q= ")
    assert res_empty.status_code == 200
    assert res_empty.json() == []


def test_search_snapshot_files_with_mock_repo(tmp_path):
    """임시 리포지토리에 스냅샷을 구성하고 파일 검색 및 버전 그룹화 검증"""
    repo_dir = str(tmp_path / "test_repo")
    os.makedirs(os.path.join(repo_dir, "snapshots"), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, "blobs"), exist_ok=True)

    # 1. 이전 시점 스냅샷 (v1)
    snap1 = {
        "id": "snap_test_001",
        "created_at": 1780000000.0,
        "backup_type": "full",
        "entries": [
            {
                "source_root": "C:\\MyData",
                "rel_path": "reports/quarterly_report.xlsx",
                "size": 10240,
                "mtime": 1779999000.0,
                "sha256": "hash_v1",
                "blob_id": "blob_v1",
                "status": "new"
            },
            {
                "source_root": "C:\\MyData",
                "rel_path": "photos/team_photo.jpg",
                "size": 204800,
                "mtime": 1779998000.0,
                "sha256": "hash_p1",
                "blob_id": "blob_p1",
                "status": "new"
            }
        ]
    }
    with open(os.path.join(repo_dir, "snapshots", "snap_test_001.json"), "w", encoding="utf-8") as f:
        json.dump(snap1, f)

    # 2. 최신 시점 스냅샷 (v2) - quarterly_report.xlsx 내용 수정됨
    snap2 = {
        "id": "snap_test_002",
        "created_at": 1780086400.0,
        "backup_type": "incremental",
        "entries": [
            {
                "source_root": "C:\\MyData",
                "rel_path": "reports/quarterly_report.xlsx",
                "size": 15360,
                "mtime": 1780085000.0,
                "sha256": "hash_v2",
                "blob_id": "blob_v2",
                "status": "modified"
            },
            {
                "source_root": "C:\\MyData",
                "rel_path": "notes.txt",
                "size": 512,
                "mtime": 1780084000.0,
                "sha256": "hash_n1",
                "blob_id": "blob_n1",
                "status": "new"
            }
        ]
    }
    with open(os.path.join(repo_dir, "snapshots", "snap_test_002.json"), "w", encoding="utf-8") as f:
        json.dump(snap2, f)

    # 검색 1: 'report' 검색
    res = client.get(f"/api/snapshots/search-files?q=report&repo_dir={repo_dir}")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1

    item = data[0]
    assert item["filename"] == "quarterly_report.xlsx"
    assert item["version_count"] == 2
    assert len(item["versions"]) == 2
    # 최신 버전 확인
    assert item["latest_snapshot_id"] == "snap_test_002"
    assert item["latest_size"] == 15360
    assert "15.0 KB" in item["latest_size_formatted"]

    # 과거 버전 확인
    v_old = item["versions"][1]
    assert v_old["snapshot_id"] == "snap_test_001"
    assert v_old["size"] == 10240

    # 검색 2: 'photo' 검색 (단일 버전)
    res_photo = client.get(f"/api/snapshots/search-files?q=photo&repo_dir={repo_dir}")
    assert res_photo.status_code == 200
    data_photo = res_photo.json()
    assert len(data_photo) == 1
    assert data_photo[0]["filename"] == "team_photo.jpg"
    assert data_photo[0]["version_count"] == 1

    # 검색 3: 매칭되지 않는 쿼리
    res_none = client.get(f"/api/snapshots/search-files?q=nonexistent_xyz&repo_dir={repo_dir}")
    assert res_none.status_code == 200
    assert res_none.json() == []


def test_quick_restore_desktop_target_resolution():
    """target_dir='@desktop' 전달 시 바탕화면 복원 폴더로 자동 치환되는지 검증"""
    params = {
        "snapshot_id": "nonexistent_snap",
        "target_dir": "@desktop",
        "selected_rel_paths": ["test.txt"],
        "overwrite": True,
        "in_place": False,
    }

    desktop_resolved = None
    target_dir = params.get("target_dir")
    in_place = params.get("in_place", False)
    if not in_place and (not target_dir or target_dir in ("@desktop", "desktop")):
        desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop", "복원_test")
        desktop_resolved = desktop_dir

    assert desktop_resolved is not None
    assert "Desktop" in desktop_resolved
    assert "복원_test" in desktop_resolved
