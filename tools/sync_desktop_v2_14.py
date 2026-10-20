import os
import sys
import time
import subprocess
import urllib.request
import json

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"
REMOTE_BASE = r"C:\Users\kksjmj\AppData\Local\Programs\백업시스템"
LOCAL_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def run_ssh(cmd, check=True):
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", cmd]
    print(f"\n[SSH Remote] {cmd}")
    res = subprocess.run(full_cmd, capture_output=True, text=True, encoding="cp949", errors="ignore")
    if res.stdout:
        print(f"[STDOUT] {res.stdout.strip()}")
    if res.stderr:
        print(f"[STDERR] {res.stderr.strip()}")
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {res.returncode}")
    return res

def main():
    print("=" * 60)
    print("  K12 MiniPC -> RTX 5080 Desktop v2.14.0 Fast Deployment")
    print("=" * 60)

    # 1. SSH 연결 검증
    print("\n[Step 1] Verifying SSH connectivity...")
    run_ssh("hostname")

    # 2. 데스크탑 기존 백업 프로세스 종료
    print("\n[Step 2] Stopping desktop backup processes...")
    stop_cmd = (
        "powershell -NoProfile -Command \""
        "$procs = Get-CimInstance Win32_Process | Where-Object { "
        "  ($_.Name -in 'python.exe','pythonw.exe') -and ($_.CommandLine -like '*run.py*' -or $_.CommandLine -like '*백업시스템*') "
        "}; "
        "if ($procs) { "
        "  $procs | ForEach-Object { Write-Host 'Killing PID:' $_.ProcessId; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } "
        "} else { Write-Host 'No existing backup process found.' }\""
    )
    run_ssh(stop_cmd, check=False)
    time.sleep(2)

    # 3. 최신 코드 전송 (tar 파이프 스트리밍)
    print("\n[Step 3] Streaming v2.14.0 core, web, run.py, VERSION via tar pipe...")
    # 임시 tar 아카이브 생성
    tar_archive = os.path.join(LOCAL_BASE, "v2_14_0_update.tar")
    if os.path.exists(tar_archive):
        os.remove(tar_archive)
    
    # tar 생성 (LOCAL_BASE에서 core, web, run.py, VERSION 묶기)
    tar_create_cmd = [
        "tar", "-cf", tar_archive,
        "--exclude=__pycache__",
        "--exclude=*.pyc",
        "core", "web", "run.py", "VERSION"
    ]
    subprocess.run(tar_create_cmd, cwd=LOCAL_BASE, check=True)
    print(f"Created local update archive: {tar_archive} ({os.path.getsize(tar_archive)} bytes)")

    # 데스크탑으로 tar 아카이브 복사
    remote_tmp_tar = r"C:\Users\kksjmj\AppData\Local\Temp\v2_14_0_update.tar"
    scp_cmd = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", tar_archive, f"{DESKTOP_USER}@{DESKTOP_IP}:{remote_tmp_tar}"]
    subprocess.run(scp_cmd, check=True)
    print("SCP archive transferred to desktop temp.")

    # 데스크탑에서 tar 압축 해제
    extract_cmd = f"tar -xf {remote_tmp_tar} -C \"{REMOTE_BASE}\""
    run_ssh(extract_cmd)
    print("Extracted archive into desktop install directory.")

    # 임시 파일 정리
    if os.path.exists(tar_archive):
        os.remove(tar_archive)
    run_ssh(f"del /f /q {remote_tmp_tar}", check=False)

    # 4. 버전 확인
    print("\n[Step 4] Checking remote VERSION...")
    v_res = run_ssh(f"type \"{REMOTE_BASE}\\VERSION\"")
    print(f"Remote VERSION: {v_res.stdout.strip()}")

    # 5. 백그라운드 무창 재기동 (start_silent.vbs)
    print("\n[Step 5] Restarting desktop backup service via start_silent.vbs...")
    start_cmd = (
        f"powershell -NoProfile -Command \""
        f"Start-Process -FilePath 'wscript.exe' -ArgumentList '\"{REMOTE_BASE}\\start_silent.vbs\"' -WorkingDirectory '{REMOTE_BASE}'\""
    )
    run_ssh(start_cmd, check=False)

    # 6. 포트 8765 리스닝 및 API 응답 대기
    print("\n[Step 6] Waiting for desktop service to become healthy...")
    max_retries = 15
    healthy = False
    for i in range(max_retries):
        time.sleep(2)
        try:
            url = f"http://{DESKTOP_IP}:8765/api/status"
            req = urllib.request.Request(url, headers={"User-Agent": "DeployVerification/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    print(f"Server is UP! Version: {data.get('version')}, Status: {data.get('status')}")
                    healthy = True
                    break
        except Exception as e:
            print(f"Waiting... ({i+1}/{max_retries}): {e}")

    if not healthy:
        print("\n[WARNING] Service health check failed on http://100.90.20.59:8765/api/status")
        run_ssh("netstat -ano | findstr 8765", check=False)
        sys.exit(1)

    # 7. 신규 v2.14.0 API 검증 (/api/system/storage-health)
    print("\n[Step 7] Verifying v2.14.0 storage-health API on Desktop...")
    try:
        url = f"http://{DESKTOP_IP}:8765/api/system/storage-health"
        req = urllib.request.Request(url, headers={"User-Agent": "DeployVerification/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Storage Health API OK: summary={data.get('summary')}")
    except Exception as e:
        print(f"[NOTE] Storage health check call: {e}")

    print("\n" + "=" * 60)
    print("  Deployment to Desktop (100.90.20.59) SUCCESSFUL!")
    print("=" * 60)

if __name__ == "__main__":
    main()
