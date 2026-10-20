import os
import sys
import time
import threading
from typing import List, Dict, Any

_cached_apps = None
_cached_apps_time = 0
_CACHE_TTL = 300.0  # 5 minutes cache
_scanner_lock = threading.Lock()

def _scan_installed_applications_impl() -> List[Dict[str, Any]]:

    apps = []
    seen_names = set()

    if sys.platform.startswith("win"):
        import winreg

        registry_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        ]

        ignore_keywords = [
            "KB", "Security Update", "Update for", "Hotfix",
            "Microsoft Visual C++", "Windows Software Development Kit",
            "Microsoft Windows Desktop Runtime", "DirectX", "Vulkan",
            "AMD GPIO", "AMD Interface", "AMD MicroPEP", "AMD PPM", "AMD PSP",
            "AMD Chipset", "Branding64", "Office 16 Click-to-Run",
            "Microsoft .NET", "Microsoft Windows Application"
        ]

        import re

        # Helper to extract executable/dir from command line
        def extract_dir_from_cmd(cmd_str: str) -> str:
            if not cmd_str or "msiexec" in cmd_str.lower():
                return ""
            m = re.search(r'"([^"]+\.exe)"', cmd_str, re.IGNORECASE)
            if not m:
                m = re.search(r'([a-zA-Z]:\\[^\s]+\.exe)', cmd_str, re.IGNORECASE)
            if m:
                exe_path = m.group(1)
                if os.path.exists(exe_path):
                    parent = os.path.dirname(exe_path)
                    if os.path.basename(parent).lower() in ["bin", "cmd"]:
                        grand = os.path.dirname(parent)
                        return grand if os.path.exists(grand) else parent
                    return parent
            return ""

        # Pre-scan candidate directories up to 2 levels deep into an in-memory map
        candidate_dirs = {}
        roaming_base = os.path.expanduser(r"~\AppData\Roaming")
        roaming_dirs = set()

        scan_roots = [
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            os.path.expanduser(r"~\AppData\Local\Programs"),
            os.path.expanduser(r"~\AppData\Local"),
            roaming_base
        ]
        user_home = os.path.expanduser("~")
        for tool_dir in [".cargo", ".rustup", ".vscode", ".docker"]:
            tp = os.path.join(user_home, tool_dir)
            if os.path.exists(tp):
                candidate_dirs[tool_dir.lower()] = tp
                candidate_dirs[tool_dir.strip(".").lower()] = tp

        for pf in scan_roots:
            if os.path.exists(pf):
                try:
                    with os.scandir(pf) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                n_lower = entry.name.lower()
                                if pf == roaming_base:
                                    roaming_dirs.add(n_lower)
                                if n_lower not in candidate_dirs:
                                    candidate_dirs[n_lower] = entry.path
                                # Depth 2 subdirectories (e.g. Futuremark/SystemInfo, Samsung/GalaxyConnect...)
                                try:
                                    with os.scandir(entry.path) as sub_it:
                                        for sub_entry in sub_it:
                                            if sub_entry.is_dir(follow_symlinks=False):
                                                sub_name = sub_entry.name.lower()
                                                if sub_name not in candidate_dirs:
                                                    candidate_dirs[sub_name] = sub_entry.path
                                                combined = f"{n_lower}{sub_name}"
                                                candidate_dirs[combined] = sub_entry.path
                                except OSError:
                                    pass
                except OSError:
                    pass

        for hkey, subkey in registry_paths:
            try:
                with winreg.OpenKey(hkey, subkey) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        try:
                            key_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, key_name) as app_key:
                                try:
                                    display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                    display_name = str(display_name).strip()
                                    if not display_name or display_name.lower() in seen_names:
                                        continue

                                    if any(ign.lower() in display_name.lower() for ign in ignore_keywords):
                                        continue

                                    loc = ""
                                    # 1. InstallLocation
                                    try:
                                        raw_loc, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                        raw_loc = str(raw_loc).strip('"').strip()
                                        if raw_loc and os.path.exists(raw_loc):
                                            loc = raw_loc
                                    except FileNotFoundError:
                                        pass

                                    # 2. DisplayIcon
                                    if not loc:
                                        try:
                                            icon, _ = winreg.QueryValueEx(app_key, "DisplayIcon")
                                            icon_p = str(icon).split(",")[0].strip('"').strip()
                                            if icon_p and os.path.exists(icon_p):
                                                parent_p = os.path.dirname(icon_p) if os.path.isfile(icon_p) else icon_p
                                                if os.path.basename(parent_p).lower() in ["bin", "cmd"]:
                                                    grand = os.path.dirname(parent_p)
                                                    loc = grand if os.path.exists(grand) else parent_p
                                                else:
                                                    loc = parent_p
                                        except FileNotFoundError:
                                            pass

                                    # 3. UninstallString / QuietUninstallString
                                    if not loc:
                                        for uninst_key in ["UninstallString", "QuietUninstallString"]:
                                            try:
                                                uninst_val, _ = winreg.QueryValueEx(app_key, uninst_key)
                                                extracted = extract_dir_from_cmd(str(uninst_val))
                                                if extracted and os.path.exists(extracted):
                                                    loc = extracted
                                                    break
                                            except FileNotFoundError:
                                                pass

                                    # 4. In-memory candidate lookup (exact & simplified)
                                    if not loc:
                                        dn_clean = re.sub(r'[^a-zA-Z0-9가-힣]', '', display_name.lower())
                                        loc = candidate_dirs.get(display_name.lower(), "")
                                        if not loc:
                                            loc = candidate_dirs.get(dn_clean, "")
                                        if not loc:
                                            # Word match in candidates
                                            for word in display_name.lower().split():
                                                if len(word) >= 4 and word in candidate_dirs:
                                                    loc = candidate_dirs[word]
                                                    break

                                    pub = ""
                                    try:
                                        pub, _ = winreg.QueryValueEx(app_key, "Publisher")
                                        pub = str(pub).strip()
                                    except FileNotFoundError:
                                        pass

                                    ver = ""
                                    try:
                                        ver, _ = winreg.QueryValueEx(app_key, "DisplayVersion")
                                        ver = str(ver).strip()
                                    except FileNotFoundError:
                                        pass

                                    is_valid = bool(loc and os.path.exists(loc))
                                    seen_names.add(display_name.lower())

                                    has_roaming = display_name.lower() in roaming_dirs
                                    roaming_guess = os.path.join(roaming_base, display_name) if has_roaming else None

                                    apps.append({
                                        "name": display_name,
                                        "publisher": pub or "소프트웨어",
                                        "version": ver,
                                        "location": loc if is_valid else "",
                                        "has_location": is_valid,
                                        "roaming_path": roaming_guess
                                    })
                                except FileNotFoundError:
                                    pass
                        except OSError:
                            pass
            except OSError:
                pass

    apps.sort(key=lambda x: (0 if x["has_location"] else 1, x["name"].lower()))
    return apps

def get_installed_applications(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Scans Windows Registry to detect genuine installed software applications.
    Caches results in memory with thread-safe double-checked locking (< 1ms).
    """
    global _cached_apps, _cached_apps_time

    now = time.time()
    if not force_refresh and _cached_apps is not None and (now - _cached_apps_time) < _CACHE_TTL:
        return _cached_apps

    with _scanner_lock:
        now = time.time()
        if not force_refresh and _cached_apps is not None and (now - _cached_apps_time) < _CACHE_TTL:
            return _cached_apps

        apps = _scan_installed_applications_impl()
        _cached_apps = apps
        _cached_apps_time = time.time()
        return apps
