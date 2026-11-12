//! 업데이트 패키지(ZIP) 적용: 검증된 압축 파일을 설치 폴더에 덮어쓰되 실패하면 원래대로 되돌린다.
//! 순서: 항목 검사(경로 탈출·개인키·필수 파일) → 임시 폴더에 풀기 → 기존 파일을 백업 폴더로 옮기며 교체 → 오류 시 롤백.
//! 서명·해시 검증은 `update::verify_package` 가 먼저 한다(이 모듈은 이미 검증된 바이트만 받는다).
use std::fs;
use std::io::{self, Cursor, Read};
use std::path::{Component, Path, PathBuf};

#[derive(Debug)]
pub enum InstallError {
    Io(io::Error),
    Zip(String),
    /// 압축 안의 경로가 설치 폴더 밖을 가리키거나 허용되지 않는 파일
    UnsafeEntry(String),
    MissingRequired(String),
}
impl std::fmt::Display for InstallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "{e}"),
            Self::Zip(e) => write!(f, "패키지 오류: {e}"),
            Self::UnsafeEntry(p) => write!(f, "허용되지 않는 항목: {p}"),
            Self::MissingRequired(p) => write!(f, "필수 파일이 패키지에 없음: {p}"),
        }
    }
}
impl From<io::Error> for InstallError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

#[derive(Debug, Default, PartialEq)]
pub struct Installed {
    pub files: usize,
    pub replaced: usize,
}

fn is_forbidden_name(name: &str) -> bool {
    let l = name.to_lowercase();
    l.ends_with(".pem") && l.contains("private")
        || l.ends_with(".key")
        || l.contains("private_key")
        || l.contains("id_rsa")
}

/// 안전한 상대 경로만 통과시킨다.
fn safe_rel(name: &str) -> Option<PathBuf> {
    let p = Path::new(name);
    if p.is_absolute() || name.contains(':') {
        return None;
    }
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::Normal(s) => out.push(s),
            Component::CurDir => {}
            _ => return None,
        }
    }
    if out.as_os_str().is_empty() {
        None
    } else {
        Some(out)
    }
}

/// zip_bytes 를 install_dir 에 적용한다. 교체되는 기존 파일은 backup_dir 에 보관된다.
/// required 는 패키지에 반드시 있어야 하는 상대 경로(예: 엔진 exe).
pub fn apply(
    zip_bytes: &[u8],
    install_dir: &Path,
    backup_dir: &Path,
    required: &[&str],
) -> Result<Installed, InstallError> {
    let mut z = zip::ZipArchive::new(Cursor::new(zip_bytes))
        .map_err(|e| InstallError::Zip(e.to_string()))?;
    // 1) 검사: 하나라도 위험하면 아무것도 건드리지 않는다
    let mut entries: Vec<(usize, PathBuf)> = vec![];
    for i in 0..z.len() {
        let f = z
            .by_index(i)
            .map_err(|e| InstallError::Zip(e.to_string()))?;
        if f.is_dir() {
            continue;
        }
        let name = f.name().replace('\\', "/");
        let rel = safe_rel(&name).ok_or_else(|| InstallError::UnsafeEntry(name.clone()))?;
        if is_forbidden_name(&name) {
            return Err(InstallError::UnsafeEntry(name));
        }
        entries.push((i, rel));
    }
    for r in required {
        let want = safe_rel(r).ok_or_else(|| InstallError::UnsafeEntry((*r).into()))?;
        if !entries.iter().any(|(_, p)| *p == want) {
            return Err(InstallError::MissingRequired((*r).into()));
        }
    }
    // 2) 임시 폴더에 풀기(설치 폴더와 같은 드라이브라 이름 바꾸기가 원자적이다)
    let stage = install_dir.with_file_name(format!(
        "{}.kova_stage",
        install_dir
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| "install".into())
    ));
    if stage.exists() {
        fs::remove_dir_all(&stage)?;
    }
    fs::create_dir_all(&stage)?;
    let mut extract = || -> Result<(), InstallError> {
        for (i, rel) in &entries {
            let mut f = z
                .by_index(*i)
                .map_err(|e| InstallError::Zip(e.to_string()))?;
            let dst = stage.join(rel);
            if let Some(p) = dst.parent() {
                fs::create_dir_all(p)?;
            }
            let mut buf = Vec::new();
            f.read_to_end(&mut buf)?;
            fs::write(&dst, buf)?;
        }
        Ok(())
    };
    if let Err(e) = extract() {
        let _ = fs::remove_dir_all(&stage);
        return Err(e);
    }
    // 3) 교체 + 롤백 기록
    fs::create_dir_all(install_dir)?;
    fs::create_dir_all(backup_dir)?;
    let mut moved_old: Vec<PathBuf> = vec![]; // 백업으로 옮긴 기존 파일(상대 경로)
    let mut placed_new: Vec<PathBuf> = vec![]; // 새로 놓은 파일(상대 경로)
    let mut result = Installed::default();
    let swap = |moved_old: &mut Vec<PathBuf>,
                placed_new: &mut Vec<PathBuf>,
                result: &mut Installed|
     -> io::Result<()> {
        for (_, rel) in &entries {
            let (dst, bak, new) = (install_dir.join(rel), backup_dir.join(rel), stage.join(rel));
            if let Some(p) = dst.parent() {
                fs::create_dir_all(p)?;
            }
            if dst.exists() {
                if let Some(p) = bak.parent() {
                    fs::create_dir_all(p)?;
                }
                if bak.exists() {
                    fs::remove_file(&bak)?;
                }
                fs::rename(&dst, &bak)?;
                moved_old.push(rel.clone());
                result.replaced += 1;
            }
            fs::rename(&new, &dst)?;
            placed_new.push(rel.clone());
            result.files += 1;
        }
        Ok(())
    };
    match swap(&mut moved_old, &mut placed_new, &mut result) {
        Ok(()) => {
            let _ = fs::remove_dir_all(&stage);
            Ok(result)
        }
        Err(e) => {
            // 롤백: 새로 놓은 것을 치우고 백업을 되돌린다
            for rel in &placed_new {
                let _ = fs::remove_file(install_dir.join(rel));
            }
            for rel in &moved_old {
                let _ = fs::rename(backup_dir.join(rel), install_dir.join(rel));
            }
            let _ = fs::remove_dir_all(&stage);
            Err(InstallError::Io(e))
        }
    }
}

/// 직전 `apply` 가 남긴 백업으로 되돌린다(설치 후 문제가 드러났을 때). 백업에 있는 파일만 복원한다.
pub fn rollback(install_dir: &Path, backup_dir: &Path) -> io::Result<usize> {
    fn walk(dir: &Path, base: &Path, out: &mut Vec<PathBuf>) -> io::Result<()> {
        for e in fs::read_dir(dir)? {
            let p = e?.path();
            if p.is_dir() {
                walk(&p, base, out)?;
            } else {
                out.push(p.strip_prefix(base).unwrap().to_path_buf());
            }
        }
        Ok(())
    }
    let mut files = vec![];
    if backup_dir.is_dir() {
        walk(backup_dir, backup_dir, &mut files)?;
    }
    for rel in &files {
        let dst = install_dir.join(rel);
        if let Some(p) = dst.parent() {
            fs::create_dir_all(p)?;
        }
        if dst.exists() {
            fs::remove_file(&dst)?;
        }
        fs::copy(backup_dir.join(rel), &dst)?;
    }
    Ok(files.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_zip(files: &[(&str, &[u8])]) -> Vec<u8> {
        let mut w = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let o = zip::write::SimpleFileOptions::default();
        for (n, d) in files {
            w.start_file(*n, o).unwrap();
            w.write_all(d).unwrap();
        }
        w.finish().unwrap().into_inner()
    }

    fn dirs() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let d = tempfile::tempdir().unwrap();
        let (i, b) = (d.path().join("app"), d.path().join("backup"));
        fs::create_dir_all(&i).unwrap();
        (d, i, b)
    }

    #[test]
    fn applies_replaces_and_rolls_back_on_demand() {
        let (_d, i, b) = dirs();
        fs::write(i.join("kova.exe"), b"old").unwrap();
        fs::write(i.join("keep.txt"), b"keep").unwrap();
        let z = make_zip(&[("kova.exe", b"new"), ("engine/kova-engine.exe", b"eng")]);
        let r = apply(&z, &i, &b, &["engine/kova-engine.exe"]).unwrap();
        assert_eq!(
            r,
            Installed {
                files: 2,
                replaced: 1
            }
        );
        assert_eq!(fs::read(i.join("kova.exe")).unwrap(), b"new");
        assert_eq!(
            fs::read(i.join("keep.txt")).unwrap(),
            b"keep",
            "패키지에 없는 파일은 그대로"
        );
        assert!(!i.with_file_name("app.kova_stage").exists());
        // 설치 후 되돌리기: 교체됐던 파일만 복원된다
        assert_eq!(rollback(&i, &b).unwrap(), 1);
        assert_eq!(fs::read(i.join("kova.exe")).unwrap(), b"old");
    }

    #[test]
    fn unsafe_or_incomplete_packages_touch_nothing() {
        let (_d, i, b) = dirs();
        fs::write(i.join("kova.exe"), b"old").unwrap();
        for bad in [
            make_zip(&[("../evil.exe", b"x"), ("kova.exe", b"new")]),
            make_zip(&[("C:/evil.exe", b"x")]),
            make_zip(&[("/abs.exe", b"x")]),
            make_zip(&[
                ("kova.exe", b"new"),
                ("keys/ed25519_private.pem", b"secret"),
            ]),
            make_zip(&[("kova.exe", b"new"), ("signing.key", b"secret")]),
        ] {
            assert!(matches!(
                apply(&bad, &i, &b, &[]),
                Err(InstallError::UnsafeEntry(_))
            ));
        }
        let no_engine = make_zip(&[("kova.exe", b"new")]);
        assert!(matches!(
            apply(&no_engine, &i, &b, &["engine/kova-engine.exe"]),
            Err(InstallError::MissingRequired(_))
        ));
        assert!(matches!(
            apply(b"not a zip", &i, &b, &[]),
            Err(InstallError::Zip(_))
        ));
        assert_eq!(fs::read(i.join("kova.exe")).unwrap(), b"old");
        assert!(!i.parent().unwrap().join("evil.exe").exists());
    }

    #[test]
    fn failure_midway_restores_the_original_files() {
        let (_d, i, b) = dirs();
        fs::write(i.join("a.exe"), b"old-a").unwrap();
        // 두 번째 항목의 목적지가 "파일이 있는 자리에 폴더가 필요"한 상황을 만들어 중간 실패를 일으킨다
        fs::write(i.join("sub"), b"i am a file").unwrap();
        let z = make_zip(&[("a.exe", b"new-a"), ("sub/b.exe", b"new-b")]);
        assert!(apply(&z, &i, &b, &[]).is_err());
        assert_eq!(
            fs::read(i.join("a.exe")).unwrap(),
            b"old-a",
            "이미 교체한 파일도 원래대로"
        );
        assert_eq!(fs::read(i.join("sub")).unwrap(), b"i am a file");
    }
}
