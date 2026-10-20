# -*- coding: utf-8 -*-
"""
web/state.py - 대시보드 전역 실행 상태 공유 모듈

역할:
    web/app.py 는 1,700줄이 넘는 단일 파일이다. APIRouter 로 분리하더라도
    백업/복원/검증 작업이 공유하는 상태(current_task, task_lock)는
    라우터 간에 동일해야 한다. 이 모듈이 그 상태의 유일한 소유자가 된다.

이유:
    라우터 모듈이 각각 상태를 들면 라우터 간 상태가 갈라져
    "백업 중인데 복원이 동시에 시작되는" 사고가 난다.
    상태를 한 곳에 두면 라우터 추가/제거가 상태에 영향을 주지 않는다.

현재 실행 중인 작업은 동시에 하나뿐이며, 409 Conflict 로 거부된다.
"""

import threading
import time
from typing import Any, Dict


# 현재 실행 중인 작업의 상태.
# type: 'backup' | 'restore' | 'verify'
# running: 진행 중 여부
# progress: 진행 정보 (엔드포인트마다 필드가 조금씩 다르다)
# cancel_event: 취소 요청 시그널
# result: 완료 결과
# error: 실패 사유
current_task: Dict[str, Any] = {
    "type": None,
    "running": False,
    "progress": {},
    "cancel_event": None,
    "start_time": None,
    "result": None,
    "error": None,
}

# 작업 시작/종료 시의 원자적 전환을 위한 락
task_lock = threading.Lock()

# UI 로그 버퍼 (최근 항목만 보관)
_task_logs = []
_TASK_LOG_MAX = 200


def append_task_log(msg: str, level: str = "INFO") -> None:
    """대시보드 작업 로그를 버퍼에 추가한다."""
    entry = "%s [%-5s] %s" % (time.strftime("%H:%M:%S"), level, msg)
    _task_logs.append(entry)
    if len(_task_logs) > _TASK_LOG_MAX:
        del _task_logs[: len(_task_logs) - _TASK_LOG_MAX]


def get_task_logs(limit: int = 30) -> list:
    """최근 로그를 반환한다."""
    return list(_task_logs)[-limit:]


def try_begin_task(task_type: str, progress: Dict[str, Any]) -> bool:
    """
    새 작업을 시작한다. 이미 실행 중이면 False 를 반환한다.

    호출자는 False 일 때 HTTP 409 로 거부해야 한다.
    """
    with task_lock:
        if current_task["running"]:
            return False
        current_task.update({
            "type": task_type,
            "running": True,
            "progress": dict(progress),
            "cancel_event": threading.Event(),
            "start_time": time.time(),
            "result": None,
            "error": None,
        })
        return True


def finish_task(result: Any = None, error: Any = None) -> None:
    """작업 종료를 기록한다."""
    with task_lock:
        current_task["running"] = False
        current_task["result"] = result
        current_task["error"] = error


def snapshot_task_status() -> Dict[str, Any]:
    """UI 표시용 상태 스냅샷을 반환한다 (직렬화 가능한 dict)."""
    with task_lock:
        return {
            "type": current_task["type"],
            "running": current_task["running"],
            "progress": current_task["progress"],
            "logs": get_task_logs(),
            "start_time": current_task["start_time"],
            "result": current_task["result"],
            "error": current_task["error"],
        }


def request_cancel() -> bool:
    """취소를 요청한다. 취소 대상이 없으면 False."""
    with task_lock:
        evt = current_task.get("cancel_event")
        if not current_task["running"] or not evt:
            return False
        evt.set()
        return True
