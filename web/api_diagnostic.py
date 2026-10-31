# -*- coding: utf-8 -*-
"""
web/api_diagnostic.py

시스템 종합 진단 로그 패키징 (ZIP) 및 상태 조회 APIRouter 모듈
"""

import time
from typing import Optional
from fastapi import APIRouter, Response, Query
from fastapi.responses import JSONResponse

from core.diagnostic import build_diagnostic_package, get_diagnostic_quick_overview

router = APIRouter()


@router.get("/api/system/diagnostic-bundle")
def download_diagnostic_bundle(repo_dir: Optional[str] = Query(None, description="특정 저장소 지정 (선택사항)")):
    """
    시스템 메타데이터, 하드웨어 SMART, 스냅샷 통계, 마스킹된 설정 및 최근 로그를
    포함하는 원클릭 진단 ZIP 패키지를 생성하여 다운로드 스트림으로 반환합니다.
    """
    ts_str = time.strftime("%Y%m%d_%H%M%S")
    zip_filename = f"Kova_Diagnostic_{ts_str}.zip"
    
    zip_bytes = build_diagnostic_package(repo_dir=repo_dir)
    
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
            "Content-Length": str(len(zip_bytes)),
            "Cache-Control": "no-store, no-cache, must-revalidate",
        }
    )


@router.get("/api/system/diagnostic-overview")
def get_diagnostic_overview(repo_dir: Optional[str] = Query(None, description="특정 저장소 지정 (선택사항)")):
    """
    ZIP 다운로드 전 웹 UI에서 진단 요약 및 텍스트 보고서를 미리 확인할 수 있는 개요 API
    """
    overview = get_diagnostic_quick_overview(repo_dir=repo_dir)
    return JSONResponse(content=overview)
