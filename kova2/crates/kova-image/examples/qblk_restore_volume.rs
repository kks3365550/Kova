//! 사용: qblk_restore_volume <image.qblk> <드라이브문자> [확인문구] [보호할드라이브문자들(예: DF)]
//! 확인문구를 생략하면 대상 볼륨 정보와 입력해야 할 문구만 출력하고 아무것도 쓰지 않는다.
//! 주의: 대상 볼륨 전체를 덮어쓴다. 시스템 볼륨·이미지가 있는 볼륨은 거부된다.
#[cfg(windows)]
fn main() {
    use kova_image::volume_restore::{confirmation_phrase, restore_image_to_volume};
    use std::path::Path;
    use std::sync::atomic::AtomicBool;
    use std::time::Instant;

    let mut a = std::env::args().skip(1);
    let img = a.next().expect("이미지 경로");
    let letter = a
        .next()
        .expect("드라이브 문자")
        .chars()
        .next()
        .expect("문자");
    let typed = a.next();
    let protected: Vec<char> = a.next().unwrap_or_default().chars().collect();

    let Some(typed) = typed else {
        match kova_image::volume_restore::volume_info(letter) {
            Ok(v) => println!("입력할 확인 문구: {}", confirmation_phrase(&v)),
            Err(e) => println!("FAIL {e}"),
        }
        return;
    };
    let t = Instant::now();
    match restore_image_to_volume(
        Path::new(&img),
        letter,
        &typed,
        &protected,
        &AtomicBool::new(false),
        &|s| {
            eprintln!(
                "chunks={} raw={:.2}GB {:.0}MB/s",
                s.chunks,
                s.raw_bytes as f64 / 1e9,
                s.raw_bytes as f64 / 1e6 / t.elapsed().as_secs_f64().max(0.001)
            )
        },
    ) {
        Ok((h, s)) => println!(
            "OK header={h:?} stats={s:?} elapsed={:.0}s",
            t.elapsed().as_secs_f64()
        ),
        Err(e) => {
            println!("FAIL {e}");
            std::process::exit(1);
        }
    }
}
#[cfg(not(windows))]
fn main() {}
