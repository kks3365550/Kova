# -*- coding: utf-8 -*-
"""
tests/test_api_contract.py - 프런트엔드/백엔드 API 계약 회귀 테스트

라우터 분리(APIRouter) 이후 경로 계약이 유지되는지 검증한다.
프런트엔드가 호출하는 엔드포인트가 하나라도 사라지거나 이름이 바뀌면
대시보드가 조용히 실패하므로, 이를 CI 게이트로 막는다.

주의:
    FastAPI 0.141 의 include_router 는 lazy _IncludedRouter 로 등록된다.
    app.routes 를 직접 순회하면 분리된 라우터가 보이지 않으므로
    original_router 로 재귀해서 수집해야 한다.
"""
import os
import re
import sys
import glob
import json
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from web.app import app


def _collect_api_paths(routes, acc):
    """app.routes 에서 /api 경로를 재귀 수집한다 (lazy 라우터 대응)."""
    for r in routes:
        orig = getattr(r, "original_router", None)
        if orig is not None:
            _collect_api_paths(orig.routes, acc)
            continue
        path = getattr(r, "path_format", None) or getattr(r, "path", None)
        if path and path.startswith("/api"):
            acc.add(path)


def _norm(p):
    """경로 파라미터와 끝자리 슬래시를 정규화한다."""
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = p.split("?")[0]
    return p.rstrip("/") or "/"


class TestApiContract(unittest.TestCase):
    """프런트엔드가 요구하는 엔드포인트가 서버에 모두 존재하는지 검증"""

    @classmethod
    def setUpClass(cls):
        cls.server_paths = set()
        _collect_api_paths(app.routes, cls.server_paths)
        cls.server_norm = {_norm(p) for p in cls.server_paths}

    def test_frontend_endpoints_all_present(self):
        """프런트엔드가 호출하는 모든 엔드포인트가 서버에 존재해야 한다."""
        patterns = (
            glob.glob(os.path.join(BASE_DIR, "web", "static", "**", "*.js"), recursive=True)
            + glob.glob(os.path.join(BASE_DIR, "web", "templates", "**", "*.html"), recursive=True)
        )
        literal = re.compile(r"[`'\"](/api/[^`'\"]*)[`'\"]")
        interp = re.compile(r"\$\{[^}]*\}")

        required = set()
        for f in patterns:
            try:
                text = open(f, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for m in literal.finditer(text):
                required.add(_norm(interp.sub("{id}", m.group(1))))

        self.assertTrue(required, "프런트엔드에서 API 경로를 찾지 못함")

        missing = sorted(required - self.server_norm)
        self.assertFalse(
            missing,
            "프런트엔드가 호출하지만 서버에 없는 엔드포인트 %d개: %s"
            % (len(missing), missing),
        )

    def test_auth_router_registered(self):
        """인증 라우터가 분리 후에도 정상 등록되어야 한다."""
        for p in ("/api/auth/status", "/api/auth/login", "/api/auth/logout",
                  "/api/auth/setup", "/api/auth/change-password",
                  "/api/auth/toggle-bypass"):
            self.assertIn(_norm(p), self.server_norm,
                          "인증 라우터 분리 후 경로 누락: %s" % p)

    def test_core_endpoints_present(self):
        """백업/복원/스냅샷 등 핵심 엔드포인트가 유지되어야 한다."""
        for p in ("/api/system-info", "/api/backup/run", "/api/backup/cancel",
                  "/api/restore/run", "/api/snapshots", "/api/task/status",
                  "/api/verify/run", "/api/maintenance/prune"):
            self.assertIn(_norm(p), self.server_norm,
                          "핵심 엔드포인트 누락: %s" % p)

    def test_task_state_is_shared_module(self):
        """작업 상태가 web.state 단일 소유자인지 확인 (라우터 간 상태 이중화 방지)."""
        import web.app as app_mod
        import web.state as state_mod
        self.assertIs(app_mod.current_task, state_mod.current_task,
                      "current_task 가 두 곳에 존재하면 라우터 간 상태가 갈라진다")
        self.assertIs(app_mod.task_lock, state_mod.task_lock,
                      "task_lock 이 두 곳에 존재하면 동시성 보장이 깨진다")

    def test_task_state_helpers_work(self):
        """상태 헬퍼가 중복 실행을 실제로 거부하는지 확인."""
        from web.state import (
            try_begin_task, finish_task, snapshot_task_status, request_cancel
        )
        finish_task(result=None, error=None)

        self.assertTrue(try_begin_task("backup", {"percent": 0}))
        # 이미 실행 중이므로 두 번째 시작은 거부되어야 한다
        self.assertFalse(try_begin_task("restore", {"percent": 0}))

        status = snapshot_task_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["type"], "backup")

        self.assertTrue(request_cancel(), "실행 중이므로 취소 요청이 성립해야 한다")
        finish_task(result="done")
        self.assertFalse(snapshot_task_status()["running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
