import os
import sys
import time
import socket
import webbrowser
import threading
import subprocess
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Handle headless execution where stdio might be None (Critical for pythonw.exe)
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
try:
    os.makedirs(_log_dir, exist_ok=True)
    _server_log = open(os.path.join(_log_dir, "server.log"), "a", encoding="utf-8", buffering=1)
except Exception:
    _server_log = None

if sys.stdin is None:
    try:
        sys.stdin = open(os.devnull, 'r')
    except Exception:
        pass
if _server_log:
    sys.stdout = _server_log
    sys.stderr = _server_log

# ---------------------------------------------------------------------------
# Preflight: Ensure Administrative Privileges (VSS / DISM / Disk Clone)
# ---------------------------------------------------------------------------
def _check_and_elevate():
    """백업시스템은 VSS 스냅샷, 디스크 복제, DISM 이미지 캡처를 위해 관리자 권한이 필수입니다.
    비관리자 권한으로 실행된 경우 Windows UAC(runas)를 통해 관리자 권한으로 자동 승격 재기동합니다."""
    try:
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin() != 0:
            return True
    except Exception:
        return True

    if "--elevated-attempt" in sys.argv:
        print("[!] 관리자 권한 획득에 실패했습니다. 관리자 권한으로 실행해 주십시오.", file=sys.stderr)
        return False

    args = [arg for arg in sys.argv if arg != "--elevated-attempt"]
    args.append("--elevated-attempt")
    params = " ".join([f'"{a}"' if " " in a else a for a in args])

    try:
        import ctypes
        show_cmd = 0 if "--silent" in sys.argv else 1
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            os.path.dirname(os.path.abspath(__file__)),
            show_cmd
        )
        if ret > 32:
            sys.exit(0)
    except Exception as e:
        print(f"[!] 관리자 권한 승격 요청 실패: {e}", file=sys.stderr)
    return False

_check_and_elevate()


# ---------------------------------------------------------------------------
# Preflight: Check for interrupted OTA update transaction (Self-Healing)
# ---------------------------------------------------------------------------
_app_base_dir = os.path.dirname(os.path.abspath(__file__))
try:
    from core.updater_v2.transaction import check_and_recover_preflight
    _rec_ok, _rec_msg = check_and_recover_preflight(_app_base_dir)
    if not _rec_ok:
        print(f"[CRITICAL FAIL-CLOSED] 비정상 중단된 업데이트 복구 실패: {_rec_msg}", file=sys.stderr)
        sys.exit(1)
    elif "restored" in _rec_msg.lower():
        print(f"[*] [Self-Healing] {_rec_msg}")
except Exception as _rec_e:
    print(f"[CRITICAL FAIL-CLOSED] Preflight 복구 검사 중 예외 발생: {_rec_e}", file=sys.stderr)
    sys.exit(1)

def ensure_dependencies():
    required = [
        "uvicorn",
        "fastapi",
        "cryptography",
        "psutil",
        "jinja2",
        "requests",
        "pydantic",
        "zstandard",
        "multipart",
        "googleapiclient",
        "google.auth",
        "google_auth_oauthlib",
    ]
    missing = []
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    
    if missing:
        req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        if os.path.exists(req_file):
            print(f"[*] 필수 패키지({', '.join(missing)})가 누락되어 자동으로 설치합니다...")
            try:
                cmd = [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"]
                # Add --break-system-packages if running under uv / PEP 668 managed python
                try:
                    subprocess.check_call(cmd + ["--break-system-packages"])
                except Exception:
                    subprocess.check_call(cmd)
            except Exception as e:
                print(f"[!] 필수 패키지 자동 설치 경고 (계속 진행 시도): {e}")

ensure_dependencies()
import uvicorn
from core.config import ConfigManager

def is_port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False

def find_available_port(start_port: int, max_tries: int = 50) -> int:
    for offset in range(max_tries):
        candidate = start_port + offset
        if 1 <= candidate <= 65535 and not is_port_in_use(candidate):
            return candidate
    return -1

ACTIVE_PORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "active_port.txt")

def get_active_port(fallback_port: int) -> int:
    try:
        if os.path.exists(ACTIVE_PORT_FILE):
            with open(ACTIVE_PORT_FILE, "r", encoding="utf-8") as f:
                p = int(f.read().strip())
                if is_port_in_use(p):
                    return p
    except Exception:
        pass
    return fallback_port

def set_active_port(port: int):
    try:
        os.makedirs(os.path.dirname(ACTIVE_PORT_FILE), exist_ok=True)
        with open(ACTIVE_PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass

_server_mutex_handle = None

def acquire_single_instance_mutex(is_silent: bool, port: int) -> bool:
    """
    Windows Named Mutex를 사용하여 단일 인스턴스를 엄격히 보장합니다.
    이미 인스턴스가 존재할 경우:
      - is_silent가 False(사용자가 직접 실행)이면 기존 대시보드 브라우저를 띄우고 종료
      - is_silent가 True(부팅 시 백그라운드 자동 기동)이면 브라우저도 띄우지 않고 조용히 종료
    """
    global _server_mutex_handle
    if not sys.platform.startswith("win"):
        return True

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        MUTEX_NAME = "Global\\BackupSystem_Server_SingleInstance_Mutex"
        mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        last_error = kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183

        if last_error == ERROR_ALREADY_EXISTS:
            if not is_silent:
                active_p = get_active_port(port)
                open_browser(active_p)
            if mutex:
                kernel32.CloseHandle(mutex)
            sys.exit(0)

        _server_mutex_handle = mutex
        return True
    except Exception:
        # Mutex 생성 실패 시 Fail-Open (서버 기동 계속 진행)
        return True

def open_browser(port: int):
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if is_port_in_use(port):
            break
        time.sleep(0.1)

    # 1. Open via Windows native ShellExecute (Strictly respects Windows Default Browser)
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)
            return
    except Exception:
        pass

    # 2. Safe fallback to standard webbrowser library
    try:
        webbrowser.open(url)
    except Exception:
        pass

def main():
    settings = ConfigManager.get_settings()
    port = settings.get("server_port", 8765)
    host = settings.get("server_host", "0.0.0.0")

    # CLI 인자: --port <number> 지원 (지정 시 우선 적용)
    try:
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
    except Exception:
        pass

    # CLI 인자 검사: --silent 가 있으면 브라우저 자동 오픈 억제 (부팅 무음 모드)
    is_silent = "--silent" in sys.argv or "-s" in sys.argv
    auto_open = False if is_silent else settings.get("auto_open_browser", True)

    # 1. Windows Named Mutex 기반 단일 인스턴스 보호
    # (기존 인스턴스 존재 시: is_silent=False면 기존 실행 포트 브라우저 오픈 후 exit, is_silent=True면 즉시 exit)
    acquire_single_instance_mutex(is_silent=is_silent, port=port)

    # 2. 타 프로그램 포트 점유 여부 확인 및 자동 우회 (Fallback)
    if is_port_in_use(port):
        original_port = port
        port = find_available_port(start_port=port, max_tries=50)
        if port == -1:
            err_msg = f"기본 포트 {original_port} 및 대체 포트(최대 50개)가 모두 점유되어 서버를 시작할 수 없습니다."
            print(f"[!] {err_msg}", file=sys.stderr)
            if not is_silent:
                show_error_dialog("백업시스템 포트 충돌", err_msg)
            return

        print(f"[!] 알림: 기본 포트({original_port})가 다른 프로그램에 의해 사용 중입니다.")
        print(f"[*] 사용 가능한 포트({port})(으)로 자동 우회하여 백업 서버를 시작합니다.")

    set_active_port(port)

    # 3. 서버 시작 준비
    print("=" * 60)
    print("  백업 매니저 시스템 (Backup System Manager)")
    print("=" * 60)
    print(f"[*] 백업 서버를 시작합니다 (포트: {port}, Silent: {is_silent})...")
    print(f"[*] 접속 주소: http://127.0.0.1:{port}")
    print("=" * 60)

    config = uvicorn.Config(
        "web.app:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False
    )
    server = uvicorn.Server(config)

    # 4. Self-Gated Browser Launch: 서버가 실제로 LISTEN하기 시작하면 브라우저 오픈
    if auto_open:
        def _wait_and_open():
            # Uvicorn이 소켓을 bind/listen할 때까지 대기 (최대 10초)
            for _ in range(100):
                if is_port_in_use(port):
                    break
                time.sleep(0.1)
            open_browser(port)

        threading.Thread(target=_wait_and_open, daemon=True).start()

    server.run()

def show_error_dialog(title: str, message: str):
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10 | 0x10000)
    except Exception:
        pass

def log_startup_error(err_str: str):
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "startup_error.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {err_str}\n")
    except Exception:
        pass

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        log_startup_error(err_msg)
        show_error_dialog("백업시스템 시작 오류", f"백업시스템 기동 중 오류가 발생했습니다:\n\n{e}\n\n자세한 내용은 logs/startup_error.log를 확인하세요.")
        sys.exit(1)
