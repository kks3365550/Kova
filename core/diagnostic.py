# -*- coding: utf-8 -*-
"""
core/diagnostic.py

원클릭 시스템 종합 진단 로그 패키징 (ZIP) 모듈
고객지원, 원격 기술지원, 장애 복구 및 감사 대응을 위해 시스템 메타데이터,
하드웨어 SMART/볼륨 상태, 스케줄러 정책, 백업 스냅샷 이력 및 최근 로그를
민감정보 마스킹(Sanitized) 후 단일 ZIP 패키지로 압축 생성합니다.
"""

import os
import sys
import io
import json
import zipfile
import platform
import subprocess
from datetime import datetime
from typing import Dict, Any, List, Optional

import psutil

from core.config import ConfigManager
from core.snapshot import SnapshotEngine
from core.storage_health import get_system_storage_health
from core.windows_task import get_windows_scheduled_task_status

# 프로젝트 기본 경로
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(BASE_DIR, "logs")

def is_sensitive_key(key: str) -> bool:
    """민감한 설정 키인지 정밀 판정합니다."""
    k_lower = str(key).lower()
    for pat in ["password", "secret", "token", "credential", "private", "cookie", "auth", "session", "hash", "salt"]:
        if pat in k_lower:
            return True
    tokens = set(k_lower.replace("-", "_").split("_"))
    if "key" in tokens:
        if any(p in tokens for p in {"api", "secret", "private", "encryption", "enc", "license", "ssh", "access", "signing", "release"}):
            return True
        if k_lower == "key":
            return True
    return False


def sanitize_data(data: Any) -> Any:
    """민감한 정보(비밀번호, API 키, 토큰 등)를 재귀적으로 안전하게 마스킹합니다."""
    if isinstance(data, dict):
        clean = {}
        for k, v in data.items():
            if is_sensitive_key(k):
                clean[k] = "***MASKED***"
            else:
                clean[k] = sanitize_data(v)
        return clean
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    return data


def get_vss_status(timeout: int = 4) -> Dict[str, Any]:
    """Windows VSS Writer 상태를 진단합니다."""
    if sys.platform != "win32":
        return {"available": False, "reason": "Non-Windows OS"}

    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            ["vssadmin", "list", "writers"],
            capture_output=True,
            timeout=timeout,
            creationflags=flags
        )
        out = res.stdout.decode("cp949", errors="replace")
        has_error = any(kw in out for kw in ["[5] Waiting for completion", "Failed", "오류", "Timed out"])
        return {
            "available": res.returncode == 0,
            "exit_code": res.returncode,
            "has_writer_errors": has_error,
            "snippet": out[:800] if out else "No output or permission denied"
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def collect_system_metadata() -> Dict[str, Any]:
    """OS, CPU, 메모리, 파이썬 런타임, 스케줄러 등 기본 시스템 제원을 수집합니다."""
    # 소프트웨어 버전 조회
    ver = "unknown"
    try:
        from core import __version__ as core_ver
        ver = core_ver
    except Exception:
        ver_file = os.path.join(BASE_DIR, "VERSION")
        if os.path.exists(ver_file):
            with open(ver_file, "r", encoding="utf-8") as f:
                ver = f.read().strip()

    mem = psutil.virtual_memory()

    return {
        "report_generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "software_version": ver,
        "python": {
            "version": sys.version,
            "executable": sys.executable,
        },
        "operating_system": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
        },
        "hardware": {
            "cpu_cores_logical": os.cpu_count(),
            "memory_total_gb": round(mem.total / (1024 ** 3), 2),
            "memory_available_gb": round(mem.available / (1024 ** 3), 2),
            "memory_used_percent": mem.percent,
        },
        "windows_task_scheduler": get_windows_scheduled_task_status(),
        "vss_status": get_vss_status(),
    }


def collect_snapshots_summary(target_repo: Optional[str] = None) -> Dict[str, Any]:
    """백업 저장소 목록 및 전체 스냅샷 통계를 집계합니다."""
    candidate_repos: List[str] = []

    if target_repo and os.path.exists(target_repo):
        candidate_repos.append(target_repo)

    # 프로필에서 저장소 경로 수집
    profiles = ConfigManager.get_profiles()
    for p in profiles:
        r = p.get("repo_dir")
        if r and os.path.exists(r) and r not in candidate_repos:
            candidate_repos.append(r)

    # 기본 저장소 경로 후보군
    default_candidates = [
        "D:\\MyBackup_Repository",
        os.path.join(os.path.expanduser("~"), "MyBackup_Repository"),
        os.path.join(BASE_DIR, "backup_repository"),
    ]
    for dc in default_candidates:
        if os.path.exists(dc) and dc not in candidate_repos:
            candidate_repos.append(dc)

    all_snapshots: List[Dict[str, Any]] = []
    repo_stats: List[Dict[str, Any]] = []

    for repo in candidate_repos:
        try:
            snaps = SnapshotEngine.list_snapshots(repo)
            repo_stats.append({
                "repo_path": repo,
                "snapshot_count": len(snaps),
                "accessible": True,
            })
            for s in snaps:
                summary = s.get("summary") or {}
                all_snapshots.append({
                    "id": s.get("id"),
                    "created_at": s.get("created_at"),
                    "profile_name": s.get("profile_name", "Unknown"),
                    "total_files": summary.get("total_files", 0),
                    "total_bytes": summary.get("total_bytes", 0),
                    "dedup_saved_bytes": summary.get("dedup_saved_bytes", 0),
                    "repo_path": repo,
                })
        except Exception as e:
            repo_stats.append({
                "repo_path": repo,
                "snapshot_count": 0,
                "accessible": False,
                "error": str(e),
            })

    # 최신 및 최고(Oldest) 스냅샷 확인
    all_snapshots.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    latest = all_snapshots[0] if all_snapshots else None
    oldest = all_snapshots[-1] if all_snapshots else None

    return {
        "total_snapshots": len(all_snapshots),
        "latest_snapshot": latest,
        "oldest_snapshot": oldest,
        "repositories": repo_stats,
        "snapshots": all_snapshots[:50],  # 최근 50개 메타데이터
    }


def collect_recent_logs(max_tail_bytes: int = 3 * 1024 * 1024) -> Dict[str, str]:
    """logs 폴더 내의 로그 파일과 인메모리 최근 작업 로그를 수집합니다."""
    collected: Dict[str, str] = {}

    # 1. logs/ 디렉토리 파일 탐색
    if os.path.exists(LOGS_DIR):
        for fname in os.listdir(LOGS_DIR):
            if fname.endswith(".log"):
                fpath = os.path.join(LOGS_DIR, fname)
                try:
                    fsize = os.path.getsize(fpath)
                    with open(fpath, "rb") as fp:
                        if fsize > max_tail_bytes:
                            fp.seek(fsize - max_tail_bytes)
                            raw = fp.read()
                            text = "[... 이전 로그 생략 (최근 3MB 수록) ...]\n" + raw.decode("utf-8", errors="replace")
                        else:
                            text = fp.read().decode("utf-8", errors="replace")
                    collected[f"logs/{fname}"] = text
                except Exception as e:
                    collected[f"logs/{fname}"] = f"[로그 읽기 실패: {e}]"

    # 2. 인메모리 최근 작업 로그
    try:
        from web.state import get_task_logs
        in_memory_logs = get_task_logs(limit=200)
        formatted_logs = []
        for entry in in_memory_logs:
            if isinstance(entry, dict):
                t = entry.get("time", "")
                lvl = entry.get("level", "INFO")
                msg = entry.get("message", "")
                formatted_logs.append(f"[{t}] [{lvl}] {msg}")
            else:
                formatted_logs.append(str(entry))
        collected["logs/in_memory_recent_tasks.log"] = "\n".join(formatted_logs)
    except Exception as e:
        collected["logs/in_memory_recent_tasks.log"] = f"[인메모리 로그 수집 알림: {e}]"

    return collected


def generate_diagnostic_summary_text(
    sys_meta: Dict[str, Any],
    storage_health: Dict[str, Any],
    snapshots_summary: Dict[str, Any],
    profiles: List[Dict[str, Any]]
) -> str:
    """기술지원 담당자 및 사용자가 바로 읽을 수 있는 텍스트 진단 보고서를 생성합니다."""
    lines = [
        "=" * 80,
        "KOVA VaultOS 시스템 종합 진단 보고서 (Diagnostic Summary Report)",
        f"생성 일시: {sys_meta.get('report_generated_at')}",
        f"소프트웨어 버전: v{sys_meta.get('software_version')}",
        "=" * 80,
        "",
        "[1. 시스템 및 하드웨어 제원]",
        f"- 운영체제: {sys_meta.get('operating_system', {}).get('platform', 'Unknown')}",
        f"- 컴퓨터 이름: {sys_meta.get('operating_system', {}).get('hostname', 'Unknown')}",
        f"- 아키텍처: {sys_meta.get('operating_system', {}).get('architecture', 'Unknown')}",
        f"- CPU 논리 코어: {sys_meta.get('hardware', {}).get('cpu_cores_logical', 0)} Cores",
        f"- 메모리: {sys_meta.get('hardware', {}).get('memory_total_gb', 0)} GB "
        f"(사용 가능: {sys_meta.get('hardware', {}).get('memory_available_gb', 0)} GB / "
        f"사용률: {sys_meta.get('hardware', {}).get('memory_used_percent', 0)}%)",
        f"- Python 런타임: {sys.version.split()[0]} ({sys.executable})",
        "",
        "[2. 스토리지 볼륨 및 물리 디스크 상태]",
        f"- 종합 스토리지 헬스: {storage_health.get('status', 'unknown').upper()}",
    ]

    # 물리 디스크
    disks = storage_health.get("disks", [])
    if disks:
        lines.append("- 물리 디스크 (SMART):")
        for d in disks:
            lines.append(
                f"  * [Disk {d.get('device_id')}] {d.get('name')} ({d.get('size_gb')} GB, "
                f"{d.get('media_type')}/{d.get('bus_type')}) - Health: {d.get('health_status')}, Op: {d.get('operational_status')}"
            )
    else:
        lines.append("- 물리 디스크 정보: 조회 불가 또는 권한 필요")

    # 볼륨
    vols = storage_health.get("volumes", [])
    if vols:
        lines.append("- 마운트 볼륨 현황:")
        for v in vols:
            lines.append(
                f"  * {v.get('drive')} ({v.get('fstype', 'NTFS')}): 총 {v.get('total_gb')} GB / "
                f"여유 {v.get('free_gb')} GB ({v.get('free_percent')}%) [{v.get('status').upper()}]"
            )

    lines.extend([
        "",
        "[3. 백업 저장소 및 스냅샷 이력]",
        f"- 전체 백업 시점 수: {snapshots_summary.get('total_snapshots', 0)}개",
    ])

    for r in snapshots_summary.get("repositories", []):
        lines.append(f"- 저장소: {r.get('repo_path')} (스냅샷: {r.get('snapshot_count')}개, 정상 접근: {r.get('accessible')})")

    latest = snapshots_summary.get("latest_snapshot")
    if latest:
        created_str = datetime.fromtimestamp(latest["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if isinstance(latest.get("created_at"), (int, float)) else str(latest.get("created_at"))
        size_mb = round(latest.get("total_bytes", 0) / (1024 ** 2), 2)
        lines.extend([
            f"- 최신 백업 시점 ID: {latest.get('id')}",
            f"- 최신 백업 생성 시각: {created_str}",
            f"- 최신 백업 파일 수: {latest.get('total_files', 0)}개 ({size_mb} MB)",
            f"- 프로필 이름: {latest.get('profile_name', 'Default')}",
        ])
    else:
        lines.append("- 스냅샷 이력: 아직 백업 기록이 없습니다.")

    # 스케줄러 정책
    task_stat = sys_meta.get("windows_task_scheduler", {})
    lines.extend([
        "",
        "[4. 자동 스케줄러 및 놓친 백업 정책]",
        f"- 작업 스케줄러 등록: {'등록됨 (ACTIVE)' if task_stat.get('registered') else '미등록'}",
        f"- 다음 실행 예정: {task_stat.get('next_run', '-')}",
        f"- 마지막 실행 결과: {task_stat.get('last_result', '-')}",
        f"- 부팅 시 놓친 백업 즉시 실행(StartWhenAvailable): {'활성화됨 (True)' if task_stat.get('missed_backup_recovery') else '미적용'}",
    ])

    # 프로필 요약
    lines.extend([
        "",
        f"[5. 구성 프로필 ({len(profiles)}개)]",
    ])
    for p in profiles:
        sources_cnt = len(p.get("sources", []))
        auto_str = "자동 실행 ON" if p.get("auto_backup_enabled") else "수동 실행 전용"
        lines.append(f"- 프로필: {p.get('name')} (ID: {p.get('id')}, 대상 {sources_cnt}개, {auto_str})")

    lines.extend([
        "",
        "[6. 첨부된 진단 파일 목록]",
        "- DIAGNOSTIC_SUMMARY.txt : 본 텍스트 요약 보고서",
        "- system_info.json        : 세부 시스템, 하드웨어 및 VSS 제원",
        "- storage_health.json     : 물리 디스크 SMART 및 볼륨 세부 진단",
        "- snapshots_summary.json  : 저장소 및 최근 스냅샷 목록",
        "- config_sanitized.json   : 민감 정보 마스킹된 설정 파일",
        "- logs/*                  : 최근 서버 및 백업 프로세스 로그",
        "=" * 80,
    ])

    return "\n".join(lines)


def build_diagnostic_package(repo_dir: Optional[str] = None) -> bytes:
    """
    모든 진단 데이터를 취합하고 메모리 상에서 ZIP 바이너리를 생성하여 반환합니다.
    """
    sys_meta = collect_system_metadata()
    storage_health = get_system_storage_health(force_refresh=True)
    snapshots_summary = collect_snapshots_summary(target_repo=repo_dir)
    logs_dict = collect_recent_logs()

    # 설정 파일 수집 (민감정보 마스킹)
    raw_profiles = ConfigManager.get_profiles()
    sanitized_profiles = sanitize_data(raw_profiles)

    app_settings_path = os.path.join(BASE_DIR, "data", "app_settings.json")
    sanitized_app_settings = {}
    if os.path.exists(app_settings_path):
        try:
            with open(app_settings_path, "r", encoding="utf-8") as f:
                sanitized_app_settings = sanitize_data(json.load(f))
        except Exception as e:
            sanitized_app_settings = {"error": f"Failed to load app_settings.json: {e}"}

    config_bundle = {
        "profiles": sanitized_profiles,
        "app_settings": sanitized_app_settings,
    }

    # 텍스트 요약 보고서 생성
    summary_text = generate_diagnostic_summary_text(
        sys_meta=sys_meta,
        storage_health=storage_health,
        snapshots_summary=snapshots_summary,
        profiles=sanitized_profiles
    )

    # In-memory ZIP 파일 생성
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. 텍스트 요약
        zf.writestr("DIAGNOSTIC_SUMMARY.txt", summary_text.encode("utf-8"))

        # 2. JSON 상세 메타데이터
        zf.writestr("system_info.json", json.dumps(sys_meta, indent=2, ensure_ascii=False).encode("utf-8"))
        zf.writestr("storage_health.json", json.dumps(storage_health, indent=2, ensure_ascii=False).encode("utf-8"))
        zf.writestr("snapshots_summary.json", json.dumps(snapshots_summary, indent=2, ensure_ascii=False).encode("utf-8"))
        zf.writestr("config_sanitized.json", json.dumps(config_bundle, indent=2, ensure_ascii=False).encode("utf-8"))

        # 3. 로그 파일들
        for log_rel_path, log_content in logs_dict.items():
            zf.writestr(log_rel_path, log_content.encode("utf-8"))

    zip_bytes = zip_buffer.getvalue()
    zip_buffer.close()
    return zip_bytes


def get_diagnostic_quick_overview(repo_dir: Optional[str] = None) -> Dict[str, Any]:
    """ZIP 생성 전 UI에서 요약 정보를 빠르게 확인할 수 있는 경량 딕셔너리를 반환합니다."""
    sys_meta = collect_system_metadata()
    storage_health = get_system_storage_health(force_refresh=False)
    snapshots_summary = collect_snapshots_summary(target_repo=repo_dir)
    sanitized_profiles = sanitize_data(ConfigManager.get_profiles())

    summary_text = generate_diagnostic_summary_text(
        sys_meta=sys_meta,
        storage_health=storage_health,
        snapshots_summary=snapshots_summary,
        profiles=sanitized_profiles
    )

    return {
        "success": True,
        "generated_at": sys_meta.get("report_generated_at"),
        "software_version": sys_meta.get("software_version"),
        "hostname": sys_meta.get("operating_system", {}).get("hostname"),
        "os": sys_meta.get("operating_system", {}).get("platform"),
        "storage_status": storage_health.get("status", "healthy"),
        "total_snapshots": snapshots_summary.get("total_snapshots", 0),
        "scheduler_registered": sys_meta.get("windows_task_scheduler", {}).get("registered", False),
        "summary_text": summary_text,
    }
