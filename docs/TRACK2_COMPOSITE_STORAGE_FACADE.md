# Track 2-11: Composite Storage Adapter (Dual-Read Facade) 설계 및 검증 보고서

본 문서는 `BackupSystem`의 장기 저장소 아키텍처에서 기존의 **Individual Blobs (`blobs/xx/{sha256}.blob`)**와 신규 **Pack Container (`packs/*.pack` + `.idx`)**가 동일 저장소 내에 공존할 때, 상위 계층(`RestoreEngine`, `VerifyEngine`, `SnapshotEngine`)의 코드 수정 없이 투명하게 데이터를 조회·복원·검증하는 **`CompositeStorageFacade` (Dual-Read Adapter)**의 설계 및 실측 검증 보고서입니다.

---

## 1. 개요 및 설계 원칙

1. **상위 계층 무수정 (Zero Impact on Upper Layers)**:
   - `RestoreEngine`, `VerifyEngine` 등은 저장소 내부가 개별 파일인지 Pack Container인지 전혀 알 필요 없이 동일한 시그니처(`has_blob`, `extract_blob_to_file`, `verify_blob`)를 호출합니다.
2. **Dual-Read 우선순위 정책 (Pack-First Lookup)**:
   - **1단계**: 메모리에 로드된 Pack Index (`pack_index_map`)를 O(1) 해시 테이블로 조회.
   - **2단계**: Pack에 존재하는 경우 해당 Pack 파일의 바이트 슬라이스를 즉각 스트리밍 읽기.
   - **3단계 (폴백)**: Pack에 없는 경우 기존의 `blobs/xx/{sha256}.blob` 개별 파일시스템 경로를 확인하여 투명하게 폴백(Fallback).
3. **독립성 및 안전성 보장**:
   - `core/*` 프로덕션 코드는 일체 수정하지 않고 독립 디렉터리(`tests/benchmarks/prototypes/composite_storage/`)에서 구현 및 실측.
   - 실제 운영 저장소(`D:\MyBackup_Repository`) 접근 0건.

---

## 2. 실측 검증 결과 (`test_composite_facade.py`)

| 검증 시나리오 | 검증 조건 | 측정된 동작 및 무결성 결과 | 판정 |
| :--- | :--- | :--- | :---: |
| **Dual-Read & Fallback** | - Blob A: Individual Blob에만 존재<br>- Blob B: Pack Container에만 존재<br>- Blob C: 부재 | - `has_blob`: A/B 정상 True, C 정상 False<br>- `extract_blob_to_file`: A/B 100% 바이트 일치 복원<br>- `verify_blob`: A/B 정상 통과, C 정상 오류 검출 | **PASS (100%)** |
| **Pack Priority** | 동일한 Blob 해시가 Individual과 Pack 양쪽에 모두 중복 존재하는 경우 | Pack Container 인덱스를 우선 탐색하여 I/O를 집중시키고 투명하게 복원 완료 | **PASS (우선순위 준수)** |
| **Mixed Snapshot E2E Restore** | 단일 스냅샷 내 100개 파일 중:<br>- 50개: 과거 백업 (Individual)<br>- 50개: 신규 백업 (Pack Container) | - 100개 파일 전수 `verify_blob` 통과<br>- 100개 파일 전수 `extract_blob_to_file` 성공<br>- 100개 파일 전수 SHA-256 Bit-for-Bit 일치 | **PASS (100/100 복원)** |

---

## 3. 전체 회귀 및 프로토타입 통합 테스트 결과

```text
tests.benchmarks.prototypes.pack_consistency.test_pack_crash_consistency (5 tests) ... OK
tests.benchmarks.prototypes.composite_storage.test_composite_facade (3 tests) ... OK
tests.test_v239_verification (5 tests) ... OK
tests.test_self_healing (2 tests) ... OK

----------------------------------------------------------------------
Ran 15 tests in 2.891s
OK (Failures=0, Errors=0)
```

---

## 4. 결론 및 향후 전망

- **결론**: 기존 Individual Blob 저장소와 신규 Pack Container 저장소의 **공존 및 점진적 전환 경로(Gradual Migration Path)**가 완벽히 증명되었습니다.
- **아키텍처 의미**: 사용자는 기존의 기가바이트 단위 과거 백업을 무리하게 일괄 변환(Migration)할 필요가 없으며, 과거 데이터는 그대로 둔 채 신규 백업부터 Pack Container로 기록하더라도 단일 엔진에서 100% 무중단 복원/검증이 가능합니다.
- **다음 단계 (Track 2-12)**:
  - 읽기 및 저장소 복합 계층이 준비되었으므로, **Chunking Strategy(WholeFile / Fixed 4MB / FastCDC)를 실제 Ingest 파이프라인(`core/snapshot.py`)에 안전하게 주입(Injection)하는 파이프라인 결합 설계**로 진행할 수 있습니다.
