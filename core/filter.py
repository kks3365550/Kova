import fnmatch
import os
from typing import List, Optional, Set

DEFAULT_EXCLUDE_PATTERNS = [
    # 1. Windows system files & OS memory/swap
    "$RECYCLE.BIN",
    "$Recycle.Bin",
    "System Volume Information",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "DumpStack.log.tmp",
    "Thumbs.db",
    "desktop.ini",
    "*.etl",
    "LiveKernelReports",
    "WindowsApps",
    "Temporary Internet Files",
    "UsrClass.dat*",
    "WebCacheLock.dat",

    # 2. Backup repositories & recursion guards (CRITICAL)
    "WindowsImageBackup",
    "MyBackup_Repository",
    "backup_repository",
    "emergency_restore",
    "*.blob",

    # 3. Temp files, locks, and incomplete downloads
    "*.tmp",
    "*.temp",
    "*.lock",
    "lockfile",
    "*~*",
    "~$*",              # Office temporary lock files (e.g. ~$Document.docx)
    "*.crdownload",     # Chrome incomplete download
    "*.part",           # Firefox incomplete download
    "*.swp",
    "*.swo",

    # 4. Crash reports & memory dumps
    "*.dmp",
    "*.mdmp",
    "*.hdmp",
    "CrashDumps",
    "*Crashpad*",
    "*CrashReporting*",
    "*crash_reports*",

    # 5. Windows Update & System Caches
    "SoftwareDistribution",
    "DeliveryOptimization",
    "INetCache",
    "*thumbcache*.db",

    # 6. Browser & Application Caches
    "Cache",
    "Caches",
    "*GPUCache*",
    "*Code Cache*",
    "*ShaderCache*",
    "*DawnCache*",
    "*D3DSCache*",
    "*GrShaderCache*",
    "*DirectXShaderCache*",
    "*CacheStorage*",
    "*Service Worker*",
    "*blob_storage*",
    "*SquirrelTemp*",
    "*Crashpad*",
    "*CrashReporting*",
    "*crash_reports*",

    # 7. Package manager & build caches
    "pip/cache",
    "npm-cache",
    "yarn-cache",
    "Package Cache",
    ".gradle/caches",
    ".m2/repository",
    ".nuget/packages",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".git",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",

    # 8. Massive VM / Disk images & installer ISOs
    "*.iso",
    "*.vhdx",
    "*.vmdk",
    "*.vdi",
    "*.qcow2",
    "*userdata*.img",
    "*system*.img"
]

class PathFilter:
    def __init__(self, exclude_patterns: Optional[List[str]] = None, include_patterns: Optional[List[str]] = None, use_defaults: bool = True, protected_prefixes: Optional[List[str]] = None):
        raw_excludes = list(DEFAULT_EXCLUDE_PATTERNS) if use_defaults else []
        if exclude_patterns:
            for p in exclude_patterns:
                p = p.strip()
                if p and p not in raw_excludes:
                    raw_excludes.append(p)
        self.include_patterns = include_patterns or []

        # Initialize protected prefixes with default AI runtime paths
        default_protected = [
            os.path.normpath(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'hermes')).lower(),
            os.path.normpath(os.path.join(os.environ.get('APPDATA', ''), 'uv')).lower()
        ]

        if protected_prefixes:
            for p in protected_prefixes:
                norm_p = os.path.normpath(p).lower()
                if norm_p not in default_protected:
                    default_protected.append(norm_p)

        self._protected_tuple = tuple(default_protected)

        # High-performance categorized structures
        self.exact_names: Set[str] = set()
        self.exts: Set[str] = set()
        self.substrs: List[str] = []
        self.complex_patterns: List[str] = []

        for pattern in raw_excludes:
            p = pattern.strip().lower()
            if not p or p.startswith('#'):
                continue
            if p.startswith('*.'):
                # e.g. *.tmp -> .tmp
                self.exts.add(p[1:])
            elif p.startswith('*') and p.endswith('*') and len(p) > 2 and '?' not in p and '[' not in p and '*' not in p[1:-1]:
                # e.g. *cache* -> cache
                self.substrs.append(p[1:-1])
            elif '*' in p or '?' in p or '[' in p:
                self.complex_patterns.append(p)
            else:
                self.exact_names.add(p)

    def _matches_common(self, name_lower: str) -> bool:
        """Helper to match exact names, substrings, and complex patterns cleanly."""
        if name_lower in self.exact_names:
            return True
        for sub in self.substrs:
            if sub in name_lower:
                return True
        for cp in self.complex_patterns:
            if fnmatch.fnmatch(name_lower, cp):
                return True
        return False

    def is_dir_excluded(self, dirname: str, full_path: Optional[str] = None) -> bool:
        """Fast O(1) check for directory exclusion (Prunes entire directory trees)."""
        dn = dirname.lower()

        # Check if inside a protected prefix
        if full_path:
            norm_full = os.path.normpath(full_path).lower().replace('\\', '/')
            for prefix in self._protected_tuple:
                if norm_full.startswith(prefix.replace('\\', '/')):
                    # Inside protected area: only exclude pure volatile caches
                    if dn in ("__pycache__", "cache", "caches", "gpucache", "shadercache", "code cache", "temp", "tmp", "crashpad", "crashreporting", "logs", "log"):
                        return True
                    # venv, node_modules, etc. should pass (return False)
                    return False

            # Check if full directory path matches any complex pattern (e.g. *androidstudio*index*)
            for cp in self.complex_patterns:
                if fnmatch.fnmatch(norm_full, cp) or fnmatch.fnmatch(norm_full, f"*{cp}*"):
                    return True

        # Not in protected area or no full_path: use default logic
        if dn in ("cache", "caches", "gpucache", "shadercache", "code cache", "temp", "tmp", "crashpad", "crashreporting", "logs", "log", "deliveryoptimization"):
            return True
        return self._matches_common(dn)

    def is_file_excluded(self, filename: str) -> bool:
        """Fast O(1) check for file exclusion."""
        fn = filename.lower()
        
        # Safety guard: Protect Python source files from cache-related substring matches
        # (e.g., prevent 'linecache.py' from being excluded by 'cache' substring)
        if fn.endswith('.py'):
            dot_idx = fn.rfind('.')
            if dot_idx != -1 and fn[dot_idx:] in self.exts:
                return True
            if fn in self.exact_names:
                return True
            return False

        dot_idx = fn.rfind('.')
        if dot_idx != -1 and fn[dot_idx:] in self.exts:
            return True
        return self._matches_common(fn)

    def is_excluded(self, path: str, is_dir: bool = False) -> bool:
        # Extract basename quickly without os.path.basename overhead
        slash_idx = max(path.rfind('\\'), path.rfind('/'))
        basename = path[slash_idx + 1:] if slash_idx != -1 else path

        if is_dir:
            if self.is_dir_excluded(basename, full_path=path):
                return True
        else:
            if self.is_file_excluded(basename):
                return True

        # Check ancestor directories if full path is passed
        norm_path = path.replace('\\', '/').lower()
        parts = norm_path.split('/')
        if not self.exact_names.isdisjoint(parts):
            return True

        # Fallback for complex relative path wildcard patterns
        for cp in self.complex_patterns:
            if fnmatch.fnmatch(norm_path, cp) or fnmatch.fnmatch(norm_path, f"*/{cp}/*") or fnmatch.fnmatch(norm_path, f"*/{cp}"):
                return True

        return False

