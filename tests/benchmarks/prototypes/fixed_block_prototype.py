"""
Track 2-4: 4MB Fixed Block Prototype Chunking Engine & Benchmark Harness.

주의:
- 기존 production 코드(core/*)는 일체 수정하거나 호출하지 않습니다.
- 4MB 고정 블록 분할, SHA-256 해싱, 청크 저장소(CAS), 매니페스트 관리 및 bit-for-bit 복원 엔진을 내장합니다.
- 10GB 단일 파일에 대해:
  1. Initial Base Chunking
  2. 중앙 10MB Overwrite
  3. 중앙 1KB Insertion (Boundary Shift 검증)
  4. Head / Middle / Tail 각 1MB Overwrite
- 각 패턴별 정밀 계측:
  - 재처리 입력 바이트
  - Chunk 재사용률(Dedup %)
  - 신규 저장 바이트
  - 총 Chunk 수 및 신규 Chunk 수
  - Wall Clock Time, CPU Time (User/Sys)
  - Peak RSS
  - 원본 vs 복원 파일 SHA-256 Bit-for-Bit 일치 여부
"""

import os
import sys
import time
import shutil
import hashlib
import tempfile
import threading
from typing import Dict, Any, List, Tuple, Optional

try:
    import psutil
except ImportError:
    print("[ERROR] psutil 라이브러리가 필요합니다.")
    sys.exit(1)

BLOCK_SIZE = 4 * 1024 * 1024  # 4MB 고정 블록 크기


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
            "peak_rss_mb": round(self._peak_rss / (1024 * 1024), 2),
        }


def format_bytes(b: int) -> str:
    """바이트 단위를 사람이 읽기 쉬운 문자열로 변환."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(b) < 1024.0:
            return f"{b:3.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


class FixedBlockEngine:
    """독립형 4MB Fixed Block 청킹 & CAS 저장소 프로토타입 엔진."""

    def __init__(self, repo_dir: str, block_size: int = BLOCK_SIZE):
        self.repo_dir = repo_dir
        self.chunks_dir = os.path.join(repo_dir, "chunks")
        self.block_size = block_size
        self.known_chunks: Dict[str, int] = {}  # sha256 -> size
        os.makedirs(self.chunks_dir, exist_ok=True)

    def backup_file(self, filepath: str) -> Dict[str, Any]:
        """
        대상 파일을 4MB 고정 크기로 스트리밍 읽고 해싱하여 CAS 저장소에 저장.
        반환값: 매니페스트 (chunk_hashes 리스트, 통계 수치)
        """
        chunk_hashes: List[str] = []
        total_input_bytes = 0
        reused_chunks_count = 0
        new_chunks_count = 0
        new_stored_bytes = 0

        with open(filepath, "rb") as f:
            while True:
                data = f.read(self.block_size)
                if not data:
                    break
                total_input_bytes += len(data)
                h = hashlib.sha256(data).hexdigest()
                chunk_hashes.append(h)

                if h in self.known_chunks:
                    reused_chunks_count += 1
                else:
                    # 신규 청크 CAS 저장
                    chunk_path = os.path.join(self.chunks_dir, f"{h}.chk")
                    if not os.path.exists(chunk_path):
                        with open(chunk_path, "wb") as cf:
                            cf.write(data)
                    self.known_chunks[h] = len(data)
                    new_chunks_count += 1
                    new_stored_bytes += len(data)

        total_chunks = len(chunk_hashes)
        dedup_ratio = (reused_chunks_count / total_chunks * 100.0) if total_chunks > 0 else 0.0

        return {
            "chunk_hashes": chunk_hashes,
            "total_chunks": total_chunks,
            "reused_chunks": reused_chunks_count,
            "new_chunks": new_chunks_count,
            "total_input_bytes": total_input_bytes,
            "new_stored_bytes": new_stored_bytes,
            "dedup_pct": round(dedup_ratio, 2),
        }

    def restore_file(self, manifest: Dict[str, Any], output_path: str) -> Tuple[bool, str]:
        """
        매니페스트에 기록된 청크 해시 목록을 순차 재조립하여 원본 파일 복원.
        복원된 파일의 SHA-256을 계산하여 반환.
        """
        sha = hashlib.sha256()
        with open(output_path, "wb") as out_f:
            for h in manifest["chunk_hashes"]:
                chunk_path = os.path.join(self.chunks_dir, f"{h}.chk")
                with open(chunk_path, "rb") as cf:
                    data = cf.read()
                out_f.write(data)
                sha.update(data)
        return True, sha.hexdigest()


def compute_file_sha256(filepath: str) -> str:
    """파일의 전체 SHA-256 해시 스트리밍 계산."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest()


def create_deterministic_10gb_file(filepath: str, size_bytes: int):
    """
    10GB 크기의 결정론적 고유 블록 파일 생성.
    각 4MB 블록이 고유한(Unique) 데이터를 가져야 실제 프로덕션 환경의 비반복 파일과 동일하며,
    1KB 삽입 시 Boundary Shift로 인해 이후의 고유 블록들이 어떻게 무효화되는지 정확히 계측 가능합니다.
    """
    print(f"[*] 테스트 데이터 생성 중 (2,560개 고유 4MB 블록): {filepath} ({format_bytes(size_bytes)})...")
    written = 0
    t0 = time.perf_counter()
    # 4MB 블록 단위로 인덱스를 기반으로 한 고유 블록 생성
    with open(filepath, "wb") as f:
        block_idx = 0
        while written < size_bytes:
            to_write = min(BLOCK_SIZE, size_bytes - written)
            # 블록 헤더에 고유 블록 인덱스와 결정론적 시드 포함
            seed_data = hashlib.sha256(f"unique_block_{block_idx}".encode()).digest()
            # 4MB를 시드 반복으로 채워 각 블록이 완전히 고유함을 보장
            block_data = (seed_data * (BLOCK_SIZE // len(seed_data) + 1))[:to_write]
            f.write(block_data)
            written += to_write
            block_idx += 1
            if written % (1024 * 1024 * 1024) == 0:
                print(f"    - {written // (1024 * 1024 * 1024)} GB 작성 완료...")
    f_time = time.perf_counter() - t0
    print(f"[+] 파일 생성 완료 ({f_time:.2f}초, {format_bytes(size_bytes)})")


def run_fixed_block_benchmark(file_size_gb: float = 10.0):
    file_size_bytes = int(file_size_gb * (1024 ** 3))

    isolated_dir = tempfile.mkdtemp(prefix="track2_4_fixed_block_")
    repo_dir = os.path.join(isolated_dir, "repo")
    base_file = os.path.join(isolated_dir, "golden_base_10g.bin")
    work_file = os.path.join(isolated_dir, "work_file.bin")
    restore_file = os.path.join(isolated_dir, "restored.bin")

    os.makedirs(repo_dir, exist_ok=True)
    engine = FixedBlockEngine(repo_dir=repo_dir, block_size=BLOCK_SIZE)

    print("\n" + "=" * 95)
    print(f" [Track 2-4: 4MB Fixed Block Prototype 벤치마크 (10GB 대상)]")
    print(f" - 격리 작업 디렉토리: {isolated_dir}")
    print(f" - 고정 블록 크기: {format_bytes(BLOCK_SIZE)}")
    print(f" - 예상 총 블록 수: {file_size_bytes // BLOCK_SIZE:,} 개")
    print("=" * 95 + "\n")

    results_table = []

    try:
        # 0. 10GB 골든 베이스라인 파일 생성
        create_deterministic_10gb_file(base_file, file_size_bytes)
        shutil.copyfile(base_file, work_file)
        golden_sha256 = compute_file_sha256(work_file)
        print(f"[*] Base File SHA-256: {golden_sha256}")

        # -----------------------------------------------------------------
        # Step 0: Initial Base Backup (최초 Chunking)
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Step 0] Initial Base Chunking 실행 중...")
        tracker = ResourceTracker()
        tracker.start()
        base_manifest = engine.backup_file(work_file)
        res_base = tracker.stop()

        # 복원 검증
        ok, restored_sha = engine.restore_file(base_manifest, restore_file)
        bit_for_bit = (restored_sha == golden_sha256)
        os.remove(restore_file)

        print(f"[+] Base Chunking 완료 (Wall: {res_base['wall_time_sec']}s, Dedup: {base_manifest['dedup_pct']}%, Bit-for-bit: {bit_for_bit})")
        results_table.append({
            "pattern": "0. Initial Base (10GB)",
            "wall_sec": res_base["wall_time_sec"],
            "io_read": res_base["io_read_bytes"],
            "input_bytes": base_manifest["total_input_bytes"],
            "reused_chunks": base_manifest["reused_chunks"],
            "new_chunks": base_manifest["new_chunks"],
            "dedup_pct": base_manifest["dedup_pct"],
            "new_stored": base_manifest["new_stored_bytes"],
            "cpu_total": res_base["cpu_total_sec"],
            "cpu_user": res_base["cpu_user_sec"],
            "cpu_sys": res_base["cpu_system_sec"],
            "rss_mb": res_base["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # Pattern 1: 중앙 10MB Overwrite (In-Place Mutation)
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Pattern 1] 중앙 10MB Overwrite 테스트...")
        shutil.copyfile(base_file, work_file)
        offset_mid = file_size_bytes // 2
        with open(work_file, "r+b") as f:
            f.seek(offset_mid)
            f.write(os.urandom(10 * 1024 * 1024))
            f.flush()
        target_sha = compute_file_sha256(work_file)

        tracker = ResourceTracker()
        tracker.start()
        m1 = engine.backup_file(work_file)
        res_m1 = tracker.stop()

        ok, restored_sha = engine.restore_file(m1, restore_file)
        bit_for_bit = (restored_sha == target_sha)
        os.remove(restore_file)

        print(f"[+] Pattern 1 완료 (Wall: {res_m1['wall_time_sec']}s, Dedup: {m1['dedup_pct']}%, 신규청크: {m1['new_chunks']}, Bit-for-bit: {bit_for_bit})")
        results_table.append({
            "pattern": "1. 10MB Overwrite (Middle)",
            "wall_sec": res_m1["wall_time_sec"],
            "io_read": res_m1["io_read_bytes"],
            "input_bytes": m1["total_input_bytes"],
            "reused_chunks": m1["reused_chunks"],
            "new_chunks": m1["new_chunks"],
            "dedup_pct": m1["dedup_pct"],
            "new_stored": m1["new_stored_bytes"],
            "cpu_total": res_m1["cpu_total_sec"],
            "cpu_user": res_m1["cpu_user_sec"],
            "cpu_sys": res_m1["cpu_system_sec"],
            "rss_mb": res_m1["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # Pattern 2: 중앙 1KB Insertion (Boundary Shift 현상 검증)
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Pattern 2] 중앙 1KB Insertion (Boundary Shift 검증) 테스트...")
        # 원본의 앞 5GB + 1KB 데이터 + 뒤 5GB 결합
        insert_temp = work_file + ".insert_tmp"
        insert_data = os.urandom(1024)  # 1KB
        with open(base_file, "rb") as src, open(insert_temp, "wb") as dst:
            dst.write(src.read(offset_mid))
            dst.write(insert_data)
            while chunk := src.read(16 * 1024 * 1024):
                dst.write(chunk)
        os.replace(insert_temp, work_file)
        target_sha = compute_file_sha256(work_file)

        tracker = ResourceTracker()
        tracker.start()
        m2 = engine.backup_file(work_file)
        res_m2 = tracker.stop()

        ok, restored_sha = engine.restore_file(m2, restore_file)
        bit_for_bit = (restored_sha == target_sha)
        os.remove(restore_file)

        print(f"[+] Pattern 2 완료 (Wall: {res_m2['wall_time_sec']}s, Dedup: {m2['dedup_pct']}%, 신규청크: {m2['new_chunks']}, Bit-for-bit: {bit_for_bit})")
        results_table.append({
            "pattern": "2. 1KB Insertion (Boundary Shift)",
            "wall_sec": res_m2["wall_time_sec"],
            "io_read": res_m2["io_read_bytes"],
            "input_bytes": m2["total_input_bytes"],
            "reused_chunks": m2["reused_chunks"],
            "new_chunks": m2["new_chunks"],
            "dedup_pct": m2["dedup_pct"],
            "new_stored": m2["new_stored_bytes"],
            "cpu_total": res_m2["cpu_total_sec"],
            "cpu_user": res_m2["cpu_user_sec"],
            "cpu_sys": res_m2["cpu_system_sec"],
            "rss_mb": res_m2["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # Pattern 3: Head / Middle / Tail 각 1MB Overwrite
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Pattern 3] Head / Middle / Tail 각 1MB Overwrite (다지점 분산) 테스트...")
        shutil.copyfile(base_file, work_file)
        # Head (0), Middle (5GB), Tail (10GB - 1MB)
        with open(work_file, "r+b") as f:
            # Head
            f.seek(0)
            f.write(os.urandom(1024 * 1024))
            # Middle
            f.seek(offset_mid)
            f.write(os.urandom(1024 * 1024))
            # Tail
            f.seek(file_size_bytes - (1024 * 1024))
            f.write(os.urandom(1024 * 1024))
            f.flush()
        target_sha = compute_file_sha256(work_file)

        tracker = ResourceTracker()
        tracker.start()
        m3 = engine.backup_file(work_file)
        res_m3 = tracker.stop()

        ok, restored_sha = engine.restore_file(m3, restore_file)
        bit_for_bit = (restored_sha == target_sha)
        os.remove(restore_file)

        print(f"[+] Pattern 3 완료 (Wall: {res_m3['wall_time_sec']}s, Dedup: {m3['dedup_pct']}%, 신규청크: {m3['new_chunks']}, Bit-for-bit: {bit_for_bit})")
        results_table.append({
            "pattern": "3. H/M/T 각 1MB Overwrite",
            "wall_sec": res_m3["wall_time_sec"],
            "io_read": res_m3["io_read_bytes"],
            "input_bytes": m3["total_input_bytes"],
            "reused_chunks": m3["reused_chunks"],
            "new_chunks": m3["new_chunks"],
            "dedup_pct": m3["dedup_pct"],
            "new_stored": m3["new_stored_bytes"],
            "cpu_total": res_m3["cpu_total_sec"],
            "cpu_user": res_m3["cpu_user_sec"],
            "cpu_sys": res_m3["cpu_system_sec"],
            "rss_mb": res_m3["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # 최종 비교표 출력
        # -----------------------------------------------------------------
        print("\n" + "=" * 105)
        print(" [Track 2-4: 4MB Fixed Block Prototype 최종 실측 비교표]")
        print("=" * 105)
        header = f"{'실험 패턴':<28} | {'Wall Time':<9} | {'Dedup %':<8} | {'재사용/신규 Chunk':<16} | {'신규 저장 바이트':<14} | {'CPU (U/S)':<14} | {'Bit-for-Bit'}"
        print(header)
        print("-" * 105)
        for r in results_table:
            chunks_str = f"{r['reused_chunks']:,} / {r['new_chunks']:,}"
            cpu_str = f"{r['cpu_total']:.2f}s ({r['cpu_user']:.1f}/{r['cpu_sys']:.1f})"
            bit_str = "SUCCESS (100%)" if r["bit_match"] else "FAILED"
            row = (
                f"{r['pattern']:<28} | "
                f"{r['wall_sec']:>7.2f}s | "
                f"{r['dedup_pct']:>6.2f}% | "
                f"{chunks_str:>16} | "
                f"{format_bytes(r['new_stored']):>14} | "
                f"{cpu_str:>14} | "
                f"{bit_str}"
            )
            print(row)
        print("=" * 105 + "\n")

    finally:
        print("[*] 격리 테스트 환경 정리 중...")
        shutil.rmtree(isolated_dir, ignore_errors=True)
        print("[+] 정리 완료.")


if __name__ == "__main__":
    run_fixed_block_benchmark()
