//! 랜섬웨어 이상 징후 조기 경보:
//! 백업 직후 대량 파일 변조율 또는 알려진 랜섬웨어 확장자 급증을 탐지한다.
//! 탐지 시 서비스는 경고 알림·웹훅을 발송하고, 정상 옛 스냅샷이 밀려 삭제되지 않도록 보존 정리(retention)를 자동 보류한다.
use kova_engine::storage::SnapshotManifest;
use std::fs;
use std::io;
use std::path::Path;

pub const SUSPECT_EXTENSIONS: &[&str] = &[
    "locked",
    "encrypted",
    "crypted",
    "crypt",
    "crypto",
    "ransom",
    "wcry",
    "wncry",
    "lockbit",
    "blackcat",
    "akira",
    "ryuk",
    "conti",
    "revil",
    "maze",
    "cerber",
    "zepto",
    "odin",
];

#[derive(Debug, Clone, Copy)]
pub struct AnomalyConfig {
    /// 변경률 검사를 적용할 최소 파일 수(소규모 폴더 오탐 방지)
    pub min_files_for_ratio: usize,
    /// 변경(수정+신규) 비율 임계치(%) — 단, 직전 스냅샷이 있는 증분 백업(`unmodified > 0` 또는 `had_previous`)일 때만 적용
    pub change_ratio_percent: u32,
    /// 의심 랜섬웨어 확장자 파일 수 임계치
    pub suspect_ext_threshold: usize,
}

impl Default for AnomalyConfig {
    fn default() -> Self {
        Self {
            min_files_for_ratio: 50,
            change_ratio_percent: 60,
            suspect_ext_threshold: 5,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnomalyReport {
    pub reasons: Vec<String>,
}

impl AnomalyReport {
    pub fn summary(&self) -> String {
        self.reasons.join("; ")
    }
}

pub fn is_suspect_extension(path: &str) -> bool {
    let Some(ext) = Path::new(path).extension().and_then(|e| e.to_str()) else {
        return false;
    };
    let lower = ext.to_ascii_lowercase();
    SUSPECT_EXTENSIONS.contains(&lower.as_str())
}

/// 스냅샷 매니페스트에서 알려진 랜섬웨어 의심 확장자를 가진 파일 수를 센다.
pub fn count_suspect_in_manifest(manifest_path: &Path) -> usize {
    let Ok(f) = fs::File::open(manifest_path) else {
        return 0;
    };
    let Ok(m): Result<SnapshotManifest, _> = serde_json::from_reader(io::BufReader::new(f)) else {
        return 0;
    };
    m.files()
        .into_iter()
        .filter(|fe| !fe.is_dir && is_suspect_extension(&fe.path))
        .count()
}

/// 백업 통계와 의심 확장자 수로 랜섬웨어 이상 징후를 판정한다.
/// `had_previous`가 false(최초 풀 백업)이면 변경률 검사는 건너뛰고 의심 확장자 검사만 수행한다.
pub fn detect(
    total_files: usize,
    new_files: usize,
    modified_files: usize,
    suspect_ext_count: usize,
    had_previous: bool,
    cfg: &AnomalyConfig,
) -> Option<AnomalyReport> {
    let mut reasons = Vec::new();
    if suspect_ext_count >= cfg.suspect_ext_threshold {
        reasons.push(format!(
            "랜섬웨어 의심 확장자 파일 {suspect_ext_count}개 감지"
        ));
    }
    if had_previous && total_files >= cfg.min_files_for_ratio {
        let changed = new_files.saturating_add(modified_files);
        let pct = ((changed as u64) * 100 / (total_files as u64)) as u32;
        if pct >= cfg.change_ratio_percent {
            reasons.push(format!(
                "단일 백업에서 전체 파일의 {pct}%({changed}/{total_files})가 급격히 변경됨"
            ));
        }
    }
    if reasons.is_empty() {
        None
    } else {
        Some(AnomalyReport { reasons })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_backup_ignores_change_ratio_but_catches_suspect_extensions() {
        let cfg = AnomalyConfig::default();
        assert!(detect(500, 500, 0, 0, false, &cfg).is_none());
        let r = detect(500, 500, 0, 12, false, &cfg).unwrap();
        assert!(r.summary().contains("12개 감지"));
    }

    #[test]
    fn incremental_mass_modification_triggers_anomaly() {
        let cfg = AnomalyConfig::default();
        // 소규모(10개) 폴더는 오탐 방지
        assert!(detect(10, 0, 9, 0, true, &cfg).is_none());
        // 정상 증분(100개 중 10개 변경)
        assert!(detect(100, 5, 5, 0, true, &cfg).is_none());
        // 이상 급변(100개 중 70개 변경)
        let r = detect(100, 20, 50, 0, true, &cfg).unwrap();
        assert!(r.summary().contains("70%"));
    }

    #[test]
    fn suspect_extension_matching_is_case_insensitive() {
        assert!(is_suspect_extension("C:\\docs\\report.xlsx.LOCKED"));
        assert!(is_suspect_extension("invoice.pdf.lockbit"));
        assert!(!is_suspect_extension("C:\\docs\\report.xlsx"));
        assert!(!is_suspect_extension("C:\\docs\\archive.zip"));
    }
}
