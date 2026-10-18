import os
import time
import shutil
import stat
from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.storage import BlobStorage

def safe_rmtree(path):
    if not os.path.exists(path):
        return
    def _handle_readonly(func, p, excinfo):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_handle_readonly)

def test_full_system_flow():
    print("\n=======================================================")
    print(" [1] Backup & Snapshot Full Lifecycle Test Start")
    print("=======================================================")

    test_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_sandbox"))
    safe_rmtree(test_root)

    src_dir = os.path.join(test_root, "source")
    repo_dir = os.path.join(test_root, "backup_repo")
    restore_v1_dir = os.path.join(test_root, "restored_v1")
    restore_v2_dir = os.path.join(test_root, "restored_v2")

    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(os.path.join(src_dir, "configs"), exist_ok=True)
    os.makedirs(os.path.join(src_dir, "data"), exist_ok=True)

    # 1. Create initial files (V1)
    file1_content = "Hello World Version 1\nServer Configuration Data\n"
    file2_content = "Database Schema v1.0\nCREATE TABLE users(id INT);\n"
    file3_bin = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024  # dummy png

    with open(os.path.join(src_dir, "configs", "server.conf"), "w", encoding="utf-8") as f:
        f.write(file1_content)
    with open(os.path.join(src_dir, "data", "schema.sql"), "w", encoding="utf-8") as f:
        f.write(file2_content)
    with open(os.path.join(src_dir, "data", "logo.png"), "wb") as f:
        f.write(file3_bin)

    print("[Step 1] Initial files created (3 files)")

    # 2. Run First Snapshot (Full Backup)
    print("\n[Step 2] Running First Full Backup...")
    snap1 = SnapshotEngine.create_snapshot(
        repo_dir=repo_dir,
        sources=[src_dir],
        profile_id="prof_test",
        profile_name="Test Backup"
    )

    print(f" -> Snapshot 1 ID: {snap1['id']}")
    print(f" -> Backup Type: {snap1['backup_type']}")
    print(f" -> Summary: {snap1['summary']}")
    assert snap1['backup_type'] == 'full'
    assert snap1['summary']['total_files'] == 3
    assert snap1['summary']['new_files'] == 3

    # 3. Run No-op Incremental Backup (No changes)
    print("\n[Step 3] Running 2nd backup without changes (Zero Delta test)...")
    snap1_noop = SnapshotEngine.create_snapshot(
        repo_dir=repo_dir,
        sources=[src_dir],
        profile_id="prof_test",
        profile_name="Test Backup"
    )
    print(f" -> No-op snapshot: new={snap1_noop['summary']['new_files']}, unmodified={snap1_noop['summary']['unmodified_files']}, new_bytes={snap1_noop['summary']['new_stored_bytes']}B")
    assert snap1_noop['summary']['new_files'] == 0
    assert snap1_noop['summary']['unmodified_files'] == 3
    assert snap1_noop['summary']['new_stored_bytes'] == 0

    # 4. Modify 1 file, Add 1 file, Delete 1 file (V2)
    time.sleep(1.0)
    print("\n[Step 4] Modifying / Adding / Deleting files...")
    # Modify server.conf
    with open(os.path.join(src_dir, "configs", "server.conf"), "w", encoding="utf-8") as f:
        f.write("Hello World Version 2 - UPDATED SERVER CONFIG!\nMaxWorkers = 16\n")
    # Add new file
    with open(os.path.join(src_dir, "data", "report.txt"), "w", encoding="utf-8") as f:
        f.write("Monthly System Report 2026-08\nStatus: All Systems Operational\n")
    # Delete logo.png
    os.remove(os.path.join(src_dir, "data", "logo.png"))

    # 5. Run Incremental Backup (V2)
    print("\n[Step 5] Running Incremental Backup (V2)...")
    snap2 = SnapshotEngine.create_snapshot(
        repo_dir=repo_dir,
        sources=[src_dir],
        profile_id="prof_test",
        profile_name="Test Backup"
    )
    print(f" -> Snapshot 2 ID: {snap2['id']}")
    print(f" -> Backup Type: {snap2['backup_type']}")
    print(f" -> Base Snapshot: {snap2['base_snapshot_id']}")
    print(f" -> New: {snap2['summary']['new_files']}, Modified: {snap2['summary']['modified_files']}, Unmodified: {snap2['summary']['unmodified_files']}, Deleted: {snap2['summary']['deleted_files']}")

    assert snap2['backup_type'] == 'incremental'
    assert snap2['summary']['new_files'] == 1
    assert snap2['summary']['modified_files'] == 1
    assert snap2['summary']['unmodified_files'] == 1
    assert snap2['summary']['deleted_files'] == 1

    # 6. Verify Integrity
    print("\n[Step 6] Verifying Snapshot SHA-256 integrity...")
    v1_check = RestoreEngine.verify_snapshot_integrity(repo_dir, snap1['id'])
    v2_check = RestoreEngine.verify_snapshot_integrity(repo_dir, snap2['id'])
    assert v1_check['is_valid']
    assert v2_check['is_valid']
    print(" -> Both Snapshots passed SHA-256 integrity verification 100%!")

    # 7. Restore Snapshot 1 (Time Machine to V1)
    print("\n[Step 7] Restoring Snapshot 1 (V1 Point-in-time)...")
    res1 = RestoreEngine.restore_snapshot(repo_dir, snap1['id'], restore_v1_dir)
    print(f" -> Restored: {res1['restored_files']} files")
    
    with open(os.path.join(restore_v1_dir, "configs", "server.conf"), "r", encoding="utf-8") as f:
        v1_conf = f.read()
    assert v1_conf == file1_content
    assert os.path.exists(os.path.join(restore_v1_dir, "data", "logo.png"))
    assert not os.path.exists(os.path.join(restore_v1_dir, "data", "report.txt"))
    print(" -> Snapshot 1 (V1) restored perfectly!")

    # 8. Restore Snapshot 2 (Time Machine to V2)
    print("\n[Step 8] Restoring Snapshot 2 (V2 Point-in-time)...")
    res2 = RestoreEngine.restore_snapshot(repo_dir, snap2['id'], restore_v2_dir)
    print(f" -> Restored: {res2['restored_files']} files")
    
    with open(os.path.join(restore_v2_dir, "configs", "server.conf"), "r", encoding="utf-8") as f:
        v2_conf = f.read()
    assert "Version 2 - UPDATED" in v2_conf
    assert os.path.exists(os.path.join(restore_v2_dir, "data", "report.txt"))
    assert not os.path.exists(os.path.join(restore_v2_dir, "data", "logo.png"))
    print(" -> Snapshot 2 (V2) restored perfectly!")

    # 9. Test Storage Stats & Deduplication
    print("\n[Step 9] Checking Deduplication & Storage Stats...")
    storage = BlobStorage(repo_dir)
    stats = storage.get_storage_stats()
    print(f" -> Storage stats: {stats}")
    assert stats['total_snapshots'] >= 3
    assert stats['dedup_saved_bytes'] > 0

    # 10. Test WORM Immutability Protection (Ransomware Defense)
    print("\n[Step 10] Testing WORM Immutability Protection (Ransomware Defense)...")
    blobs_found = []
    for root, dirs, files in os.walk(storage.blobs_dir):
        for f in files:
            blobs_found.append(os.path.join(root, f))
    assert len(blobs_found) > 0, "No blob files found"

    sample_blob = blobs_found[0]
    # Verify blob file is read-only
    blob_mode = os.stat(sample_blob).st_mode
    assert not (blob_mode & stat.S_IWRITE), f"Blob file {sample_blob} should be read-only"

    # Verify tampering attempt throws PermissionError
    tamper_failed = False
    try:
        with open(sample_blob, "ab") as f:
            f.write(b"RANSOMWARE_ENCRYPTED_DATA")
    except PermissionError:
        tamper_failed = True
    assert tamper_failed, "Tampering with blob should have been blocked by OS read-only lock!"

    # Verify snapshot JSON is also read-only
    snap_json = os.path.join(storage.snapshots_dir, f"{snap2['id']}.json")
    snap_mode = os.stat(snap_json).st_mode
    assert not (snap_mode & stat.S_IWRITE), f"Snapshot JSON {snap_json} should be read-only"

    snap_tamper_failed = False
    try:
        with open(snap_json, "a") as f:
            f.write("tamper")
    except PermissionError:
        snap_tamper_failed = True
    assert snap_tamper_failed, "Tampering with snapshot JSON should have been blocked!"
    print(" -> WORM lock successfully prevented unauthorized modification of blobs and snapshot manifests!")

    # 11. Test Fail-Closed Low-Disk Safeguard (Abort backup under low disk space without pruning)
    print("\n[Step 11] Testing Fail-Closed Low-Disk Safeguard...")
    initial_snaps = SnapshotEngine.list_snapshots(repo_dir)
    assert len(initial_snaps) >= 3

    # Under Fail-Closed policy, requesting more free GB than exists must raise InsufficientDiskSpaceError
    # and NEVER delete/prune historical snapshots.
    from core.storage import InsufficientDiskSpaceError
    fail_closed_triggered = False
    try:
        SnapshotEngine.ensure_disk_space(repo_dir, min_free_gb=999999.0)
    except InsufficientDiskSpaceError:
        fail_closed_triggered = True

    assert fail_closed_triggered, "Low disk safeguard should have raised InsufficientDiskSpaceError (Fail-Closed)!"
    remaining_snaps = SnapshotEngine.list_snapshots(repo_dir)
    assert len(remaining_snaps) == len(initial_snaps), "Fail-Closed policy violated: historical snapshots were unexpectedly pruned!"
    print(" -> Fail-Closed Low-Disk Safeguard correctly preserved 100% of historical snapshots and safely aborted!")

    # Cleanup test sandbox
    safe_rmtree(test_root)
    print("\n=======================================================")
    print(" [SUCCESS] All Integration Tests Passed Successfully!")
    print("=======================================================\n")

if __name__ == "__main__":
    test_full_system_flow()

