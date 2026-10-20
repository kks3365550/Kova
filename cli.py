import argparse
import sys
import os
from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.config import ConfigManager
from core.storage import BlobStorage

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def cmd_backup(args):
    profile = None
    if args.profile:
        profile = ConfigManager.get_profile(args.profile)
        if not profile:
            print(f"[ERROR] Profile '{args.profile}' not found.", file=sys.stderr)
            sys.exit(1)
    else:
        profiles = ConfigManager.get_profiles()
        profile = profiles[0] if profiles else None

    repo_dir = args.repo or (profile.get("repo_dir") if profile else "./backup_repository")
    sources = [args.source] if args.source else (profile.get("sources") if profile else [os.getcwd()])
    exclude = profile.get("exclude_patterns", []) if profile else []
    compress = profile.get("compression_level", 6) if profile else 6

    print("==================================================")
    print(" [BACKUP START]")
    print(f" Profile: {profile.get('name') if profile else 'Custom'}")
    print(f" Sources: {sources}")
    print(f" Repository: {repo_dir}")
    print("==================================================")

    def on_progress(p):
        print(f"\rProgress: {p.get('percent', 0)}% | File {p.get('processed_files')}/{p.get('total_files')} | {p.get('current_file')[:40]:<40}", end="", flush=True)

    manifest = SnapshotEngine.create_snapshot(
        repo_dir=repo_dir,
        sources=sources,
        profile_id=profile.get("id", "cli") if profile else "cli",
        profile_name=profile.get("name", "CLI Backup") if profile else "CLI Backup",
        exclude_patterns=exclude,
        compress_level=compress,
        progress_callback=on_progress
    )
    print()

    summary = manifest.get("summary", {})
    print("==================================================")
    print(f" [BACKUP FINISHED] Snapshot ID: {manifest['id']}")
    print(f" Type: {manifest['backup_type'].upper()}")
    print(f" Total files: {summary.get('total_files')} ({format_bytes(summary.get('total_bytes', 0))})")
    print(f" New: {summary.get('new_files')}, Modified: {summary.get('modified_files')}, Unmodified: {summary.get('unmodified_files')}")
    print(f" Saved via Dedup: {format_bytes(summary.get('dedup_saved_bytes', 0))}")
    print(f" Duration: {summary.get('duration_seconds')}s")
    print("==================================================")

def cmd_list(args):
    repo_dir = args.repo or "./backup_repository"
    snapshots = SnapshotEngine.list_snapshots(repo_dir)
    if not snapshots:
        print(f"No snapshots found in {repo_dir}")
        return

    print(f"\nFound {len(snapshots)} snapshots in {repo_dir}:\n")
    print(f"{'Snapshot ID':<26} | {'Created At':<20} | {'Type':<12} | {'Files':<8} | {'Total Size':<12} | {'Profile'}")
    print("-" * 105)
    for s in snapshots:
        sum_data = s.get("summary", {})
        total_b = sum_data.get("total_bytes", 0)
        print(f"{s['id']:<26} | {s.get('iso_time', '')[:19]:<20} | {s.get('backup_type', 'full'):<12} | {sum_data.get('total_files', 0):<8} | {format_bytes(total_b):<12} | {s.get('profile_name', '')}")
    print()

def cmd_restore(args):
    repo_dir = args.repo or "./backup_repository"
    target = os.path.abspath(args.target)
    snap_id = args.snapshot_id
    select = [args.select] if args.select else None

    print(f"Restoring snapshot '{snap_id}' to '{target}'...")
    def on_progress(p):
        print(f"\rRestoring: {p.get('percent', 0)}% | {p.get('restored_files')}/{p.get('total_files')} files", end="", flush=True)

    result = RestoreEngine.restore_snapshot(
        repo_dir=repo_dir,
        snapshot_id=snap_id,
        target_dir=target,
        selected_rel_paths=select,
        overwrite=not args.no_overwrite,
        progress_callback=on_progress
    )
    print()
    print(f"Restore completed! Restored: {result['restored_files']} files ({format_bytes(result['restored_bytes'])}), Skipped: {result['skipped_files']}, Failed: {len(result['failed_files'])} in {result['duration_seconds']}s")

def cmd_verify(args):
    repo_dir = args.repo or "./backup_repository"
    snap_id = args.snapshot_id
    print(f"Verifying integrity of snapshot '{snap_id}'...")
    res = RestoreEngine.verify_snapshot_integrity(repo_dir, snap_id)
    if res["is_valid"]:
        print(f"[SUCCESS] All {res['total_files']} files in snapshot are valid and uncorrupted!")
    else:
        print(f"[FAILED] Missing blobs: {len(res['missing_blobs'])}, Corrupted blobs: {len(res['corrupted_blobs'])}")

def cmd_stats(args):
    repo_dir = args.repo or "./backup_repository"
    storage = BlobStorage(repo_dir)
    stats = storage.get_storage_stats()
    print("==================================================")
    print(f" Repository Statistics ({repo_dir})")
    print("==================================================")
    print(f" Total Snapshots:        {stats['total_snapshots']}")
    print(f" Unique Blobs Stored:    {stats['total_blobs']}")
    print(f" Actual Stored Size:     {format_bytes(stats['stored_bytes'])}")
    print(f" Total Logical Size:     {format_bytes(stats['logical_bytes'])}")
    print(f" Deduplication Saved:    {format_bytes(stats['dedup_saved_bytes'])} ({stats['savings_percentage']}%)")
    print("==================================================")

def main():
    parser = argparse.ArgumentParser(description="System & Server Incremental Backup CLI Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # backup
    p_backup = subparsers.add_parser("backup", help="Run a backup snapshot")
    p_backup.add_argument("--profile", help="Profile ID to run")
    p_backup.add_argument("--repo", help="Target backup repository directory")
    p_backup.add_argument("--source", help="Source directory to backup")
    p_backup.set_defaults(func=cmd_backup)

    # list
    p_list = subparsers.add_parser("list-snapshots", help="List all backup snapshots")
    p_list.add_argument("--repo", help="Backup repository directory")
    p_list.set_defaults(func=cmd_list)

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore a snapshot")
    p_restore.add_argument("--snapshot-id", required=True, help="Snapshot ID to restore")
    p_restore.add_argument("--target", required=True, help="Destination directory to restore into")
    p_restore.add_argument("--repo", help="Backup repository directory")
    p_restore.add_argument("--select", help="Specific relative file or folder path to restore")
    p_restore.add_argument("--no-overwrite", action="store_true", help="Do not overwrite existing files")
    p_restore.set_defaults(func=cmd_restore)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify snapshot integrity")
    p_verify.add_argument("--snapshot-id", required=True, help="Snapshot ID to verify")
    p_verify.add_argument("--repo", help="Backup repository directory")
    p_verify.set_defaults(func=cmd_verify)

    # stats
    p_stats = subparsers.add_parser("stats", help="Show repository deduplication and storage statistics")
    p_stats.add_argument("--repo", help="Backup repository directory")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
