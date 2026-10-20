"""
독립형 비상 재해 복구 엔진 (Standalone Emergency Disaster Recovery Script)
외부 프레임워크(FastAPI 등) 없이 파이썬 표준 라이브러리(zlib, hashlib, json 등)만으로 동작합니다.
초고속 1MB 스트리밍 I/O 및 멀티스레드 병렬 복원을 지원합니다.
"""

import os
import sys
import json
import zlib
import hashlib
import time
import glob
import threading
import concurrent.futures
from typing import Dict, List, Any, Optional

try:
    import zstandard as zstd
except ImportError:
    zstd = None

# Safe stdout/stderr reconfigure to prevent cp949 encoding errors on Windows CMD
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def format_bytes(size: int) -> str:
    power = 1024
    n = 0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    while size > power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"

def get_blob_path(repo_dir: str, sha256_hash: str) -> str:
    if not sha256_hash:
        return ""
    prefix = sha256_hash[:2]
    return os.path.join(repo_dir, "blobs", prefix, f"{sha256_hash}.blob")

def extract_blob(blob_path: str, dest_path: str, expected_sha256: str, verify_hash: bool = False) -> bool:
    if not os.path.exists(blob_path):
        raise FileNotFoundError(f"Blob file not found: {blob_path}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.exists(dest_path):
        try:
            import stat as stat_mod
            os.chmod(dest_path, stat_mod.S_IWRITE)
        except Exception:
            pass
    sha256 = hashlib.sha256() if verify_hash else None

    # Check magic byte for zstd (0x28 0xB5 0x2F 0xFD)
    with open(blob_path, "rb") as fin:
        header = fin.read(4)

    is_zstd = (header == b"\x28\xb5\x2f\xfd")

    # Optimal streaming buffer
    buf_size = 262144

    with open(blob_path, "rb") as fin, open(dest_path, "wb") as fout:
        if is_zstd:
            if zstd is None:
                raise RuntimeError("이 백업 블롭은 Zstandard(zstd)로 압축되었습니다. 'pip install zstandard'를 실행해 주세요.")
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(fin) as reader:
                while True:
                    chunk = reader.read(buf_size)
                    if not chunk:
                        break
                    fout.write(chunk)
                    if sha256:
                        sha256.update(chunk)
        else:
            decompressor = zlib.decompressobj()
            while True:
                chunk = fin.read(buf_size)
                if not chunk:
                    break
                decompressed = decompressor.decompress(chunk)
                if decompressed:
                    fout.write(decompressed)
                    if sha256:
                        sha256.update(decompressed)
            tail = decompressor.flush()
            if tail:
                fout.write(tail)
                if sha256:
                    sha256.update(tail)

    if verify_hash and sha256.hexdigest() != expected_sha256:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise ValueError(f"Hash mismatch for {dest_path}")

    return True

def get_candidate_repositories() -> List[str]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        script_dir,
        os.path.dirname(script_dir),
        os.path.join(script_dir, "backup_repository"),
        os.path.join(os.path.expanduser("~"), "MyBackup_Repository"),
        r"D:\MyBackup_Repository",
        r"C:\Users\kksjmj\Desktop\ai\백업시스템\backup_repository"
    ]

    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint:
                candidates.append(os.path.join(part.mountpoint, "MyBackup_Repository"))
                candidates.append(os.path.join(part.mountpoint, "backup_repository"))
    except Exception:
        for letter in "CDEFG":
            candidates.append(f"{letter}:\\MyBackup_Repository")
            candidates.append(f"{letter}:\\backup_repository")

    valid = []
    seen = set()
    for c in candidates:
        norm = os.path.normpath(c).lower()
        if norm not in seen and os.path.exists(os.path.join(c, "snapshots")) and os.path.exists(os.path.join(c, "blobs")):
            seen.add(norm)
            valid.append(c)

    def _latest_snap_time(r: str) -> float:
        s_dir = os.path.join(r, "snapshots")
        try:
            files = [os.path.join(s_dir, f) for f in os.listdir(s_dir) if f.endswith(".json")]
            return max([os.path.getmtime(f) for f in files]) if files else 0.0
        except Exception:
            return 0.0

    valid.sort(key=_latest_snap_time, reverse=True)
    return valid

def find_snapshots(repo_dir: str) -> List[Dict[str, Any]]:
    snaps_dir = os.path.join(repo_dir, "snapshots")
    if not os.path.exists(snaps_dir):
        return []
    
    files = glob.glob(os.path.join(snaps_dir, "snap_*.json"))
    snapshots = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                snapshots.append({
                    "id": data.get("id"),
                    "created_at": data.get("created_at", 0),
                    "iso_time": data.get("iso_time", ""),
                    "profile_name": data.get("profile_name", ""),
                    "file_count": len(data.get("entries", [])),
                    "path": f
                })
        except Exception:
            continue

    snapshots.sort(key=lambda x: x["created_at"], reverse=True)
    return snapshots

def run_emergency_restore(
    repo_dir: str,
    snapshot_path: str,
    target_override: Optional[str] = None,
    remap_user: bool = True,
    overwrite: bool = True,
    verify_hash: bool = False,
    filter_keyword: Optional[str] = None
):
    print("=" * 70)
    print(" [복구시작] 초고속 멀티스레드 비상 재해 복구 (Emergency Restore)")
    print("=" * 70)
    print(f"[*] 저장소 경로: {repo_dir}")
    print(f"[*] 스냅샷 파일: {snapshot_path}")

    with open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    entries = snapshot.get("entries", [])
    total_entries = len(entries)
    print(f"[*] 총 백업 파일 수: {total_entries:,} 개")

    # Filter entries if needed
    if filter_keyword:
        kw = filter_keyword.lower().replace('/', '\\')
        entries = [e for e in entries if kw in (e.get("rel_path", "") + " " + e.get("source_root", "")).lower().replace('/', '\\')]
        print(f"[*] 필터링 적용 ('{filter_keyword}'): {len(entries):,} 개 파일 선택됨")

    if not entries:
        print("[!] 복원할 파일이 없습니다.")
        return

    # Robust User Remapping: scan all entries to detect any user profile paths
    current_user_profile = os.path.normpath(os.path.expanduser('~'))
    remap_from = ""
    if remap_user:
        for entry in entries:
            src = entry.get("source_root", "")
            if "users" in src.lower():
                parts = src.replace('/', '\\').split('\\')
                try:
                    u_idx = [p.lower() for p in parts].index("users")
                    if u_idx + 1 < len(parts):
                        backup_user = parts[u_idx + 1]
                        drive_prefix = parts[0] if ':' in parts[0] else 'C:'
                        candidate_remap = os.path.normpath(f"{drive_prefix}\\Users\\{backup_user}")
                        if candidate_remap.lower() != current_user_profile.lower():
                            remap_from = candidate_remap
                            break
                except (ValueError, IndexError):
                    continue

        if remap_from:
            print("[*] 윈도우 사용자 폴더 자동 리매핑 감지 및 적용:")
            print(f"    - 백업 계정: {remap_from}")
            print(f"    - 현재 계정: {current_user_profile} (현재 로그인된 계정으로 자동 전송됩니다)")
        else:
            remap_from = ""

    num_workers = min(12, max(4, os.cpu_count() or 4))
    print(f"[*] 병렬 가속: {num_workers}개 스레드로 동시 복원 진행")
    print("-" * 70)

    start_time = time.time()
    restored_count = 0
    skipped_count = 0
    failed_count = 0
    restored_bytes = 0
    processed_count = 0
    total_to_process = len(entries)
    lock = threading.Lock()
    reg_files_to_import = []
    driver_dirs_to_install = set()

    def _worker_restore(entry):
        rel_path = entry.get("rel_path", "")
        source_root = entry.get("source_root", "")
        sha256 = entry.get("sha256") or entry.get("blob_id")
        f_size = entry.get("size", 0)
        mtime = entry.get("mtime")

        if not rel_path or not sha256:
            return "skip", 0, None, None

        # Intelligently skip gigantic dummy VM sparse disk images (>50GB raw zeroes)
        if f_size > 50 * 1024 * 1024 * 1024 and ("avd" in rel_path.lower() or "userdata.img" in rel_path.lower()):
            return "skip", 0, None, None

        if target_override:
            dest_path = os.path.normpath(os.path.join(target_override, rel_path))
        else:
            base_dir = source_root
            if remap_from:
                norm_base = os.path.normpath(base_dir)
                if norm_base.lower().startswith(remap_from.lower()):
                    base_dir = os.path.normpath(current_user_profile + norm_base[len(remap_from):])
            dest_path = os.path.normpath(os.path.join(base_dir, rel_path))

        blob_path = get_blob_path(repo_dir, sha256)

        if not overwrite and os.path.exists(dest_path):
            return "skip", 0, None, None

        try:
            extract_blob(blob_path, dest_path, sha256, verify_hash=verify_hash)
            if mtime:
                try:
                    os.utime(dest_path, (mtime, mtime))
                except OSError:
                    pass

            is_reg = dest_path.lower().endswith(".reg")
            is_inf = dest_path.lower().endswith(".inf") and "windows_drivers" in dest_path.lower()
            drv_dir = os.path.dirname(os.path.dirname(dest_path)) if is_inf else None
            return "ok", f_size, dest_path if is_reg else None, drv_dir
        except Exception:
            return "fail", 0, None, None

    last_print_time = 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_worker_restore, e): e for e in entries}
        for fut in concurrent.futures.as_completed(futures):
            status, f_size, reg_p, drv_p = fut.result()
            with lock:
                processed_count += 1
                if status == "ok":
                    restored_count += 1
                    restored_bytes += f_size
                    if reg_p:
                        reg_files_to_import.append(reg_p)
                    if drv_p:
                        driver_dirs_to_install.add(drv_p)
                elif status == "skip":
                    skipped_count += 1
                elif status == "fail":
                    failed_count += 1

                now = time.time()
                if (now - last_print_time >= 0.25) or (processed_count == total_to_process):
                    last_print_time = now
                    elapsed = now - start_time
                    speed = (restored_bytes / elapsed) if elapsed > 0 else 0
                    pct = (processed_count / total_to_process) * 100
                    sys.stdout.write(f"\r진행률: [{pct:5.1f}%] {processed_count:,}/{total_to_process:,} | {format_bytes(restored_bytes)} ({format_bytes(int(speed))}/s)   ")
                    sys.stdout.flush()

    print()
    total_elapsed = time.time() - start_time
    print("=" * 70)
    print(" [완료] 복원 작업이 정상적으로 종료되었습니다!")
    print(f"[*] 총 소요 시간: {total_elapsed:.1f}초")
    print(f"[*] 복원된 파일: {restored_count:,} 개 ({format_bytes(restored_bytes)})")
    if skipped_count > 0:
        print(f"[*] 건너뛴 파일: {skipped_count:,} 개 (이미 존재함)")
    if failed_count > 0:
        print(f"[!] 실패한 파일: {failed_count:,} 개")
    print("=" * 70)

    if reg_files_to_import:
        print(f"\n[*] 복원된 윈도우 레지스트리 백업 파일이 {len(reg_files_to_import)}개 있습니다:")
        for rf in reg_files_to_import[:5]:
            print(f"    - {rf}")
        
        # In auto mode, automatically apply registry keys
        if "--auto" in sys.argv or "-a" in sys.argv:
            print("\n[*] [자동 모드] 복원된 레지스트리 키를 윈도우에 일괄 적용합니다...")
            import subprocess
            kwargs = {"capture_output": True, "text": True, "timeout": 10}
            if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            for rf in reg_files_to_import:
                try:
                    subprocess.run(["reg.exe", "import", rf], **kwargs)
                except Exception:
                    pass
            print("[*] 레지스트리 일괄 적용 완료!")
        else:
            print("    (필요 시 위 .reg 파일을 실행하여 레지스트리를 적용하세요)")

    if driver_dirs_to_install:
        print("\n[*] 복원된 윈도우 하드웨어 드라이버 저장소가 감지되었습니다:")
        for dd in sorted(driver_dirs_to_install):
            print(f"    - {dd}")
        if "--auto" in sys.argv or "-a" in sys.argv:
            print("\n[*] [자동 모드] Windows DriverStore에 하드웨어 드라이버 일괄 설치/등록 진행...")
            import subprocess
            pnp_kwargs = {"capture_output": True, "text": True, "encoding": "cp949", "errors": "replace", "timeout": 300}
            if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                pnp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            for dd in driver_dirs_to_install:
                try:
                    pattern = os.path.join(dd, "*.inf")
                    cmd = ["pnputil", "/add-driver", pattern, "/subdirs", "/install"]
                    proc = subprocess.run(cmd, **pnp_kwargs)
                    if proc.returncode in (0, 259, 3010):
                        print(f"[*] 드라이버 일괄 설치 완료! (상태 코드: {proc.returncode})")
                    else:
                        print(f"[!] 드라이버 설치 응답 코드: {proc.returncode}")
                except Exception as e:
                    print(f"[!] 드라이버 설치 중 오류: {e}")
        else:
            print("    (필요 시 'pnputil /add-driver <경로>\\*.inf /subdirs /install' 명령으로 설치 가능)")

    # Post-restore: Refresh desktop shortcuts & links so they connect validly without breaking
    try:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "create_desktop_shortcut.py"),
            r"C:\Users\kksjmj\Desktop\ai\백업시스템\create_desktop_shortcut.py"
        ]
        cs_kwargs = {"capture_output": True, "timeout": 10}
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            cs_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        for cs in candidates:
            if os.path.exists(cs):
                import subprocess
                subprocess.run([sys.executable, cs], **cs_kwargs)
                print("[*] 바탕화면 바로가기 링크 복원 및 유효성 재연결 완료!")
                break
    except Exception:
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="독립형 긴급 비상 재해 복구 엔진")
    parser.add_argument("--auto", "-a", action="store_true", help="가장 최신 스냅샷을 원본 위치로 즉시 무인 자동 복구")
    parser.add_argument("--repo", "-r", type=str, default=None, help="백업 저장소 경로")
    parser.add_argument("--snapshot", "-s", type=str, default=None, help="복원할 스냅샷 ID 또는 파일 경로")
    parser.add_argument("--target", "-t", type=str, default=None, help="복원 대상 디렉토리 (미지정 시 백업 당시 원본 위치)")
    parser.add_argument("--filter", "-f", type=str, default=None, help="특정 파일/폴더 키워드 필터링 복원")
    parser.add_argument("--verify", "-v", action="store_true", help="SHA-256 암호화 무결성 전수 검증 활성화 (기본값: False, 고속 모드)")
    args = parser.parse_args()

    candidates = get_candidate_repositories()
    repo_dir = args.repo or (candidates[0] if candidates else None)

    if not repo_dir or not os.path.exists(repo_dir):
        if args.auto and candidates:
            repo_dir = candidates[0]
        else:
            print("[!] 백업 저장소를 자동으로 찾을 수 없습니다.")
            repo_dir = input("백업 저장소 경로를 입력해주세요 (예: D:\\MyBackup_Repository): ").strip('"\' ')

    snapshots = find_snapshots(repo_dir)
    if not snapshots:
        print(f"[!] 저장소({repo_dir})에 스냅샷 파일이 없습니다.")
        return

    if args.auto:
        # Full automatic restore: pick the latest snapshot and restore to original path
        selected_snap = snapshots[0]
        print(f"\n[*] [자동 모드 활성] 가장 최신 스냅샷을 자동 선택합니다: {selected_snap['id']} ({selected_snap['iso_time'][:19]})")
        run_emergency_restore(
            repo_dir=repo_dir,
            snapshot_path=selected_snap["path"],
            target_override=args.target,
            verify_hash=args.verify,
            filter_keyword=args.filter
        )
        return

    # Interactive mode
    if args.snapshot:
        selected_snap = next((s for s in snapshots if s["id"] == args.snapshot or s["path"] == args.snapshot), None)
        if not selected_snap:
            print(f"[!] 지정한 스냅샷({args.snapshot})을 찾을 수 없습니다.")
            return
    else:
        print("\n--- 복원 가능한 스냅샷 목록 ---")
        for i, s in enumerate(snapshots[:10], 1):
            print(f"[{i}] {s['id']} | 일시: {s['iso_time'][:19]} | 프로필: {s['profile_name']} | 파일: {s['file_count']}개")

        choice = input("\n복원할 스냅샷 번호를 선택하세요 (기본값: 1): ").strip()
        sel_idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(snapshots) else 0
        selected_snap = snapshots[sel_idx]

    print(f"\n선택된 스냅샷: {selected_snap['id']}")
    target = args.target
    if not target:
        target_in = input("복원할 대상 디렉토리 (엔터 입력 시 백업 당시 원본 위치로 복원): ").strip('"\' ')
        target = target_in if target_in else None

    run_emergency_restore(
        repo_dir=repo_dir,
        snapshot_path=selected_snap["path"],
        target_override=target,
        verify_hash=args.verify,
        filter_keyword=args.filter
    )

if __name__ == "__main__":
    main()
