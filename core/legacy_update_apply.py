# -*- coding: utf-8 -*-
"""서명 검증을 마친 업데이트 zip 을 적용하고, 새 서버가 기동하지 않으면 이전 파일로 되돌린다.

표준 라이브러리만 사용한다(업데이트 도중 교체되는 프로젝트 코드에 의존하지 않기 위함).
web/api_update.py 의 self-update 가 이 파일을 임시 폴더로 복사해 별도 프로세스로 실행한다.

사용: python legacy_update_apply.py <zip> <base_dir> <port> [launcher]
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _log(base_dir, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        log_dir = os.path.join(base_dir, "data")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "legacy_update.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(port, want_open, timeout_sec):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _port_open(port) == want_open:
            return True
        time.sleep(0.5)
    return _port_open(port) == want_open


def _start_server(base_dir, launcher):
    return subprocess.Popen(
        [launcher, "run.py"], cwd=base_dir,
        creationflags=NO_WINDOW | DETACHED, close_fds=True,
    )


def _kill_tree(proc):
    if proc is None:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       creationflags=NO_WINDOW, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _stop_engine(base_dir):
    """이 설치 폴더의 kova_fast_engine.exe 만 종료한다(서버가 이미 꺼진 뒤라 남은 고아 프로세스)."""
    exe = os.path.abspath(os.path.join(base_dir, "engine_rust", "kova_fast_engine.exe")).lower()
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='kova_fast_engine.exe'\" | "
              "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower() -eq '" + exe.replace("'", "''") + "' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _safe_members(zf, base_dir):
    base_abs = os.path.abspath(base_dir)
    out = []
    for m in zf.infolist():
        dest = os.path.abspath(os.path.join(base_abs, m.filename))
        if not (dest == base_abs or dest.startswith(base_abs + os.sep)):
            raise ValueError(f"허용되지 않은 경로: {m.filename}")
        out.append((m, dest))
    return out


def apply_update(zip_path, base_dir, port, launcher=None, start_timeout=40, stop_timeout=20):
    """업데이트를 적용한다. 반환: 'ok' | 'rolled_back' | 'rollback_failed' | 'aborted'."""
    launcher = launcher or sys.executable
    base_dir = os.path.abspath(base_dir)

    # 이전 서버가 포트를 놓을 때까지 기다린다. 끝내 열려 있으면 파일을 건드리지 않고 중단한다.
    if not _wait_port(port, False, stop_timeout):
        _log(base_dir, f"중단: 포트 {port} 가 닫히지 않아 파일을 교체하지 않았습니다.")
        return "aborted"

    _stop_engine(base_dir)
    backup_dir = tempfile.mkdtemp(prefix="kova_update_backup_")
    created, replaced = [], []
    proc = None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = _safe_members(zf, base_dir)
            # 1) 덮어쓸 기존 파일을 먼저 백업하고, 새로 생길 파일은 기록해 둔다
            for m, dest in members:
                if m.is_dir():
                    continue
                rel = os.path.relpath(dest, base_dir)
                if os.path.isfile(dest):
                    bpath = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(bpath), exist_ok=True)
                    shutil.copy2(dest, bpath)
                    replaced.append(rel)
                else:
                    created.append(rel)
            # 2) 압축 해제
            try:
                zf.extractall(base_dir)
            except Exception as e:
                _log(base_dir, f"압축 해제 실패: {e} -> 복원 시도")
                raise

        # 3) 새 버전 기동 + 헬스체크
        proc = _start_server(base_dir, launcher)
        if _wait_port(port, True, start_timeout) and proc.poll() is None:
            _log(base_dir, f"업데이트 적용 성공 (교체 {len(replaced)}개, 신규 {len(created)}개)")
            shutil.rmtree(backup_dir, ignore_errors=True)
            try:
                os.remove(zip_path)
            except OSError:
                pass
            return "ok"
        _log(base_dir, f"새 버전이 {start_timeout}초 안에 기동하지 않아 롤백합니다.")
    except Exception as e:
        _log(base_dir, f"업데이트 중 오류: {e} -> 롤백합니다.")

    # ---- 롤백 ----
    try:
        _kill_tree(proc)
        _wait_port(port, False, 10)
        for rel in replaced:
            src = os.path.join(backup_dir, rel)
            dst = os.path.join(base_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        for rel in created:
            try:
                os.remove(os.path.join(base_dir, rel))
            except OSError:
                pass
        proc2 = _start_server(base_dir, launcher)
        healthy = _wait_port(port, True, start_timeout) and proc2.poll() is None
        _log(base_dir, "롤백 완료, 이전 버전 기동 " + ("성공" if healthy else "실패(수동 확인 필요)"))
        if healthy:
            shutil.rmtree(backup_dir, ignore_errors=True)
            return "rolled_back"
        return "rollback_failed"
    except Exception as e:
        _log(base_dir, f"롤백 실패: {e} (백업 위치: {backup_dir})")
        return "rollback_failed"


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(2)
    res = apply_update(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
    sys.exit(0 if res in ("ok", "rolled_back") else 1)
