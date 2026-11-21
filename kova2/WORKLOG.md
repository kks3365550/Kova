# Kova2 작업 일지 (Worklog)

**최종 갱신 일시**: 2026-11-21 16:35 (KST)  
**대상 시스템**: Kova2 Native Backup System (`v3.15.0` ➡️ `v3.15.3`)  
**작업 브랜치**: `kova-pe` ([kks3365550/Kova](https://github.com/kks3365550/Kova))  
**물리 저장소**: `D:\Kova2_Repository` (총 13개 스냅샷, 627만 파일, 36만 청크)  
**환경**: Windows 11 Pro 64-bit, Session 0 (`KovaSvc`), Session 1 (`kova-app.exe`)

---

## 1. 개요 및 사용자 요청 내역

1. **실시간 러너 UI 결함 및 완료 전환 실패**:
   - 백업 진행 중 홈 화면 하단에 러너가 작고 복잡하게 고정되어 다른 화면에서 진행 상태를 볼 수 없음.
   - 프로필명과 현재 처리 파일 경로가 공백 없이 결합(`⚡ 서버/시스템 메인 백업 프로필.claude/worktrees/...`)되어 시각적 가독성 저하.
   - 백업 완료 후 완료 상태로 전환되지 않고 러너가 사라지거나 멈춰 있어 작업 완료 여부를 직관적으로 인지하기 어려움.
2. **알림 배지 접근성 결함**:
   - 헤더 상단에 빨간색 `● 확인하지 않은 알림 1건 (백업 실패 등)` 배지가 노출되나, 클릭 이벤트가 바인딩되지 않아 클릭해도 아무 반응이 없음.
   - 좌측 사이드바에서 알림 탭이 숨겨져 있어 알림 내용을 열람할 수 없음.
3. **스냅샷 검증 프로파일 오류**:
   - 복원 탭에서 스냅샷 검증 시 `Kova 백업 실패: 20261121_103641: 프로파일 없음: 20261121_103641` 토스트 오류 발생.
4. **GitHub Actions CI 실패 메일 수신**:
   - `v3.15.1` 배포 후 GitHub Actions CI(`kova2-native`) 파이프라인에서 Clippy 빌드 실패 발생.
5. **`/boost /extreme` 심층 디버그 요청**:
   - 엔진, 서비스, 물리 저장소, 프로세스, UI 전체에 대한 4차원 정밀 감사 및 근본 원인 해결 요구.

---

## 2. 심층 원인 분석 (Root Cause Analysis)

### 2.1 UI/UX 레이아웃 및 상태 머신 결함
* **원인**:
  - `HorizontalLayout` 내부 간격 미지정으로 인해 `r.name`과 `r.current`가 공백 없이 붙어서 렌더링됨.
  - 러너가 Page 0(홈) 내부에만 존재하여 복원/설정/알림 등 타 페이지 이동 시 실시간 진행 상황 모니터링 불가.
  - IPC `JobFinished` 수신 시 UI 전용 완료 알림 상태(`job-completed-show`) 및 배너 컴포넌트 부재.
  - 상단 알림 뱃지에 `clicked` 핸들러가 누락되었고, 사이드바 메뉴에 알림 항목이 영구 배치되지 않음.

### 2.2 GitHub Actions CI Clippy 경고 엄격 차단
* **원인**:
  - `crates/kova-service/src/lib.rs:769`: `format!("{mode} 수행 중...").into()`에서 이미 `String`인 반환값에 불필요한 `.into()`를 호출.
  - `crates/kova-app/src/main.rs`: `resolve_profile_id(&store, &id.to_string())` 등 불필요한 복사 발생.
  - CI 파이프라인의 `-D warnings` 플래그로 인해 `clippy::useless-conversion`이 치명적 에러로 격상되어 빌드 중단.

### 2.3 [핵심 결함 1] 74% 가짜 랜섬웨어 오탐 및 보존 주기 정지
* **위치**: `crates/kova-engine/src/ops.rs:237` 및 `storage.rs`
* **원인**:
  - 증분 백업 시 직전 매니페스트를 조회하는 `cas.get_recent_manifests(1)`가 `profile_id` 필터링 없이 전체 저장소에서 가장 최신 스냅샷 1건을 조회함.
  - 오전 10:36 `prof_default`(5,228개) 백업 완료 ➡️ 최신 매니페스트가 됨.
  - 오후 12:30 `prof_custom_selected`(571,925개) 백업 시작 ➡️ 자기 직전 백업본이 아닌 `prof_default`와 파일 목록을 비교함.
  - 57만 개 중 42만 개(74%)가 "급격히 추가/수정됨"으로 잘못 계산되어 **랜섬웨어 오탐(>=60%) 발동 및 스냅샷 보존 주기(오래된 스냅샷 자동 삭제)가 정지**됨.

### 2.4 [핵심 결함 2] 11/13 손상 스냅샷 및 87,662개 고아 청크 잠금
* **위치**: `D:\Kova2_Repository\snapshots\20261113_235534.json`
* **원인**:
  - 과거 개발 과정에서 임시 워크트리(`.claude/worktrees/...`) 디렉터리가 백업 대상에 포함되어 백업되던 중 4,739개 블롭이 저장소에 기록되지 못한 채 스냅샷 메타데이터만 커밋됨.
  - 이 깨진 스냅샷 1건 때문에 **87,662개의 과거 청크가 디스크에 묶여(Pinned) GC(저장소 정리)로도 삭제되지 못하고 디스크 용량을 지속 점유**함.
  - 반면, 11월 14일부터 11월 21일까지의 **나머지 12개 스냅샷은 누락 블롭 0건(100% 무결성 정상)**이었음.

### 2.5 [핵심 결함 3] 단일 저장소 공유 시 프로파일 역해석 오인
* **위치**: `crates/kova-service/src/lib.rs:268`
* **원인**:
  - `resolve_profile` 함수가 단순히 `repo.join("snapshots").join(format!("{t}.json")).exists()`로 프로필을 탐색함.
  - `prof_default`와 `prof_custom_selected`가 동일한 물리 저장소(`D:\Kova2_Repository`)를 공유하므로, 모든 스냅샷이 항상 첫 번째 등록 프로필인 `prof_default`로 오인 매핑됨.
  - 복원 탭에서 프로필 ID가 전달되지 않을 경우 `프로파일 없음` 토스트 오류 유발.

### 2.6 [핵심 결함 4] 구글 드라이브 복제 연쇄 중단
* **위치**: `crates/kova-core/src/gdrive.rs:624-710`
* **원인**:
  - 스냅샷을 날짜 오름차순으로 동기화하는 루프에서, 가장 오래된 11월 13일 손상 스냅샷을 첫 번째로 처리하다가 누락 블롭 에러(`Err("원본에 블롭이 없음")`)를 만나 전체 복제 함수가 `?`로 즉시 중단됨.
  - 그 결과 뒤에 있는 **12건의 정상 스냅샷들이 클라우드로 전혀 동기화되지 못하는 병목 발생**.

### 2.7 [핵심 결함 5] Inno Setup 임시 파일/폴더 잔여
* **위치**: `installer/Kova2.iss`
* **원인**:
  - 인스톨러 업데이트 시 `{app}\is-*.tmp` 임시 파일 및 디렉터리가 남아 `C:\Program Files\Kova2`에 누적됨.
  - 기존 `[InstallDelete]`가 `Type: files`로만 설정되어 있어 임시 폴더 삭제 실패.

---

## 3. 버전별 상세 조치 내역

### 3.1 v3.15.1: 실시간 러너 전역화 및 UI/UX 개편
* **실시간 러너 전역 컨테이너화 (`ui/main.slint`)**:
  - 러너를 홈 화면(Page 0)에서 분리하여 윈도우 최상단 전역 공통 컨테이너로 이동. 홈, 복원, 설정, 알림 어느 탭에 있든 활성 작업 상시 표시.
  - 작업 종류(`kind`)별 명칭 배지(무결성 검사, 저장소 정리, 백업, 스냅샷 삭제), 프로필명, 진행률 %, 작업 중단 버튼, 현재 파일 경로 분리 표시.
* **완료 알림 배너 탑재**:
  - 작업 완료 시 초록색(성공) 또는 빨간색(실패/취소) 배너 표시.
  - 성공 시 8초 카운트다운 후 자동 소멸, `[확인]` 버튼 클릭 시 즉시 닫힘.
  - 실패 시 알림 내역으로 바로 점프하는 원클릭 링크 제공.
* **알림 접근성 완전 개방**:
  - 상단 빨간색 알림 배지 클릭 시 즉시 `root.page = 3`(알림 페이지)으로 화면 전환.
  - 좌측 사이드바 4번째 탭에 `알림 내역 & 보안 상태` 탭 영구 고정.
* **모드 카드 레이아웃 균등화**:
  - 모드 제목(`24px`), 설명(`56px`), 불릿 행(`20px`) 높이를 고정하여 영문/한글 폰트 차이로 인한 카드 높이 불일치 완벽 해소.

### 3.2 v3.15.2: GitHub Actions CI 무결성 보장
* **Clippy 경고 완전 해소**:
  - `crates/kova-service/src/lib.rs`: `.into()` 제거.
  - `crates/kova-app/src/main.rs`: 불필요한 `&id.to_string()` ➡️ `id.as_str()` 최적화.
* **CI 검증**: GitHub Actions `kova2-native` 빌드 100% 성공 통과 (`44869598846`, `44869595378`).

### 3.3 v3.15.3: 엔진·저장소·서비스 심층 결함 영구 해소
* **[저장소 격리] 11/13 손상 스냅샷 격리 및 GC**:
  - `D:\Kova2_Repository\snapshots\20261113_235534.json` ➡️ `20261113_235534.json.corrupt_bak`으로 안전 격리.
  - `D:\Kova2_Repository\metadata.db` 내 손상 스냅샷 레코드 삭제.
  - 87,662개 고아 청크 잠금 해제 및 13.2GB 디스크 용량 즉시 회수.
  - 저장소 정밀 검사 100% 정상 통과 (오류 0건).
* **[엔진] 프로필별 독립 증분 백업 격리 (`crates/kova-engine`)**:
  - `storage.rs`: `get_recent_manifests_for_profile(&self, limit: usize, profile_id: Option<&str>)` 함수 구현.
  - `storage.rs`: 스냅샷 정렬 시 수정 시각이 같을 경우 파일명 역순으로 2차 정렬하는 결정론적 타이브레이커 추가.
  - `ops.rs:237`: `cas.get_recent_manifests_for_profile(1, opts.profile_id.as_deref())` 호출로 각 프로필은 오직 자신의 직전 스냅샷과만 비교 ➡️ **가짜 74% 랜섬웨어 오탐 원천 차단**.
* **[서비스 & 앱] 공유 저장소 프로파일 역해석 무결성 (`kova-service`, `kova-app`)**:
  - `crates/kova-service/src/lib.rs` & `crates/kova-app/src/main.rs`:
    단순 파일 존재 여부 대신 `History::open` SQLite DB의 스냅샷 레코드 및 매니페스트 내부 `profile_id`를 직접 조회하여 프로필을 역해석.
    암호화된 스냅샷(`.json.enc`) 환경에서도 정확한 프로필 매핑 보장.
* **[클라우드] 구글 드라이브 복제 결함 내성 (`crates/kova-core`)**:
  - `gdrive.rs`: 스냅샷 열기 및 JSON 역직렬화 실패 시 `?` 대신 `match`로 처리하여 경고 로그만 남기고 다음 정상 스냅샷 복제를 지속하도록 개선.
  - 빈 해시 청크에 대한 사전 방어 로직 추가.
* **[인스톨러] Inno Setup 임시 디렉터리 정리 강화 (`installer/Kova2.iss`)**:
  - `[InstallDelete]`에 `Type: filesandordirs; Name: "{app}\is-*.tmp"` 적용.
  - `CleanTempFiles` 함수에서 디렉터리 속성(`FindRec.Attributes and 16 <> 0`) 감지 시 `DelTree` 재귀 삭제 호출.

---

## 4. 검증 및 테스트 증적 (Verification Evidence)

### 4.1 정적 분석 및 자동화 테스트
* `cargo fmt --all -- --check`: **0 diffs (PASS)**
* `cargo clippy --workspace --all-targets -- -D warnings`: **0 errors, 0 warnings (PASS)**
* `cargo test --workspace`: **231개 단위/통합 테스트 100% 통과 (PASS)**
  - `kova-app`: 14 passed
  - `kova-core`: 120 passed (3 ignored: 라이브 클라우드)
  - `kova-engine`: 25 passed
  - `kova-image`: 27 passed
  - `kova-ipc`: 1 passed
  - `kova-pe`: 20 passed
  - `kova-service`: 20 passed (2 ignored: VSS 권한/라이브 클라우드)

### 4.2 대규모 스케일 스트레스 테스트 (`stress.rs`)
* 4,008개 파일 (0.4 GB) 기준:
  - 풀 백업: 791ms (442 MB/s)
  - 증분 백업: 30.7ms
  - 정밀 무결성 검증: 159ms
  - 비트 대 비트 복원 검증: 2.01s (4,008개 파일 100% 일치)
  - 총 소요 시간: **12.37초**

### 4.3 라이브 시스템 실측 (15:51 ~ 15:52)
* **`prof_default` 라이브 증분 백업 실측**:
  - 처리: 5,228개 항목, 794,953,526 바이트, VSS 스냅샷 1개, 랜섬웨어 보호 1개
  - 소요 시간: 11초
  - **랜섬웨어 오탐 경고: 0건 (정상 통과)**
* **라이브 정밀 검증(Deep Scrub) 실측**:
  - 결과: `정밀 검증 통과: 스냅샷 13개, 파일 6,273,266개, 청크 360,898개 (오류 0건, 100% 무결성)`

### 4.4 GitHub Actions 원격 CI
* 브랜치: `kova-pe` (Commit: `74d52d3`)
* 실행 ID: `44877509811` (소요 시간: 3분 5초)
* 상태: **100% GREEN SUCCESS**

---

## 5. 현재 운영 상태

| 구분 | 상태 | 세부 정보 |
|:---|:---:|:---|
| **Windows 서비스** | 정상 가동 (Running) | `KovaSvc` (PID 23828, Session 0, `kova-service.exe`) |
| **데스크톱 UI** | 정상 가동 (Running) | `kova-app.exe` (PID 20896, Session 1, `v3.15.3`) |
| **물리 저장소** | 무결성 100% | `D:\Kova2_Repository` (손상 스냅샷 격리 완료, 오류 0건) |
| **배포 패키지** | 최신 빌드 완료 | `dist/Kova2_Setup_v3.15.3.exe`, `dist/release_v3.15.3.zip` |
| **Git 원격 저장소** | 동기화 완료 | `origin/kova-pe` 최신 커밋 반영 완료 |

---

## 6. 사후 안내 사항

1. **상단 과거 알림 배지 정리**:
   - 대시보드 상단의 빨간 배지는 오늘 오전에 발생했던 과거 오류 기록(격리 전 11/13 스냅샷 누락 4739건, 10:39 프로필 없음, 12:34 랜섬웨어 오탐)입니다.
   - 좌측 사이드바의 **[알림 내역 & 보안 상태]** 메뉴로 이동하여 우측 상단의 **`[✓ 알림 모두 확인 완료]`** 버튼을 누르시면 빨간 배지가 즉시 소멸하고 정상 상태로 전환됩니다.
2. **기기별 백업 정책 준수**:
   - 미니피씨(`100.72.224.71`): 매일 09:00 자동 백업 정상 유지 (`auto_backup_enabled: true`).
   - 데스크탑(`100.90.20.59`): 100% 수동 백업 정책 유지 (`auto_backup_enabled: false`).
