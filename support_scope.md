# 💻 백업시스템 지원 범위 및 시스템 요구사양 (Support Scope)

> **버전**: v2.9.20  
> **기준 일자**: 2026-09-30

---

## 1. 운영체제 (OS Support)
- **Windows 11 (64-bit)**: 전 에디션 (Home, Pro, Enterprise) 공식 지원 (완전 호환)
- **Windows 10 (64-bit)**: 버전 1809 이상 전 에디션 공식 지원
- **Windows Server**: 2016, 2019, 2022 64-bit 공식 지원
- *(참고: 32비트 OS는 지원하지 않음)*

---

## 2. 하드웨어 요구사양 (Hardware Requirements)
- **CPU**: x86-64 아키텍처 듀얼 코어 이상 권장
- **RAM**: 최소 512MB 여유 메모리, 권장 2GB 이상
- **디스크 공간**:
  - 프로그램 설치 공간: 약 150MB (내장 파이썬 런타임 포함)
  - 백업 저장소: 백업 대상 데이터 용량 및 스냅샷 보존 정책에 따라 가변 (최소 10GB 이상 여유 공간 권장)

---

## 3. 네트워크 및 포트 (Network & Firewall)
- **로컬 웹 UI & REST API 포트**: TCP `8765` (인스톨러에서 Windows 방화벽 자동 등록)
- **외부 연동**:
  - Firebase/Firestore 자동 업데이트 확인: HTTPS 아웃바운드 포트 `443`
  - Tailscale 사설망 P2P 동기화 지원

---

## 4. 파일 시스템 지원 (Filesystem Support)
- **NTFS**: 완전 지원 (VSS 섀도 복사, 파일 대체 데이터 스트림, 대용량 파일)
- **ReFS**: 지원
- **exFAT / FAT32**: 지원 (단, VSS 섀도 복사는 볼륨 특성상 제한될 수 있음)
- **네트워크 드라이브 (SMB/CIFS NAS)**: 백업 저장소 타겟 지원
