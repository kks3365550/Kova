# -*- coding: utf-8 -*-
"""
v2.9.21 Official Release Packaging, Signing & Inno Setup Build Tool
"""
import os
import sys
import zipfile
import hashlib
import subprocess

BASE_DIR = r"c:\Users\kksjmj\Desktop\ai\백업시스템"
DIST_DIR = os.path.join(BASE_DIR, "dist")
os.makedirs(DIST_DIR, exist_ok=True)

VERSION = "2.9.21"

def build_zip():
    zip_path = os.path.join(DIST_DIR, f"release_v{VERSION}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
        
    print(f"[*] Building release package: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 1. core
        for root, dirs, files in os.walk(os.path.join(BASE_DIR, "core")):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".pyc") or f.endswith(".pyo"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, BASE_DIR)
                zf.write(full, rel)
                
        # 2. web
        for root, dirs, files in os.walk(os.path.join(BASE_DIR, "web")):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".pyc") or f.endswith(".pyo"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, BASE_DIR)
                zf.write(full, rel)
                
        # 3. root scripts & public key
        root_files = [
            "run.py",
            "VERSION",
            "start_silent.vbs",
            "start_tray.vbs",
            "launch_dashboard.vbs",
            "stop_backup_system.bat",
            "2_백업시스템_실행.bat",
            "tray_app.py",
            "requirements.txt"
        ]
        for rf in root_files:
            p = os.path.join(BASE_DIR, rf)
            if os.path.exists(p):
                zf.write(p, rf)
                
        pub_key = os.path.join(BASE_DIR, "keys", "release_ed25519.pub")
        if os.path.exists(pub_key):
            zf.write(pub_key, "keys/release_ed25519.pub")
            
    print(f"[+] ZIP created successfully: {os.path.getsize(zip_path)} bytes")
    return zip_path

def sign_zip(zip_path):
    sig_path = f"{zip_path}.sig"
    print(f"[*] Signing ZIP with Ed25519...")
    
    # Check for private key in local backup or keys dir
    priv_key_candidates = [
        os.path.join(BASE_DIR, "backup", "v2.9.10", "keys", "release_ed25519.key"),
        os.path.join(BASE_DIR, "keys", "release_ed25519.key")
    ]
    priv_key_path = None
    for pk in priv_key_candidates:
        if os.path.exists(pk):
            priv_key_path = pk
            break
            
    if not priv_key_path:
        raise RuntimeError("Private key not found for signing!")
        
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    
    with open(priv_key_path, "rb") as f:
        priv_key = serialization.load_pem_private_key(f.read(), password=None)
        
    with open(zip_path, "rb") as f:
        data = f.read()
        
    sig = priv_key.sign(data)
    with open(sig_path, "w", encoding="utf-8") as f:
        f.write(sig.hex())
        
    print(f"[+] Signed successfully -> {sig_path} (Hex: {sig.hex()[:32]}...)")
    return sig_path

def build_installer():
    print(f"[*] Compiling Inno Setup installer for v{VERSION}...")
    iscc_candidates = [
        r"C:\Users\kksjmj\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe"
    ]
    iscc_path = None
    for p in iscc_candidates:
        if os.path.exists(p):
            iscc_path = p
            break
            
    if not iscc_path:
        print("[-] ISCC.exe not found in standard paths.")
        return None
        
    iss_file = os.path.join(BASE_DIR, "installer", "BackupSystem.iss")
    res = subprocess.run([iscc_path, iss_file], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[-] Inno Setup failed:\n{res.stderr}\n{res.stdout}")
        return None
        
    setup_exe = os.path.join(DIST_DIR, f"BackupSystem_Setup_v{VERSION}.exe")
    if os.path.exists(setup_exe):
        print(f"[+] Installer built successfully: {setup_exe} ({os.path.getsize(setup_exe)} bytes)")
        return setup_exe
    else:
        print(f"[-] Expected setup exe not found: {setup_exe}")
        return None

def generate_checksums():
    sums_file = os.path.join(DIST_DIR, "SHA256SUMS.txt")
    print(f"[*] Generating SHA-256 checksums...")
    
    targets = [
        f"BackupSystem_Setup_v{VERSION}.exe",
        f"release_v{VERSION}.zip",
        f"release_v{VERSION}.zip.sig"
    ]
    
    lines = []
    for t in targets:
        p = os.path.join(DIST_DIR, t)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest().upper()
            lines.append(f"{h} *{t}")
            print(f"  {h} *{t}")
            
    with open(sums_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        
    print(f"[+] Checksums saved to: {sums_file}")
    return sums_file

if __name__ == "__main__":
    zip_p = build_zip()
    sign_zip(zip_p)
    inst_p = build_installer()
    generate_checksums()
    print("\n[SUCCESS] PHASE 3 BUILD & SIGNING COMPLETE!")
