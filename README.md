# Kova2

Windows용 백업·복구 프로그램입니다. Rust로 만들었고, 파일 백업(중복 제거·압축·암호화), 볼륨 통째 이미지(.qblk) 백업, VSS 스냅샷, 랜섬웨어 보호(WORM), 오프사이트 복제, 예약 백업을 지원합니다.

- 소스: [`kova2/`](kova2/) (Cargo 워크스페이스)
- 설치 파일·업데이트 패키지: [Releases](../../releases) (`Kova2_Setup_vX.Y.Z.exe`)
- 앱은 이 저장소의 최신 릴리즈를 확인해 서명된 업데이트만 설치합니다.

## 빌드·검사

```
cd kova2
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## 이전 버전(Kova 1, 파이썬)

2.x 계열(파이썬판)은 이 저장소에서 제거했습니다. 마지막 상태는 태그 [`kova1-final`](../../tree/kova1-final)에 보존되어 있고, 2.x 릴리즈 파일은 Releases에 그대로 있습니다.
