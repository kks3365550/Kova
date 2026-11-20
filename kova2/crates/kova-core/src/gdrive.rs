//! Google 드라이브 오프사이트 백엔드("구글로 로그인" 방식).
//! - 로그인: 루프백(127.0.0.1) + PKCE. 범위는 `drive.file` 하나뿐(Kova2가 만든 파일만 접근).
//! - 토큰: DPAPI(머신 범위)로 보호해 설정 폴더에 저장 → 서비스(SYSTEM)가 사용자 로그인 없이 쓴다.
//! - 업로드: 이어올리기(resumable) 세션, 폴더별 목록 캐시로 이름·크기가 같으면 건너뛴다.
#![allow(clippy::result_large_err)] // ureq::Error 가 커서 클로저 반환형에서 경고
use crate::repocrypto::dpapi;
use kova_engine::storage::SnapshotManifest;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const SCOPE: &str = "https://www.googleapis.com/auth/drive.file";
const FOLDER_MIME: &str = "application/vnd.google-apps.folder";
const CHUNK: usize = 8 * 1024 * 1024;
const TOKEN_FILE: &str = "gdrive_token.bin";
const CLIENT_FILE: &str = "gdrive_client.json";

#[derive(Debug, Clone)]
pub struct Endpoints {
    pub auth: String,
    pub token: String,
    pub revoke: String,
    pub api: String,
    pub upload: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Endpoints {
            auth: "https://accounts.google.com/o/oauth2/v2/auth".into(),
            token: "https://oauth2.googleapis.com/token".into(),
            revoke: "https://oauth2.googleapis.com/revoke".into(),
            api: "https://www.googleapis.com/drive/v3".into(),
            upload: "https://www.googleapis.com/upload/drive/v3".into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClientCreds {
    pub client_id: String,
    pub client_secret: String,
}

/// 클라이언트 ID/비밀: 빌드 시 환경변수(KOVA_GDRIVE_CLIENT_ID/SECRET) 우선, 없으면 설정 폴더의 gdrive_client.json.
/// (데스크톱 앱의 클라이언트 비밀은 비밀이 아니지만, 공개 저장소에는 커밋하지 않는다.)
pub fn load_client(dir: &Path) -> Result<ClientCreds, String> {
    if let (Some(id), Some(sec)) = (
        option_env!("KOVA_GDRIVE_CLIENT_ID"),
        option_env!("KOVA_GDRIVE_CLIENT_SECRET"),
    ) {
        return Ok(ClientCreds {
            client_id: id.into(),
            client_secret: sec.into(),
        });
    }
    let s = fs::read_to_string(dir.join(CLIENT_FILE))
        .map_err(|_| "이 빌드에는 구글 연결 정보가 들어 있지 않습니다".to_string())?;
    serde_json::from_str(&s).map_err(|e| format!("{CLIENT_FILE} 형식 오류: {e}"))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Token {
    refresh: String,
    access: String,
    /// 유닉스 초
    expires_at: i64,
}

fn now_secs() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn save_token(dir: &Path, t: &Token) -> Result<(), String> {
    let enc = dpapi::protect(&serde_json::to_vec(t).map_err(|e| e.to_string())?)?;
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    fs::write(dir.join(TOKEN_FILE), enc).map_err(|e| format!("토큰 저장 실패: {e}"))
}

fn load_token(dir: &Path) -> Result<Token, String> {
    let enc = fs::read(dir.join(TOKEN_FILE))
        .map_err(|_| "구글 드라이브가 연결돼 있지 않습니다".to_string())?;
    let raw = dpapi::unprotect(&enc)?;
    serde_json::from_slice(&raw).map_err(|e| format!("토큰 형식 오류: {e}"))
}

pub fn is_connected(dir: &Path) -> bool {
    dir.join(TOKEN_FILE).is_file()
}

/// 연결 해제: 구글에 토큰 폐기를 요청(실패해도 무시)하고 로컬 토큰을 지운다.
pub fn disconnect(dir: &Path, ep: &Endpoints) {
    if let Ok(t) = load_token(dir) {
        let _ = agent()
            .post(&ep.revoke)
            .send_form(&[("token", t.refresh.as_str())]);
    }
    let _ = fs::remove_file(dir.join(TOKEN_FILE));
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(30))
        .timeout_read(Duration::from_secs(120))
        .timeout_write(Duration::from_secs(120))
        .redirects(0)
        .build()
}

// ---------- 문자열 도우미 ----------

fn b64url(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(data.len() * 4 / 3 + 3);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        if c.len() > 1 {
            out.push(T[(n >> 6) as usize & 63] as char);
        }
        if c.len() > 2 {
            out.push(T[n as usize & 63] as char);
        }
    }
    out
}

fn pct_encode(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            o.push(b as char);
        } else {
            o.push_str(&format!("%{b:02X}"));
        }
    }
    o
}

pub(crate) fn pct_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut o = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => o.push(b' '),
            b'%' if i + 2 < b.len() => {
                if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                    o.push(v);
                    i += 2;
                } else {
                    o.push(b'%');
                }
            }
            c => o.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&o).into_owned()
}

fn query_map(q: &str) -> HashMap<String, String> {
    q.split('&')
        .filter_map(|kv| kv.split_once('='))
        .map(|(k, v)| (pct_decode(k), pct_decode(v)))
        .collect()
}

fn random_b64(n: usize) -> Result<String, String> {
    let mut buf = vec![0u8; n];
    getrandom::getrandom(&mut buf).map_err(|e| e.to_string())?;
    Ok(b64url(&buf))
}

fn q_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('\'', "\\'")
}

// ---------- 로그인(루프백 + PKCE) ----------

fn token_response(dir: &Path, body: &str, old_refresh: Option<&str>) -> Result<Token, String> {
    let v: serde_json::Value =
        serde_json::from_str(body).map_err(|e| format!("토큰 응답 오류: {e}"))?;
    if let Some(sc) = v.get("scope").and_then(|x| x.as_str()) {
        if !sc.contains("drive.file") {
            return Err("드라이브 접근 권한이 허용되지 않았습니다".into());
        }
    }
    let access = v["access_token"]
        .as_str()
        .ok_or("토큰 응답에 access_token 이 없습니다")?;
    let refresh = v["refresh_token"]
        .as_str()
        .or(old_refresh)
        .ok_or("토큰 응답에 refresh_token 이 없습니다")?;
    let t = Token {
        refresh: refresh.into(),
        access: access.into(),
        expires_at: now_secs() + v["expires_in"].as_i64().unwrap_or(3600),
    };
    save_token(dir, &t)?;
    Ok(t)
}

fn page(msg: &str) -> String {
    let body = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Kova2</title></head><body style=\"font-family:sans-serif;text-align:center;margin-top:80px\"><h2>{msg}</h2></body></html>"
    );
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

/// 브라우저 로그인으로 연결한다. `open` 에 로그인 주소가 전달되며(브라우저 열기), 사용자가 동의하면 토큰을 저장한다.
pub fn login(
    ep: &Endpoints,
    creds: &ClientCreds,
    dir: &Path,
    open: impl FnOnce(&str),
    cancel: &AtomicBool,
    timeout: Duration,
) -> Result<(), String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|e| format!("로그인 대기 포트 열기 실패: {e}"))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let redirect = format!("http://127.0.0.1:{port}/");
    let verifier = random_b64(32)?;
    let state = random_b64(16)?;
    let challenge = b64url(&Sha256::digest(verifier.as_bytes()));
    let url = format!(
        "{}?client_id={}&redirect_uri={}&response_type=code&scope={}&code_challenge={}&code_challenge_method=S256&state={}&access_type=offline&prompt=consent",
        ep.auth,
        pct_encode(&creds.client_id),
        pct_encode(&redirect),
        pct_encode(SCOPE),
        challenge,
        state
    );
    open(&url);

    let deadline = Instant::now() + timeout;
    let code = loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("취소됨".into());
        }
        if Instant::now() > deadline {
            return Err("로그인 시간이 초과되었습니다".into());
        }
        let mut stream = match listener.accept() {
            Ok((s, _)) => s,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(100));
                continue;
            }
            Err(e) => return Err(e.to_string()),
        };
        let _ = stream.set_nonblocking(false);
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let mut buf = Vec::new();
        let mut tmp = [0u8; 2048];
        while !buf.windows(4).any(|w| w == b"\r\n\r\n") && buf.len() < 16384 {
            match stream.read(&mut tmp) {
                Ok(0) | Err(_) => break,
                Ok(n) => buf.extend_from_slice(&tmp[..n]),
            }
        }
        let head = String::from_utf8_lossy(&buf).into_owned();
        let target = head.split_whitespace().nth(1).unwrap_or("");
        let query = target.split_once('?').map(|x| x.1).unwrap_or("");
        let q = query_map(query);
        if !q.contains_key("code") && !q.contains_key("error") {
            let _ = stream.write_all(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
            continue;
        }
        if q.get("state").map(|s| s.as_str()) != Some(state.as_str()) {
            let _ = stream.write_all(page("잘못된 요청입니다").as_bytes());
            continue;
        }
        if let Some(e) = q.get("error") {
            let _ = stream.write_all(page("연결이 취소되었습니다. 이 창을 닫으세요").as_bytes());
            return Err(format!("로그인이 거부되었습니다({e})"));
        }
        let _ = stream.write_all(page("Kova2 연결 완료 — 이 창을 닫으세요").as_bytes());
        break q["code"].clone();
    };

    let resp = agent()
        .post(&ep.token)
        .send_form(&[
            ("code", code.as_str()),
            ("client_id", creds.client_id.as_str()),
            ("client_secret", creds.client_secret.as_str()),
            ("code_verifier", verifier.as_str()),
            ("redirect_uri", redirect.as_str()),
            ("grant_type", "authorization_code"),
        ])
        .map_err(|e| format!("토큰 교환 실패: {e}"))?;
    let body = resp.into_string().map_err(|e| e.to_string())?;
    token_response(dir, &body, None).map(|_| ())
}

// ---------- 드라이브 클라이언트 ----------

#[derive(Debug, Clone)]
struct Entry {
    id: String,
    size: u64,
    folder: bool,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct DriveReplicaReport {
    pub snapshots_uploaded: usize,
    pub snapshots_skipped: usize,
    pub blobs_uploaded: usize,
    pub blobs_skipped: usize,
    pub bytes_uploaded: u64,
}

pub struct DriveClient {
    ep: Endpoints,
    creds: ClientCreds,
    dir: PathBuf,
    token: Token,
    agent: ureq::Agent,
    listings: HashMap<String, HashMap<String, Entry>>,
    folders: HashMap<String, String>,
    retry_base: Duration,
}

impl DriveClient {
    pub fn open(dir: &Path, ep: Endpoints, creds: ClientCreds) -> Result<Self, String> {
        Ok(DriveClient {
            ep,
            creds,
            dir: dir.to_path_buf(),
            token: load_token(dir)?,
            agent: agent(),
            listings: HashMap::new(),
            folders: HashMap::new(),
            retry_base: Duration::from_secs(2),
        })
    }

    fn refresh(&mut self) -> Result<(), String> {
        let r = self.agent.post(&self.ep.token).send_form(&[
            ("client_id", self.creds.client_id.as_str()),
            ("client_secret", self.creds.client_secret.as_str()),
            ("refresh_token", self.token.refresh.as_str()),
            ("grant_type", "refresh_token"),
        ]);
        match r {
            Ok(resp) => {
                let body = resp.into_string().map_err(|e| e.to_string())?;
                self.token = token_response(&self.dir, &body, Some(&self.token.refresh.clone()))?;
                Ok(())
            }
            Err(ureq::Error::Status(400 | 401, _)) => {
                Err("구글 연결이 만료되었거나 취소되었습니다. 앱에서 다시 연결해 주세요".into())
            }
            Err(e) => Err(format!("토큰 갱신 실패: {e}")),
        }
    }

    fn call(
        &mut self,
        mut f: impl FnMut(&ureq::Agent, &str) -> Result<ureq::Response, ureq::Error>,
    ) -> Result<ureq::Response, String> {
        let (mut tries, mut refreshed) = (0u32, false);
        loop {
            if now_secs() + 60 >= self.token.expires_at {
                self.refresh()?;
            }
            let tok = self.token.access.clone();
            match f(&self.agent, &tok) {
                Ok(r) => return Ok(r),
                Err(ureq::Error::Status(401, _)) if !refreshed => {
                    refreshed = true;
                    self.refresh()?;
                }
                Err(ureq::Error::Status(c, r)) if (c == 429 || c >= 500) && tries < 4 => {
                    let _ = r;
                    tries += 1;
                    std::thread::sleep(self.retry_base * (1 << (tries - 1)));
                }
                Err(ureq::Error::Transport(_)) if tries < 4 => {
                    tries += 1;
                    std::thread::sleep(self.retry_base * (1 << (tries - 1)));
                }
                Err(ureq::Error::Status(c, r)) => {
                    let b = r.into_string().unwrap_or_default();
                    return Err(format!(
                        "Drive HTTP {c}: {}",
                        b.chars().take(300).collect::<String>()
                    ));
                }
                Err(e) => return Err(format!("Drive 통신 실패: {e}")),
            }
        }
    }

    fn list(&mut self, parent: &str) -> Result<&mut HashMap<String, Entry>, String> {
        if !self.listings.contains_key(parent) {
            let mut map = HashMap::new();
            let mut page: Option<String> = None;
            loop {
                let url = format!("{}/files", self.ep.api);
                let q = format!("'{}' in parents and trashed = false", q_escape(parent));
                let pt = page.clone();
                let resp = self.call(|a, t| {
                    let mut r = a
                        .get(&url)
                        .set("Authorization", &format!("Bearer {t}"))
                        .query("q", &q)
                        .query("fields", "nextPageToken,files(id,name,size,mimeType)")
                        .query("pageSize", "1000");
                    if let Some(p) = &pt {
                        r = r.query("pageToken", p);
                    }
                    r.call()
                })?;
                let v: serde_json::Value =
                    serde_json::from_str(&resp.into_string().map_err(|e| e.to_string())?)
                        .map_err(|e| format!("목록 응답 오류: {e}"))?;
                for f in v["files"].as_array().cloned().unwrap_or_default() {
                    let name = f["name"].as_str().unwrap_or("").to_string();
                    map.insert(
                        name,
                        Entry {
                            id: f["id"].as_str().unwrap_or("").into(),
                            size: f["size"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0),
                            folder: f["mimeType"].as_str() == Some(FOLDER_MIME),
                        },
                    );
                }
                match v["nextPageToken"].as_str() {
                    Some(p) => page = Some(p.to_string()),
                    None => break,
                }
            }
            self.listings.insert(parent.to_string(), map);
        }
        Ok(self.listings.get_mut(parent).unwrap())
    }

    /// 경로(예: ["Kova2 Backup","id","blobs","ab"])의 폴더를 만들어 가며 마지막 폴더 id 를 돌려준다.
    fn ensure_folder(&mut self, path: &[&str]) -> Result<String, String> {
        let key = path.join("/");
        if let Some(id) = self.folders.get(&key) {
            return Ok(id.clone());
        }
        let mut parent = "root".to_string();
        let mut acc = String::new();
        for name in path {
            if !acc.is_empty() {
                acc.push('/');
            }
            acc.push_str(name);
            if let Some(id) = self.folders.get(&acc) {
                parent = id.clone();
                continue;
            }
            let found = self.list(&parent)?.get(*name).filter(|e| e.folder).cloned();
            let id = match found {
                Some(e) => e.id,
                None => {
                    let url = format!("{}/files", self.ep.api);
                    let meta = serde_json::json!({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).to_string();
                    let resp = self.call(|a, t| {
                        a.post(&url)
                            .set("Authorization", &format!("Bearer {t}"))
                            .set("Content-Type", "application/json; charset=UTF-8")
                            .query("fields", "id")
                            .send_string(&meta)
                    })?;
                    let v: serde_json::Value =
                        serde_json::from_str(&resp.into_string().map_err(|e| e.to_string())?)
                            .map_err(|e| e.to_string())?;
                    let id = v["id"]
                        .as_str()
                        .ok_or("폴더 생성 응답에 id 가 없습니다")?
                        .to_string();
                    self.list(&parent)?.insert(
                        name.to_string(),
                        Entry {
                            id: id.clone(),
                            size: 0,
                            folder: true,
                        },
                    );
                    id
                }
            };
            self.folders.insert(acc.clone(), id.clone());
            parent = id;
        }
        Ok(parent)
    }

    /// 폴더 `dir_path` 의 `name` 파일이 같은 크기로 이미 있으면 true(건너뜀), 아니면 올린다(크기가 다르면 덮어씀).
    fn put_file(
        &mut self,
        dir_path: &[&str],
        name: &str,
        local: &Path,
    ) -> Result<Option<u64>, String> {
        let parent = self.ensure_folder(dir_path)?;
        let size = fs::metadata(local).map_err(|e| e.to_string())?.len();
        let existing = self.list(&parent)?.get(name).cloned();
        if let Some(e) = &existing {
            if !e.folder && e.size == size {
                return Ok(None);
            }
        }
        let id = self.upload(existing.map(|e| e.id), name, &parent, local, size)?;
        self.list(&parent)?.insert(
            name.to_string(),
            Entry {
                id,
                size,
                folder: false,
            },
        );
        Ok(Some(size))
    }

    fn upload(
        &mut self,
        existing: Option<String>,
        name: &str,
        parent: &str,
        local: &Path,
        size: u64,
    ) -> Result<String, String> {
        let (method, url, meta) = match &existing {
            Some(id) => (
                "PATCH",
                format!(
                    "{}/files/{id}?uploadType=resumable&fields=id",
                    self.ep.upload
                ),
                "{}".to_string(),
            ),
            None => (
                "POST",
                format!("{}/files?uploadType=resumable&fields=id", self.ep.upload),
                serde_json::json!({"name": name, "parents": [parent]}).to_string(),
            ),
        };
        let init = self.call(|a, t| {
            a.request(method, &url)
                .set("Authorization", &format!("Bearer {t}"))
                .set("Content-Type", "application/json; charset=UTF-8")
                .set("X-Upload-Content-Length", &size.to_string())
                .send_string(&meta)
        })?;
        let session = init
            .header("Location")
            .ok_or("업로드 세션 주소를 받지 못했습니다")?
            .to_string();
        let mut f = fs::File::open(local).map_err(|e| format!("{}: {e}", local.display()))?;
        let mut start = 0u64;
        loop {
            let len = ((size - start) as usize).min(CHUNK);
            let mut buf = vec![0u8; len];
            f.read_exact(&mut buf).map_err(|e| e.to_string())?;
            let range = if size == 0 {
                "bytes */0".to_string()
            } else {
                format!("bytes {start}-{}/{size}", start + len as u64 - 1)
            };
            let resp = self.call(|a, t| {
                a.put(&session)
                    .set("Authorization", &format!("Bearer {t}"))
                    .set("Content-Range", &range)
                    .send_bytes(&buf)
            })?;
            match resp.status() {
                200 | 201 => {
                    let v: serde_json::Value =
                        serde_json::from_str(&resp.into_string().map_err(|e| e.to_string())?)
                            .unwrap_or_default();
                    return Ok(v["id"].as_str().unwrap_or("").to_string());
                }
                308 if size > 0 => start += len as u64,
                s => return Err(format!("업로드 응답 오류: HTTP {s}")),
            }
            if start >= size {
                return Err("업로드가 끝났지만 완료 응답이 없습니다".into());
            }
        }
    }
}

/// 저장소의 스냅샷과 블롭(팩 포함)을 구글 드라이브 `folder`(예: ["Kova2 Backup", "프로파일ID"]) 아래로 증분 복제한다.
/// 블롭을 먼저 올리고 매니페스트를 마지막에 올려 불완전 스냅샷이 보이지 않게 한다.
pub fn replicate_to_drive(
    repo: &Path,
    client: &mut DriveClient,
    folder: &[&str],
    cancel: &AtomicBool,
) -> Result<DriveReplicaReport, String> {
    let mut rep = DriveReplicaReport::default();
    let snaps = repo.join("snapshots");
    if !snaps.is_dir() {
        return Ok(rep);
    }
    let mut manifests: Vec<PathBuf> = fs::read_dir(&snaps)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    manifests.sort();
    let packs = kova_engine::pack::load_index(repo);
    let mut done_packs = HashSet::new();
    let sub = |s: &str| -> Vec<String> {
        folder
            .iter()
            .map(|x| x.to_string())
            .chain(s.split('/').map(|x| x.to_string()))
            .collect()
    };

    for mp in manifests {
        if cancel.load(Ordering::Relaxed) {
            return Err("취소됨".into());
        }
        let name = mp.file_name().unwrap().to_string_lossy().to_string();
        let snap_dir = sub("snapshots");
        let snap_dir_ref: Vec<&str> = snap_dir.iter().map(|s| s.as_str()).collect();
        let parent = client.ensure_folder(&snap_dir_ref)?;
        if client.list(&parent)?.get(&name).is_some() {
            rep.snapshots_skipped += 1;
            continue;
        }
        let m: SnapshotManifest = serde_json::from_reader(io::BufReader::new(
            fs::File::open(&mp).map_err(|e| e.to_string())?,
        ))
        .map_err(|e| format!("{}: {e}", mp.display()))?;
        let mut hashes = Vec::new();
        for fe in m.files() {
            hashes.extend(fe.chunks.into_iter().map(|c| c.hash));
            if let Some(b) = fe.blob_id {
                hashes.push(b);
            }
            if let Some(s) = fe.sha256 {
                hashes.push(s);
            }
        }
        let mut seen = HashSet::new();
        for h in hashes {
            if !seen.insert(h.clone()) {
                continue;
            }
            if cancel.load(Ordering::Relaxed) {
                return Err("취소됨".into());
            }
            let prefix = if h.len() >= 2 { &h[0..2] } else { "misc" };
            let local_blob = repo.join("blobs").join(prefix).join(format!("{h}.blob"));
            if local_blob.is_file() {
                let d = sub(&format!("blobs/{prefix}"));
                let dr: Vec<&str> = d.iter().map(|s| s.as_str()).collect();
                match client.put_file(&dr, &format!("{h}.blob"), &local_blob)? {
                    Some(n) => {
                        rep.bytes_uploaded += n;
                        rep.blobs_uploaded += 1;
                    }
                    None => rep.blobs_skipped += 1,
                }
            } else if let Some(entry) = packs.get(&h) {
                if done_packs.insert(entry.pack.clone()) {
                    use kova_engine::pack::{idx_file, pack_file};
                    let d = sub("packs");
                    let dr: Vec<&str> = d.iter().map(|s| s.as_str()).collect();
                    let a = client.put_file(
                        &dr,
                        &format!("{}.pack", entry.pack),
                        &pack_file(repo, &entry.pack),
                    )?;
                    let b = client.put_file(
                        &dr,
                        &format!("{}.idx", entry.pack),
                        &idx_file(repo, &entry.pack),
                    )?;
                    if a.is_none() && b.is_none() {
                        rep.blobs_skipped += 1;
                    } else {
                        rep.bytes_uploaded += a.unwrap_or(0) + b.unwrap_or(0);
                        rep.blobs_uploaded += 1;
                    }
                }
            } else {
                return Err(format!("원본에 블롭이 없음: {h}"));
            }
        }
        let n = client
            .put_file(&snap_dir_ref, &name, &mp)?
            .unwrap_or_default();
        rep.bytes_uploaded += n;
        rep.snapshots_uploaded += 1;
    }
    Ok(rep)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpStream;
    use std::sync::{Arc, Mutex};

    #[test]
    fn b64url_matches_rfc4648_vectors() {
        assert_eq!(b64url(b""), "");
        assert_eq!(b64url(b"f"), "Zg");
        assert_eq!(b64url(b"fo"), "Zm8");
        assert_eq!(b64url(b"foo"), "Zm9v");
        assert_eq!(b64url(&[0xfb, 0xff]), "-_8");
        // RFC 7636 부록 B
        let v = b64url(&Sha256::digest(
            b"dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        ));
        assert_eq!(v, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
    }

    #[test]
    fn pct_roundtrip() {
        let s = "http://127.0.0.1:5/ a&b=한";
        assert_eq!(pct_decode(&pct_encode(s)), s);
        assert_eq!(pct_decode("a%2Fb+c%"), "a/b c%");
    }

    /// 모의 구글 서버: 토큰·목록·폴더 생성·이어올리기를 메모리로 흉내낸다.
    #[derive(Default)]
    struct Mock {
        // id -> (name, parent, folder, data)
        files: Vec<(String, String, bool, Vec<u8>)>,
        sessions: Vec<(Option<usize>, String, String, Vec<u8>)>, // (기존 index, name, parent, 누적)
        token_calls: usize,
        refresh_calls: usize,
        code_verifier_ok: bool,
        expect_challenge: Option<String>,
    }

    fn read_req(s: &mut TcpStream) -> (String, String, Vec<u8>, HashMap<String, String>) {
        let mut buf = Vec::new();
        let mut tmp = [0u8; 8192];
        let head_end = loop {
            let n = s.read(&mut tmp).unwrap();
            if n == 0 {
                break buf.len();
            }
            buf.extend_from_slice(&tmp[..n]);
            if let Some(p) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                break p + 4;
            }
        };
        let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
        let mut lines = head.lines();
        let first = lines.next().unwrap_or("").to_string();
        let mut headers = HashMap::new();
        for l in lines {
            if let Some((k, v)) = l.split_once(':') {
                headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
            }
        }
        let cl: usize = headers
            .get("content-length")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        let mut body = buf[head_end..].to_vec();
        while body.len() < cl {
            let n = s.read(&mut tmp).unwrap();
            if n == 0 {
                break;
            }
            body.extend_from_slice(&tmp[..n]);
        }
        let mut p = first.split_whitespace();
        (
            p.next().unwrap_or("").to_string(),
            p.next().unwrap_or("").to_string(),
            body,
            headers,
        )
    }

    fn respond(s: &mut TcpStream, status: &str, extra: &str, body: &str) {
        let _ = write!(
            s,
            "HTTP/1.1 {status}\r\n{extra}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
    }

    fn start_mock(st: Arc<Mutex<Mock>>) -> (String, Endpoints) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", l.local_addr().unwrap());
        let b2 = base.clone();
        std::thread::spawn(move || {
            for conn in l.incoming() {
                let Ok(mut s) = conn else { continue };
                let (method, target, body, headers) = read_req(&mut s);
                let (path, query) = target.split_once('?').unwrap_or((&target, ""));
                let q = query_map(query);
                let mut m = st.lock().unwrap();
                if path == "/token" {
                    let f = query_map(&String::from_utf8_lossy(&body));
                    m.token_calls += 1;
                    if f.get("grant_type").map(|s| s.as_str()) == Some("refresh_token") {
                        m.refresh_calls += 1;
                        respond(
                            &mut s,
                            "200 OK",
                            "",
                            r#"{"access_token":"AT2","expires_in":3600,"scope":"https://www.googleapis.com/auth/drive.file"}"#,
                        );
                    } else {
                        let ver = f.get("code_verifier").cloned().unwrap_or_default();
                        m.code_verifier_ok = Some(b64url(&Sha256::digest(ver.as_bytes())))
                            == m.expect_challenge
                            && f.get("code").map(|s| s.as_str()) == Some("the-code");
                        respond(
                            &mut s,
                            "200 OK",
                            "",
                            r#"{"access_token":"AT1","refresh_token":"RT1","expires_in":3600,"scope":"https://www.googleapis.com/auth/drive.file"}"#,
                        );
                    }
                } else if path == "/revoke" {
                    respond(&mut s, "200 OK", "", "{}");
                } else if path == "/api/files" && method == "GET" {
                    let qq = q.get("q").cloned().unwrap_or_default();
                    let parent = qq.split('\'').nth(1).unwrap_or("").to_string();
                    let list: Vec<String> = m
                        .files
                        .iter()
                        .enumerate()
                        .filter(|(_, f)| f.1 == parent)
                        .map(|(i, f)| {
                            format!(
                                r#"{{"id":"f{i}","name":{:?},"size":"{}","mimeType":"{}"}}"#,
                                f.0,
                                f.3.len(),
                                if f.2 {
                                    FOLDER_MIME
                                } else {
                                    "application/octet-stream"
                                }
                            )
                        })
                        .collect();
                    respond(
                        &mut s,
                        "200 OK",
                        "",
                        &format!(r#"{{"files":[{}]}}"#, list.join(",")),
                    );
                } else if path == "/api/files" && method == "POST" {
                    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
                    m.files.push((
                        v["name"].as_str().unwrap().into(),
                        v["parents"][0].as_str().unwrap().into(),
                        true,
                        vec![],
                    ));
                    let id = format!("f{}", m.files.len() - 1);
                    respond(&mut s, "200 OK", "", &format!(r#"{{"id":"{id}"}}"#));
                } else if path.starts_with("/up/files") {
                    let v: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
                    let existing = path
                        .strip_prefix("/up/files/")
                        .map(|id| id.trim_start_matches('f').parse::<usize>().unwrap());
                    let (name, parent) = match existing {
                        Some(i) => (m.files[i].0.clone(), m.files[i].1.clone()),
                        None => (
                            v["name"].as_str().unwrap().to_string(),
                            v["parents"][0].as_str().unwrap().to_string(),
                        ),
                    };
                    m.sessions.push((existing, name, parent, vec![]));
                    let n = m.sessions.len() - 1;
                    let loc = format!(
                        "Location: {base_url}/session/{n}\r\n",
                        base_url = headers.get("host").map(|h| format!("http://{h}")).unwrap()
                    );
                    respond(&mut s, "200 OK", &loc, "{}");
                } else if let Some(n) = path.strip_prefix("/session/") {
                    let n: usize = n.parse().unwrap();
                    let range = headers.get("content-range").cloned().unwrap_or_default();
                    let total: usize = range
                        .rsplit('/')
                        .next()
                        .and_then(|t| t.parse().ok())
                        .unwrap_or(0);
                    m.sessions[n].3.extend_from_slice(&body);
                    if m.sessions[n].3.len() >= total {
                        let (existing, name, parent, data) = m.sessions[n].clone();
                        let idx = match existing {
                            Some(i) => {
                                m.files[i].3 = data;
                                i
                            }
                            None => {
                                m.files.push((name, parent, false, data));
                                m.files.len() - 1
                            }
                        };
                        respond(&mut s, "200 OK", "", &format!(r#"{{"id":"f{idx}"}}"#));
                    } else {
                        respond(&mut s, "308 Resume Incomplete", "", "");
                    }
                } else {
                    respond(&mut s, "404 Not Found", "", "{}");
                }
            }
        });
        let ep = Endpoints {
            auth: format!("{b2}/auth"),
            token: format!("{b2}/token"),
            revoke: format!("{b2}/revoke"),
            api: format!("{b2}/api"),
            upload: format!("{b2}/up"),
        };
        (b2, ep)
    }

    fn creds() -> ClientCreds {
        ClientCreds {
            client_id: "cid".into(),
            client_secret: "sec".into(),
        }
    }

    #[test]
    fn login_stores_token_and_checks_pkce() {
        let st = Arc::new(Mutex::new(Mock::default()));
        let (_b, ep) = start_mock(st.clone());
        let dir = tempfile::tempdir().unwrap();
        let cancel = AtomicBool::new(false);
        let st2 = st.clone();
        login(
            &ep,
            &creds(),
            dir.path(),
            move |url| {
                let q = query_map(url.split_once('?').unwrap().1);
                assert_eq!(q["scope"], SCOPE);
                assert_eq!(q["code_challenge_method"], "S256");
                st2.lock().unwrap().expect_challenge = Some(q["code_challenge"].clone());
                let cb = format!(
                    "{}?code=the-code&state={}",
                    q["redirect_uri"],
                    pct_encode(&q["state"])
                );
                std::thread::spawn(move || {
                    let _ = agent().get(&cb).call();
                });
            },
            &cancel,
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(
            st.lock().unwrap().code_verifier_ok,
            "PKCE verifier/code 불일치"
        );
        assert!(is_connected(dir.path()));
        let t = load_token(dir.path()).unwrap();
        assert_eq!((t.refresh.as_str(), t.access.as_str()), ("RT1", "AT1"));
        disconnect(dir.path(), &ep);
        assert!(!is_connected(dir.path()));
    }

    #[test]
    fn login_rejects_wrong_state_and_times_out() {
        let st = Arc::new(Mutex::new(Mock::default()));
        let (_b, ep) = start_mock(st);
        let dir = tempfile::tempdir().unwrap();
        let cancel = AtomicBool::new(false);
        let r = login(
            &ep,
            &creds(),
            dir.path(),
            |url| {
                let q = query_map(url.split_once('?').unwrap().1);
                let cb = format!("{}?code=x&state=WRONG", q["redirect_uri"]);
                std::thread::spawn(move || {
                    let _ = agent().get(&cb).call();
                });
            },
            &cancel,
            Duration::from_millis(800),
        );
        assert!(r.unwrap_err().contains("시간이 초과"));
        assert!(!is_connected(dir.path()));
    }

    #[test]
    fn replicate_uploads_incrementally_and_refreshes_expired_token() {
        let st = Arc::new(Mutex::new(Mock::default()));
        let (_b, ep) = start_mock(st.clone());
        let dir = tempfile::tempdir().unwrap();
        // 만료된 토큰을 심어 갱신 경로를 지난다
        save_token(
            dir.path(),
            &Token {
                refresh: "RT1".into(),
                access: "OLD".into(),
                expires_at: 0,
            },
        )
        .unwrap();

        // 저장소: 스냅샷 매니페스트 1개 + 블롭 (hashes_of 는 서비스 규약: files() 의 chunks)
        let work = tempfile::tempdir().unwrap();
        let src = work.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.txt"), "hello drive").unwrap();
        let repo = work.path().join("repo");
        kova_engine::ops::run_backup(
            &kova_engine::ops::BackupOptions {
                sources: vec![src],
                repo: repo.clone(),
                session_id: None,
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &|_| {},
            &AtomicBool::new(false),
        )
        .unwrap();

        let mut c = DriveClient::open(dir.path(), ep.clone(), creds()).unwrap();
        c.retry_base = Duration::ZERO;
        let cancel = AtomicBool::new(false);
        let r1 = replicate_to_drive(&repo, &mut c, &["Kova2 Backup", "p1"], &cancel).unwrap();
        assert_eq!(r1.snapshots_uploaded, 1);
        assert!(r1.blobs_uploaded >= 1);
        assert_eq!(st.lock().unwrap().refresh_calls, 1);
        {
            let m = st.lock().unwrap();
            assert!(m.files.iter().any(|f| f.2 && f.0 == "Kova2 Backup"));
            assert!(m.files.iter().any(|f| !f.2 && f.0.ends_with(".json")));
        }
        // 두 번째 실행은 새 클라이언트(목록 캐시 없음)로도 전부 건너뛴다
        let mut c2 = DriveClient::open(dir.path(), ep, creds()).unwrap();
        let r2 = replicate_to_drive(&repo, &mut c2, &["Kova2 Backup", "p1"], &cancel).unwrap();
        assert_eq!(r2.snapshots_uploaded, 0);
        assert_eq!(r2.snapshots_skipped, 1);
        assert_eq!(r2.bytes_uploaded, 0);
    }

    /// 실계정 소량 시험(수동): 앱에서 먼저 연결한 뒤 `cargo test -p kova-core real_drive -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn real_drive_smoke() {
        let dir = PathBuf::from(std::env::var("ProgramData").unwrap())
            .join("Kova2")
            .join("config");
        let creds = load_client(&dir).unwrap();
        let work = tempfile::tempdir().unwrap();
        let src = work.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("a.txt"), "kova2 drive smoke test").unwrap();
        let mut big = vec![
            0u8;
            std::env::var("KOVA_SMOKE_MB")
                .ok()
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(20)
                * 1024
                * 1024
        ]; // 압축이 안 되는 20MB: 8MB 단위 이어올리기 경로를 탄다
        getrandom::getrandom(&mut big).unwrap();
        fs::write(src.join("b.bin"), big).unwrap();
        let repo = work.path().join("repo");
        kova_engine::ops::run_backup(
            &kova_engine::ops::BackupOptions {
                sources: vec![src],
                repo: repo.clone(),
                session_id: None,
                zstd_level: 3,
                exclude: vec![],
                profile_id: None,
                scan_from: None,
            },
            &|_| {},
            &AtomicBool::new(false),
        )
        .unwrap();
        let mut c = DriveClient::open(&dir, Endpoints::default(), creds).unwrap();
        let cancel = AtomicBool::new(false);
        let f = ["Kova2 Backup", "_smoke_test"];
        let t0 = Instant::now();
        let r1 = replicate_to_drive(&repo, &mut c, &f, &cancel).unwrap();
        println!("1st: {r1:?} {:.1}s", t0.elapsed().as_secs_f64());
        let mut c2 =
            DriveClient::open(&dir, Endpoints::default(), load_client(&dir).unwrap()).unwrap();
        let r2 = replicate_to_drive(&repo, &mut c2, &f, &cancel).unwrap();
        println!("2nd: {r2:?}");
        assert_eq!(r2.snapshots_uploaded, 0);
        assert_eq!(r2.bytes_uploaded, 0);
    }

    /// 수동: Kova2 가 만든 드라이브 폴더 최상위 목록(이름·개수)을 출력한다. 삭제는 하지 않는다.
    #[test]
    #[ignore]
    fn real_drive_list_top() {
        let dir = PathBuf::from(std::env::var("ProgramData").unwrap())
            .join("Kova2")
            .join("config");
        let mut c =
            DriveClient::open(&dir, Endpoints::default(), load_client(&dir).unwrap()).unwrap();
        let top: Vec<(String, Entry)> = c.list("root").unwrap().clone().into_iter().collect();
        for (n, e) in top {
            println!("ROOT {n} folder={} id={}", e.folder, e.id);
            if e.folder {
                let sub: Vec<(String, Entry)> =
                    c.list(&e.id).unwrap().clone().into_iter().collect();
                for (n2, e2) in sub {
                    let cnt = if e2.folder {
                        c.list(&e2.id).unwrap().len()
                    } else {
                        0
                    };
                    println!("  {n2} folder={} children={cnt}", e2.folder);
                }
            }
        }
    }

    /// 수동: 지정한 폴더 id 들을 휴지통으로 보낸다(복구 가능). KOVA_TRASH_IDS=id1,id2
    #[test]
    #[ignore]
    fn real_drive_trash() {
        let dir = PathBuf::from(std::env::var("ProgramData").unwrap())
            .join("Kova2")
            .join("config");
        let mut c =
            DriveClient::open(&dir, Endpoints::default(), load_client(&dir).unwrap()).unwrap();
        for id in std::env::var("KOVA_TRASH_IDS").unwrap().split(',') {
            let url = format!("{}/files/{id}", c.ep.api);
            c.call(|a, t| {
                a.request("PATCH", &url)
                    .set("Authorization", &format!("Bearer {t}"))
                    .set("Content-Type", "application/json")
                    .send_string(r#"{"trashed":true}"#)
            })
            .unwrap();
            println!("TRASHED {id}");
        }
    }
}
