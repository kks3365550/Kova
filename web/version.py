# -*- coding: utf-8 -*-
"""
web/version.py

버전 정보 및 업데이트 캐시 관리 모듈
"""
import os
from web.paths import BASE_DIR

def get_current_version() -> str:
    v_file = os.path.join(BASE_DIR, "VERSION")
    if os.path.exists(v_file):
        try:
            with open(v_file, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    try:
        from core import __version__
        return __version__
    except Exception:
        return "2.8.9"

VERSION = get_current_version()

_cached_update_info = {"checked_at": 0, "data": None}
