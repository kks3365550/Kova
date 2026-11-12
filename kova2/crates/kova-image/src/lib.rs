//! 블록 이미지(.qblk) 읽기·쓰기·검증·복원 (core/block_stream.py 포맷 호환).
//!
//! 파일 = 헤더(82B, `<8sHIQQ16s32sI`) + 반복 [청크 헤더(24B, `<QIIII`) + zstd 본문].
//! 파이썬 리더는 파일 끝이 청크 경계가 아니어도 정상 종료로 보지만, 여기서는 잘림을
//! 오류로 보고하고 헤더의 used_clusters 합계와 대조해 끝부분 유실도 잡는다.
pub mod bitmap;
pub mod capture;
pub mod qblk;
pub mod restore;
pub mod volume;
pub use qblk::*;
