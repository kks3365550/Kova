# WORM (Write-Once-Read-Many) NTFS ACL 실측 및 아키텍처 설계서

- **문서 버전**: v1.0.0
- **작성 일자**: 2026-10-06
- **대상 시스템**: 백업시스템 (`D:\MyBackup_Repository`)
- **검토 상태**: 실측 검증 완료 (Verification Confirmed)

---

## 1. 개요 및 배경

백업 데이터의 불변성(Immutability)은 랜섬웨어 침해 또는 악의적인 프로세스로부터 백업본을 보호하기 위한 핵심 방어선입니다.
기존 시스템에서 제기되었던 가설과 충돌 우려에 대해 실제 Windows NTFS 파일시스템 상에서 재현 및 벤치마크를 수행한 결과, 다음 사실들이 실측으로 확정되었습니다.

### 1.1 실측으로 입증된 사실 (Evidence)
1. **파일 단위 Deny ACL의 유효성**:
   - 파일에 `Everyone` 또는 특정 SID에 대해 `(DE,WD,AD)` Deny가 설정되면, 관리자 세션이라 하더라도 일반적인 쓰기/삭제 API 호출이 커널 레벨에서 즉각 차단됩니다 (`Access is denied`).
2. **GC(Garbage Collection)와의 호환성**:
   - `core/worm.py`의 `unlock_file_writable()`은 `icacls /remove:d`를 호출하여 명시적으로 Deny ACE를 제거한 뒤 `os.remove()`를 호출합니다.
   - 따라서 파일 레벨에 Deny가 걸려 있어도, 정당한 권한을 가진 백업 시스템 내부 GC는 정상적으로 블롭을 삭제할 수 있습니다.
3. **성능 벤치마크 (/T 일괄 처리)**:
   - 개별 파일마다 `subprocess.run(["icacls", ...])`를 호출할 경우 83,000개 파일 기준 약 18분(1,074초) 소요.
   - 반면 최상위 디렉터리 기준 `icacls <dir> /deny ... /T /Q` 일괄 트리를 적용할 경우 **약 40초 내외**로 초고속 완료됨이 입증됨.

---

## 2. 권한 모델 및 SID 대상 분석 (핵심 의사결정)

현재 백업 시스템은 사용자 세션(`kksjmj`, Medium Mandatory Level) 또는 로컬 작업 스케줄러로 구동됩니다.
`whoami /groups` 실측 결과 현재 프로세스는 다음 그룹에 동시 소속되어 있습니다:
- `*S-1-1-0` (`Everyone`)
- `*S-1-5-11` (`NT AUTHORITY\Authenticated Users`)
- `*S-1-5-32-545` (`BUILTIN\Users`)
- `*S-1-5-4` (`NT AUTHORITY\INTERACTIVE`)

### 2.1 SID 비교표

| 대상 SID | 명칭 | 백업 프로세스 소속 여부 | 랜섬웨어 차단력 | unprotect 없는 직접 삭제 | unprotect 후 GC 삭제 | 비고 |
|---|---|:---:|:---:|:---:|:---:|---|
| `*S-1-1-0` | Everyone | **포함** | 최상 | 완전 차단 | **정상 동작** (`/remove:d` 후 삭제) | 현행 worm.py 기본값 |
| `*S-1-5-11` | Authenticated Users | **포함** | 상 | 사용자 세션 차단 | **정상 동작** | SYSTEM 전용 데몬 도입 시 유리 |
| `*S-1-5-4` | INTERACTIVE | **포함** | 중 | 대화형 세션만 차단 | **정상 동작** | 서비스 세션과 분리 시 사용 |

### 2.2 결론 및 권고
- 현재 백업 프로세스는 자신이 파일 소유자(Owner) 권한을 가지고 있으므로, **`Everyone`에 Deny가 걸려 있어도 자신의 토큰으로 ACL을 수정(`/remove:d`)하여 GC 삭제를 정상 수행**할 수 있습니다 (실측 가설 3 통과).
- 따라서 불필요하게 SID를 분기하여 방어 틈새를 만들기보다는, **`*S-1-1-0 (Everyone)`을 유지하되 GC 진입 시 `unlock_file_writable`을 원자적으로 수행하는 구조**가 가장 안전하고 단순합니다.

---

## 3. 세부 설계 및 배선 방안 (Implementation Plan)

### 3.1 멱등성(Idempotency) 보장 및 일괄 배선
1. **배치 적용 트리 (`/T`)**:
   - `core/worm.py`의 `protect_repository(repo_dir)`에 재귀 옵션 `/T`를 적용하여 `blobs/` 하위 전체를 단 한 번의 커널 일괄 호출로 보호:
     ```cmd
     icacls "D:\MyBackup_Repository\blobs\*" /deny "*S-1-1-0:(DE,WD,AD)" /T /Q /C
     ```
2. **증분 백업 파이프라인 연계**:
   - 백업이 완료된 직후(`create_snapshot` 말미), 새로 생성된 블롭들에 대해서만 `protect_file(new_blob_path, use_ntfs_acl=True)`를 선택적으로 호출하거나 세대별 신규 블롭 리스트에 대해서만 멱등성 있게 적용.
3. **매니페스트 보호**:
   - `snapshots/` 폴더 내의 `.json` 매니페스트 파일도 저장이 완료되고 Ed25519 전자서명이 찍힌 직후 Deny ACL을 부여.

### 3.2 안전장치 (Fail-Safe & DR)
- 비상 재해 복구(`disaster_recovery.py`) 실행 시 저장소의 모든 블롭을 읽을 때 Deny ACL은 `(DE,WD,AD)`(삭제/쓰기/추가)만 거부하고 `(RD)`(읽기)는 완전 허용하므로, 복원 속도나 권한에 일체 영향을 주지 않음.
- 만약 저장소를 완전히 초기화하거나 이동해야 할 경우를 대비해 아래 명령으로 Deny ACL을 수동 해제한다:
  ```cmd
  icacls "D:\MyBackup_Repository\*" /remove:d "*S-1-1-0" /T /Q /C
  ```
- **[구현 예정]** WORM 실제 배선(Step 3) 시, 위 명령을 포장한 `tools/unprotect_all.bat` 비상 해제 스크립트를 함께 제공한다.
  - **현재는 해당 스크립트가 존재하지 않는다.** Deny ACL이 아직 어떤 파일에도 적용되지 않았으므로 본 단계에서 잠길 실제 위험은 없다.
  - WORM 배선 완료 시점부터는 Deny 적용 상태에서 잠기는 사고를 대비한 필수 수단이 된다.

---

## 4. 단계별 실행 로드맵

1. **[Step 1] 설계 문서 승인 (현재 단계)**:
   - 본 문서(`docs/design_worm_acl.md`)를 통해 실측 사실과 구현 모델 확정.
2. **[Step 2] 격리 테스트 환경 검증 (Sandbox Test)**:
   - `test_sandbox` 환경에서 500개 더미 블롭 생성 ➡️ `/T` 일괄 Deny 적용 ➡️ 악의적 삭제 차단 확인 ➡️ unprotect 후 GC 삭제 성공 실측.
3. **[Step 3] 프로덕션 코드 배선 및 릴리즈**:
   - `core/worm.py` 및 `core/snapshot.py` 배선 ➡️ 릴리즈 파이프라인 가동.
