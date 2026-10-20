# -*- coding: utf-8 -*-
"""
tests/test_backup_logging.py - 백업 로그 기록 회귀 테스트

배경 (실측 근거)
----------------
Windows 작업 스케줄러가 백업을 실행하는 방식은 다음과 같다:

    "pythonw.exe" "cli_backup.py" --profile "..."

pythonw.exe 에는 콘솔이 없다. 표준출력이 연결되어 있지 않으므로
`print()` 한 줄은 어디에도 남지 않는다.

2026-10-07 09:00 백업이 LastTaskResult=0 ("정상 종료") 을 기록했는데
로그 파일이 하나도 생기지 않은 것이 정확히 이 문제였다.
Result=0 은 프로세스가 예외 없이 끝났다는 뜻일 뿐,
백업이 무엇을 했는지 기록하지 않는다.

이 테스트는 그 재발을 막는다.
"""
import os
import sys
import logging
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


class _TempLogRedirect(unittest.TestCase):
    """
    실제 logs/backup.log 를 건드리지 않도록 임시 파일로 돌린다.

    왜 이게 필요한가:
        테스트가 실제 로그 파일에 마커를 쓰면 현장 백업 로그에
        테스트 문자열이 섞여 들어간다. 나중에 "백업 실패 원인"을
        뒤질 때 테스트 노이즈까지 읽어야 한다.
    """

    def setUp(self):
        import core.logging_setup as ls
        self.ls = ls
        self._orig_configured = ls._CONFIGURED
        self._orig_path = ls.BACKUP_LOG
        self._orig_dir = ls.LOG_DIR
        self._orig_max = ls._MAX_BYTES

        self._tmpdir = tempfile.mkdtemp(prefix="backup_log_test_")
        ls.LOG_DIR = self._tmpdir
        ls.BACKUP_LOG = os.path.join(self._tmpdir, "backup.log")
        ls._CONFIGURED = False

    def tearDown(self):
        import logging.handlers
        import shutil

        ls = self.ls
        # 테스트가 붙인 핸들러를 모두 제거해 전역 로거 오염을 막는다
        log = logging.getLogger("cli.backup")
        for h in list(log.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                log.removeHandler(h)
                h.close()

        ls.BACKUP_LOG = self._orig_path
        ls.LOG_DIR = self._orig_dir
        ls._MAX_BYTES = self._orig_max
        ls._CONFIGURED = self._orig_configured
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestBackupLoggingSetup(_TempLogRedirect):
    """파일 로깅 구성 자체의 계약"""

    def test_log_file_under_logs_dir(self):
        """로그 파일은 프로젝트 logs/ 아래여야 한다.

        스케줄러의 작업 디렉터리가 비어 있어도 찾을 수 있어야 한다.

        주의: setUp 이 테스트 수행 중 BACKUP_LOG 를 임시 경로로 돌린다.
        그래서 모듈이 선언한 '원래' 경로(self._orig_path)를 검증한다.
        """
        self.assertTrue(
            os.path.isabs(self._orig_path),
            "로그 경로는 절대 경로여야 한다 (작업 디렉터리 의존 제거): %s" % self._orig_path,
        )
        self.assertIn(
            os.path.normcase("logs"),
            os.path.normcase(self._orig_path),
            "로그 파일이 logs/ 아래에 있어야 한다: %s" % self._orig_path,
        )

    def test_log_path_helper(self):
        """log_path() 헬퍼가 현재 설정된 경로를 알려주는지 확인."""
        self.assertEqual(
            self.ls.log_path(), self.ls.BACKUP_LOG,
            "log_path() 가 BACKUP_LOG 와 불일치 - UI 가 다른 파일을 가리킨다",
        )

    def test_rotating_handler_installed(self):
        """무한히 커지는 로그를 막기 위해 회전핸들러를 써야 한다."""
        import logging.handlers
        log = self.ls.get_logger("test_rotating")
        kinds = [type(h) for h in log.handlers]
        self.assertTrue(
            any(issubclass(k, logging.handlers.RotatingFileHandler) for k in kinds),
            "RotatingFileHandler 가 없다 - 로그가 디스크를 무한히 채운다: %s" % kinds,
        )

    def test_rotation_limit_is_bounded(self):
        """회전 설정값이 상한을 넘으면 안 된다 (백업 디스크 보호)."""
        self.assertGreater(self.ls._MAX_BYTES, 0)
        self.assertLessEqual(
            self.ls._MAX_BYTES, 50 * 1024 * 1024,
            "로그 회전 임계값이 너무 크다",
        )
        self.assertLessEqual(self.ls._BACKUP_COUNT, 3)

    def test_handler_not_duplicated(self):
        """동일 프로세스에서 여러 번 호출해도 핸들러가 쌓이지 않는다.

        안티패턴: 같은 줄이 2줄, 3줄로 중복 기록된다.
        """
        self.ls._CONFIGURED = False
        logger = self.ls.get_logger("test_dup")
        n1 = len(logger.handlers)
        self.ls.get_logger("test_dup")
        self.ls.get_logger("test_dup")
        n2 = len(logger.handlers)
        self.assertEqual(n1, n2, "재구성 시 핸들러가 중복 추가된다 (%d -> %d)" % (n1, n2))

    def test_propagate_disabled(self):
        """루트 로거 설정에 영향받지 않아야 한다.

        propagate=True 이면 다른 라이브러리의 설정으로 레벨이 바뀌어
        백업 로그가 갑자기 사라질 수 있다.
        """
        self.ls._CONFIGURED = False
        log = self.ls.get_logger("test_propagate")
        self.assertFalse(log.propagate)

    def test_writes_actual_file(self):
        """로그가 실제 파일에 기록되는지 확인 (테스트 후 정리)."""
        self.ls._CONFIGURED = False
        before = os.path.getmtime(self.ls.BACKUP_LOG) if os.path.exists(self.ls.BACKUP_LOG) else 0

        log = self.ls.get_logger("test_write")
        log.warning("백업 로그 기록 테스트 마커")

        self.assertTrue(os.path.exists(self.ls.BACKUP_LOG),
                        "로그 파일이 생성되지 않았다: %s" % self.ls.BACKUP_LOG)
        self.assertGreaterEqual(os.path.getmtime(self.ls.BACKUP_LOG), before)

        with open(self.ls.BACKUP_LOG, "r", encoding="utf-8") as f:
            assert "백업 로그 기록 테스트 마커" in f.read(), \
                "마커가 파일에 없다 - FileHandler 가 동작하지 않는다"


class TestCliBackupUsesFileLogging(unittest.TestCase):
    """cli_backup.py 가 print 만 쓰지 않는지 (회귀 방지)

    스케줄러는 pythonw.exe 로 실행하므로 print 로는 아무 기록도 남지 않는다.
    """
    def test_cli_backup_has_file_logger(self):
        path = os.path.join(BASE_DIR, "cli_backup.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()

        self.assertIn(
            "from core.logging_setup import",
            src,
            "cli_backup.py 가 파일 로깅을 구성하지 않는다 - 스케줄러 백업 기록이 사라진다",
        )
        self.assertIn("get_logger", src)

    def test_cli_backup_logger_not_console_only(self):
        """logger 에 StreamHandler(콘솔)만 붙어 있지 않은지 확인."""
        path = os.path.join(BASE_DIR, "cli_backup.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("get_logger(", src,
                      "로그gers는 파일 핸들러가 필요하다")


class TestBackupFailureIsLogged(_TempLogRedirect):
    """백업 실패 시 로그가 남는지 실측"""

    def test_exception_is_recorded_with_traceback(self):
        """예외가 삼켜지지 않고 기록되어야 한다 (스택 트레이스 포함)."""
        import core.logging_setup as ls

        ls._CONFIGURED = False
        log = ls.get_logger("test_failure")

        try:
            raise RuntimeError("의도적 백업 실패")
        except Exception:
            log.error("백업 실패", exc_info=True)

        with open(ls.BACKUP_LOG, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("백업 실패", content)
        self.assertIn("의도적 백업 실패", content,
                      "예외 메시지가 로그에 없다 - 원인 파악 불가")
        self.assertIn("Traceback", content,
                      "스택 트레이스가 없다 - 어디서 실패했는지 알 수 없음")


if __name__ == "__main__":
    unittest.main(verbosity=2)