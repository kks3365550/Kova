"""
Track 2-5: FastCDC (Content-Defined Chunking) Prototype Engine & Benchmark Harness.

주의:
- 기존 production 코드(core/*)는 일체 수정하거나 호출하지 않습니다.
- FastCDC 알고리즘 사양:
  * min_size: 1MB (1,048,576 B)
  * avg_size: 4MB (4,194,304 B)
  * max_size: 8MB (8,388,608 B)
- 10GB 단일 파일(2,560개 고유 블록 데이터) 대상 3대 변이 패턴 실측:
  1. Base: 10GB 최초 FastCDC 청킹
  2. 중앙 10MB Overwrite (In-Place Mutation)
  3. 중앙 1KB Insertion (핵심: Fixed Block 5GB 무효화 대비 FastCDC 경계 재동기화 및 실제 재사용률 실측)
  4. Head / Middle / Tail 각 1MB Overwrite (다지점 분산)
- 측정 지표:
  * Base chunk 수, 평균/최소/최대 chunk 크기
  * 실제 재처리 입력 바이트
  * 기존 chunk 재사용률(Dedup %)
  * 신규 chunk 수 및 신규 저장 바이트
  * Wall Clock Time, CPU Time (User/Sys), Peak RSS
  * 원본 vs 복원 SHA-256 Bit-for-Bit 100% 일치 검증
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

try:
    import fastcdc
except ImportError:
    print("[ERROR] fastcdc 라이브러리가 필요합니다.")
    sys.exit(1)

# FastCDC 파라미터 규격 (요청 사양)
MIN_CHUNK_SIZE = 1 * 1024 * 1024   # 1MB
AVG_CHUNK_SIZE = 4 * 1024 * 1024   # 4MB
MAX_CHUNK_SIZE = 8 * 1024 * 1024   # 8MB


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


class FastCDCEngine:
    """독립형 FastCDC 청킹 & CAS 저장소 프로토타입 엔진."""

    def __init__(self, repo_dir: str, min_size: int = MIN_CHUNK_SIZE, avg_size: int = AVG_CHUNK_SIZE, max_size: int = MAX_CHUNK_SIZE):
        self.repo_dir = repo_dir
        self.chunks_dir = os.path.join(repo_dir, "chunks")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size
        self.known_chunks: Dict[str, int] = {}  # sha256 -> size
        os.makedirs(self.chunks_dir, exist_ok=True)

    def backup_file(self, filepath: str) -> Dict[str, Any]:
        """
        FastCDC 가변 청킹을 수행하고 각 청크의 SHA-256을 CAS에 저장.
        """
        chunk_hashes: List[str] = []
        chunk_sizes: List[int] = []
        total_input_bytes = 0
        reused_chunks_count = 0
        new_chunks_count = 0
        new_stored_bytes = 0

        with open(filepath, "rb") as f:
            for c in fastcdc.fastcdc(f, min_size=self.min_size, avg_size=self.avg_size, max_size=self.max_size):
                chunk_len = c.length
                total_input_bytes += chunk_len
                chunk_sizes.append(chunk_len)

                # 파일에서 해당 청크 데이터 읽기 및 SHA-256 연산
                f.seek(c.offset)
                chunk_data = f.read(chunk_len)
                h = hashlib.sha256(chunk_data).hexdigest()
                chunk_hashes.append(h)

                if h in self.known_chunks:
                    reused_chunks_count += 1
                else:
                    chunk_path = os.path.join(self.chunks_dir, f"{h}.chk")
                    if not os.path.exists(chunk_path):
                        with open(chunk_path, "wb") as cf:
                            cf.write(chunk_data)
                    self.known_chunks[h] = chunk_len
                    new_chunks_count += 1
                    new_stored_bytes += chunk_len

        total_chunks = len(chunk_hashes)
        dedup_ratio = (reused_chunks_count / total_chunks * 100.0) if total_chunks > 0 else 0.0

        min_s = min(chunk_sizes) if chunk_sizes else 0
        max_s = max(chunk_sizes) if chunk_sizes else 0
        avg_s = (sum(chunk_sizes) // len(chunk_sizes)) if chunk_sizes else 0

        return {
            "chunk_hashes": chunk_hashes,
            "total_chunks": total_chunks,
            "reused_chunks": reused_chunks_count,
            "new_chunks": new_chunks_count,
            "total_input_bytes": total_input_bytes,
            "new_stored_bytes": new_stored_bytes,
            "dedup_pct": round(dedup_ratio, 2),
            "min_chunk_bytes": min_s,
            "max_chunk_bytes": max_s,
            "avg_chunk_bytes": avg_s,
        }

    def restore_file(self, manifest: Dict[str, Any], output_path: str) -> Tuple[bool, str]:
        """
        FastCDC 가변 청크들을 순차 재조립하여 원본 복원 및 SHA-256 검증.
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
    """Track 2-4와 100% 동일한 2,560개 고유 블록 10GB 파일 생성."""
    print(f"[*] 테스트 데이터 생성 중 (2,560개 고유 블록): {filepath} ({format_bytes(size_bytes)})...")
    block_size = 4 * 1024 * 1024
    written = 0
    t0 = time.perf_counter()
    with open(filepath, "wb") as f:
        block_idx = 0
        while written < size_bytes:
            to_write = min(block_size, size_bytes - written)
            seed_data = hashlib.sha256(f"unique_block_{block_idx}".encode()).digest()
            block_data = (seed_data * (block_size // len(seed_data) + 1))[:to_write]
            f.write(block_data)
            written += to_write
            block_idx += 1
            if written % (1024 * 1024 * 1024) == 0:
                print(f"    - {written // (1024 * 1024 * 1024)} GB 작성 완료...")
    f_time = time.perf_counter() - t0
    print(f"[+] 파일 생성 완료 ({f_time:.2f}초, {format_bytes(size_bytes)})")


def run_fastcdc_benchmark(file_size_gb: float = 10.0):
    file_size_bytes = int(file_size_gb * (1024 ** 3))

    isolated_dir = tempfile.mkdtemp(prefix="track2_5_fastcdc_")
    repo_dir = os.path.join(isolated_dir, "repo")
    base_file = os.path.join(isolated_dir, "golden_base_10g.bin")
    work_file = os.path.join(isolated_dir, "work_file.bin")
    restore_file = os.path.join(isolated_dir, "restored.bin")

    os.makedirs(repo_dir, exist_ok=True)
    engine = FastCDCEngine(repo_dir=repo_dir, min_size=MIN_CHUNK_SIZE, avg_size=AVG_CHUNK_SIZE, max_size=MAX_CHUNK_SIZE)

    print("\n" + "=" * 95)
    print(f" [Track 2-5 Phase B: FastCDC Prototype 벤치마크 (10GB 대상)]")
    print(f" - 격리 작업 디렉토리: {isolated_dir}")
    print(f" - FastCDC 파라미터: Min {format_bytes(MIN_CHUNK_SIZE)} / Target {format_bytes(AVG_CHUNK_SIZE)} / Max {format_bytes(MAX_CHUNK_SIZE)}")
    print("=" * 95 + "\n")

    results_table = []

    try:
        # 0. 10GB 골든 베이스라인 파일 생성
        create_deterministic_10gb_file(base_file, file_size_bytes)
        shutil.copyfile(base_file, work_file)
        golden_sha256 = compute_file_sha256(work_file)
        print(f"[*] Base File SHA-256: {golden_sha256}")

        # -----------------------------------------------------------------
        # Step 0: Initial Base Backup (최초 FastCDC 청킹)
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Step 0] Initial Base FastCDC 청킹 실행 중...")
        tracker = ResourceTracker()
        tracker.start()
        base_manifest = engine.backup_file(work_file)
        res_base = tracker.stop()

        # 복원 검증
        ok, restored_sha = engine.restore_file(base_manifest, restore_file)
        bit_for_bit = (restored_sha == golden_sha256)
        os.remove(restore_file)

        print(f"[+] Base Chunking 완료 (Wall: {res_base['wall_time_sec']}s, 총 청크: {base_manifest['total_chunks']:,}개, "
              f"Avg 크기: {format_bytes(base_manifest['avg_chunk_bytes'])}, Bit-for-bit: {bit_for_bit})")
        results_table.append({
            "pattern": "0. Initial Base (10GB)",
            "wall_sec": res_base["wall_time_sec"],
            "total_chunks": base_manifest["total_chunks"],
            "reused_chunks": base_manifest["reused_chunks"],
            "new_chunks": base_manifest["new_chunks"],
            "dedup_pct": base_manifest["dedup_pct"],
            "new_stored": base_manifest["new_stored_bytes"],
            "avg_chunk": base_manifest["avg_chunk_bytes"],
            "min_chunk": base_manifest["min_chunk_bytes"],
            "max_chunk": base_manifest["max_chunk_bytes"],
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
            "total_chunks": m1["total_chunks"],
            "reused_chunks": m1["reused_chunks"],
            "new_chunks": m1["new_chunks"],
            "dedup_pct": m1["dedup_pct"],
            "new_stored": m1["new_stored_bytes"],
            "avg_chunk": m1["avg_chunk_bytes"],
            "min_chunk": m1["min_chunk_bytes"],
            "max_chunk": m1["max_chunk_bytes"],
            "cpu_total": res_m1["cpu_total_sec"],
            "cpu_user": res_m1["cpu_user_sec"],
            "cpu_sys": res_m1["cpu_system_sec"],
            "rss_mb": res_m1["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # Pattern 2: 중앙 1KB Insertion (FastCDC 경계 재동기화 핵심 검증)
        # -----------------------------------------------------------------
        print("\n" + "-" * 80)
        print("[Pattern 2] 중앙 1KB Insertion (경계 재동기화 검증) 테스트...")
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
            "pattern": "2. 1KB Insertion (Resync)",
            "wall_sec": res_m2["wall_time_sec"],
            "total_chunks": m2["total_chunks"],
            "reused_chunks": m2["reused_chunks"],
            "new_chunks": m2["new_chunks"],
            "dedup_pct": m2["dedup_pct"],
            "new_stored": m2["new_stored_bytes"],
            "avg_chunk": m2["avg_chunk_bytes"],
            "min_chunk": m2["min_chunk_bytes"],
            "max_chunk": m2["max_chunk_bytes"],
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
            "total_chunks": m3["total_chunks"],
            "reused_chunks": m3["reused_chunks"],
            "new_chunks": m3["new_chunks"],
            "dedup_pct": m3["dedup_pct"],
            "new_stored": m3["new_stored_bytes"],
            "avg_chunk": m3["avg_chunk_bytes"],
            "min_chunk": m3["min_chunk_bytes"],
            "max_chunk": m3["max_chunk_bytes"],
            "cpu_total": res_m3["cpu_total_sec"],
            "cpu_user": res_m3["cpu_user_sec"],
            "cpu_sys": res_m3["cpu_system_sec"],
            "rss_mb": res_m3["peak_rss_mb"],
            "bit_match": bit_for_bit
        })

        # -----------------------------------------------------------------
        # 최종 비교표 출력
        # -----------------------------------------------------------------
        print("\n" + "=" * 115)
        print(" [Track 2-5 Phase B: FastCDC Prototype 최종 실측 결과표]")
        print("=" * 115)
        header = f"{'실험 패턴':<28} | {'Wall Time':<9} | {'Dedup %':<8} | {'재사용/신규 Chunk':<16} | {'신규 저장 바이트':<14} | {'청크 크기 (Min/Avg/Max)':<22} | {'Bit-for-Bit'}"
        print(header)
        print("-" * 115)
        for r in results_table:
            chunks_str = f"{r['reused_chunks']:,} / {r['new_chunks']:,}"
            size_str = f"{format_bytes(r['min_chunk'])} / {format_bytes(r['avg_chunk'])} / {format_bytes(r['max_chunk'])}"
            bit_str = "SUCCESS (100%)" if r["bit_match"] else "FAILED"
            row = (
                f"{r['pattern']:<28} | "
                f"{r['wall_sec']:>7.2f}s | "
                f"{r['dedup_pct']:>6.2f}% | "
                f"{chunks_str:>16} | "
                f"{format_bytes(r['new_stored']):>14} | "
                f"{size_str:>22} | "
                f"{bit_str}"
            )
            print(row)
        print("=" * 115 + "\n")

    finally:
        print("[*] 격리 테스트 환경 정리 중...")
        shutil.rmtree(isolated_dir, ignore_errors=True)
        print("[+] 정리 완료.")


if __name__ == "__main__":
    run_fastcdc_benchmark()
