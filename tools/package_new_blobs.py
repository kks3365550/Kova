import os
import sys
import time
import tarfile

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def main():
    blobs_dir = r"D:\MyBackup_Repository\blobs"
    tar_path = r"D:\new_blobs.tar"
    two_hours_ago = time.time() - 7200

    print(f"[*] Packaging recent blobs from {blobs_dir} into {tar_path}...")
    start_time = time.time()

    count = 0
    total_bytes = 0
    with tarfile.open(tar_path, "w") as tar:
        for root, dirs, files in os.walk(blobs_dir):
            for f in files:
                if f.endswith(".blob"):
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                        if st.st_mtime >= two_hours_ago:
                            arcname = os.path.relpath(fp, blobs_dir)
                            tar.add(fp, arcname=arcname)
                            count += 1
                            total_bytes += st.st_size
                            if count % 10000 == 0:
                                print(f"   Packed {count:,} blobs ({total_bytes / (1024**2):.1f} MB)...")
                    except Exception:
                        pass

    elapsed = time.time() - start_time
    tar_mb = os.path.getsize(tar_path) / (1024**2)
    print(f"✅ Packaging complete: {count:,} blobs, {tar_mb:.2f} MB in {elapsed:.2f}s")

if __name__ == "__main__":
    main()
