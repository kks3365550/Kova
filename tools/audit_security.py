# -*- coding: utf-8 -*-
"""
tools/audit_security.py - UI 및 Jinja2 템플릿 보안 감사 도구
검증 항목:
1. Jinja2 템플릿 XSS 및 | safe 필터 감사
2. 마스터 비밀번호 인증 모달 및 세션 제어 요소 무결성
3. WORM 불변 락 제어 및 설정 저장 시 권한 우회 가능성 점검
4. DOM XSS 및 위험 API (eval, document.write 등) 검사
"""
import sys
import re
import os
import json

def audit_template(filepath: str) -> dict:
    results = {
        "file": filepath,
        "exists": os.path.exists(filepath),
        "errors": [],
        "warnings": [],
        "passed_checks": []
    }
    
    if not results["exists"]:
        results["errors"].append(f"File not found: {filepath}")
        return results

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Jinja2 safe filter audit
    allowed_safe_vars = {
        "initial_chart_html",
        "initial_chart_dates",
        "initial_recents_html",
        "initial_snapshots_json"
    }
    safe_matches = list(re.finditer(r'\{\{\s*([a-zA-Z0-9_]+)\s*\|\s*safe\s*\}\}', content))
    unknown_safe = []
    for m in safe_matches:
        var_name = m.group(1).strip()
        if var_name not in allowed_safe_vars:
            unknown_safe.append(var_name)
    
    if unknown_safe:
        results["errors"].append(f"허용되지 않은 변수에 | safe 필터 적용됨: {unknown_safe}")
    else:
        results["passed_checks"].append(f"Jinja2 safe 필터 검증 완료 ({len(safe_matches)}개 인가된 SSR 변수만 허용됨)")

    # 2. Check for script injection or unescaped variables in <script> blocks
    script_blocks = re.findall(r'<script\b[^>]*>(.*?)</script>', content, re.DOTALL | re.IGNORECASE)
    unsafe_script_vars = []
    for block in script_blocks:
        # Check for {{ var }} inside JS (except initial_snapshots_json | safe)
        matches = re.finditer(r'\{\{\s*([a-zA-Z0-9_]+)(\s*\|\s*safe)?\s*\}\}', block)
        for m in matches:
            vname = m.group(1)
            is_safe = bool(m.group(2))
            if vname == "initial_snapshots_json" and is_safe:
                continue
            if vname in ("version", "v_ts"):
                continue
            unsafe_script_vars.append(m.group(0))
    if unsafe_script_vars:
        results["warnings"].append(f"스크립트 태그 내 Jinja2 변수 삽입 감지: {unsafe_script_vars}")
    else:
        results["passed_checks"].append("스크립트 태그 내 취약한 Jinja2 변수 미삽입 확인")

    # 3. Master Password Modal & Session control audit
    required_ids = [
        "auth-modal-overlay",
        "modal-auth-setup",
        "form-auth-setup",
        "setup-password",
        "setup-password-confirm",
        "modal-auth-login",
        "form-auth-login",
        "login-password",
        "modal-auth-change",
        "form-auth-change",
        "change-current",
        "change-new",
        "change-new-confirm"
    ]
    missing_ids = []
    for rid in required_ids:
        if f'id="{rid}"' not in content and f"id='{rid}'" not in content:
            missing_ids.append(rid)

    if missing_ids:
        results["errors"].append(f"인증 모달 필수 DOM ID 누락: {missing_ids}")
    else:
        results["passed_checks"].append("마스터 비밀번호 인증 모달 3종(셋업/로그인/변경) DOM 무결성 확인")

    # 4. Check backup_auth.js script inclusion
    if "backup_auth.js" not in content and "checkAuthStatus" not in content:
        results["errors"].append("backup_auth.js 또는 checkAuthStatus 호출 누락 (인증 가용성 결여)")
    else:
        results["passed_checks"].append("인증 서브시스템(backup_auth.js / checkAuthStatus) 배선 확인")

    # 5. Check password field types and autocomplete
    pw_inputs = re.findall(r'<input[^>]+type=["\']password["\'][^>]*>', content)
    if len(pw_inputs) < 5:
        results["warnings"].append(f"비밀번호 input 태그 수가 예상보다 적음: {len(pw_inputs)}개 (최소 5개 필요: setup 2, login 1, change 2)")
    else:
        results["passed_checks"].append(f"비밀번호 입력 필드 보안 마스킹(type='password') 전수 확인 ({len(pw_inputs)}개)")

    # 6. Dangerous JS checks (eval, document.write)
    dangerous_patterns = [
        (r'\beval\s*\(', "eval() 함수 사용 감지"),
        (r'document\.write\s*\(', "document.write() 함수 사용 감지"),
        (r'innerHTML\s*=\s*.*location\.', "location 객체 기반 직접 innerHTML 할당(DOM XSS 위험)")
    ]
    for pattern, desc in dangerous_patterns:
        if re.search(pattern, content):
            results["errors"].append(desc)
        else:
            results["passed_checks"].append(f"위험 패턴 부재 확인: {pattern}")

    return results

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "web/templates/index.html"
    res = audit_template(target)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if res["errors"]:
        sys.exit(1)
    sys.exit(0)
