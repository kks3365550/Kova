import os
import sys
import re
import time
import json
import shutil
import ctypes
import subprocess
import datetime
import threading
from typing import Dict, Any, Optional, Callable, List

class SystemImageManager:
    _lock = threading.Lock()
    _is_running = False
    _logs = []
    _process = None
    _progress_percent = 0.0
    _current_phase = "대기 중"
    _status = "idle"

    @classmethod
    def is_admin(cls) -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    @classmethod
    def get_status(cls, target_drive: str = "D:") -> Dict[str, Any]:
        target_drive = target_drive.rstrip("\\/")
        if not target_drive.endswith(":"):
            target_drive += ":"

        # 1. Disk usage
        try:
            c_total, c_used, c_free = shutil.disk_usage("C:\\")
        except Exception:
            c_total, c_used, c_free = 0, 0, 0

        try:
            d_total, d_used, d_free = shutil.disk_usage(target_drive + "\\")
        except Exception:
            d_total, d_used, d_free = 0, 0, 0

        # 2. Check 통백업 아카이브 (DriveArchiveManager) 및 WindowsImageBackup 폴더
        has_backup = False
        backup_size = 0
        last_modified = None
        backup_path = None
        backup_type = None
        is_verified = False
        backup_name = None

        try:
            from core.drive_archive import DriveArchiveManager
            archives = DriveArchiveManager.get_instance().list_archives()
            if archives:
                latest = archives[0]
                has_backup = True
                backup_size = latest.get("size_bytes", 0)
                last_modified = latest.get("modified_at") or latest.get("created_at")
                backup_path = latest.get("path")
                backup_type = latest.get("backup_type")
                is_verified = latest.get("is_verified", False)
                backup_name = latest.get("name")
        except Exception:
            archives = []

        if not has_backup:
            backup_folder = os.path.join(target_drive + "\\", "WindowsImageBackup")
            if os.path.exists(backup_folder):
                try:
                    mtime = os.path.getmtime(backup_folder)
                    last_modified = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    total_sz = 0
                    for root, dirs, files in os.walk(backup_folder):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                total_sz += os.path.getsize(fp)
                            except OSError:
                                pass
                    if total_sz > 0:
                        has_backup = True
                        backup_size = total_sz
                        backup_path = backup_folder
                        backup_type = "wbadmin"
                except Exception:
                    pass
            if not backup_path:
                backup_path = backup_folder
        else:
            backup_folder = backup_path

        with cls._lock:
            progress_percent = cls._progress_percent
            current_phase = cls._current_phase
            status = cls._status
            is_running = cls._is_running

        return {
            "target_drive": target_drive,
            "is_admin": cls.is_admin(),
            "is_running": is_running,
            "progress_percent": progress_percent,
            "current_phase": current_phase,
            "status": status,
            "c_drive": {
                "total_bytes": c_total,
                "used_bytes": c_used,
                "free_bytes": c_free,
                "used_gb": round(c_used / (1024 ** 3), 1),
                "total_gb": round(c_total / (1024 ** 3), 1)
            },
            "target_drive_info": {
                "drive": target_drive,
                "total_bytes": d_total,
                "used_bytes": d_used,
                "free_bytes": d_free,
                "free_gb": round(d_free / (1024 ** 3), 1),
                "total_gb": round(d_total / (1024 ** 3), 1),
                "sufficient_space": d_free > (c_used * 0.7) # Image is compressed ~50-70%
            },
            "backup_info": {
                "exists": has_backup,
                "path": backup_path or backup_folder,
                "last_modified": last_modified,
                "size_bytes": backup_size,
                "size_gb": round(backup_size / (1024 ** 3), 2),
                "is_verified": is_verified,
                "backup_type": backup_type,
                "name": backup_name
            }
        }

    @classmethod
    def append_log(cls, msg: str, level: str = "INFO"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {msg}"
        with cls._lock:
            cls._logs.append(entry)
            if len(cls._logs) > 500:
                cls._logs.pop(0)

    @classmethod
    def get_logs(cls) -> Dict[str, Any]:
        with cls._lock:
            return {
                "is_running": cls._is_running,
                "progress_percent": cls._progress_percent,
                "current_phase": cls._current_phase,
                "status": cls._status,
                "logs": list(cls._logs)
            }

    @classmethod
    def start_backup(cls, target_drive: str = "D:", log_callback: Optional[Callable[[str], None]] = None, engine: str = "wbadmin") -> Dict[str, Any]:
        with cls._lock:
            if cls._is_running:
                return {"success": False, "error": "이미 윈도우 시스템 이미지 백업이 진행 중입니다."}
            cls._is_running = True
            cls._progress_percent = 0.0
            cls._current_phase = "백업 준비 중..."
            cls._status = "running"
            cls._logs.clear()

        target_drive = target_drive.rstrip("\\/")
        if not target_drive.endswith(":"):
            target_drive += ":"

        def _run_worker():
            t_start = time.time()
            last_pct = -1

            def _parse_and_log(raw_line: str):
                nonlocal last_pct
                line = raw_line.strip()
                if not line:
                    return

                # Search for percentage: e.g. 15%, 15.5%
                m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
                if m:
                    try:
                        pct = float(m.group(1))
                        if 0 <= pct <= 100:
                            with cls._lock:
                                cls._progress_percent = pct
                                cls._current_phase = f"볼륨 데이터 복사 중 ({pct:.0f}%)"
                            if int(pct) != last_pct and int(pct) % 2 == 0:
                                last_pct = int(pct)
                                cls.append_log(f"  - 볼륨 복사 진행률: {pct:.0f}%")
                                if log_callback:
                                    log_callback(f"  - 볼륨 복사 진행률: {pct:.0f}%")
                    except Exception:
                        pass
                else:
                    cls.append_log(line)
                    if log_callback:
                        log_callback(line)
                    if "준비" in line or "prepar" in line.lower():
                        with cls._lock:
                            cls._current_phase = "볼륨 및 섀도 복사본(VSS) 준비 중"
                    elif "검색" in line or "scan" in line.lower():
                        with cls._lock:
                            cls._current_phase = "볼륨 목록 검색 중"

            try:
                if engine == "kimg":
                    result = cls._run_kimg_backup(target_drive, t_start, log_callback)
                    if result in ("ok", "cancelled"):
                        return
                    cls.append_log("kimg 백업이 검증을 통과하지 못해 wbadmin으로 대체합니다.", level="WARNING")
                    if log_callback:
                        log_callback("kimg 백업이 검증을 통과하지 못해 wbadmin으로 대체합니다.")
                cls.append_log(f"윈도우 베어메탈 시스템 이미지 백업을 시작합니다 (대상: {target_drive})...")
                cls.append_log("부팅(EFI) 파티션, 복구 파티션 및 C: 드라이브 전체 캡처를 준비합니다.")

                cmd = f"wbadmin start backup -backupTarget:{target_drive} -include:C: -allCritical -quiet"
                cls.append_log(f"실행 명령: {cmd}")

                # Check admin elevation
                if not cls.is_admin():
                    cls.append_log("관리자 권한 승격(UAC)을 요청하여 백업을 백그라운드에서 실행합니다...", level="WARNING")
                    log_file = os.path.join(target_drive + "\\", "system_image_backup.log")
                    if os.path.exists(log_file):
                        try:
                            os.remove(log_file)
                        except Exception:
                            pass

                    ps_cmd = f"Start-Process cmd -ArgumentList '/c wbadmin start backup -backupTarget:{target_drive} -include:C: -allCritical -quiet > \"{log_file}\" 2>&1' -WindowStyle Hidden -Verb RunAs -Wait"
                    
                    p_flags = 0
                    if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                        p_flags = subprocess.CREATE_NO_WINDOW
                    p = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_cmd], creationflags=p_flags)
                    with cls._lock:
                        cls._process = p
                    
                    # Monitor log file while running (splitting on \r and \n)
                    last_pos = 0
                    read_buf = bytearray()
                    while p.poll() is None:
                        time.sleep(1)
                        if os.path.exists(log_file):
                            try:
                                with open(log_file, "rb") as f:
                                    f.seek(last_pos)
                                    chunk = f.read()
                                    last_pos = f.tell()
                                    for b in chunk:
                                        if b in (10, 13):
                                            if read_buf:
                                                try:
                                                    s = read_buf.decode("cp949", errors="replace")
                                                except Exception:
                                                    s = read_buf.decode("utf-8", errors="replace")
                                                read_buf.clear()
                                                _parse_and_log(s)
                                        else:
                                            read_buf.append(b)
                            except Exception:
                                pass

                    # Read remaining tail
                    if os.path.exists(log_file):
                        try:
                            with open(log_file, "rb") as f:
                                f.seek(last_pos)
                                chunk = f.read()
                                for b in chunk:
                                    if b in (10, 13):
                                        if read_buf:
                                            try:
                                                s = read_buf.decode("cp949", errors="replace")
                                            except Exception:
                                                s = read_buf.decode("utf-8", errors="replace")
                                            read_buf.clear()
                                            _parse_and_log(s)
                                    else:
                                        read_buf.append(b)
                        except Exception:
                            pass

                    ret_code = p.returncode
                    cls.append_log(f"백업 작업 프로세스 종료 코드: {ret_code}")
                else:
                    # Run directly elevated with unbuffered byte stream reader
                    popen_kwargs = {
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.STDOUT,
                        "bufsize": 0
                    }
                    if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    p = subprocess.Popen(
                        ["wbadmin", "start", "backup", f"-backupTarget:{target_drive}", "-include:C:", "-allCritical", "-quiet"],
                        **popen_kwargs
                    )
                    with cls._lock:
                        cls._process = p

                    read_buf = bytearray()
                    while True:
                        b = p.stdout.read(1)
                        if not b:
                            break
                        if b in (b'\r', b'\n'):
                            if read_buf:
                                try:
                                    s = read_buf.decode("cp949", errors="replace")
                                except Exception:
                                    s = read_buf.decode("utf-8", errors="replace")
                                read_buf.clear()
                                _parse_and_log(s)
                        else:
                            read_buf.extend(b)

                    p.wait()
                    ret_code = p.returncode

                if ret_code == 0:
                    elapsed_min = round((time.time() - t_start) / 60, 1)
                    with cls._lock:
                        cls._progress_percent = 100.0
                        cls._current_phase = "백업 완료"
                        cls._status = "completed"
                    cls.append_log(f"🎉 윈도우 전체 베어메탈 시스템 이미지 백업이 성공적으로 완료되었습니다! (소요 시간: {elapsed_min}분)", level="SUCCESS")
                else:
                    with cls._lock:
                        cls._current_phase = f"백업 종료 (코드: {ret_code})"
                        cls._status = "failed"
                    cls.append_log(f"백업이 종료되었습니다 (코드: {ret_code}). 로그를 확인하세요.", level="WARNING")

            except Exception as e:
                with cls._lock:
                    cls._current_phase = f"오류 발생: {str(e)}"
                    cls._status = "failed"
                cls.append_log(f"시스템 이미지 백업 중 오류 발생: {str(e)}", level="ERROR")
            finally:
                with cls._lock:
                    if cls._process and cls._process.poll() is None:
                        try:
                            cls._process.terminate()
                            cls._process.wait(timeout=5)
                        except Exception:
                            pass
                    cls._is_running = False
                    cls._process = None

        t = threading.Thread(target=_run_worker, daemon=True)
        t.start()
        return {"success": True, "message": "시스템 이미지 백업 작업이 시작되었습니다."}

    @staticmethod
    def _find_kimg_exe() -> Optional[str]:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for c in (os.environ.get("KOVA_KIMG_EXE"), os.path.join(base, "engine_rust", "kimg.exe")):
            if c and os.path.isfile(c):
                return c
        return None

    @staticmethod
    def _collect_disk_layout() -> Optional[Dict[str, Any]]:
        """C:가 있는 디스크의 파티션 구성·펌웨어 종류를 수집한다(복원 시 diskpart 레이아웃 결정용). 실패하면 None."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        ps = ("$ErrorActionPreference='Stop';"
              "$p=Get-Partition -DriveLetter C;$d=Get-Disk -Number $p.DiskNumber;"
              "$parts=@(Get-Partition -DiskNumber $d.Number|ForEach-Object{[pscustomobject]@{"
              "number=$_.PartitionNumber;size=[int64]$_.Size;type=[string]$_.Type;gpt_type=[string]$_.GptType;"
              "drive=$(if($_.DriveLetter -and [int]$_.DriveLetter -gt 0){[string]$_.DriveLetter}else{''})}});"
              "[pscustomobject]@{partition_style=[string]$d.PartitionStyle;disk_size=[int64]$d.Size;"
              "c_partition_size=[int64]$p.Size;c_partition_number=$p.PartitionNumber;"
              "firmware=[string]$env:firmware_type;partitions=$parts}|ConvertTo-Json -Depth 4 -Compress")
        try:
            cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                creationflags=flags, timeout=60)
            if cp.returncode != 0 or not (cp.stdout or "").strip():
                return None
            data = json.loads(cp.stdout)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _write_kimg_meta(final_path: str, layout: Optional[Dict[str, Any]], exe: str) -> None:
        """이미지 옆에 `<이미지>.json` 저장(원자적). 호출자가 예외를 처리한다."""
        meta = {
            "format": 1,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": "C:",
            "layout": layout,
            "image_bytes": os.path.getsize(final_path),
            "verified_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        try:
            st = os.stat(exe)
            meta["kimg_exe_size"] = st.st_size
            meta["kimg_exe_mtime"] = int(st.st_mtime)
        except OSError:
            pass
        tmp = final_path + ".json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, final_path + ".json")

    @classmethod
    def _run_kimg_backup(cls, target_drive: str, t_start: float, log_callback: Optional[Callable[[str], None]] = None) -> str:
        """kimg로 C: 볼륨 이미지를 쓰고 verify까지 통과해야 'ok'. 실패는 'failed'(호출자가 wbadmin으로 대체), 중단은 'cancelled'."""
        def log(msg, level="INFO"):
            cls.append_log(msg, level=level)
            if log_callback:
                log_callback(msg)

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        exe = cls._find_kimg_exe()
        if not exe:
            log("kimg.exe를 찾을 수 없습니다.", "WARNING")
            return "failed"
        if not cls.is_admin():
            log("kimg는 관리자 권한이 필요합니다.", "WARNING")
            return "failed"
        try:
            _, c_used, _ = shutil.disk_usage("C:\\")
            _, _, free = shutil.disk_usage(target_drive + "\\")
            if free < c_used * 0.7:
                log("대상 드라이브 여유 공간이 부족합니다.", "WARNING")
                return "failed"
        except Exception as e:
            log(f"디스크 공간 확인 실패: {e}", "WARNING")
            return "failed"

        out_dir = os.path.join(target_drive + "\\", "KovaImages")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = os.path.join(out_dir, f"C_{stamp}.kimg")
        part_path = final_path + ".part"
        shadow_id = None
        ok = False
        try:
            os.makedirs(out_dir, exist_ok=True)
            with cls._lock:
                cls._current_phase = "VSS 스냅샷 생성 중 (kimg)"
            ps = ("$r=(Get-WmiObject -List Win32_ShadowCopy).Create('C:\\','ClientAccessible');"
                  "if($r.ReturnValue -ne 0){exit 2};"
                  "$d=(Get-WmiObject Win32_ShadowCopy|Where-Object{$_.ID -eq $r.ShadowID}).DeviceObject.TrimEnd('\\');"
                  "Write-Output ($r.ShadowID+'|'+$d)")
            cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, creationflags=flags)
            line = (cp.stdout or "").strip().splitlines()[-1] if (cp.stdout or "").strip() else ""
            if cp.returncode != 0 or "|" not in line:
                log(f"VSS 스냅샷 생성 실패 (코드 {cp.returncode}): {((cp.stderr or '').strip() or (cp.stdout or '').strip())[:200]}", "WARNING")
                return "failed"
            shadow_id, dev = line.split("|", 1)
            log(f"스냅샷 생성: {dev}")

            for step, args in (("이미지 쓰기", ["write", "--source", dev, "--target", part_path, "--threads", "4", "--chunk-mb", "4", "--level", "1"]),
                               ("이미지 검증", ["verify", "--image", part_path, "--threads", "4"])):
                with cls._lock:
                    cls._current_phase = f"kimg {step} 중"
                p = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
                with cls._lock:
                    cls._process = p
                tail = []
                for raw in p.stdout:
                    t = raw.decode("utf-8", errors="replace").strip()
                    if t:
                        tail.append(t)
                        del tail[:-3]
                p.wait()
                if cls._status == "cancelled":
                    return "cancelled"
                if p.returncode != 0:
                    log(f"kimg {step} 실패 (코드 {p.returncode}): {' / '.join(tail)}", "WARNING")
                    return "failed"
                log(f"kimg {step} 성공")

            os.replace(part_path, final_path)
            ok = True
            try:
                cls._write_kimg_meta(final_path, cls._collect_disk_layout(), exe)
            except Exception as e:
                log(f"kimg 메타데이터(.json) 저장 실패 (이미지는 유효): {e}", "WARNING")
            with cls._lock:
                cls._progress_percent = 100.0
                cls._current_phase = "백업 완료 (kimg)"
                cls._status = "completed"
            log(f"kimg 볼륨 이미지 백업 완료 (검증 통과): {final_path} (소요 {round((time.time() - t_start) / 60, 1)}분)", "SUCCESS")
            return "ok"
        except Exception as e:
            log(f"kimg 백업 중 오류: {e}", "WARNING")
            return "failed"
        finally:
            if shadow_id:
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    f"(Get-WmiObject Win32_ShadowCopy|Where-Object{{$_.ID -eq '{shadow_id}'}}).Delete()"],
                                   capture_output=True, creationflags=flags)
                except Exception:
                    pass
            if not ok and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass

    @classmethod
    def stop_backup(cls) -> Dict[str, Any]:
        cls.append_log("백업 중단 명령을 전송합니다...")
        try:
            kwargs = {"capture_output": True, "text": True}
            if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(["wbadmin", "stop", "job", "-quiet"], **kwargs)
            with cls._lock:
                if cls._process:
                    cls._process.terminate()
                    try:
                        cls._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        cls._process.kill()
                        cls._process.wait()
                cls._is_running = False
                cls._status = "cancelled"
                cls._current_phase = "백업 중단됨"
            cls.append_log("백업 작업이 중단되었습니다.", level="WARNING")
            return {"success": True, "message": "백업 작업 중단 요청 완료"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    def list_vhdx_images(cls, search_drive: str = "D:") -> List[Dict[str, Any]]:
        """
        지정 드라이브(기본 D:) 및 WindowsImageBackup 내의 VHDX 이미지들을 탐색하고
        각 파일의 마운트 상태와 볼륨 정보를 반환.
        """
        search_drive = search_drive.rstrip("\\/") + "\\"
        results = []
        target_dir = os.path.join(search_drive, "WindowsImageBackup")
        if not os.path.exists(target_dir):
            return results

        # 현재 마운트된 디스크 이미지 정보 수집
        mounted_map = cls._get_all_mounted_images()

        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.lower().endswith(".vhdx"):
                    full_path = os.path.join(root, file)
                    try:
                        sz = os.path.getsize(full_path)
                        mtime = os.path.getmtime(full_path)
                        mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                        
                        norm_path = os.path.normpath(full_path).lower()
                        mount_info = mounted_map.get(norm_path, None)

                        results.append({
                            "path": full_path,
                            "filename": file,
                            "size_bytes": sz,
                            "size_gb": round(sz / (1024 ** 3), 2),
                            "modified_at": mtime_str,
                            "is_mounted": bool(mount_info and mount_info.get("attached")),
                            "mount_drive": mount_info.get("drive_letter") if mount_info else None
                        })
                    except OSError:
                        continue

        # 영구 캐시 저장 및 폴백 (권한 부족/일시적 스캔 실패 대비)
        cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ai", "vhdx_cache.json")
        seen_vhdx = {os.path.normpath(r["path"]).lower() for r in results}

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f_c:
                    cached_list = json.load(f_c)
                    if isinstance(cached_list, list):
                        for c_item in cached_list:
                            cp = c_item.get("path")
                            if cp and os.path.exists(cp):
                                norm_p = os.path.normpath(cp).lower()
                                drive_prefix = search_drive.lower()
                                if norm_p.startswith(drive_prefix) and norm_p not in seen_vhdx:
                                    mount_info = mounted_map.get(norm_p, None)
                                    item_copy = dict(c_item)
                                    item_copy["is_mounted"] = bool(mount_info and mount_info.get("attached"))
                                    item_copy["mount_drive"] = mount_info.get("drive_letter") if mount_info else None
                                    results.append(item_copy)
                                    seen_vhdx.add(norm_p)
            except Exception:
                pass

        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            # 물리 파일이 존재하는 항목만 캐시에 보관
            valid_cached = [r for r in results if os.path.exists(r.get("path", ""))]
            with open(cache_path, "w", encoding="utf-8") as f_c:
                json.dump(valid_cached, f_c, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # 최신 파일 순으로 정렬
        results.sort(key=lambda x: x["modified_at"], reverse=True)
        return results

    @classmethod
    def _get_all_mounted_images(cls) -> Dict[str, Dict[str, Any]]:
        """현재 시스템에 마운트된 모든 VHD/VHDX 이미지 맵 반환 (소문자 정규화 경로 -> 정보)"""
        if sys.platform != "win32":
            return {}

        ps_script = (
            "Get-DiskImage -ErrorAction SilentlyContinue | "
            "ForEach-Object { "
            "  $img = $_; "
            "  $vol = $img | Get-Volume -ErrorAction SilentlyContinue; "
            "  [PSCustomObject]@{ "
            "    ImagePath = $img.ImagePath; "
            "    Attached = $img.Attached; "
            "    DriveLetter = if ($vol) { $vol.DriveLetter } else { $null } "
            "  } "
            "} | ConvertTo-Json -Compress"
        )
        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=flags
            )
            if res.returncode != 0 or not res.stdout.strip():
                return {}
            data = json.loads(res.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            out = {}
            for item in data:
                ipath = item.get("ImagePath")
                if ipath:
                    norm = os.path.normpath(ipath).lower()
                    dl = item.get("DriveLetter")
                    out[norm] = {
                        "attached": item.get("Attached", False),
                        "drive_letter": f"{dl}:" if dl else None
                    }
            return out
        except Exception as e:
            cls.append_log(f"마운트된 디스크 이미지 조회 예외: {e}", level="WARNING")
            return {}

    @classmethod
    def delete_vhdx(cls, vhdx_path: str) -> Dict[str, Any]:
        """
        VHDX 이미지를 안전하게 삭제: 마운트 여부 확인 및 자동 언마운트 후 파일 삭제, 캐시 즉시 제거
        """
        if not vhdx_path or not isinstance(vhdx_path, str):
            return {"success": False, "error": "VHDX 경로가 유효하지 않습니다."}

        norm_path = os.path.normpath(vhdx_path)
        if not norm_path.lower().endswith(".vhdx"):
            return {"success": False, "error": "VHDX(.vhdx) 파일만 삭제할 수 있습니다."}

        if not os.path.exists(norm_path):
            return {"success": False, "error": f"VHDX 파일을 찾을 수 없습니다: {norm_path}"}

        if os.path.isdir(norm_path):
            return {"success": False, "error": f"디렉토리는 삭제할 수 없습니다: {norm_path}"}

        parent = os.path.dirname(norm_path)
        if parent in ("", ".", "..") or parent.lower() in ("c:\\", "c:", "d:\\", "d:", "e:\\", "e:"):
            return {"success": False, "error": "시스템 루트 폴더의 파일은 삭제할 수 없습니다."}

        # 1. 마운트된 경우 선제적 언마운트
        try:
            mounted_map = cls._get_all_mounted_images()
            if mounted_map.get(norm_path.lower(), {}).get("attached"):
                cls.append_log(f"삭제 전 VHDX 마운트 자동 해제 시도: {norm_path}")
                dismount_result = cls.dismount_vhdx(norm_path)
                if not dismount_result.get("success"):
                    return {"success": False, "error": f"삭제 전 언마운트 실패: {dismount_result.get('error')}"}
        except Exception as e:
            cls.append_log(f"마운트 상태 확인 중 예외 (계속 진행): {e}", level="WARNING")

        # 2. 물리 파일 크기 계측 및 삭제
        try:
            sz = os.path.getsize(norm_path)
            os.remove(norm_path)
            cls.append_log(f"VHDX 파일 물리 삭제 완료: {norm_path} ({round(sz / (1024**3), 2)} GB 확보)", level="SUCCESS")
        except Exception as e:
            return {"success": False, "error": f"VHDX 파일 삭제 실패: {e}"}

        # 3. 캐시 파일들에서 즉시 Purge
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache_paths = [
                os.path.join(project_root, ".ai", "vhdx_cache.json"),
                os.path.join(project_root, ".ai", "archives_cache.json"),
                os.path.join("D:\\", "MyBackup_Repository", "archives", ".archives_cache.json"),
            ]
            removed_norm = norm_path.lower()

            for cp in cache_paths:
                if not os.path.exists(cp):
                    continue
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if isinstance(data, list):
                        data = [item for item in data if os.path.normpath(str(item.get("path", ""))).lower() != removed_norm]
                    elif isinstance(data, dict) and "archives" in data:
                        data["archives"] = [
                            item for item in data["archives"]
                            if os.path.normpath(str(item.get("path", ""))).lower() != removed_norm
                            and os.path.normpath(str(item.get("vhdx_path", ""))).lower() != removed_norm
                        ]

                    with open(cp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "success": True,
            "message": f"VHDX 이미지가 성공적으로 삭제되었습니다. ({round(sz / (1024 ** 3), 2)} GB 확보)",
            "freed_bytes": sz,
            "freed_gb": round(sz / (1024 ** 3), 2)
        }

    @classmethod
    def mount_vhdx(cls, vhdx_path: str, read_only: bool = True) -> Dict[str, Any]:
        """
        VHDX 이미지를 읽기 전용으로 안전하게 가상 드라이브에 마운트.
        """
        if not os.path.exists(vhdx_path):
            return {"success": False, "error": f"VHDX 파일을 찾을 수 없습니다: {vhdx_path}"}

        access_opt = "-Access ReadOnly" if read_only else ""
        escaped_path = vhdx_path.replace("'", "''")
        ps_script = (
            f"$p = '{escaped_path}'; "
            f"$img = Mount-DiskImage -ImagePath $p {access_opt} -PassThru; "
            f"$vol = $img | Get-Volume -ErrorAction SilentlyContinue; "
            f"[PSCustomObject]@{{"
            f"  Attached = $img.Attached; "
            f"  DriveLetter = if ($vol) {{ $vol.DriveLetter }} else {{ $null }} "
            f"}} | ConvertTo-Json -Compress"
        )

        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=flags
            )
            if res.returncode != 0:
                return {"success": False, "error": f"VHDX 마운트 실패: {res.stderr.strip() or res.stdout.strip()}"}
            
            data = json.loads(res.stdout.strip())
            drive_letter = data.get("DriveLetter")
            drive_str = f"{drive_letter}:" if drive_letter else "마운트됨(문자 미할당)"
            cls.append_log(f"VHDX 마운트 성공 ({drive_str}): {vhdx_path}", level="SUCCESS")
            return {
                "success": True,
                "message": f"성공적으로 마운트되었습니다 ({drive_str}).",
                "drive_letter": drive_str,
                "attached": data.get("Attached", True)
            }
        except Exception as e:
            return {"success": False, "error": f"VHDX 마운트 중 오류 발생: {str(e)}"}

    @classmethod
    def dismount_vhdx(cls, vhdx_path: str) -> Dict[str, Any]:
        """
        마운트된 VHDX 이미지 안전 분리(언마운트).
        """
        if not os.path.exists(vhdx_path):
            return {"success": False, "error": f"VHDX 파일을 찾을 수 없습니다: {vhdx_path}"}

        escaped_path = vhdx_path.replace("'", "''")
        ps_script = f"Dismount-DiskImage -ImagePath '{escaped_path}'"

        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=flags
            )
            if res.returncode == 0:
                cls.append_log(f"VHDX 언마운트 완료: {vhdx_path}", level="INFO")
                return {"success": True, "message": "VHDX가 안전하게 언마운트되었습니다."}
            else:
                return {"success": False, "error": f"VHDX 언마운트 실패: {res.stderr.strip()}"}
        except Exception as e:
            return {"success": False, "error": f"VHDX 언마운트 중 오류: {str(e)}"}
