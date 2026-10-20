# -*- coding: utf-8 -*-
"""
core/storage_health.py

스토리지 물리 디스크 SMART 헬스 및 볼륨 상태 진단 모듈
- Windows Get-PhysicalDisk 파싱 및 60초 TTL 메모리 캐싱 (무지연 응답)
- 드라이브별 용량, 여유율 및 위험도 진단
"""

import sys
import time
import json
import subprocess
import threading
from typing import Dict, List, Any

# 60초 TTL 캐시
_health_cache: Dict[str, Any] = {}
_last_check_ts: float = 0.0
_cache_lock = threading.Lock()


def get_physical_disks_info() -> List[Dict[str, Any]]:
    """Windows Get-PhysicalDisk 를 실행하여 물리 디스크 SMART 상태 수집"""
    if sys.platform != "win32":
        return []

    ps_cmd = (
        "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, "
        "@{N='SizeGB';E={[math]::Round($_.Size/1GB, 1)}}, HealthStatus, OperationalStatus | "
        "ConvertTo-Json -Compress"
    )

    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=flags
        )
        if res.returncode != 0 or not res.stdout.strip():
            return []

        raw = json.loads(res.stdout.strip())
        if isinstance(raw, dict):
            raw = [raw]

        disks = []
        for d in raw:
            disks.append({
                "device_id": str(d.get("DeviceId", "")),
                "name": d.get("FriendlyName", "Unknown Disk"),
                "media_type": d.get("MediaType", "SSD"),
                "bus_type": d.get("BusType", "NVMe"),
                "size_gb": float(d.get("SizeGB", 0.0)),
                "health_status": str(d.get("HealthStatus", "Healthy")),
                "operational_status": str(d.get("OperationalStatus", "OK"))
            })
        return disks
    except Exception:
        return []


def get_volumes_info() -> List[Dict[str, Any]]:
    """모든 마운트된 로컬 볼륨의 용량 및 사용률 진단"""
    import psutil
    vols = []
    try:
        partitions = psutil.disk_partitions(all=False)
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                total_gb = round(usage.total / (1024 ** 3), 1)
                free_gb = round(usage.free / (1024 ** 3), 1)
                used_gb = round(usage.used / (1024 ** 3), 1)
                free_pct = round((usage.free / usage.total) * 100, 1) if usage.total > 0 else 0.0

                # 헬스 판정: 5% 미만이면 위험, 10% 미만이면 경고
                status = "healthy"
                if free_pct < 5.0:
                    status = "critical"
                elif free_pct < 10.0:
                    status = "warning"

                vols.append({
                    "drive": p.device.rstrip("\\"),
                    "mountpoint": p.mountpoint,
                    "fstype": p.fstype,
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "free_gb": free_gb,
                    "free_percent": free_pct,
                    "status": status
                })
            except (PermissionError, OSError):
                continue
    except Exception:
        pass
    return vols


def get_system_storage_health(force_refresh: bool = False) -> Dict[str, Any]:
    """
    물리 디스크 SMART 및 볼륨 상태를 취합한 종합 헬스 리포트 (60초 캐싱)
    """
    global _health_cache, _last_check_ts

    now = time.time()
    with _cache_lock:
        if not force_refresh and _health_cache and (now - _last_check_ts < 60.0):
            return dict(_health_cache)

        disks = get_physical_disks_info()
        volumes = get_volumes_info()

        overall = "healthy"
        # 수집 결과가 아예 없는 경우 False Positive 방지
        if not disks and not volumes:
            overall = "unknown"
        elif not disks:
            overall = "warning"


        # 물리 디스크 중 하나라도 Healthy가 아니면 경고
        for d in disks:
            h = d.get("health_status", "").lower()
            if h in ("unhealthy", "failed"):
                overall = "critical"
                break
            elif h in ("warning",):
                overall = "warning"

        # 볼륨 중 critical이 있으면 critical
        if overall not in ("critical", "unknown"):
            for v in volumes:
                if v.get("status") == "critical":
                    overall = "critical"
                    break
                elif v.get("status") == "warning" and overall == "healthy":
                    overall = "warning"


        result = {
            "success": True,
            "overall_status": overall,
            "timestamp": now,
            "physical_disks": disks,
            "volumes": volumes,
            "summary": {
                "total_disks": len(disks),
                "total_volumes": len(volumes),
                "has_warning": overall != "healthy"
            }
        }

        _health_cache = result
        _last_check_ts = now
        return result
