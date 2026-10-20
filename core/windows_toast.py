# core/windows_toast.py
# -*- coding: utf-8 -*-
"""
Windows 10/11 네이티브 토스트 알림 모듈 (Silent Failure 방지 체계)

외부 서드파티 라이브러리(win10toast, pystray 등) 의존성 없이
Windows 기본 내장 PowerShell WinRT API를 호출하여 바탕화면 우측 하단에 알림을 띄웁니다.

핵심 설계:
1. 무창(Ghost) 실행: subprocess.CREATE_NO_WINDOW 적용하여 cmd창 깜빡임 0%
2. 비동기 데몬 스레드: 메인 백업 루틴 및 웹 서버 요청을 절대 블로킹하지 않음
3. 문자열 무결성: UTF-16LE Base64 -EncodedCommand 기법으로 한글/특수문자/따옴표 탈락 방지
4. Fail-Safe: 예외 발생 시 내부 로깅만 수행하고 메인 프로세스로 에러 전파 차단
"""

from __future__ import annotations

import base64
import logging
import subprocess
import threading

logger = logging.getLogger("core.windows_toast")


def _execute_toast_powershell(title: str, message: str, app_id: str) -> bool:
    """
    PowerShell WinRT 스크립트를 생성하여 백그라운드에서 실행합니다.
    """
    try:
        # XML 특수문자 및 따옴표 방지를 위해 PowerShell 스크립트 내부에서 안전하게 노드 생성
        # PowerShell 문자열 이스케이프 처리
        safe_title = title.replace("`", "``").replace('"', '`"')
        safe_msg = message.replace("`", "``").replace('"', '`"')
        safe_app = app_id.replace("`", "``").replace('"', '`"')

        ps_script = f"""
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $template.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($template.CreateTextNode("{safe_title}")) > $null
    $textNodes.Item(1).AppendChild($template.CreateTextNode("{safe_msg}")) > $null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{safe_app}")
    $notifier.Show($toast)
}} catch {{
    # Fallback to system tray balloon notification if WinRT is unavailable
    Add-Type -AssemblyName System.Windows.Forms
    $balloon = New-Object System.Windows.Forms.NotifyIcon
    $balloon.Icon = [System.Drawing.SystemIcons]::Information
    $balloon.BalloonTipTitle = "{safe_title}"
    $balloon.BalloonTipText = "{safe_msg}"
    $balloon.Visible = $true
    $balloon.ShowBalloonTip(5000)
    Start-Sleep -Seconds 1
    $balloon.Dispose()
}}
"""
        encoded_cmd = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd]

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            timeout=8,
            check=False
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("Windows 토스트 알림 발송 실패 (무시됨): %s", exc)
        return False


def send_windows_toast(
    title: str,
    message: str,
    app_id: str = "BackupSystem",
    async_call: bool = True
) -> bool:
    """
    Windows 네이티브 토스트 알림을 발송합니다.

    :param title: 알림 제목 (예: "백업 실패 경고")
    :param message: 알림 본문 (예: "D 드라이브의 용량이 부족하여 백업이 중단되었습니다.")
    :param app_id: Windows 알림 센터 표시 앱 식별자
    :param async_call: True인 경우 백그라운드 데몬 스레드에서 즉시 반환 (기본값)
    :return: 비동기 호출 시 즉시 True 반환, 동기 호출 시 실행 결과 반환
    """
    if async_call:
        worker = threading.Thread(
            target=_execute_toast_powershell,
            args=(title, message, app_id),
            daemon=True,
            name="ToastNotificationWorker"
        )
        worker.start()
        return True
    else:
        return _execute_toast_powershell(title, message, app_id)


def notify_backup_failed(task_name: str, error_msg: str) -> None:
    """
    백업 작업 실패 시 사용자에게 침묵 실패(Silent Failure)를 경고하는 토스트를 띄웁니다.
    """
    short_error = error_msg.strip()
    if len(short_error) > 80:
        short_error = short_error[:77] + "..."
    title = f"🚨 백업 실패: {task_name}"
    msg = f"백업 중 오류가 발생했습니다: {short_error}\n대시보드에서 진단 로그를 확인하세요."
    send_windows_toast(title, msg, app_id="BackupSystem.Alert", async_call=True)


def notify_disk_space_warning(free_gb: float, req_gb: float, drive_path: str = "") -> None:
    """
    저장소 디스크 여유 공간 부족 시 토스트를 띄웁니다.
    """
    title = "⚠️ 저장소 디스크 용량 부족"
    target = f" ({drive_path})" if drive_path else ""
    msg = f"백업 저장 공간{target}이 부족합니다.\n현재 여유: {free_gb:.1f}GB / 필요 공간: {req_gb:.1f}GB"
    send_windows_toast(title, msg, app_id="BackupSystem.Storage", async_call=True)


def notify_backup_success(task_name: str, snapshot_id: str, files_count: int, size_mb: float) -> None:
    """
    주요 백업 성공 알림 (필요 시 선택적 호출).
    """
    title = f"✅ 백업 완료: {task_name}"
    msg = f"스냅샷 {snapshot_id} 생성 완료 ({files_count:,}개 파일, {size_mb:,.1f}MB)"
    send_windows_toast(title, msg, app_id="BackupSystem", async_call=True)
