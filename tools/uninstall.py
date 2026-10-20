#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
백업시스템 v2.9.12 — G1-C 3단계 제거(Uninstall) 도구 (tools/uninstall.py)

독립 제거 도구. 코어 코드(core/, web/, run.py)를 전혀 수정하지 않고 배포/운영 계층에서 동작합니다.

정책:
  Policy 1 (소프트 언인스톨 / 기본 권장):
    - 작업 스케줄러 태스크('BackupSystem_AutoBackup') 삭제
    - 실행 중인 백업/웹 프로세스 안전 종료
    - 바로가기(.lnk) 및 임시 캐시(.tmp, __pycache__) 정리
    - [보존]: data/ 설정/프로필 및 D:\MyBackup_Repository 백업 저장소 완전 보존

  Policy 2 (완전 제거 / 설정 포함):
    - Policy 1의 모든 작업 수행
    - data/ 디렉터리 내 설정 및 프로필 삭제
    - logs/ 디렉터리 내 실행/에러 로그 삭제
    - [보존]: D:\MyBackup_Repository 백업 저장소 완전 보존

  Policy 3 (데이터 완전 파기 / 팩토리 리셋):
    - Policy 2의 모든 작업 수행
    - 백업 저장소(D:\MyBackup_Repository) 완전 파기
    - [이중 안전장치]: --confirm-destroy-all-backups 플래그 + 안전 확인 토큰('DESTROY-ALL-BACKUPS') 필수

모드:
  - 기본값: --dry-run (아무것도 삭제하지 않고 대상 리소스 상태 및 삭제/보존 계획만 시뮬레이션 보고)
  - 실제 실행: --execute 명시 필수
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from pathlib import Path
from typing import List, Optional, Dict, Any

# Windows 콘솔 UTF-8 설정 (CP949 출력 깨짐 방지)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 기본 상수 정의
# ---------------------------------------------------------------------------
APP_NAME = "백업시스템"
APP_VERSION = "v2.9.12"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASK_NAME = "BackupSystem_AutoBackup"
DEFAULT_REPO_DIR = Path(r"D:\MyBackup_Repository")
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
SAFETY_CONFIRMATION_TOKEN = "DESTROY-ALL-BACKUPS"


class Policy(IntEnum):
    P1 = 1  # 소프트 제거 (스케줄러, 바로가기, 프로세스 종료 / 설정·저장소 보존)
    P2 = 2  # 완전 제거 (P1 + 설정·로그 제거 / 저장소 보존)
    P3 = 3  # 데이터 완전 파기 (P2 + 백업 저장소 파기 / 2중 안전장치)


@dataclass
class ResourceItem:
    kind: str           # "task" | "process" | "shortcut" | "cache" | "data" | "logs" | "repository"
    name: str
    target_path: str = ""
    action: str = "PRESERVE"   # "DELETE" | "KILL" | "PRESERVE"
    status: str = "PLANNED"    # "PLANNED" | "SUCCESS" | "SKIPPED" | "FAILED"
    details: str = ""


@dataclass
class UninstallReport:
    policy: int
    dry_run: bool
    timestamp_iso: str
    items: List[ResourceItem] = field(default_factory=list)
    success: bool = True
    summary_message: str = ""


# ---------------------------------------------------------------------------
# 유틸리티 함수
# ---------------------------------------------------------------------------
def run_command(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **kwargs)
        out_b = proc.stdout or b""
        err_b = proc.stderr or b""
        try:
            out_s = out_b.decode("utf-8")
        except UnicodeDecodeError:
            out_s = out_b.decode("cp949", errors="replace")
        try:
            err_s = err_b.decode("utf-8")
        except UnicodeDecodeError:
            err_s = err_b.decode("cp949", errors="replace")
        return proc.returncode, out_s.strip(), err_s.strip()
    except Exception as e:
        return -1, "", str(e)


def find_shortcuts() -> List[Path]:
    """바탕화면 및 시작메뉴에서 백업시스템 관련 바로가기(.lnk, .url) 탐색"""
    shortcuts: List[Path] = []
    search_dirs = []

    user_desktop = Path(os.path.expanduser("~")) / "Desktop"
    if user_desktop.exists():
        search_dirs.append(user_desktop)

    public_desktop = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
    if public_desktop.exists():
        search_dirs.append(public_desktop)

    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if start_menu.exists():
        search_dirs.append(start_menu)

    for base_dir in search_dirs:
        try:
            for item in base_dir.rglob("*"):
                if item.is_file() and item.suffix.lower() in (".lnk", ".url"):
                    if "백업" in item.name or "backup" in item.name.lower():
                        shortcuts.append(item)
        except Exception:
            pass

    return shortcuts


def find_running_processes() -> List[Dict[str, str]]:
    """백업시스템 관련 실행 중인 프로세스(cli_backup.py, run.py, web/app.py) 탐색"""
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*cli_backup.py*' -or $_.CommandLine -like '*run.py*' -or ($_.CommandLine -like '*python*' -and $_.CommandLine -like '*백업시스템*') } | "
        "Select-Object ProcessId, Name, CommandLine"
    )
    code, out, _ = run_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd])
    procs = []
    if code == 0 and out:
        current_pid = ""
        current_name = ""
        current_cmd = ""
        for line in out.splitlines():
            line = line.strip()
            if not line:
                if current_pid:
                    procs.append({"pid": current_pid, "name": current_name, "cmd": current_cmd})
                    current_pid, current_name, current_cmd = "", "", ""
                continue
            if line.startswith("ProcessId"):
                current_pid = line.split(":", 1)[-1].strip()
            elif line.startswith("Name"):
                current_name = line.split(":", 1)[-1].strip()
            elif line.startswith("CommandLine"):
                current_cmd = line.split(":", 1)[-1].strip()
        if current_pid:
            procs.append({"pid": current_pid, "name": current_name, "cmd": current_cmd})
    return procs


# ---------------------------------------------------------------------------
# 언인스톨러 코어 엔진
# ---------------------------------------------------------------------------
class Uninstaller:
    def __init__(self, policy: Policy, dry_run: bool, repo_dir: Path = DEFAULT_REPO_DIR, safety_token: str = ""):
        self.policy = policy
        self.dry_run = dry_run
        self.repo_dir = repo_dir
        self.safety_token = safety_token
        self.report = UninstallReport(
            policy=int(policy),
            dry_run=dry_run,
            timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%S")
        )

    def plan_and_execute(self) -> UninstallReport:
        print("=" * 80)
        mode_str = "🔍 [DRY-RUN 시뮬레이션 모드 (실제 삭제 없음)]" if self.dry_run else "⚠️ [실제 제거(EXECUTE) 모드]"
        print(f" 🚀 {APP_NAME} {APP_VERSION} — G1-C 3단계 제거 도구")
        print(f" >> 모드: {mode_str}")
        print(f" >> 적용 정책: Policy {self.policy.value} ({self._policy_desc()})")
        print(f" >> 프로젝트 루트: {PROJECT_ROOT}")
        print(f" >> 백업 저장소: {self.repo_dir}")
        print("=" * 80)

        # 1. 태스크 스케줄러 점검 및 처리
        self._handle_scheduler_tasks()

        # 2. 실행 중 프로세스 점검 및 처리
        self._handle_running_processes()

        # 3. 바로가기 점검 및 처리
        self._handle_shortcuts()

        # 4. 임시 캐시 및 래퍼 점검 및 처리
        self._handle_caches()

        # 5. Policy 2 이상: data/ 설정 및 logs/ 로그 처리
        if self.policy >= Policy.P2:
            self._handle_data_and_logs()
        else:
            self._preserve_data_and_logs()

        # 6. Policy 3: 백업 저장소 처리 (이중 안전장치)
        if self.policy >= Policy.P3:
            self._handle_repository_destruction()
        else:
            self._preserve_repository()

        self._print_summary()
        return self.report

    def _policy_desc(self) -> str:
        if self.policy == Policy.P1:
            return "소프트 제거: 스케줄러/바로가기/프로세스 정리, 설정·저장소 보존"
        elif self.policy == Policy.P2:
            return "완전 제거: P1 + 설정/로그 삭제, 저장소 보존"
        elif self.policy == Policy.P3:
            return "데이터 파기: P2 + 저장소(D:\\MyBackup_Repository) 완전 파기"
        return "UNKNOWN"

    def _handle_scheduler_tasks(self):
        code, out, _ = run_command(["schtasks", "/Query", "/TN", DEFAULT_TASK_NAME])
        task_exists = (code == 0)
        item = ResourceItem(
            kind="task",
            name=DEFAULT_TASK_NAME,
            target_path=f"schtasks://{DEFAULT_TASK_NAME}",
            action="DELETE" if task_exists else "PRESERVE",
            status="PLANNED",
            details="등록된 Windows 자동 백업 작업" if task_exists else "미등록 상태"
        )
        if not task_exists:
            item.status = "SKIPPED"
            item.details = "태스크 미등록 (작업 불필요)"
        elif self.dry_run:
            item.status = "PLANNED"
            item.details = "삭제 대상 식별 (dry-run)"
        else:
            del_code, _, del_err = run_command(["schtasks", "/Delete", "/TN", DEFAULT_TASK_NAME, "/F"])
            if del_code == 0:
                item.status = "SUCCESS"
                item.details = "태스크 성공적으로 삭제됨"
            else:
                item.status = "FAILED"
                item.details = f"삭제 실패: {del_err}"
                self.report.success = False
        self.report.items.append(item)

    def _handle_running_processes(self):
        procs = find_running_processes()
        current_pid = str(os.getpid())
        target_procs = [p for p in procs if p.get("pid") != current_pid]

        if not target_procs:
            self.report.items.append(ResourceItem(
                kind="process",
                name="running_processes",
                action="KILL",
                status="SKIPPED",
                details="실행 중인 잔여 백업시스템 프로세스 없음"
            ))
            return

        for p in target_procs:
            pid = p.get("pid")
            name = p.get("name")
            cmd = p.get("cmd")
            item = ResourceItem(
                kind="process",
                name=f"{name} (PID: {pid})",
                target_path=cmd[:80] + "..." if len(cmd) > 80 else cmd,
                action="KILL",
                status="PLANNED",
                details="실행 중인 프로세스 감지"
            )
            if self.dry_run:
                item.status = "PLANNED"
                item.details = "종료 대상 식별 (dry-run)"
            else:
                kill_code, _, kill_err = run_command(["taskkill", "/F", "/PID", pid])
                if kill_code == 0:
                    item.status = "SUCCESS"
                    item.details = "프로세스 강제 종료 완료"
                else:
                    item.status = "FAILED"
                    item.details = f"종료 실패: {kill_err}"
            self.report.items.append(item)

    def _handle_shortcuts(self):
        shortcuts = find_shortcuts()
        if not shortcuts:
            self.report.items.append(ResourceItem(
                kind="shortcut",
                name="shortcuts",
                action="DELETE",
                status="SKIPPED",
                details="바탕화면/시작메뉴에 등록된 바로가기 없음"
            ))
            return

        for sc in shortcuts:
            item = ResourceItem(
                kind="shortcut",
                name=sc.name,
                target_path=str(sc),
                action="DELETE",
                status="PLANNED",
                details="바로가기 파일 발견"
            )
            if self.dry_run:
                item.status = "PLANNED"
            else:
                try:
                    sc.unlink(missing_ok=True)
                    item.status = "SUCCESS"
                    item.details = "바로가기 삭제 완료"
                except Exception as e:
                    item.status = "FAILED"
                    item.details = f"삭제 오류: {e}"
            self.report.items.append(item)

    def _handle_caches(self):
        cache_targets = [
            PROJECT_ROOT / ".pytest_cache",
            PROJECT_ROOT / "core" / "__pycache__",
            PROJECT_ROOT / "web" / "__pycache__",
            PROJECT_ROOT / "tools" / "__pycache__",
        ]
        found_any = False
        for c in cache_targets:
            if c.exists():
                found_any = True
                item = ResourceItem(
                    kind="cache",
                    name=c.name,
                    target_path=str(c),
                    action="DELETE",
                    status="PLANNED",
                    details="바이트코드 캐시 디렉터리"
                )
                if self.dry_run:
                    item.status = "PLANNED"
                else:
                    try:
                        shutil.rmtree(c, ignore_errors=True)
                        item.status = "SUCCESS"
                        item.details = "캐시 디렉터리 삭제 완료"
                    except Exception as e:
                        item.status = "FAILED"
                        item.details = f"삭제 실패: {e}"
                self.report.items.append(item)

        if not found_any:
            self.report.items.append(ResourceItem(
                kind="cache",
                name="cache_dirs",
                action="DELETE",
                status="SKIPPED",
                details="정리할 임시 캐시 없음"
            ))

    def _preserve_data_and_logs(self):
        # Policy 1: data/ 및 logs/ 보존
        self.report.items.append(ResourceItem(
            kind="data",
            name="data_directory",
            target_path=str(DATA_DIR),
            action="PRESERVE",
            status="SUCCESS",
            details="Policy 1 정책에 따라 설정/프로필(data/) 영구 보존"
        ))
        self.report.items.append(ResourceItem(
            kind="logs",
            name="logs_directory",
            target_path=str(LOGS_DIR),
            action="PRESERVE",
            status="SUCCESS",
            details="Policy 1 정책에 따라 실행 이력/로그(logs/) 영구 보존"
        ))

    def _handle_data_and_logs(self):
        # Policy 2 이상: data/ 및 logs/ 삭제
        for d, k, desc in [(DATA_DIR, "data", "설정 및 프로필"), (LOGS_DIR, "logs", "실행 이력 및 로그")]:
            if d.exists():
                item = ResourceItem(
                    kind=k,
                    name=d.name,
                    target_path=str(d),
                    action="DELETE",
                    status="PLANNED",
                    details=f"{desc} 디렉터리"
                )
                if self.dry_run:
                    item.status = "PLANNED"
                else:
                    try:
                        # 디렉터리 내부 파일들만 삭제하거나 디렉터리 전체 삭제
                        for child in d.iterdir():
                            if child.is_file():
                                child.unlink()
                            elif child.is_dir():
                                shutil.rmtree(child, ignore_errors=True)
                        item.status = "SUCCESS"
                        item.details = f"{desc} 디렉터리 내용물 정리 완료"
                    except Exception as e:
                        item.status = "FAILED"
                        item.details = f"정리 오류: {e}"
                self.report.items.append(item)
            else:
                self.report.items.append(ResourceItem(
                    kind=k,
                    name=d.name,
                    target_path=str(d),
                    action="DELETE",
                    status="SKIPPED",
                    details=f"{desc} 디렉터리 존재하지 않음"
                ))

    def _preserve_repository(self):
        # Policy 1, Policy 2: 백업 저장소(D:\MyBackup_Repository) 절대 보존
        repo_exists = self.repo_dir.exists()
        self.report.items.append(ResourceItem(
            kind="repository",
            name="backup_repository",
            target_path=str(self.repo_dir),
            action="PRESERVE",
            status="SUCCESS",
            details=f"Policy {self.policy.value} 절대 안전 원칙: 백업 원본 데이터 전량 영구 보존 (존재: {repo_exists})"
        ))

    def _handle_repository_destruction(self):
        # Policy 3: 백업 저장소 완전 파기 (초강력 2중 안전장치)
        if not self.repo_dir.exists():
            self.report.items.append(ResourceItem(
                kind="repository",
                name="backup_repository",
                target_path=str(self.repo_dir),
                action="DELETE",
                status="SKIPPED",
                details="백업 저장소가 존재하지 않아 삭제 불필요"
            ))
            return

        item = ResourceItem(
            kind="repository",
            name="backup_repository",
            target_path=str(self.repo_dir),
            action="DELETE",
            status="PLANNED",
            details="백업 저장소(스냅샷, CAS blob, metadata.db) 파기 대상"
        )

        # 안전장치 1: 확인 토큰 일치 여부
        if self.safety_token != SAFETY_CONFIRMATION_TOKEN:
            item.status = "SKIPPED"
            item.details = f"❌ [안전 차단] 확인 토큰 불일치 (입력: '{self.safety_token}' != 필수: '{SAFETY_CONFIRMATION_TOKEN}'). 저장소 파기 거부됨."
            self.report.items.append(item)
            self.report.success = False
            return

        if self.dry_run:
            item.status = "PLANNED"
            item.details = "안전 확인 완료 (dry-run: 실제 삭제는 스킵됨)"
        else:
            try:
                shutil.rmtree(self.repo_dir, ignore_errors=False)
                item.status = "SUCCESS"
                item.details = "백업 저장소 완전 파기 완료 (팩토리 리셋)"
            except Exception as e:
                item.status = "FAILED"
                item.details = f"저장소 삭제 실패: {e}"
                self.report.success = False

        self.report.items.append(item)

    def _print_summary(self):
        print("\n" + "=" * 80)
        print(" 📋 [제거 계획 및 결과 요약]")
        print("=" * 80)
        for item in self.report.items:
            icon = "✅" if item.status == "SUCCESS" else ("🔍" if item.status == "PLANNED" else ("⏭️" if item.status == "SKIPPED" else "❌"))
            act_str = f"[{item.action}]"
            print(f" {icon} {act_str:10s} {item.kind:10s} {item.name:25s} | {item.details}")
            if item.target_path:
                print(f"    └─ 경로: {item.target_path}")

        print("=" * 80)
        final_status = "SUCCESS (완료)" if self.report.success else "FAILED (오류/차단)"
        print(f" 최종 결과: {final_status}")
        print("=" * 80)


# ---------------------------------------------------------------------------
# CLI 엔트리포인트
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION} G1-C 3단계 제거 도구")
    parser.add_argument("--policy", type=int, choices=[1, 2, 3], default=1,
                        help="제거 정책 수준: 1 (소프트 제거, 기본값), 2 (완전 제거, 설정 포함), 3 (데이터 완전 파기)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="시뮬레이션 모드 (기본 활성, --execute가 없을 경우 자동 적용)")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="실제 삭제 실행 (명시하지 않으면 dry-run으로 작동)")
    parser.add_argument("--repo-dir", type=str, default=str(DEFAULT_REPO_DIR),
                        help="백업 저장소 경로 (기본값: D:\\MyBackup_Repository)")
    parser.add_argument("--confirm-destroy-all-backups", action="store_true", default=False,
                        help="Policy 3 실행 시 필수 플래그")
    parser.add_argument("--safety-token", type=str, default="",
                        help=f"Policy 3 실행 시 필수 안전 문자열 ('{SAFETY_CONFIRMATION_TOKEN}')")
    parser.add_argument("--json", action="store_true", default=False,
                        help="결과를 JSON 포맷으로 출력")

    args = parser.parse_args()

    # --execute가 명시되지 않으면 무조건 dry-run
    is_dry_run = not args.execute or args.dry_run

    policy = Policy(args.policy)
    repo_dir = Path(args.repo_dir)

    # Policy 3 추가 안전 검증
    safety_token = args.safety_token
    if policy == Policy.P3 and not is_dry_run:
        if not args.confirm_destroy_all_backups:
            print(f"❌ [안전 오류] Policy 3을 실행하려면 --confirm-destroy-all-backups 플래그가 필요합니다.", file=sys.stderr)
            sys.exit(1)
        if not safety_token:
            # 대화형 프롬프트 시도
            if sys.stdin.isatty():
                print("⚠️ [경고] Policy 3은 모든 백업 원본 데이터를 영구 파기합니다!")
                confirm = input(f"진행하려면 '{SAFETY_CONFIRMATION_TOKEN}'을 정확히 입력하십시오: ").strip()
                safety_token = confirm
            else:
                print(f"❌ [안전 오류] 비대화형 환경에서는 --safety-token '{SAFETY_CONFIRMATION_TOKEN}' 인자가 필요합니다.", file=sys.stderr)
                sys.exit(1)

    uninstaller = Uninstaller(
        policy=policy,
        dry_run=is_dry_run,
        repo_dir=repo_dir,
        safety_token=safety_token
    )
    report = uninstaller.plan_and_execute()

    if args.json:
        report_dict = asdict(report)
        print("\n[JSON_OUTPUT_START]")
        print(json.dumps(report_dict, indent=2, ensure_ascii=False))
        print("[JSON_OUTPUT_END]")

    if not report.success:
        sys.exit(1)


if __name__ == "__main__":
    main()
