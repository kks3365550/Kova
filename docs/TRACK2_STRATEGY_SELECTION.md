# Track 2-7: 파일별 청킹 전략 선택 정책 설계서
(Chunking Strategy Selection Policy & Adapter Architecture)

> **[설계 원칙 및 제약 사항 준수]**
> - 본 문서는 순수 아키텍처 및 선택 정책 설계 문서이며, 기존 `core/*` 프로덕션 코드를 일체 수정하지 않습니다.
> - Track 2-1 ~ Track 2-6의 **실측 데이터(Measured Evidence)**를 팩트 베이스로 삼아 작성되었습니다.
> - 청킹 알고리즘 선택(Chunking Strategy)과 물리적 디스크 저장 레이아웃(Object Storage Layout / Pack Container)을 엄격히 분리합니다.

---

## 1. Problem (문제 정의)

현행 BackupSystem의 **Whole-file CAS (Whole-file Content-Addressable Storage)**는 단일 해싱 기반으로 구조가 단순하고 소형 파일 처리가 빠르지만, 대형 파일에서 심각한 병목을 유발합니다:
1. **I/O Read 낭비**: 10GB 단일 파일 중 1MB(0.0098%)만 수정되어도 **10.00 GB 전체 바이트를 전수 재독출**함 (10,240배 증폭).
2. **증분 시간 단축 부재**: 최초 10GB 백업(15.92초)과 1MB 변경 증분 백업(15.62초) 간의 소요 시간 차이가 0.3초 미만.
3. **스토리지 중복 점유**: 파일 전체 해시 변경으로 인해 10GB 분량의 신규 블롭이 통째로 추가 저장됨.

이를 해결하기 위해 블록/청크 단위 분할이 필수적이나, 단일 청킹 알고리즘만으로는 모든 워크로드를 최적화할 수 없다는 점이 실측을 통해 확인되었습니다.

---

## 2. Measured Evidence (Track 2-4 ~ 2-6 실측 근거)

10GB 단일 파일과 3종 데이터셋(High-entropy random, Structured binary, Mixed realistic)을 대상으로 수집된 객관적 실측 결과입니다:

| 비교 항목 | Whole-file CAS | 4MB Fixed Block | FastCDC (Target 4MB) |
| :--- | :---: | :---: | :---: |
| **10MB Overwrite 소요 시간** | 7.5 ~ 8.7s | **7.5 ~ 8.0s (최고 속도)** | 14.5 ~ 15.5s (Gear 해싱으로 약 1.8~2배 느림) |
| **10MB Overwrite 재사용률** | 0.0% | **99.88% ~ 99.9%** | **99.6% ~ 99.9%** |
| **10MB Overwrite 신규 저장량** | 10.00 GB | **12.00 MB** | 12.3 ~ 32.6 MB |
| **1KB Insertion 소요 시간** | 7.5s (전체 재저장) | 7.6 ~ 7.9s | 14.8 ~ 15.7s |
| **1KB Insertion 재사용률** | 0.0% | **49.98% ~ 54.0% (경계 밀림 폭락)** | **99.82% ~ 99.96% (완벽한 경계 재동기화)** |
| **1KB Insertion 신규 저장량** | 10.00 GB | **4.60 ~ 5.00 GB (절반 무효화)** | **2.91 ~ 20.65 MB (스토리지 99.9% 방어)** |
| **Base 청크 생성 수 (10GB)** | 1개 | 2,560개 | 1,689 ~ 2,617개 |

### 실측이 증명한 핵심 진실
- **Fixed 4MB**: 오프셋이 고정된 덮어쓰기(In-place Overwrite)에서는 최고 속도(7.5s)와 최고 효율(12MB)을 보이나, **바이트 삽입/삭제 시 후속 5GB가 무효화되는 치명적 취약점**이 존재함.
- **FastCDC**: Gear 해싱 연산으로 인해 Fixed Block 대비 **약 1.8~2배의 CPU/Wall Time 오버헤드**가 발생하지만, **1KB 삽입 시 99.9% 재사용률(2.9MB 신규)을 유지하며 경계를 완벽히 복원**함.

---

## 3. Strategy Candidates (전략 후보군)

1. **Candidate A: Whole-file CAS**
   - 파일 전체를 단일 블롭으로 취급.
   - 적합 대상: 64MB 미만의 소형 파일, 단발성 임시 파일, 변경 시 파일 전체가 완전히 새로 쓰이는 아카이브.
2. **Candidate B: 4MB Fixed Block**
   - 4MB 고정 크기 슬라이싱.
   - 적합 대상: VM 가상 디스크(`.vhdx`, `.vmdk`), 데이터베이스 데이터 파일(`.mdf`, `.sqlite`), 고정 블록 구조의 디스크 이미지.
3. **Candidate C: FastCDC 4MB Target (Min 1MB, Max 8MB)**
   - Gear Hashing 기반 가변 크기 내용 정의 청킹.
   - 적합 대상: 텍스트 로그, 소스 코드 tarball, 압축 해제된 데이터 덤프, 문서 모음 등 내부 바이트의 삽입/삭제(Shift)가 잦은 파일.
4. **Candidate D: Hybrid Selector**
   - 파일의 메타데이터, 크기, 변경 패턴 관측 신호를 조합하여 A, B, C 중 최적의 어댑터를 동적으로 결정.

---

## 4. Selection Signals (분류 신호의 득실 분석)

청킹 전략을 결정하기 위해 활용 가능한 4가지 신호의 장점과 한계입니다:

| 신호 종류 | 장점 | 한계 및 맹점 |
| :--- | :--- | :--- |
| **1. 파일 크기 (File Size)** | - 가장 결정적이고 오버헤드 0<br>- 소형 파일에 대한 불필요한 청킹 분할을 원천 차단 | - 대형 파일 내부의 워크로드 성격(Overwrite vs Insertion)을 전혀 구별하지 못함 |
| **2. 확장자 (Extension)** | - 사전 판별 가능 (`.vhdx` → Fixed, `.tar` → FastCDC)<br>- 추가 I/O 없음 | - 확장자가 없거나 불명확한 바이너리 파일 오판<br>- 실제 사용 패턴과 확장자 불일치 가능성 |
| **3. 변경 패턴 (Mutation Pattern)** | - 덮어쓰기인지 삽입인지 실제 물리적 진실 반영 | - 백업 수행 시점에 파일 내용을 대조해보지 않고는 사전에 알 수 없음 (추가 Read 비용) |
| **4. 과거 Snapshot Diff 히스토리** | - 직전 스냅샷과 현재 파일의 크기 변화(`size_delta == 0`)를 즉시 확인 가능 | - 최초 백업 시점에는 히스토리가 없음<br>- 파일 크기가 유지되면서 내부 바이트가 밀리는 극단적 변이 감지 한계 |

### 가장 범용적인 1차 신호: "파일 크기 + 크기 변화(Size Delta)"
- `크기 < 64MB`: **Whole-file CAS** (최고 속도, 무오버헤드)
- `크기 >= 64MB & 직전 스냅샷과 크기 동일(Δsize == 0)`: **Fixed Block 후보** (VM/DB의 Overwrite 가능성 농후)
- `크기 >= 64MB & 직전 스냅샷과 크기 상이(Δsize != 0)`: **FastCDC 후보** (바이트 삽입/삭제/추가 발생)

---

## 5. Hybrid Policies (후보 정책 3종 비교)

### Policy 1: 순수 크기 기반 정책 (Size-Only Threshold)
- **규칙**:
  - `Size < 64MB`: Whole-file CAS
  - `Size >= 64MB`: Fixed Block (단순화)
- **장점**: 구현 복잡도 극히 낮음, 런타임 판단 비용 0.
- **단점/오판**: 대형 로그나 덤프 파일에 1KB가 삽입될 경우 Fixed Block의 경계 밀림(5GB 무효화) 재앙 발생.
- **오판 가능성**: **높음** (대형 파일의 삽입 워크로드 무방비).

### Policy 2: 크기 + 확장자/크기변화 정적 분류 정책 (Static Heuristic)
- **규칙**:
  - `Size < 64MB`: Whole-file CAS
  - `Size >= 64MB & (.vhdx|.vmdk|.mdf|.db|Δsize == 0)`: Fixed Block (4MB)
  - `Size >= 64MB & (그 외 확장자 또는 Δsize != 0)`: FastCDC (4MB Target)
- **장점**:
  - 10GB VM 디스크는 최고 속도(7.5초)의 Fixed Block으로 처리.
  - 크기가 변한 파일은 FastCDC로 유도하여 경계 밀림(99.9% 재사용) 방어.
- **단점**: 사전 화이트리스트 규칙 관리 필요, 첫 백업 시 크기 변화 신호 없음.
- **구현 복잡도**: **중간 (Medium)**.

### Policy 3: 적응형 히스토리 관측 정책 (Adaptive Feedback Policy)
- **규칙**:
  - 첫 백업: FastCDC로 안전하게 시작 (또는 기본 Fixed).
  - 이후 스냅샷에서 청크 재사용률을 모니터링:
    - Fixed 적용 중 재사용률이 70% 이하로 급락하면 FastCDC로 강제 승격(Fallback).
    - 지속적으로 99.8% 이상의 재사용률과 Δsize == 0이 유지되면 Fixed 유지.
- **장점**: 파일 종류와 무관하게 실제 시스템 동작 결과에 기반한 자가 최적화(Self-tuning).
- **단점**: 메타데이터 DB에 파일별 청킹 상태 및 피드백 히스토리 누적 필요. 정책 전이 시점의 일시적 중복 발생.
- **구현 복잡도**: **높음 (High)**.

---

## 6. Adapter Contract (인터페이스 분리 설계)

"청킹 알고리즘(Chunking Strategy)"과 "저장소 레이아웃(Object Storage Layout)"을 완전히 분리하기 위해, 모든 청킹 전략은 공통의 **ChunkingAdapter Contract**를 구현해야 합니다.

```
                      ┌──────────────────────────┐
                      │    Snapshot Pipeline     │
                      └─────────────┬────────────┘
                                    │ File Path & Meta
                                    ▼
                      ┌──────────────────────────┐
                      │ Strategy Selector        │
                      │ (Policy 1 / 2 / 3)       │
                      └─────────────┬────────────┘
                                    │ Selects Adapter
         ┌──────────────────────────┼──────────────────────────┐
         ▼                          ▼                          ▼
┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
│ WholeFileAdapter│        │ FixedBlockAdapter│       │ FastCDCAdapter  │
└────────┬────────┘        └────────┬────────┘        └────────┬────────┘
         │                          │                          │
         └──────────────────────────┼──────────────────────────┘
                                    │ Chunk Streams (ChunkID, Offset, Length)
                                    ▼
                      ┌──────────────────────────┐
                      │ Object Storage Layout    │
                      │ (Loose Blobs or Pack)    │
                      └──────────────────────────┘
```

### 최소 Contract 명세 (Python Abstract Interface)
```python
from abc import ABC, abstractmethod
from typing import Iterator, Dict, Any, List

class ChunkItem:
    chunk_id: str        # Content Hash (SHA-256)
    offset: int          # 원본 파일 내 시작 오프셋
    length: int          # 청크 바이트 길이
    data: bytes          # 청크 원본 페이로드 (스트리밍 시)

class ChunkingAdapter(ABC):
    @property
    @abstractmethod
    def strategy_name(self) -> str:
        """'whole_file', 'fixed_4mb', 'fastcdc_4mb' 등 전략 식별자"""
        pass

    @abstractmethod
    def chunk_file(self, filepath: str) -> Iterator[ChunkItem]:
        """파일을 스트리밍 읽기하여 청크 시퀀스를 생성"""
        pass

    @abstractmethod
    def build_manifest_entry(self, chunks: List[ChunkItem], file_stat: Dict[str, Any]) -> Dict[str, Any]:
        """스냅샷 JSON에 기록될 청크 순서 및 파일 메타데이터 매니페스트 생성"""
        pass

    @abstractmethod
    def restore_stream(self, chunk_reader_callable, manifest_entry: Dict[str, Any], output_stream) -> bool:
        """매니페스트 순서대로 청크를 인출하여 원본 바이트로 복원 스트리밍"""
        pass

    @abstractmethod
    def verify_integrity(self, manifest_entry: Dict[str, Any], chunk_verifier_callable) -> bool:
        """청크 레벨 무결성 검증"""
        pass
```

---

## 7. Failure / Misclassification Cases (오판 시의 영향)

| 오판 시나리오 | 발생 원인 | 시스템 영향 및 페널티 |
| :--- | :--- | :--- |
| **Case 1: Shift형 파일을 Fixed로 잘못 분류** | 확장자가 DB 파일 같아서 Fixed로 분류했으나, 실제로는 중간에 레코드가 삽입된 경우 | **[치명적 스토리지 낭비]**<br>실측 데이터대로 1KB 변경에 5GB의 신규 청크가 발생하여 디스크가 급격히 소진됨. |
| **Case 2: Overwrite형 파일을 FastCDC로 잘못 분류** | 순수 VM VHDX 파일인데 규칙 누락으로 FastCDC로 처리된 경우 | **[처리 시간 및 CPU 낭비]**<br>실측 데이터대로 Fixed(7.5초) 대비 2배 느린 14.8~15.5초 소요. 백업 윈도우 시간 지연. |
| **Case 3: 소형 파일을 청킹으로 잘못 분류** | 100KB 소스코드/설정 파일을 청킹 파이프라인으로 태운 경우 | **[메타데이터 폭증 및 복원 지연]**<br>단일 블롭으로 끝나야 할 작업이 청크 인덱스 생성 및 DB 오버헤드로 번짐. |

---

## 8. Pack Container와의 경계 (Strategy ≠ Layout)

1. **문제의 분리**:
   - 10GB 단일 파일을 4MB로 청킹하면 **Fixed는 2,560개**, **FastCDC는 1,700~2,600개**의 청크가 생성됩니다.
   - 이를 디스크에 개별 파일(`.chk`)로 저장하면 NTFS 파일시스템의 MFT(Master File Table) 파편화와 디렉토리 락 경합이 발생합니다.
2. **책임의 분리**:
   - **Chunking Strategy**: "파일 바이트 스트림을 어떤 경계(4MB 고정 or Gear 해시 가변)로 자를 것인가"만 책임집니다.
   - **Object Storage Layout**: "잘려 나온 2,560개의 청크를 개별 파일(Loose Blobs)로 둘 것인가, 아니면 512MB~2GB 크기의 **Pack Container**에 순차 병합하여 단일 파일 내 오프셋 인덱스로 보관할 것인가"를 담당합니다.
3. **현재 단계 결론**:
   - 청킹 알고리즘 효율성 검증이 끝난 후, 물리 스토리지 계층에 **Pack Container Layout**을 플러그인 형태로 덧붙이는 구조를 유지합니다.

---

## 9. 다음 검증 단계 (Next Verification)

1. **Adapter Contract 단위 테스트**: `WholeFileAdapter`, `FixedBlockAdapter`, `FastCDCAdapter`의 격리 프로토타입 구현 및 인터페이스 호환성 검증.
2. **Policy 2 분류 시뮬레이션**: 다양한 실제 파일 100개(소스, VM 디스크, sqlite, zip, 로그 등) 대상 휴리스틱 분류 정확도 측정.
3. **Pack Container 프로토타입**: 2,560개 청크를 1개 컨테이너 파일로 묶었을 때의 Windows NTFS I/O 속도 및 복원 속도 계측.

---

## 10. Production Readiness Assessment (최종 판단)

### ❓ "현재 실측만으로 Hybrid selector를 production에 넣어도 되는가?"

### 💡 [최종 판정]: **"아직 넣어서는 안 됩니다. (NO - Hold for Integration Verification)"**

### 판단 근거 구분:

#### ✅ 이미 실측으로 입증된 사실 (Verified):
1. Whole-file CAS는 10GB 단일 파일의 부분 변경에 대해 10GB 전체 독출(15.6초)이라는 치명적 병목을 유발함.
2. Fixed 4MB는 덮어쓰기 워크로드에서 7.5초 및 99.9% 재사용이라는 최고 성능을 보장함.
3. FastCDC는 1KB 삽입 워크로드에서 99.96% 재사용(2.9MB 신규)을 보이며 경계 재동기화를 완벽히 입증함.
4. 모든 청킹 엔진이 원본과 100% 동일한 Bit-for-Bit 복원을 달성함.

#### ❌ 아직 프로덕션 투입 전에 검증되지 않은 핵심 항목 (Unverified):
1. **오판 시의 자가 치유(Fallback) 메커니즘 부재**:
   - 실제 사용자의 복잡한 파일 환경에서 Policy 2나 Policy 3가 오판했을 때, 시스템이 이를 감지하고 안전하게 전략을 교체하는 런타임 롤백 파이프라인이 미구현 상태임.
2. **메타데이터 DB 스케일링 미검증**:
   - 현행 `MetadataDB`는 파일 단위(1 file = 1 row) 구조인데, 수만~수십만 개의 청크 매핑이 유입될 때 SQLite 잠금 및 쿼리 지연이 프로덕션 수준에서 버티는지 미실측.
3. **Pack Container 부재 상태의 NTFS 파일 개수 부담**:
   - Pack Container 없이 10GB 파일 여러 개를 청킹하면 수만 개의 파일이 생성되어 Windows 탐색기와 백업 저장소 읽기 속도가 저하될 수 있음.

**따라서 프로덕션 코드(`core/*`)는 현 상태를 완벽히 유지하고, 격리 환경에서 Adapter Contract 및 Pack Container 프로토타입을 단계적으로 검증한 후 최종 통합을 진행해야 합니다.**
