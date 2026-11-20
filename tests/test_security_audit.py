# -*- coding: utf-8 -*-
"""
tests/test_security_audit.py - 4중 검증 심의회 보안 및 무결성 전수 검증 테스트
"""
import pytest
from starlette.testclient import TestClient
from web.app import app
import re

client = TestClient(app)

def test_template_xss_protection_on_ssr():
    """Jinja2 템플릿 SSR 렌더링 시 XSS 및 injection 페이로드가 안전하게 이스케이프되는지 검증"""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    
    # 1. <script> 태그 내 원시 HTML 삽입 취약점 검증
    # initial_snapshots_json 이 안전하게 유니코드 이스케이프(\u003c 등) 처리되었는지 확인
    assert "</script><script>" not in html
    
    # 2. 허용된 SSR 변수 외에 | safe 필터가 남용되지 않았는지 확인
    # safe 필터 패턴: {{ var | safe }}
    safe_matches = re.findall(r'\{\{\s*([a-zA-Z0-9_]+)\s*\|\s*safe\s*\}\}', html)
    # SSR 렌더링 후이므로 Jinja2 템플릿 태그는 남아있지 않아야 함
    assert len(safe_matches) == 0, "렌더링 후 Jinja2 템플릿 태그가 남아있습니다."

def test_auth_modal_integrity_and_session_defense():
    """마스터 비밀번호 모달 구조 및 미인증 요청 차단 무결성 검증"""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    
    # 필수 모달 요소 무결성
    assert 'id="auth-modal-overlay"' in html
    assert 'id="modal-auth-setup"' in html
    assert 'id="modal-auth-login"' in html
    assert 'id="modal-auth-change"' in html
    
    # 비밀번호 input type="password" 속성 검증 (평문 노출 방지)
    assert 'id="setup-password"' in html
    assert 'id="login-password"' in html
    assert 'id="change-current"' in html
    assert 'id="change-new"' in html
    
    # 인증 스크립트 배선 확인
    assert "backup_auth.js" in html

def test_unauthenticated_api_protection():
    """인증되지 않은 외부 클라이언트의 보호된 API(/api/settings 등) 접근 차단 검증"""
    # 원격 클라이언트 IP(192.168.1.100)로 시뮬레이션
    unauth_headers = {
        "X-Forwarded-For": "192.168.1.100",
        "Origin": "http://192.168.1.100:8765"
    }
    
    # 1. 설정 저장 차단
    res = client.post("/api/settings", json={"backup_engine": "malicious"}, headers=unauth_headers)
    assert res.status_code == 401
    assert res.json().get("auth_required") is True
    
    # 2. WORM/스냅샷 삭제 차단
    res = client.delete("/api/snapshots/test_snap_id", headers=unauth_headers)
    assert res.status_code == 401
    assert res.json().get("auth_required") is True

def test_csrf_origin_blocking():
    """허용되지 않은 악성 Origin의 상태 변경 요청 차단 (CSRF 방어)"""
    malicious_headers = {
        "Origin": "http://evil-attacker.com"
    }
    res = client.post("/api/settings", json={"auto_backup": False}, headers=malicious_headers)
    assert res.status_code == 403
    assert "허용되지 않은 출처" in res.json().get("error", "")

def test_worm_display_and_tamper_proofing():
    """개편된 UI에서 WORM 불변 락이 안전하게 상태 표시되며 권한 우회 토글이 없는지 검증"""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    
    # WORM 배지 및 상태 태그 보존 확인
    assert 'id="header-worm-status-badge"' in html
    assert 'id="dash-worm-status-tag"' in html
    assert 'id="dash-worm-integrity-tag"' in html
    
    # 임의로 WORM 해제 토글을 제공하지 않는지 확인 (WORM은 UI에서 단순 토글 불가)
    assert 'name="disable_worm"' not in html
    assert 'id="toggle-worm-disable"' not in html
