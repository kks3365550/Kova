# -*- coding: utf-8 -*-
"""
web/api_bitlocker.py

Windows BitLocker 및 장치 암호화 상태 감지, 복구키 조회 및 안전 백업 파일 저장 APIRouter
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from core.bitlocker import get_bitlocker_info, export_bitlocker_guide_file

router = APIRouter()


@router.get("/api/system/bitlocker-status")
def api_get_bitlocker_status(drive: str = Query("C:", description="확인할 드라이브 문자")):
    """C: 또는 지정 드라이브의 BitLocker 암호화 상태 및 복구 가이드 정보 반환"""
    data = get_bitlocker_info(drive)
    return JSONResponse(content={"ok": True, "data": data})


@router.post("/api/system/bitlocker-save-guide")
def api_save_bitlocker_guide():
    """사용자 바탕화면에 BitLocker 긴급 복구키 안내 텍스트 파일을 안전하게 저장"""
    try:
        saved_path = export_bitlocker_guide_file()
        return JSONResponse(content={
            "ok": True,
            "saved_path": saved_path,
            "message": f"바탕화면에 복구 키 보관 파일이 성공적으로 저장되었습니다:\n{saved_path}"
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)
