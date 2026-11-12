//! 서비스(KovaSvc)와 화면(kova-app)이 주고받는 메시지와 named pipe 전송.
//! 한 줄에 JSON 하나(줄 단위 프레이밍). 서비스는 진행 이벤트를 푸시한다.
//! 서버는 `Handler` 를 받아 동작하므로 이 크레이트는 서비스 로직을 모른다.
use serde::{Deserialize, Serialize};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::windows::named_pipe::{ClientOptions, ServerOptions};

pub const PIPE_NAME: &str = r"\\.\pipe\kova";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "cmd")]
pub enum Request {
    Ping,
    Status,
    /// kind: "backup" | "verify"
    RunNow {
        profile_id: String,
        kind: String,
    },
    Cancel {
        job_id: u64,
    },
    /// 연결을 유지한 채 이후 모든 작업 이벤트를 받는다.
    Subscribe,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "event")]
pub enum Event {
    Pong {
        pid: u32,
    },
    Status {
        waiting: usize,
        running: usize,
    },
    Queued {
        job_id: u64,
        merged: bool,
    },
    Cancelled {
        job_id: u64,
        found: bool,
    },
    JobStarted {
        job_id: u64,
        profile_id: String,
        kind: String,
    },
    Progress {
        job_id: u64,
        percent: u64,
        current: String,
    },
    JobFinished {
        job_id: u64,
        profile_id: String,
        ok: bool,
        message: String,
        snapshot_id: Option<String>,
    },
    Error {
        message: String,
    },
}

pub enum Reply {
    /// 이벤트 몇 개를 보내고 연결을 닫는다.
    Once(Vec<Event>),
    /// 클라이언트가 끊을 때까지 이벤트를 흘려보낸다.
    Stream(Receiver<Event>),
}

pub trait Handler: Send + Sync + 'static {
    fn handle(&self, req: Request) -> Reply;
}

/// 파이프 접근 권한(SDDL): SYSTEM·관리자는 전체, 로그인한 일반 사용자는 읽기/쓰기(화면이 요청을 보내야 하므로).
/// 원격 접속은 tokio 기본값(reject_remote_clients)으로 막혀 있다. 이 IPC 에는 파괴적 요청(복원·삭제)이 없고,
/// 생기면 요청 처리 쪽에서 호출자 관리자 여부를 따로 확인해야 한다.
const PIPE_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)";

#[repr(C)]
struct SecurityAttributes {
    n_length: u32,
    descriptor: *mut core::ffi::c_void,
    inherit: i32,
}
#[link(name = "advapi32")]
extern "system" {
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        s: *const u16,
        rev: u32,
        out: *mut *mut core::ffi::c_void,
        size: *mut u32,
    ) -> i32;
    fn LocalFree(p: *mut core::ffi::c_void) -> *mut core::ffi::c_void;
}

/// 위 권한을 적용한 파이프 인스턴스를 만든다.
fn create_pipe(
    name: &str,
    first: bool,
) -> std::io::Result<tokio::net::windows::named_pipe::NamedPipeServer> {
    let wide: Vec<u16> = PIPE_SDDL.encode_utf16().chain(std::iter::once(0)).collect();
    let mut sd: *mut core::ffi::c_void = std::ptr::null_mut();
    // SAFETY: 유효한 널 종료 UTF-16 와 출력 포인터를 넘긴다. sd 는 아래에서 LocalFree 한다.
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide.as_ptr(),
            1,
            &mut sd,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    let mut sa = SecurityAttributes {
        n_length: std::mem::size_of::<SecurityAttributes>() as u32,
        descriptor: sd,
        inherit: 0,
    };
    let mut opts = ServerOptions::new();
    opts.first_pipe_instance(first);
    // SAFETY: sa 는 이 호출 동안 유효하다(커널이 값을 복사해 가진다).
    let res = unsafe {
        opts.create_with_security_attributes_raw(name, &mut sa as *mut _ as *mut core::ffi::c_void)
    };
    unsafe { LocalFree(sd) };
    res
}

/// 서버: 항상 대기 중인 파이프 인스턴스를 하나 유지하고, 접속마다 별도 작업으로 처리한다.
pub async fn serve(name: &str, handler: Arc<dyn Handler>) -> std::io::Result<()> {
    let mut server = create_pipe(name, true)?;
    loop {
        server.connect().await?;
        let connected = server;
        server = create_pipe(name, false)?;
        let h = handler.clone();
        tokio::spawn(async move {
            let _ = handle(connected, h).await;
        });
    }
}

async fn send<W: tokio::io::AsyncWrite + Unpin>(w: &mut W, e: &Event) -> std::io::Result<()> {
    let mut line = serde_json::to_string(e).unwrap();
    line.push('\n');
    w.write_all(line.as_bytes()).await?;
    w.flush().await
}

async fn handle(
    pipe: tokio::net::windows::named_pipe::NamedPipeServer,
    h: Arc<dyn Handler>,
) -> std::io::Result<()> {
    let (r, mut w) = tokio::io::split(pipe);
    let mut lines = BufReader::new(r).lines();
    let Some(line) = lines.next_line().await? else {
        return Ok(());
    };
    let reply = match serde_json::from_str::<Request>(&line) {
        Ok(req) => h.handle(req),
        Err(e) => Reply::Once(vec![Event::Error {
            message: format!("잘못된 요청: {e}"),
        }]),
    };
    match reply {
        Reply::Once(evs) => {
            for e in evs {
                send(&mut w, &e).await?;
            }
        }
        Reply::Stream(rx) => {
            let rx = Arc::new(Mutex::new(rx));
            loop {
                let rx2 = rx.clone();
                let got = tokio::task::spawn_blocking(move || {
                    rx2.lock().unwrap().recv_timeout(Duration::from_millis(500))
                })
                .await
                .map_err(std::io::Error::other)?;
                match got {
                    Ok(e) => send(&mut w, &e).await?, // 클라이언트가 끊으면 여기서 오류로 끝난다
                    Err(RecvTimeoutError::Timeout) => {
                        // 끊긴 연결을 빨리 알아채려고 빈 줄을 보낸다(클라이언트는 무시)
                        w.write_all(b"\n").await?;
                        w.flush().await?;
                    }
                    Err(RecvTimeoutError::Disconnected) => break,
                }
            }
        }
    }
    Ok(())
}

fn parse(line: &str) -> std::io::Result<Option<Event>> {
    if line.trim().is_empty() {
        return Ok(None);
    }
    serde_json::from_str(line)
        .map(Some)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

/// 클라이언트: 요청 한 줄을 보내고 연결이 닫힐 때까지 이벤트를 모은다.
pub async fn request(name: &str, req: &Request) -> std::io::Result<Vec<Event>> {
    request_until(name, req, |_| false).await
}

/// 이벤트를 받다가 `stop` 이 true 를 돌려주는 이벤트(포함)에서 끝낸다. Subscribe 용.
pub async fn request_until(
    name: &str,
    req: &Request,
    stop: impl Fn(&Event) -> bool,
) -> std::io::Result<Vec<Event>> {
    let client = ClientOptions::new().open(name)?;
    let (r, mut w) = tokio::io::split(client);
    let mut line = serde_json::to_string(req).unwrap();
    line.push('\n');
    w.write_all(line.as_bytes()).await?;
    w.flush().await?;
    let mut out = Vec::new();
    let mut lines = BufReader::new(r).lines();
    while let Some(l) = lines.next_line().await? {
        if let Some(e) = parse(&l)? {
            let done = stop(&e);
            out.push(e);
            if done {
                break;
            }
        }
    }
    Ok(out)
}

/// 스트림 구독: 이벤트가 올 때마다 `on_event` 를 부르고, false 를 돌려주거나 연결이 끊기면 끝난다.
pub async fn stream(
    name: &str,
    req: &Request,
    mut on_event: impl FnMut(Event) -> bool,
) -> std::io::Result<()> {
    let client = ClientOptions::new().open(name)?;
    let (r, mut w) = tokio::io::split(client);
    let mut line = serde_json::to_string(req).unwrap();
    line.push('\n');
    w.write_all(line.as_bytes()).await?;
    w.flush().await?;
    let mut lines = BufReader::new(r).lines();
    while let Some(l) = lines.next_line().await? {
        if let Some(e) = parse(&l)? {
            if !on_event(e) {
                break;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::channel;

    struct Echo(Mutex<Option<std::sync::mpsc::Sender<Event>>>);
    impl Handler for Echo {
        fn handle(&self, req: Request) -> Reply {
            match req {
                Request::Ping => Reply::Once(vec![Event::Pong {
                    pid: std::process::id(),
                }]),
                Request::Status => Reply::Once(vec![Event::Status {
                    waiting: 1,
                    running: 2,
                }]),
                Request::Subscribe => {
                    let (tx, rx) = channel();
                    for i in 1..=3 {
                        tx.send(Event::Progress {
                            job_id: 1,
                            percent: i * 30,
                            current: "파일.txt".into(),
                        })
                        .unwrap();
                    }
                    *self.0.lock().unwrap() = Some(tx); // 연결을 열어 둔 채 유지
                    Reply::Stream(rx)
                }
                _ => Reply::Once(vec![Event::Error {
                    message: "미지원".into(),
                }]),
            }
        }
    }

    #[tokio::test]
    async fn once_stream_and_bad_request() {
        let name = format!(r"\\.\pipe\kova-test-{}", std::process::id());
        let h: Arc<dyn Handler> = Arc::new(Echo(Mutex::new(None)));
        let n2 = name.clone();
        tokio::spawn(async move {
            let _ = serve(&n2, h).await;
        });
        tokio::time::sleep(Duration::from_millis(100)).await;

        let pong = request(&name, &Request::Ping).await.unwrap();
        assert!(matches!(pong[0], Event::Pong { pid } if pid == std::process::id()));
        assert_eq!(
            request(&name, &Request::Status).await.unwrap(),
            vec![Event::Status {
                waiting: 1,
                running: 2
            }]
        );

        // 구독: 이벤트 3개를 받고 클라이언트가 먼저 끊는다
        let got = request_until(&name, &Request::Subscribe, |e| {
            matches!(e, Event::Progress { percent: 90, .. })
        })
        .await
        .unwrap();
        assert_eq!(got.len(), 3);

        // 잘못된 JSON 은 Error 이벤트로 돌려준다
        let client = ClientOptions::new().open(&name).unwrap();
        let (r, mut w) = tokio::io::split(client);
        w.write_all(b"{not json}\n").await.unwrap();
        let line = BufReader::new(r)
            .lines()
            .next_line()
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(
            serde_json::from_str::<Event>(&line).unwrap(),
            Event::Error { .. }
        ));
    }
}
