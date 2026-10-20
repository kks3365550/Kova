# Changelog

이 문서는 BackupSystem의 릴리스 이력을 기록한다.
형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따른다.

---

## [2.13.7] - 2026-10-07

### 변경
- `web/app.py` 모듈화: 1,741 → 436줄
  - `web/paths.py` (경로 상수), `web/version.py` (버전·업데이트 캐시),
    `web/state.py` (전역 실행 상태 단일 소유자),
    `web/api_auth.py` (인증 6개), `web/api_snapshots.py`,
    `web/api_update.py`, `web/api_backup.py`
  - API 경로 계약 유지 (`tests/test_api_contract.py` 로 검증)
- 스케줄러 백업 로그 파이프라인 (`core/logging_setup.py`, `logs/backup.log`)
  - `pythonw.exe` 에는 콘솔이 없어 `print()` 65줄이 전부 버려지던 문제 해결
- `core/retention.py`: 목록 읽기 실패 상태를 `no_snapshots` → `list_failed` 로 정정
- `GET /api/backup/logs` + 대시보드 "마지막 스케줄러 백업 로그 보기" 버튼
- `tests/test_updater.py` 가 배포 서명 키 대신 테스트용 키를 생성하도록 변경
- `data/profiles.json`·`data/auth_config.json` 추적 해제 (런타임 상태·비밀번호 해시)
- 레거시 `backup/v2.9.10/` 추적 해제 (개인키 포함)

---

## [2.13.6] - 2026-10-06

### 변경
- 카카오톡 알림 모듈 및 호출부 제거 (`core/notifier.py`, `kakao_setup_helper.py`)
- 알림 실패 시 백업 결과에 영향을 주던 예외 처리 정리

---

## [2.13.5] - 2026-10-06

### 변경
- WORM 방어 한계를 검증된 사실만 남기도록 문서화 (`docs/design_worm_acl.md`)
- `disaster_recovery.py` 의 죽은 코드(`common_prefix`) 제거
- 배치·스크립트 개행 CRLF 정규화
- `scratch/` 테스트 산출물 git 추적 해제 (49개)

---

## [2.13.4] - 2026-10-06

### 수정
- **CRITICAL** `BackupLock` 이 프로세스 내 다른 스레드의 진행 중 백업을 무혈입 통과시키는 결함
  → 재진입 키를 `(repo_dir, thread_id)` 로 분리
- **CRITICAL** `ed25519_signature` 생성 실패가 조용히 무효화되던 문제
  → `raise RuntimeError` 로 백업 중단 (Fail-Closed)
- **HIGH** `auth_config.json` 손상 시 전체 API 가 무인증으로 개방되던 문제
  → 손상 감지 시 HTTP 500 Fail-Closed 차단
- **HIGH** 마스터 암호 최소 4자리 + 로그인 무제한 시도
  → 최소 8자리 + 5회 실패 시 5분 락아웃 (HTTP 429)
- **MEDIUM** 삭제·보존 권한 기본값이 fail-open 이던 문제
  → `authorized=False` 로 통일
- WORM 보호 주장 철회 및 오염된 테스트 산출물 정리

---

## [2.13.3] - 2026-10-06

### 수정
- **MEDIUM** 스냅샷 전자서명 실패를 로그만 남기고 진행하던 문제
  → 보안 경고 로깅 및 UI 콜백 통지
- **LOW** `put_bytes_blob` 임시 파일명 고정 → PID + 랜덤 hex 로 통일
- **LOW** 프로필 보존 기간(`retention_days`) 미명시 → 60일 명시

### 참고
- WORM 디렉토리 DeleteChild ACL 적용 시도는 이후 `2.13.4` 에서 철회됨

---

## [2.13.2] - 2026-10-06

### 수정
- **HIGH** 인증 Fail-Open (설정 파일 손상 시 API 무인증 개방)
- **HIGH** 마스터 암호 최소 길이 4자리 → 8자리
- **HIGH** 로그인 시 무제한 무차별 대입 가능 → 5회/5분 락아웃

---

## [2.13.1] - 2026-10-06

### 수정
- **CRITICAL** GC(정리)와 백업 실행 간 동시성 경합
  → `BackupLock` 재진입 지원 + Prune/GC 전역 락 적용
- **CRITICAL** 매니페스트 손실 시 `except: pass` 로 무음 무시되어 정상 블롭이 영구 삭제되던 문제
  → Fail-Closed 로 즉시 중단

### 추가
- `pytest` 를 `requirements.txt` 에 명시

---

## [2.13.0] - 2026-10-06

### 추가
- Track 2-17: 유휴 시간 Pack Consolidation 및 Delayed GC

---

## [2.12.0] - 2026-10-06

### 추가
- Track 2-16: Pack Container 프로덕션 정식 활성화, Dual-Read 지원

---

## [2.11.0] - 2026-10-05

### 추가
- Track 2-12~2-14: Multi-chunk Ingest 및 Assembly 결합, Zero-Touch 릴리즈

---

## 미배포 (main 브랜치, v2.13.7 이후)

### 수정
- `web/api_backup.py`·`web/api_update.py` 의 중복 import 를 모듈 상단으로 통합
  (pyflakes 는 중복을 잡지 못하므로 한쪽만 수정하면 조용히 어긋난다)
- FastAPI 버전 선언이 `"2.9.22"` 로 굳어 있던 문제 → `VERSION` 파일 참조
  (`GET /openapi.json` 의 버전이 실제 릴리즈와 일치)
- `tools/scan_secrets.py`·`tools/rotate_release_key.py` 의 cp1252 크래시
  (windows-latest 러너의 표준출력은 cp1252라 한국어 출력이 죽었다.
  stdout 재설정 + CI 에 `PYTHONIOENCODING: utf-8` 명시)
- CI 게이트의 PowerShell `\` 줄바꿈 문법 오류 → 한 줄 명령으로 수정

### 추가
- **보안 감사** (`docs/SECURITY_20261008.md`): 공개 저장소 이력에서 실제
  credential 5종 발견 (배포 서명 개인키·마스터 해시 2종·Firebase 키·사내 계정·테스트 키).
  익명 클론으로 실증 확인. 키 회전 없이 이력 정리만 하면 안심만 생긴다.
- **비밀 스캐너** (`tools/scan_secrets.py`) + CI `secret-scan` 잡
  (추적 파일 검사. 이력 전체 검사는 수동 ` --history` 로만 실행한다.
  기존 유출 31건 때문에 게이트에 넣으면 CI 가 영구히 빨갛게 된다)
- **다중 공개키 검증** (`core/crypto_sign.verify_bytes_ed25519_any`,
  `core/updater.get_trusted_public_key_paths`): 전환 기간에 구·신 키 동시 신뢰.
  키 교체 없이는 이력 정리가 무의미하므로 선행 조건을 먼저 만들었다
- **키 회전 도구** (`tools/rotate_release_key.py`, dry-run 기본) +
  회귀 테스트 `tests/test_key_rotation.py` (12건)
- CI 게이트에 `test_key_rotation.py` 추가. 현재 CI 4잡 전부 통과

### 아직 안 된 것 (사람 조치 필요)
- 마스터 비밀번호 변경 (대시보드에서 즉시. 유출 해시 무효화)
- Firebase 키 폐기 (콘솔. 키가 살아있고 유효함 — HTTP 200 확인)
- 배포 키 재발급 판단 (다중키 지원은 됐으나 기존 설치본 호환성 검토 필요)
- 이력 정리 (`git filter-repo`. 키 회전 후에야 의미 있음)

[2.13.7]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.7
[2.13.6]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.6
[2.13.5]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.5
[2.13.4]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.4
[2.13.3]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.3
[2.13.2]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.2
[2.13.1]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.1
[2.13.0]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.13.0
[2.12.0]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.12.0
[2.11.0]: https://github.com/kks3365550/BackupSystem/releases/tag/v2.11.0
