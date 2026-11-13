//! 팩 컨테이너: 작은 블롭 여러 개를 파일 하나(`packs/<id>.pack`)로 이어 붙이고, 어디에 무엇이 있는지는
//! 색인(`packs/<id>.idx`, 줄마다 `해시 오프셋 길이`)에 적는다. 팩은 한 번 쓰면 다시 고치지 않는다.
//! 색인이 있어야 팩이 유효하다: 팩을 먼저 쓰고 색인을 나중에 이름 바꾸기로 놓으므로, 중간에 끊겨도
//! 색인 없는 팩(고아)이 남을 뿐 저장소가 깨지지 않는다.
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackEntry {
    pub pack: String,
    pub offset: u64,
    pub len: u64,
}

pub fn packs_dir(repo: &Path) -> PathBuf {
    repo.join("packs")
}

pub fn pack_file(repo: &Path, id: &str) -> PathBuf {
    packs_dir(repo).join(format!("{id}.pack"))
}

pub fn idx_file(repo: &Path, id: &str) -> PathBuf {
    packs_dir(repo).join(format!("{id}.idx"))
}

/// 색인 한 개를 읽는다. 팩 파일이 없거나 색인이 가리키는 범위보다 짧으면 None(그 팩은 쓰지 않는다).
pub fn read_idx(repo: &Path, id: &str) -> Option<Vec<(String, PackEntry)>> {
    let text = fs::read_to_string(idx_file(repo, id)).ok()?;
    let pack_len = fs::metadata(pack_file(repo, id)).ok()?.len();
    let mut out = Vec::new();
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let mut it = line.split_whitespace();
        let (h, o, l) = (it.next()?, it.next()?, it.next()?);
        let (offset, len): (u64, u64) = (o.parse().ok()?, l.parse().ok()?);
        if offset.checked_add(len)? > pack_len {
            return None;
        }
        out.push((
            h.to_string(),
            PackEntry {
                pack: id.to_string(),
                offset,
                len,
            },
        ));
    }
    Some(out)
}

/// 저장소의 모든 유효한 팩 id.
pub fn pack_ids(repo: &Path) -> Vec<String> {
    let mut ids: Vec<String> = fs::read_dir(packs_dir(repo))
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            (p.extension().is_some_and(|x| x == "idx"))
                .then(|| p.file_stem().map(|s| s.to_string_lossy().to_string()))
                .flatten()
        })
        .collect();
    ids.sort();
    ids
}

/// 해시 → 위치 전체 색인. 같은 해시가 여러 팩에 있으면 먼저 읽힌 쪽을 쓴다.
pub fn load_index(repo: &Path) -> HashMap<String, PackEntry> {
    let mut map = HashMap::new();
    for id in pack_ids(repo) {
        if let Some(entries) = read_idx(repo, &id) {
            for (h, e) in entries {
                map.entry(h).or_insert(e);
            }
        }
    }
    map
}

pub fn read_entry(repo: &Path, e: &PackEntry) -> io::Result<Vec<u8>> {
    let mut f = File::open(pack_file(repo, &e.pack))?;
    f.seek(SeekFrom::Start(e.offset))?;
    let mut buf = vec![0u8; e.len as usize];
    f.read_exact(&mut buf)?;
    Ok(buf)
}

static SEQ: AtomicU64 = AtomicU64::new(0);

fn new_id() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!(
        "p{nanos:x}{:x}{:x}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::Relaxed)
    )
}

/// 블롭들을 팩 하나로 쓴다(저장 형식 그대로, 다시 압축하거나 풀지 않는다). 새 팩 id 를 돌려준다.
pub fn write_pack(repo: &Path, items: &[(String, Vec<u8>)]) -> io::Result<String> {
    if items.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "빈 팩"));
    }
    let dir = packs_dir(repo);
    fs::create_dir_all(&dir)?;
    let id = new_id();
    let pack_tmp = dir.join(format!("{id}.pack.tmp"));
    let idx_tmp = dir.join(format!("{id}.idx.tmp"));
    let mut idx = String::new();
    {
        let mut f = File::create(&pack_tmp)?;
        let mut off = 0u64;
        for (h, data) in items {
            f.write_all(data)?;
            idx.push_str(&format!("{h} {off} {}\n", data.len()));
            off += data.len() as u64;
        }
        f.flush()?;
        f.sync_all()?;
    }
    fs::rename(&pack_tmp, pack_file(repo, &id))?;
    {
        let mut f = File::create(&idx_tmp)?;
        f.write_all(idx.as_bytes())?;
        f.flush()?;
        f.sync_all()?;
    }
    fs::rename(&idx_tmp, idx_file(repo, &id))?;
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn items() -> Vec<(String, Vec<u8>)> {
        vec![
            ("aa01".into(), b"hello".to_vec()),
            ("bb02".into(), b"world!!".to_vec()),
            ("cc03".into(), vec![7u8; 1000]),
        ]
    }

    #[test]
    fn write_then_read_roundtrips_every_blob() {
        let d = tempfile::tempdir().unwrap();
        let id = write_pack(d.path(), &items()).unwrap();
        let idx = load_index(d.path());
        assert_eq!(idx.len(), 3);
        for (h, data) in items() {
            let e = &idx[&h];
            assert_eq!(e.pack, id);
            assert_eq!(read_entry(d.path(), e).unwrap(), data);
        }
    }

    #[test]
    fn pack_without_index_or_truncated_pack_is_ignored() {
        let d = tempfile::tempdir().unwrap();
        let id = write_pack(d.path(), &items()).unwrap();
        // 팩이 잘리면 그 팩 전체를 쓰지 않는다(잘못된 바이트를 돌려주느니 없는 편이 낫다)
        let p = pack_file(d.path(), &id);
        let bytes = fs::read(&p).unwrap();
        fs::write(&p, &bytes[..bytes.len() - 10]).unwrap();
        assert!(load_index(d.path()).is_empty());
        // 색인이 없는 고아 팩도 무시
        fs::write(&p, &bytes).unwrap();
        fs::remove_file(idx_file(d.path(), &id)).unwrap();
        assert!(load_index(d.path()).is_empty());
    }

    #[test]
    fn empty_pack_is_rejected_and_missing_dir_is_empty_index() {
        let d = tempfile::tempdir().unwrap();
        assert!(load_index(d.path()).is_empty());
        assert!(write_pack(d.path(), &[]).is_err());
    }
}
