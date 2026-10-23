# -*- coding: utf-8 -*-
"""install_namespace_logging: get_logger 를 쓰지 않는 모듈 로거도 backup.log 에 남는지 검증."""
import logging
import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import core.logging_setup as ls


class TestNamespaceLogging(unittest.TestCase):
    def setUp(self):
        self._saved = (ls.LOG_DIR, ls.BACKUP_LOG, ls._CONFIGURED, ls._HANDLER)
        self.tmp = tempfile.mkdtemp(prefix="kova_nslog_")
        ls.LOG_DIR = self.tmp
        ls.BACKUP_LOG = os.path.join(self.tmp, "backup.log")
        ls._CONFIGURED = False
        ls._HANDLER = None
        self._ns_handlers = {ns: list(logging.getLogger(ns).handlers) for ns in ls.LOG_NAMESPACES}

    def tearDown(self):
        for ns in ls.LOG_NAMESPACES:
            lg = logging.getLogger(ns)
            for h in list(lg.handlers):
                if h not in self._ns_handlers[ns]:
                    lg.removeHandler(h)
        if ls._HANDLER is not None:
            ls._HANDLER.close()
        ls.LOG_DIR, ls.BACKUP_LOG, ls._CONFIGURED, ls._HANDLER = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self):
        with open(ls.BACKUP_LOG, encoding="utf-8") as f:
            return f.read()

    def test_plain_module_loggers_reach_the_file(self):
        ls.install_namespace_logging()
        logging.getLogger("core.some_module").warning("한글 경고 A")
        logging.getLogger("BackupSystem.VSS").error("VSS 오류 B")
        text = self._read()
        self.assertIn("한글 경고 A", text)
        self.assertIn("VSS 오류 B", text)

    def test_install_is_idempotent_no_duplicate_lines(self):
        ls.install_namespace_logging()
        ls.install_namespace_logging()
        logging.getLogger("core.dup_check").warning("once-only")
        self.assertEqual(self._read().count("once-only"), 1)

    def test_get_logger_children_are_not_double_written(self):
        ls.install_namespace_logging()
        ls.get_logger("core.dual_check").warning("single-line")
        self.assertEqual(self._read().count("single-line"), 1)


if __name__ == "__main__":
    unittest.main()
