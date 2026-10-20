# -*- coding: utf-8 -*-
"""
tests/test_storage_health.py

스프린트 1 검증: 스토리지 SMART 헬스 및 볼륨 상태 진단 모듈 테스트
"""

import pytest
from core.storage_health import get_system_storage_health, get_volumes_info, get_physical_disks_info
from core.config import DEFAULT_PROFILE

def test_default_profile_has_smart_excludes():
    excludes = DEFAULT_PROFILE.get("exclude_patterns", [])
    assert "pagefile.sys" in excludes
    assert "hiberfil.sys" in excludes
    assert "$Recycle.Bin" in excludes
    assert "node_modules" in excludes
    assert "*.tmp" in excludes

def test_get_volumes_info():
    vols = get_volumes_info()
    assert isinstance(vols, list)
    assert len(vols) > 0
    first_vol = vols[0]
    assert "drive" in first_vol
    assert "free_percent" in first_vol
    assert "status" in first_vol
    assert first_vol["status"] in ["healthy", "warning", "critical"]

def test_get_system_storage_health():
    res = get_system_storage_health(force_refresh=True)
    assert res["success"] is True
    assert "overall_status" in res
    assert res["overall_status"] in ["healthy", "warning", "critical"]
    assert "physical_disks" in res
    assert "volumes" in res
    assert "summary" in res
    assert res["summary"]["total_volumes"] > 0
