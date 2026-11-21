//! 블롭 코덱: 저장소에 `crypto.json` 표식이 있으면 등록된 제공자가 만든 코덱(암호화)으로 읽고 쓴다.
//! 표식이 있는데 제공자가 없거나 키를 못 얻으면 평문으로 쓰지 않고 실패한다(fail closed).
use std::io::Read;
use std::path::Path;
use std::sync::{Arc, RwLock};

pub const MARKER: &str = "crypto.json";

/// 암호화 블롭 머리표(kova-core crypto.rs 의 MAGIC_V1 과 같다).
const ENC_MAGIC: &[u8; 4] = b"ENC\x01";

fn starts_with_enc(path: &Path) -> bool {
    let mut head = [0u8; 4];
    std::fs::File::open(path)
        .and_then(|mut f| f.read_exact(&mut head))
        .is_ok()
        && &head == ENC_MAGIC
}

/// 표본 검사: 낱개 블롭 몇 개와 첫·마지막 팩(팩의 첫 블롭)의 머리표를 본다.
fn has_encrypted_blob(repo: &Path) -> bool {
    let ids = crate::pack::pack_ids(repo);
    if ids
        .first()
        .into_iter()
        .chain(ids.last())
        .any(|id| starts_with_enc(&crate::pack::pack_file(repo, id)))
    {
        return true;
    }
    let Ok(rd) = std::fs::read_dir(repo.join("blobs")) else {
        return false;
    };
    for d in rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .take(4)
    {
        let Ok(files) = std::fs::read_dir(&d) else {
            continue;
        };
        let mut blobs = files
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "blob"));
        if let Some(b) = blobs.next() {
            if starts_with_enc(&b) {
                return true;
            }
        }
    }
    false
}

pub trait BlobCodec: Send + Sync {
    /// 평문 청크 → 저장 형식. `hash` 는 평문 SHA-256(중복제거 키).
    fn encode(&self, plain: &[u8], hash: &str, level: i32) -> Result<Vec<u8>, String>;
    /// 저장 형식 → 평문(해시 재검증 포함).
    fn decode(&self, blob: &[u8], hash: &str) -> Result<Vec<u8>, String>;
}

pub type Provider = Arc<dyn Fn(&Path) -> Result<Arc<dyn BlobCodec>, String> + Send + Sync>;

static PROVIDER: RwLock<Option<Provider>> = RwLock::new(None);

pub fn set_provider(p: Provider) {
    *PROVIDER.write().unwrap() = Some(p);
}

/// 저장소가 암호화 저장소면 코덱을, 아니면 None 을 돌려준다.
pub fn resolve(repo: &Path) -> Result<Option<Arc<dyn BlobCodec>>, String> {
    if !repo.join(MARKER).is_file() {
        // 표식이 사라진 암호화 저장소(예: 표식 없이 복제된 사본)에 평문 블롭을 섞어 쓰지 않도록 거부한다
        if has_encrypted_blob(repo) {
            return Err(format!(
                "암호화된 블롭이 있는데 암호화 표식({MARKER})이 없습니다. 저장소의 {MARKER} 를 복구하세요"
            ));
        }
        return Ok(None);
    }
    let p = PROVIDER.read().unwrap().clone();
    match p {
        Some(f) => f(repo).map(Some),
        None => Err("암호화된 저장소인데 키 제공자가 등록되지 않았습니다".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 표식(crypto.json)이 없는데 암호화 블롭이 있으면 평문 저장소로 열지 않는다.
    #[test]
    fn missing_marker_with_encrypted_blobs_is_rejected() {
        let d = tempfile::tempdir().unwrap();
        assert!(resolve(d.path()).unwrap().is_none());
        crate::pack::write_pack(d.path(), &[("aa01".into(), b"plain-zstd".to_vec())]).unwrap();
        assert!(resolve(d.path()).unwrap().is_none());
        crate::pack::write_pack(d.path(), &[("bb02".into(), b"ENC\x01nonce...".to_vec())]).unwrap();
        assert!(resolve(d.path()).is_err());
        assert!(crate::storage::CasStorage::new(d.path()).is_err());

        let d2 = tempfile::tempdir().unwrap();
        let dir = d2.path().join("blobs").join("cc");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("cc03.blob"), b"ENC\x01xxxx").unwrap();
        assert!(resolve(d2.path()).is_err());
    }
}
