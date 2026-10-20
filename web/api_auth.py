# -*- coding: utf-8 -*-
"""
web/api_auth.py - 인증 API 라우터

마스터 비밀번호 설정/로그인/로그아웃/변경/로컬 우회 토글과 인증 상태 조회를 담당한다.
경로 계약은 분리 전과 동일하다 (/api/auth/*).

주의: 인증 미들웨어는 app.py 에서 유지된다. 앱 전체에 적용되어야 하며
      라우터 단위로 옮기면 정적 리소스 보호가 빠진다.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from core.auth import (
    get_auth_status, setup_master_password,
    verify_master_password, change_master_password, create_session,
    validate_session, revoke_session, set_localhost_bypass,
    check_login_rate_limit, record_login_failure, record_login_success
)

router = APIRouter()


# ==================== Auth Pydantic Models ====================
class AuthSetupRequest(BaseModel):
    password: str = Field(..., min_length=8, description="최소 8자 이상의 마스터 비밀번호")
    allow_localhost_bypass: bool = Field(default=True, description="로컬 루프백 접속 시 인증 우회 여부")

class AuthLoginRequest(BaseModel):
    password: str = Field(..., description="마스터 비밀번호")

class AuthChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="현재 비밀번호")
    new_password: str = Field(..., min_length=8, description="최소 8자 이상의 새 비밀번호")

class AuthBypassRequest(BaseModel):
    enabled: bool = Field(..., description="로컬 루프백 인증 우회 활성화 여부")



def _extract_token(request: Request) -> Optional[str]:
    token = request.cookies.get("backup_session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    return token


# ==================== Auth Endpoints ====================
@router.get("/api/auth/status")
async def auth_status(request: Request):
    client_ip = request.client.host if request.client else ""
    status = get_auth_status(client_ip)
    token = _extract_token(request)
    status["authenticated"] = bool(token and validate_session(token))
    return JSONResponse(content={"success": True, "data": status})

@router.post("/api/auth/setup")
async def auth_setup(req: AuthSetupRequest):
    try:
        setup_master_password(req.password, req.allow_localhost_bypass)
        return JSONResponse(content={"success": True, "message": "마스터 비밀번호가 성공적으로 설정되었습니다."})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": f"설정 중 오류: {str(e)}"})

@router.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    locked, remaining_sec = check_login_rate_limit(client_ip)
    if locked:
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "error": f"로그인 시도 횟수를 초과했습니다. {remaining_sec}초 후 다시 시도하세요.",
                "locked": True,
                "remaining_sec": remaining_sec
            }
        )
    if not verify_master_password(req.password):
        failures, is_now_locked = record_login_failure(client_ip)
        err_msg = "비밀번호가 일치하지 않습니다."
        if is_now_locked:
            err_msg += " (5회 연속 실패: 5분간 로그인이 제한됩니다.)"
        else:
            err_msg += f" (실패 {failures}/5회)"
        return JSONResponse(status_code=401, content={"success": False, "error": err_msg})
    
    record_login_success(client_ip)
    token = create_session()
    resp = JSONResponse(content={"success": True, "token": token, "message": "로그인 성공"})
    resp.set_cookie(
        key="backup_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,
        path="/"
    )
    return resp

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = _extract_token(request)
    if token:
        revoke_session(token)
    resp = JSONResponse(content={"success": True, "message": "로그아웃 성공"})
    resp.delete_cookie("backup_session", path="/")
    return resp

@router.post("/api/auth/change-password")
async def auth_change_password(req: AuthChangePasswordRequest):
    try:
        change_master_password(req.old_password, req.new_password)
        return JSONResponse(content={"success": True, "message": "비밀번호가 성공적으로 변경되었습니다."})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": f"변경 중 오류: {str(e)}"})

@router.post("/api/auth/toggle-bypass")
async def auth_toggle_bypass(req: AuthBypassRequest, request: Request):
    client_ip = request.client.host if request.client else ""
    token = _extract_token(request)
    is_authed = bool(token and validate_session(token))
    is_localhost = get_auth_status(client_ip).get("is_localhost", False)
    if not is_authed and not is_localhost:
        return JSONResponse(status_code=401, content={"success": False, "error": "인증이 필요한 작업입니다."})
    try:
        set_localhost_bypass(req.enabled)
        return JSONResponse(content={"success": True, "message": f"로컬 루프백 자동 우회 설정이 {'활성화' if req.enabled else '비활성화'}되었습니다."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": f"설정 변경 중 오류: {str(e)}"})
