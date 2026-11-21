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

/// 연결·읽기 시간 제한을 따로 둔다(전체 제한 하나면 큰 패키지는 느린 회선에서 반드시 끊긴다).
/// 읽기 제한은 "응답이 멈춘 시간" 기준이라 천천히라도 받는 중이면 끊지 않는다.
fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(60))
        .timeout_write(Duration::from_secs(60))
        .user_agent("Kova-Updater")
        .build()
}

const RETRIES: u32 = 3;

/// 전송 오류·5xx·429 는 짧게 기다렸다가 다시 시도한다(4xx 는 바로 실패).
fn get_with_retry(url: &str, wait: Duration) -> Result<ureq::Response, FetchError> {
    let mut tries = 0;
    loop {
        match agent().get(url).call() {
            Ok(r) => return Ok(r),
            Err(ureq::Error::Status(c, _)) if (c == 429 || c >= 500) && tries + 1 < RETRIES => {}
            Err(ureq::Error::Transport(_)) if tries + 1 < RETRIES => {}
            Err(e) => return Err(FetchError::Http(e.to_string())),
        }
        tries += 1;
        std::thread::sleep(wait * tries);
    }
}

const RETRY_WAIT: Duration = Duration::from_secs(2);

/// `{base}/{repo}/releases/latest` 의 최종 주소에서 태그를 뽑는다. 예: base="https://github.com", repo="owner/Kova".
pub fn latest_tag(base: &str, repo: &str) -> Result<String, FetchError> {
    let url = format!("{}/{}/releases/latest", base.trim_end_matches('/'), repo);
    let resp = get_with_retry(&url, RETRY_WAIT)?;
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
    download_with_wait(url, max_bytes, RETRY_WAIT)
}

fn download_with_wait(url: &str, max_bytes: u64, wait: Duration) -> Result<Vec<u8>, FetchError> {
    read_capped(get_with_retry(url, wait)?, max_bytes)
}

fn read_capped(resp: ureq::Response, max_bytes: u64) -> Result<Vec<u8>, FetchError> {
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

/// 자산이 없으면(404) Ok(None). 그 밖의 오류는 재시도 경로로 다시 받아 보고, 그래도 실패하면 오류(fail-closed).
fn download_if_exists(
    url: &str,
    max_bytes: u64,
    wait: Duration,
) -> Result<Option<Vec<u8>>, FetchError> {
    match agent().get(url).call() {
        Ok(r) => read_capped(r, max_bytes).map(Some),
        Err(ureq::Error::Status(404, _)) => Ok(None),
        Err(_) => download_with_wait(url, max_bytes, wait).map(Some),
    }
}

/// `.sig2` 가 없을 때 옛 `.sig`(zip 바이트만 서명, 태그와 안 묶임)로 검증하는 것을 허용할지.
/// 기존 설치본(3.15.x/3.16.0)용으로 `.sig` 도 계속 게시하고, 새 릴리즈부터 `.sig2` 를 함께 게시한다.
/// 계획: `.sig2` 를 게시한 릴리즈가 한 번 나간 뒤 다음 릴리즈에서 false 로 바꾼다
/// (`.sig2` 를 지우고 옛 `.sig` 만 남기는 공격은 그때까지 `ensure_package_newer` 가 막는다).
pub const ALLOW_LEGACY_SIG: bool = true;

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
    /// 패키지 안 프로그램 버전이 현재보다 높지 않음(또는 읽을 수 없음) — 옛 서명 패키지로 되돌리기 방지
    NotNewer(String),
}
impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Fetch(e) => write!(f, "{e}"),
            Self::Verify(e) => write!(f, "{e}"),
            Self::BadSignatureFile => write!(f, "서명 파일 형식 오류"),
            Self::NotNewer(e) => write!(f, "설치하지 않음: {e}"),
        }
    }
}
impl From<FetchError> for UpdateError {
    fn from(e: FetchError) -> Self {
        Self::Fetch(e)
    }
}

/// 패키지(`release_<tag>.zip`)와 서명을 받아 검증까지 마친 바이트를 돌려준다. 설치는 하지 않는다.
/// `.zip.sig2`(태그에 묶인 서명)가 있으면 반드시 그것으로 검증하고(태그 불일치 거부),
/// 없을 때만 `ALLOW_LEGACY_SIG` 가 켜져 있으면 옛 `.zip.sig` 로 검증한다.
pub fn fetch_verified(
    base: &str,
    repo: &str,
    tag: &str,
    trusted: &[&str],
    max_bytes: u64,
) -> Result<Vec<u8>, UpdateError> {
    fetch_verified_with(
        base,
        repo,
        tag,
        trusted,
        max_bytes,
        ALLOW_LEGACY_SIG,
        RETRY_WAIT,
    )
}

fn parse_sig_file(sig: Vec<u8>) -> Result<String, UpdateError> {
    let sig = String::from_utf8(sig).map_err(|_| UpdateError::BadSignatureFile)?;
    let sig = sig.trim();
    if sig.is_empty() || !sig.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(UpdateError::BadSignatureFile);
    }
    Ok(sig.to_string())
}

fn fetch_verified_with(
    base: &str,
    repo: &str,
    tag: &str,
    trusted: &[&str],
    max_bytes: u64,
    allow_legacy: bool,
    wait: Duration,
) -> Result<Vec<u8>, UpdateError> {
    let url = package_url(base, repo, tag);
    let pkg = download_with_wait(&url, max_bytes, wait)?;
    if let Some(sig2) = download_if_exists(&format!("{url}.sig2"), 4096, wait)? {
        let sig2 = parse_sig_file(sig2)?;
        crate::update::verify_package_v2(&pkg, tag, &sig2, trusted).map_err(UpdateError::Verify)?;
        return Ok(pkg);
    }
    if !allow_legacy {
        return Err(UpdateError::Verify(crate::update::VerifyError::NoSignature));
    }
    let sig = parse_sig_file(download_with_wait(&format!("{url}.sig"), 4096, wait)?)?;
    crate::update::verify_package(&pkg, "", &sig, trusted).map_err(UpdateError::Verify)?;
    Ok(pkg)
}

/// 패키지(ZIP) 안 `exe` 의 파일 버전(VS_FIXEDFILEINFO)을 "a.b.c.d" 로 읽는다. 없거나 못 읽으면 None.
pub fn package_exe_version(zip_bytes: &[u8], exe: &str) -> Option<String> {
    let mut z = zip::ZipArchive::new(std::io::Cursor::new(zip_bytes)).ok()?;
    let mut f = z.by_name(exe).ok()?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf).ok()?;
    let sig = 0xFEEF_04BDu32.to_le_bytes();
    let rd =
        |o: usize| -> Option<u32> { Some(u32::from_le_bytes(buf.get(o..o + 4)?.try_into().ok()?)) };
    // 서명 뒤 구조 버전(1.0)까지 맞는 첫 위치를 쓴다
    let pos = (0..buf.len().saturating_sub(16))
        .find(|&i| buf[i..i + 4] == sig && rd(i + 4) == Some(0x0001_0000))?;
    let (ms, ls) = (rd(pos + 8)?, rd(pos + 12)?);
    Some(format!(
        "{}.{}.{}.{}",
        ms >> 16,
        ms & 0xffff,
        ls >> 16,
        ls & 0xffff
    ))
}

/// 설치 직전 확인: 서명 검증을 마친 패키지 안 `exe` 의 버전이 `current` 보다 높아야 한다.
/// 서명이 버전에 묶여 있지 않아, 새 태그 이름으로 옛 서명 패키지를 내려보내는 되돌리기를 여기서 막는다.
/// 버전을 읽지 못하면 설치하지 않는다(fail-closed). 통과하면 패키지 버전을 돌려준다.
pub fn ensure_package_newer(
    zip_bytes: &[u8],
    exe: &str,
    current: &str,
) -> Result<String, UpdateError> {
    let v = package_exe_version(zip_bytes, exe)
        .ok_or_else(|| UpdateError::NotNewer(format!("패키지의 {exe} 버전을 읽을 수 없습니다")))?;
    if !crate::update::is_newer(&v, current) {
        return Err(UpdateError::NotNewer(format!(
            "패키지 버전 {v} 이 현재 {current} 보다 높지 않습니다"
        )));
    }
    Ok(v)
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
    fn transient_5xx_is_retried_then_succeeds_and_4xx_is_not() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        let hits = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let h2 = hits.clone();
        std::thread::spawn(move || {
            for conn in l.incoming() {
                let Ok(mut s) = conn else { continue };
                let mut line = String::new();
                BufReader::new(s.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let n = h2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let resp = if line.contains("/flaky") && n == 0 {
                    "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else if line.contains("/flaky") {
                    "HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc"
                } else {
                    "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                };
                let _ = s.write_all(resp.as_bytes());
            }
        });
        let base = format!("http://{addr}");
        let w = Duration::from_millis(10);
        assert_eq!(
            download_with_wait(&format!("{base}/flaky"), 10, w).unwrap(),
            b"abc"
        );
        assert_eq!(hits.load(std::sync::atomic::Ordering::SeqCst), 2);
        assert!(download_with_wait(&format!("{base}/missing"), 10, w).is_err());
        assert_eq!(
            hits.load(std::sync::atomic::Ordering::SeqCst),
            3,
            "404 는 재시도 안 함"
        );
    }

    fn zip_with(name: &str, data: &[u8]) -> Vec<u8> {
        let mut w = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        w.start_file(name, zip::write::SimpleFileOptions::default())
            .unwrap();
        w.write_all(data).unwrap();
        w.finish().unwrap().into_inner()
    }

    fn fake_exe(a: u32, b: u32, c: u32) -> Vec<u8> {
        let mut v = b"MZ....VS_VERSION_INFO".to_vec();
        v.extend(0xFEEF_04BDu32.to_le_bytes());
        v.extend(0x0001_0000u32.to_le_bytes());
        v.extend((a << 16 | b).to_le_bytes());
        v.extend((c << 16).to_le_bytes());
        v.extend([0u8; 40]);
        v
    }

    #[test]
    fn package_version_must_be_newer_than_current() {
        let pkg = zip_with("kova-app.exe", &fake_exe(3, 15, 5));
        assert_eq!(
            package_exe_version(&pkg, "kova-app.exe").as_deref(),
            Some("3.15.5.0")
        );
        assert_eq!(
            ensure_package_newer(&pkg, "kova-app.exe", "3.15.4").unwrap(),
            "3.15.5.0"
        );
        // 같은 버전·옛 버전(되돌리기)·버전 정보 없음은 거부
        assert!(matches!(
            ensure_package_newer(&pkg, "kova-app.exe", "3.15.5"),
            Err(UpdateError::NotNewer(_))
        ));
        let old = zip_with("kova-app.exe", &fake_exe(3, 14, 0));
        assert!(ensure_package_newer(&old, "kova-app.exe", "3.15.4").is_err());
        let none = zip_with("kova-app.exe", b"MZ no resource");
        assert!(ensure_package_newer(&none, "kova-app.exe", "3.15.4").is_err());
        assert!(ensure_package_newer(&pkg, "other.exe", "1.0").is_err());
    }

    /// 시험용 릴리즈 자산 서버: 경로→내용 표에 있으면 200, 없으면 404.
    fn serve_assets(assets: Vec<(String, Vec<u8>)>) -> String {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        std::thread::spawn(move || {
            for conn in l.incoming() {
                let Ok(mut s) = conn else { continue };
                let mut line = String::new();
                BufReader::new(s.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
                let resp = match assets.iter().find(|(p, _)| *p == path) {
                    Some((_, body)) => {
                        let mut r = format!(
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes();
                        r.extend_from_slice(body);
                        r
                    }
                    None => {
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            .to_vec()
                    }
                };
                let _ = s.write_all(&resp);
            }
        });
        format!("http://{addr}")
    }

    fn asset(tag: &str, ext: &str, body: &[u8]) -> (String, Vec<u8>) {
        (
            format!("/o/Kova/releases/download/{tag}/release_{tag}.zip{ext}"),
            body.to_vec(),
        )
    }

    fn fetch(base: &str, tag: &str, public: &str, legacy: bool) -> Result<Vec<u8>, UpdateError> {
        fetch_verified_with(
            base,
            "o/Kova",
            tag,
            &[public],
            1 << 20,
            legacy,
            Duration::from_millis(10),
        )
    }

    #[test]
    fn fetch_verified_prefers_tag_bound_sig2_and_falls_back_to_legacy_sig() {
        let (private, public) = crate::sign::generate_key_pair().unwrap();
        let (new_tag, old_tag) = ("kova2-v3.17.0", "kova2-v3.15.0");
        let new_zip = b"new package".to_vec();
        let old_zip = b"old package".to_vec();
        let sig1 = |z: &[u8]| crate::sign::sign_bytes(z, &private).unwrap().into_bytes();
        let sig2 = |z: &[u8], t: &str| {
            crate::update::sign_package_v2(z, t, &private)
                .unwrap()
                .into_bytes()
        };

        // 1) 올바른 태그의 .sig2 통과
        let base = serve_assets(vec![
            asset(new_tag, "", &new_zip),
            asset(new_tag, ".sig", &sig1(&new_zip)),
            asset(new_tag, ".sig2", &sig2(&new_zip, new_tag)),
        ]);
        assert_eq!(fetch(&base, new_tag, &public, true).unwrap(), new_zip);
        assert_eq!(fetch(&base, new_tag, &public, false).unwrap(), new_zip);

        // 2) 옛 정식 패키지를 새 태그로 올림: .sig 는 맞아도 .sig2 의 태그가 달라 거부(폴백 안 함)
        let base = serve_assets(vec![
            asset(new_tag, "", &old_zip),
            asset(new_tag, ".sig", &sig1(&old_zip)),
            asset(new_tag, ".sig2", &sig2(&old_zip, old_tag)),
        ]);
        assert!(matches!(
            fetch(&base, new_tag, &public, true),
            Err(UpdateError::Verify(
                crate::update::VerifyError::BadSignature
            ))
        ));

        // 3) 변조 zip 거부
        let base = serve_assets(vec![
            asset(new_tag, "", b"tampered"),
            asset(new_tag, ".sig", &sig1(&new_zip)),
            asset(new_tag, ".sig2", &sig2(&new_zip, new_tag)),
        ]);
        assert!(matches!(
            fetch(&base, new_tag, &public, true),
            Err(UpdateError::Verify(
                crate::update::VerifyError::BadSignature
            ))
        ));

        // 4) .sig2 형식 오류면 .sig 로 폴백하지 않는다
        let base = serve_assets(vec![
            asset(new_tag, "", &new_zip),
            asset(new_tag, ".sig", &sig1(&new_zip)),
            asset(new_tag, ".sig2", b"not hex!"),
        ]);
        assert!(matches!(
            fetch(&base, new_tag, &public, true),
            Err(UpdateError::BadSignatureFile)
        ));

        // 5) .sig2 없음: 폴백 허용이면 옛 .sig 로 통과(기존 릴리즈 호환), 불허면 거부
        let base = serve_assets(vec![
            asset(new_tag, "", &new_zip),
            asset(new_tag, ".sig", &sig1(&new_zip)),
        ]);
        assert_eq!(fetch(&base, new_tag, &public, true).unwrap(), new_zip);
        assert!(matches!(
            fetch(&base, new_tag, &public, false),
            Err(UpdateError::Verify(crate::update::VerifyError::NoSignature))
        ));

        // 6) .sig2 없음 + 틀린 .sig·서명 없음은 폴백해도 거부
        let base = serve_assets(vec![
            asset(new_tag, "", &new_zip),
            asset(new_tag, ".sig", &sig1(b"other")),
        ]);
        assert!(fetch(&base, new_tag, &public, true).is_err());
        let base = serve_assets(vec![asset(new_tag, "", &new_zip)]);
        assert!(matches!(
            fetch(&base, new_tag, &public, true),
            Err(UpdateError::Fetch(_))
        ));
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
