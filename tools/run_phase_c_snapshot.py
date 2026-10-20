import os
import sys

# Ensure UTF-8 stdout/stderr on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
import hashlib
from typing import Dict, Any

from core.snapshot import SnapshotEngine
from core.config import ConfigManager

def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def main():
    print("=" * 60)
    print("🚀 [Phase C] 신규 무결성 백업 스냅샷 생성 및 정밀 검증 시작")
    print("=" * 60)

    # 1. Load Profile
    profile_path = os.path.abspath("data/profiles.json")
    if not os.path.exists(profile_path):
        print(f"❌ Error: {profile_path} 를 찾을 수 없습니다.")
        sys.exit(1)

    with open(profile_path, "r", encoding="utf-8") as f:
        profiles = json.load(f)

    target_profile = None
    for p in profiles:
        if p.get("id") == "prof_custom_selected":
            target_profile = p
            break

    if not target_profile:
        print("❌ Error: prof_custom_selected 프로필을 찾을 수 없습니다.")
        sys.exit(1)

    snap_id = None
    elapsed = 0.0
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-only":
        snap_id = sys.argv[2]
        print(f"📌 [기존 스냅샷 정밀 검증 모드]: {snap_id}")
    else:
        print(f"📌 대상 프로필: {target_profile['name']} ({target_profile['id']})")
        print(f"📌 저장소 경로: {target_profile['repo_dir']}")
        print(f"📌 소스 개수: {len(target_profile['sources'])} 개")

        last_log_time = 0
        def on_progress(p_data: Dict[str, Any]):
            nonlocal last_log_time
            now = time.time()
            if now - last_log_time >= 3.0 or p_data.get("percent") in (0, 100):
                last_log_time = now
                processed = p_data.get("processed_files", 0)
                total = p_data.get("total_files", 0)
                pct = p_data.get("percent", 0)
                msg = p_data.get("current_file", "")
                if len(msg) > 60:
                    msg = "..." + msg[-57:]
                print(f"   [{pct}%] {processed}/{total} 파일 | {msg}")

        print("\n📦 스냅샷 생성 작업 착수 (VSS 자동 연동)...")
        start_time = time.time()

        manifest = SnapshotEngine.create_snapshot(
            repo_dir=target_profile["repo_dir"],
            sources=target_profile["sources"],
            profile_id=target_profile["id"],
            profile_name=target_profile["name"],
            exclude_patterns=target_profile.get("exclude_patterns", []),
            compress_level=target_profile.get("compression_level", 6),
            progress_callback=on_progress,
            min_free_disk_gb=10.0
        )

        elapsed = time.time() - start_time
        snap_id = manifest["id"]
        print(f"\n✅ 스냅샷 생성 완료! ID: {snap_id} (소요시간: {elapsed:.2f}초)")

        # Update profile metadata
        target_profile["last_run"] = time.time()
        target_profile["last_status"] = "success"
        target_profile["last_snapshot_id"] = snap_id
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)

    # 2. Inspect Created Snapshot
    snap_json_path = os.path.join(target_profile["repo_dir"], "snapshots", f"{snap_id}.json")
    if not os.path.exists(snap_json_path):
        print(f"❌ Error: 스냅샷 파일 {snap_json_path} 이 존재하지 않습니다.")
        sys.exit(1)

    snap_sha256 = compute_sha256(snap_json_path)
    snap_size_mb = os.path.getsize(snap_json_path) / (1024 * 1024)
    print(f"📄 스냅샷 JSON 크기: {snap_size_mb:.2f} MB | SHA-256: {snap_sha256}")

    with open(snap_json_path, "r", encoding="utf-8") as f:
        snap_data = json.load(f)

    entries = snap_data.get("entries", [])
    total_files = len(entries)
    total_bytes = sum(e.get("size", 0) for e in entries)
    total_gb = total_bytes / (1024 ** 3)

    print("\n🔍 [정밀 포렌식 감사 결과]")
    print(f"   - 총 등록 파일 수: {total_files:,} 개")
    print(f"   - 총 백업 원본 용량: {total_gb:.2f} GB")

    hermes_entries = [e for e in entries if "hermes" in e.get("source_root", "").lower()]
    hermes_venv = [e for e in hermes_entries if "venv" in e.get("rel_path", "").lower()]
    hermes_node = [e for e in hermes_entries if "node" in e.get("rel_path", "").lower()]
    uv_cpython = [e for e in entries if "cpython-3.11" in e.get("source_root", "").lower() or "cpython-3.11" in e.get("rel_path", "").lower()]
    as_index = [e for e in entries if "androidstudio" in e.get("source_root", "").lower() and "index" in e.get("rel_path", "").lower() and not e.get("rel_path", "").endswith(".xml")]

    print(f"   - Hermes 전체 파일: {len(hermes_entries):,} 개")
    print(f"     * Hermes venv: {len(hermes_venv):,} 개 (목표: >20,000)")
    print(f"     * Hermes Node/npm: {len(hermes_node):,} 개 (목표: >2,000)")
    print(f"   - UV Base CPython 3.11: {len(uv_cpython):,} 개 (목표: >4,000)")
    print(f"   - Android Studio 인덱스 캐시: {len(as_index):,} 개 (목표: 0)")

    # Gate Evaluation
    gate_checks = {
        "hermes_venv_present": len(hermes_venv) >= 20000,
        "hermes_node_present": len(hermes_node) >= 2000,
        "uv_cpython_present": len(uv_cpython) >= 4000,
        "as_index_cache_eliminated": len(as_index) == 0,
        "total_files_sufficient": total_files >= 150000
    }

    all_passed = all(gate_checks.values())
    print("\n🚦 [품질 게이트 판정]")
    for k, passed in gate_checks.items():
        mark = "✅ PASS" if passed else "❌ FAIL"
        print(f"   - {k}: {mark}")

    audit_result = {
        "snapshot_id": snap_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "manifest_path": snap_json_path,
        "manifest_sha256": snap_sha256,
        "elapsed_seconds": elapsed,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_gb": round(total_gb, 3),
        "breakdown": {
            "hermes_total": len(hermes_entries),
            "hermes_venv": len(hermes_venv),
            "hermes_node": len(hermes_node),
            "uv_cpython": len(uv_cpython),
            "as_index_cache": len(as_index)
        },
        "gate_checks": gate_checks,
        "all_passed": all_passed
    }

    os.makedirs("data/dr_result", exist_ok=True)
    report_file = os.path.abspath("data/dr_result/phase_c_snapshot_manifest.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(audit_result, f, indent=2, ensure_ascii=False)

    print(f"\n💾 감사 리포트 저장 완료: {report_file}")
    if all_passed:
        print("🎉 [Phase C 성공] 모든 핵심 런타임이 무결하게 봉인되었습니다!")
        sys.exit(0)
    else:
        print("⚠️ [Phase C 주의] 일부 검증 기준에 미달했습니다.")
        sys.exit(2)

if __name__ == "__main__":
    main()
