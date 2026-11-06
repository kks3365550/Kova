# -*- coding: utf-8 -*-
"""
tests/test_rust_search_engine.py

Rust 기반 네이티브 백업본 초고속 검색 엔진(kova_fast_engine search)
및 한글 초성 검색, 스냅샷 버전 집계, API 연동 검증 테스트
"""
import os
import json
import pytest
from fastapi.testclient import TestClient

from core.rust_adapter import find_rust_engine_binary, search_snapshot_files_rust
from web.app import app

client = TestClient(app)


@pytest.fixture
def mock_search_repo(tmp_path):
    repo_dir = str(tmp_path / "mock_search_repo")
    snapshots_dir = os.path.join(repo_dir, "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)

    # 1. 이전 스냅샷 (v1)
    snap1 = {
        "id": "snap_custom_20261101_100000_aaa",
        "created_at": 1793500000.0,
        "iso_time": "2026-11-01T10:00:00.000000",
        "profile_name": "삼영 제품 규격서 백업",
        "source_root": "C:\\CompanyDocs",
        "entries": [
            {
                "rel_path": "haccp/규격서_불고기소스_v1.xlsx",
                "size": 20480,
                "mtime": 1793400000000,
                "is_dir": False,
                "blob_id": "blob_spec_v1",
            },
            {
                "rel_path": "daily/업무일지.docx",
                "size": 15000,
                "mtime": 1793450000000,
                "is_dir": False,
                "blob_id": "blob_journal_v1",
            },
        ],
    }
    with open(os.path.join(snapshots_dir, "snap_custom_20261101_100000_aaa.json"), "w", encoding="utf-8") as f:
        json.dump(snap1, f)

    # 2. 최신 스냅샷 (v2) - 규격서 수정 및 신규 파일 추가
    snap2 = {
        "id": "snap_custom_20261105_100000_bbb",
        "created_at": 1793850000.0,
        "iso_time": "2026-11-05T10:00:00.000000",
        "profile_name": "삼영 제품 규격서 백업",
        "source_root": "C:\\CompanyDocs",
        "entries": [
            {
                "rel_path": "haccp/규격서_불고기소스_v1.xlsx",
                "size": 25600,
                "mtime": 1793800000000,
                "is_dir": False,
                "blob_id": "blob_spec_v2",
            },
            {
                "rel_path": "finance/매출보고서_2026.xlsx",
                "size": 51200,
                "mtime": 1793810000000,
                "is_dir": False,
                "blob_id": "blob_sales_v1",
            },
        ],
    }
    with open(os.path.join(snapshots_dir, "snap_custom_20261105_100000_bbb.json"), "w", encoding="utf-8") as f:
        json.dump(snap2, f)

    return repo_dir


def test_rust_binary_discovered():
    """Rust kova_fast_engine 실행 파일이 정상 탐색되는지 검증"""
    binary = find_rust_engine_binary()
    assert binary is not None
    assert os.path.isfile(binary)


def test_rust_search_chosung_matching(mock_search_repo):
    """한글 초성 검색 (ㄱㄱㅅ -> 규격서) 동작 및 버전 집계 검증"""
    results = search_snapshot_files_rust(mock_search_repo, "ㄱㄱㅅ", max_snapshots=5, limit=10)
    assert results is not None
    assert len(results) >= 1

    item = results[0]
    assert item["filename"] == "규격서_불고기소스_v1.xlsx"
    assert item["relevance"] >= 75
    assert item["version_count"] == 2
    assert item["latest_snapshot_id"] == "snap_custom_20261105_100000_bbb"
    assert item["latest_size"] == 25600
    assert item["latest_size_formatted"] == "25.0 KB"
    assert "2026-11-05" in item["latest_time"]

    # 과거 버전 히스토리 검증
    versions = item["versions"]
    assert len(versions) == 2
    assert versions[0]["snapshot_id"] == "snap_custom_20261105_100000_bbb"
    assert versions[1]["snapshot_id"] == "snap_custom_20261101_100000_aaa"


def test_rust_search_exact_and_prefix(mock_search_repo):
    """일반 단어 및 접두사 검색 검증"""
    # 1. '매출보고서' 검색
    res_sales = search_snapshot_files_rust(mock_search_repo, "매출보고서", max_snapshots=5, limit=10)
    assert res_sales is not None
    assert len(res_sales) == 1
    assert res_sales[0]["filename"] == "매출보고서_2026.xlsx"

    # 2. 초성 검색 'ㅁㅊㅂㄱㅅ'
    res_chosung = search_snapshot_files_rust(mock_search_repo, "ㅁㅊㅂㄱㅅ", max_snapshots=5, limit=10)
    assert res_chosung is not None
    assert len(res_chosung) == 1
    assert res_chosung[0]["filename"] == "매출보고서_2026.xlsx"


def test_rust_search_non_matching(mock_search_repo):
    """일치하지 않는 검색어 처리"""
    results = search_snapshot_files_rust(mock_search_repo, "존재하지않는파일999", max_snapshots=5, limit=10)
    assert results == []


def test_api_snapshots_search_endpoint_with_rust(mock_search_repo):
    """웹 API (/api/snapshots/search-files)를 통한 초성 검색 End-to-End 검증"""
    resp = client.get(f"/api/snapshots/search-files?q=ㄱㄱㅅ&repo_dir={mock_search_repo}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["filename"] == "규격서_불고기소스_v1.xlsx"
    assert data[0]["version_count"] == 2
    assert data[0]["latest_snapshot_id"] == "snap_custom_20261105_100000_bbb"
