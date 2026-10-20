# 🗄️ 백업시스템 저장소 포맷 및 스키마 명세 (Repository Format Specification)

> **포맷 버전**: CAS-V2 / Schema v2.7+  
> **기준 일자**: 2026-09-30

---

## 1. 저장소 디렉토리 레이아웃 (Layout)

백업 저장소(`repo_dir`)의 물리적 디렉토리 구조는 다음과 같습니다:

```text
<repo_dir>/
├── blobs/                        # 콘텐츠 주소화 스토리지 (CAS) 오브젝트
│   ├── 00/
│   ├── 01/
│   ├── ...
│   └── ff/                       # SHA-256 해시 앞 2자리 256개 샤딩
│       └── <sha256_hex>.blob     # 압축/암호화된 실제 데이터 청크
├── snapshots/                    # 스냅샷 매니페스트 메타데이터
│   ├── snap_<timestamp>_<id>.json
│   └── ...
├── metadata.db                   # SQLite 기반 고속 검색 및 통계 메타데이터 DB
├── metadata.db-wal               # SQLite WAL 저널 (원자적 트랜잭션 보장)
└── metadata.db-shm
```

---

## 2. 블롭(Blob) 바이너리 포맷

블롭은 원본 데이터의 SHA-256 해시를 고유 키로 가지며, 버전에 따라 다음 바이너리 헤더를 갖습니다:

### 1) v0 평문 블롭 (Zstandard 압축)
```text
[Magic: 0x28B52FFD (4B)] + [Zstandard Compressed Data]
```

### 2) v1 암호화 블롭 (AES-256-GCM + Zstandard)
```text
[Magic: b"ENC\x01" (4B)] + [12B Random Nonce] + [AES-256-GCM Ciphertext (Zstd Payload)] + [16B Auth Tag]
```
- **CAS Identity 보존**: 암호화가 적용되어도 블롭의 파일명(키)은 원본 평문의 SHA-256 해시를 그대로 유지하므로, **중복제거(Dedup) 효율이 100% 보존**됩니다.

---

## 3. 스냅샷 매니페스트 포맷 (JSON)

각 스냅샷은 `snapshots/snap_<timestamp>_<id>.json`에 JSON으로 기록됩니다:

```json
{
  "id": "snap_20260930_121248_31280b",
  "created_at": 1790737968.12,
  "profile_id": "prof_default",
  "profile_name": "서버/시스템 메인 백업 프로필",
  "is_verified": true,
  "verify_timestamp": 1790737968.55,
  "encryption": {
    "enabled": false,
    "version": 0,
    "cipher": "none"
  },
  "sources": ["C:\\TargetFolder"],
  "summary": {
    "total_files": 1250,
    "total_bytes": 104857600,
    "new_files": 15,
    "dedup_saved_bytes": 85000000,
    "duration_seconds": 3.42
  },
  "entries": [
    {
      "rel_path": "sub/document.docx",
      "size": 45020,
      "mtime": 1790731000.0,
      "sha256": "482cb080806eaafc...",
      "is_dir": false
    }
  ]
}
```

---

## 4. 하위 호환성 및 마이그레이션 정책
- 신규 버전의 백업 엔진은 이전 버전(v0 평문 블롭 및 이전 스냅샷 포맷)을 100% 자동으로 인식하며, 별도의 수동 마이그레이션 작업 없이 즉각 복원 가능합니다.
