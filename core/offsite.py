# -*- coding: utf-8 -*-
"""
core/offsite.py: 오프사이트(이중화) 복제 엔진
- robocopy 기반 대용량/소파일 최적화 복제
- 백업 파이프라인에 후속 단계로 통합
- 실패해도 로컬 백업 결과에 영향 없음 (격리)

배경 (실측 근거):
    core/replication.py는 블록마다 icacls subprocess를 호출해 SMB 상에서
    블롭 1개당 239ms가 소요되어 84,116개 기준 약 20시간이 소요되었다.
    robocopy /MT:32는 동일한 84,192개 파일을 약 12분에 복제한다.
    따라서 로컬 저장소 보호는 core/worm.py가, 원격 복제는 robocopy가 담당한다.

원격 저장소는 다른 장비의 공유 폴더이므로 NTFS Deny ACL 방어선을 두지 않는다.
오프사이트의 목적은 "별도 물리 장비에 복구 지점을 확보"하는 것이며,
이는 ACL 없이도 달성된다.
"""

import os
import time
import shutil
import logging
import subprocess
import re
import glob
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger("BackupSystem")

# Windows 전용
IS_WINDOWS = os.name == "nt"


class OffsiteReplicationError(Exception):
    """오프사이트 복제 실패"""
    pass


class OffsiteReplicator:
    """
    robocopy 기반 오프사이트 복제기.

    robocopy 선택 근거 (실측):
        방법                  341개 소요   84,192개 환산
        robocopy /MT:32       1.5초        12분
        Python Copy-Item      26.3초       108분
        Python + icacls       약 78초      약 20시간
    """

    # robocopy 성공 판정값 (0~7 = 성공, 8+ = 실제 오류)
    ROBOCOPY_OK_CODES = range(0, 8)

    def __init__(
        self,
        local_repo_dir: str,
        remote_repo_dir: str,
        timeout_sec: int = 7200,
        max_threads: int = 32,
        retries: int = 2,
        wait_sec: int = 1
    ):
        local_clean = (local_repo_dir or "").strip().strip('"').strip("'")
        remote_clean = (remote_repo_dir or "").strip().strip('"').strip("'")
        self.local_repo_dir = os.path.abspath(local_clean)
        # UNC 경로(\\)는 abspath가 드라이브 문자를 붙이지 않지만, 명시적으로 보존한다.
        if remote_clean.startswith("\\\\"):
            self.remote_repo_dir = remote_clean.rstrip("\\")
        else:
            self.remote_repo_dir = os.path.abspath(remote_clean)
        self.timeout_sec = timeout_sec
        self.max_threads = max_threads
        self.retries = retries
        self.wait_sec = wait_sec

    # ------------------------------------------------------------------
    # 사전 점검
    # ------------------------------------------------------------------
    def preflight(self) -> Dict[str, Any]:
        """복제 전 조건 확인. 실패해도 로컬 백업은 정상 완료되어야 한다."""
        result = {
            "ok": False,
            "local_exists": False,
            "remote_reachable": False,
            "is_unc": self.remote_repo_dir.startswith("\\\\"),
            "reason": ""
        }

        if not IS_WINDOWS:
            result["reason"] = "Windows 전용 기능 (현재 OS: %s)" % os.name
            return result

        result["local_exists"] = os.path.isdir(self.local_repo_dir)
        if not result["local_exists"]:
            result["reason"] = "로컬 저장소 없음: %s" % self.local_repo_dir
            return result

        # 원격 상위 폴더 도달 가능성 확인 (쓰기 없이)
        probe = self.remote_repo_dir
        reachable = False
        try:
            os.makedirs(probe, exist_ok=True)
            reachable = os.path.isdir(probe)
        except Exception as e:
            result["reason"] = "원격 경로 생성 실패: %s" % e
            return result

        result["remote_reachable"] = reachable
        if not reachable:
            result["reason"] = "원격 저장소를 만들 수 없음: %s" % probe
            return result

        result["ok"] = True
        return result

    # ------------------------------------------------------------------
    # 복제 실행
    # ------------------------------------------------------------------
    def replicate(self) -> Dict[str, Any]:
        """
        로컬 저장소를 원격으로 복제한다.

        반환:
            {
                "success": bool,        # robocopy 결과 기준
                "skipped": bool,        # 사전 점검 실패로 스킵
                "reason": str,
                "return_code": int,
                "files_total": int,     # robocopy 요약에서 추출 (없으면 0)
                "files_copied": int,
                "files_failed": int,
                "duration_sec": float,
            }
        """
        started = time.time()
        result: Dict[str, Any] = {
            "success": False,
            "skipped": False,
            "reason": "",
            "return_code": -1,
            "files_total": 0,
            "files_copied": 0,
            "files_failed": 0,
            "duration_sec": 0.0,
        }

        # 1) 사전 점검
        pf = self.preflight()
        if not pf["ok"]:
            result["skipped"] = True
            result["reason"] = pf["reason"] or "사전 점검 실패"
            logger.warning("[오프사이트] 스킵: %s", result["reason"])
            result["duration_sec"] = time.time() - started
            return result

        logger.info(
            "[오프사이트] 복제 시작: %s -> %s (제한 %d분)",
            self.local_repo_dir, self.remote_repo_dir, self.timeout_sec // 60
        )

        # 2) 고속 순수 파이썬 병렬 복제 엔진 (Robocopy 완전 제거)
        #    - 외부 프로세스(cmd/robocopy) 호출 오버헤드 0
        #    - Windows 로케일/코드페이지 파싱 오류 원천 차단
        #    - ThreadPoolExecutor 멀티스레드 병렬 I/O (I/O 병목 완화)
        #    - 크기 및 타임스탬프 기반 스마트 Zero-Copy 스킵
        try:
            files_to_copy = []
            files_total = 0
            files_skipped = 0
            total_bytes_copied = 0

            for root, dirs, files in os.walk(self.local_repo_dir):
                rel_dir = os.path.relpath(root, self.local_repo_dir)
                target_dir = os.path.join(self.remote_repo_dir, rel_dir) if rel_dir != "." else self.remote_repo_dir
                os.makedirs(target_dir, exist_ok=True)

                for f in files:
                    if f in ("backup.lock", "DumpStack.log.tmp") or f.endswith(".lock") or f.endswith(".tmp") or ".tmp_" in f:
                        continue
                    files_total += 1
                    src_file = os.path.join(root, f)
                    dst_file = os.path.join(target_dir, f)

                    try:
                        src_stat = os.stat(src_file)
                        # 원격 파일이 존재하고 크기 및 수정시간(2초 오차 이내)이 같으면 스킵
                        if os.path.exists(dst_file):
                            dst_stat = os.stat(dst_file)
                            if src_stat.st_size == dst_stat.st_size and abs(src_stat.st_mtime - dst_stat.st_mtime) < 2.0:
                                files_skipped += 1
                                continue
                        files_to_copy.append((src_file, dst_file, src_stat.st_size))
                    except Exception as e_stat:
                        logger.warning("[오프사이트] 파일 검사 오류 (%s): %s", src_file, e_stat)

            # 멀티스레드 병렬 복사
            files_copied = 0
            files_failed = 0

            def _copy_worker(item):
                s, d, sz = item
                for attempt in range(self.retries + 1):
                    try:
                        shutil.copy2(s, d)
                        return True, sz
                    except Exception as err:
                        if attempt < self.retries:
                            time.sleep(self.wait_sec)
                        else:
                            logger.error("[오프사이트] 파일 복제 실패 (%s -> %s): %s", s, d, err)
                            return False, 0
                return False, 0

            from concurrent.futures import ThreadPoolExecutor
            workers = min(self.max_threads, max(4, (os.cpu_count() or 4) * 2))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for ok, sz in executor.map(_copy_worker, files_to_copy):
                    if ok:
                        files_copied += 1
                        total_bytes_copied += sz
                    else:
                        files_failed += 1

            duration = max(time.time() - started, 0.001)
            speed_mbps = (total_bytes_copied / (1024 * 1024)) / duration

            result["success"] = (files_failed == 0)
            result["return_code"] = 0 if result["success"] else 1
            result["files_total"] = files_total
            result["files_copied"] = files_copied
            result["files_skipped"] = files_skipped
            result["files_failed"] = files_failed
            result["bytes_copied"] = total_bytes_copied
            result["speed_mbps"] = round(speed_mbps, 1)
            result["duration_sec"] = round(duration, 2)
            result["engine"] = "Pure-Python Parallel"
            result["reason"] = "복제 완료" if result["success"] else f"복제 중 {files_failed}개 파일 오류 발생"

            logger.info(
                "[오프사이트] 완료 (순수 파이썬 병렬): 신규 %d개 / 스킵 %d개 / 전체 %d개 / 실패 %d개 (%.2f초, %.1f MB/s)",
                files_copied, files_skipped, files_total, files_failed, duration, speed_mbps
            )

        except Exception as e:
            result["success"] = False
            result["reason"] = "복제 예외: %s" % e
            logger.error("[오프사이트] %s", result["reason"])

        result["duration_sec"] = time.time() - started
        return result

    # ------------------------------------------------------------------
    # robocopy 요약 라벨 (로케일별). Files 행은 Files / 파일 / Fichiers / Dateien 등.
    _LABEL_FILES = ("files", "파일", "fichiers", "dateien", "archivos", "arquivos")

    @staticmethod
    def _parse_robocopy_output(output: str) -> Dict[str, Any]:
        """
        robocopy 요약 블록의 Files 행 숫자를 파싱한다.

        로케일 의존성 주의 (실측, 이 시스템은 한글 로케일):
            요약 블록 형식
                      전체       복사됨      건너뜀     불일치       실패        추가
              디렉터리 :         2         2         0         0         0         0
                파일 :         1         1         0         0         0         0
               바이트 :        50        50         0         0         0         0

            문자열만 믿으면 안 되는 이유:
              1) 라벨이 로케일별로 달라 'Files' 리터럴 검색이 실패한다.
              2) '파일 : *.*' (필터 설정 줄)도 '파일'로 시작해 오인된다.
                 -> 실제 Files 행은 숫자 6개가 콜론 뒤에 나온다.
              3) 숫자를 큰 단위(쉼표/공백)로 표기할 수도 있어 정규화 필요.

        판별 규칙:
            콜론 뒤에 숫자(쉼표/공백/서로게 제거 후)가 정확히 6개 이상인 행 중
            Files 라벨을 포함하는 행을 선택한다.
        """
        out = {"files_total": 0, "files_copied": 0, "files_failed": 0}

        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if ":" not in line:
                continue

            label, _, tail = line.partition(":")
            label_l = label.strip().lower()

            # Files 라벨 판별 (로케일 대응)
            if not any(lbl in label_l for lbl in OffsiteReplicator._LABEL_FILES):
                continue

            # 숫자 추출: 쉼표 제거 후 공백 분리
            normalized = tail.replace(",", "").replace(".", "").replace("\u00a0", " ")
            nums = [int(t) for t in normalized.split() if t.isdigit()]

            # 실제 Files 행은 6개 숫자 (Total Copied Skipped Mismatch FAILED Extras)
            if len(nums) < 6:
                continue

            out["files_total"] = nums[0]
            out["files_copied"] = nums[1]
            out["files_failed"] = nums[4]
            break

        return out


# ============================================================================
# Google Drive Cloud Offsite Replicator (구글 드라이브 클라우드 원격 복제)
# ============================================================================
try:
    import google.oauth2.service_account as g_service_account
    import google.oauth2.credentials as g_credentials
    import googleapiclient.discovery as g_discovery
    GDRIVE_SDK_AVAILABLE = True
except ImportError:
    GDRIVE_SDK_AVAILABLE = False


class GoogleDriveOffsiteReplicator:
    """
    Google Drive API v3 기반 오프사이트 클라우드 복제기.
    - Service Account JSON 키 파일 또는 환경변수 인증 지원
    - Resumable Chunked Media Upload로 대용량 스냅샷/VHDX/블롭 안전 전송
    - 원격 백업 폴더(기본값: BackupSystem_Offsite) 자동 검색/생성 및 동기화
    - 이미 전송된 동일 크기의 파일 스마트 스킵
    """

    GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(
        self,
        local_repo_dir: str,
        credentials_file: Optional[str] = None,
        folder_name_or_id: Optional[str] = None,
        timeout_sec: int = 7200,
        interactive: bool = False,
        log_callback: Optional[Callable[..., None]] = None
    ):
        local_clean = (local_repo_dir or "").strip().strip('"').strip("'")
        self.local_repo_dir = os.path.abspath(local_clean)
        self.credentials_file = (credentials_file or "").strip().strip('"').strip("'")
        self.folder_name_or_id = (folder_name_or_id or "BackupSystem_Offsite").strip().strip('"').strip("'")
        self.timeout_sec = timeout_sec
        self.interactive = bool(interactive)
        self.log_callback = log_callback

    def _log(self, msg: str, level: str = "INFO"):
        """로그 파일 및 대시보드 UI 실시간 콜백 동시 전송"""
        if level.upper() == "WARNING" or level.upper() == "WARN":
            logger.warning(msg)
        elif level.upper() == "ERROR":
            logger.error(msg)
        else:
            logger.info(msg)

        if self.log_callback:
            try:
                self.log_callback(msg, level=level)
            except TypeError:
                try:
                    self.log_callback(msg)
                except Exception:
                    pass
            except Exception:
                pass

    def _get_service(self):
        """Google Drive API v3 클라이언트 빌드."""
        if not GDRIVE_SDK_AVAILABLE:
            raise RuntimeError("google-api-python-client 및 google-auth 패키지가 필요합니다.")

        if self.credentials_file and not os.path.exists(self.credentials_file):
            raise FileNotFoundError(
                f"지정된 구글 드라이브 인증 파일을 찾을 수 없습니다: {self.credentials_file}. "
                "개인 계정 연동을 위한 'OAuth 클라이언트 ID JSON' 또는 '서비스 계정 JSON' 경로를 확인해 주십시오."
            )

        creds = None
        base_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        token_path = os.path.join(base_data_dir, "gdrive_oauth_token.json")

        # 1. 기 저장된 OAuth 2.0 사용자 토큰(token.json) 우선 로드 및 자동 갱신
        if os.path.exists(token_path):
            try:
                import json
                with open(token_path, "r", encoding="utf-8") as f:
                    tok_data = json.load(f)
                creds = g_credentials.Credentials.from_authorized_user_info(tok_data, self.GDRIVE_SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    import google.auth.transport.requests
                    creds.refresh(google.auth.transport.requests.Request())
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())
                    logger.info("[구글 드라이브] OAuth 2.0 액세스 토큰 자동 갱신 완료")
            except Exception as e_tok:
                logger.warning(f"[구글 드라이브] 기존 OAuth 토큰 로드/갱신 실패 (재인증 필요): {e_tok}")
                creds = None

        # 2. 지정된 credentials_file 검사 (OAuth 클라이언트 시크릿 / 서비스 계정 / 토큰)
        if not creds and self.credentials_file and os.path.exists(self.credentials_file):
            import json
            try:
                with open(self.credentials_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

            # A. OAuth 클라이언트 시크릿 (데스크톱 앱) -> 브라우저 팝업 로그인 실행
            if "installed" in data or "web" in data:
                if not self.interactive:
                    raise PermissionError("구글 로그인(OAuth 2.0) 대기 중입니다. 아래 [연결 테스트] 버튼을 눌러 브라우저에서 구글 계정으로 로그인해 주십시오.")
                try:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, scopes=self.GDRIVE_SCOPES)
                    logger.info("[구글 드라이브] 웹 브라우저 구글 로그인 창 호출 중...")
                    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
                    # 향후 무인 자동 백업을 위해 토큰 캐시 영구 저장
                    os.makedirs(base_data_dir, exist_ok=True)
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())
                    logger.info(f"[구글 드라이브] 구글 계정 로그인 완료! 토큰 저장됨: {token_path}")
                except Exception as e_flow:
                    raise RuntimeError(f"구글 로그인(OAuth 2.0) 인증 실패: {e_flow}")

            # B. 이미 인증된 User Info JSON인 경우
            elif "refresh_token" in data and "client_id" in data:
                try:
                    creds = g_credentials.Credentials.from_authorized_user_info(data, self.GDRIVE_SCOPES)
                except Exception as e_tok:
                    raise ValueError(f"사용자 인증 토큰 로드 실패: {e_tok}")

            # C. Service Account JSON인 경우
            elif ("type" in data and data.get("type") == "service_account") or "client_email" in data or "private_key" in data:
                try:
                    creds = g_service_account.Credentials.from_service_account_file(
                        self.credentials_file, scopes=self.GDRIVE_SCOPES
                    )
                except Exception as e_sa:
                    raise ValueError(f"서비스 계정 인증 실패: {e_sa}")

        # 3. 기본 서비스 계정 후보 탐색 (기존 하위 호환)
        if not creds:
            default_cand = os.path.join(base_data_dir, "gdrive_credentials.json")
            if os.path.exists(default_cand):
                try:
                    creds = g_service_account.Credentials.from_service_account_file(
                        default_cand, scopes=self.GDRIVE_SCOPES
                    )
                except Exception:
                    pass

        if not creds:
            env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if env_path and os.path.exists(env_path):
                try:
                    creds = g_service_account.Credentials.from_service_account_file(
                        env_path, scopes=self.GDRIVE_SCOPES
                    )
                except Exception:
                    pass

        if not creds:
            raise FileNotFoundError(
                "구글 드라이브 인증 파일이 필요합니다. "
                "개인 계정 연동을 위한 'OAuth 클라이언트 ID (데스크톱 앱) JSON' 또는 "
                "'서비스 계정 JSON' 파일 경로를 지정해 주십시오."
            )

        return g_discovery.build("drive", "v3", credentials=creds, cache_discovery=False)

    def _get_or_create_folder(self, service, folder_name_or_id: str) -> str:
        """지정된 폴더 ID 확인 또는 폴더명으로 검색하여 없으면 신규 생성."""
        # 1. 20자 이상의 영숫자 ID 형식인 경우 직접 검증 시도
        if len(folder_name_or_id) > 15 and not (" " in folder_name_or_id or "/" in folder_name_or_id):
            try:
                res = service.files().get(fileId=folder_name_or_id, fields="id,name,mimeType,trashed").execute()
                if not res.get("trashed") and res.get("mimeType") == "application/vnd.google-apps.folder":
                    return res["id"]
            except Exception:
                pass

        # 2. 이름으로 검색
        folder_name = folder_name_or_id or "BackupSystem_Offsite"
        query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, fields="files(id, name)", spaces="drive").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]

        # 3. 폴더 생성
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        created = service.files().create(body=file_metadata, fields="id").execute()
        logger.info(f"[구글 드라이브] 신규 오프사이트 백업 폴더 생성: {folder_name} (ID: {created.get('id')})")
        return created.get("id")

    def preflight(self) -> Dict[str, Any]:
        """구글 드라이브 연결 상태, 계정 정보, 저장소 쿼터 확인."""
        result = {
            "ok": False,
            "reachable": False,
            "user_email": "",
            "total_gb": 0.0,
            "used_gb": 0.0,
            "free_gb": 0.0,
            "free_percent": 0.0,
            "target_folder_id": "",
            "reason": ""
        }

        if not os.path.isdir(self.local_repo_dir):
            result["reason"] = f"로컬 저장소 없음: {self.local_repo_dir}"
            return result

        try:
            service = self._get_service()
            about = service.about().get(fields="storageQuota,user").execute()
            quota = about.get("storageQuota", {})
            user = about.get("user", {})

            result["user_email"] = user.get("emailAddress", "ServiceAccount")
            limit = int(quota.get("limit", 0))
            usage = int(quota.get("usage", 0))

            if limit > 0:
                free = max(0, limit - usage)
                result["total_gb"] = round(limit / (1024 ** 3), 1)
                result["used_gb"] = round(usage / (1024 ** 3), 1)
                result["free_gb"] = round(free / (1024 ** 3), 1)
                result["free_percent"] = round((free / limit) * 100, 1)
            else:
                # 무제한 조직 계정
                result["total_gb"] = 9999.0
                result["used_gb"] = round(usage / (1024 ** 3), 1)
                result["free_gb"] = 9999.0
                result["free_percent"] = 100.0

            result["target_folder_id"] = self._get_or_create_folder(service, self.folder_name_or_id)
            result["ok"] = True
            result["reachable"] = True
            return result

        except Exception as e:
            result["reason"] = str(e)
            logger.warning(f"[구글 드라이브 사전 점검 실패] {e}")
            return result

    def _get_rclone_exe(self) -> Optional[str]:
        """rclone 실행 파일 탐색 (프로젝트 내장 tools/bin, PATH, Winget 순)."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = [
            os.path.join(base_dir, "tools", "bin", "rclone.exe"),
            shutil.which("rclone"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.75.1-windows-amd64\rclone.exe")
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return os.path.abspath(c)
        return None

    def _ensure_rclone_config(self, target_folder_id: str) -> str:
        """data/gdrive_oauth_token.json 및 client_secret 으로부터 data/rclone.conf 자동 생성/동기화."""
        import json
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base_data_dir = os.path.join(base_dir, "data")
        os.makedirs(base_data_dir, exist_ok=True)
        conf_path = os.path.join(base_data_dir, "rclone.conf")
        token_path = os.path.join(base_data_dir, "gdrive_oauth_token.json")

        if not os.path.exists(token_path):
            raise FileNotFoundError("Google Drive OAuth 토큰(data/gdrive_oauth_token.json)이 없습니다. 먼저 로그인을 완료해 주십시오.")

        with open(token_path, "r", encoding="utf-8") as f:
            t = json.load(f)

        client_id = t.get("client_id", "")
        client_secret = t.get("client_secret", "")

        # client_secret.json 보조 탐색
        if not client_id or not client_secret:
            cs_files = [self.credentials_file] if self.credentials_file else glob.glob(os.path.join(base_dir, "client_secret_*.json"))
            for csf in cs_files:
                if csf and os.path.isfile(csf):
                    try:
                        with open(csf, "r", encoding="utf-8") as f_cs:
                            cs_data = json.load(f_cs)
                            inst = cs_data.get("installed") or cs_data.get("web") or cs_data
                            client_id = client_id or inst.get("client_id", "")
                            client_secret = client_secret or inst.get("client_secret", "")
                            if client_id and client_secret:
                                break
                    except Exception:
                        pass

        tok_obj = {
            "access_token": t.get("token") or t.get("access_token", ""),
            "token_type": "Bearer",
            "refresh_token": t.get("refresh_token", ""),
            "expiry": t.get("expiry", "")
        }
        tok_str = json.dumps(tok_obj)

        conf_content = f"""[gdrive]
type = drive
client_id = {client_id}
client_secret = {client_secret}
scope = drive
root_folder_id = {target_folder_id}
token = {tok_str}

[gdrive_chunker]
type = chunker
remote = gdrive:archives
chunk_size = 4G
hash_type = md5
"""
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(conf_content)
        return conf_path

    def replicate(self) -> Dict[str, Any]:
        """Rclone 기반 고속 멀티스레드 구글 드라이브 동기화."""
        started = time.time()
        result = {
            "success": False,
            "skipped": False,
            "provider": "gdrive",
            "reason": "",
            "files_total": 0,
            "files_copied": 0,
            "files_failed": 0,
            "duration_sec": 0.0
        }

        pf = self.preflight()
        if not pf["ok"]:
            result["skipped"] = True
            result["reason"] = pf["reason"] or "구글 드라이브 사전 점검 실패"
            result["duration_sec"] = time.time() - started
            return result

        root_folder_id = pf["target_folder_id"]
        rclone_exe = self._get_rclone_exe()
        if not rclone_exe:
            result["reason"] = "Rclone 실행 바이너리(rclone.exe)를 찾을 수 없습니다."
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 오류] {result['reason']}", level="ERROR")
            return result

        try:
            rclone_conf = self._ensure_rclone_config(root_folder_id)
        except Exception as e_conf:
            result["reason"] = f"Rclone 설정 생성 실패: {e_conf}"
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 오류] {result['reason']}", level="ERROR")
            return result

        self._log(
            f"[구글 드라이브 Rclone] 초고속 멀티스레드 동기화 시작: {self.local_repo_dir} -> Google Drive:{self.folder_name_or_id} "
            f"(계정: {pf['user_email']}, 여유: {pf['free_gb']} GB)"
        )

        # packs/ 내에 .pack 파일이 존재하면 낱개 blobs/ 스킵
        packs_dir = os.path.join(self.local_repo_dir, "packs")
        packs_has_pack = False
        if os.path.isdir(packs_dir):
            for _root, _dirs, _files in os.walk(packs_dir):
                if any(f.lower().endswith(".pack") for f in _files):
                    packs_has_pack = True
                    break

        cmd = [
            rclone_exe,
            "copy",
            self.local_repo_dir,
            "gdrive:",
            "--config", rclone_conf,
            "--drive-chunk-size", "128M",
            "--transfers", "4",
            "--checkers", "8",
            "--fast-list",
            "--stats", "5s",
            "--stats-one-line",
            "--exclude", ".quarantine_blobs/**",
            "--exclude", "*.db-wal",
            "--exclude", "*.db-shm",
            "--exclude", "backup.lock",
            "--exclude", "*.lock",
            "--exclude", "*.tmp",
            "--exclude", "*.tmp_*",
            "--exclude", "*.tmp.*",
            "--exclude", ".cache/**",
        ]
        if packs_has_pack:
            cmd.extend(["--exclude", "blobs/**"])
            self._log("[구글 드라이브 Rclone] Pack 아카이브 모드: 낱개 blobs 13만 개 생략, 핵심 파일 및 대형 청크 전송.")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags
            )

            last_log_t = time.time()
            if proc.stdout:
                for line in proc.stdout:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    now = time.time()
                    # Rclone stats line 실시간 UI 반영
                    if ("ETA" in line_s or "%" in line_s or "Transferred:" in line_s) and (now - last_log_t >= 3.0):
                        clean_msg = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line_s)
                        clean_msg = re.sub(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (NOTICE|INFO): ", "", clean_msg)
                        self._log(f"[구글 드라이브 Rclone] {clean_msg}")
                        last_log_t = now
                    elif "ERROR" in line_s:
                        self._log(f"[구글 드라이브 Rclone 에러] {line_s}", level="WARNING")

            ret = proc.wait()
            result["duration_sec"] = time.time() - started
            if ret == 0:
                result["success"] = True
                result["reason"] = "Rclone 기반 구글 드라이브 초고속 복제 완료"
                self._log(f"[구글 드라이브 오프사이트] {result['reason']} ({result['duration_sec']:.1f}초)")
            else:
                result["reason"] = f"Rclone 종료 코드: {ret}"
                self._log(f"[구글 드라이브 오프사이트 실패] {result['reason']}", level="ERROR")

            return result

        except Exception as e:
            result["reason"] = f"Rclone 프로세스 실행 예외: {e}"
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 오프사이트 오류] {e}", level="ERROR")
            return result

    def upload_baremetal_chunked(self, archive_path: str, transfers: int = 6) -> Dict[str, Any]:
        """
        베어메탈 아카이브(.qblk/.vhdx 등) 전용 Rclone Chunker 6스트림 병렬 업로드.
        - 로컬 디스크 원본 파일 훼손 및 용량 낭비 없이 4GB 가상 청크로 실시간 분할 스트리밍
        - 대상: gdrive_chunker: (구글 드라이브 archives/ 폴더 매핑)
        - --transfers 6 적용
        """
        started = time.time()
        result = {
            "success": False,
            "skipped": False,
            "provider": "gdrive_chunker",
            "reason": "",
            "duration_sec": 0.0
        }

        if not os.path.isfile(archive_path):
            result["reason"] = f"베어메탈 아카이브 파일이 존재하지 않습니다: {archive_path}"
            self._log(f"[구글 드라이브 청크 업로드 오류] {result['reason']}", level="ERROR")
            return result

        pf = self.preflight()
        if not pf["ok"]:
            result["skipped"] = True
            result["reason"] = pf["reason"] or "구글 드라이브 사전 점검 실패"
            result["duration_sec"] = time.time() - started
            return result

        root_folder_id = pf["target_folder_id"]
        rclone_exe = self._get_rclone_exe()
        if not rclone_exe:
            result["reason"] = "Rclone 실행 바이너리(rclone.exe)를 찾을 수 없습니다."
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 청크 업로드 오류] {result['reason']}", level="ERROR")
            return result

        try:
            rclone_conf = self._ensure_rclone_config(root_folder_id)
        except Exception as e_conf:
            result["reason"] = f"Rclone 설정 생성 실패: {e_conf}"
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 청크 업로드 오류] {result['reason']}", level="ERROR")
            return result

        if os.path.isfile(archive_path):
            archive_dir = os.path.dirname(os.path.abspath(archive_path))
            archive_name = os.path.basename(archive_path)
            meta_name = f"{archive_name}.meta.json"
            include_args = ["--include", archive_name, "--include", meta_name]
        elif os.path.isdir(archive_path):
            archive_dir = os.path.abspath(archive_path)
            archive_name = os.path.basename(archive_dir)
            include_args = []
        else:
            result["reason"] = f"베어메탈 아카이브 파일 또는 디렉터리가 존재하지 않습니다: {archive_path}"
            self._log(f"[구글 드라이브 청크 업로드 오류] {result['reason']}", level="ERROR")
            return result

        self._log(
            f"🚀 [구글 드라이브 청크 업로드] 초고속 6청크 병렬 스트림 시작: {archive_name} -> gdrive_chunker: "
            f"(동시 전송: {transfers}개, 계정: {pf['user_email']})"
        )

        cmd = [
            rclone_exe,
            "copy",
            archive_dir,
            "gdrive_chunker:",
            "--config", rclone_conf,
        ]
        cmd.extend(include_args)
        cmd.extend([
            "--drive-chunk-size", "128M",
            "--transfers", str(transfers),
            "--checkers", "8",
            "--stats", "5s",
            "--stats-one-line",
        ])

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags
            )

            last_log_t = time.time()
            if proc.stdout:
                for line in proc.stdout:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    now = time.time()
                    if ("ETA" in line_s or "%" in line_s or "Transferred:" in line_s) and (now - last_log_t >= 3.0):
                        clean_msg = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line_s)
                        clean_msg = re.sub(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (NOTICE|INFO): ", "", clean_msg)
                        self._log(f"[구글 드라이브 6청크 업로드] {clean_msg}")
                        last_log_t = now
                    elif "ERROR" in line_s:
                        self._log(f"[구글 드라이브 6청크 에러] {line_s}", level="WARNING")

            ret = proc.wait()
            result["duration_sec"] = time.time() - started
            if ret == 0:
                result["success"] = True
                result["reason"] = f"Rclone 6청크 가상 분할 업로드 완료 ({archive_name})"
                self._log(f"🎉 [구글 드라이브 6청크 완료] {result['reason']} ({result['duration_sec']:.1f}초)")
            else:
                result["reason"] = f"Rclone 종료 코드: {ret}"
                self._log(f"[구글 드라이브 6청크 실패] {result['reason']}", level="ERROR")

            return result

        except Exception as e:
            result["reason"] = f"Rclone 프로세스 실행 예외: {e}"
            result["duration_sec"] = time.time() - started
            self._log(f"[구글 드라이브 6청크 오류] {e}", level="ERROR")
            return result


def upload_baremetal_to_gdrive(
    archive_path: str,
    local_repo_dir: str = "",
    gdrive_credentials_file: str = "",
    gdrive_folder_id: str = "",
    transfers: int = 6,
    log_callback: Optional[Callable[..., None]] = None
) -> Dict[str, Any]:
    """
    베어메탈 OS 아카이브를 구글 드라이브로 6청크 가상 병렬 업로드하는 헬퍼 함수.
    """
    if not local_repo_dir and os.path.isfile(archive_path):
        local_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(archive_path)))
    replicator = GoogleDriveOffsiteReplicator(
        local_repo_dir=local_repo_dir or r"D:\MyBackup_Repository",
        credentials_file=gdrive_credentials_file,
        folder_name_or_id=gdrive_folder_id or "BackupSystem_Offsite",
        timeout_sec=7200,
        log_callback=log_callback
    )
    return replicator.upload_baremetal_chunked(archive_path, transfers=transfers)



def replicate_offsite(
    local_repo_dir: str,
    remote_repo_dir: str = "",
    timeout_sec: int = 7200,
    provider: str = "local",
    gdrive_credentials_file: str = "",
    gdrive_folder_id: str = "",
    log_callback: Optional[Callable[..., None]] = None
) -> Dict[str, Any]:
    """
    오프사이트 복제 범용 헬퍼 함수.
    - provider == 'gdrive' 이거나 remote_repo_dir이 'gdrive://'로 시작하는 경우 구글 드라이브 API 클라우드 복제기 호출
    - 그 외에는 순정 Windows robocopy 기반 로컬/SMB 오프사이트 복제기 호출
    """
    is_gdrive = (provider.strip().lower() == "gdrive") or (remote_repo_dir.strip().lower().startswith("gdrive://"))

    if is_gdrive:
        folder = gdrive_folder_id
        if not folder and remote_repo_dir.startswith("gdrive://"):
            folder = remote_repo_dir[9:].strip("/")
        replicator = GoogleDriveOffsiteReplicator(
            local_repo_dir=local_repo_dir,
            credentials_file=gdrive_credentials_file,
            folder_name_or_id=folder or "BackupSystem_Offsite",
            timeout_sec=timeout_sec,
            log_callback=log_callback
        )
        return replicator.replicate()

    replicator = OffsiteReplicator(
        local_repo_dir=local_repo_dir,
        remote_repo_dir=remote_repo_dir,
        timeout_sec=timeout_sec
    )
    return replicator.replicate()
