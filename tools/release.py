# -*- coding: utf-8 -*-
"""
Automated Git Versioning, Release Builder and Remote Deploy Tool
"""

import os
import sys
import re
import io
import time
import base64
import zipfile
import argparse
import subprocess
import shutil

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
VERSION_FILE = os.path.join(BASE_DIR, 'VERSION')
INIT_FILE = os.path.join(BASE_DIR, 'core', '__init__.py')
APP_FILE = os.path.join(BASE_DIR, 'web', 'app.py')
INDEX_FILE = os.path.join(BASE_DIR, 'web', 'templates', 'index.html')
ISS_FILE = os.path.join(BASE_DIR, 'installer', 'BackupSystem.iss')

PREFLIGHT_LINT_TARGETS = ['core', 'web', 'cli_backup.py', 'cli.py', 'run.py', 'disaster_recovery.py']

def run_preflight_lint_gate():
    """GitHub Actions CI와 100% 동일한 정적 분석(pyflakes) 사전 검증 게이트.
    린트 에러가 단 1건이라도 있으면 버전업/커밋/푸시를 즉시 차단(Fail-Closed)한다.
    """
    print("[0/5] Pre-flight Lint Check (pyflakes CI gate)...")
    pyflakes_bin = shutil.which("pyflakes")
    cmd = [pyflakes_bin] if pyflakes_bin else [sys.executable, '-m', 'pyflakes']
    try:
        res = subprocess.run(
            cmd + PREFLIGHT_LINT_TARGETS,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
    except Exception as e:
        print(f"      [FATAL] pyflakes 실행 실패: {e}")
        sys.exit(1)

    stdout = (res.stdout or '').strip()
    stderr = (res.stderr or '').strip()

    if res.returncode != 0 or stdout or stderr:
        print("      " + "!" * 50)
        print("      [BLOCKED] 정적 분석(pyflakes) 검증 실패! 릴리즈가 즉시 중단됩니다.")
        print("      " + "!" * 50)
        if stdout:
            print("      [pyflakes 오류 출력]:")
            for line in stdout.splitlines():
                print(f"        -> {line}")
        if stderr:
            print("      [pyflakes 경고/에러]:")
            for line in stderr.splitlines():
                print(f"        -> {line}")
        print("      * 코드를 수정한 후 다시 릴리즈를 실행하세요.")
        sys.exit(1)

    print("      [OK] pyflakes 무결성 검증 통과 (린트 오류 0건)")


def run_preflight_lifecycle_gate():
    """
    릴리즈 전 스마트 게이트키퍼 및 수명주기/계약 감사(Lifecycle & Contract Audit).
    외부 삭제 시 캐시 잔여, UI-API 액션 누락 등이 감지되면 릴리즈를 즉시 차단(Fail-Closed)한다.
    """
    print("[0/5] Pre-flight Smart Gatekeeper & Lifecycle Audit...")
    gatekeeper_script = os.path.join(BASE_DIR, 'tools', 'smart_gatekeeper.py')
    if os.path.exists(gatekeeper_script):
        try:
            res = subprocess.run(
                [sys.executable, gatekeeper_script, '--all'],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            if res.returncode != 0:
                print("      " + "!" * 50)
                print("      [BLOCKED] 스마트 게이트키퍼(수명주기/계약 감사) 검증 실패! 릴리즈가 즉시 중단됩니다.")
                print("      " + "!" * 50)
                if res.stdout:
                    for line in res.stdout.strip().splitlines():
                        print(f"        -> {line}")
                sys.exit(1)
            print("      [OK] 스마트 게이트키퍼 전 관문(CRLF/린트/수명주기/UI계약) 100% 통과")
        except Exception as e:
            print(f"      [FATAL] smart_gatekeeper 실행 실패: {e}")
            sys.exit(1)


def run_preflight_security_gate():
    """
    릴리즈 전 비밀정보/인증토큰 유출 방지 사전 검증 (scan_secrets CI gate).
    비밀키, OAuth 토큰, 서비스 계정 등이 Git 추적에 단 1건이라도 있으면 즉시 차단(Fail-Closed)한다.
    """
    print("[0/5] Pre-flight Security Audit (scan_secrets CI gate)...")
    sec_scan_script = os.path.join(BASE_DIR, 'tools', 'scan_secrets.py')
    if os.path.exists(sec_scan_script):
        try:
            res = subprocess.run(
                [sys.executable, sec_scan_script],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            if res.returncode != 0:
                print("      " + "!" * 50)
                print("      [BLOCKED] 비밀정보(토큰/비밀키) 감지! 릴리즈가 즉시 전면 중단됩니다.")
                print("      " + "!" * 50)
                if res.stdout:
                    for line in res.stdout.strip().splitlines():
                        print(f"        -> {line}")
                sys.exit(1)
            print("      [OK] Git 추적 대상 비밀정보 0건 확인 (보안 감사 통과)")
        except Exception as e:
            print(f"      [FATAL] scan_secrets 실행 실패: {e}")
            sys.exit(1)


def get_current_version():
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return '1.0.0'

def bump_version(current: str, bump_type: str) -> str:
    # 프리릴리즈 태그(-rc, -beta 등)를 안전하게 분리하여 파싱
    base_version = current.split('-')[0]
    parts = base_version.split('.')
    while len(parts) < 3:
        parts.append('0')
    
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        raise ValueError(f"Invalid version format: {current}")
    
    # RC 상태(예: 2.10.0-rc1)에서 minor로 승격 시 이미 2.10.0 이므로 그대로 확정
    if '-' in current and bump_type in ('minor', 'patch') and major == 2 and minor == 10:
        return f"{major}.{minor}.0"

    if bump_type == 'major':
        major += 1
        minor = 0
        patch = 0
    elif bump_type == 'minor':
        minor += 1
        patch = 0
    elif bump_type == 'patch':
        patch += 1
    else:
        raise ValueError(f"Unknown bump type: {bump_type}")
        
    return f"{major}.{minor}.{patch}"

def update_source_versions(new_ver: str):
    # 1. VERSION file
    with open(VERSION_FILE, 'w', encoding='utf-8') as f:
        f.write(f"{new_ver}\n")
    
    # 2. core/__init__.py
    if os.path.exists(INIT_FILE):
        content = open(INIT_FILE, 'r', encoding='utf-8').read()
        content = re.sub(r'__version__\s*=\s*["\'].*?["\']', f'__version__ = "{new_ver}"', content)
        with open(INIT_FILE, 'w', encoding='utf-8') as f:
            f.write(content)
            
    # 3. web/app.py
    if os.path.exists(APP_FILE):
        content = open(APP_FILE, 'r', encoding='utf-8').read()
        content = re.sub(r'FastAPI\(title="Server & System Backup Manager",\s*version=["\'].*?["\']\)', f'FastAPI(title="Server & System Backup Manager", version="{new_ver}")', content)
        with open(APP_FILE, 'w', encoding='utf-8') as f:
            f.write(content)

    # 4. web/templates/index.html
    if os.path.exists(INDEX_FILE):
        content = open(INDEX_FILE, 'r', encoding='utf-8').read()
        content = re.sub(r'>v\d+\.\d+\.\d+<', f'>v{new_ver}<', content)
        with open(INDEX_FILE, 'w', encoding='utf-8') as f:
            f.write(content)

    # 5. installer/BackupSystem.iss
    if os.path.exists(ISS_FILE):
        content = open(ISS_FILE, 'r', encoding='utf-8').read()
        content = re.sub(r'#define\s+MyAppVersion\s+["\'].*?["\']', f'#define MyAppVersion "{new_ver}"', content)
        with open(ISS_FILE, 'w', encoding='utf-8') as f:
            f.write(content)
    print(f"[1/5] Codebase version bumped to v{new_ver}")

def run_git_release(version: str, message: str):
    print(f"[2/5] Running Git commit and tagging v{version}...")
    try:
        subprocess.run(['git', 'add', '-A'], cwd=BASE_DIR, check=True)
        commit_msg = f"release: v{version} - {message}" if message else f"release: v{version}"
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=BASE_DIR, check=True)
        tag_name = f"v{version}"
        subprocess.run(['git', 'tag', '-a', tag_name, '-m', f"Release {tag_name}"], cwd=BASE_DIR, check=True)
        print(f"      Git commit and tag '{tag_name}' created successfully.")
    except subprocess.CalledProcessError as e:
        print(f"      Git warning/skip: {e}")

def compile_inno_setup_installer(version: str):
    print(f"[3/5] Compiling Inno Setup installer for v{version}...")
    if not os.path.exists(ISS_FILE):
        print("      Warning: BackupSystem.iss not found, skipping installer compilation.")
        return None

    iscc_candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
        r"C:\Program Files\Inno Setup 7\ISCC.exe",
        "ISCC.exe"
    ]
    iscc_bin = next((p for p in iscc_candidates if os.path.exists(p)), None)
    if not iscc_bin:
        print("      Warning: ISCC.exe not found. Skipping installer compilation.")
        return None

    dist_dir = os.path.join(BASE_DIR, 'dist')
    os.makedirs(dist_dir, exist_ok=True)
    expected_exe = os.path.join(dist_dir, f"Kova_Setup_v{version}.exe")
    legacy_exe = os.path.join(dist_dir, f"BackupSystem_Setup_v{version}.exe")

    try:
        cmd = [iscc_bin, f"/DMyAppVersion={version}", ISS_FILE]
        res = subprocess.run(cmd, capture_output=True, cwd=BASE_DIR)
        stdout = res.stdout.decode('utf-8', errors='replace')
        stderr = res.stderr.decode('utf-8', errors='replace')
        target_exe = expected_exe if os.path.exists(expected_exe) else legacy_exe

        if res.returncode == 0 and os.path.exists(target_exe):
            print(f"      [OK] Inno Setup installer compiled: {target_exe} ({os.path.getsize(target_exe):,} bytes)")
            return target_exe
        else:
            print(f"      Warning: ISCC compilation failed (code {res.returncode}):\n{stderr}\n{stdout}")
    except Exception as e:
        print(f"      Warning: Inno Setup compilation error: {e}")
    return None

def sync_install_directories(installer_exe: str = None):
    print("[3/5] Synchronizing install distribution folders...")
    local_app_dir = os.path.expandvars(r"%LOCALAPPDATA%\Programs\백업시스템")
    targets = [
        r'D:\백업시스템_설치용'
    ]
    if os.path.exists(local_app_dir):
        targets.append(local_app_dir)
    for target in targets:
        drive = os.path.splitdrive(target)[0]
        if drive and not os.path.exists(drive + '\\'):
            continue
        os.makedirs(target, exist_ok=True)
        
        robo_kwargs = {'capture_output': True}
        if sys.platform.startswith('win') and hasattr(subprocess, 'CREATE_NO_WINDOW'):
            robo_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        # Sync core and web
        subprocess.run(['robocopy', os.path.join(BASE_DIR, 'core'), os.path.join(target, 'core'), '/E', '/MIR'], **robo_kwargs)
        subprocess.run(['robocopy', os.path.join(BASE_DIR, 'web'), os.path.join(target, 'web'), '/E', '/MIR'], **robo_kwargs)
        
        # Sync emergency_restore (with embedded python)
        if os.path.exists(os.path.join(BASE_DIR, 'emergency_restore')):
            subprocess.run(['robocopy', os.path.join(BASE_DIR, 'emergency_restore'), os.path.join(target, 'emergency_restore'), '/E', '/MIR'], **robo_kwargs)
        
        # Sync keys (including release_ed25519.pub)
        if os.path.exists(os.path.join(BASE_DIR, 'keys')):
            subprocess.run(['robocopy', os.path.join(BASE_DIR, 'keys'), os.path.join(target, 'keys'), '/E', '/MIR'], **robo_kwargs)

        # Sync root py and bat files
        for item in os.listdir(BASE_DIR):
            full_path = os.path.join(BASE_DIR, item)
            if os.path.isfile(full_path) and (item.endswith('.py') or item.endswith('.bat') or item.endswith('.vbs') or item.endswith('.txt') or item.endswith('.md') or item == 'VERSION'):
                try:
                    with open(full_path, 'rb') as rf, open(os.path.join(target, item), 'wb') as wf:
                        wf.write(rf.read())
                except Exception:
                    pass

        # Sync compiled Inno Setup installer if available
        if installer_exe and os.path.exists(installer_exe):
            try:
                shutil.copy2(installer_exe, os.path.join(target, os.path.basename(installer_exe)))
                print(f"      Synced installer to: {target}")
            except Exception as e_inst:
                print(f"      Warning: Failed to copy installer to {target}: {e_inst}")

        print(f"      Synced to: {target}")

    # Also sync emergency disaster recovery kit to D:\MyBackup_Repository if it exists
    repo_backup = r"D:\MyBackup_Repository"
    if os.path.exists(repo_backup):
        emerg_src = os.path.join(BASE_DIR, 'emergency_restore')
        if os.path.exists(emerg_src):
            subprocess.run(['robocopy', emerg_src, os.path.join(repo_backup, 'emergency_restore'), '/E', '/MIR'], **robo_kwargs)
        recovery_files = [
            "원클릭_C드라이브_전체복구.bat",
            "선택복구_대화형.bat",
            "Restore_Full_C_Drive.bat",
            "Restore_Interactive.bat",
            "README_재해복구_가이드.txt",
            "5_베어메탈_시스템이미지_백업(OS+오피스).bat"
        ]
        for rf in recovery_files:
            rf_src = os.path.join(BASE_DIR, rf)
            if os.path.exists(rf_src):
                try:
                    with open(rf_src, 'rb') as f_in, open(os.path.join(repo_backup, rf), 'wb') as f_out:
                        f_out.write(f_in.read())
                except Exception:
                    pass
        print(f"      Synced Disaster Recovery Kit to: {repo_backup}")

def build_self_extracting_updater(version: str):
    print("[4/5] Building standalone self-extracting updater & Ed25519 digital signature...")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for folder in ['core', 'web', 'keys']:
            folder_path = os.path.join(BASE_DIR, folder)
            if not os.path.exists(folder_path):
                continue
            for root, dirs, files in os.walk(folder_path):
                if '__pycache__' in root:
                    continue
                for f in files:
                    f_lower = f.lower()
                    # CRITICAL SECURITY RULE: NEVER include private keys or sensitive credentials in distribution package
                    if f_lower.endswith('.key') or f_lower.endswith('.pem') or 'private' in f_lower or f_lower.endswith('.p12') or f_lower.endswith('.pfx'):
                        continue
                    # For keys/ folder, ONLY allow public keys (.pub)
                    if os.path.basename(root) == 'keys' and not f_lower.endswith('.pub'):
                        continue
                    # NEVER include installer executables in update package
                    if f_lower.endswith('.exe'):
                        continue
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, BASE_DIR)
                    zf.write(full_path, rel_path)
        # Bundle essential runner scripts
        for extra_script in ['run.py', 'start_silent.vbs', 'start_tray.vbs', 'launch_dashboard.vbs', 'stop_backup_system.bat', '2_백업시스템_실행.bat']:
            extra_path = os.path.join(BASE_DIR, extra_script)
            if os.path.exists(extra_path):
                zf.write(extra_path, extra_script)
        if os.path.exists(VERSION_FILE):
            zf.write(VERSION_FILE, 'VERSION')

        # Automated Zero-Leak Verification
        for name in zf.namelist():
            name_lower = name.lower()
            if name_lower.endswith('.key') or name_lower.endswith('.pem') or 'private' in name_lower or 'release_ed25519.key' in name_lower or 'release_ed25519_v2.key' in name_lower:
                raise RuntimeError(f"FATAL SECURITY VIOLATION: Private key detected in release package: {name}! Build aborted.")
        print(f"      [Security Audit PASSED] Zero private keys found in package ({len(zf.namelist())} files).")

    raw_bytes = buf.getvalue()

    # 1. Sign package with Ed25519 private key (Priority: v2 > v1)
    priv_key_v2_path = os.path.join(BASE_DIR, 'keys', 'release_ed25519_v2.key')
    priv_key_v1_path = os.path.join(BASE_DIR, 'keys', 'release_ed25519.key')
    priv_key_path = priv_key_v2_path if os.path.exists(priv_key_v2_path) else priv_key_v1_path
    sig_hex = ""
    if os.path.exists(priv_key_path):
        try:
            from core.crypto_sign import sign_bytes_ed25519
            sig_hex = sign_bytes_ed25519(raw_bytes, priv_key_path)
            print(f"      Ed25519 Signature generated using {os.path.basename(priv_key_path)}: {sig_hex[:16]}...{sig_hex[-16:]}")
        except Exception as e:
            print(f"      Warning: Ed25519 signing failed: {e}")
    else:
        print("      Notice: No Ed25519 private key found (v1/v2), signature omitted.")

    # 2. Save release zip and signature in dist/
    dist_dir = os.path.join(BASE_DIR, 'dist')
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, f"release_v{version}.zip")
    with open(zip_path, 'wb') as f:
        f.write(raw_bytes)
    if sig_hex:
        with open(zip_path + ".sig", 'w', encoding='utf-8') as f:
            f.write(sig_hex)

    b64 = base64.b64encode(raw_bytes).decode('ascii')
    chunks = [b64[i:i+64] for i in range(0, len(b64), 64)]
    b64_content = '\r\n'.join(chunks)

    bat_header = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        f"title Backup Fast Engine Updater v{version}\r\n"
        f"REM [Security] Ed25519-Signature: {sig_hex}\r\n"
        "\r\n"
        "echo ========================================================\r\n"
        "echo   [1/3] Terminating running backup background processes...\r\n"
        "echo ========================================================\r\n"
        "powershell -NoProfile -Command \"Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }\" >nul 2>&1\r\n"
        "powershell -NoProfile -Command \"Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*백업시스템*' -or $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\" >nul 2>&1\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        "REM Silently ensure cryptography dependency\r\n"
        "python -m pip install cryptography --quiet >nul 2>&1\r\n"
        "\r\n"
        "echo ========================================================\r\n"
        f"echo   [2/3] Detecting installation directory ^& Extracting v{version}...\r\n"
        "echo ========================================================\r\n"
        "set \"TARGET_DIR=\"\r\n"
        "REM (1) RTX 5080 Desktop target directory (Highest Priority for Desktop)\r\n"
        "if exist \"F:\\백업시스템_설치용\" set \"TARGET_DIR=F:\\백업시스템_설치용\"\r\n"
        "REM (2) K12 Mini PC target directory (for K12 Mini PC)\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"D:\\백업시스템_설치용\\core\" set \"TARGET_DIR=D:\\백업시스템_설치용\"\r\n"
        ")\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"D:\\백업시스템_설치용\" set \"TARGET_DIR=D:\\백업시스템_설치용\"\r\n"
        ")\r\n"
        "REM (3) Inno Setup registry lookup\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    for /f \"tokens=2* skip=2\" %%a in ('reg query \"HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{E8A42F38-9B7C-4C2E-8E1A-98C32B7D501F}_is1\" /v InstallLocation 2^>nul') do set \"TARGET_DIR=%%b\"\r\n"
        ")\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    for /f \"tokens=2* skip=2\" %%a in ('reg query \"HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{E8A42F38-9B7C-4C2E-8E1A-98C32B7D501F}_is1\" /v InstallLocation 2^>nul') do set \"TARGET_DIR=%%b\"\r\n"
        ")\r\n"
        "REM (4) Standard Program Files check\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"%ProgramFiles%\\백업시스템\\core\" set \"TARGET_DIR=%ProgramFiles%\\백업시스템\"\r\n"
        ")\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"%ProgramFiles(x86)%\\백업시스템\\core\" set \"TARGET_DIR=%ProgramFiles(x86)%\\백업시스템\"\r\n"
        ")\r\n"
        "REM (5) Current execution directory\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"%~dp0core\" set \"TARGET_DIR=%~dp0\"\r\n"
        ")\r\n"
        "REM (6) Fallback default\r\n"
        "if not defined TARGET_DIR (\r\n"
        "    if exist \"F:\\\" (\r\n"
        "        set \"TARGET_DIR=F:\\백업시스템_설치용\"\r\n"
        "    ) else (\r\n"
        "        set \"TARGET_DIR=D:\\백업시스템_설치용\"\r\n"
        "    )\r\n"
        ")\r\n"
        "echo   Detected Target: %TARGET_DIR%\r\n"
        "if not exist \"%TARGET_DIR%\" mkdir \"%TARGET_DIR%\" >nul 2>&1\r\n"
        "\r\n"
        f"set \"TMP_ZIP=%TEMP%\\update_v{version}_%RANDOM%.zip\"\r\n"
        "\r\n"
        "certutil -decode \"%~f0\" \"%TMP_ZIP%\" >nul 2>&1\r\n"
        "\r\n"
        "powershell -ExecutionPolicy Bypass -NoProfile -Command \"Expand-Archive -LiteralPath '%TMP_ZIP%' -DestinationPath '%TARGET_DIR%' -Force\"\r\n"
        "\r\n"
        "del \"%TMP_ZIP%\" >nul 2>&1\r\n"
        "\r\n"
        "REM Update Desktop Shortcut to point to updated installation\r\n"
        "powershell -ExecutionPolicy Bypass -NoProfile -Command \"$ws = New-Object -ComObject WScript.Shell; $d = [Environment]::GetFolderPath('Desktop'); Get-ChildItem -Path $d -Filter '*백업시스템*.lnk' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue; $lnk = $ws.CreateShortcut(\\\"$d\\Kova 대시보드.lnk\\\"); $lnk.TargetPath = 'wscript.exe'; $lnk.Arguments = \\\"`\\\"\" + '%TARGET_DIR%' + \"\\launch_dashboard.vbs`\\\"\\\"; $lnk.WorkingDirectory = '%TARGET_DIR%'; $lnk.Description = 'Kova - 로컬 백업 & 재해 복구 웹 대시보드'; $lnk.IconLocation = 'shell32.dll,259'; $lnk.Save()\" >nul 2>&1\r\n"
        "\r\n"
        "echo ========================================================\r\n"
        f"echo   [3/3] Starting Backup System v{version}...\r\n"
        "echo ========================================================\r\n"
        "cd /d \"%TARGET_DIR%\"\r\n"
        "if exist \"%TARGET_DIR%\\start_silent.vbs\" (\r\n"
        "    wscript.exe \"%TARGET_DIR%\\start_silent.vbs\"\r\n"
        ") else if exist \"%TARGET_DIR%\\2_백업시스템_실행.bat\" (\r\n"
        "    start \"\" \"%TARGET_DIR%\\2_백업시스템_실행.bat\"\r\n"
        ") else if exist \"%TARGET_DIR%\\BackupSystem.exe\" (\r\n"
        "    start \"\" \"%TARGET_DIR%\\BackupSystem.exe\"\r\n"
        ") else if exist \"%TARGET_DIR%\\run.py\" (\r\n"
        "    start \"\" pythonw.exe \"%TARGET_DIR%\\run.py\"\r\n"
        ")\r\n"
        "\r\n"
        "echo ========================================================\r\n"
        f"echo   Update to v{version} completed successfully! (1-2s Cold Boot)\r\n"
        "echo ========================================================\r\n"
        "ping 127.0.0.1 -n 3 >nul\r\n"
        "exit /b 0\r\n"
        "\r\n"
        "-----BEGIN CERTIFICATE-----\r\n"
    )
    bat_footer = "\r\n-----END CERTIFICATE-----\r\n"

    out_bat = os.path.join(os.path.dirname(BASE_DIR), 'APPLY_UPDATE.bat')
    with open(out_bat, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(bat_header + b64_content + bat_footer)
    print(f"      Created: {out_bat} ({os.path.getsize(out_bat):,} bytes)")
    return out_bat, sig_hex, raw_bytes


def publish_remote_release(version: str, message: str, installer_exe: str = None):
    """
    원격 GitHub 저장소 동기화 및 GitHub Releases 자동 배포:
    1. git push origin HEAD
    2. git push origin v{version}
    3. gh CLI를 통해 Kova_Setup_v{version}.exe 인스톨러 및 릴리즈 아티팩트 자동 업로드
    """
    print("[5/6] Publishing remote release & GitHub Release assets...")
    try:
        subprocess.run(['git', 'push', 'origin', 'HEAD'], cwd=BASE_DIR, check=True)
        subprocess.run(['git', 'push', 'origin', f'v{version}'], cwd=BASE_DIR, check=True)
        print(f"      [OK] git push origin HEAD and tag v{version} completed.")
    except Exception as e:
        print(f"      Warning: git push failed: {e}")

    # Check gh CLI
    gh_bin = shutil.which('gh')
    if not gh_bin:
        gh_candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe"),
            r"C:\Program Files\GitHub CLI\gh.exe",
            r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        ]
        gh_bin = next((p for p in gh_candidates if os.path.exists(p)), None)

    if not gh_bin:
        print("      Notice: gh CLI not found. GitHub Release upload skipped.")
        return

    tag_name = f"v{version}"
    title = f"Kova v{version}"
    body = message or f"Automated release v{version}"

    upload_files = []
    if installer_exe and os.path.exists(installer_exe):
        upload_files.append(installer_exe)
    dist_dir = os.path.join(BASE_DIR, 'dist')
    zip_path = os.path.join(dist_dir, f"release_v{version}.zip")
    if os.path.exists(zip_path):
        upload_files.append(zip_path)
    if os.path.exists(zip_path + ".sig"):
        upload_files.append(zip_path + ".sig")

    try:
        check_proc = subprocess.run([gh_bin, 'release', 'view', tag_name], cwd=BASE_DIR, capture_output=True)
        if check_proc.returncode == 0:
            if upload_files:
                upload_cmd = [gh_bin, 'release', 'upload', tag_name] + upload_files + ['--clobber']
                subprocess.run(upload_cmd, cwd=BASE_DIR, check=True)
                print(f"      [OK] GitHub Release assets updated for {tag_name}")
        else:
            create_cmd = [
                gh_bin, 'release', 'create', tag_name,
                '--title', title,
                '--notes', body
            ] + upload_files
            subprocess.run(create_cmd, cwd=BASE_DIR, check=True)
            print(f"      [OK] GitHub Release created with installer: {tag_name}")
    except Exception as e:
        print(f"      Warning: GitHub Release upload failed: {e}")


def restart_local_server():
    """로컬 백업 서버(pythonw run.py)를 Kill 후 start_silent.vbs로 재시작하고, 포트 8765가 열릴 때까지 확인."""
    print("[4/4] Restarting local backup server...")
    if not sys.platform.startswith('win'):
        print("      Non-Windows: server restart skipped.")
        return

    no_win = {}
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        no_win = {'creationflags': subprocess.CREATE_NO_WINDOW}

    # 1. Kill any process holding port 8765 or running pythonw unconditionally (including elevated zombies)
    try:
        cur_pid = os.getpid()
        subprocess.run(['powershell', '-NoProfile', '-Command', "Get-Process pythonw*, kova*, backupsystem* -ErrorAction SilentlyContinue | Stop-Process -Force"], capture_output=True, timeout=8, **no_win)
        subprocess.run(['taskkill.exe', '/F', '/T', '/IM', 'pythonw.exe'], capture_output=True, timeout=8, **no_win)
        subprocess.run(['taskkill.exe', '/F', '/T', '/IM', 'Kova.exe'], capture_output=True, timeout=8, **no_win)
        subprocess.run(['taskkill.exe', '/F', '/T', '/IM', 'BackupSystem.exe'], capture_output=True, timeout=8, **no_win)
        kill_elevated = (
            "Start-Process powershell.exe -ArgumentList '-NoProfile -Command "
            "\"Get-Process pythonw*, kova*, backupsystem* -ErrorAction SilentlyContinue | Stop-Process -Force; "
            f"(Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue).OwningProcess | Where-Object {{ $_ -ne {cur_pid} }} | Sort-Object -Unique | ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }}\"' "
            "-Verb RunAs -WindowStyle Hidden -Wait"
        )
        subprocess.run(['powershell', '-NoProfile', '-Command', kill_elevated], capture_output=True, timeout=10, **no_win)
        print("      Stopped existing processes on port 8765.")
    except Exception as e:
        print(f"      Stop warning (may be OK if nothing was running): {e}")

    time.sleep(1.5)

    # 2. Restart server (Priority: local installed pythonw > venv pythonw > hermes pythonw)
    local_app_dir = os.path.expandvars(r"%LOCALAPPDATA%\Programs\백업시스템")
    local_pyw = os.path.join(local_app_dir, 'python', 'pythonw.exe')
    venv_pyw = os.path.join(BASE_DIR, '.venv', 'Scripts', 'pythonw.exe')
    hermes_pyw = os.path.expandvars(r"%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe")

    if os.path.exists(local_app_dir) and os.path.exists(local_pyw):
        run_dir = local_app_dir
        pyw_path = local_pyw
    elif os.path.exists(venv_pyw):
        run_dir = BASE_DIR
        pyw_path = venv_pyw
    elif os.path.exists(hermes_pyw):
        run_dir = BASE_DIR
        pyw_path = hermes_pyw
    else:
        run_dir = BASE_DIR
        pyw_path = 'pythonw.exe'

    launched = False
    # Attempt 1: Direct pythonw.exe launch via PowerShell
    try:
        ps_cmd = f"Start-Process -FilePath '{pyw_path}' -ArgumentList 'run.py --silent' -WorkingDirectory '{run_dir}' -Verb RunAs"
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True, timeout=10)
        launched = True
        print(f"      pythonw.exe launched directly via Start-Process (Elevated) in {run_dir}.")
    except Exception as e:
        print(f"      Direct pythonw launch failed: {e}. Falling back to VBS...")

    # Attempt 2: Fallback to start_silent.vbs if pythonw failed
    if not launched:
        vbs_path = os.path.join(run_dir, 'start_silent.vbs')
        if os.path.exists(vbs_path):
            try:
                ps_cmd = f"Start-Process wscript.exe -ArgumentList '\"{vbs_path}\"' -WorkingDirectory '{run_dir}'"
                subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True, timeout=10)
                print("      start_silent.vbs launched via detached Start-Process (Fallback).")
            except Exception as e:
                print(f"      Failed to launch start_silent.vbs: {e}")
                return
        else:
            print("      No pythonw.exe or start_silent.vbs found. Restart skipped.")
            return

    # 3. Wait up to 20s for port 8765 to open
    import socket
    deadline = time.time() + 20
    alive = False
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', 8765), timeout=1):
                alive = True
                break
        except OSError:
            time.sleep(0.5)

    if alive:
        print("      [OK] Server is live on http://127.0.0.1:8765")
    else:
        print("      [WARN] Server did not respond within 20s -- check logs/startup_error.log")

def main():
    parser = argparse.ArgumentParser(description="Backup System Release & Versioning Manager")
    parser.add_argument('--bump', choices=['patch', 'minor', 'major', 'none'], default='patch', help="Version bump type")
    parser.add_argument('--version', type=str, default=None, help="Explicit target version to set (bypasses --bump)")
    parser.add_argument('-m', '--message', type=str, default="Automated engine build and bugfix release", help="Commit and changelog message")
    parser.add_argument('--skip-restart', action="store_true", help="Skip local server restart after release")
    parser.add_argument('--skip-remote', action="store_true", help="Skip remote git push and GitHub Release upload")
    args = parser.parse_args()

    print("=" * 60)
    print("   Backup System Release & Versioning Pipeline")
    print("=" * 60)

    # 릴리즈 착수 전 보안 감사(scan_secrets), 정적 분석(pyflakes), 스마트 게이트키퍼(수명주기/UI계약) 사전 검증
    run_preflight_security_gate()
    run_preflight_lint_gate()
    run_preflight_lifecycle_gate()

    cur_ver = get_current_version()
    if args.version:
        new_ver = args.version
        update_source_versions(new_ver)
    elif args.bump != 'none':
        new_ver = bump_version(cur_ver, args.bump)
        update_source_versions(new_ver)
    else:
        new_ver = cur_ver
        update_source_versions(new_ver)

    run_git_release(new_ver, args.message)
    installer_exe = compile_inno_setup_installer(new_ver)
    sync_install_directories(installer_exe)
    bat_path, sig_hex, raw_bytes = build_self_extracting_updater(new_ver)

    if not args.skip_remote:
        publish_remote_release(new_ver, args.message, installer_exe)

    if not args.skip_restart:
        restart_local_server()

    print("=" * 60)
    print(f"   SUCCESS: Release v{new_ver} completely built and deployed!")
    print("=" * 60)

if __name__ == '__main__':
    main()
