import os
import json
import uuid
import threading
from typing import Dict, List, Any, Optional

_CONFIG_LOCK = threading.RLock()  # 프로필/설정 read-modify-write 직렬화
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "app_settings.json")

DEFAULT_PROFILE = {
    "id": "prof_default",
    "name": "서버/시스템 메인 백업 프로필",
    "sources": [
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ],
    "repo_dir": r"D:\MyBackup_Repository",
    "exclude_patterns": [
        "pagefile.sys", "hiberfil.sys", "swapfile.sys",
        "$RECYCLE.BIN", "$Recycle.Bin", "System Volume Information",
        "*.tmp", "*.temp", "*.lock", "lockfile",
        "*.log", ".git", "__pycache__", "*.pyc",
        "node_modules", ".cache", ".pytest_cache",
        "backup_repository", "MyBackup_Repository", "WindowsImageBackup"
    ],
    "schedule_type": "interval_hours",
    "schedule_value": "12",
    "auto_backup_enabled": False,
    "retention_count": 30,
    "retention_days": 60,
    "compression_level": 3,
    "min_free_disk_gb": 10,
    "enable_ransomware_protection": True,
    "last_run": None,
    "last_status": "never",
    "last_snapshot_id": None
}

DEFAULT_SETTINGS = {
    "server_port": 8765,
    "server_host": "0.0.0.0",
    "auto_open_browser": True,
    "dark_mode": True,
    "log_level": "INFO",
    "min_free_disk_gb": 10,
    "enable_ransomware_protection": True,
    "offsite_enabled": False,
    "offsite_provider": "local",
    "offsite_remote_path": "",
    "offsite_auto_sync": True,
    "offsite_gdrive_credentials_file": "",
    "offsite_gdrive_folder_id": "",
    "backup_engine": "rust"  # "rust" (ultra-fast native) or "python" (legacy)
}

class ConfigManager:
    @staticmethod
    def _ensure_dir():
        os.makedirs(DATA_DIR, exist_ok=True)

    @classmethod
    def get_settings(cls) -> Dict[str, Any]:
        cls._ensure_dir()
        if not os.path.exists(SETTINGS_FILE):
            cls.save_settings(DEFAULT_SETTINGS)
            return dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                res = dict(DEFAULT_SETTINGS)
                res.update(data)
                return res
        except Exception:
            # 파싱 실패 시: 손상된 파일을 .bak으로 보존하여 데이터 유실 방지
            try:
                import shutil
                shutil.copy2(SETTINGS_FILE, SETTINGS_FILE + ".bak")
            except Exception:
                pass
            return dict(DEFAULT_SETTINGS)

    @classmethod
    def save_settings(cls, settings: Dict[str, Any]):
        cls._ensure_dir()
        # 원자적 쓰기: temp 파일 → os.replace()로 교체 (중간 실패 시 기존 파일 보존)
        tmp_path = f"{SETTINGS_FILE}.tmp_{os.getpid()}_{threading.get_ident()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            if os.path.exists(SETTINGS_FILE):
                os.replace(tmp_path, SETTINGS_FILE)
            else:
                os.rename(tmp_path, SETTINGS_FILE)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

    @classmethod
    def get_offsite_config(cls) -> Dict[str, Any]:
        settings = cls.get_settings()
        return {
            "enabled": bool(settings.get("offsite_enabled", False)),
            "provider": str(settings.get("offsite_provider", "local") or "local").strip().lower(),
            "remote_path": str(settings.get("offsite_remote_path", "") or "").strip(),
            "auto_sync": bool(settings.get("offsite_auto_sync", True)),
            "gdrive_credentials_file": str(settings.get("offsite_gdrive_credentials_file", "") or "").strip(),
            "gdrive_folder_id": str(settings.get("offsite_gdrive_folder_id", "") or "").strip(),
        }

    @classmethod
    def save_offsite_config(cls, offsite_data: Dict[str, Any]) -> Dict[str, Any]:
        settings = cls.get_settings()
        if "enabled" in offsite_data:
            settings["offsite_enabled"] = bool(offsite_data["enabled"])
        if "provider" in offsite_data:
            settings["offsite_provider"] = str(offsite_data["provider"] or "local").strip().lower()
        if "remote_path" in offsite_data:
            settings["offsite_remote_path"] = str(offsite_data["remote_path"] or "").strip()
        if "auto_sync" in offsite_data:
            settings["offsite_auto_sync"] = bool(offsite_data["auto_sync"])
        if "gdrive_credentials_file" in offsite_data:
            settings["offsite_gdrive_credentials_file"] = str(offsite_data["gdrive_credentials_file"] or "").strip()
        if "gdrive_folder_id" in offsite_data:
            settings["offsite_gdrive_folder_id"] = str(offsite_data["gdrive_folder_id"] or "").strip()
        cls.save_settings(settings)
        return cls.get_offsite_config()

    @classmethod
    def get_profiles(cls) -> List[Dict[str, Any]]:
        cls._ensure_dir()
        if not os.path.exists(PROFILES_FILE):
            # 최초 1회만: profiles.json이 아예 없을 때만 기본값 생성
            default_p = dict(DEFAULT_PROFILE)
            cls.save_profiles([default_p])
            return [default_p]
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                profiles = json.load(f)
            # 빈 리스트([])는 사용자가 모든 프로필을 삭제한 정상 상태 → 덮어쓰지 않음
            if not isinstance(profiles, list):
                return []
            return profiles
        except Exception:
            # 파싱 실패 시: 손상된 파일을 .bak으로 보존하고 빈 리스트 반환
            # (기본값으로 덮어쓰지 않음 → 데이터 손실 방지)
            try:
                import time as _time
                bak_path = f"{PROFILES_FILE}.corrupt_{int(_time.time())}.bak"
                import shutil
                shutil.copy2(PROFILES_FILE, bak_path)
            except Exception:
                pass
            return []

    @classmethod
    def save_profiles(cls, profiles: List[Dict[str, Any]]):
        cls._ensure_dir()
        # 원자적 쓰기: temp 파일 → os.replace()로 교체 (중간 실패 시 기존 파일 보존)
        tmp_path = f"{PROFILES_FILE}.tmp_{os.getpid()}_{threading.get_ident()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(profiles, f, indent=2, ensure_ascii=False)
            if os.path.exists(PROFILES_FILE):
                os.replace(tmp_path, PROFILES_FILE)
            else:
                os.rename(tmp_path, PROFILES_FILE)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise


    @classmethod
    def get_profile(cls, profile_id: str) -> Optional[Dict[str, Any]]:
        profiles = cls.get_profiles()
        for p in profiles:
            if p.get("id") == profile_id:
                return p
        return None

    @classmethod
    def _profiles_file_unreadable(cls) -> bool:
        """profiles.json 이 있는데 읽을 수 없으면 True (이때 덮어쓰면 기존 프로필이 모두 사라진다)."""
        if not os.path.exists(PROFILES_FILE):
            return False
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                return not isinstance(json.load(f), list)
        except Exception:
            return True

    @classmethod
    def save_profile(cls, profile: Dict[str, Any]) -> Dict[str, Any]:
        with _CONFIG_LOCK:
            profiles = cls.get_profiles()
            if cls._profiles_file_unreadable():
                raise RuntimeError("profiles.json 을 읽을 수 없어 저장을 중단합니다(손상 파일은 .corrupt_*.bak 으로 보존됨).")
            if not profile.get("id"):
                profile["id"] = f"prof_{uuid.uuid4().hex[:8]}"

            updated = False
            for i, p in enumerate(profiles):
                if p.get("id") == profile["id"]:
                    profiles[i] = profile
                    updated = True
                    break

            if not updated:
                profiles.append(profile)

            cls.save_profiles(profiles)
            return profile

    @classmethod
    def update_profile_fields(cls, profile_id: str, fields: Dict[str, Any]) -> bool:
        """최신 프로필을 다시 읽어 지정한 필드만 갱신한다(실행 중 사용자가 바꾼 다른 설정을 덮어쓰지 않음)."""
        with _CONFIG_LOCK:
            profiles = cls.get_profiles()
            if cls._profiles_file_unreadable():
                raise RuntimeError("profiles.json 을 읽을 수 없어 저장을 중단합니다.")
            for p in profiles:
                if p.get("id") == profile_id:
                    p.update(fields)
                    cls.save_profiles(profiles)
                    return True
            return False

    @classmethod
    def delete_profile(cls, profile_id: str) -> bool:
        with _CONFIG_LOCK:
            profiles = cls.get_profiles()
            if cls._profiles_file_unreadable():
                raise RuntimeError("profiles.json 을 읽을 수 없어 삭제를 중단합니다.")
            initial_len = len(profiles)
            profiles = [p for p in profiles if p.get("id") != profile_id]
            if len(profiles) < initial_len:
                cls.save_profiles(profiles)
                return True
            return False
