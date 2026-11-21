//! 스냅샷 복원 대상 폴더 검사: 새 폴더(또는 빈 폴더)만 허용한다. 장치·드라이브 루트·기존 파일·내용이 있는 폴더·저장소 안쪽은 거부한다.
use std::path::{Path, PathBuf};

#[derive(Debug, PartialEq, Eq)]
pub enum TargetError {
    Empty,
    NotAbsolute,
    DriveRoot,
    Device,
    NotEmpty,
    IsFile,
    InsideRepo,
}

impl std::fmt::Display for TargetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            Self::Empty => "복원할 폴더 경로를 입력하세요",
            Self::NotAbsolute => "전체 경로(예: D:\\복원)를 입력하세요",
            Self::DriveRoot => "드라이브 루트에는 복원할 수 없습니다 — 새 폴더를 지정하세요",
            Self::Device => "장치 경로에는 복원할 수 없습니다",
            Self::NotEmpty => "이미 내용이 있는 폴더입니다 — 새 폴더나 빈 폴더를 지정하세요",
            Self::IsFile => "같은 이름의 파일이 이미 있습니다",
            Self::InsideRepo => "백업 저장소 안쪽에는 복원할 수 없습니다",
        };
        f.write_str(s)
    }
}

/// `\` 로 구분된 Windows 경로의 `.`·`..`·빈 조각을 글자 그대로 풀어 정리한다(파일 시스템은 보지 않는다).
/// 드라이브(`D:`)나 UNC 공유(`\\서버\공유`) 위로는 올라가지 않는다.
pub(crate) fn lexical_normalize(path: &str) -> String {
    let (prefix, rest) = if let Some(r) = path.strip_prefix("\\\\") {
        let mut it = r.splitn(3, '\\');
        let server = it.next().unwrap_or("");
        let share = it.next().unwrap_or("");
        (format!("\\\\{server}\\{share}"), it.next().unwrap_or(""))
    } else if path.len() >= 2 && path.as_bytes()[1] == b':' {
        (path[..2].to_string(), &path[2..])
    } else {
        (String::new(), path)
    };
    let mut parts: Vec<&str> = Vec::new();
    for c in rest.split('\\') {
        match c {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            x => parts.push(x),
        }
    }
    format!("{prefix}\\{}", parts.join("\\"))
}

/// 통과하면 정리된 대상 경로를 돌려준다. 폴더는 아직 만들지 않는다(엔진이 만든다).
pub fn check(target: &str, repo: &Path) -> Result<PathBuf, TargetError> {
    let t = target.trim().trim_matches('"').trim();
    if t.is_empty() {
        return Err(TargetError::Empty);
    }
    let norm = t.replace('/', "\\");
    if norm.starts_with("\\\\.\\") || norm.starts_with("\\\\?\\") {
        return Err(TargetError::Device);
    }
    let b = norm.as_bytes();
    let unc = norm.starts_with("\\\\");
    if !unc && !(b.len() >= 3 && b[1] == b':' && b[2] == b'\\') {
        // "D:" 처럼 문자만 있는 경우도 여기서 걸린다
        return Err(TargetError::NotAbsolute);
    }
    // `D:\a\..\저장소\x` 처럼 `..` 로 돌아가는 경로가 저장소 안쪽·드라이브 루트 검사를 빠져나가지 않게 정리한다
    let p = PathBuf::from(lexical_normalize(&norm));
    if p.parent().is_none() || p.file_name().is_none() {
        return Err(TargetError::DriveRoot);
    }
    let lower = |x: &Path| {
        lexical_normalize(&x.to_string_lossy().replace('/', "\\"))
            .trim_end_matches('\\')
            .to_lowercase()
    };
    let (tl, rl) = (lower(&p), lower(repo));
    if !rl.is_empty() && (tl == rl || tl.starts_with(&format!("{rl}\\"))) {
        return Err(TargetError::InsideRepo);
    }
    if p.is_file() {
        return Err(TargetError::IsFile);
    }
    if p.is_dir()
        && std::fs::read_dir(&p)
            .map(|mut d| d.next().is_some())
            .unwrap_or(true)
    {
        return Err(TargetError::NotEmpty);
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_new_or_empty_absolute_folders_outside_repo_pass() {
        let d = tempfile::tempdir().unwrap();
        let repo = d.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let ok = d.path().join("new_restore");
        assert_eq!(check(ok.to_str().unwrap(), &repo).unwrap(), ok);
        let empty = d.path().join("empty");
        std::fs::create_dir_all(&empty).unwrap();
        assert!(check(empty.to_str().unwrap(), &repo).is_ok());
        let full = d.path().join("full");
        std::fs::create_dir_all(&full).unwrap();
        std::fs::write(full.join("a"), b"x").unwrap();
        assert_eq!(
            check(full.to_str().unwrap(), &repo),
            Err(TargetError::NotEmpty)
        );
        let f = d.path().join("file.txt");
        std::fs::write(&f, b"x").unwrap();
        assert_eq!(check(f.to_str().unwrap(), &repo), Err(TargetError::IsFile));
        assert_eq!(
            check(repo.join("sub").to_str().unwrap(), &repo),
            Err(TargetError::InsideRepo)
        );
        assert_eq!(check("", &repo), Err(TargetError::Empty));
        assert_eq!(check("relative\\x", &repo), Err(TargetError::NotAbsolute));
        assert_eq!(check("D:", &repo), Err(TargetError::NotAbsolute));
        assert_eq!(check("D:\\", &repo), Err(TargetError::DriveRoot));
        assert_eq!(
            check(r"\\.\PhysicalDrive0", &repo),
            Err(TargetError::Device)
        );
    }

    /// `..`·`.` 로 돌아가는 경로도 정리한 뒤 검사한다(저장소 안쪽·드라이브 루트 우회 차단).
    #[test]
    fn dot_dot_paths_are_normalized_before_checks() {
        let d = tempfile::tempdir().unwrap();
        let repo = d.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let sneaky = format!("{}\\other\\..\\repo\\.\\sub", d.path().display());
        assert_eq!(check(&sneaky, &repo), Err(TargetError::InsideRepo));
        let via_repo = d.path().join("other").join("..").join("REPO").join("x");
        assert_eq!(
            check(via_repo.to_str().unwrap(), &repo),
            Err(TargetError::InsideRepo)
        );
        assert_eq!(check(r"D:\a\..", &repo), Err(TargetError::DriveRoot));
        assert_eq!(check(r"D:\..\..", &repo), Err(TargetError::DriveRoot));
        let ok = format!("{}\\x\\..\\fresh", d.path().display());
        assert_eq!(check(&ok, &repo).unwrap(), d.path().join("fresh"));
        assert_eq!(
            lexical_normalize(r"\\srv\share\a\..\..\b"),
            r"\\srv\share\b"
        );
    }
}
