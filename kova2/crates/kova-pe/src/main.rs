//! kova-pe: WinPE 에서 .qblk 이미지를 대상 디스크에 통째로 복원하는 콘솔 프로그램.
//! 흐름: 이미지 찾기 -> 이미지 검증(디스크를 건드리기 전) -> 대상 디스크 선택·확인 -> 파티션 -> 복원 -> bcdboot.
//! 자동 모드: 이미지 폴더가 있는 드라이브 루트에 KOVA_AUTO.txt(image=, disk_id=, confirm=ERASE DISK ID <ID>)가 있을 때만(시험용).
//! 디스크를 건드리기 직전에 KOVA_AUTO.done.txt 로 이름을 바꿔 재부팅마다 반복하지 않는다.
#[cfg(windows)]
mod run;

#[cfg(windows)]
fn main() {
    std::process::exit(run::main());
}

#[cfg(not(windows))]
fn main() {
    eprintln!("kova-pe 는 Windows(WinPE) 전용입니다.");
    std::process::exit(2);
}
