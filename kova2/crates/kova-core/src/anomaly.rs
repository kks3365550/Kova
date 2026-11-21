//! 랜섬웨어 이상 징후 조기 경보:
//! 백업 직후 대량 파일 변조율 또는 알려진 랜섬웨어 확장자 급증을 탐지한다.
//! 탐지 시 서비스는 경고 알림·웹훅을 발송하고, 정상 옛 스냅샷이 밀려 삭제되지 않도록 보존 정리(retention)를 자동 보류한다.
use kova_engine::storage::{self, SnapshotManifest};
use std::collections::HashMap;
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
    /// 직전 스냅샷 대비 삭제 비율 임계치(%) — `assess_manifest` 에서만 쓴다
    pub delete_ratio_percent: u32,
}

impl Default for AnomalyConfig {
    fn default() -> Self {
        Self {
            min_files_for_ratio: 50,
            change_ratio_percent: 60,
            suspect_ext_threshold: 5,
            delete_ratio_percent: 50,
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

/// 직전 스냅샷과 비교한 이번 스냅샷의 변화(폴더 제외, 파일 수 기준).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ManifestDelta {
    /// 직전(같은 프로파일·같은 원본) 스냅샷이 있었는지
    pub had_previous: bool,
    /// 이번 스냅샷의 파일 수(폴더 제외)
    pub files: usize,
    pub new_files: usize,
    pub modified_files: usize,
    /// 직전 스냅샷에는 있었는데 이번에 없어진 파일 수
    pub deleted_files: usize,
    /// 직전 스냅샷의 파일 수
    pub previous_files: usize,
    /// 신규·수정 파일 중 의심 확장자 파일 수
    pub suspect_changed: usize,
}

fn load_manifest(path: &Path) -> Option<SnapshotManifest> {
    let f = fs::File::open(path).ok()?;
    serde_json::from_reader(io::BufReader::new(f)).ok()
}

/// (원본 경로, 상대 경로) → 내용 지문(크기 + 청크 해시). 원본을 확정할 수 없는 항목은 뺀다.
fn file_map(m: SnapshotManifest) -> HashMap<String, (u64, Vec<String>)> {
    let srcs: Vec<String> = storage::manifest_sources(&m)
        .iter()
        .map(|s| storage::norm_source(s))
        .collect();
    let mut out = HashMap::new();
    for fe in m.files().into_iter().filter(|f| !f.is_dir) {
        let src = match fe.src {
            Some(i) => srcs.get(i as usize).cloned(),
            None if srcs.len() <= 1 => Some(srcs.first().cloned().unwrap_or_default()),
            None => None,
        };
        let Some(src) = src else { continue };
        let key = format!("{src}|{}", fe.path.replace('\\', "/").to_lowercase());
        let mut fp: Vec<String> = fe.chunks.into_iter().map(|c| c.hash).collect();
        if fp.is_empty() {
            fp.extend(fe.blob_id.or(fe.sha256));
        }
        out.insert(key, (fe.size, fp));
    }
    out
}

/// 매니페스트를 직전 기준 스냅샷(같은 프로파일 또는 프로파일 기록 없는 구형, 원본 경로 집합이 같은 것)과 비교한다.
pub fn manifest_delta(manifest_path: &Path) -> Option<ManifestDelta> {
    let cur = load_manifest(manifest_path)?;
    let sid = cur.session_id();
    let pid = cur.profile_id.clone();
    let sources = storage::manifest_sources(&cur);
    let dir = manifest_path.parent()?;
    let prev = storage::find_recent_manifest(dir, Some(&sid), &|m| {
        storage::is_baseline_for(m, pid.as_deref(), &sources)
    });
    let cur_map = file_map(cur);
    let mut d = ManifestDelta {
        had_previous: prev.is_some(),
        files: cur_map.len(),
        ..Default::default()
    };
    let prev_map = prev.map(file_map).unwrap_or_default();
    d.previous_files = prev_map.len();
    for (k, v) in &cur_map {
        let changed = match prev_map.get(k) {
            None => {
                d.new_files += 1;
                true
            }
            Some(p) if p != v => {
                d.modified_files += 1;
                true
            }
            Some(_) => false,
        };
        if changed && is_suspect_extension(k) {
            d.suspect_changed += 1;
        }
    }
    d.deleted_files = prev_map
        .keys()
        .filter(|k| !cur_map.contains_key(*k))
        .count();
    Some(d)
}

/// 스냅샷 매니페스트에서 알려진 랜섬웨어 의심 확장자를 가진 파일 수를 센다.
/// 직전 스냅샷과 비교해 이번에 새로 생기거나 바뀐 파일만 센다(원래부터 있던 `.crypt` 등 정상 파일로 매번 경보하지 않음).
pub fn count_suspect_in_manifest(manifest_path: &Path) -> usize {
    manifest_delta(manifest_path).map_or(0, |d| d.suspect_changed)
}

/// 매니페스트만으로 판정한다(권장): 파일 수(폴더 제외) 기준 변경률, 신규·수정 파일의 의심 확장자, 삭제율을 본다.
/// 직전 기준 스냅샷이 없으면(최초·업그레이드 후 첫 백업) 변경률·삭제율 검사는 하지 않는다.
pub fn assess_manifest(manifest_path: &Path, cfg: &AnomalyConfig) -> Option<AnomalyReport> {
    let d = manifest_delta(manifest_path)?;
    let mut reasons = detect(
        d.files,
        d.new_files,
        d.modified_files,
        d.suspect_changed,
        d.had_previous,
        cfg,
    )
    .map(|r| r.reasons)
    .unwrap_or_default();
    if d.had_previous && d.previous_files >= cfg.min_files_for_ratio {
        let pct = ((d.deleted_files as u64) * 100 / (d.previous_files as u64)) as u32;
        if pct >= cfg.delete_ratio_percent {
            reasons.push(format!(
                "단일 백업에서 직전 파일의 {pct}%({}/{})가 삭제됨",
                d.deleted_files, d.previous_files
            ));
        }
    }
    (!reasons.is_empty()).then_some(AnomalyReport { reasons })
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

    /// files: (경로, 청크 해시). dirs: 폴더 수.
    fn write_manifest(
        dir: &Path,
        id: &str,
        pid: Option<&str>,
        files: &[(String, String)],
        dirs: usize,
    ) -> std::path::PathBuf {
        let mut entries: Vec<serde_json::Value> = files
            .iter()
            .map(|(p, h)| {
                serde_json::json!({"path": p, "size": 1, "src": 0, "chunks": [{"hash": h, "length": 1}]})
            })
            .collect();
        for i in 0..dirs {
            entries.push(serde_json::json!({"path": format!("d{i}"), "is_dir": true, "src": 0}));
        }
        let mut m = serde_json::json!({"id": id, "sources": ["C:\\Data"], "files": entries});
        if let Some(p) = pid {
            m["profile_id"] = p.into();
        }
        let path = dir.join(format!("{id}.json"));
        fs::write(&path, m.to_string()).unwrap();
        // 수정 시각 순서를 확실히 한다
        std::thread::sleep(std::time::Duration::from_millis(20));
        path
    }

    fn files(n: usize, ext: &str, tag: &str) -> Vec<(String, String)> {
        (0..n)
            .map(|i| (format!("f{i}.{ext}"), format!("{tag}{i:062}")))
            .collect()
    }

    /// 원래부터 있던 의심 확장자 파일은 세지 않고, 이번에 새로 생기거나 바뀐 것만 센다.
    #[test]
    fn suspect_extensions_count_only_new_or_modified_files() {
        let d = tempfile::tempdir().unwrap();
        let old = files(10, "locked", "aa");
        write_manifest(d.path(), "s1", Some("p"), &old, 0);
        let same = write_manifest(d.path(), "s2", Some("p"), &old, 0);
        assert_eq!(count_suspect_in_manifest(&same), 0);
        let mut more = old.clone();
        more.extend((0..6).map(|i| (format!("new{i}.docx.lockbit"), format!("bb{i:062}"))));
        let p3 = write_manifest(d.path(), "s3", Some("p"), &more, 0);
        assert_eq!(count_suspect_in_manifest(&p3), 6);
        assert!(assess_manifest(&p3, &AnomalyConfig::default())
            .unwrap()
            .summary()
            .contains("6개"));
    }

    /// 변경률은 폴더를 빼고 파일 수로 계산하고, 대량 삭제도 잡는다.
    #[test]
    fn ratio_uses_file_count_and_mass_deletion_is_detected() {
        let cfg = AnomalyConfig::default();
        let d = tempfile::tempdir().unwrap();
        write_manifest(d.path(), "s1", Some("p"), &files(100, "docx", "aa"), 200);
        // 100개 중 70개 수정(폴더 200개는 분모에서 뺀다)
        let mut f = files(100, "docx", "aa");
        for x in f.iter_mut().take(70) {
            x.1 = format!("cc{}", &x.1[2..]);
        }
        let p2 = write_manifest(d.path(), "s2", Some("p"), &f, 200);
        let delta = manifest_delta(&p2).unwrap();
        assert_eq!((delta.files, delta.modified_files), (100, 70));
        assert!(assess_manifest(&p2, &cfg)
            .unwrap()
            .summary()
            .contains("70%"));
        // 직전 100개 중 60개 삭제
        let p3 = write_manifest(d.path(), "s3", Some("p"), &f[..40], 200);
        let r = assess_manifest(&p3, &cfg).unwrap();
        assert!(r.summary().contains("삭제"), "{}", r.summary());
    }

    /// 업그레이드 직후(직전 스냅샷에 프로파일 기록 없음)에도 원본이 같으면 기준으로 삼아 100% 변경 오탐을 내지 않는다.
    /// 다른 프로파일의 스냅샷은 기준이 아니다.
    #[test]
    fn upgrade_baseline_without_profile_id_avoids_false_positive() {
        let cfg = AnomalyConfig::default();
        let d = tempfile::tempdir().unwrap();
        let f = files(100, "xlsx", "aa");
        write_manifest(d.path(), "s1", None, &f, 0);
        write_manifest(d.path(), "s1b", Some("other"), &files(100, "xlsx", "ff"), 0);
        let p2 = write_manifest(d.path(), "s2", Some("p"), &f, 0);
        let delta = manifest_delta(&p2).unwrap();
        assert!(delta.had_previous);
        assert_eq!(delta.new_files + delta.modified_files, 0);
        assert!(assess_manifest(&p2, &cfg).is_none());
    }

    #[test]
    fn suspect_extension_matching_is_case_insensitive() {
        assert!(is_suspect_extension("C:\\docs\\report.xlsx.LOCKED"));
        assert!(is_suspect_extension("invoice.pdf.lockbit"));
        assert!(!is_suspect_extension("C:\\docs\\report.xlsx"));
        assert!(!is_suspect_extension("C:\\docs\\archive.zip"));
    }
}
