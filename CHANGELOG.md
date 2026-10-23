# Changelog

이 문서는 BackupSystem의 릴리스 이력을 기록한다.
형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따른다.

---

## [2.17.19] - 2026-10-23

### 수정
- 휴지통 정리(GC): 스냅샷 매니페스트에 `"chunks": null` 이 있으면 GC 전체가 중단되던 문제
- 로깅: `logging.getLogger()` 로 만든 모듈 로거(`core.verify`, `BackupSystem.VSS` 등)가
  `logs/backup.log` 에 기록되지 않아 일정(pythonw) 실행에서 오류가 사라지던 문제.
  진입점(`cli_backup`, `web.app`)에서 전체 로거를 파일에 연결
- `tools/release.py`: 커밋 메시지에 `release:` 접두어가 두 번 붙던 문제

### 테스트
- 테스트 실행이 실제 `logs/backup.log` 에 가짜 오류를 쌓던 문제 수정 (`tests/conftest.py`)
- GC null chunks, 로깅 연결, 로그 격리, 릴리즈 커밋 메시지 테스트 추가

---

## [2.17.18] - 2026-10-23

### 수정
- 조용히 무시되던 오류에 로그 추가: 스냅샷 WORM 보호·복제 큐·오프사이트 복제,
  복원 시 레지스트리 가져오기, 팩 인덱스 복구, 드라이브 인증 정보 로드,
  통백업 아카이브 메타 읽기·저장 경로 대체·캐시 갱신·VHDX 분리

### 테스트
- 설치/업그레이드 E2E (서명 검증, 사용자 데이터 보존, 업그레이드 후 기동)
- Rust 엔진 단위 테스트 9건, `hasher`·`snapshot_cache`·`archive_worker`·
  `windows_task`·`registry_backup` 단위 테스트
- 시스템 호출을 건드리던 테스트(VSS 등) 격리, 서명 키가 없는 환경에서도 통과

### 빌드·CI
- CI: Python 3.11/3.12/3.14 매트릭스, Rust `cargo test`, 업데이트 패키지 dry-run 빌드,
  실패를 가리던 `continue-on-error` 제거, 액션 버전 갱신
- 의존성: 운영/개발 분리(`requirements-dev.txt`), 검증된 버전 고정(`constraints.txt`)
- `tools/release.py --dry-run` 추가

### 문서
- TRACK2 설계 문서를 `docs/` 로 이동, `release_notes.md`·`known_issues.md` 보관 표시

---

## [2.17.17] - 2026-10-22

### 추가
- 스케줄러 자동 백업 실패 시 재시도 (최대 3회, 실패 후 30분 간격).
  사용자 취소는 재시도·알림 대상에서 제외, 한도 초과 시 정상 스케줄로 복귀
- Rust 엔진 백업 결과에 자동 복원 검증(샘플 20개) 적용. 실패 시 백업을 실패 처리
- 백업 실패 시 Windows 토스트 알림
- `tests/test_scheduler_retry.py` (7건)

### 수정
- `support_scope.md` 의 자동 업데이트 확인 대상을 Firebase → GitHub Releases 로 정정

### 문서
- `SECURITY.md` 추가 (취약점 비공개 신고 절차)
- 구 감사·매니페스트 보고서에 보관 문서 안내 추가

---

## [2.17.16] - 2026-10-20
### 수정
- 공개키 개행을 LF 로 정규화하고 `.gitattributes` 추가

## [2.17.15] - 2026-10-20
### 보안
- `get_trusted_public_key_paths` 를 v2 키로 고정, 인스톨러(ISS) `InstallDelete` 로 v1 키 제거
- 암호화 구조 문서를 실제 동작 기준으로 정정

## [2.17.14] - 2026-10-20
### 수정
- `test_offsite` 에서 `_get_rclone_exe` 를 모킹해 GitHub Actions 러너 실패 해소

## [2.17.13] - 2026-10-20
### 보안
- v2 Ed25519 루트 신뢰 앵커 강제, `target`·바이너리 도구 추적 해제
- `app_settings.json.example` 보안 처리, 클라우드 암호화 문서화

## [2.17.12] - 2026-10-20
### 변경
- 라이선스를 MIT 로 전환 (기존 Enterprise Proprietary)
- 사실 기반으로 README 재작성, `.gitignore` 강화, 저장소 정리

## [2.17.11] / [2.17.10] - 2026-10-19
### 수정
- 증분 백업의 신규/수정 오탐: 다중 스냅샷 인덱스 합집합 + 5ms 타임스탬프 허용 오차

## [2.17.9] - 2026-10-18
### 수정
- rclone 오프사이트 동기화에서 `backup.lock`·임시 파일 제외, `prune_storage` 가 청크 지원

## [2.17.8] - 2026-10-18
### 수정
- 증분 기준 스냅샷 로딩 및 다중 스키마 중복 필드 역직렬화

## [2.17.7] - 2026-10-18
### 수정
- Rust 증분 백업 스키마 호환, 완료 시 UI 100% 멈춤 현상

## [2.17.6] - 2026-10-18
### 추가
- Rust 엔진 mtime 기반 증분 패스트패스
### 수정
- 대형 스냅샷에서 메모리 정지

## [2.17.5] - 2026-10-18
### 추가
- 사전 검증과 지연시간 SLA 를 갖춘 Zero-Flaw 릴리즈 게이트

## [2.17.4] - 2026-10-18
### 변경
- `get_storage_stats` 지연 6s → 0.02s (메모리 TTL 캐시 + 스마트 샘플링)

## [2.17.3] - 2026-10-18
### 수정
- `test_backup_system.py` 저용량 안전장치 단언을 Fail-Closed 정책에 맞춤

## [2.17.2] - 2026-10-18
### 수정
- Windows 11 작업표시줄에서 트레이 앱이 보이지 않던 문제

## [2.17.1] - 2026-10-18
### 변경
- 베어메탈 클론에 동적 2단계 스트리밍과 Rust IOCP 엔진 적용

## [2.17.0] - 2026-10-18
### 변경
- 핵심 백업 엔진을 Rust 엔진(`kova_fast_engine`)으로 교체 (실패 시 Python 엔진 폴백)

## [2.15.40] ~ [2.15.36] - 2026-10-17 ~ 10-18
### 수정
- 외부 hermes venv 의존 제거 (러너·인스톨러) (2.15.40)
- 통백업 엔진 명칭을 실제 형식인 LBA 블록 스트리밍(.qblk)으로 정정 (2.15.39)
- 통백업 패널 높이 균등화, VHDX 중복 박스 제거 (2.15.38)
- 전체 페이지 2열 레이아웃, 통백업 아카이브 삭제 기능 (2.15.37)
- `retention.py` ISO 8601 타임스탬프 파싱, OS 백업 상태 자동 갱신 (2.15.36)

> 참고: v2.13.8 ~ v2.15.35 구간은 저장소 이력 정리로 GitHub Release 노트가 남아 있지 않아
> 2.13.8 이후 상세 이력은 복원하지 못했다.

---

## [2.13.8] - 2026-10-08 ~ 10-09

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
