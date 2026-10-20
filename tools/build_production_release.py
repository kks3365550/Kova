import os
import sys
import zipfile
import subprocess
import hashlib
import shutil

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)
DIST_DIR = os.path.join(BASE_DIR, "dist")
os.makedirs(DIST_DIR, exist_ok=True)

with open(os.path.join(BASE_DIR, "VERSION"), "r", encoding="utf-8") as f:
    VERSION = f.read().strip()

print("=" * 60)
print(f"[BUILD] CAS BackupSystem v{VERSION} Production Build & Sign")
print("=" * 60)

# 1. Build release_vX.Y.Z.zip
zip_name = f"release_v{VERSION}.zip"
zip_path = os.path.join(DIST_DIR, zip_name)
if os.path.exists(zip_path):
    os.remove(zip_path)

print(f"[*] Packaging {zip_name}...")
exclude_dirs = {".git", ".venv", ".ai", "scratch", "dist", "logs", "__pycache__", "backup_repository", "runtime", "TEMP"}
exclude_files = {".env", "profiles.json", "auth_config.json", "app_settings.json", "metadata.db"}

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for file in files:
            if file in exclude_files or file.endswith((".pyc", ".pyo", ".tmp")):
                continue
            if root == BASE_DIR and file in ("keys/release_ed25519.key", "keys/private.key"):
                continue
            abs_p = os.path.join(root, file)
            rel_p = os.path.relpath(abs_p, BASE_DIR)
            if "keys" in rel_p and "key" in rel_p and not rel_p.endswith(".pub"):
                continue
            zf.write(abs_p, rel_p)

zip_size = os.path.getsize(zip_path)
print(f"[+] Created {zip_name} ({zip_size:,} bytes)")

# 2. Sign release_vX.Y.Z.zip with Ed25519
from core.crypto_sign import sign_bytes_ed25519
key_path = os.path.join(BASE_DIR, "keys", "release_ed25519_v2.key")
if not os.path.exists(key_path):
    key_path = os.path.join(BASE_DIR, "keys", "release_ed25519.key")
sig_path = f"{zip_path}.sig"
assert os.path.exists(key_path), "Release private key missing!"
with open(zip_path, "rb") as zf_in:
    raw_zip_bytes = zf_in.read()
sig_hex = sign_bytes_ed25519(raw_zip_bytes, key_path)
with open(sig_path, "w", encoding="utf-8") as sf_out:
    sf_out.write(sig_hex)
print(f"[+] Ed25519 Signature generated: {sig_path} ({sig_hex[:16]}...)")

# 3. Compile Inno Setup Installer
iscc_candidates = [
    r"C:\Users\kksjmj\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    "ISCC.exe"
]
iscc_bin = next((p for p in iscc_candidates if os.path.exists(p)), "ISCC.exe")

iss_file = os.path.join(BASE_DIR, "installer", "BackupSystem.iss")
print(f"[*] Compiling Inno Setup installer...")
cmd = [iscc_bin, f"/DMyAppVersion={VERSION}", iss_file]
res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", cwd=BASE_DIR)
if res.returncode != 0:
    print(f"[-] ISCC compilation failed:\n{res.stderr}\n{res.stdout}")
    sys.exit(1)

setup_exe = os.path.join(DIST_DIR, f"BackupSystem_Setup_v{VERSION}.exe")
assert os.path.exists(setup_exe), f"Installer not found at {setup_exe}"
print(f"[+] Inno Setup compiled successfully: {setup_exe} ({os.path.getsize(setup_exe):,} bytes)")

# 4. Generate SHA256SUMS.txt
print(f"[*] Generating SHA256SUMS.txt...")
sha_file = os.path.join(DIST_DIR, "SHA256SUMS.txt")
artifacts = [setup_exe, zip_path, sig_path]
lines = []
for art in artifacts:
    h = hashlib.sha256()
    with open(art, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    digest = h.hexdigest().upper()
    bname = os.path.basename(art)
    lines.append(f"{digest} *{bname}")
    print(f"    - {bname}: {digest}")

with open(sha_file, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"[+] SHA256SUMS.txt generated at {sha_file}")

# 5. Sync to D:\백업시스템_설치용
install_dir = r"D:\백업시스템_설치용"
if os.path.exists(install_dir):
    print(f"[*] Syncing release artifacts to {install_dir}...")
    for art in artifacts + [sha_file]:
        dst = os.path.join(install_dir, os.path.basename(art))
        shutil.copy2(art, dst)
    print(f"[+] Synchronized to {install_dir} 100% complete!")

print("=" * 60)
print(f"[BUILD COMPLETE] v{VERSION} ALL PRODUCTION ASSETS READY!")
print("=" * 60)
