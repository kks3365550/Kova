# KOVA Engine 복원 경로(WinPE) 설계

## 전제 (코드·실측 근거)
- KOVA Engine 이미지는 **C: 볼륨의 사용 중인 클러스터만** 담는다. 파티션 표, EFI 시스템 파티션, 복구 파티션은 들어 있지 않다.
- `kova_engine restore --image <f> --target \\.\C:` 로 볼륨에 직접 쓸 수 있다. 작은 테스트 볼륨 30회 복원, 비교, chkdsk가 모두 통과했다.
- `kova_engine.exe`는 단일 실행 파일(1.9MB)이라 WinPE에 그대로 넣을 수 있다.
- 따라서 베어메탈 복원 = (1) 디스크 레이아웃 재구성 → (2) 볼륨 복원 → (3) 부팅 파일 재생성.

## 복원 절차 (WinPE 안에서)
1. 대상 디스크 선택 (사용자 확인 필수, 기존 데이터가 지워진다는 경고 표시).
2. `diskpart`로 GPT 레이아웃 생성: EFI 100~260MB(FAT32), MSR 16MB, 주 파티션(NTFS), 복구 파티션(선택).
3. 주 파티션을 C: 로 지정하고 `format fs=ntfs quick`.
4. `kova_engine verify --image X` 로 이미지 무결성 먼저 확인. 실패하면 중단.
5. `kova_engine restore --image X --target \\.\C:`.
6. `bcdboot C:\Windows /s S: /f UEFI` 로 EFI 부팅 파일 재생성 (EFI 파티션 백업 불필요).
7. 재부팅 후 부팅 확인.

## 백업 시 같이 저장할 메타데이터 (코드 변경 필요)
`C_YYYYmmdd_HHMMSS.kova` 옆에 `.kova.json`:
- 원본 디스크의 파티션 스타일(GPT/MBR), 파티션별 크기와 종류, C: 볼륨 크기, 펌웨어(UEFI/BIOS)
- KOVA Engine 버전, 이미지 SHA-256 또는 verify 통과 시각
복원 스크립트는 이 파일로 diskpart 스크립트를 만든다. 메타데이터가 없으면 기본 GPT/UEFI 레이아웃을 쓰되, 사용자에게 확인을 받는다.

## WinPE 만들기
- Windows ADK + WinPE 애드온 필요. **이 PC에는 설치되어 있지 않다** (설치는 승인 필요).
- `copype amd64 <dir>` → `MakeWinPEMedia /ISO` 로 ISO 생성. `kova_engine.exe`와 `restore.cmd`를 `\Kova\` 에 넣는다.
- 필요 드라이버: 대상 디스크가 NVMe/RAID면 해당 드라이버를 `dism /add-driver`로 주입.
- 이미지가 외장 디스크나 네트워크에 있으면 WinPE에서 드라이브 문자가 달라지므로, 스크립트가 `*.kova` 파일을 모든 볼륨에서 탐색해 목록으로 보여준다.

## 검증 계획 (상용 출시 전 필수)
| 단계 | 내용 |
|---|---|
| A | Hyper-V VM(UEFI, Gen2)에 Windows 설치 → KOVA Engine 백업 → 디스크를 새 VHDX로 교체 → WinPE로 복원 → 부팅 성공 확인 |
| B | 위 과정을 BIOS(Gen1)/MBR 구성에서도 반복 |
| C | 복원 후 chkdsk, 파일 해시 샘플 비교 |
| D | 이 PC의 실제 이미지로 별도 디스크에 복원 시험 (대상 디스크가 비어 있을 때만) |
A, B, C가 통과하기 전에는 KOVA Engine을 기본 엔진으로 바꾸지 않는다.

## 알려진 제약
- 이 PC에서 실제 C: 덮어쓰기 복원은 하지 않는다 (실행 중인 OS 볼륨이라 불가능하고 위험).
- 증분 백업은 아직 없다. 매번 전체 이미지(약 213GB)다.
- C: 사용량 약 300GB 기준 이미지가 213GB(약 70%)다. 공간 계획에 반영한다.

## 다음 구현 순서
1. 백업 시 `.kova.json` 메타데이터 저장 (`core/system_image.py`, 테스트 포함)
2. `restore.cmd` / diskpart 템플릿 작성 (ASCII, CRLF)
3. ADK 설치 승인 후 WinPE ISO 빌드
4. Hyper-V 시나리오 A~C 실행

## 구현 현황과 사용법 (2단계 완료)
- 백업 시 `.kova.json` 메타데이터 저장: 구현·테스트 완료 (`core/system_image.py`).
- `kova_engine restore`는 장치 대상이 이미지 볼륨보다 작으면 쓰기 전에 거부한다 (`target is too small`).
- `winpe/restore.cmd` (+ `layout_gpt_uefi.txt`, `layout_mbr_bios.txt`, `extend.txt`), WinPE 안에서 `kova_engine.exe`와 같은 폴더(`\Kova\`)에 둔다.
  1. 모든 볼륨의 `KovaImages\*.kova`를 나열, 번호 선택
  2. **디스크를 건드리기 전에** `kova_engine verify` (실패 시 중단)
  3. 펌웨어 감지(UEFI/BIOS) 후 대상 디스크 번호 입력, `ERASE` 입력으로 확인
  4. diskpart 레이아웃 (S: 부팅, W: 윈도우) → `kova_engine restore --target \\.\W:` → `extend filesystem` → `bcdboot`
- 이미지가 S: 또는 W: 드라이브에 있으면 중단한다 (복원 대상 문자와 충돌).
- 검증: `tests/test_winpe_scripts.py`는 인코딩과 순서만 확인한다. 실제 복원 동작은 3~4단계(WinPE ISO + VM)에서 확인한다.

## Hyper-V restore test results (2026-10-24)

Image: 16.1 GiB C: volume (1207 chunks). Restore media: KovaRestore.iso (WinPE + kova_engine.exe + restore.cmd).

| Scenario | Result |
|---|---|
| A. Gen2 / UEFI / GPT | PASS - verify, restore ~21 s, bcdboot UEFI, booted from HDD to OOBE |
| B. Gen1 / BIOS / MBR | PASS - firmware detected as BIOS, MBR layout, restore ~25 s, bcdboot BIOS, booted from HDD to OOBE |
| C. Integrity (A restored disk, before first boot) | chkdsk: no errors; 20/20 sampled System32 file hashes identical; block compare 13/1207 chunks differ (small byte diffs, NTFS metadata touched by filesystem extend/mount; the source VM had also been booted after backup) |
| D. Empty physical disk | Not run: no empty disk on the test PC |
| D-VM. 이 PC 실제 C: 이미지(331.5 GiB, 파일 207.4 GB) -> Hyper-V Gen2 VM 빈 1 TB VHDX | PASS - 백업(write+verify) 37.2분, WinPE restore.cmd: verify 151초, restore 약 475초(약 700 MB/s), extend, bcdboot UEFI 성공. HDD 부팅 후 로그인된 바탕화면 도달. chkdsk(읽기 전용) 오류 없음, System32 20/20 SHA-256 일치. 대상 VHDX는 C:\KovaDrill(E: 용량 부족). 실제 NVMe/RAID 드라이버, Secure Boot, TPM, BitLocker는 미검증 |

Findings:
- WinPE verify ran out of memory (16 MB allocations) while the VM used Hyper-V Dynamic Memory. With static 4 GB it passes (8 threads). Use >= 4 GB RAM for the restore target; if low-memory machines matter, add `--threads 4` to the verify call in restore.cmd.
- findstr is not in WinPE; restore.cmd uses `find`.
- Scenario D (VM, real C: image) passed on 2026-10-30; KOVA Engine is now the default engine (wbadmin remains selectable and is the automatic fallback). Real-hardware items (NVMe/RAID drivers in WinPE, Secure Boot, TPM, BitLocker) are still unverified.
