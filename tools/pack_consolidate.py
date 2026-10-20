# -*- coding: utf-8 -*-
"""
tools/pack_consolidate.py
CLI tool to run Pack Consolidation and Delayed GC in background or on demand.
Usage:
    python tools/pack_consolidate.py --repo "D:/MyBackup_Repository" --dry-run
    python tools/pack_consolidate.py --repo "D:/MyBackup_Repository" --run
    python tools/pack_consolidate.py --repo "D:/MyBackup_Repository" --gc-only --force-gc
"""

import sys
import argparse
import os

# Add root dir to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.pack_consolidator import PackConsolidator


def main():
    parser = argparse.ArgumentParser(description="Backup System Pack Consolidation & Delayed GC Compactor")
    parser.add_argument("--repo", type=str, default="D:/MyBackup_Repository", help="Target backup repository path")
    parser.add_argument("--run", action="store_true", help="Execute consolidation and quarantine")
    parser.add_argument("--dry-run", action="store_true", help="Scan candidate blobs without modifying disk")
    parser.add_argument("--gc-only", action="store_true", help="Only run delayed GC on quarantined blobs")
    parser.add_argument("--force-gc", action="store_true", help="Force delete all quarantined blobs immediately")
    parser.add_argument("--max-blobs", type=int, default=5000, help="Maximum number of blobs to pack per run")

    args = parser.parse_args()

    if not os.path.exists(args.repo):
        print(f"[ERROR] Repository directory not found: {args.repo}")
        sys.exit(1)

    consolidator = PackConsolidator(args.repo, max_blobs_per_run=args.max_blobs)

    if args.gc_only:
        print(f"[*] Running Delayed GC on {args.repo} (force_all={args.force_gc})...")
        deleted, freed = consolidator.run_delayed_gc(force_all=args.force_gc)
        print(f"[OK] Delayed GC complete: {deleted} files deleted, {freed:,} bytes freed.")
        return

    if args.dry_run or (not args.run):
        print(f"[*] Running Consolidation Scan (Dry-Run) on {args.repo}...")
        res = consolidator.consolidate(dry_run=True)
        print(f"[Result] Status: {res['status']}")
        print(f"         Candidates: {res['consolidated_count']} blobs ({res['consolidated_bytes']:,} bytes)")
        if not args.run:
            print("[Note] To execute, add --run flag.")
        return

    print(f"[*] Starting Pack Consolidation on {args.repo} (max {args.max_blobs} blobs)...")
    res = consolidator.consolidate(dry_run=False)
    print(f"[OK] Consolidation finished: {res['status']}")
    if res.get("pack_id"):
        print(f"     Pack ID: {res['pack_id']}")
        print(f"     Consolidated: {res['consolidated_count']} blobs")
        print(f"     Quarantined: {res['quarantined_count']} blobs")
        print(f"     Total Bytes: {res['consolidated_bytes']:,} bytes")


if __name__ == "__main__":
    main()
