//! 서비스(KovaSvc)와 화면(kova-app)이 주고받는 메시지와 named pipe 전송.
//! 한 줄에 JSON 하나(줄 단위 프레이밍). 서비스는 진행 이벤트를 푸시한다.
//! 서버는 `Handler` 를 받아 동작하므로 이 크레이트는 서비스 로직을 모른다.
use serde::{Deserialize, Serialize};
use std::os::windows::io::AsRawHandle;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::windows::named_pipe::{ClientOptions, ServerOptions};

pub const PIPE_NAME: &str = r"\\.\pipe\kova";

/// 클라이언트가 읽지 않아 쓰기가 막히면 이 시간 뒤 연결을 끊는다(구독이 서비스 쪽에 쌓이지 않게).
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

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

/// 요청을 보낸 쪽(파이프 클라이언트). 서버가 클라이언트를 잠깐 가장(impersonate)해 토큰을 확인한다.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Caller {
    /// SYSTEM 이거나 Administrators 그룹이 켜진(권한 상승된) 토큰이면 true. 확인 실패는 false(거부 쪽).
    pub privileged: bool,
}

pub trait Handler: Send + Sync + 'static {
    fn handle(&self, req: Request, caller: Caller) -> Reply;
    /// 서버 자체 오류(연결 수락·파이프 생성 실패 등). 서버는 이 오류로 멈추지 않고 계속 돈다.
    fn on_error(&self, _message: &str) {}
}

/// 파이프 접근 권한(SDDL): SYSTEM·관리자는 전체, 로그인한 일반 사용자는 읽기/쓰기(화면이 요청을 보내야 하므로).
/// 원격 접속은 tokio 기본값(reject_remote_clients)으로 막혀 있다. 일반 사용자도 접속할 수 있으므로
/// 파괴적 요청(스냅샷 삭제·정리·이미지·취소)은 처리하는 쪽이 `Caller::privileged` 를 확인해 거부해야 한다.
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
    fn ImpersonateNamedPipeClient(pipe: *mut core::ffi::c_void) -> i32;
    fn RevertToSelf() -> i32;
    fn OpenThreadToken(
        thread: *mut core::ffi::c_void,
        access: u32,
        open_as_self: i32,
        token: *mut *mut core::ffi::c_void,
    ) -> i32;
    fn CreateWellKnownSid(
        kind: i32,
        domain: *mut core::ffi::c_void,
        sid: *mut core::ffi::c_void,
        size: *mut u32,
    ) -> i32;
    fn CheckTokenMembership(
        token: *mut core::ffi::c_void,
        sid: *mut core::ffi::c_void,
        is_member: *mut i32,
    ) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThread() -> *mut core::ffi::c_void;
    fn CloseHandle(h: *mut core::ffi::c_void) -> i32;
}
const TOKEN_QUERY: u32 = 0x0008;
const WIN_LOCAL_SYSTEM_SID: i32 = 22;
const WIN_BUILTIN_ADMINISTRATORS_SID: i32 = 26;

/// `token` 이 켜진 그룹으로 well-known SID 를 갖는가. token 이 null 이면 호출 스레드/프로세스 토큰을 본다.
unsafe fn token_has(token: *mut core::ffi::c_void, kind: i32) -> bool {
    let mut sid = [0u32; 17]; // SECURITY_MAX_SID_SIZE(68바이트), 4바이트 정렬
    let mut size = std::mem::size_of_val(&sid) as u32;
    if CreateWellKnownSid(
        kind,
        std::ptr::null_mut(),
        sid.as_mut_ptr() as *mut _,
        &mut size,
    ) == 0
    {
        return false;
    }
    let mut member = 0i32;
    CheckTokenMembership(token, sid.as_mut_ptr() as *mut _, &mut member) != 0 && member != 0
}

/// 첫 요청 줄을 읽은 뒤(가장은 데이터를 받은 뒤에만 된다) 클라이언트 토큰을 확인한다.
fn caller_of(pipe: *mut core::ffi::c_void) -> Caller {
    // SAFETY: pipe 는 살아 있는 서버 파이프 핸들이다. 가장 → 토큰 열기 → 되돌리기 사이에 await 가 없어
    // 같은 스레드에서 끝난다. 되돌리기에 실패하면 다른 작업이 클라이언트 권한으로 돌 수 있으므로 즉시 끝낸다.
    unsafe {
        if ImpersonateNamedPipeClient(pipe) == 0 {
            return Caller::default();
        }
        let mut tok: *mut core::ffi::c_void = std::ptr::null_mut();
        let opened = OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut tok) != 0;
        if RevertToSelf() == 0 {
            std::process::abort();
        }
        if !opened {
            return Caller::default();
        }
        let privileged =
            token_has(tok, WIN_LOCAL_SYSTEM_SID) || token_has(tok, WIN_BUILTIN_ADMINISTRATORS_SID);
        CloseHandle(tok);
        Caller { privileged }
    }
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

/// 다음 대기 인스턴스를 만들 때까지 재시도한다(실패는 기록하고 1초 뒤 다시).
async fn create_next(
    name: &str,
    h: &Arc<dyn Handler>,
) -> tokio::net::windows::named_pipe::NamedPipeServer {
    loop {
        match create_pipe(name, false) {
            Ok(s) => return s,
            Err(e) => {
                h.on_error(&format!("파이프 인스턴스 생성 실패(재시도): {e}"));
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        }
    }
}

/// 서버: 항상 대기 중인 파이프 인스턴스를 하나 유지하고, 접속마다 별도 작업으로 처리한다.
/// 처음 파이프를 못 만들 때만 오류로 끝난다. 이후 연결·생성 오류는 기록하고 계속 돈다.
pub async fn serve(name: &str, handler: Arc<dyn Handler>) -> std::io::Result<()> {
    let mut server = create_pipe(name, true)?;
    loop {
        if let Err(e) = server.connect().await {
            // 클라이언트가 붙자마자 끊는 등. 새 인스턴스를 먼저 만든 뒤 깨진 것을 버린다(이름을 비워 두지 않음).
            handler.on_error(&format!("파이프 연결 수락 실패(계속): {e}"));
            server = create_next(name, &handler).await;
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        }
        let next = create_next(name, &handler).await;
        let connected = std::mem::replace(&mut server, next);
        let h = handler.clone();
        tokio::spawn(async move {
            let _ = handle(connected, h).await;
        });
    }
}

async fn write_within<W: tokio::io::AsyncWrite + Unpin>(
    w: &mut W,
    bytes: &[u8],
    limit: Duration,
) -> std::io::Result<()> {
    match tokio::time::timeout(limit, async {
        w.write_all(bytes).await?;
        w.flush().await
    })
    .await
    {
        Ok(r) => r,
        Err(_) => Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "클라이언트가 읽지 않아 쓰기 시간 초과",
        )),
    }
}

async fn send<W: tokio::io::AsyncWrite + Unpin>(w: &mut W, e: &Event) -> std::io::Result<()> {
    let mut line = serde_json::to_string(e).unwrap();
    line.push('\n');
    write_within(w, line.as_bytes(), WRITE_TIMEOUT).await
}

async fn handle(
    pipe: tokio::net::windows::named_pipe::NamedPipeServer,
    h: Arc<dyn Handler>,
) -> std::io::Result<()> {
    // 원시 포인터는 Send 가 아니라 정수로 들고 있다(핸들은 pipe 의 두 반쪽이 살아 있는 동안 유효).
    let raw = pipe.as_raw_handle() as usize;
    let (r, mut w) = tokio::io::split(pipe);
    let mut lines = BufReader::new(r).lines();
    let Some(line) = lines.next_line().await? else {
        return Ok(());
    };
    let caller = caller_of(raw as *mut core::ffi::c_void);
    let reply = match serde_json::from_str::<Request>(&line) {
        Ok(req) => h.handle(req, caller),
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
                    Ok(e) => send(&mut w, &e).await?, // 클라이언트가 끊거나 읽지 않으면 여기서 오류로 끝난다
                    Err(RecvTimeoutError::Timeout) => {
                        // 끊긴 연결을 빨리 알아채려고 빈 줄을 보낸다(클라이언트는 무시)
                        write_within(&mut w, b"\n", WRITE_TIMEOUT).await?;
                    }
                    Err(RecvTimeoutError::Disconnected) => break,
                }
            }
        }
    }
    Ok(())
}

/// 빈 줄(살아 있음 확인)과 이 버전이 모르는 이벤트 줄은 건너뛴다(새 서비스가 이벤트를 추가해도 화면이 끊기지 않게).
fn parse(line: &str) -> Option<Event> {
    if line.trim().is_empty() {
        return None;
    }
    serde_json::from_str(line).ok()
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
        if let Some(e) = parse(&l) {
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
        if let Some(e) = parse(&l) {
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
        fn handle(&self, req: Request, caller: Caller) -> Reply {
            match req {
                Request::Ping => Reply::Once(vec![Event::Pong {
                    pid: std::process::id(),
                }]),
                Request::Status => Reply::Once(vec![Event::Status {
                    waiting: 1,
                    running: 2,
                }]),
                // 서버가 본 호출자 권한을 돌려준다(시험용)
                Request::Cancel { .. } => Reply::Once(vec![Event::Cancelled {
                    job_id: 0,
                    found: caller.privileged,
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

    /// 같은 프로세스가 클라이언트이므로, 서버가 가장해서 본 권한은 이 프로세스 토큰의 권한과 같아야 한다.
    #[tokio::test]
    async fn caller_privilege_matches_this_process_token() {
        let name = format!(r"\\.\pipe\kova-test-caller-{}", std::process::id());
        let h: Arc<dyn Handler> = Arc::new(Echo(Mutex::new(None)));
        let n2 = name.clone();
        tokio::spawn(async move {
            let _ = serve(&n2, h).await;
        });
        tokio::time::sleep(Duration::from_millis(100)).await;
        let me = unsafe {
            token_has(std::ptr::null_mut(), WIN_LOCAL_SYSTEM_SID)
                || token_has(std::ptr::null_mut(), WIN_BUILTIN_ADMINISTRATORS_SID)
        };
        let got = request(&name, &Request::Cancel { job_id: 1 })
            .await
            .unwrap();
        assert_eq!(
            got,
            vec![Event::Cancelled {
                job_id: 0,
                found: me
            }]
        );
    }

    /// 접속 직후 끊는 클라이언트가 많아도(연결 수락 오류) 서버는 멈추지 않는다.
    #[tokio::test]
    async fn server_survives_clients_that_disconnect_immediately() {
        let name = format!(r"\\.\pipe\kova-test-churn-{}", std::process::id());
        let h: Arc<dyn Handler> = Arc::new(Echo(Mutex::new(None)));
        let n2 = name.clone();
        tokio::spawn(async move {
            let _ = serve(&n2, h).await;
        });
        tokio::time::sleep(Duration::from_millis(100)).await;
        for _ in 0..30 {
            if let Ok(c) = ClientOptions::new().open(&name) {
                drop(c);
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let mut ok = false;
        for _ in 0..50 {
            if let Ok(v) = request(&name, &Request::Ping).await {
                ok = matches!(v.first(), Some(Event::Pong { .. }));
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(ok, "끊김이 반복된 뒤에도 Ping 에 응답해야 함");
    }

    #[test]
    fn unknown_or_blank_event_lines_are_skipped() {
        assert_eq!(parse(""), None);
        assert_eq!(parse("   "), None);
        assert_eq!(parse(r#"{"event":"NewKindFromNewerService","x":1}"#), None);
        assert_eq!(parse("not json"), None);
        assert_eq!(
            parse(r#"{"event":"Pong","pid":7}"#),
            Some(Event::Pong { pid: 7 })
        );
    }

    #[tokio::test]
    async fn write_gives_up_when_peer_does_not_read() {
        let (mut a, _b) = tokio::io::duplex(16); // 상대가 읽지 않는 작은 버퍼
        let big = vec![b'x'; 1024];
        let r = write_within(&mut a, &big, Duration::from_millis(100)).await;
        assert_eq!(r.unwrap_err().kind(), std::io::ErrorKind::TimedOut);
    }
}
