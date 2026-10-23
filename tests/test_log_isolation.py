# -*- coding: utf-8 -*-
"""테스트 세션이 실제 logs/backup.log 를 쓰지 않는지 확인한다."""
import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import core.logging_setup as ls


def test_backup_log_is_outside_project_logs_dir():
    real = os.path.normcase(os.path.join(BASE_DIR, "logs"))
    assert not os.path.normcase(ls.BACKUP_LOG).startswith(real + os.sep)


def test_logging_during_tests_does_not_touch_real_log():
    real_log = os.path.join(BASE_DIR, "logs", "backup.log")
    before = os.path.getsize(real_log) if os.path.exists(real_log) else 0
    ls.get_logger("core.isolation_probe").error("isolation probe")
    logging.getLogger("BackupSystem").error("isolation probe 2")
    after = os.path.getsize(real_log) if os.path.exists(real_log) else 0
    assert after == before
