#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emergency_restore/emergency_restore.py - 긴급 재해 복구 엔진
독립 실행 가능하며, 루트의 disaster_recovery.py와 완벽 호환됩니다.
"""

import os
import sys

# 상위 디렉터리 경로 추가
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# disaster_recovery.py 엔진 로드
try:
    from disaster_recovery import main
except ImportError:
    # 단독 분리된 환경일 경우 현재 디렉터리 기반 실행
    dr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "disaster_recovery.py")
    if os.path.exists(dr_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location("disaster_recovery", dr_path)
        dr_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dr_mod)
        main = dr_mod.main
    else:
        raise

if __name__ == "__main__":
    main()
