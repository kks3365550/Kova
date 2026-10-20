# -*- coding: utf-8 -*-
"""
tests/benchmarks/prototypes/pack_consistency/stress_crash_simulation.py
Track 2-10: Stress & Chaos Injection for Pack Container Crash Consistency
Simulates random process kills and torn writes during high-throughput ingest.
"""

import os
import sys
import time
import shutil
import struct
import random
import hashlib
import tempfile
from pathlib import Path

from tests.benchmarks.prototypes.pack_consistency.pack_format import (
    PackContainerWriter,
    PackContainerReader,
    PackRecoveryEngine,
    PackConsistencyError,
    HEADER_FORMAT,
    HEADER_SIZE,
    FOOTER_SIZE
)



def run_chaos_stress_test(iterations: int = 30):
    print("=" * 70)
    print(f"[*] Starting Pack Container Crash Consistency Chaos Stress Test ({iterations} rounds)")
    print("=" * 70)

    tmp_dir = tempfile.mkdtemp(prefix="pack_chaos_")
    try:
        pack_path = Path(tmp_dir) / "chaos.pack"
        idx_path = Path(tmp_dir) / "chaos.idx"

        total_injected_crashes = 0
        total_recovered_chunks = 0
        start_time = time.time()

        for round_idx in range(1, iterations + 1):
            chunk_count = random.randint(30, 80)
            chunk_size = random.randint(4096, 32768)
            crash_point = random.randint(5, chunk_count - 5)
            cut_mode = random.choice(["header_cut", "payload_cut", "uncommitted_clean"])

            # 1. Generate chunks
            chunks = []
            for i in range(chunk_count):
                payload = os.urandom(chunk_size)
                h = hashlib.sha256(payload).hexdigest()
                chunks.append((h, payload))

            # 2. Write up to crash_point
            writer = PackContainerWriter(pack_path, idx_path)
            for i in range(crash_point):
                writer.write_chunk(chunks[i][0], chunks[i][1])

            # 3. Simulate Chaos / Sudden Interruption
            if cut_mode == "header_cut":
                # Write partial header (10 bytes) then crash
                writer.pack_file.write(b"CHNK\x00\x00\x00\x01\x12\x34")
                writer.pack_file.flush()
                writer.close()
            elif cut_mode == "payload_cut":
                # Write full header + 50 bytes partial payload, but interrupt before footer/CRC
                next_h, next_p = chunks[crash_point]
                header = struct.pack(HEADER_FORMAT, b"CHNK", crash_point, len(next_p), bytes.fromhex(next_h))
                writer.pack_file.write(header)
                writer.pack_file.write(next_p[:50]) # Incomplete payload
                writer.pack_file.flush()
                writer.close()
            else:
                # Uncommitted clean (writer died before commit_index)
                writer.close()


            total_injected_crashes += 1

            # 4. Trigger Recovery Engine
            rebuilt_count, valid_offset = PackRecoveryEngine.rebuild_index_from_pack(
                pack_path, idx_path
            )
            PackRecoveryEngine.truncate_to_valid_offset(pack_path, valid_offset)

            # 5. Verify Invariant: All chunks before crash_point must be 100% bit-exact!
            reader = PackContainerReader(pack_path, idx_path)
            if len(reader.index) != crash_point:
                raise RuntimeError(
                    f"[FAIL] Round {round_idx}: Expected {crash_point} chunks, got {len(reader.index)}"
                )

            for i in range(crash_point):
                h, expected_payload = chunks[i]
                read_payload = reader.read_chunk(h, verify=True)
                if read_payload != expected_payload:
                    raise RuntimeError(f"[FAIL] Round {round_idx}: Payload mismatch for chunk {h[:12]}")

            total_recovered_chunks += crash_point

            # Clean for next round
            if pack_path.exists():
                pack_path.unlink()
            if idx_path.exists():
                idx_path.unlink()

            if round_idx % 5 == 0 or round_idx == iterations:
                print(f"[-] Round {round_idx:02d}/{iterations:02d} PASSED | Recovered {crash_point} chunks | Chaos Mode: {cut_mode}")

        elapsed = time.time() - start_time
        print("=" * 70)
        print(f"[OK] Chaos Stress Test PASSED 100%!")
        print(f" - Total Crashes Injected: {total_injected_crashes}")
        print(f" - Total Chunks Recovered Bit-for-Bit: {total_recovered_chunks}")
        print(f" - Elapsed Time: {elapsed:.2f}s")
        print("=" * 70)
        return True

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    success = run_chaos_stress_test(30)
    sys.exit(0 if success else 1)
