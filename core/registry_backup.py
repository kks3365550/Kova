import os
import sys
import subprocess
from typing import List, Tuple, Optional

class AppPackageCollector:
    """
    Pre-indexes Start Menu shortcuts, AppData directories, and Registry Uninstall keys
    ONCE in memory, reducing per-application scanning overhead from 30s to < 100ms.
    """
    def __init__(self, dest_dir: str = ""):
        if not dest_dir:
            import tempfile
            dest_dir = os.path.join(tempfile.gettempdir(), "Universal_Registry_Backup")
        self.dest_dir = dest_dir
        os.makedirs(self.dest_dir, exist_ok=True)
        self.seen_reg_keys = set()
        
        self.shortcuts_index = self._index_shortcuts()
        self.appdata_index = self._index_appdata()
        self.uninstall_index = self._index_uninstall_registry()

    def _index_shortcuts(self) -> List[Tuple[str, str, str]]:
        # Returns [(filename_lower, parent_dir_lower, full_path), ...]
        start_menus = [
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(os.path.expanduser("~"), r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs")
        ]
        result = []
        for sm in start_menus:
            if os.path.exists(sm):
                for root, dirs, files in os.walk(sm):
                    root_lower = root.lower()
                    for f in files:
                        if f.lower().endswith(".lnk"):
                            result.append((f.lower(), root_lower, os.path.join(root, f)))
        return result

    def _index_appdata(self) -> List[Tuple[str, str]]:
        # Returns [(dir_name_lower, full_path), ...]
        bases = [
            os.path.expanduser(r"~\AppData\Roaming"),
            os.path.expanduser(r"~\AppData\Local")
        ]
        result = []
        for base in bases:
            if os.path.exists(base):
                try:
                    with os.scandir(base) as it:
                        for entry in it:
                            if entry.is_dir():
                                name_lower = entry.name.lower()
                                if not name_lower.startswith("temp"):
                                    result.append((name_lower, entry.path))
                except OSError:
                    pass
        return result

    def _index_uninstall_registry(self) -> List[Tuple[str, str, str]]:
        # Returns [(display_name_lower, full_reg_key, file_tag), ...]
        if not sys.platform.startswith("win"):
            return []
        import winreg
        uninstall_roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", "HKLM"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", "HKLM_WOW64"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU", "HKCU")
        ]
        result = []
        for root_hkey, subkey_path, reg_hive_str, file_tag in uninstall_roots:
            try:
                with winreg.OpenKey(root_hkey, subkey_path) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        try:
                            kname = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, kname) as sub:
                                try:
                                    dname, _ = winreg.QueryValueEx(sub, "DisplayName")
                                    if dname:
                                        full_reg_key = f"{reg_hive_str}\\{subkey_path}\\{kname}"
                                        result.append((str(dname).strip().lower(), full_reg_key, file_tag))
                                except FileNotFoundError:
                                    pass
                        except OSError:
                            pass
            except OSError:
                pass
        return result

def find_app_start_menu_shortcuts(app_name: str, publisher: str = "", collector: Optional[AppPackageCollector] = None) -> List[str]:
    tokens = [t.lower() for t in [app_name, app_name.split()[0] if app_name else "", publisher] if t and len(t) > 1]
    if not tokens:
        return []

    index = collector.shortcuts_index if collector else AppPackageCollector().shortcuts_index
    shortcuts = []
    for f_lower, root_lower, full_path in index:
        if any(token in f_lower or token in root_lower for token in tokens):
            if full_path not in shortcuts:
                shortcuts.append(full_path)
    return shortcuts

def find_app_data_directories(app_name: str, publisher: str = "", collector: Optional[AppPackageCollector] = None) -> List[str]:
    tokens = [t.lower() for t in [app_name, app_name.split()[0] if app_name else "", publisher] if t and len(t) > 1]
    if not tokens:
        return []

    index = collector.appdata_index if collector else AppPackageCollector().appdata_index
    appdatas = []
    for entry_lower, full_p in index:
        if any(t == entry_lower or t in entry_lower for t in tokens):
            if full_p not in appdatas:
                appdatas.append(full_p)
    return appdatas

def export_universal_app_registry(app_name: str, publisher: str = "", dest_dir: str = "", collector: Optional[AppPackageCollector] = None) -> List[str]:
    if not sys.platform.startswith("win"):
        return []

    coll = collector or AppPackageCollector(dest_dir)
    dest = coll.dest_dir
    seen = coll.seen_reg_keys
    exported_files = []

    clean_name = "".join(c for c in app_name if c.isalnum() or c in (" ", "_", "-")).strip()
    file_prefix = clean_name.replace(" ", "_")[:30]

    app_lower = app_name.lower()

    # 1. Fast in-memory match for Uninstall keys
    idx = 0
    for dname_lower, full_reg_key, file_tag in coll.uninstall_index:
        if dname_lower == app_lower or app_lower in dname_lower:
            if full_reg_key not in seen:
                seen.add(full_reg_key)
                out_file = os.path.join(dest, f"{file_prefix}_Uninstall_{file_tag}_{idx}.reg")
                idx += 1
                cmd = ["reg", "export", full_reg_key, out_file, "/y"]
                try:
                    kwargs = {"capture_output": True, "timeout": 5}
                    if sys.platform.startswith("win"):
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    res = subprocess.run(cmd, **kwargs)
                    if res.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
                        exported_files.append(out_file)
                except Exception:
                    pass

    # 2. Match Software Configuration Keys
    software_roots = [
        ("HKCU\\Software", "HKCU"),
        ("HKLM\\Software", "HKLM"),
        ("HKLM\\Software\\WOW6432Node", "HKLM_WOW64")
    ]
    tokens = [t for t in [app_name, app_name.split()[0] if app_name else "", publisher] if t and len(t) > 1]

    for root_path, hive_tag in software_roots:
        for t in tokens:
            full_reg_key = f"{root_path}\\{t}"
            if full_reg_key in seen:
                continue

            clean_t = "".join(c for c in t if c.isalnum() or c in ("_", "-")).strip()
            out_file = os.path.join(dest, f"{file_prefix}_{clean_t}_{hive_tag}.reg")
            cmd = ["reg", "export", full_reg_key, out_file, "/y"]
            try:
                kwargs = {"capture_output": True, "timeout": 5}
                if sys.platform.startswith("win"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                res = subprocess.run(cmd, **kwargs)
                if res.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
                    seen.add(full_reg_key)
                    exported_files.append(out_file)
            except Exception:
                pass

    return exported_files

def import_registry_file(reg_filepath: str) -> bool:
    if not sys.platform.startswith("win") or not os.path.exists(reg_filepath):
        return False
    cmd = ["reg", "import", os.path.abspath(reg_filepath)]
    try:
        kwargs = {"capture_output": True, "timeout": 15}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        res = subprocess.run(cmd, **kwargs)
        return res.returncode == 0
    except Exception:
        return False

def collect_full_app_package(app_name: str, publisher: str = "", install_location: str = "", temp_reg_dir: str = "", collector: Optional[AppPackageCollector] = None) -> Tuple[List[str], List[str]]:
    coll = collector or AppPackageCollector(temp_reg_dir)
    sources = []
    
    if install_location and os.path.exists(install_location):
        sources.append(os.path.abspath(install_location))

    for ap in find_app_data_directories(app_name, publisher, collector=coll):
        if os.path.exists(ap) and ap not in sources:
            sources.append(ap)

    for sc in find_app_start_menu_shortcuts(app_name, publisher, collector=coll):
        if os.path.exists(sc) and sc not in sources:
            sources.append(sc)

    reg_files = export_universal_app_registry(app_name, publisher, temp_reg_dir, collector=coll)
    for rf in reg_files:
        parent_dir = os.path.dirname(rf)
        if parent_dir not in sources:
            sources.append(parent_dir)

    return sources, reg_files
