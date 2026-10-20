# 📋 백업시스템 v2.10.3 릴리즈 빌드 매니페스트 (Release Build Manifest)

> **빌드 일자**: 2026-10-01  
> **공식 버전**: `2.10.3`  
> **Git 태그**: `v2.10.3`  
> **컴파일러**: Inno Setup 6.7.3 (Modern Wizard, LZMA2/Ultra64, 64-bit Native)

---

## 1. 공식 배포 바이너리 정보

### 1) 통합 인스톨러 (Full Setup Installer)
- **파일명**: `BackupSystem_Setup_v2.10.3.exe`
- **파일 크기**: `35,532,496 bytes` (약 33.9 MB)
- **SHA-256 Checksum**:
  ```text
  DDB80FC9E6A984FEF34E1914117E458D2A5E0FFBC0C418DF6BED8400DD54FEAB
  ```
- **포함 런타임 및 구성**:
  - Embedded Python 3.11 x64 Self-Contained 런타임
  - FastAPI / Uvicorn 비동기 백엔드 서버
  - Win32 ctypes 네이티브 트레이 에이전트
  - VBScript 무창(Silent) 실행기
  - SnapshotMetadataCache (~11ms 초저지연 메타데이터 캐시)

### 2) 독립 자동 업데이트 패키지 (Auto-Update Package)
- **파일명**: `release_v2.10.3.zip`
- **파일 크기**: `172,019,414 bytes`
- **SHA-256 Checksum**:
  ```text
  E46AA2A7FBF10A564256D22E82B25DD1EAA31BB6A35643E23320BB8201B35957
  ```
- **디지털 서명 파일**: `release_v2.10.3.zip.sig` (Ed25519)
- **서명 파일 SHA-256**:
  ```text
  DC2FA55424B890A158D501AC43238DA7479FD0FCB2C3F8F0ACE54A796E7B7109
  ```
- **공개키 위치**: `keys/release_ed25519.pub`
- **Zero-Leak 감사**: 통과 (개인키 및 개발자 자격증명 0건 포함)

---

## 2. 무결성 및 출시 합격 판정 (Go/No-Go Gate)

- [x] **Firebase 완전 제거 (Clean Purge)**: 통과 (관련 소스/UI/API 100% 제거)
- [x] **Release Blocking 결함**: `0건` (RELEASE FIX = 0)
- [x] **SHA-256 체크섬**: 일치 및 고정 완료
- [x] **Ed25519 디지털 서명**: 정상 검증 완료
- [x] **격리 Dry-Run**: 백업 ➔ 자동 검증 ➔ 복원 ➔ SHA-256 100% 일치 통과
- [x] **기존 운영 저장소 보호**: 완전 분리 및 오염 0건 보장

**최종 판정**: **GO (Commercial Release Approved)**
