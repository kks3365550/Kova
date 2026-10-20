import os
import sys
import time
import argparse
import datetime

from core.config import ConfigManager
from core.snapshot import SnapshotEngine

# NOTE: export_windows_drivers / collect_full_app_package 는 현재 미사용(pyflakes).
# 제거하지 않는 이유: 웹 UI(app.py:688)는 드라이버를 .inf로 '추출'하지만,
# CLI 스케줄 백업은 프로필 sources(137개 경로)의 폴더를 그대로 '복사'한다.
# 즉 두 경로의 백업 방식이 다르며, 이 import는 두 방식의 차이를 명시한다.
# 프로필에 드라이버 추출을 추가하려면 아래를 활성화해야 한다.
# from core.driver_backup import export_windows_drivers
# from core.registry_backup import collect_full_app_package

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

# 작업 스케줄러는 pythonw.exe 로 이 스크립트를 실행한다.
# pythonw 에는 콘솔이 없어 print() 출력이 어디에도 남지 않는다.
# 2026-10-07 09:00 백업이 Result=0 으로 "정상" 이어도 로그가 없었던 것이 그 때문이다.
# 따라서 파일 로그를 먼저 구성하고, 그 뒤에 print 를 남긴다(수동 실행 시 콘솔용).
from core.logging_setup import get_logger, log_path  # noqa: E402

log = get_logger("cli.backup")


def _say(msg: str) -> None:
    """로그 파일과 표준출력에 동시에 남긴다."""
    log.info(msg)
    try:
        print(msg)
    except Exception:
        pass


def _warn(msg: str) -> None:
    log.warning(msg)
    try:
        print(msg)
    except Exception:
        pass


def _error(msg: str) -> None:
    log.error(msg)
    try:
        print(msg)
    except Exception:
        pass


def _run_offsite_replication(repo_dir: str, profile: dict):
    """
    오프사이트(이중화) 복제를 수행한다.

    설계 원칙:
      - 로컬 백업 결과를 절대 훼손하지 않는다 (격리된 예외 처리)
      - 실패해도 로컬 백업은 '성공'으로 유지된다
      - 원격 미도달 시 조용히 스킵하되 로그와 프로필에 상태를 남긴다
    """
    remote = profile.get("offsite_repo_dir")
    if not remote:
        return  # 미설정 = 정상 (단일 저장소 운용)

    try:
        from core.offsite import replicate_offsite
        timeout = int(profile.get("offsite_timeout_sec", 7200))
    except Exception as e_import:
        _warn(f"[오프사이트] 모듈 로드 실패, 복제 건너뜀: {e_import}")
        return

    _say(f" -> 오프사이트 복제 시작: {repo_dir} -> {remote}")
    try:
        res = replicate_offsite(repo_dir, remote, timeout_sec=timeout)
    except Exception as e_rep:
        # 어떤 예외든 로컬 백업 결과에는 영향 없음
        log.warning("[오프사이트] 복제 중 예외 (백업 결과에 영향 없음): %s", e_rep, exc_info=True)
        profile["last_offsite_status"] = "error"
        profile["last_offsite_message"] = str(e_rep)[:300]
        try:
            ConfigManager.save_profile(profile)
        except Exception as e_save:
            # 상태 기록 실패는 조용히 삼키면 "복제도 실패했고 기록도 없다"가 된다
            log.error("[오프사이트] 프로필 상태 저장 실패: %s", e_save, exc_info=True)
        return

    if res.get("skipped"):
        _say(f"[오프사이트] 스킵: {res.get('reason')}")
        profile["last_offsite_status"] = "skipped"
        profile["last_offsite_message"] = res.get("reason", "")[:300]
    elif res.get("success"):
        _say(
            f"[오프사이트] 완료: 신규 {res.get('files_copied', 0)}개 "
            f"(전체 {res.get('files_total', 0)}, 실패 {res.get('files_failed', 0)}, "
            f"{res.get('duration_sec', 0):.0f}초)"
        )
        profile["last_offsite_status"] = "success"
        profile["last_offsite_message"] = (
            f"신규 {res.get('files_copied', 0)}개 / {res.get('duration_sec', 0):.0f}초"
        )
        profile["last_offsite_run"] = time.time()
    else:
        # 실패는 조용히 넘기지 않는다 - 로컬 백업은 성공이어도 오프사이트는 없을 수 있음
        _warn(f"[오프사이트] 실패: {res.get('reason')}")
        profile["last_offsite_status"] = "failed"
        profile["last_offsite_message"] = res.get("reason", "")[:300]

    try:
        ConfigManager.save_profile(profile)
    except Exception as e_save:
        log.error("[오프사이트] 프로필 상태 저장 실패: %s", e_save, exc_info=True)


def run_cli_backup(profile_id: str = None):
    log.info("백업 로그 시작 (파일: %s)", log_path())
    _say(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === 스마트 원샷 백업 시작 ===")

    # 1. Load profile
    profile = None
    if profile_id:
        profile = ConfigManager.get_profile(profile_id)

    if not profile:
        profiles = ConfigManager.get_profiles()
        # Find active profile with auto_backup_enabled == True, or first profile
        profile = next((p for p in profiles if p.get("auto_backup_enabled")), (profiles[0] if profiles else None))

    if not profile:
        _error("[ERROR] 백업할 프로필을 찾을 수 없습니다.")
        sys.exit(1)

    prof_id = profile.get("id", "prof_custom_selected")
    profile_name = profile.get("name", "자동 스케줄 백업")
    repo_dir = profile.get("repo_dir") or os.path.join(BASE_DIR, "backup_repository")
    sources = profile.get("sources") or [BASE_DIR]
    excludes = profile.get("exclude_patterns") or []
    compress = profile.get("compression_level", 6)
    retention_count = profile.get("retention_count", 30)
    retention_days = profile.get("retention_days", 60)

    _say(f" -> 프로필: {profile_name} ({prof_id})")
    _say(f" -> 대상 경로 수: {len(sources)}개")
    _say(f" -> 백업 저장소: {repo_dir}")

    # 2. Run backup
    try:
        manifest = SnapshotEngine.create_snapshot(
            repo_dir=repo_dir,
            sources=sources,
            profile_id=prof_id,
            profile_name=profile_name,
            exclude_patterns=excludes,
            compress_level=compress
        )

        # 3. Prune old snapshots
        try:
            pruned = SnapshotEngine.prune_snapshots(repo_dir, retention_count, retention_days, authorized=True)
            _say(f"[보존 정책] 만료된 스냅샷 {len(pruned)}개 정리 완료")
        except Exception as e_prune:
            # 백업은 성공했지만 정리 실패는 데이터 증가로 이어진다 -> 경고로 남긴다
            _warn(f"[보존 정책] 정리 실패/중단: {e_prune}")
            log.warning("보존 정책 정리 실패 상세", exc_info=True)

        # 4. Update profile last run time
        profile["last_run"] = time.time()
        profile["last_status"] = "success"
        profile["last_snapshot_id"] = manifest["id"]
        ConfigManager.save_profile(profile)

        # 5. Offsite Replication (2차 이중화 - 백업 성공 후에만 수행)
        _run_offsite_replication(repo_dir, profile)

        _say("=== 백업 프로세스 정상 종료 (메모리 완전 회수) ===")
        return 0

    except Exception as e:
        log.error("백업 실행 실패: %s", e, exc_info=True)
        _error(f"[ERROR] 백업 실행 실패: {e}")
        try:
            from core.windows_toast import notify_backup_failed
            notify_backup_failed(profile_name, str(e))
        except Exception:
            pass
        try:
            profile["last_status"] = "failed"
            profile["last_error"] = str(e)[:300]
            ConfigManager.save_profile(profile)
        except Exception as e_save:
            # 실패 상태 기록까지 실패하면 다음 확인이 불가능해진다
            log.error("실패 상태 저장 실패: %s", e_save, exc_info=True)
        return 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="백업 매니저 원샷 CLI 실행기")
    parser.add_argument("--profile", "-p", help="실행할 프로필 ID", default=None)
    args = parser.parse_args()
    sys.exit(run_cli_backup(args.profile))