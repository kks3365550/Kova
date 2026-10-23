# -*- coding: utf-8 -*-
"""pytest 공통 설정: 테스트 실행이 실제 logs/backup.log 를 오염시키지 않게 한다.

테스트는 일부러 설치 실패, 서명 위조, 블롭 손상 같은 오류를 만든다. 로그 파일 경로를
임시 폴더로 돌리지 않으면 이 가짜 ERROR/CRITICAL 이 현장 백업 로그에 섞여 실제 장애
원인을 찾기 어렵고, 5MB 회전 때문에 실제 백업 이력도 밀려 지워진다.
"""
import os
import shutil
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import core.logging_setup as _ls  # noqa: E402

_REAL_LOG_DIR = _ls.LOG_DIR
_TMP_ROOT = tempfile.mkdtemp(prefix="kova_pytest_logs_")
_TMP_LOG_DIR = os.path.join(_TMP_ROOT, "logs")
os.makedirs(_TMP_LOG_DIR, exist_ok=True)

# 핸들러는 처음 쓰일 때 만들어지므로, 이미 만들어졌다면 닫고 다시 만들게 한다.
if _ls._HANDLER is not None:
    try:
        _ls._HANDLER.close()
    except Exception:
        pass
    _ls._HANDLER = None
_ls.LOG_DIR = _TMP_LOG_DIR
_ls.BACKUP_LOG = os.path.join(_TMP_LOG_DIR, "backup.log")
_ls._CONFIGURED = False


def pytest_sessionfinish(session, exitstatus):
    if _ls._HANDLER is not None:
        try:
            _ls._HANDLER.close()
        except Exception:
            pass
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
