//! 마스터 비밀번호(앱 잠금): 설정 변경·프로파일 삭제·스냅샷 삭제·제자리 복원 같은 되돌리기 어려운 동작을
//! 비밀번호로 잠근다. 비밀번호 자체는 저장하지 않고 scrypt 해시만 설정 폴더의 `applock.json` 에 둔다.
//! 백업 실행·복원(새 위치)·검증은 잠겨 있어도 된다. 이 잠금은 같은 PC의 다른 사용자가 실수로 건드리는 것을
//! 막는 용도이며, 파일을 직접 지울 수 있는 관리자까지 막지는 못한다.
use crate::crypto::{derive_key, hex, random_bytes, unhex};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

const N: u32 = 16384;
const R: u32 = 8;
const P: u32 = 1;
/// 너무 짧은 비밀번호는 받지 않는다.
pub const MIN_LEN: usize = 4;

#[derive(Serialize, Deserialize)]
struct Lock {
    salt: String,
    hash: String,
}

fn file(dir: &Path) -> PathBuf {
    dir.join("applock.json")
}

fn read(dir: &Path) -> Option<Lock> {
    serde_json::from_str(&fs::read_to_string(file(dir)).ok()?).ok()
}

fn digest(pw: &str, salt: &[u8]) -> Option<String> {
    derive_key(pw, salt, N, R, P).ok().map(|k| hex(&k))
}

/// 같은 길이의 문자열을 시간 차이 없이 비교한다.
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len() && a.bytes().zip(b.bytes()).fold(0u8, |d, (x, y)| d | (x ^ y)) == 0
}

/// 마스터 비밀번호가 설정돼 있는가. 파일이 있는데 읽을 수 없으면(깨짐) 안전하게 "설정됨"으로 본다.
pub fn is_set(dir: &Path) -> bool {
    file(dir).exists()
}

pub fn verify(dir: &Path, pw: &str) -> bool {
    let Some(l) = read(dir) else { return false };
    let Some(salt) = unhex(&l.salt) else {
        return false;
    };
    digest(pw, &salt).is_some_and(|h| same(&h, &l.hash))
}

/// 비밀번호를 새로 정하거나 바꾼다(이미 있으면 현재 비밀번호가 맞아야 한다). `new` 가 비어 있으면 해제한다.
pub fn change(dir: &Path, current: &str, new: &str) -> Result<String, String> {
    if is_set(dir) && !verify(dir, current) {
        return Err("현재 마스터 비밀번호가 맞지 않습니다".into());
    }
    if new.is_empty() {
        return match fs::remove_file(file(dir)) {
            Ok(()) => Ok("마스터 비밀번호를 해제했습니다".into()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Ok("설정된 마스터 비밀번호가 없습니다".into())
            }
            Err(e) => Err(format!("해제하지 못했습니다: {e}")),
        };
    }
    if new.chars().count() < MIN_LEN {
        return Err(format!("비밀번호는 {MIN_LEN}자 이상이어야 합니다"));
    }
    let salt: [u8; 16] = random_bytes();
    let hash = digest(new, &salt).ok_or("비밀번호 처리에 실패했습니다")?;
    let lock = Lock {
        salt: hex(&salt),
        hash,
    };
    fs::create_dir_all(dir).map_err(|e| format!("저장하지 못했습니다: {e}"))?;
    let tmp = file(dir).with_extension("json.tmp");
    let text = serde_json::to_string(&lock).map_err(|e| e.to_string())?;
    fs::write(&tmp, text)
        .and_then(|_| fs::rename(&tmp, file(dir)))
        .map_err(|e| format!("저장하지 못했습니다: {e}"))?;
    Ok("마스터 비밀번호를 설정했습니다. 잊으면 이 PC에서 풀 수 없으니 따로 적어 두세요".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_verify_change_and_clear() {
        let d = tempfile::tempdir().unwrap();
        assert!(!is_set(d.path()));
        assert!(!verify(d.path(), "anything"));
        change(d.path(), "", "abcd1234").unwrap();
        assert!(is_set(d.path()));
        assert!(verify(d.path(), "abcd1234"));
        assert!(!verify(d.path(), "abcd1235"));
        assert!(!verify(d.path(), ""));
        // 현재 비밀번호 없이는 바꾸거나 해제할 수 없다
        assert!(change(d.path(), "wrong", "newpass1").is_err());
        assert!(change(d.path(), "", "").is_err());
        assert!(verify(d.path(), "abcd1234"));
        change(d.path(), "abcd1234", "newpass1").unwrap();
        assert!(verify(d.path(), "newpass1") && !verify(d.path(), "abcd1234"));
        change(d.path(), "newpass1", "").unwrap();
        assert!(!is_set(d.path()));
    }

    #[test]
    fn short_password_is_rejected_and_file_has_no_plaintext() {
        let d = tempfile::tempdir().unwrap();
        assert!(change(d.path(), "", "abc").is_err());
        assert!(!is_set(d.path()));
        change(d.path(), "", "secret-한글-pw").unwrap();
        let text = fs::read_to_string(file(d.path())).unwrap();
        assert!(!text.contains("secret"));
        assert!(verify(d.path(), "secret-한글-pw"));
    }

    #[test]
    fn corrupted_file_stays_locked() {
        let d = tempfile::tempdir().unwrap();
        fs::write(file(d.path()), "not json").unwrap();
        assert!(is_set(d.path()));
        assert!(!verify(d.path(), ""));
        assert!(!verify(d.path(), "anything"));
    }
}
