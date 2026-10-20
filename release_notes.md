# CAS BackupSystem 릴리즈 노트

## [v2.10.3] - 2026-10-01 (아키텍처 정밀 교정 및 Firebase 완전 제거)

### 🧹 클린 아키텍처 (Clean Purge)
- **Firebase 외부 클라우드 의존성 완전 제거**:
  - `core/firebase_sync.py` 및 프론트엔드 `web/static/js/backup_firebase.js` 영구 삭제.
  - 웹 대시보드 내 실시간 파이어베이스 관제 카드, 동기화 모달, 잔여 API 정리.
  - 사내 폐쇄망 및 로컬/사설망 환경에 최적화된 자립형 아키텍처 정립.

### 🏛️ 문서화 및 기술 명세 정밀 교정
- **3계층(3-Tier) 복구 아키텍처 명문화**:
  - **Tier 1 (CAS)**: Zero-Lock VSS 및 CAS 기반의 일상 파일/레지스트리 시점 복원.
  - **Tier 2 (BMR)**: Windows Native(`wbadmin -allCritical`) 및 WinRE 기반 전체 디스크 시스템 이미지 복구 연동.
  - **Tier 3 (DR)**: 표준 라이브러리 기반 무설치 `disaster_recovery.py`를 통한 독립 무결성 감사 및 긴급 복원.
- **엔지니어링 기술 설명 정밀화**:
  - VSS Crash-Consistent 시점 백업 표기 정밀화 (strict mode 시 일관성 미보장 백업 거부).
  - 22만 개 파일 실측 벤치마크 환경 명시 (Warm Cache 평균 11.46ms).
  - NTFS ACL 및 소프트웨어 수준 Immutable/WORM 보호 기술 명확화.

---

## [v2.10.0] - 2026-10-01 (초저지연 메타데이터 캐시 릴리즈)

### ⚡ 성능 개선 (Performance Breakthrough)
- **`SnapshotMetadataCache` 엔진 신설 (~11ms 초저지연 달성)**:
  - `core/snapshot_cache.py`: 인메모리 스냅샷 매니페스트 캐시 계층 구현.
  - 디렉터리 핑거프린트(mtime + 파일 개수) 기반의 무효화(Invalidation) 안전망 탑재 (0.05ms 판별).
  - 22만 개 파일 실측 환경에서 `/api/snapshots` 조회 지연을 2,947ms에서 **11.46ms(약 257배 가속, 99.6% 지연 절감)**로 대폭 단축.
  - 캐시 히트 시 0.077ms 수준의 O(1) 초고속 응답 달성.

---

## [v2.9.23] - 2026-10-01 (Tier 1 저위험 성능 최적화 릴리즈)

### ⚡ 성능 개선 (Performance Optimizations - Tier 1)
- **`/api/snapshots` N+1 쿼리 병목 완전 제거 (배치 쿼리 도입)**:
  - `core/replication_queue.py`: `ReplicationQueueManager.get_status_batch()` 메서드 신설. SQLite 변수 한계 방어를 위한 `CHUNK_SIZE=500` 안전 분할 적용.
  - `web/app.py`: `list_snapshots()`에서 스냅샷마다 매번 수행하던 단건 쿼리를 저장소당 1회의 배치 조회로 전환하여 DB 부하 및 왕복 레이턴시 대폭 감축.
- **`core/hasher.py` 중복 Win32 파일 타입 커널 조회 제거**:
  - `get_file_stat()`에서 이미 획득한 `st.st_mode` 비트마스크(`stat.S_ISDIR`, `stat.S_ISREG`)를 재사용하여 파일 탐색 시 매 파일마다 발생하던 중복 `os.path.isdir`, `os.path.isfile` 시스템 콜 2회 제거.
  - 심볼릭 링크/정션 포인트는 `stat.S_ISLNK` 및 안전한 예외 처리를 결합하여 무한 재귀 및 순환 참조 방어력 유지.

---

## [v2.9.22] - 2026-10-01 (DEF-03 종결 정식 패치 릴리즈)

### 🚀 결함 해결 (Resolved Defect)
- **[DEF-03] 바탕화면 바로가기 Cold-Start 연결 거부 결함 완전 해결 (`Self-Gated Browser Launch`)**:
  - `launch_dashboard.vbs`: 임의 3초 대기 루프 및 VBScript 내부 브라우저 강제 호출 제거. 백그라운드 서버 깨우기만 수행하고 0.01초 만에 즉시 종료.
  - `run.py`: Uvicorn/FastAPI 서버 소켓이 8765 포트에 BIND & LISTEN 된 것을 자체적으로 확인한 바로 그 순간에만 `open_browser(8765)`를 직접 호출하도록 상태 전이 재설계.
  - 브라우저 오픈과 서버 준비 간의 레이스 컨디션을 원천 차단하여 `127.0.0.1:8765` 연결 거부(`ERR_CONNECTION_REFUSED`) 발생 가능성 0% 달성.
  - 8대 검증 시나리오(콜드스타트, 빠른 연타, 지연 부팅, 2차 실행 인스턴스, `--silent` 부팅, 부팅 실패 다이얼로그 등) 전수 통과.

---

## [v2.9.21] - 2026-09-30 (DEF-02 패치 릴리즈)

### 🚀 결함 해결 (Resolved Defect)
- **[DEF-02] 재부팅 시 백업시스템 웹 대시보드 창 2개 중복 팝업 결함 완전 해결**:
  - 구버전 Inno Setup 및 작업 스케줄러 중복 실행 방지
  - Windows Named Mutex 기반 단일 인스턴스 보호 도입
  - 부팅 자동시작 시 `--silent` 모드 적용
