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

/// 토큰·복구 코드 같은 비밀 문자열. 전송(JSON)은 그대로 하고 Debug 출력은 가린다(로그에 남지 않게).
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct Secret(pub String);

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secret(***)")
    }
}

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
    /// 구글 드라이브 연결 여부(토큰 파일은 SYSTEM·관리자만 열 수 있어 서비스가 확인한다). 답: `GdriveStatus`.
    GdriveStatus,
    /// 앱이 구글 로그인으로 받은 토큰(JSON)을 서비스가 보호해 저장한다. 답: `Done` / `Error`.
    /// 토큰 내용을 돌려주는 요청은 만들지 않는다.
    GdriveStoreToken {
        token: Secret,
    },
    /// 구글에 토큰 폐기를 요청하고 저장된 토큰을 지운다. 답: `Done`.
    GdriveDisconnect,
    /// 프로파일 저장소마다 암호화를 켠다(키는 서비스가 만들고 보관). 답: `Encrypted`(새로 켠 곳의 복구 코드, 이때 한 번만).
    EnableEncryption,
    /// 복구 코드로 이 PC의 저장소 키를 다시 만든다. 답: `Done` / `Error`.
    RestoreKey {
        code: Secret,
    },
    /// 복구 코드 내보내기(관리자/SYSTEM 호출자만). 답: `RecoveryCodes`.
    ExportRecoveryCodes,
    /// 암호화 저장소 복원(키가 필요해 서비스가 한다). 파일 쓰기는 요청한 사용자 권한(가장)으로 하므로
    /// `stream_as_caller` 로 보내야 한다. 답: `RestoreProgress`… 뒤 `Done` / `Error`.
    Restore {
        repo: String,
        session_id: String,
        /// None 이면 원래 위치
        target: Option<String>,
        only: Option<Vec<String>>,
    },
    /// 프로파일의 최근 스냅샷을 서비스 임시 폴더에 복원해 보고 지운다(복구 훈련). 답: `Done` / `Error`.
    Drill {
        profile_id: String,
    },
}

/// 복구 키트 한 줄.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RecoveryEntry {
    pub name: String,
    pub repo: String,
    pub code: Secret,
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
    GdriveStatus {
        connected: bool,
    },
    /// 요청을 처리했다(사람이 읽을 결과 문구).
    Done {
        message: String,
    },
    /// 새로 암호화를 켠 저장소의 "이름 복구 코드: …" 줄(없으면 빈 목록)
    Encrypted {
        codes: Vec<Secret>,
    },
    RecoveryCodes {
        entries: Vec<RecoveryEntry>,
    },
    RestoreProgress {
        done: u64,
        total: u64,
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

/// 클라이언트가 가장(impersonation) 수준으로 접속했을 때(`stream_as_caller`) 잡아 둔 그 사용자의 토큰.
/// 서비스는 이것으로 작업 스레드를 그 사용자 권한으로 돌려, SYSTEM 권한으로 사용자 대신 파일을 쓰지 않는다.
#[derive(Debug)]
pub struct ClientToken(usize);

impl ClientToken {
    /// 지금 스레드를 이 사용자로 가장한다. 성공하면 true(스레드가 끝나거나 되돌릴 때까지 유지).
    pub fn impersonate_current_thread(&self) -> bool {
        // SAFETY: self.0 은 TOKEN_IMPERSONATE 권한으로 연 가장 토큰 핸들이다. null 스레드 = 현재 스레드.
        unsafe { SetThreadToken(std::ptr::null_mut(), self.0 as *mut _) != 0 }
    }
}

impl Drop for ClientToken {
    fn drop(&mut self) {
        // SAFETY: caller_of 가 연 핸들을 한 번만 닫는다.
        unsafe { CloseHandle(self.0 as *mut _) };
    }
}

pub trait Handler: Send + Sync + 'static {
    fn handle(&self, req: Request, caller: Caller) -> Reply;
    /// 서버가 실제로 부르는 입구. 클라이언트가 가장을 허용했으면 `token` 이 있다. 기본은 토큰을 버리고 `handle`.
    fn handle_with_token(&self, req: Request, caller: Caller, token: Option<ClientToken>) -> Reply {
        drop(token);
        self.handle(req, caller)
    }
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
    fn GetTokenInformation(
        token: *mut core::ffi::c_void,
        class: i32,
        info: *mut core::ffi::c_void,
        len: u32,
        ret: *mut u32,
    ) -> i32;
    fn SetThreadToken(thread: *mut *mut core::ffi::c_void, token: *mut core::ffi::c_void) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThread() -> *mut core::ffi::c_void;
    fn CloseHandle(h: *mut core::ffi::c_void) -> i32;
}
const TOKEN_QUERY: u32 = 0x0008;
const TOKEN_DUPLICATE: u32 = 0x0002;
const TOKEN_IMPERSONATE: u32 = 0x0004;
/// TOKEN_INFORMATION_CLASS::TokenImpersonationLevel
const TOKEN_IMPERSONATION_LEVEL_CLASS: i32 = 9;
/// SECURITY_IMPERSONATION_LEVEL::SecurityImpersonation
const SECURITY_IMPERSONATION_LEVEL: u32 = 2;
/// 클라이언트 쪽 QoS: 서버가 이 연결 동안 클라이언트로 가장할 수 있게 한다(SECURITY_IMPERSONATION)
const SQOS_IMPERSONATION: u32 = 0x0002_0000;
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
/// 클라이언트가 가장 수준(SECURITY_IMPERSONATION 이상)으로 접속했으면 그 토큰을 넘겨 준다(아니면 닫는다).
fn caller_of(pipe: *mut core::ffi::c_void) -> (Caller, Option<ClientToken>) {
    // SAFETY: pipe 는 살아 있는 서버 파이프 핸들이다. 가장 → 토큰 열기 → 되돌리기 사이에 await 가 없어
    // 같은 스레드에서 끝난다. 되돌리기에 실패하면 다른 작업이 클라이언트 권한으로 돌 수 있으므로 즉시 끝낸다.
    unsafe {
        if ImpersonateNamedPipeClient(pipe) == 0 {
            return (Caller::default(), None);
        }
        let mut tok: *mut core::ffi::c_void = std::ptr::null_mut();
        let full = OpenThreadToken(
            GetCurrentThread(),
            TOKEN_QUERY | TOKEN_IMPERSONATE | TOKEN_DUPLICATE,
            1,
            &mut tok,
        ) != 0;
        let opened = full || OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut tok) != 0;
        if RevertToSelf() == 0 {
            std::process::abort();
        }
        if !opened {
            return (Caller::default(), None);
        }
        let privileged =
            token_has(tok, WIN_LOCAL_SYSTEM_SID) || token_has(tok, WIN_BUILTIN_ADMINISTRATORS_SID);
        let mut level = 0u32;
        let mut ret = 0u32;
        let can_impersonate = full
            && GetTokenInformation(
                tok,
                TOKEN_IMPERSONATION_LEVEL_CLASS,
                &mut level as *mut u32 as *mut _,
                4,
                &mut ret,
            ) != 0
            && level >= SECURITY_IMPERSONATION_LEVEL;
        let token = if can_impersonate {
            Some(ClientToken(tok as usize))
        } else {
            CloseHandle(tok);
            None
        };
        (Caller { privileged }, token)
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
    let (caller, token) = caller_of(raw as *mut core::ffi::c_void);
    let reply = match serde_json::from_str::<Request>(&line) {
        Ok(req) => h.handle_with_token(req, caller, token),
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
    on_event: impl FnMut(Event) -> bool,
) -> std::io::Result<()> {
    stream_with(ClientOptions::new().open(name)?, req, on_event).await
}

/// `stream` 과 같되, 서비스가 이 연결 동안 호출한 사용자로 가장할 수 있게 접속한다(`Request::Restore` 용:
/// 서비스가 SYSTEM 이 아니라 이 사용자 권한으로 파일을 쓴다). 서비스 파이프에만 쓴다.
pub async fn stream_as_caller(
    name: &str,
    req: &Request,
    on_event: impl FnMut(Event) -> bool,
) -> std::io::Result<()> {
    let client = ClientOptions::new()
        .security_qos_flags(SQOS_IMPERSONATION)
        .open(name)?;
    stream_with(client, req, on_event).await
}

async fn stream_with(
    client: tokio::net::windows::named_pipe::NamedPipeClient,
    req: &Request,
    mut on_event: impl FnMut(Event) -> bool,
) -> std::io::Result<()> {
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

    /// 가장을 허용한 접속(stream_as_caller)에서만 서버가 클라이언트 토큰을 받고, 그 토큰으로 스레드를 가장할 수 있다.
    struct TokenProbe;
    impl Handler for TokenProbe {
        fn handle(&self, _: Request, _: Caller) -> Reply {
            Reply::Once(vec![Event::Done {
                message: "no-token-path".into(),
            }])
        }
        fn handle_with_token(&self, _: Request, _: Caller, token: Option<ClientToken>) -> Reply {
            let msg = match token {
                None => "none".to_string(),
                Some(t) => std::thread::spawn(move || {
                    if !t.impersonate_current_thread() {
                        return "impersonate-failed".to_string();
                    }
                    // 가장한 스레드에서도 이 사용자(같은 계정)로 파일을 쓸 수 있다
                    let d = std::env::temp_dir().join(format!("kova_imp_{}", std::process::id()));
                    let ok = std::fs::write(&d, b"x").is_ok();
                    let _ = std::fs::remove_file(&d);
                    format!("impersonated:{ok}")
                })
                .join()
                .unwrap(),
            };
            Reply::Once(vec![Event::Done { message: msg }])
        }
    }

    #[tokio::test]
    async fn client_token_is_given_only_when_caller_allows_impersonation() {
        let name = format!(r"\\.\pipe\kova-test-imp-{}", std::process::id());
        let h: Arc<dyn Handler> = Arc::new(TokenProbe);
        let n2 = name.clone();
        tokio::spawn(async move {
            let _ = serve(&n2, h).await;
        });
        tokio::time::sleep(Duration::from_millis(100)).await;
        let got = |v: Vec<Event>| match v.first() {
            Some(Event::Done { message }) => message.clone(),
            other => panic!("{other:?}"),
        };
        assert_eq!(got(request(&name, &Request::Ping).await.unwrap()), "none");
        let mut seen = Vec::new();
        stream_as_caller(&name, &Request::Ping, |e| {
            seen.push(e);
            true
        })
        .await
        .unwrap();
        assert_eq!(got(seen), "impersonated:true");
    }

    #[test]
    fn secret_is_sent_as_plain_string_but_hidden_in_debug() {
        let r = Request::GdriveStoreToken {
            token: Secret("tok-123".into()),
        };
        let line = serde_json::to_string(&r).unwrap();
        assert_eq!(line, r#"{"cmd":"GdriveStoreToken","token":"tok-123"}"#);
        assert_eq!(serde_json::from_str::<Request>(&line).unwrap(), r);
        assert!(!format!("{r:?}").contains("tok-123"));
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
