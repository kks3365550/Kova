# -*- coding: utf-8 -*-
"""
core/system_power.py

Windows 전원 관리 및 백업 중 절전 모드 방지 (PowerSleepGuard)
장시간 대용량 백업이나 CLI 스케줄 백업 중 OS가 절전(Sleep) 상태로 진입하여
백업이 중단되거나 지연되는 현상을 물리적으로 방지합니다.
"""
import sys
import ctypes
import logging

logger = logging.getLogger("BackupSystem.power")

# Windows Kernel32 Execution State Flags
# Reference: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
ES_SYSTEM_REQUIRED = 0x00000001   # Forces the system to be in the working state by resetting system idle timer
ES_DISPLAY_REQUIRED = 0x00000002  # Forces the display to be on by resetting the display idle timer
ES_AWAYMODE_REQUIRED = 0x00000040 # Enables away mode for background processing
ES_CONTINUOUS = 0x80000000        # Informs system that the state being set should remain until next call


class PowerSleepGuard:
    """
    백업 또는 복원 작업 중 윈도우 OS가 절전(Sleep)/대기 모드로 진입하는 것을 차단하는 컨텍스트 매니저.
    작업 완료 또는 예외 발생 시(finally) 즉시 이전 기본 전원 정책으로 복원합니다.
    """
    def __init__(self, reason: str = "Backup operation in progress", keep_display_on: bool = False):
        self.reason = reason
        self.keep_display_on = keep_display_on
        self.active: bool = False

    def __enter__(self) -> "PowerSleepGuard":
        if sys.platform.startswith("win"):
            try:
                flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
                if self.keep_display_on:
                    flags |= ES_DISPLAY_REQUIRED
                prev_state = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
                if prev_state != 0:
                    self.active = True
                    logger.info("[PowerSleepGuard] 절전 방지 활성화: %s", self.reason)
                else:
                    logger.warning("[PowerSleepGuard] SetThreadExecutionState 반환값 0 (호출 실패)")
            except Exception as e:
                logger.warning("[PowerSleepGuard] 전원 상태 설정 실패: %s", e)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.active and sys.platform.startswith("win"):
            try:
                # ES_CONTINUOUS 단독 호출로 이전 시스템 상태 해제
                ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(ES_CONTINUOUS))
                self.active = False
                logger.info("[PowerSleepGuard] 절전 방지 해제 (정상 복구): %s", self.reason)
            except Exception as e:
                logger.warning("[PowerSleepGuard] 전원 상태 복구 실패: %s", e)
        return False
