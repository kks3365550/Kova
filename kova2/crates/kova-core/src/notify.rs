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
        .map_err(|e| format!("웹훅 전송 실패: {e}"))?;
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
