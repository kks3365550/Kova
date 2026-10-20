# -*- coding: utf-8 -*-
"""
core/logging_setup.py - 백업 프로세스 공통 로깅 설정

왜 이 모듈이 필요한가 (실측 근거)
----------------------------------
Windows 작업 스케줄러는 백업을 다음처럼 실행한다:

    "pythonw.exe" "cli_backup.py" --profile "..."

pythonw.exe 에는 콘솔이 없다. 표준출력/표준오류가 연결되어 있지 않으므로
`print()` 한 줄은 어디에도 남지 않는다. 2026-10-07 09:00 백업이
"정상 종료(Result=0)" 하면서도 로그 파일이 하나도 생기지 않은 것이
정확히 이 때문이다.

`logging.FileHandler` 는 표준출력과 무관하게 파일에 쓰이므로,
일정 실행 로그는 반드시 logging 로 남겨야 한다.

설계 결정
----------
- 파일 로그는 `logs/backup.log` 한 곳에만 쓴다.
  일정 실행과 수동 실행이 같은 파일에 시간순으로 쌓이므로,
  "어제 야간 백업이 왜 실패했나" 를 한 파일만 열어 확인할 수 있다.
- 5MB 를 넘으면 1개로 회전하고 이전 파일은 지운다(백업 디스크 보호).
- 예외는 스택 트레이스까지 남긴다. 백업 실패 원인은 대부분 예외 안쪽에 있다.

사용법
------
    import logging
    from core.logging_setup import get_logger
    log = get_logger(__name__)
    log.info("...")
"""

import os
import logging
import logging.handlers

# 로그 파일 경로 (프로젝트 루트/logs)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKUP_LOG = os.path.join(LOG_DIR, "backup.log")

# 회전 임계값
_MAX_BYTES = 5 * 1024 * 1024   # 5MB
_BACKUP_COUNT = 1              # 이전 파일 1개만 보관

# 모듈 레벨 싱글톤 핸들러 및 구성 플래그
_CONFIGURED = False
_HANDLER = None


def _get_shared_handler() -> logging.Handler:
    """
    공유 RotatingFileHandler를 생성하거나 반환한다.
    _CONFIGURED가 False로 리셋되면 이전 핸들러를 닫고 새로 생성한다 (테스트 격리 지원).
    """
    global _HANDLER, _CONFIGURED
    if _HANDLER is None or not _CONFIGURED:
        if _HANDLER is not None:
            try:
                _HANDLER.close()
            except Exception:
                pass
            _HANDLER = None

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError:
            # 로그 디스크를 만들 수 없으면 표준출력 기본 설정으로 대체
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
            _CONFIGURED = True
            return None

        _HANDLER = logging.handlers.RotatingFileHandler(
            BACKUP_LOG,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        _HANDLER.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        _CONFIGURED = True

    return _HANDLER


def setup_file_logging(name: str = "backup", level: int = logging.INFO) -> logging.Logger:
    """
    logs/backup.log 로 쓰는 로거를 구성한다.
    요청된 로거에 공유 핸들러를 부착하여 모든 모듈 로거가 파일에 정상 기록되도록 한다.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    handler = _get_shared_handler()
    if handler and handler not in logger.handlers:
        logger.addHandler(handler)

    return logger


def get_logger(name: str = "backup", level: int = logging.INFO) -> logging.Logger:
    """모듈 로거를 얻는다. 요청된 로거에 공유 파일 핸들러가 부착된다."""
    return setup_file_logging(name, level)


def log_path() -> str:
    """현재 백업 로그 파일 경로 (UI 표시용)."""
    return BACKUP_LOG