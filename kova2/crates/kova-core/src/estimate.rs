//! 백업 용량 예측: 제외 규칙을 적용해 원본을 훑어 파일 수·크기를 센다(저장소에는 쓰지 않는다).
//! 첫 백업은 이 크기가 상한이고(중복 제거·압축 전), 이후 백업은 바뀐 부분만 늘어난다.
use crate::config::Profile;
use kova_engine::scanner;
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub struct Estimate {
    pub files: u64,
    pub bytes: u64,
    /// 저장소 드라이브의 여유 공간(알 수 없으면 None)
    pub repo_free: Option<u64>,
}

pub fn estimate(p: &Profile) -> Estimate {
    let filter = scanner::ExcludeFilter::new(&p.exclude_patterns);
    let (mut files, mut bytes) = (0u64, 0u64);
    for src in &p.sources {
        for it in scanner::scan_directory(Path::new(src), &filter) {
            if !it.is_dir {
                files += 1;
                bytes += it.size;
            }
        }
    }
    let repo = Path::new(&p.repo_dir);
    // 저장소 폴더가 아직 없으면 가장 가까운 기존 상위 폴더 기준
    let probe = repo.ancestors().find(|a| a.exists()).unwrap_or(repo);
    Estimate {
        files,
        bytes,
        repo_free: crate::diskspace::free_bytes(probe),
    }
}

/// 화면에 보일 문장. 여유 공간이 예상 크기보다 작으면 경고를 붙인다.
pub fn describe(e: &Estimate) -> String {
    let gb = |b: u64| b as f64 / (1024.0 * 1024.0 * 1024.0);
    let mut s = format!(
        "백업 대상 파일 {}개, 약 {:.2} GB (중복 제거·압축 전 크기)",
        e.files,
        gb(e.bytes)
    );
    match e.repo_free {
        Some(f) if f < e.bytes => s.push_str(&format!(
            " · 저장소 여유 {:.2} GB: 첫 백업에는 모자랄 수 있습니다",
            gb(f)
        )),
        Some(f) => s.push_str(&format!(" · 저장소 여유 {:.2} GB", gb(f))),
        None => {}
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_files_and_honors_exclude() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        std::fs::create_dir_all(src.join("sub")).unwrap();
        std::fs::write(src.join("a.txt"), vec![0u8; 1000]).unwrap();
        std::fs::write(src.join("sub").join("b.bin"), vec![0u8; 500]).unwrap();
        std::fs::write(src.join("skip.tmp"), vec![0u8; 9999]).unwrap();
        let mut p = Profile::new("p", "P");
        p.sources = vec![src.to_string_lossy().into()];
        p.repo_dir = d.path().join("repo").to_string_lossy().into();
        p.exclude_patterns = vec!["*.tmp".into()];
        let e = estimate(&p);
        assert_eq!((e.files, e.bytes), (2, 1500));
        assert!(e.repo_free.is_some());
        let text = describe(&Estimate {
            files: 2,
            bytes: 10 << 30,
            repo_free: Some(1 << 30),
        });
        assert!(text.contains("모자랄 수 있습니다"));
    }
}
