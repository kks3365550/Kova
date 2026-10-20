# 제품화 진행 상황 — 2026-10-07

## 1. 시작 상태 (제품화 관점 평가)

"제품화 단계까지 쭉 진행해보자" 요청으로 시작.

### 그 시점의 평가

| 항목 | 상태 |
|---|---|
| 핵심 기능 | 완료 |
| 복구 능력 | 실증 완료 (오프사이트 9,474파일 복원) |
| 무결성 검증 | 강력 (Ed25519 + 블롭 해시 자동 검증) |
| 테스트 | **194개 중 1 failed, 1 정지** |
| CI | 없음 |
| 라이선스 | 없음 (README만 "Enterprise Proprietary" 명시) |
| pytest 설정 | 없음 |

**판정**: 사내 도입용으로 양호, 외부 판매용으로 3개 갭.

---

## 2. 해결한 것

### 2.1 테스트 결함 7건

| 결함 | 원인 | 조치 |
|---|---|---|
| `test_updater.py::test_check_for_update_detection` | v2.9.10 시절 **Firebase 형식**으로 작성. v2.10.1에서 GitHub Releases API로 전환되며 깨짐 | GitHub API 형식(`tag_name` + `assets[]`)으로 테스트 갱신 |
| `test_encryption_chaos.py::test_18` | 복원 경로 계약 미인지 (dest 루트 하위 가정) | `os.walk` 탐색으로 실제 계약에 맞춤 |
| `test_encryption_chaos.py::test_22` | 동일 | 동일 |
| `test_v241_features.py` 3건 | **`authorized` 기본값을 `True`→`False`로 바꾼 영향** | 정리 루틴에 `authorized=True` 명시 |
| `test_updater_v2_operational_e2e.py` 4건 (ERROR) | `dist/BackupSystem_v2.9.11_RC1.bundle` 하드코딩. RC1은 v2.11.0부터 생성되지 않음 | 자체 키쌍 + 기준 번들 생성으로 자립화 |

**특히 `test_v241_features` 3건은 어제 제가 `authorized` 기본값을 바꾼 영향**이었습니다. fail-open → fail-close는 올바른 보안 방향이지만, 그 계약을 전제하던 테스트가 조용히 실패하고 있었습니다. 테스트 10곳에 `authorized=True`를 명시했습니다.

### 2.2 `test_updater_v2_operational_e2e` 자립화 상세

4단계로 고쳤습니다.

```
1단계: RC1 번들 경로 하드코딩 제거
       → 자체 키쌍(Ed25519) + 기준 번들 생성으로 대체

2단계: BundleCreator 가 요구하는 정확한 5개 엔트리 사용
       policy.json / policy.sig / manifest.json / manifest.sig / package.zip

3단계: PolicyDocument / ArtifactManifest 스키마 준수
       필수 필드 7개 + canonicalize() 서명

4단계: package.zip 을 실제 유효한 ZIP 로 구성
       + run.py, core/updater_v2/transaction.py 등 실제 파일 포함
       (더미를 담으면 preflight 훅이 설치되지 않아 검증 실패)
```

### 2.3 추가 산출물

| 파일 | 내용 |
|---|---|
| `pytest.ini` | testpaths, addopts, 마커 정의. `benchmarks` 자동 제외 |
| `.github/workflows/ci.yml` | 3개 잡: 정적 분석 / 회귀 테스트 / 빌드 |
| `LICENSE` | Enterprise Proprietary (README와 일치, 8개 조항) |

---

## 3. 발견한 결함 (오늘 새로)

### 🔴 `replication_queue`의 WinError 5

```
core/replication.py:145   os.replace(tmp_path, remote_path)   →  WinError 5
```

**근본**: `lock_file_immutable()` 이 ReadOnly 를 설정하면 Windows 에서
`os.replace()` 로 ReadOnly 대상 파일을 덮어쓸 수 없습니다.

```powershell
icacls 확인 결과:
  NT AUTHORITY\SYSTEM:(I)(F)
  BUILTIN\Administrators:(I)(F)
  OWNER RIGHTS:(I)(F)          ← chmod 시 Windows 가 자동 부여
```

**재현**:
```
lock_file_immutable(blob)  → ReadOnly 설정
os.replace(tmp, blob)      → WinError 5 (접근 거부)
```

**영향**:
| 경로 | 결과 |
|---|---|
| 최초 복제 | 성공 (대상 파일 없음) |
| **재실행 / 재시도** | **실패** (대상이 이미 ReadOnly) |
| 운영 | 영향 없음 — robocopy 경로 사용 |
| 테스트 | tearDown 이 `rmtree(ignore_errors=True)` 로 후킹 |

**미수정 상태**: WORM 철회 결정과 연결되어 있어 별도 검토 필요.
`docs/` 에 결함으로 기록하고, 당장은 운영 영향이 없어 보류했습니다.

---

## 4. 테스트 실행 방식

```powershell
$py = "C:\Users\kksjmj\AppData\Local\Python\pythoncore-3.14-64\python.exe"
cd "C:\Users\kksjmj\Desktop\ai\백업시스템"

# 전체 (약 20분)
& $py -m pytest -q

# CI 게이트용 빠른 subset (약 5분)
& $py -m pytest -q `
    tests/test_critical_fixes.py `
    tests/test_offsite.py `
    tests/test_p0_fail_closed.py `
    tests/test_disaster_scenarios.py `
    tests/test_crypto_at_rest.py `
    tests/test_auth.py `
    tests/test_repository_chaos.py `
    tests/test_encryption_chaos.py `
    tests/test_updater.py

# 정적 분석
$files = @(Get-ChildItem core -Filter *.py | % { $_.FullName }) + `
         @("$PWD\web\app.py", "$PWD\cli_backup.py", "$PWD\cli.py", "$PWD\run.py")
& $py -m pyflakes @files
```

**주의**: pytest 는 프로젝트 `.venv` 가 아니라 **시스템 Python 3.14** 에 있습니다.

---

## 5. 남은 제품화 갭

| 항목 | 상태 | 규모 |
|---|---|---|
| CI 실행 확인 | 파이프라인 작성만 함, GitHub Actions 미실행 | push 후 확인 |
| `replication_queue` WinError 5 | 미수정 | 1줄 |
| pytest 를 venv 에 설치 | 미완 | 5분 |
| CHANGELOG | 없음 | 30분 |
| 로깅 표준화 | 파일별 제각각 | 2~3시간 |
| `web/app.py` 1,551줄 분리 | 단일 파일 | 4~6시간 |

---

## 6. 외부 판매 시 추가로 필요

| 항목 | 비고 |
|---|---|
| 실제 설치/업그레이드 E2E | 현재 CI 는 Windows-latest 에서만 실행 |
| 다중 버전 매트릭스 | Python 3.11 / 3.12 / 3.13 |
| 성능 벤치마크 기준선 | `tests/benchmarks/` 존재하나 CI 미연결 |
| 장애 모드 매뉴얼 | `support_scope.md` 존재하나 상업 배포판 미작성 |
| 취약점 대응 정책 | SECURITY.md 부재 |

---

작성: 2026-10-07
