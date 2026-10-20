# Track 2-9 다음 단계: Pack Container 프로덕션 통합 설계서
(Production Integration Design & Storage Coexistence Architecture)

> **[원칙 및 제약 사항 준수]**
> - 기존 `core/*` 프로덕션 코드는 일체 수정하지 않습니다.
> - 기존 BackupSystem 저장소 데이터의 수정/삭제 및 migration 실행을 엄격히 금지합니다.
> - 새로운 대규모 벤치마크나 불필요한 테스트를 추가하지 않고, 순수 통합 설계 및 호환성 아키텍처를 확정합니다.

---

## 1. 현재 Individual Storage (Whole-file CAS) 구조 및 결합도 진단

실제 `core/*` 프로덕션 소스코드를 분석한 결과, 백업 파이프라인 전반이 개별 블롭 파일 레이아웃(`blobs/xx/{sha256}.blob`)에 깊게 결합되어 있음을 확인했습니다.

| 모듈 및 클래스 | 핵심 역할 | 현재 저장 레이아웃 결합도 | 구체적 결합 지점 및 의존성 분석 |
| :--- | :--- | :---: | :--- |
| **`core.storage.BlobStorage`** | 물리 데이터 저장 및 읽기 | **극도로 높음 (Tight)** | - `get_blob_abs_path(hash)`: `blobs/xx/{hash}.blob` 물리 경로를 직접 생성하여 외부에 노출.<br>- `has_blob`: `os.path.exists(blob_path)` 파일 존재 여부 호출.<br>- `put_file_blob_onepass`: 개별 `.blob` 임시 파일 생성 후 `os.replace` 원자적 이동. |
| **`core.snapshot.SnapshotEngine`** | 스냅샷 생성 및 증분 Diff | **중간 (Medium)** | - `entries` 목록에 `blob_id` 및 `sha256` 식별자로 기록하여 논리적으로는 추상화됨.<br>- 그러나 내부의 `bulk_add_blob_cache` 및 이전 스냅샷 기반 diff 검사 시 `storage.has_blob`에 직접 의존. |
| **`core.restore.RestoreEngine`** | 스냅샷 기반 파일 복원 | **높음 (High)** | - `storage.extract_blob_to_file(blob_id, dest_path)` 호출.<br>- 해당 메서드 내부에서 `get_blob_abs_path`를 통해 개별 블롭 파일을 직접 `open()`하여 압축 해제함 (Pack에서 꺼내는 로직 전무). |
| **`core.verify.IntegrityVerifier`** | 무결성 자동 검증 | **높음 (High)** | - `verify_blob(sha256_hash)`가 `storage.get_blob_abs_path`로 개별 블롭 파일을 직접 열어 매직 바이트(`ZSTD_MAGIC`, `ENC\x01`) 확인 및 해시 대조. |
| **`core.retention.RetentionManager`** | 보존 주기 및 스토리지 정리 | **높음 (High)** | - 스냅샷 삭제 후 `SnapshotEngine.prune_storage()` ➡️ `storage.prune_unreferenced_blobs()` 호출.<br>- `blobs/` 디렉토리를 `os.walk`하여 미참조 `.blob` 개별 파일을 `os.remove`로 직접 삭제함. |

---

## 2. Pack Container 구조 및 프로토타입 실측 요약

Track 2-9 프로토타입에서 검증된 Pack Container 레이아웃의 구조적 특징:
- **물리 구조**: 1GB 단위 대형 컨테이너(`packs/pack_0000.bin`, `pack_0001.bin`...) + SQLite 인덱스 DB(`pack_index.db`).
- **바이너리 레코드 (45B Header/Footer)**:
  `[Magic: 4B ("BKPK")]` + `[Version: 1B]` + `[Binary SHA-256: 32B]` + `[Payload Length: 4B]` + `[Payload Data]` + `[CRC32: 4B]`
- **확정된 실측 사실 (10GB / 2,560개 청크)**:
  1. 원본 100% Bit-for-Bit 복원 무결성 달성.
  2. 물리 파일 개수 **2,560개 ➡️ 12개**로 99.5% 극적 축소 (Windows NTFS MFT 파편화 해소).
  3. 무작위 100회 읽기(Random Read) 속도 **9.5% 개선** (파일 핸들 오픈 오버헤드 감소).
  4. 2회 중복 Ingest 시 신규 저장 바이트 **0B** (완벽한 인덱스 재사용).
  5. Truncated/Corrupted 데이터 감지 시 **Fail-Closed** 보장.
  6. **현재의 한계**: SQLite 단건 커밋으로 인해 Ingest 속도(52.3초) 및 순차 복원(53.0초)이 개별 파일(18.3초 / 41.8초)보다 느림.

---

## 3. Storage Abstraction Boundary (추상화 경계 정의)

핵심 원칙: **청킹(Chunking) 및 스냅샷/복원 코드는 대상이 "개별 파일"인지 "Pack 파일"인지 전혀 알지 않아야 한다.**

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Backup / Restore Business Logic                      │
│        (SnapshotEngine, RestoreEngine, IntegrityVerifier)              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Logical Chunk API
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                     CompositeChunkStore (Facade)                       │
│  - has_chunk(chunk_id) -> bool                                         │
│  - get_chunk_stream(chunk_id) -> BinaryIO                              │
│  - put_chunk(chunk_id, data) -> StoredResult                           │
│  - verify_chunk(chunk_id) -> VerifyResult                              │
└─────────────────┬────────────────────────────────────┬─────────────────┘
                  │ Miss / Fallback                    │ Primary
                  ▼                                    ▼
┌──────────────────────────────────┐  ┌──────────────────────────────────┐
│      IndividualFileStore         │  │        PackContainerStore        │
│   (기존 blobs/xx/{hash}.blob)    │  │   (신규 packs/*.bin + index.db)  │
└──────────────────────────────────┘  └──────────────────────────────────┘
```

---

## 4. 공존 가능한 Target Architecture (Coexistence & Dual-Read)

기존 데이터를 파괴하거나 강제로 변환하지 않고 구형 스냅샷과 신규 스냅샷이 공존하는 **2중 읽기(Dual-Read) / 단일 쓰기(Pack-Write)** 아키텍처를 채택합니다.

```text
[과거 스냅샷 (Snapshot A)]  ──► entries: [ {blob_id: "hash1"}, {blob_id: "hash2"} ]
                                              │
                                              ▼ (Pack index 조회 -> Miss)
                                       IndividualFileStore (blobs/xx/hash1.blob 읽기)

[신규 스냅샷 (Snapshot B)]  ──► entries: [ {blob_id: "hash3"}, {blob_id: "hash4"} ]
                                              │
                                              ▼ (Pack index 조회 -> Hit)
                                       PackContainerStore (packs/pack_0001.bin 에서 seek/read)
```

### 공존 읽기 파이프라인 (Composite Read Algorithm)
1. 복원 또는 검증 시 `CompositeChunkStore.get_chunk_stream(hash)`를 호출.
2. **1차 조회**: `PackContainerStore`의 인덱스 DB/인메모리 캐시 조회 ➡️ 존재하면 컨테이너에서 오프셋 `seek/read`.
3. **2차 조회 (Fallback)**: Pack에 없으면 `IndividualFileStore` 경로(`blobs/xx/{hash}.blob`) 확인 후 스트리밍 읽기.
4. 양쪽 모두 없으면 `ChunkNotFoundError` 발생.

---

## 5. Snapshot / Restore Compatibility (호환성 보장)

- **스냅샷 메타데이터 하위 호환성**:
  - 기존 `SnapshotManifest`의 `entries` 구조는 그대로 유지됩니다:
    `{"rel_path": "...", "size": 1024, "mtime": ..., "sha256": "...", "blob_id": "..."}`
  - 개별 파일이든 Pack이든 `blob_id`는 내용 해시(SHA-256)이므로 기존 JSON 스키마를 100% 호환합니다.
- **다중 레이아웃 혼합 복원 (Mixed Restore)**:
  - 스냅샷 A(Individual), 스냅샷 B(Pack), 스냅샷 C(혼합)가 존재하더라도 복원 엔진은 `CompositeChunkStore`를 통해 청크 출처와 무관하게 동일한 스트림으로 파일을 완벽히 복원합니다.

---

## 6. 기존 데이터 보존 전략 (Migration 배제 원칙)

1. **인-플레이스 마이그레이션(In-Place Migration) 영구 배제**:
   - 기존 저장소의 수십만 개 `.blob` 파일을 Pack으로 강제 변환하는 마이그레이션 스크립트를 일체 실행하지 않습니다.
   - 마이그레이션 도중 전원 차단, 디스크 용량 부족, 인덱스 꼬임이 발생하면 과거 백업 체인 전체가 파괴될 수 있기 때문입니다.
2. **자연 퇴역(Natural Retirement via Retention) 원칙**:
   - 기존 Individual 블롭들은 스냅샷 보존 주기(Retention Policy, 예: 30일/60일)에 따라 오래된 스냅샷이 만료되면서 점진적·자연적으로 삭제되도록 둡니다.
   - 신규 생성되는 백업만 Pack Container에 쌓이므로, 시간이 흐르면 저장소가 자연스럽게 순수 Pack 체제로 수렴합니다.

---

## 7. Pack Container 미해결 9대 과제 상태 및 위험도 진단

프로덕션 투입 전에 반드시 해결/검증되어야 하는 9대 과제의 정량 평가입니다:

| 번호 | 미해결 과제 항목 | 현재 상태 | 위험도 | 프로덕션 통합 전 필수 여부 | 상세 위험 및 기술적 요구사항 |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **01** | **SQLite Batch Commit** | 미구현 (단건 커밋) | **높음 (High)** | **[필수]** | 청크당 단건 INSERT로 인해 Ingest 시간이 52초로 지연됨. 배치 커밋(예: 500개 단위) 적용 필수. |
| **02** | **WAL (Write-Ahead Logging)** | 미적용 | **높음 (High)** | **[필수]** | 쓰기 중 비정상 종료 시 인덱스 DB 손상 방지 및 읽기-쓰기 동시성 보장을 위해 `PRAGMA journal_mode=WAL` 필수. |
| **03** | **Pack Rotation** | 기본 1GB 롤링 | **낮음 (Low)** | [선택/보통] | 1GB 도달 시 다음 번호(`pack_0001.bin`)로 자동 분할되는 기본 로직은 프로토타입에서 동작 확인됨. |
| **04** | **Crash Recovery** | 기본 감지 | **높음 (High)** | **[필수]** | 쓰기 도중 크래시 발생 시, Pack 끝에 남은 불완전 레코드를 자동으로 Truncate(복구)하고 인덱스와 일치시키는 복구 루틴 필수. |
| **05** | **Compaction / GC** | 미구현 | **중간 (Medium)** | [초기 보류 가능] | 스냅샷 삭제 시 Pack 내부 청크 회수 로직. 초기에는 "스냅샷 만료 시 완전히 비어있는 Pack 파일 통째 삭제" 수준으로 시작 가능. |
| **06** | **Concurrent Writer Lock** | 프로세스 단일 락 | **높음 (High)** | **[필수]** | `SnapshotEngine`의 다중 워커 스레드(8~16 스레드)가 단일 Pack 파일에 쓸 때 스레드 세이프 락(`threading.Lock`) 보장 필수. |
| **07** | **Orphan Pack 처리** | 미구현 | **중간 (Medium)** | [선택/보통] | 인덱스 DB에는 없는데 디스크에 남아있는 미아 Pack 파일 정리 루틴. |
| **08** | **Index-Pack 불일치 복구** | 미구현 | **높음 (High)** | **[필수]** | 인덱스 DB가 손상되었을 때 Pack 파일들을 처음부터 순차 스캔하여 `pack_index.db`를 100% 재생성(Rebuild)하는 도구 필수. |
| **09** | **Snapshot 삭제 시 참조 청크 처리**| 개별 파일만 지원 | **높음 (High)** | **[필수]** | 현재 `prune_unreferenced_blobs`가 개별 파일만 지우므로, Pack 인덱스에서 청크 참조 카운트를 감소시키는 로직 연동 필수. |

---

## 8. Production Integration 단계적 로드맵

- **Phase 1 (추상화 계층 도입)**:
  `core/storage.py` 내부에 `StorageAdapter` 기반의 `CompositeChunkStore`를 먼저 도입하고, 기본 구현체는 기존 `IndividualFileStore`로 유지하여 프로덕션 안정성 무영향 배포.
- **Phase 2 (Pack Store 인프라 안정화)**:
  위 9대 과제 중 필수 6대 항목(Batch Commit, WAL, Crash Recovery, Concurrent Lock, Index Rebuild, Retention 연동)을 프로토타입 디렉토리에서 완결.
- **Phase 3 (신규 스냅샷 Pack 전환)**:
  설정 플래그(`use_pack_container=True`)를 통해 신규 스냅샷부터 Pack으로 기록 시작하고, 복원/검증 엔진의 듀얼 리드(Dual-Read) 가동.

---

## 9. 최종 결론 및 판정

### 💡 [최종 판정]: **"B. Storage abstraction을 먼저 최소 수정한 뒤 통합 가능"**

### 판정 근거 (core/* 코드 및 실측 기준):
1. **현재 구조에 바로 통합 불가 (A 배제)**:
   - `core/restore.py`, `core/verify.py`, `core/retention.py`가 `storage.get_blob_abs_path()`라는 개별 파일 경로에 직접 결합되어 있어, Pack Container를 바로 밀어 넣으면 복원, 무결성 검증, 스토리지 정리가 즉시 붕괴됩니다.
   - 또한 Ingest 트랜잭션 지연(52초)과 Crash Recovery 부재 상태에서는 프로덕션 안정성을 보장할 수 없습니다.
2. **프로토타입 영구 유지 불필요 (C 배제)**:
   - 10GB 단일 파일만으로도 2,560개 파일이 생성되는 Windows NTFS의 한계가 명확히 실측되었으며, Pack Container가 파일 개수를 99.5% 줄이고 Random Read를 개선한다는 물리적 가치가 이미 입증되었습니다.
3. **Storage Abstraction 선행 도입의 정당성 (B 선택)**:
   - 기존 코드를 파괴하지 않고 `CompositeChunkStore` 인터페이스를 통해 구형 Individual 저장소와 신규 Pack 저장소를 듀얼 리드로 감싸는 최소 추상화를 먼저 구축하는 것이 가장 안전하고 결함 없는 최적의 아키텍처 전환 경로입니다.
