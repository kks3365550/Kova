# -*- coding: utf-8 -*-
"""
Scan all 19 projects under Desktop\ai to determine true type and entrypoints.
"""
import os
import sys
import json

BASE = r"c:\Users\kksjmj\Desktop\ai"

def scan():
    items = sorted(os.listdir(BASE))
    catalog = {}
    for item in items:
        full_path = os.path.join(BASE, item)
        if not os.path.isdir(full_path):
            continue
        
        all_files = []
        for root, dirs, files in os.walk(full_path):
            for f in files:
                all_files.append(os.path.relpath(os.path.join(root, f), full_path))
        
        entrypoints = []
        candidate_names = [
            "main.py", "app.py", "run.py", "server.py", "index.py", "cli.py",
            "package.json", "index.html", "requirements.txt", "Pipfile", "pyproject.toml"
        ]
        for c in candidate_names:
            if os.path.exists(os.path.join(full_path, c)):
                entrypoints.append(c)
        
        # Subdirectory entrypoints
        for f in all_files:
            if f.endswith(".py") and f.count(os.sep) <= 1:
                base_f = os.path.basename(f)
                if base_f in ("app.py", "main.py", "run.py", "server.py") and f not in entrypoints:
                    entrypoints.append(f)

        # Classification
        if any(f.endswith(".py") for f in all_files):
            proj_type = "PYTHON_APP"
        elif any(f.endswith((".js", ".ts", ".html")) for f in all_files):
            proj_type = "WEB_FRONTEND"
        else:
            proj_type = "DATA_DOC_STORAGE"

        catalog[item] = {
            "type": proj_type,
            "total_files": len(all_files),
            "entrypoints": entrypoints,
            "sample_files": all_files[:5]
        }
    
    out_file = os.path.join(r"c:\Users\kksjmj\Desktop\ai\백업시스템\data\dr_result", "project_catalog.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    
    print(f"[*] Cataloged {len(catalog)} projects. Saved to {out_file}")
    for name, info in catalog.items():
        print(f"[{info['type']:16s}] {name:25s} | Files: {info['total_files']:4d} | Entries: {info['entrypoints']}")

if __name__ == "__main__":
    scan()
