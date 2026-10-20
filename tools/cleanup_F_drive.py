#!/usr/bin/env python3
"""
F: 드라이브 과거 구버전 잔여 디렉터리 및 9/26 DR 테스트 산출물 안전 삭제 스크립트.
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DRIVE_ROOT = Path("F:/")

# 절대 보존 대상 (이 경로 및 그 하위 어디든 삭제 시도 시 즉시 중단)
PROTECTED_PATHS: list[Path] = [
    Path("F:/MyBackup_Repository"),
    Path("F:/WindowsImageBackup"),
    Path("F:/SteamLibrary"),
    Path("F:/RedDeadRedemption2"),
    Path("F:/GTAVEnhanced"),
    Path("F:/MODELS"),
    Path("F:/SidMeiersCivilizationVI"),
    Path("F:/Trails in the Sky 1st Chapter"),
    Path("F:/Oxygen.Not.Included.v7003861.3.4-P2P"),
    Path("F:/SNOW.BROS.SPECIAL.ANNIVERSARY.EDITION-GoldBerg"),
    Path("F:/Cmedia_CM6206LX-1.04_CR"),
    Path("F:/InternetDownloadManager6.42Build35.q.taiwebs.com"),
]

# 삭제 대상 디렉터리
TARGET_DIRS: list[Path] = [
    Path("F:/dr_extracted"),
    Path("F:/keys"),
    Path("F:/snapshots"),
    Path("F:/blobs"),
    Path("F:/manifests"),
]

# 삭제 대상 파일 (정확한 경로)
TARGET_FILES: list[Path] = [
    Path("F:/repo.tar"),
    Path("F:/metadata.db"),
    Path("F:/repo_meta.json"),
    Path("F:/disaster_recovery.py"),
    Path("F:/inspect_snap.py"),
    Path("F:/linecache.py"),
    Path("F:/system_image_backup.log"),
    Path("F:/test_guest.txt"),
    Path("F:/phase_e_deep_audit_report.json"),
    Path("F:/vhdx_3way_results.json"),
    Path("F:/vhd_deep_audit_report.json"),
    Path("F:/merge_vhd.ps1"),
    Path("F:/pipeline_phase_d2.ps1"),
    Path("F:/revert_and_check.ps1"),
]

# 삭제 대상 파일 (glob 패턴)
TARGET_FILE_PATTERNS: list[str] = [
    "replication_queue.db*",
    "dr_*.txt",
    "dr_*.json",
    "audit_*.ps1",
    "check_*.ps1",
    "inspect_vhd_*.ps1",
    "run_*.ps1",
    "1_*.bat",
]

LOG_FILE = Path("F:/cleanup_F_drive.log")


@dataclass
class CleanupResult:
    total_items: int = 0
    total_bytes: int = 0
    deleted_dirs: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("cleanup_F_drive")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    try:
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as e:
        logger.warning(f"로그 파일 생성 실패: {e} (콘솔 로깅만 사용)")

    return logger


def get_disk_usage(path: Path) -> tuple[int, int, int]:
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.used, usage.free
    except OSError:
        return 0, 0, 0


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def is_protected(path: Path) -> bool:
    try:
        path_res = path.resolve()
    except Exception:
        path_res = path
    for prot in PROTECTED_PATHS:
        try:
            prot_res = prot.resolve()
        except Exception:
            prot_res = prot
        if path_res == prot_res or prot_res in path_res.parents:
            return True
        try:
            path_res.relative_to(prot_res)
            return True
        except ValueError:
            pass
    return False


def collect_pattern_files(patterns: list[str], root: Path) -> list[Path]:
    matched: list[Path] = []
    for pat in patterns:
        try:
            for p in root.glob(pat):
                if p.is_file():
                    if is_protected(p):
                        continue
                    matched.append(p)
        except (OSError, PermissionError):
            continue
    return list(dict.fromkeys(matched))


def preflight_check(logger: logging.Logger) -> bool:
    if not DRIVE_ROOT.exists():
        logger.critical(f"드라이브 {DRIVE_ROOT} 존재하지 않음. 중단.")
        return False

    # Dynamically find any legacy installation folders on F:\ (e.g. 백업시스템_설치본)
    try:
        for item in DRIVE_ROOT.iterdir():
            if item.is_dir() and not is_protected(item):
                has_backup_marker = (item / "cli_backup.py").exists() or (item / "driver_backup.py").exists()
                if has_backup_marker or "설치" in item.name or "ý" in item.name:
                    if item not in TARGET_DIRS:
                        TARGET_DIRS.append(item)
                        logger.info(f"동적 잔여 설치 폴더 발견 및 추가: {item.name}")
    except Exception as e:
        logger.warning(f"동적 폴더 검색 예외: {e}")

    all_targets: list[Path] = []
    all_targets.extend(TARGET_DIRS)
    all_targets.extend(TARGET_FILES)
    all_targets.extend(collect_pattern_files(TARGET_FILE_PATTERNS, DRIVE_ROOT))

    for t in all_targets:
        if is_protected(t):
            logger.critical(
                f"FAIL-SAFE: 삭제 대상 '{t}'이(가) 보호 경로와 겹칩니다. 즉시 중단."
            )
            return False

    logger.info(f"사전 검증 통과. 삭제 대상 {len(all_targets)}개 확인.")
    return True


def remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def delete_directory(path: Path, logger: logging.Logger, result: CleanupResult,
                     dry_run: bool) -> None:
    if is_protected(path):
        logger.critical(f"FAIL-SAFE: '{path}'이(가) 보호 경로입니다. 삭제 거부.")
        result.errors.append(f"FAIL-SAFE: {path}")
        return

    if not path.exists():
        logger.debug(f"존재하지 않음 (스킵): {path}")
        result.skipped.append(str(path))
        return

    dir_size = 0
    # Measure size and remove read-only attributes
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = Path(dirpath) / f
                try:
                    dir_size += fp.stat().st_size
                    if not dry_run:
                        os.chmod(fp, stat.S_IWRITE)
                except OSError:
                    pass
            for d in dirnames:
                dp = Path(dirpath) / d
                try:
                    if not dry_run:
                        os.chmod(dp, stat.S_IWRITE)
                except OSError:
                    pass
        if not dry_run:
            os.chmod(path, stat.S_IWRITE)
    except (OSError, PermissionError) as e:
        logger.warning(f"권한 해제/용량 측정 실패 ({path}): {e}")

    if dry_run:
        logger.info(f"[DRY-RUN] 디렉터리 삭제 예정: {path} ({format_bytes(dir_size)})")
        result.total_items += 1
        result.total_bytes += dir_size
        result.deleted_dirs.append(str(path))
        return

    try:
        shutil.rmtree(path, onerror=remove_readonly)
        logger.info(f"삭제 완료: {path} ({format_bytes(dir_size)})")
        result.total_items += 1
        result.total_bytes += dir_size
        result.deleted_dirs.append(str(path))
    except (OSError, PermissionError) as e:
        logger.error(f"디렉터리 삭제 실패: {path} → {e}")
        result.errors.append(f"DIR: {path}: {e}")


def delete_file(path: Path, logger: logging.Logger, result: CleanupResult,
                dry_run: bool) -> None:
    if is_protected(path):
        logger.critical(f"FAIL-SAFE: '{path}'이(가) 보호 경로입니다. 삭제 거부.")
        result.errors.append(f"FAIL-SAFE: {path}")
        return

    if not path.exists():
        logger.debug(f"존재하지 않음 (스킵): {path}")
        result.skipped.append(str(path))
        return

    try:
        fsize = path.stat().st_size
    except OSError:
        fsize = 0

    if dry_run:
        logger.info(f"[DRY-RUN] 파일 삭제 예정: {path} ({format_bytes(fsize)})")
        result.total_items += 1
        result.total_bytes += fsize
        result.deleted_files.append(str(path))
        return

    try:
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass
        path.unlink()
        logger.info(f"삭제 완료: {path} ({format_bytes(fsize)})")
        result.total_items += 1
        result.total_bytes += fsize
        result.deleted_files.append(str(path))
    except (OSError, PermissionError) as e:
        logger.error(f"파일 삭제 실패: {path} → {e}")
        result.errors.append(f"FILE: {path}: {e}")


def run_cleanup(logger: logging.Logger, dry_run: bool) -> CleanupResult:
    result = CleanupResult()

    if not preflight_check(logger):
        logger.critical("사전 검증 실패. 삭제 작업 중단.")
        return result

    total_before, used_before, free_before = get_disk_usage(DRIVE_ROOT)
    logger.info(
        f"디스크 상태 (작업 전): 총 {format_bytes(total_before)}, "
        f"사용 {format_bytes(used_before)}, "
        f"여유 {format_bytes(free_before)}"
    )

    logger.info("── [1] 디렉터리 삭제 시작 ──")
    for d in TARGET_DIRS:
        delete_directory(d, logger, result, dry_run)

    logger.info("── [2] 명시적 파일 삭제 시작 ──")
    for f in TARGET_FILES:
        delete_file(f, logger, result, dry_run)

    logger.info("── [3] 패턴 기반 파일 삭제 시작 ──")
    pattern_files = collect_pattern_files(TARGET_FILE_PATTERNS, DRIVE_ROOT)
    logger.info(f"패턴 매칭 파일 {len(pattern_files)}개 발견.")
    for f in pattern_files:
        delete_file(f, logger, result, dry_run)

    total_after, used_after, free_after = get_disk_usage(DRIVE_ROOT)
    logger.info(
        f"디스크 상태 (작업 후): 총 {format_bytes(total_after)}, "
        f"사용 {format_bytes(used_after)}, "
        f"여유 {format_bytes(free_after)}"
    )

    freed = free_after - free_before
    logger.info("=" * 60)
    logger.info(f"총 처리 항목: {result.total_items}개")
    logger.info(f"정리된 용량: {format_bytes(result.total_bytes)}")
    if not dry_run:
        logger.info(f"실제 확보된 여유 공간: {format_bytes(freed)}")
    logger.info(f"삭제된 디렉터리: {len(result.deleted_dirs)}개")
    logger.info(f"삭제된 파일: {len(result.deleted_files)}개")
    logger.info(f"스킵: {len(result.skipped)}개")
    logger.info(f"오류: {len(result.errors)}개")
    if result.errors:
        for err in result.errors:
            logger.warning(f"  ERROR: {err}")
    logger.info("=" * 60)

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F: 드라이브 잔여물 안전 삭제 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="삭제 없이 검증만 수행",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=LOG_FILE,
        help=f"로그 파일 경로 (기본: {LOG_FILE})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(args.log_file)
    logger.info("F: 드라이브 클린업 프로세스 시작" + (" [DRY-RUN 모드]" if args.dry_run else " [REAL EXECUTION]"))
    res = run_cleanup(logger, args.dry_run)
    return 0 if not res.errors else 1


if __name__ == "__main__":
    sys.exit(main())
