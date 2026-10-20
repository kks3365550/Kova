"""
Track 2-1: 현행 File CAS 대형 파일(10GB) + 1MB 변경 격리 Baseline 벤치마크 스크립트.

주의:
- 본 스크립트는 최적화, Fixed Block, FastCDC 등의 새로운 청킹을 일체 적용하지 않고,
  현행 core.snapshot.SnapshotEngine 및 core.storage.BlobStorage의 동작을 순수 측정합니다.
- 프로세스 I/O 카운터(psutil.Process().io_counters()), CPU 시간, 피크 RSS, 실제 파일시스템 변화량을 수집합니다.
- OS Page Cache 등으로 인해 드라이버 레벨의 실제 물리 디스크 I/O와 차이가 있을 수 있음을 명시합니다.
"""

import os
import sys
import time
import shutil
import tempfile
import threading
import argparse
from typing import Dict, Any, Optional

# 백업시스템 루트 경로를 sys.path에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    import psutil
except ImportError:
    print("[ERROR] psutil 라이브러리가 필요합니다. 'pip install psutil'을 실행하세요.")
    sys.exit(1)

from core.snapshot import SnapshotEngine
from core.storage import BlobStorage


class ResourceTracker:
    """프로세스 리소스(I/O, CPU, RSS) 변화를 추적하는 정밀 계측기."""

    def __init__(self, pid: Optional[int] = None):
        self.proc = psutil.Process(pid or os.getpid())
        self._peak_rss = 0
        self._sampling = False
        self._sampler_thread: Optional[threading.Thread] = None
        self._start_wall = 0.0
        self._start_io = None
        self._start_cpu = None

    def _sample_rss(self):
        while self._sampling:
            try:
                rss = self.proc.memory_info().rss
                if rss > self._peak_rss:
                    self._peak_rss = rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            time.sleep(0.05)

    def start(self):
        self._start_wall = time.perf_counter()
        try:
            self._start_io = self.proc.io_counters()
        except Exception:
            self._start_io = None
        self._start_cpu = self.proc.cpu_times()
        self._peak_rss = self.proc.memory_info().rss

        self._sampling = True
        self._sampler_thread = threading.Thread(target=self._sample_rss, daemon=True)
        self._sampler_thread.start()

    def stop(self) -> Dict[str, Any]:
        self._sampling = False
        if self._sampler_thread and self._sampler_thread.is_alive():
            self._sampler_thread.join(timeout=0.5)

        end_wall = time.perf_counter()
        end_cpu = self.proc.cpu_times()
        try:
            end_io = self.proc.io_counters()
        except Exception:
            end_io = None

        wall_sec = end_wall - self._start_wall
        user_cpu_sec = end_cpu.user - self._start_cpu.user
        sys_cpu_sec = end_cpu.system - self._start_cpu.system
        total_cpu_sec = user_cpu_sec + sys_cpu_sec

        read_bytes = 0
        write_bytes = 0
        if self._start_io and end_io:
            read_bytes = end_io.read_bytes - self._start_io.read_bytes
            write_bytes = end_io.write_bytes - self._start_io.write_bytes

        return {
            "wall_time_sec": round(wall_sec, 3),
            "cpu_user_sec": round(user_cpu_sec, 3),
            "cpu_system_sec": round(sys_cpu_sec, 3),
            "cpu_total_sec": round(total_cpu_sec, 3),
            "io_read_bytes": read_bytes,
            "io_write_bytes": write_bytes,
            "peak_rss_bytes": self._peak_rss,
            "peak_rss_mb": round(self._peak_rss / (1024 * 1024), 2),
        }


def format_bytes(b: int) -> str:
    """바이트 크기를 읽기 쉬운 문자열로 변환."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(b) < 1024.0:
            return f"{b:3.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def get_dir_size(path: str) -> int:
    """디렉토리 내 모든 파일의 총 크기 계산."""
    total = 0
    if not os.path.exists(path):
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def create_deterministic_large_file(filepath: str, size_bytes: int, chunk_size: int = 16 * 1024 * 1024):
    """
    10GB 대형 파일을 생성합니다.
    (완전한 0바이트 파일은 zstd 압축 시 비현실적으로 0에 가깝게 줄어들므로,
     적절한 패턴 데이터를 섞어 현실적인 I/O 및 압축 부하를 유도합니다)
    """
    print(f"[*] 테스트 데이터 생성 중: {filepath} ({format_bytes(size_bytes)})...")
    written = 0
    pattern_block = os.urandom(64 * 1024) * (chunk_size // (64 * 1024))
    
    t0 = time.perf_counter()
    with open(filepath, "wb") as f:
        while written < size_bytes:
            to_write = min(chunk_size, size_bytes - written)
            if to_write == chunk_size:
                f.write(pattern_block)
            else:
                f.write(pattern_block[:to_write])
            written += to_write
            if written % (1024 * 1024 * 1024) == 0:
                print(f"    - {written // (1024 * 1024 * 1024)} GB 작성 완료...")
    f_time = time.perf_counter() - t0
    print(f"[+] 파일 생성 완료 ({f_time:.2f}초, {format_bytes(size_bytes)})")


def mutate_file(filepath: str, offset: int, change_size: int):
    """지정된 오프셋의 데이터를 덮어쓰고 mtime을 명시적으로 갱신합니다."""
    print(f"[*] 파일 변이(Mutation) 실행: 오프셋 {format_bytes(offset)} 위치에 {format_bytes(change_size)} 덮어쓰기...")
    new_data = os.urandom(change_size)
    with open(filepath, "r+b") as f:
        f.seek(offset)
        f.write(new_data)
        f.flush()
        os.fsync(f.fileno())

    # mtime 갱신 보장
    now = time.time()
    os.utime(filepath, (now, now))
    print(f"[+] 파일 변이 완료: mtime={now}")


def run_benchmark(file_size_gb: float = 10.0, change_size_mb: float = 1.0, base_dir: Optional[str] = None):
    file_size_bytes = int(file_size_gb * (1024 ** 3))
    change_size_bytes = int(change_size_mb * (1024 ** 2))

    # 테스트 전용 격리 폴더 생성
    if base_dir:
        bench_root = os.path.abspath(base_dir)
        os.makedirs(bench_root, exist_ok=True)
        isolated_dir = tempfile.mkdtemp(prefix="track2_bench_", dir=bench_root)
    else:
        isolated_dir = tempfile.mkdtemp(prefix="track2_bench_")

    source_dir = os.path.join(isolated_dir, "source")
    repo_dir = os.path.join(isolated_dir, "repo")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(repo_dir, exist_ok=True)

    target_file = os.path.join(source_dir, "large_file.bin")

    print("\n" + "=" * 80)
    print(" [Track 2-1 Baseline Benchmark: 현행 File CAS 대형 파일 측정]")
    print(f" - 격리 작업 디렉토리: {isolated_dir}")
    print(f" - 단일 파일 크기: {file_size_gb} GB ({file_size_bytes:,} Bytes)")
    print(f" - 부분 변경 크기: {change_size_mb} MB ({change_size_bytes:,} Bytes)")
    print("=" * 80 + "\n")

    try:
        # 0. 10GB 테스트 파일 생성
        create_deterministic_large_file(target_file, file_size_bytes)

        # -------------------------------------------------------------
        # 1. Base 백업 (최초 전체 백업)
        # -------------------------------------------------------------
        print("\n[Step 1] 최초 전체 백업(Base Backup) 시작...")
        tracker_base = ResourceTracker()
        tracker_base.start()

        snap_base = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[source_dir],
            profile_id="track2_bench",
            profile_name="Track2 Baseline Test",
            use_vss=False,
            strict_vss=False,
            min_free_disk_gb=1.0
        )

        res_base = tracker_base.stop()
        repo_size_base = get_dir_size(repo_dir)
        blobs_size_base = get_dir_size(os.path.join(repo_dir, "blobs"))

        print(f"[Step 1 결과] 완료 (Snapshot ID: {snap_base.get('id')})")
        print(f" - 소요 시간 (Wall Clock) : {res_base['wall_time_sec']} 초")
        print(f" - 프로세스 읽기 (I/O Read) : {format_bytes(res_base['io_read_bytes'])} ({res_base['io_read_bytes']:,} B)")
        print(f" - 프로세스 쓰기 (I/O Write): {format_bytes(res_base['io_write_bytes'])} ({res_base['io_write_bytes']:,} B)")
        print(f" - 총 CPU 시간             : {res_base['cpu_total_sec']} 초 (User: {res_base['cpu_user_sec']}s, Sys: {res_base['cpu_system_sec']}s)")
        print(f" - 최대 물리 메모리 (RSS)   : {res_base['peak_rss_mb']} MB")
        print(f" - 저장소 Blobs 디스크 용량 : {format_bytes(blobs_size_base)}")

        # -------------------------------------------------------------
        # 2. 파일 변이 (1MB 변경 - 파일 중앙 위치)
        # -------------------------------------------------------------
        # NTFS/FAT32 mtime tolerance(1.0s)를 확실히 초과하도록 최소 1.5초 지연
        time.sleep(1.5)
        print("\n[Step 2] 1MB 부분 변경(Middle Mutation) 적용 중...")
        middle_offset = file_size_bytes // 2
        mutate_file(target_file, middle_offset, change_size_bytes)

        # -------------------------------------------------------------
        # 3. 증분 백업 (Incr 백업)
        # -------------------------------------------------------------
        print("\n[Step 3] 증분 백업(Incremental Backup) 시작...")
        tracker_incr = ResourceTracker()
        tracker_incr.start()

        snap_incr = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=[source_dir],
            profile_id="track2_bench",
            profile_name="Track2 Baseline Test",
            use_vss=False,
            strict_vss=False,
            min_free_disk_gb=1.0
        )

        res_incr = tracker_incr.stop()
        repo_size_incr = get_dir_size(repo_dir)
        blobs_size_incr = get_dir_size(os.path.join(repo_dir, "blobs"))
        added_blob_disk = blobs_size_incr - blobs_size_base

        print(f"[Step 3 결과] 완료 (Snapshot ID: {snap_incr.get('id')})")
        print(f" - 소요 시간 (Wall Clock) : {res_incr['wall_time_sec']} 초")
        print(f" - 프로세스 읽기 (I/O Read) : {format_bytes(res_incr['io_read_bytes'])} ({res_incr['io_read_bytes']:,} B)")
        print(f" - 프로세스 쓰기 (I/O Write): {format_bytes(res_incr['io_write_bytes'])} ({res_incr['io_write_bytes']:,} B)")
        print(f" - 총 CPU 시간             : {res_incr['cpu_total_sec']} 초 (User: {res_incr['cpu_user_sec']}s, Sys: {res_incr['cpu_system_sec']}s)")
        print(f" - 최대 물리 메모리 (RSS)   : {res_incr['peak_rss_mb']} MB")
        print(f" - 신규 추가된 Blobs 용량   : {format_bytes(added_blob_disk)}")

        # -------------------------------------------------------------
        # 4. 종합 분석 및 요약 표
        # -------------------------------------------------------------
        print("\n" + "=" * 80)
        print(" [Track 2-1 Baseline Benchmark 최종 측정 결과 보고서]")
        print("=" * 80)
        print(f"변경량: {format_bytes(change_size_bytes)} (파일 전체의 {(change_size_bytes / file_size_bytes)*100:.4f}%)")
        print("-" * 80)
        print(f"{'지표 항목':<25} | {'Base 백업':<20} | {'증분 백업 (1MB 변경 후)':<25}")
        print("-" * 80)
        print(f"{'소요 시간 (Wall Time)':<25} | {str(res_base['wall_time_sec']) + 's':<20} | {str(res_incr['wall_time_sec']) + 's':<25}")
        print(f"{'I/O Read 바이트':<25} | {format_bytes(res_base['io_read_bytes']):<20} | {format_bytes(res_incr['io_read_bytes']):<25}")
        print(f"{'I/O Write 바이트':<25} | {format_bytes(res_base['io_write_bytes']):<20} | {format_bytes(res_incr['io_write_bytes']):<25}")
        print(f"{'CPU Time (User/Sys)':<25} | {str(res_base['cpu_total_sec']) + 's':<20} | {str(res_incr['cpu_total_sec']) + 's':<25}")
        print(f"{'Peak RSS 메모리':<25} | {str(res_base['peak_rss_mb']) + ' MB':<20} | {str(res_incr['peak_rss_mb']) + ' MB':<25}")
        print(f"{'저장소 Blobs 크기':<25} | {format_bytes(blobs_size_base):<20} | {format_bytes(added_blob_disk) + ' (추가)':<25}")
        print("-" * 80)

        # 분석 결론
        read_ratio = res_incr['io_read_bytes'] / file_size_bytes if file_size_bytes else 0
        print("\n[핵심 관찰 및 분석]")
        if read_ratio >= 0.8:
            print(f"  [확인] 1MB만 변경되었음에도 전체 파일 대비 {read_ratio*100:.1f}% ({format_bytes(res_incr['io_read_bytes'])})를 다시 읽었습니다.")
            print("  -> File CAS 구조상 전체 파일 재해싱 및 재압축이 발생함을 실측으로 입증함.")
        else:
            print(f"  [관찰] 파일 읽기 비율: {read_ratio*100:.1f}%")

        if added_blob_disk >= (change_size_bytes * 10):
            print(f"  [확인] 1MB 변경에 대해 저장소에 {format_bytes(added_blob_disk)} 크기의 신규 블롭이 생성되었습니다.")
            print("  -> File CAS 구조상 새 파일 전체 블롭이 중복 저장됨을 실측으로 입증함.")

        print("\n[측정 한계 및 유의사항]")
        print("  - 'I/O Read Bytes'는 커널 ReadFile 호출 기준이며, OS Page Cache에 의해 실제 물리 SSD 읽기는 감소했을 수 있습니다.")
        print("  - 실제 드라이버 레벨 쓰기는 OS Dirty Page 지연 쓰기로 인해 백업 함수 종료 후 백그라운드에서 완료될 수 있습니다.")
        print("=" * 80 + "\n")

    finally:
        print("[*] 격리 테스트 환경 정리 중...")
        try:
            shutil.rmtree(isolated_dir, ignore_errors=True)
            print("[+] 정리 완료.")
        except Exception as e:
            print(f"[!] 정리 중 오류 발생: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track 2-1 Baseline Benchmark Runner")
    parser.add_argument("--size-gb", type=float, default=10.0, help="단일 파일 크기(GB) (기본값: 10.0)")
    parser.add_argument("--change-mb", type=float, default=1.0, help="부분 변경 크기(MB) (기본값: 1.0)")
    parser.add_argument("--dir", type=str, default=None, help="테스트 격리 디렉토리 부모 경로 (선택)")
    args = parser.parse_args()

    run_benchmark(file_size_gb=args.size_gb, change_size_mb=args.change_mb, base_dir=args.dir)
