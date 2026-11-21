//! 원격 웹훅 알림: 백업 실패·경고(또는 선택 시 성공) 이벤트를 Slack / Discord / Teams / 일반 HTTP 웹훅으로 발송한다.
use chrono::Local;
use std::time::Duration;

pub const WEBHOOK_TIMEOUT: Duration = Duration::from_secs(10);

pub fn validate_webhook_url(url: &str) -> Result<&str, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("웹훅 URL이 비어 있습니다".into());
    }
    if !(trimmed.starts_with("https://") || trimmed.starts_with("http://")) {
        return Err("웹훅 URL은 http:// 또는 https:// 로 시작해야 합니다".into());
    }
    Ok(trimmed)
}

/// 웹훅 URL 은 경로·쿼리에 비밀 토큰이 들어 있으므로 로그·오류에는 `scheme://host/***` 만 남긴다.
pub fn mask_url(url: &str) -> String {
    let t = url.trim();
    let Some((scheme, rest)) = t.split_once("://") else {
        return "***".into();
    };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    // 사용자 정보(user:pass@)도 숨긴다
    let host = host.rsplit('@').next().unwrap_or("");
    format!("{scheme}://{host}/***")
}

/// 문자열 안의 웹훅 URL(앞뒤 공백 제거 전·후 모두)을 가린다.
pub fn redact(text: &str, url: &str) -> String {
    let t = url.trim();
    if t.is_empty() {
        return text.to_string();
    }
    let m = mask_url(t);
    let mut out = text.replace(t, &m);
    if url != t && !url.is_empty() {
        out = out.replace(url, &m);
    }
    out
}

/// Slack(`text`) · Discord(`content`) · 일반 JSON 수신기 모두 호환되는 페이로드를 POST 한다.
pub fn send_webhook(url: &str, level: &str, title: &str, message: &str) -> Result<(), String> {
    let url = validate_webhook_url(url)?;
    let host = std::env::var("COMPUTERNAME").unwrap_or_else(|_| "UNKNOWN".into());
    let ts = Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let line = format!("[Kova2][{level}][{host}] {title}: {message}");
    let body = serde_json::json!({
        "text": line,
        "content": line,
        "level": level,
        "title": title,
        "message": message,
        "host": host,
        "timestamp": ts,
    });
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(WEBHOOK_TIMEOUT)
        .timeout_read(WEBHOOK_TIMEOUT)
        .timeout_write(WEBHOOK_TIMEOUT)
        .build();
    let resp = agent
        .post(url)
        .set("Content-Type", "application/json")
        .send_string(&body.to_string())
        // ureq 오류 문구에는 요청 URL 이 그대로 들어가므로 가린다
        .map_err(|e| redact(&format!("웹훅 전송 실패: {e}"), url))?;
    let status = resp.status();
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!("웹훅 응답 오류: HTTP {status}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    #[test]
    fn validates_scheme() {
        assert!(validate_webhook_url("").is_err());
        assert!(validate_webhook_url("ftp://example.com").is_err());
        assert_eq!(
            validate_webhook_url("  https://example.com/hook  ").unwrap(),
            "https://example.com/hook"
        );
    }

    #[test]
    fn webhook_url_is_masked_in_errors() {
        let secret = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN";
        assert_eq!(mask_url(secret), "https://hooks.slack.com/***");
        assert_eq!(mask_url("http://u:p@h:9/x?y"), "http://h:9/***");
        assert_eq!(mask_url("garbage"), "***");
        assert_eq!(
            redact(&format!("err {secret}: boom"), &format!(" {secret} ")),
            "err https://hooks.slack.com/***: boom"
        );
        // 연결이 거부되는 주소로 보내도 오류 문구에 토큰이 남지 않는다
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        let url = format!("http://127.0.0.1:{port}/hook/SECRETTOKEN");
        let e = send_webhook(&url, "error", "t", "m").unwrap_err();
        assert!(!e.contains("SECRETTOKEN"), "{e}");
    }

    #[test]
    fn posts_json_payload_to_mock_http_server() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut data = Vec::new();
            let mut buf = [0u8; 4096];
            loop {
                let n = sock.read(&mut buf).unwrap();
                if n == 0 {
                    break;
                }
                data.extend_from_slice(&buf[..n]);
                if let Some(pos) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                    let hdr = String::from_utf8_lossy(&data[..pos]);
                    let cl: usize = hdr
                        .lines()
                        .find_map(|l| {
                            let (k, v) = l.split_once(':')?;
                            if k.eq_ignore_ascii_case("content-length") {
                                v.trim().parse().ok()
                            } else {
                                None
                            }
                        })
                        .unwrap_or(0);
                    if data.len() >= pos + 4 + cl {
                        break;
                    }
                }
            }
            let _ = sock
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
            let _ = sock.flush();
            String::from_utf8_lossy(&data).to_string()
        });
        let url = format!("http://{addr}/hook");
        send_webhook(&url, "error", "백업 실패", "디스크 공간 부족").unwrap();
        let raw_req = server.join().unwrap();
        assert!(raw_req.starts_with("POST /hook "));
        assert!(raw_req.contains("백업 실패"));
        assert!(raw_req.contains("디스크 공간 부족"));
    }
}
