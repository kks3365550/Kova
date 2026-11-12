//! 업데이트 조회·다운로드 (core/updater.py 의 GitHub 웹 폴백 방식 이식).
//! `releases/latest` 가 가리키는 태그를 리다이렉트로 알아내고, 패키지는 크기 상한을 두고 메모리로 받는다.
//! 받은 바이트는 반드시 `update::verify_package` 를 통과한 뒤에만 `install::apply` 로 넘긴다.
use std::io::Read;
use std::time::Duration;

#[derive(Debug)]
pub enum FetchError {
    Http(String),
    /// 응답이 기대한 형태가 아님(태그 주소가 아님 등)
    Unexpected(String),
    TooLarge(u64),
}
impl std::fmt::Display for FetchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Http(e) => write!(f, "네트워크 오류: {e}"),
            Self::Unexpected(e) => write!(f, "예상 밖 응답: {e}"),
            Self::TooLarge(n) => write!(f, "패키지가 너무 큼(상한 {n}바이트)"),
        }
    }
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(30))
        .user_agent("Kova-Updater")
        .build()
}

/// `{base}/{repo}/releases/latest` 의 최종 주소에서 태그를 뽑는다. 예: base="https://github.com", repo="owner/Kova".
pub fn latest_tag(base: &str, repo: &str) -> Result<String, FetchError> {
    let url = format!("{}/{}/releases/latest", base.trim_end_matches('/'), repo);
    let resp = agent()
        .get(&url)
        .call()
        .map_err(|e| FetchError::Http(e.to_string()))?;
    let final_url = resp.get_url().to_string();
    final_url
        .split("/releases/tag/")
        .nth(1)
        .map(|t| t.trim().trim_end_matches('/').to_string())
        .filter(|t| !t.is_empty())
        .ok_or(FetchError::Unexpected(final_url))
}

/// 파이썬 릴리즈 관례: release_<tag>.zip
pub fn package_url(base: &str, repo: &str, tag: &str) -> String {
    format!(
        "{}/{}/releases/download/{}/release_{}.zip",
        base.trim_end_matches('/'),
        repo,
        tag,
        tag
    )
}

/// 최대 max_bytes 까지만 받는다(초과하면 중단·오류).
pub fn download(url: &str, max_bytes: u64) -> Result<Vec<u8>, FetchError> {
    let resp = agent()
        .get(url)
        .call()
        .map_err(|e| FetchError::Http(e.to_string()))?;
    let mut buf = Vec::new();
    resp.into_reader()
        .take(max_bytes + 1)
        .read_to_end(&mut buf)
        .map_err(|e| FetchError::Http(e.to_string()))?;
    if buf.len() as u64 > max_bytes {
        return Err(FetchError::TooLarge(max_bytes));
    }
    Ok(buf)
}

/// 배포 서명에 쓰는 신뢰 공개키(공개키만 포함). 키를 바꾸면 여기와 릴리즈 서명을 함께 바꾼다.
pub const TRUSTED_KEYS: &[&str] = &[
    include_str!("../keys/release_ed25519_v2.pub"),
    // Kova2 릴리즈 서명 키(개인키는 저장소 밖 %USERPROFILE%\.kova2_keys 에만 있다)
    include_str!("../keys/release_ed25519_kova2.pub"),
];

#[derive(Debug)]
pub enum UpdateError {
    Fetch(FetchError),
    Verify(crate::update::VerifyError),
    /// 서명 파일이 hex 문자열이 아님
    BadSignatureFile,
}
impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Fetch(e) => write!(f, "{e}"),
            Self::Verify(e) => write!(f, "{e}"),
            Self::BadSignatureFile => write!(f, "서명 파일 형식 오류"),
        }
    }
}
impl From<FetchError> for UpdateError {
    fn from(e: FetchError) -> Self {
        Self::Fetch(e)
    }
}

/// 패키지(`release_<tag>.zip`)와 서명(`.zip.sig`)을 받아 검증까지 마친 바이트를 돌려준다. 설치는 하지 않는다.
pub fn fetch_verified(
    base: &str,
    repo: &str,
    tag: &str,
    trusted: &[&str],
    max_bytes: u64,
) -> Result<Vec<u8>, UpdateError> {
    let url = package_url(base, repo, tag);
    let pkg = download(&url, max_bytes)?;
    let sig = download(&format!("{url}.sig"), 4096)?;
    let sig = String::from_utf8(sig).map_err(|_| UpdateError::BadSignatureFile)?;
    let sig = sig.trim();
    if sig.is_empty() || !sig.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(UpdateError::BadSignatureFile);
    }
    crate::update::verify_package(&pkg, "", sig, trusted).map_err(UpdateError::Verify)?;
    Ok(pkg)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    /// 시험용 로컬 HTTP 서버: 연결마다 한 요청만 처리한다.
    fn serve(n: usize) -> String {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        std::thread::spawn(move || {
            for _ in 0..n {
                let (mut s, _) = l.accept().unwrap();
                let mut line = String::new();
                BufReader::new(s.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
                let resp = if path.ends_with("/releases/latest") {
                    "HTTP/1.1 302 Found\r\nLocation: /o/Kova/releases/tag/v9.9.9\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_string()
                } else if path.contains("/releases/tag/") {
                    "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                        .to_string()
                } else if path == "/big" {
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}",
                        "x".repeat(100)
                    )
                } else {
                    "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        .to_string()
                };
                let _ = s.write_all(resp.as_bytes());
            }
        });
        format!("http://{addr}")
    }

    #[test]
    fn latest_tag_follows_redirect_and_urls_are_built() {
        let base = serve(2);
        assert_eq!(latest_tag(&base, "o/Kova").unwrap(), "v9.9.9");
        assert_eq!(
            package_url("https://github.com/", "o/Kova", "v9.9.9"),
            "https://github.com/o/Kova/releases/download/v9.9.9/release_v9.9.9.zip"
        );
        // 404 면 오류
        assert!(matches!(
            latest_tag(&serve(1), "x/y"),
            Err(FetchError::Http(_))
        ));
        let _ = base;
    }

    #[test]
    fn download_respects_size_cap() {
        let base = serve(2);
        assert_eq!(download(&format!("{base}/big"), 100).unwrap().len(), 100);
        assert!(matches!(
            download(&format!("{base}/big"), 99),
            Err(FetchError::TooLarge(99))
        ));
    }
}
