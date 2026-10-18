# -*- coding: utf-8 -*-
"""
core/clone_worker.py: 관리자 권한 격리 LBA 블록 스트리밍 복제 워커
- 비관리자 권한 웹서버 환경에서 WinError 5(볼륨 핸들 접근 거부)를 회피하기 위해 UAC 승격으로 단독 실행
- 실시간 진행률 및 완료 결과를 IPC 로그 파일에 스트리밍
"""
import os
import sys
import json
import time
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def main():
    parser = argparse.ArgumentParser(description="Elevated LBA Block Stream Worker")
    parser.add_argument("--src", default="C:", help="Source drive letter (e.g. C:)")
    parser.add_argument("--dst", required=True, help="Target drive letter (e.g. E:)")
    parser.add_argument("--shadow", default="", help="Optional VSS snapshot device path")
    parser.add_argument("--ipc-file", required=True, help="Shared IPC JSONL log file")
    parser.add_argument("--chunk-mb", type=int, default=16, help="Chunk size in MB (default: 16)")
    parser.add_argument("--queue-depth", type=int, default=12, help="Ring buffer queue depth (default: 12)")
    parser.add_argument("--gap-mb", type=int, default=16, help="Max coalesce gap in MB (default: 16)")
    parser.add_argument("--min-chunk-mb", type=int, default=4, help="Min chunk floor in MB (default: 4)")
    parser.add_argument("--two-stage-gb", type=int, default=200, help="Two-stage downshift threshold in GB (default: 200)")
    args = parser.parse_args()

    ipc_path = args.ipc_file

    def emit(data: dict):
        try:
            with open(ipc_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass

    try:
        from core.block_stream import stream_volume_to_volume

        last_emit = 0.0

        def on_progress(p: dict):
            nonlocal last_emit
            now = time.time()
            if now - last_emit >= 0.5 or p.get("percent", 0.0) >= 100.0:
                emit({"type": "progress", "data": p})
                last_emit = now

        emit({"type": "status", "message": "워커 프로세스 시작 완료 (관리자 권한 획득)"})

        shadow_arg = args.shadow if (args.shadow and args.shadow.strip()) else None

        res = stream_volume_to_volume(
            src_drive=args.src,
            dst_drive=args.dst,
            progress_callback=on_progress,
            chunk_bytes=args.chunk_mb * 1024 * 1024,
            queue_depth=args.queue_depth,
            src_shadow_path=shadow_arg,
            max_coalesce_gap_bytes=args.gap_mb * 1024 * 1024,
            min_chunk_bytes=args.min_chunk_mb * 1024 * 1024,
            two_stage_threshold_bytes=args.two_stage_gb * 1024 * 1024 * 1024
        )

        emit({"type": "done", "result": res})
        sys.exit(0)

    except Exception as exc:
        emit({"type": "error", "error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
