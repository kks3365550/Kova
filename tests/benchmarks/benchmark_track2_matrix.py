"""
Track 2-2: 현행 File CAS 대형 파일(10GB) 9개 매트릭스 벤치마크 러너.
(1MB/10MB/100MB x Head/Middle/Tail)

주의:
- 현행 File CAS 코드를 최적화하거나 변경하지 않습니다.
- 1MB-Middle은 Track 2-1 결과(15.621s, 10.00GB read, 2.04MB write, CPU 15.312s, RSS 45.55MB, 신규 blob 1.00MB)를 baseline으로 유지합니다.
- 나머지 8개 조건을 동일한 격리 환경 및 ResourceTracker 계측 방식으로 순차 측정합니다.
"""

import os
import sys
import time
import shutil
import tempfile
import threading
from typing import Dict, Any, Optional, List

# 백업시스템 루트 경로를 sys.path에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    import psutil
except ImportError:
    print("[ERROR] psutil 라이브러리가 필요합니다.")
    sys.exit(1)

from core.snapshot import SnapshotEngine
from tests.benchmarks.benchmark_track2_baseline import (
    ResourceTracker, format_bytes, get_dir_size, create_deterministic_large_file, mutate_file
)

# 1MB-Middle의 Track 2-1 Baseline 실측값
BASELINE_1MB_MIDDLE = {
    "condition": "1MB - Middle (Track 2-1 Baseline)",
    "change_size_mb": 1.0,
    "position": "Middle",
    "wall_time_sec": 15.621,
    "io_read_bytes": 10739804021,
    "io_write_bytes": 2136648,
    "cpu_user_sec": 11.531,
    "cpu_system_sec": 3.781,
    "cpu_total_sec": 15.312,
    "peak_rss_mb": 45.55,
    "new_blob_bytes": 1048576,  # 1.00 MB
}


def calculate_offset(file_size: int, change_size: int, position: str) -> int:
    """Head, Middle, Tail에 따른 바이트 오프셋 계산."""
    if position == "Head":
        return 0
    elif position == "Middle":
        return file_size // 2
    elif position == "Tail":
        return max(0, file_size - change_size)
    else:
        raise ValueError(f"Unknown position: {position}")


def run_single_condition(
    base_file_path: str,
    source_dir: str,
    repo_dir: str,
    target_file: str,
    file_size_bytes: int,
    change_size_mb: float,
    position: str
) -> Dict[str, Any]:
    change_size_bytes = int(change_size_mb * (1024 ** 2))
    offset = calculate_offset(file_size_bytes, change_size_bytes, position)

    print("\n" + "=" * 80)
    print(f"[*] 조건 측정 시작: {change_size_mb} MB - {position} (Offset: {format_bytes(offset)})")
    print("=" * 80)

    # 1. 원본 파일에서 복사하여 타겟 파일 복원 (항상 동일한 원본 베이스라인 상태로 복원)
    shutil.copyfile(base_file_path, target_file)

    # 2. 파일 변이 적용 (지정 오프셋 덮어쓰기)
    time.sleep(1.5)  # mtime 변경 확실성 확보
    mutate_file(target_file, offset, change_size_bytes)

    # 3. 증분 백업 계측 실행
    blobs_dir = os.path.join(repo_dir, "blobs")
    blobs_size_before = get_dir_size(blobs_dir)

    tracker = ResourceTracker()
    tracker.start()

    snap = SnapshotEngine.create_snapshot(
        repo_dir=repo_dir,
        sources=[source_dir],
        profile_id="track2_bench",
        profile_name="Track2 Baseline Test",
        use_vss=False,
        strict_vss=False,
        min_free_disk_gb=1.0
    )

    res = tracker.stop()
    blobs_size_after = get_dir_size(blobs_dir)
    added_blob_disk = max(0, blobs_size_after - blobs_size_before)

    print(f"[+] 완료 (Snapshot ID: {snap.get('id')})")
    print(f" - 소요 시간 (Wall Clock) : {res['wall_time_sec']} 초")
    print(f" - 프로세스 읽기 (I/O Read) : {format_bytes(res['io_read_bytes'])} ({res['io_read_bytes']:,} B)")
    print(f" - 프로세스 쓰기 (I/O Write): {format_bytes(res['io_write_bytes'])} ({res['io_write_bytes']:,} B)")
    print(f" - 총 CPU 시간             : {res['cpu_total_sec']} 초 (User: {res['cpu_user_sec']}s, Sys: {res['cpu_system_sec']}s)")
    print(f" - 최대 물리 메모리 (RSS)   : {res['peak_rss_mb']} MB")
    print(f" - 신규 Blobs 디스크 추가   : {format_bytes(added_blob_disk)}")

    return {
        "condition": f"{change_size_mb}MB - {position}",
        "change_size_mb": change_size_mb,
        "position": position,
        "wall_time_sec": res["wall_time_sec"],
        "io_read_bytes": res["io_read_bytes"],
        "io_write_bytes": res["io_write_bytes"],
        "cpu_user_sec": res["cpu_user_sec"],
        "cpu_system_sec": res["cpu_system_sec"],
        "cpu_total_sec": res["cpu_total_sec"],
        "peak_rss_mb": res["peak_rss_mb"],
        "new_blob_bytes": added_blob_disk,
    }


def run_track2_2_matrix():
    file_size_gb = 10.0
    file_size_bytes = int(file_size_gb * (1024 ** 3))

    isolated_dir = tempfile.mkdtemp(prefix="track2_2_matrix_")
    source_dir = os.path.join(isolated_dir, "source")
    repo_dir = os.path.join(isolated_dir, "repo")
    base_file_path = os.path.join(isolated_dir, "golden_base_10g.bin")
    target_file = os.path.join(source_dir, "large_file.bin")

    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(repo_dir, exist_ok=True)

    print("=" * 80)
    print(" [Track 2-2: 현행 File CAS 10GB x 9개 매트릭스 벤치마크]")
    print(f" - 격리 작업 디렉토리: {isolated_dir}")
    print(f" - 파일 크기: {file_size_gb} GB ({file_size_bytes:,} Bytes)")
    print("=" * 80)

    results: List[Dict[str, Any]] = []

    try:
        # 1. 골든 베이스라인 10GB 파일 생성
        create_deterministic_large_file(base_file_path, file_size_bytes)

        # 2. 타겟 위치로 복사 후 Base 스냅샷 생성
        shutil.copyfile(base_file_path, target_file)
        print("\n[*] Initial Base Snapshot 생성 중 (Repository 초기화)...")
        snap_base = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[source_dir],
            profile_id="track2_bench",
            profile_name="Track2 Baseline Test",
            use_vss=False,
            strict_vss=False,
            min_free_disk_gb=1.0
        )
        print(f"[+] Base Snapshot 생성 완료: {snap_base.get('id')}")

        # 3. 9개 매트릭스 순차 실행
        matrix_conditions = [
            (1.0, "Head"),
            (1.0, "Middle"),  # Baseline 재사용
            (1.0, "Tail"),
            (10.0, "Head"),
            (10.0, "Middle"),
            (10.0, "Tail"),
            (100.0, "Head"),
            (100.0, "Middle"),
            (100.0, "Tail"),
        ]

        for change_mb, pos in matrix_conditions:
            if change_mb == 1.0 and pos == "Middle":
                print(f"\n[*] 1MB - Middle 조건: Track 2-1 Baseline 실측값 재사용 (Skip Execution)")
                results.append(BASELINE_1MB_MIDDLE)
                continue

            cond_res = run_single_condition(
                base_file_path=base_file_path,
                source_dir=source_dir,
                repo_dir=repo_dir,
                target_file=target_file,
                file_size_bytes=file_size_bytes,
                change_size_mb=change_mb,
                position=pos
            )
            results.append(cond_res)

        # 4. 최종 통합 비교표 출력
        print("\n" + "=" * 95)
        print(" [Track 2-2: 현행 File CAS 10GB 대형 파일 9개 매트릭스 최종 통합 실측표]")
        print("=" * 95)
        header = f"{'조건 (변경량 - 위치)':<22} | {'Wall Time':<10} | {'I/O Read':<11} | {'I/O Write':<11} | {'CPU (U/S)':<15} | {'Peak RSS':<9} | {'신규 Blob'}"
        print(header)
        print("-" * 95)
        for r in results:
            cpu_str = f"{r['cpu_total_sec']:.2f}s ({r['cpu_user_sec']:.1f}/{r['cpu_system_sec']:.1f})"
            row = (
                f"{r['condition']:<22} | "
                f"{r['wall_time_sec']:>8.2f}s | "
                f"{format_bytes(r['io_read_bytes']):>11} | "
                f"{format_bytes(r['io_write_bytes']):>11} | "
                f"{cpu_str:>15} | "
                f"{r['peak_rss_mb']:>6.1f} MB | "
                f"{format_bytes(r['new_blob_bytes'])}"
            )
            print(row)
        print("=" * 95 + "\n")

    finally:
        print("[*] 격리 테스트 환경 정리 중...")
        shutil.rmtree(isolated_dir, ignore_errors=True)
        print("[+] 정리 완료.")


if __name__ == "__main__":
    run_track2_2_matrix()
