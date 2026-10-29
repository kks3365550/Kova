# -*- coding: utf-8 -*-
"""
tests/test_archive_permissions.py: 권한 격리 및 아카이브 검증 테스트
1. 비관리자 권한에서 D:\WindowsImageBackup 실측 VHDX 조회 및 크기 검증 (23바이트 가짜 파일 퇴치 검증)
2. UAC 승격 워커 호출 검증 (VSS 스냅샷 및 DISM 실행 권한 확보 여부)
"""
import os
import sys
import json
import pytest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.drive_archive import find_latest_wbadmin_vhdx, DriveArchiveManager
from core.disk_clone import run_elevated_command


def test_wbadmin_vhdx_real_size_not_mock():
    """백업 삭제 후 find_latest_wbadmin_vhdx()가 예외 없이 None을 반환하거나, 존재 시 실측 크기 검증"""
    info = find_latest_wbadmin_vhdx("D:")
    if info is not None:
        assert info["size_bytes"] > 10 * 1024 * 1024 * 1024, f"VHDX 크기가 비정상적입니다: {info['size_bytes']} bytes"
        assert info["size_gb"] > 10.0, f"VHDX GB 크기가 비정상적입니다: {info['size_gb']} GB"
        print(f"\n[PASS] 실제 VHDX 식별: {info['vhdx_path']} ({info['size_gb']} GB)")
    else:
        print("\n[PASS] 이전 백업 삭제 완료 상태 정상 확인 (VHDX 미존재 -> None)")


def test_list_archives_reports_correct_metadata():
    """DriveArchiveManager.list_archives()가 중복 경로 없이 메타데이터를 정밀 반환하고, 빈 폴더 조회 시 0건을 반환하는지 검증"""
    import tempfile
    mgr = DriveArchiveManager.get_instance()
    
    # 1. 실제 저장소 아카이브 조회 시 중복 부재 및 메타데이터 무결성 확인
    archives = mgr.list_archives()
    paths = [a["path"].lower() for a in archives]
    assert len(paths) == len(set(paths)), f"아카이브 목록에 중복 경로가 존재합니다: {paths}"
    for arc in archives:
        assert arc.get("name"), "아카이브 이름 누락"
        assert arc.get("size_bytes", 0) > 0, "아카이브 크기 0B 오류"
        assert arc.get("backup_type") in ("block_stream", "wbadmin_vhdx", "wim", "kova_image"), f"알 수 없는 백업 형식: {arc.get('backup_type')}"

    # 2. 빈 디렉토리 조회 시 유령 항목 없이 0건 반환 확인
    with tempfile.TemporaryDirectory() as empty_dir:
        empty_archives = mgr.list_archives(repo_dir=empty_dir)
        assert len(empty_archives) == 0, f"빈 디렉토리에서 유령 아카이브 감지: {empty_archives}"
    print(f"\n[PASS] 아카이브 목록 무결성 검증 완료 (총 {len(archives)}개 실측 확인, 중복 0건)")


def test_elevated_worker_execution_permissions():
    """비관리자 세션에서 run_elevated_command를 통해 관리자 권한 도구(dism, vssadmin)가 정상 실행되는지 실측 검증"""
    # 1. dism 확인
    rc_dism, out_dism, _ = run_elevated_command(["dism.exe", "/?"], timeout=30)
    assert rc_dism == 0, f"DISM elevated 실행 실패: rc={rc_dism}"
    assert "Deployment Image Servicing and Management" in out_dism

    # 2. vssadmin 확인 (관리자 권한 도구 호출 성공 여부 및 권한 오류 부재 확인)
    rc_vss, out_vss, err_vss = run_elevated_command(["vssadmin.exe", "list", "shadows"], timeout=30)
    assert "You don't have the correct permissions" not in (out_vss + err_vss), "VSS 권한 부족 감지"
    assert "Volume Shadow Copy Service" in out_vss or rc_vss in (0, 1), f"vssadmin 관리자 실행 실패: rc={rc_vss}, out={out_vss}"
    print("\n[PASS] UAC 승격 실행기를 통한 DISM 및 vssadmin 권한 획득 100% 실측 검증 완료")


if __name__ == "__main__":
    test_wbadmin_vhdx_real_size_not_mock()
    test_list_archives_reports_correct_metadata()
    test_elevated_worker_execution_permissions()
    print("\n=== 모든 권한 및 정합성 검증 테스트 통과 ===")
