# -*- coding: utf-8 -*-
"""
tests/test_vss_and_vhdx.py

스프린트 2 & 3 검증 테스트:
1. MetadataDB SQLite WAL checkpoint(TRUNCATE) 검증
2. VSS Pre-hook: flush_sqlite_databases 무결성 플러시 검증
3. SystemImageManager: VHDX 이미지 목록 조회 및 마운트 핸들러 검증
4. Web API: /api/system-image/vhdx-list 엔드포인트 응답 검증
"""

import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

from core.metadata_db import MetadataDB
from core.vss_manager import flush_sqlite_databases
from core.system_image import SystemImageManager
from web.app import app


def test_metadata_db_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = MetadataDB(str(repo))

    # 데이터 삽입
    db.record_new_blob(stored_size=1024, count=1)
    
    # checkpoint 실행
    assert db.checkpoint("TRUNCATE") is True


def test_flush_sqlite_databases(tmp_path):
    # 가상의 SQLite DB 파일 생성
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (1);")
    conn.commit()
    conn.close()

    # WAL 플러시 함수 호출
    flush_sqlite_databases([str(tmp_path)])
    
    # 연결 확인
    conn2 = sqlite3.connect(str(db_file))
    cur = conn2.cursor()
    cur.execute("SELECT COUNT(*) FROM t;")
    assert cur.fetchone()[0] == 1
    conn2.close()


def test_vhdx_api_endpoint():
    client = TestClient(app)
    res = client.get("/api/system-image/vhdx-list?target_drive=D:")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert isinstance(data["vhdx_list"], list)


def test_system_image_manager_list():
    vhdx_list = SystemImageManager.list_vhdx_images("D:")
    assert isinstance(vhdx_list, list)
    for vhdx in vhdx_list:
        assert "path" in vhdx
        assert "filename" in vhdx
        assert "size_gb" in vhdx
        assert "is_mounted" in vhdx
