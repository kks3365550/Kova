//! S3 호환(AWS S3 / Cloudflare R2 / Backblaze B2 / MinIO) 클라우드 오프사이트 백엔드.
//! 추가 외부 SDK 없이 표준 AWS Signature Version 4(HMAC-SHA256)와 `ureq`로 구현한다.
use chrono::Utc;
use kova_engine::storage::SnapshotManifest;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

const S3_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct S3Config {
    /// 예: "https://s3.ap-northeast-2.amazonaws.com" 또는 "https://<acct>.r2.cloudflarestorage.com"
    pub endpoint: String,
    pub region: String,
    pub bucket: String,
    #[serde(default)]
    pub prefix: String,
    pub access_key: String,
    pub secret_key: String,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct S3ReplicaReport {
    pub snapshots_uploaded: usize,
    pub snapshots_skipped: usize,
    pub blobs_uploaded: usize,
    pub blobs_skipped: usize,
    pub bytes_uploaded: u64,
}

pub fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut k = [0u8; 64];
    if key.len() > 64 {
        let h = Sha256::digest(key);
        k[..32].copy_from_slice(&h);
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut ipad = [0x36u8; 64];
    let mut opad = [0x5cu8; 64];
    for i in 0..64 {
        ipad[i] ^= k[i];
        opad[i] ^= k[i];
    }
    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(data);
    let inner_hash = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_hash);
    outer.finalize().into()
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(&mut s, "{b:02x}");
    }
    s
}

fn sha256_hex(data: &[u8]) -> String {
    hex_lower(&Sha256::digest(data))
}

fn host_from_endpoint(endpoint: &str) -> Result<String, String> {
    let rest = endpoint
        .trim()
        .strip_prefix("https://")
        .or_else(|| endpoint.trim().strip_prefix("http://"))
        .ok_or("S3 endpoint는 http:// 또는 https:// 로 시작해야 합니다")?;
    let host = rest.split('/').next().unwrap_or("");
    if host.is_empty() {
        return Err("S3 endpoint 호스트가 비어 있습니다".into());
    }
    Ok(host.to_string())
}

fn object_uri(bucket: &str, key: &str) -> String {
    let key = key.trim_start_matches('/');
    format!("/{}/{}", bucket.trim_matches('/'), key)
}

/// AWS Signature Version 4 인증 헤더(`Authorization`, `x-amz-date`, `x-amz-content-sha256`, `Host`)를 생성한다.
#[allow(clippy::too_many_arguments)]
pub fn sign_v4(
    method: &str,
    host: &str,
    canonical_uri: &str,
    region: &str,
    access_key: &str,
    secret_key: &str,
    payload: &[u8],
    amz_date: &str,
) -> (String, String, String) {
    let date_stamp = &amz_date[..8];
    let payload_hash = sha256_hex(payload);
    let canonical_headers =
        format!("host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n");
    let signed_headers = "host;x-amz-content-sha256;x-amz-date";
    let canonical_request = format!(
        "{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    );
    let scope = format!("{date_stamp}/{region}/s3/aws4_request");
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}",
        sha256_hex(canonical_request.as_bytes())
    );
    let k_date = hmac_sha256(
        format!("AWS4{secret_key}").as_bytes(),
        date_stamp.as_bytes(),
    );
    let k_region = hmac_sha256(&k_date, region.as_bytes());
    let k_service = hmac_sha256(&k_region, b"s3");
    let k_signing = hmac_sha256(&k_service, b"aws4_request");
    let signature = hex_lower(&hmac_sha256(&k_signing, string_to_sign.as_bytes()));
    let authorization = format!(
        "AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    );
    (authorization, amz_date.to_string(), payload_hash)
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(S3_TIMEOUT)
        .timeout_read(S3_TIMEOUT)
        .timeout_write(S3_TIMEOUT)
        .build()
}

fn full_key(cfg: &S3Config, rel: &str) -> String {
    let p = cfg.prefix.trim_matches('/');
    let r = rel.trim_start_matches('/');
    if p.is_empty() {
        r.to_string()
    } else {
        format!("{p}/{r}")
    }
}

pub fn head_object(cfg: &S3Config, rel_key: &str) -> Result<bool, String> {
    let host = host_from_endpoint(&cfg.endpoint)?;
    let uri = object_uri(&cfg.bucket, &full_key(cfg, rel_key));
    let url = format!("{}{}", cfg.endpoint.trim_end_matches('/'), uri);
    let amz_date = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let (auth, date, hash) = sign_v4(
        "HEAD",
        &host,
        &uri,
        &cfg.region,
        &cfg.access_key,
        &cfg.secret_key,
        b"",
        &amz_date,
    );
    match agent()
        .head(&url)
        .set("Host", &host)
        .set("x-amz-date", &date)
        .set("x-amz-content-sha256", &hash)
        .set("Authorization", &auth)
        .call()
    {
        Ok(r) => Ok((200..300).contains(&r.status())),
        Err(ureq::Error::Status(404, _)) => Ok(false),
        Err(e) => Err(format!("S3 HEAD 실패({rel_key}): {e}")),
    }
}

pub fn put_object(cfg: &S3Config, rel_key: &str, data: &[u8]) -> Result<(), String> {
    let host = host_from_endpoint(&cfg.endpoint)?;
    let uri = object_uri(&cfg.bucket, &full_key(cfg, rel_key));
    let url = format!("{}{}", cfg.endpoint.trim_end_matches('/'), uri);
    let amz_date = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let (auth, date, hash) = sign_v4(
        "PUT",
        &host,
        &uri,
        &cfg.region,
        &cfg.access_key,
        &cfg.secret_key,
        data,
        &amz_date,
    );
    let resp = agent()
        .put(&url)
        .set("Host", &host)
        .set("x-amz-date", &date)
        .set("x-amz-content-sha256", &hash)
        .set("Authorization", &auth)
        .set("Content-Type", "application/octet-stream")
        .send_bytes(data)
        .map_err(|e| format!("S3 PUT 실패({rel_key}): {e}"))?;
    if (200..300).contains(&resp.status()) {
        Ok(())
    } else {
        Err(format!("S3 PUT 오류({rel_key}): HTTP {}", resp.status()))
    }
}

/// 저장소의 스냅샷과 블롭(팩 포함)을 S3 호환 버킷으로 증분 복제한다.
/// 블롭을 먼저 올리고 매니페스트를 마지막에 올려 불완전 스냅샷이 보이지 않게 한다.
pub fn replicate_to_s3(
    repo: &Path,
    cfg: &S3Config,
    cancel: &AtomicBool,
) -> Result<S3ReplicaReport, String> {
    let mut rep = S3ReplicaReport::default();
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

    for mp in manifests {
        if cancel.load(Ordering::Relaxed) {
            return Err("취소됨".into());
        }
        let name = mp.file_name().unwrap().to_string_lossy().to_string();
        let snap_key = format!("snapshots/{name}");
        if head_object(cfg, &snap_key)? {
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
                let key = format!("blobs/{prefix}/{h}.blob");
                if head_object(cfg, &key)? {
                    rep.blobs_skipped += 1;
                } else {
                    let data = fs::read(&local_blob).map_err(|e| e.to_string())?;
                    put_object(cfg, &key, &data)?;
                    rep.bytes_uploaded += data.len() as u64;
                    rep.blobs_uploaded += 1;
                }
            } else if let Some(entry) = packs.get(&h) {
                if done_packs.insert(entry.pack.clone()) {
                    use kova_engine::pack::{idx_file, pack_file};
                    let pk_key = format!("packs/{}.pack", entry.pack);
                    let ix_key = format!("packs/{}.idx", entry.pack);
                    if head_object(cfg, &ix_key)? && head_object(cfg, &pk_key)? {
                        rep.blobs_skipped += 1;
                    } else {
                        let pk_data =
                            fs::read(pack_file(repo, &entry.pack)).map_err(|e| e.to_string())?;
                        let ix_data =
                            fs::read(idx_file(repo, &entry.pack)).map_err(|e| e.to_string())?;
                        put_object(cfg, &pk_key, &pk_data)?;
                        put_object(cfg, &ix_key, &ix_data)?;
                        rep.bytes_uploaded += (pk_data.len() + ix_data.len()) as u64;
                        rep.blobs_uploaded += 1;
                    }
                }
            } else {
                return Err(format!("원본에 블롭이 없음: {h}"));
            }
        }
        let manifest_bytes = fs::read(&mp).map_err(|e| e.to_string())?;
        put_object(cfg, &snap_key, &manifest_bytes)?;
        rep.bytes_uploaded += manifest_bytes.len() as u64;
        rep.snapshots_uploaded += 1;
    }
    Ok(rep)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    #[test]
    fn sigv4_signature_is_deterministic_and_well_formed() {
        let (auth, date, hash) = sign_v4(
            "PUT",
            "s3.us-east-1.amazonaws.com",
            "/mybucket/test.txt",
            "us-east-1",
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            b"hello",
            "20261119T120000Z",
        );
        assert_eq!(date, "20261119T120000Z");
        assert_eq!(
            hash,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        assert!(auth.starts_with(
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261119/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
        ));
        assert_eq!(auth.split("Signature=").nth(1).unwrap().len(), 64);
    }

    #[test]
    fn replicate_to_mock_s3_uploads_then_skips() {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("doc.txt"), "s3 offsite test content").unwrap();
        let repo = d.path().join("repo");
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

        let store: Arc<Mutex<HashMap<String, Vec<u8>>>> = Arc::new(Mutex::new(HashMap::new()));
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stop_srv = stop.clone();
        let store_srv = store.clone();
        let srv = std::thread::spawn(move || {
            listener.set_nonblocking(true).unwrap();
            while !stop_srv.load(Ordering::Relaxed) {
                let Ok((mut sock, _)) = listener.accept() else {
                    std::thread::sleep(Duration::from_millis(5));
                    continue;
                };
                sock.set_nonblocking(false).unwrap();
                let mut data = Vec::new();
                let mut buf = [0u8; 8192];
                let mut body_start = None;
                let mut content_len = 0usize;
                loop {
                    let n = sock.read(&mut buf).unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    data.extend_from_slice(&buf[..n]);
                    if body_start.is_none() {
                        if let Some(pos) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                            let hdr = String::from_utf8_lossy(&data[..pos]);
                            content_len = hdr
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
                            body_start = Some(pos + 4);
                        }
                    }
                    if let Some(bs) = body_start {
                        if data.len() >= bs + content_len {
                            break;
                        }
                    }
                }
                let bs = body_start.unwrap_or(data.len());
                let hdr = String::from_utf8_lossy(&data[..bs]).to_string();
                let first = hdr.lines().next().unwrap_or("");
                let mut parts = first.split_whitespace();
                let method = parts.next().unwrap_or("");
                let uri = parts.next().unwrap_or("").to_string();
                assert!(hdr.contains("AWS4-HMAC-SHA256"));
                match method {
                    "HEAD" => {
                        let exists = store_srv.lock().unwrap().contains_key(&uri);
                        let resp = if exists {
                            "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        } else {
                            "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        };
                        let _ = sock.write_all(resp.as_bytes());
                    }
                    "PUT" => {
                        let body = data[bs..bs + content_len].to_vec();
                        store_srv.lock().unwrap().insert(uri, body);
                        let _ = sock.write_all(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        );
                    }
                    _ => {
                        let _ = sock.write_all(
                            b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        );
                    }
                }
                let _ = sock.flush();
            }
        });

        let cfg = S3Config {
            endpoint: format!("http://{addr}"),
            region: "ap-northeast-2".into(),
            bucket: "kova-bucket".into(),
            prefix: "pc1".into(),
            access_key: "TESTKEY".into(),
            secret_key: "TESTSECRET".into(),
        };
        let no = AtomicBool::new(false);
        let r1 = replicate_to_s3(&repo, &cfg, &no).unwrap();
        assert_eq!(r1.snapshots_uploaded, 1);
        assert!(r1.blobs_uploaded >= 1);
        assert!(r1.bytes_uploaded > 0);

        let r2 = replicate_to_s3(&repo, &cfg, &no).unwrap();
        assert_eq!((r2.snapshots_uploaded, r2.snapshots_skipped), (0, 1));

        stop.store(true, Ordering::Relaxed);
        srv.join().unwrap();
    }
}
