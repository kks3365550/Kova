# -*- coding: utf-8 -*-
"""
tests/test_deleted_image_cache_purge.py

검증 시나리오:
1. SystemImageManager.list_vhdx_images:
   - 캐시에 남아있더라도 실제 디스크에서 삭제된 VHDX 파일은 리스트에 나타나지 않아야 함.
   - 캐시 파일 저장 시 실존하는 파일만 보관되어야 함.
2. DriveArchiveManager.list_archives & delete_archive:
   - 캐시에 기록된 아카이브가 삭제된 경우 list_archives에 부활하지 않아야 함.
   - delete_archive 호출 시 실제 파일 및 메타데이터 삭제뿐 아니라 캐시 파일에서도 즉시 제거되어야 함.
"""

import os
import json
from core.system_image import SystemImageManager
from core.drive_archive import DriveArchiveManager


def test_system_image_manager_cache_purge(tmp_path, monkeypatch):
    wib_dir = tmp_path / "WindowsImageBackup"
    wib_dir.mkdir()
    
    # 가상의 VHDX 생성
    vhdx_file = wib_dir / "test_image.vhdx"
    vhdx_file.write_text("dummy vhdx content")

    # 가상의 캐시 디렉터리 및 캐시 파일 모킹
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    cache_path = str(ai_dir / "vhdx_cache.json")

    # 캐시에 실존 파일 + 이미 삭제된 유령 파일 2개 작성
    ghost_path = str(wib_dir / "ghost_deleted.vhdx")
    initial_cache = [
        {
            "path": str(vhdx_file),
            "filename": "test_image.vhdx",
            "size_bytes": 1024,
            "size_gb": 0.0,
            "modified_at": "2026-10-18 00:00:00",
            "is_mounted": False,
            "mount_drive": None
        },
        {
            "path": ghost_path,
            "filename": "ghost_deleted.vhdx",
            "size_bytes": 1024,
            "size_gb": 0.0,
            "modified_at": "2026-10-17 00:00:00",
            "is_mounted": False,
            "mount_drive": None
        }
    ]
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(initial_cache, f)

    # os.path.dirname 모킹 등을 통해 해당 캐시 경로 사용 유도
    original_join = os.path.join
    def mock_join(*args):
        if len(args) == 3 and args[1] == ".ai" and args[2] == "vhdx_cache.json":
            return cache_path
        return original_join(*args)

    monkeypatch.setattr(os.path, "join", mock_join)

    # 1. list_vhdx_images 실행 -> 유령 파일은 결과에 없어야 함
    results = SystemImageManager.list_vhdx_images(str(tmp_path))
    paths = [r["path"] for r in results]
    assert str(vhdx_file) in paths
    assert ghost_path not in paths

    # 2. 캐시 파일에도 유령 파일이 정제(Purge)되어 저장되었는지 확인
    with open(cache_path, "r", encoding="utf-8") as f:
        saved_cache = json.load(f)
    saved_paths = [c["path"] for c in saved_cache]
    assert str(vhdx_file) in saved_paths
    assert ghost_path not in saved_paths

    # 3. 실존하던 VHDX 파일마저 물리적으로 삭제
    vhdx_file.unlink()
    assert not os.path.exists(str(vhdx_file))

    # 4. 재조회 시 결과는 0개여야 하며 캐시에서도 완전히 사라져야 함
    results2 = SystemImageManager.list_vhdx_images(str(tmp_path))
    assert len(results2) == 0

    with open(cache_path, "r", encoding="utf-8") as f:
        saved_cache2 = json.load(f)
    assert len(saved_cache2) == 0


def test_drive_archive_manager_delete_and_cache_purge(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()

    # 1. 테스트용 아카이브 및 메타데이터 파일 생성
    qblk_file = archive_dir / "System_Drive_C_test.qblk"
    qblk_file.write_text("dummy block data")
    meta_file = archive_dir / "System_Drive_C_test.qblk.meta.json"
    meta_file.write_text(json.dumps({
        "volume_serial_number": "1234-5678",
        "created_at": "2026-10-18 12:00:00",
        "is_verified": True
    }))

    # 캐시 파일 모킹
    cache_file = archive_dir / ".archives_cache.json"
    cache_data = {
        "archives": [
            {
                "name": "System_Drive_C_test.qblk",
                "path": str(qblk_file),
                "modified_at": "2026-10-18 12:00:00"
            },
            {
                "name": "Deleted_Ghost.qblk",
                "path": str(archive_dir / "Deleted_Ghost.qblk"),
                "modified_at": "2026-10-17 12:00:00"
            }
        ]
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache_data, f)

    mgr = DriveArchiveManager.get_instance()

    # list_archives 호출 시 실존하지 않는 Deleted_Ghost는 목록에 나타나지 않아야 함
    archives = mgr.list_archives(repo_dir=str(archive_dir))
    archive_names = [a["name"] for a in archives]
    assert "System_Drive_C_test.qblk" in archive_names
    assert "Deleted_Ghost.qblk" not in archive_names

    # delete_archive 호출
    res = mgr.delete_archive(str(qblk_file))
    assert res["success"] is True
    assert not os.path.exists(str(qblk_file))
    assert not os.path.exists(str(meta_file))

    # 삭제 후 캐시 파일 확인 및 재조회 시 목록이 비어있어야 함
    archives_after = mgr.list_archives(repo_dir=str(archive_dir))
    assert len(archives_after) == 0


def test_system_image_delete_vhdx_api(tmp_path):
    from fastapi.testclient import TestClient
    from web.app import app

    client = TestClient(app)

    # 1. 테스트용 VHDX 파일 생성
    vhdx_dir = tmp_path / "WindowsImageBackup" / "TestPC"
    vhdx_dir.mkdir(parents=True)
    vhdx_file = vhdx_dir / "test_backup.vhdx"
    vhdx_file.write_text("dummy vhdx binary content")
    assert os.path.exists(str(vhdx_file))

    # 2. DELETE /api/system-image/delete 호출
    res = client.request("DELETE", "/api/system-image/delete", json={"vhdx_path": str(vhdx_file)})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert not os.path.exists(str(vhdx_file))

    # 3. 잘못된 경로 및 비 VHDX 파일 거부 검증
    res_bad = client.request("DELETE", "/api/system-image/delete", json={"vhdx_path": "C:\\Windows\\System32\\calc.exe"})
    assert res_bad.status_code == 200
    assert res_bad.json()["success"] is False

