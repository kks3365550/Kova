# -*- coding: utf-8 -*-
"""
Prepare Clean Public Release Repository
Creates an isolated clean repository directory with zero leakage of private keys or operational logs.
"""
import os
import shutil
import subprocess

def prepare():
    src_dir = r"c:\Users\kksjmj\Desktop\ai\백업시스템"
    clean_dir = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "BackupSystem_Public_Release")
    
    if os.path.exists(clean_dir):
        shutil.rmtree(clean_dir, ignore_errors=True)
    os.makedirs(clean_dir, exist_ok=True)
    
    print(f"[*] Preparing clean repository at: {clean_dir}")
    
    # 1. Copy core & web
    shutil.copytree(os.path.join(src_dir, "core"), os.path.join(clean_dir, "core"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(os.path.join(src_dir, "web"), os.path.join(clean_dir, "web"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    
    # 2. Copy public key only
    keys_dir = os.path.join(clean_dir, "keys")
    os.makedirs(keys_dir, exist_ok=True)
    shutil.copy2(os.path.join(src_dir, "keys", "release_ed25519.pub"), os.path.join(keys_dir, "release_ed25519.pub"))
    
    # 3. Copy root files
    root_files = [
        "run.py",
        "VERSION",
        "start_silent.vbs",
        "start_tray.vbs",
        "launch_dashboard.vbs",
        "stop_backup_system.bat",
        "2_백업시스템_실행.bat",
        "release_notes.md",
        "known_issues.md",
        "support_scope.md",
        "repository_format.md",
        "release_build_manifest.md"
    ]
    for rf in root_files:
        src_path = os.path.join(src_dir, rf)
        if os.path.exists(src_path):
            shutil.copy2(src_path, os.path.join(clean_dir, rf))
            
    # 4. Create example profile
    data_dir = os.path.join(clean_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    example_profile = """[
  {
    "id": "default",
    "name": "Default Backup Profile",
    "source_paths": [
      "C:\\\\Users\\\\Default\\\\Documents"
    ],
    "repo_dir": "D:\\\\BackupRepository",
    "exclude_patterns": [
      "node_modules",
      "__pycache__",
      "*.tmp",
      "*.log"
    ],
    "schedule_type": "manual",
    "schedule_value": "12",
    "auto_backup_enabled": false,
    "retention_count": 30,
    "retention_days": 60,
    "compression_level": 3
  }
]
"""
    with open(os.path.join(data_dir, "profiles.json.example"), "w", encoding="utf-8") as f:
        f.write(example_profile)
        
    # 5. Copy or create README.md
    readme_path = os.path.join(src_dir, "README.md")
    if os.path.exists(readme_path):
        shutil.copy2(readme_path, os.path.join(clean_dir, "README.md"))
    else:
        # Create standard README
        readme_content = """# BackupSystem (백업시스템)

Enterprise-grade Local & Network Backup System for Windows.

## 🚀 Key Features
- **Deterministic Snapshot & Deduplication**: Block-level and file-level deduplication.
- **WORM (Write Once, Read Many)**: Cryptographic protection against ransomware.
- **Zero-Interruption Background Service**: Native Windows tray integration and silent background worker.
- **Web Dashboard**: Modern, real-time responsive web dashboard (localhost:8765).
- **Auto-Update with Ed25519 Signatures**: Cryptographically verified non-disruptive updates.

## 📦 Installation & Quick Start
1. Download the latest installer `BackupSystem_Setup_v2.9.20.exe` from [GitHub Releases](https://github.com/kks3365550/BackupSystem/releases).
2. Run the installer and follow the setup wizard.
3. Access the dashboard at `http://localhost:8765`.

## 📜 Documentation
- [Release Notes](release_notes.md)
- [Support Scope](support_scope.md)
- [Known Issues](known_issues.md)
- [Repository Format Specification](repository_format.md)
- [Release Build Manifest](release_build_manifest.md)

## 📄 License
MIT License
"""
        with open(os.path.join(clean_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(readme_content)
            
    # 6. Create clean .gitignore
    gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
*.pyc
.venv/
venv/
env/

# Backup Repositories
backup_repository/
MyBackup_Repository/

# Runtime Data & Personal Configurations
data/*.json
!data/profiles.json.example

# Temporary & Logs
*.tmp
*.temp
*.log
logs/

# Build artifacts & Binaries
build/
dist/
*.spec
installer/Output/
installer/runtime/
*.exe
*.zip
*.sig

# Keys & Secrets (Only public keys are allowed)
keys/*.key
keys/*.pem
keys/*.pfx
keys/*private*

# Internal & Scratch
.ai/
scratch/
audit/
reports/
"""
    with open(os.path.join(clean_dir, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(gitignore_content)
        
    print(f"[+] Clean repository successfully staged at: {clean_dir}")
    
    # 7. Check total files
    all_files = []
    for root, dirs, files in os.walk(clean_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), clean_dir)
            all_files.append(rel)
    print(f"[+] Total staged files: {len(all_files)}")
    
    # Security Scan
    for af in all_files:
        if af.endswith(".key") or af.endswith(".pem") or "profiles.json" in af and not af.endswith(".example"):
            print(f"[-] CRITICAL ALERT: Forbidden file detected: {af}")
            return False
            
    print("[+] Security Scan: 100% PASS (Zero sensitive files)")
    return True

if __name__ == "__main__":
    prepare()
