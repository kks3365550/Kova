"""
tray_app.py
Windows 네이티브 시스템 트레이 & 토스트 알림 에이전트
- 순수 ctypes 기반 (외부 의존성 제로: pystray, Pillow 불필요)
- Windows 10/11 작업 표시줄 트레이 아이콘 상주
- 우클릭 팝업 메뉴 및 더블클릭 대시보드 자동 열기
- 백그라운드 태스크 완료/실패 시 Windows Native Balloon/Toast 알림 발송
"""

import os
import sys
import json
import time
import ctypes
import webbrowser
import threading
import subprocess
import urllib.request
import urllib.error
from ctypes import wintypes

# =========================================================================
# Windows API 상수 정의
# =========================================================================
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

# Shell_NotifyIconW 동작
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

# NotifyIcon 플래그
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

# Balloon/Toast 알림 아이콘
NIIF_NONE = 0x00000000
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003

# 메뉴 플래그
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020

# 메뉴 ID
IDM_OPEN_DASHBOARD = 1001
IDM_RUN_BACKUP = 1002
IDM_CHECK_STATUS = 1003
IDM_RESTART_SERVER = 1004
IDM_EXIT = 1005

SERVER_URL = "http://127.0.0.1:8765"


# =========================================================================
# NOTIFYICONDATAW 구조체 선언
# =========================================================================
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8)
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON)
    ]


# Window Procedure 함수 포인터 타입
WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON)
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


# Win32 API 함수 바인딩
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = wintypes.LPARAM


class BackupTrayApp:
    def __init__(self):
        self.class_name = "BackupSystemTrayWindowClass"
        self.hwnd = None
        self.nid = None
        self.running = True
        self.last_task_state = None  # 이전 백그라운드 태스크 상태 추적
        self._notif_lock = threading.Lock()
        self._wndproc = WNDPROC(self.wnd_proc)

    def _switch_to_default_desktop(self):
        """대화형 사용자 데스크톱(Default)으로 현재 스레드 데스크톱 전환"""
        try:
            h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_desk:
                user32.SetThreadDesktop(h_desk)
        except Exception:
            pass

    def run(self):
        # 대화형 데스크톱(작업 표시줄/트레이 영역)으로 스레드 연결 보장
        self._switch_to_default_desktop()

        # 윈도우 클래스 등록
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinstance
        wc.hIcon = user32.LoadIconW(None, wintypes.LPCWSTR(32512))  # IDI_APPLICATION
        wc.hCursor = user32.LoadCursorW(None, wintypes.LPCWSTR(32512))
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self.class_name
        wc.hIconSm = user32.LoadIconW(None, wintypes.LPCWSTR(32512))

        user32.RegisterClassExW(ctypes.byref(wc))

        # 메시지 전용 숨김 윈도우 생성
        self.hwnd = user32.CreateWindowExW(
            0, self.class_name, "BackupSystemTrayAgent",
            0, 0, 0, 0, 0,
            None, None, hinstance, None
        )

        if not self.hwnd:
            return

        # 트레이 아이콘 등록
        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_TRAYICON
        self.nid.hIcon = user32.LoadIconW(None, wintypes.LPCWSTR(32518))  # IDI_SHIELD (방패 아이콘)
        if not self.nid.hIcon:
            self.nid.hIcon = user32.LoadIconW(None, wintypes.LPCWSTR(32512))
        self.nid.szTip = "서버 & 시스템 백업 매니저"

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

        # 백그라운드 태스크 모니터링 스레드 시작
        threading.Thread(target=self._monitor_server_status, daemon=True).start()

        # 백업 서버 미실행 시 자동 무음 구동
        threading.Thread(target=self._ensure_server_running, daemon=True).start()

        # 최초 시작 알림
        self.show_notification(
            "백업 시스템 활성화",
            "백그라운드 백업 및 무결성 보호 에이전트가 가동되었습니다."
        )

        # 윈도우 메시지 루프
        msg = wintypes.MSG()
        while self.running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 종료 시 트레이 아이콘 제거
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))

    def show_notification(self, title: str, text: str, info_flag: int = NIIF_INFO):
        """Windows Native 풍선/토스트 알림 발송"""
        if not self.nid or not self.hwnd:
            return
        with self._notif_lock:
            self.nid.uFlags = NIF_INFO
            self.nid.szInfoTitle = title[:63]
            self.nid.szInfo = text[:255]
            self.nid.dwInfoFlags = info_flag
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            # 트레이 아이콘 이벤트
            if lparam == WM_LBUTTONDBLCLK:
                self.open_dashboard()
                return 0
            elif lparam == WM_RBUTTONUP:
                self.show_context_menu()
                return 0
        elif msg == WM_COMMAND:
            cmd_id = wparam & 0xFFFF
            if cmd_id == IDM_OPEN_DASHBOARD:
                self.open_dashboard()
            elif cmd_id == IDM_RUN_BACKUP:
                self.trigger_backup()
            elif cmd_id == IDM_CHECK_STATUS:
                self.check_status_now()
            elif cmd_id == IDM_RESTART_SERVER:
                self.restart_server()
            elif cmd_id == IDM_EXIT:
                self.exit_app()
            return 0
        elif msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            self.running = False
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def show_context_menu(self):
        """우클릭 팝업 메뉴 표시"""
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))

        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, IDM_OPEN_DASHBOARD, "🌐 웹 대시보드 열기")
        user32.AppendMenuW(hmenu, MF_STRING, IDM_RUN_BACKUP, "⚡ 지금 백업 시작")
        user32.AppendMenuW(hmenu, MF_STRING, IDM_CHECK_STATUS, "📊 백업 상태 확인")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, IDM_RESTART_SERVER, "🔄 백업 서버 재시작")
        user32.AppendMenuW(hmenu, MF_STRING, IDM_EXIT, "❌ 백업시스템 종료")

        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenuEx(hmenu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, pt.x, pt.y, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)

    def open_dashboard(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(SERVER_URL)
                return
        except Exception:
            pass
        try:
            webbrowser.open(SERVER_URL)
        except Exception:
            pass

    def trigger_backup(self):
        def _call():
            try:
                req = urllib.request.Request(
                    f"{SERVER_URL}/api/backup/run",
                    data=json.dumps({}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    if data.get("status") == "started" or data.get("success"):
                        self.show_notification("백업 시작", "새로운 시스템 백업 작업이 시작되었습니다.")
                    else:
                        self.show_notification("백업 알림", data.get("error", "백업 요청 실패"), NIIF_WARNING)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    try:
                        err_data = json.loads(e.read().decode('utf-8'))
                        detail = err_data.get("detail", "이미 다른 백업 또는 복원 작업이 실행 중입니다.")
                    except Exception:
                        detail = "이미 다른 작업이 진행 중입니다."
                    self.show_notification("백업 충돌", detail, NIIF_WARNING)
                else:
                    self.show_notification("백업 요청 실패", f"서버 오류 (HTTP {e.code})", NIIF_ERROR)
            except Exception as e:
                self.show_notification("백업 요청 실패", f"서버와 통신할 수 없습니다: {e}", NIIF_ERROR)

        threading.Thread(target=_call, daemon=True).start()

    def check_status_now(self):
        def _call():
            try:
                with urllib.request.urlopen(f"{SERVER_URL}/api/task/status", timeout=5) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    if data.get("running"):
                        raw_type = data.get("type", "backup")
                        type_map = {
                            "backup": "시스템 백업",
                            "restore": "시스템 복원",
                            "verify": "무결성 검증"
                        }
                        task_name = type_map.get(raw_type, raw_type or "작업")
                        progress = data.get("progress") or {}
                        percent = progress.get("percent")
                        curr_file = progress.get("current_file", "")
                        if percent is not None:
                            msg = f"현재 [{task_name}] 진행 중 ({percent}%)\n{curr_file}"[:250]
                        else:
                            msg = f"현재 [{task_name}] 진행 중입니다."
                        self.show_notification("작업 진행 중", msg, NIIF_INFO)
                    else:
                        last_err = data.get("error")
                        if last_err:
                            self.show_notification("상태: 오류 발생", f"최근 작업 오류: {last_err}"[:250], NIIF_WARNING)
                        else:
                            self.show_notification("정상 대기 중", "모든 백업 엔진이 정상 대기 상태입니다.")
            except Exception:
                self.show_notification("서버 상태", "백업 서버 응답 없음 (오프라인)", NIIF_WARNING)

        threading.Thread(target=_call, daemon=True).start()

    def _ensure_server_running(self):
        """서버가 실행 중이지 않으면 백그라운드로 start_silent.vbs를 실행합니다."""
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/api/task/status", timeout=2) as res:
                if res.status == 200:
                    return
        except Exception:
            pass

        base_dir = os.path.dirname(os.path.abspath(__file__))
        vbs_path = os.path.join(base_dir, "start_silent.vbs")
        if os.path.exists(vbs_path):
            try:
                subprocess.run(
                    ["wscript.exe", vbs_path],
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            except Exception:
                pass

    def restart_server(self):
        def _call():
            try:
                self.show_notification("서버 재시작", "백업 서버를 재기동합니다...", NIIF_INFO)
                base_dir = os.path.dirname(os.path.abspath(__file__))

                # 1. 기존 서버(8765 포트 / APP_DIR 프로세스) 안전 종료
                stop_bat = os.path.join(base_dir, "stop_backup_system.bat")
                if os.path.exists(stop_bat):
                    subprocess.run(
                        ["cmd.exe", "/c", stop_bat],
                        cwd=base_dir,
                        creationflags=0x08000000,
                        timeout=10
                    )
                time.sleep(1.5)

                # 2. start_silent.vbs 구동
                vbs_path = os.path.join(base_dir, "start_silent.vbs")
                if os.path.exists(vbs_path):
                    subprocess.Popen(
                        ["wscript.exe", vbs_path],
                        cwd=base_dir,
                        creationflags=0x08000000
                    )
                else:
                    py_exe = sys.executable
                    subprocess.Popen(
                        [py_exe, os.path.join(base_dir, "run.py"), "--silent"],
                        cwd=base_dir,
                        creationflags=0x08000000
                    )

                # 3. 서버 응답 대기 (최대 10초)
                restarted = False
                for _ in range(20):
                    time.sleep(0.5)
                    try:
                        with urllib.request.urlopen(f"{SERVER_URL}/api/task/status", timeout=1) as res:
                            if res.status == 200:
                                restarted = True
                                break
                    except Exception:
                        pass

                if restarted:
                    self.show_notification("재시작 완료", "백업 서버가 정상적으로 재시작되었습니다.", NIIF_INFO)
                else:
                    self.show_notification("재시작 확인", "서버 시작 명령을 전송했습니다.", NIIF_WARNING)

            except Exception as e:
                self.show_notification("재시작 실패", str(e), NIIF_ERROR)

        threading.Thread(target=_call, daemon=True).start()

    def exit_app(self):
        self.running = False
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

        def _cleanup():
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                stop_bat = os.path.join(base_dir, "stop_backup_system.bat")
                if os.path.exists(stop_bat):
                    subprocess.run(
                        ["cmd.exe", "/c", stop_bat],
                        cwd=base_dir,
                        creationflags=0x08000000,
                        timeout=10
                    )
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=False).start()

    def _monitor_server_status(self):
        """백그라운드에서 백업 진행/완료 상태를 모니터링하여 토스트 알림 발송"""
        while self.running:
            time.sleep(3)
            try:
                with urllib.request.urlopen(f"{SERVER_URL}/api/task/status", timeout=3) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    is_running = data.get("running", False)

                    # 실행 중 -> 완료로 상태 전이 감지
                    if self.last_task_state is True and not is_running:
                        error = data.get("error")
                        if error:
                            self.show_notification(
                                "백업 작업 실패",
                                f"작업 중 오류가 발생했습니다: {error}",
                                NIIF_ERROR
                            )
                        else:
                            self.show_notification(
                                "백업 작업 완료 🎉",
                                "새로운 증분 스냅샷이 성공적으로 저장되었습니다."
                            )

                    self.last_task_state = is_running
            except Exception:
                pass


if __name__ == "__main__":
    app = BackupTrayApp()
    app.run()
