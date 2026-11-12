//! 블롭 코덱: 저장소에 `crypto.json` 표식이 있으면 등록된 제공자가 만든 코덱(암호화)으로 읽고 쓴다.
//! 표식이 있는데 제공자가 없거나 키를 못 얻으면 평문으로 쓰지 않고 실패한다(fail closed).
use std::path::Path;
use std::sync::{Arc, RwLock};

pub const MARKER: &str = "crypto.json";

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
        return Ok(None);
    }
    let p = PROVIDER.read().unwrap().clone();
    match p {
        Some(f) => f(repo).map(Some),
        None => Err("암호화된 저장소인데 키 제공자가 등록되지 않았습니다".into()),
    }
}
