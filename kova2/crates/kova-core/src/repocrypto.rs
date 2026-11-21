//! 저장소 암호화 켜기·키 보관. 키는 저장소 밖(`%ProgramData%\Kova2\keys`)에 DPAPI(PC 단위)로 보호해 두고,
//! 저장소에는 `crypto.json` 표식(키 확인값만, 키 없음)을 둔다. 저장소 드라이브만 분실·도난되면 열 수 없다.
//! 복구 코드(키 16진수)를 따로 보관하지 않으면 이 PC가 망가졌을 때 백업을 읽을 수 없다.
use crate::crypto::{hex, random_bytes, unhex, CryptoEngine};
use kova_engine::codec::{self, BlobCodec, MARKER};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Serialize, Deserialize)]
struct Marker {
    version: u32,
    repo_id: String,
    key_check: String,
}

struct EngineCodec(CryptoEngine);

impl BlobCodec for EngineCodec {
    fn encode(&self, plain: &[u8], hash: &str, level: i32) -> Result<Vec<u8>, String> {
        self.0
            .encrypt_blob(plain, hash, level)
            .map_err(|e| e.to_string())
    }
    fn decode(&self, blob: &[u8], hash: &str) -> Result<Vec<u8>, String> {
        self.0
            .decrypt_unchecked(blob, hash)
            .map_err(|e| e.to_string())
    }
}

fn key_check(key: &[u8; 32]) -> String {
    let mut h = Sha256::new();
    h.update(b"kova2-key-check");
    h.update(key);
    hex(&h.finalize())
}

pub fn key_dir() -> PathBuf {
    if let Ok(d) = std::env::var("KOVA2_KEY_DIR") {
        return PathBuf::from(d);
    }
    let base = std::env::var("ProgramData").unwrap_or_else(|_| "C:\\ProgramData".into());
    PathBuf::from(base).join("Kova2").join("keys")
}

fn key_file(repo_id: &str) -> PathBuf {
    key_dir().join(format!("{repo_id}.key"))
}

fn read_marker(repo: &Path) -> Result<Marker, String> {
    let s = fs::read_to_string(repo.join(MARKER)).map_err(|e| format!("crypto.json 읽기: {e}"))?;
    serde_json::from_str(&s).map_err(|e| format!("crypto.json 형식 오류: {e}"))
}

pub fn is_enabled(repo: &Path) -> bool {
    repo.join(MARKER).is_file()
}

#[cfg(windows)]
pub(crate) mod dpapi {
    use std::ffi::c_void;
    #[repr(C)]
    struct Blob {
        len: u32,
        data: *mut u8,
    }
    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            din: *const Blob,
            desc: *const u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            dout: *mut Blob,
        ) -> i32;
        fn CryptUnprotectData(
            din: *const Blob,
            desc: *mut *mut u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            dout: *mut Blob,
        ) -> i32;
    }
    extern "system" {
        fn LocalFree(h: *mut c_void) -> *mut c_void;
    }
    const UI_FORBIDDEN: u32 = 0x1;
    const LOCAL_MACHINE: u32 = 0x4;

    fn run(data: &[u8], protect: bool) -> Result<Vec<u8>, String> {
        let din = Blob {
            len: data.len() as u32,
            data: data.as_ptr() as *mut u8,
        };
        let mut out = Blob {
            len: 0,
            data: std::ptr::null_mut(),
        };
        let flags = UI_FORBIDDEN | LOCAL_MACHINE;
        let ok = unsafe {
            if protect {
                CryptProtectData(
                    &din,
                    std::ptr::null(),
                    std::ptr::null(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    flags,
                    &mut out,
                )
            } else {
                CryptUnprotectData(
                    &din,
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    flags,
                    &mut out,
                )
            }
        };
        if ok == 0 {
            return Err(if protect {
                "DPAPI 보호 실패".into()
            } else {
                "DPAPI 해제 실패(다른 PC에서 만든 키이거나 손상)".into()
            });
        }
        let v = unsafe { std::slice::from_raw_parts(out.data, out.len as usize) }.to_vec();
        unsafe { LocalFree(out.data as *mut c_void) };
        Ok(v)
    }
    pub fn protect(d: &[u8]) -> Result<Vec<u8>, String> {
        run(d, true)
    }
    pub fn unprotect(d: &[u8]) -> Result<Vec<u8>, String> {
        run(d, false)
    }
}
#[cfg(not(windows))]
pub(crate) mod dpapi {
    pub fn protect(_: &[u8]) -> Result<Vec<u8>, String> {
        Err("DPAPI 는 Windows 전용".into())
    }
    pub fn unprotect(_: &[u8]) -> Result<Vec<u8>, String> {
        Err("DPAPI 는 Windows 전용".into())
    }
}

fn store_key(repo_id: &str, key: &[u8; 32]) -> Result<(), String> {
    let enc = dpapi::protect(key)?;
    fs::create_dir_all(key_dir()).map_err(|e| format!("키 폴더 만들기: {e}"))?;
    fs::write(key_file(repo_id), enc).map_err(|e| format!("키 저장: {e}"))
}

fn load_key(repo: &Path) -> Result<[u8; 32], String> {
    let m = read_marker(repo)?;
    let enc = fs::read(key_file(&m.repo_id))
        .map_err(|_| "이 PC에 저장소 키가 없습니다. 복구 코드로 키를 복원하세요".to_string())?;
    let raw = dpapi::unprotect(&enc)?;
    let key: [u8; 32] = raw.try_into().map_err(|_| "키 길이 오류".to_string())?;
    if key_check(&key) != m.key_check {
        return Err("키가 이 저장소의 것이 아닙니다".into());
    }
    Ok(key)
}

fn group(code: &str) -> String {
    code.as_bytes()
        .chunks(8)
        .map(|c| String::from_utf8_lossy(c).into_owned())
        .collect::<Vec<_>>()
        .join("-")
}

/// 저장소 암호화를 켠다. 새로 켰으면 복구 코드를, 이미 켜져 있으면 None 을 돌려준다.
/// 이미 있는 평문 블롭은 그대로 두고(읽기 가능), 이후 새 블롭만 암호화한다.
pub fn enable(repo: &Path) -> Result<Option<String>, String> {
    if is_enabled(repo) {
        return Ok(None);
    }
    fs::create_dir_all(repo).map_err(|e| format!("저장소 폴더: {e}"))?;
    // 백업·정리 중에는 켜지 않는다(쓰기 도중 표식이 생기면 한 스냅샷에 평문·암호문이 섞인다).
    // 잠금 파일을 열 권한이 없는 경우(일반 사용자 앱)에는 기존처럼 잠금 없이 진행한다.
    let _lock = match crate::lock::RepoLock::try_acquire(repo) {
        Ok(l) => Some(l),
        Err(crate::lock::LockError::Busy) => {
            return Err("저장소를 쓰는 작업이 있어 지금은 암호화를 켤 수 없습니다".into())
        }
        Err(crate::lock::LockError::Io(_)) => None,
    };
    if is_enabled(repo) {
        return Ok(None);
    }
    if has_encrypted_blobs(repo) {
        return Err(format!(
            "저장소에 암호화된 블롭이 있는데 암호화 표식({MARKER})이 없습니다. \
             새 키를 만들면 기존 백업을 영영 읽을 수 없으므로 거부합니다. 원래 {MARKER} 을 복원하세요"
        ));
    }
    let key: [u8; 32] = random_bytes();
    let repo_id = hex(&random_bytes::<8>());
    store_key(&repo_id, &key)?;
    let m = Marker {
        version: 1,
        repo_id,
        key_check: key_check(&key),
    };
    let json = serde_json::to_string_pretty(&m).map_err(|e| e.to_string())?;
    // 원자적 저장: 임시 파일에 쓰고 디스크에 내려쓴 뒤 이름을 바꾼다(반쯤 쓴 표식이 남지 않게)
    let tmp = repo.join(format!("{MARKER}.{}.tmp", std::process::id()));
    let write = || -> std::io::Result<()> {
        use std::io::Write;
        let mut f = fs::File::create(&tmp)?;
        f.write_all(json.as_bytes())?;
        f.sync_all()?;
        drop(f);
        fs::rename(&tmp, repo.join(MARKER))
    };
    if let Err(e) = write() {
        let _ = fs::remove_file(&tmp);
        return Err(format!("crypto.json 쓰기: {e}"));
    }
    Ok(Some(group(&hex(&key))))
}

/// 표식 없이 암호화 머리표(ENC\x01)를 가진 블롭·팩이 하나라도 있는가.
fn has_encrypted_blobs(repo: &Path) -> bool {
    use std::io::Read;
    let is_enc = |p: &Path| {
        let mut b = [0u8; 4];
        fs::File::open(p)
            .and_then(|mut f| f.read_exact(&mut b))
            .is_ok()
            && &b == b"ENC\x01"
    };
    let files = |d: PathBuf, ext: &'static str| {
        fs::read_dir(d)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(move |p| p.extension().is_some_and(|x| x == ext))
    };
    if files(repo.join("packs"), "pack").any(|p| is_enc(&p)) {
        return true;
    }
    fs::read_dir(repo.join("blobs"))
        .into_iter()
        .flatten()
        .flatten()
        .filter(|d| d.path().is_dir())
        .any(|d| files(d.path(), "blob").any(|p| is_enc(&p)))
}

/// 시험 간 `KOVA2_KEY_DIR` 환경 변수 경합을 막는 잠금(키 폴더를 바꾸는 시험은 모두 이것을 쥔다).
#[cfg(test)]
pub(crate) static TEST_KEY_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// 복구 코드로 이 PC의 키 파일을 다시 만든다(PC 교체·키 파일 분실 시).
pub fn restore_key(repo: &Path, code: &str) -> Result<(), String> {
    let m = read_marker(repo)?;
    let clean: String = code.chars().filter(|c| c.is_ascii_hexdigit()).collect();
    let raw = unhex(&clean).ok_or("복구 코드 형식 오류")?;
    let key: [u8; 32] = raw
        .try_into()
        .map_err(|_| "복구 코드 길이 오류".to_string())?;
    if key_check(&key) != m.key_check {
        return Err("복구 코드가 이 저장소의 것이 아닙니다".into());
    }
    store_key(&m.repo_id, &key)
}

/// 이 PC에 보관된 암호화 저장소 키를 복구 코드(8자리 그룹 16진수)로 내보낸다.
pub fn export_recovery_code(repo: &Path) -> Result<String, String> {
    let key = load_key(repo)?;
    Ok(group(&hex(&key)))
}

/// 복구 키트(Recovery Kit) 문서를 인쇄·보관하기 쉬운 텍스트로 만든다.
/// `entries`: `(프로파일 이름, 저장소 경로, 복구 코드)`
pub fn format_recovery_kit(entries: &[(String, String, String)]) -> String {
    let bitlocker_key = crate::health::bitlocker_recovery_key_c().ok();
    format_recovery_kit_with_bitlocker(entries, bitlocker_key.as_deref())
}

/// BitLocker 복구 키를 명시적으로 주입받아 복구 키트 문서를 생성한다.
pub fn format_recovery_kit_with_bitlocker(
    entries: &[(String, String, String)],
    bitlocker_key: Option<&str>,
) -> String {
    let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    let host = std::env::var("COMPUTERNAME").unwrap_or_else(|_| "UNKNOWN".into());
    let mut out = String::new();
    out.push_str("============================================================\r\n");
    out.push_str("  Kova2 암호화 복구 키트 (Recovery Kit)\r\n");
    out.push_str("============================================================\r\n");
    out.push_str(&format!("생성 시각: {now}\r\n"));
    out.push_str(&format!("생성 PC  : {host}\r\n"));
    out.push_str("\r\n");
    out.push_str("[중요 안내]\r\n");
    out.push_str(
        "- 이 문서는 PC 포맷·교체·Windows 재설치 후 암호화 백업을 복구하는 유일한 열쇠입니다.\r\n",
    );
    out.push_str(
        "- 인쇄하거나 별도 USB·안전 금고에 보관하고, 저장소와 같은 드라이브에 두지 마세요.\r\n",
    );
    out.push_str("- 복원 방법: Kova2 > 보호 및 시스템 관리 > 상세 엔지니어링 설정 > '복구 코드로 마스터 키 복원'\r\n");
    out.push_str("\r\n");
    out.push_str("------------------------------------------------------------\r\n");
    out.push_str("  [Windows OS C: 드라이브 BitLocker 비상 복구 키]\r\n");
    out.push_str("------------------------------------------------------------\r\n");
    if let Some(key) = bitlocker_key {
        out.push_str(&format!("복구 암호: {key}\r\n"));
        out.push_str("사용 방법:\r\n");
        out.push_str("  - Windows 복구 환경(WinRE) 또는 WinPE 비상 부팅 명령 프롬프트에서:\r\n");
        out.push_str(&format!(
            "    manage-bde -unlock C: -RecoveryPassword {key}\r\n"
        ));
    } else {
        out.push_str("상태: BitLocker 미사용 또는 숫자 복구 암호 없음\r\n");
    }
    out.push_str("\r\n");
    out.push_str("------------------------------------------------------------\r\n");
    out.push_str("  [Kova2 백업 저장소 암호화 복구 코드]\r\n");
    out.push_str("------------------------------------------------------------\r\n");
    for (i, (name, repo, code)) in entries.iter().enumerate() {
        out.push_str(&format!(
            "--- [{}/{}] {} ---\r\n",
            i + 1,
            entries.len(),
            name
        ));
        out.push_str(&format!("저장소 경로: {repo}\r\n"));
        out.push_str(&format!("복구 코드  : {code}\r\n\r\n"));
    }
    out
}

/// 복구 키트 문서를 파일로 저장한다.
pub fn write_recovery_kit(path: &Path, entries: &[(String, String, String)]) -> Result<(), String> {
    if entries.is_empty() {
        return Err("내보낼 암호화 저장소가 없습니다".into());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("폴더 생성 실패: {e}"))?;
    }
    let body = format_recovery_kit(entries);
    fs::write(path, body).map_err(|e| format!("복구 키트 저장 실패: {e}"))
}

/// 엔진이 암호화 저장소를 열 때 쓸 키 제공자를 등록한다(여러 번 호출해도 된다).
pub fn install_provider() {
    codec::set_provider(Arc::new(|repo: &Path| {
        let key = load_key(repo)?;
        Ok(Arc::new(EngineCodec(CryptoEngine::new(&key))) as Arc<dyn BlobCodec>)
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_engine::ops::{self, BackupOptions, RestoreOptions};
    use std::sync::atomic::AtomicBool;

    fn backup(src: &Path, repo: &Path) {
        let opts = BackupOptions {
            sources: vec![src.to_path_buf()],
            repo: repo.to_path_buf(),
            session_id: Some("20260101_000000".into()),
            zstd_level: 3,
            exclude: vec![],
            profile_id: None,
            scan_from: None,
        };
        ops::run_backup(&opts, &|_| {}, &AtomicBool::new(false)).unwrap();
    }

    fn restore(repo: &Path, to: &Path) {
        let ro = RestoreOptions {
            repo: repo.to_path_buf(),
            session_id: "20260101_000000".into(),
            target: Some(to.to_path_buf()),
            only: None,
        };
        ops::run_restore(&ro, &|_| {}, &AtomicBool::new(false)).unwrap();
    }

    fn walk(p: &Path) -> Vec<PathBuf> {
        let mut v = Vec::new();
        for e in fs::read_dir(p).unwrap().flatten() {
            let q = e.path();
            if q.is_dir() {
                v.extend(walk(&q));
            } else {
                v.push(q);
            }
        }
        v
    }

    fn find_restored(root: &Path, name: &str) -> String {
        let f = walk(root)
            .into_iter()
            .find(|p| p.file_name().is_some_and(|n| n == name))
            .expect("복원된 파일");
        fs::read_to_string(f).unwrap()
    }

    #[test]
    fn encrypted_repo_roundtrips_hides_plaintext_and_fails_closed_without_key() {
        let _g = TEST_KEY_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let keys = tempfile::tempdir().unwrap();
        std::env::set_var("KOVA2_KEY_DIR", keys.path());
        install_provider();
        let src = tempfile::tempdir().unwrap();
        let repo = tempfile::tempdir().unwrap();
        let secret = "SUPER-SECRET-PAYLOAD-0123456789".repeat(40);
        fs::write(src.path().join("a.txt"), &secret).unwrap();
        let code = enable(repo.path()).unwrap().expect("새로 켜짐");
        assert_eq!(code.len(), 64 + 7);
        assert_eq!(export_recovery_code(repo.path()).unwrap(), code);
        assert!(enable(repo.path()).unwrap().is_none());
        backup(src.path(), repo.path());
        // 저장소 어디에도 평문이 없어야 한다
        let needle = &secret.as_bytes()[..20];
        let mut found_blob = false;
        for e in walk(repo.path()) {
            let b = fs::read(&e).unwrap();
            assert!(!b.windows(20).any(|w| w == needle), "{e:?}");
            // 백업은 새 블롭을 팩에 이어 쓰므로 팩의 첫 블롭(맨 앞)도 같은 머리표를 가진다
            if e.extension().is_some_and(|x| x == "blob" || x == "pack") {
                assert_eq!(&b[..4], b"ENC\x01");
                found_blob = true;
            }
        }
        assert!(found_blob);
        let out = tempfile::tempdir().unwrap();
        restore(repo.path(), out.path());
        assert_eq!(find_restored(out.path(), "a.txt"), secret);
        // 키 파일이 없으면 평문으로 가지 않고 열기부터 실패
        for f in fs::read_dir(keys.path()).unwrap() {
            fs::remove_file(f.unwrap().path()).unwrap();
        }
        assert!(kova_engine::storage::CasStorage::new(repo.path()).is_err());
        // 복구 코드로 복원하면 다시 열린다
        assert!(restore_key(repo.path(), "00112233").is_err());
        restore_key(repo.path(), &code).unwrap();
        let out2 = tempfile::tempdir().unwrap();
        restore(repo.path(), out2.path());
        assert_eq!(find_restored(out2.path(), "a.txt"), secret);
        // 변조된 블롭은 깊은 검사에서 걸린다
        let blob = walk(repo.path())
            .into_iter()
            .find(|p| p.extension().is_some_and(|x| x == "blob" || x == "pack"))
            .unwrap();
        let mut b = fs::read(&blob).unwrap();
        let n = b.len();
        b[n - 1] ^= 1;
        let mut perm = fs::metadata(&blob).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        perm.set_readonly(false);
        fs::set_permissions(&blob, perm).unwrap();
        fs::write(&blob, b).unwrap();
        let rep = crate::verify::verify_repo(repo.path(), true).unwrap();
        assert!(!rep.problems.is_empty());
    }

    #[test]
    fn enable_is_atomic_and_refuses_when_locked_or_encrypted_blobs_lack_marker() {
        let _g = TEST_KEY_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let keys = tempfile::tempdir().unwrap();
        std::env::set_var("KOVA2_KEY_DIR", keys.path());
        let repo = tempfile::tempdir().unwrap();
        // 다른 작업이 저장소를 쓰는 중이면 거부
        let l = crate::lock::RepoLock::try_acquire(repo.path()).unwrap();
        assert!(enable(repo.path()).unwrap_err().contains("작업"));
        assert!(!is_enabled(repo.path()));
        drop(l);
        // 표식 없이 암호화 블롭이 있으면(표식 유실) 새 키를 만들지 않는다
        let b = repo.path().join("blobs").join("ab");
        fs::create_dir_all(&b).unwrap();
        fs::write(b.join("ab01.blob"), b"ENC\x01payload").unwrap();
        assert!(enable(repo.path()).unwrap_err().contains(MARKER));
        assert!(!is_enabled(repo.path()));
        assert_eq!(fs::read_dir(keys.path()).unwrap().count(), 0);
        // 평문 블롭만 있으면 켜지고, 임시 파일이 남지 않는다
        fs::write(b.join("ab01.blob"), b"plain").unwrap();
        assert!(enable(repo.path()).unwrap().is_some());
        assert!(read_marker(repo.path()).is_ok());
        assert!(!fs::read_dir(repo.path())
            .unwrap()
            .flatten()
            .any(|e| e.file_name().to_string_lossy().ends_with(".tmp")));
    }

    #[test]
    fn recovery_kit_writes_formatted_entries() {
        let d = tempfile::tempdir().unwrap();
        let out = d.path().join("kit.txt");
        assert!(write_recovery_kit(&out, &[]).is_err());
        let entries = vec![(
            "문서 백업".into(),
            "D:\\KovaRepo".into(),
            "01234567-89abcdef".into(),
        )];
        write_recovery_kit(&out, &entries).unwrap();
        let body = fs::read_to_string(&out).unwrap();
        assert!(body.contains("Recovery Kit"));
        assert!(body.contains("문서 백업"));
        assert!(body.contains("01234567-89abcdef"));
        assert!(body.contains("BitLocker"));
    }

    #[test]
    fn recovery_kit_includes_bitlocker_key_when_provided() {
        let entries = vec![(
            "시스템 백업".into(),
            "D:\\Repo".into(),
            "11223344-55667788".into(),
        )];
        let fake_bl = "123456-234567-345678-456789-567890-678901-789012-890123";
        let body = format_recovery_kit_with_bitlocker(&entries, Some(fake_bl));
        assert!(body.contains(fake_bl));
        assert!(body.contains("manage-bde -unlock C: -RecoveryPassword"));
    }
}
