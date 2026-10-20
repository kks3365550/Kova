# -*- coding: utf-8 -*-
"""
core/updater_v2/artifact_safety.py: Step 4B 아티팩트 무해성 및 시스템 안전 검증기 (Reinforced)

보강된 방어 체계:
1. Windows 경로 정규화 우회 벡터 전수 차단:
   - 혼합 슬래시 (`../`, `..\\`, `/../`, `.\\..`)
   - 유니코드 경로 분리자 / 전각 문자 치환 방어
   - posixpath 및 ntpath 2중 검사
2. 4축 독립 리소스 제한 (Zip Bomb 방어):
   - 축 1: 단일 파일 최대 크기 초과 차단 (기본 50MB)
   - 축 2: 단일 파일 압축비 초과 차단 (기본 50.0배, 1KB 이상 대상)
   - 축 3: 전체 누적 압축 해제 용량 초과 차단 (기본 150MB)
   - 축 4: 전체 파일 개수 초과 차단 (기본 3,000개)
3. Symlink / Reparse Point 카탈로그 검사:
   - UNIX 0o120000 (S_IFLNK) 및 Windows 0x400 (Reparse Point) 차단
4. TOCTOU 방어를 위한 안전 추출 검증 헬퍼 (assert_safe_destination_path)
"""

import io
import os
import posixpath
import ntpath
import zipfile
from typing import Union, List, Set, Tuple


class SafetyViolationError(Exception):
    """아티팩트 안전성 검증 위반 예외

    이전 코드는 `SecurityError if "SecurityError" in dir(__builtins__) else Exception`
    이었다. __builtins__ 는 모듈(dict) 또는 모듈(lexical) 두形态이므로
    dir() 결과가 실행 환경마다 달라졌다. pyflakes 는 여기서
    `undefined name 'SecurityError'` 로 잡았고, 실제로 조건식이
    의도와 다르게 평가될 수 있었다.
    상위 예외 타입을 바꾸지 않고 Exception 으로 고정한다.
    """
    pass


# 4축 리소스 임계치 기본값
DEFAULT_MAX_FILE_COUNT = 3000
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 150 * 1024 * 1024  # 150 MB
DEFAULT_MAX_PER_FILE_BYTES = 50 * 1024 * 1024             # 50 MB
DEFAULT_MAX_COMPRESSION_RATIO = 50.0                      # 50.0x

# 허용 디렉터리 및 확장자
ALLOWED_TOP_DIRS: Set[str] = {"core", "web", "keys"}
ALLOWED_ROOT_FILES: Set[str] = {
    "run.py", "start_silent.vbs", "start_tray.vbs",
    "stop_backup_system.bat", "2_백업시스템_실행.bat", "VERSION"
}
ALLOWED_EXTENSIONS: Set[str] = {
    ".py", ".pyw", ".bat", ".vbs", ".html", ".htm", ".css", ".js",
    ".json", ".pub", ".txt", ".md", ".ico", ".png", ".svg",
    ".woff", ".woff2", ".ttf", ".eot"
}
DISALLOWED_SECRET_PATTERNS = [".key", ".pem", "private", ".p12", ".pfx", ".kdbx"]

# 유니코드 특수/전각 경로 구분자 등 정규화 대상
UNICODE_SEPARATOR_TRANSLATION = str.maketrans({
    '\u2044': '/',  # Fraction slash
    '\u2215': '/',  # Division slash
    '\uff0f': '/',  # Fullwidth solidus
    '\uff3c': '\\', # Fullwidth reverse solidus
    '\u29f5': '\\', # Reverse solidus operator
    '\u29f8': '/',  # Big solidus
    '\u29f9': '\\', # Big reverse solidus
})


class ArtifactSafetyChecker:
    """배포 ZIP 패키지의 시스템 안전성을 엄격히 검증하는 클래스"""

    def __init__(
        self,
        max_file_count: int = DEFAULT_MAX_FILE_COUNT,
        max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
        max_per_file_bytes: int = DEFAULT_MAX_PER_FILE_BYTES,
        max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO
    ):
        self.max_file_count = max_file_count
        self.max_total_uncompressed_bytes = max_total_uncompressed_bytes
        self.max_per_file_bytes = max_per_file_bytes
        self.max_compression_ratio = max_compression_ratio

    def check_archive(self, zip_source: Union[str, bytes]) -> Tuple[bool, List[str]]:
        """
        ZIP 패키지를 메모리/스트림으로 열어 5대 안전 수칙을 전수 검사합니다.

        Args:
            zip_source: 검사할 ZIP 파일 경로 또는 바이너리 바이트

        Returns:
            (is_safe, error_messages)
        """
        errors: List[str] = []

        try:
            if isinstance(zip_source, bytes):
                zf = zipfile.ZipFile(io.BytesIO(zip_source), "r")
            else:
                if not os.path.exists(zip_source):
                    return False, [f"Package file not found: {zip_source}"]
                zf = zipfile.ZipFile(zip_source, "r")
        except zipfile.BadZipFile as e:
            return False, [f"Corrupt or invalid zip archive: {str(e)}"]
        except Exception as e:
            return False, [f"Failed to open zip archive: {str(e)}"]

        with zf:
            infolist = zf.infolist()
            total_files = len(infolist)

            # [축 4] 전체 파일 개수 검사
            if total_files > self.max_file_count:
                errors.append(
                    f"RESOURCE_VIOLATION_FILE_COUNT: File count ({total_files}) exceeds limit ({self.max_file_count})"
                )
                return False, errors

            total_uncompressed_size = 0
            has_core_or_web = False

            for info in infolist:
                filename = info.filename

                # 1. 유니코드 전각/특수 슬래시 치환 및 혼합 슬래시 통일
                translated = filename.translate(UNICODE_SEPARATOR_TRANSLATION)
                normalized = translated.replace("\\", "/")

                # 2. Path Traversal & Zip-Slip 정밀 검사
                # 2.1 절대 경로 및 드라이브 문자 검사 (POSIX / Windows)
                if (
                    normalized.startswith("/") or
                    (len(normalized) >= 2 and normalized[1] == ":") or
                    ntpath.isabs(translated) or
                    posixpath.isabs(normalized)
                ):
                    errors.append(f"ZIP_SLIP_VIOLATION: Absolute path or drive letter: '{filename}'")
                    continue

                # 2.2 상위 디렉터리 탐색 (..) 다각도 검사 (./../, ../, ..\/, CORE/../ 등)
                parts = [p for p in normalized.split("/") if p]
                if ".." in parts or "." in parts:
                    errors.append(f"ZIP_SLIP_VIOLATION: Path traversal segment detected: '{filename}'")
                    continue

                # 2.3 posixpath 정규화 대조 (정규화 결과와 원본 경로 구조 비교)
                posix_norm = posixpath.normpath(normalized)
                if posix_norm.startswith("..") or posix_norm.startswith("/"):
                    errors.append(f"ZIP_SLIP_VIOLATION: Normalized path escape: '{filename}'")
                    continue

                # 2.4 Windows 금지 특수문자 검사 (파일명 내 콜론, 와일드카드, 제어문자 등)
                for part in parts:
                    if any(c in part for c in [":", "*", "?", '"', "<", ">", "|"]):
                        errors.append(f"ILLEGAL_CHAR_VIOLATION: Invalid filename character in: '{filename}'")
                        break

                # 3. Symlink 및 NTFS Junction/Reparse Point 검사
                # UNIX symlink file mode (0o120000 = S_IFLNK)
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    errors.append(f"SYMLINK_VIOLATION: Symbolic link detected in zip: '{filename}'")

                # Windows Reparse Point attribute (0x400)
                win_attr = info.external_attr & 0xFFFF
                if (win_attr & 0x400) != 0:
                    errors.append(f"REPARSE_POINT_VIOLATION: Windows junction/reparse point detected: '{filename}'")

                # 4. Secret / Private Key 누출 검사
                lower_name = normalized.lower()
                for secret_pat in DISALLOWED_SECRET_PATTERNS:
                    if secret_pat in lower_name:
                        # keys/ 폴더의 .pub 공개키는 명시적 예외 허용
                        if secret_pat == ".key" and lower_name.endswith(".pub"):
                            continue
                        errors.append(f"SECRET_LEAK_VIOLATION: Sensitive/Private file detected: '{filename}'")
                        break

                # 5. File & Directory Allowlist 검사
                is_dir = info.is_dir() or normalized.endswith("/")
                if not is_dir and parts:
                    top_dir = parts[0]
                    if len(parts) == 1:
                        # 루트 파일인 경우
                        if top_dir not in ALLOWED_ROOT_FILES:
                            errors.append(f"ALLOWLIST_VIOLATION: Unapproved root file: '{filename}'")
                    else:
                        # 하위 디렉터리인 경우
                        if top_dir not in ALLOWED_TOP_DIRS:
                            errors.append(f"ALLOWLIST_VIOLATION: Unapproved top-level directory '{top_dir}': '{filename}'")

                        # keys/ 폴더는 반드시 .pub 공개키만 허용
                        if top_dir == "keys" and not lower_name.endswith(".pub"):
                            errors.append(f"SECURITY_VIOLATION: Non-public-key file in keys/ directory: '{filename}'")

                    # 파일 확장자 검사
                    base_file = parts[-1]
                    _, ext = os.path.splitext(base_file)
                    ext_lower = ext.lower()
                    if base_file != "VERSION" and ext_lower not in ALLOWED_EXTENSIONS:
                        errors.append(f"ALLOWLIST_VIOLATION: Disallowed file extension '{ext}' in: '{filename}'")

                # 필수 패키지 구성 요소 존재 확인
                if "core/" in normalized or "web/" in normalized:
                    has_core_or_web = True

                # 6. [4축 독립 리소스 제한 검사]
                uncomp_size = info.file_size
                comp_size = info.compress_size
                total_uncompressed_size += uncomp_size

                # [축 1] 단일 파일 최대 용량 초과 검사
                if uncomp_size > self.max_per_file_bytes:
                    errors.append(
                        f"RESOURCE_VIOLATION_PER_FILE_SIZE: File '{filename}' size ({uncomp_size} bytes) exceeds limit ({self.max_per_file_bytes} bytes)"
                    )

                # [축 2] 단일 파일 압축비 초과 검사
                if uncomp_size > 1024 and comp_size > 0:
                    ratio = uncomp_size / comp_size
                    if ratio > self.max_compression_ratio:
                        errors.append(
                            f"RESOURCE_VIOLATION_COMPRESSION_RATIO: Excessive compression ratio ({ratio:.1f}x) in: '{filename}'"
                        )

                # [축 3] 전체 누적 압축 해제 용량 검사
                if total_uncompressed_size > self.max_total_uncompressed_bytes:
                    errors.append(
                        f"RESOURCE_VIOLATION_TOTAL_SIZE: Cumulative size ({total_uncompressed_size} bytes) exceeds limit ({self.max_total_uncompressed_bytes} bytes)"
                    )
                    break

            if not has_core_or_web and not errors:
                errors.append("INTEGRITY_VIOLATION: Essential component ('core/' or 'web/') missing from package")

        is_safe = len(errors) == 0
        return is_safe, errors


def assert_safe_destination_path(target_base_dir: str, rel_path: str) -> str:
    """
    TOCTOU 및 Reparse Point 공격 방어를 위해, 압축 해제 전 대상 경로가
    심볼릭 링크나 정션을 타고 외부로 탈출하지 않는지 검증하고 절대 경로를 반환합니다.
    """
    target_base_abs = os.path.abspath(target_base_dir)
    full_dest = os.path.abspath(os.path.join(target_base_abs, rel_path))

    # 대상 경로가 target_base_abs 내부에 위치하는지 엄격 검사
    try:
        common = os.path.commonpath([target_base_abs, full_dest])
        if common != target_base_abs:
            raise SafetyViolationError(f"Path escape detected: '{full_dest}' is outside '{target_base_abs}'")
    except ValueError as e:
        raise SafetyViolationError(f"Cross-drive path escape detected: {e}")

    # 중간 경로 또는 최종 파일이 기존 symlink/junction인지 검사
    curr = target_base_abs
    for part in rel_path.replace("\\", "/").split("/"):
        if not part:
            continue
        curr = os.path.join(curr, part)
        if os.path.islink(curr):
            raise SafetyViolationError(f"TOCTOU Reparse Attack: Pre-existing symlink/junction in destination: '{curr}'")

    return full_dest
