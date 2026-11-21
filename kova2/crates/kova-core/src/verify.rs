//! 저장소 검증: 매니페스트가 가리키는 모든 청크가 존재하고(빠른 검사),
//! 필요하면 압축을 풀어 BLAKE3 해시와 길이가 일치하는지(전체 검사) 확인한다.
use kova_engine::{
    hasher,
    storage::{CasStorage, SnapshotManifest},
};
use rayon::prelude::*;
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub enum Problem {
    MissingBlob {
        snapshot: String,
        path: String,
        hash: String,
    },
    CorruptBlob {
        hash: String,
        reason: String,
    },
    BadManifest {
        file: String,
        reason: String,
    },
}

#[derive(Debug, Default)]
pub struct VerifyReport {
    pub snapshots: usize,
    pub files: usize,
    pub unique_chunks: usize,
    pub problems: Vec<Problem>,
}

impl VerifyReport {
    pub fn ok(&self) -> bool {
        self.problems.is_empty()
    }
}

/// deep=false: 청크 파일 존재만 확인. deep=true: 전부 읽어 압축 해제 + 해시 + 길이 확인.
pub fn verify_repo(repo: &Path, deep: bool) -> io::Result<VerifyReport> {
    let cas = CasStorage::new(repo)?;
    let mut rep = VerifyReport::default();
    // 해시 -> (기대 길이(0 이면 미상), 구형 blob_id/sha256 여부). 같은 청크는 한 번만 검사한다.
    let mut unique: BTreeMap<String, (usize, bool)> = BTreeMap::new();
    let dir = repo.join("snapshots");
    for e in fs::read_dir(&dir)? {
        let p = e?.path();
        if p.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let name = p.file_name().unwrap().to_string_lossy().to_string();
        let manifest: SnapshotManifest = match fs::File::open(&p)
            .map_err(|e| e.to_string())
            .and_then(|f| serde_json::from_reader(io::BufReader::new(f)).map_err(|e| e.to_string()))
        {
            Ok(m) => m,
            Err(reason) => {
                rep.problems
                    .push(Problem::BadManifest { file: name, reason });
                continue;
            }
        };
        rep.snapshots += 1;
        let sid = manifest.session_id();
        for fe in manifest.files() {
            rep.files += 1;
            // 청크 정보가 없는 구형 항목은 blob_id/sha256 이 가리키는 블롭(파일 전체)을 검사한다
            if fe.chunks.is_empty() && !fe.is_dir {
                if let Some(id) = fe.blob_id.clone().or(fe.sha256.clone()) {
                    let found = [fe.blob_id.as_ref(), fe.sha256.as_ref()]
                        .into_iter()
                        .flatten()
                        .find(|h| cas.has_blob(h))
                        .cloned();
                    match found {
                        Some(h) => {
                            unique.entry(h).or_insert((fe.size as usize, true));
                        }
                        None => rep.problems.push(Problem::MissingBlob {
                            snapshot: sid.clone(),
                            path: fe.path.clone(),
                            hash: id,
                        }),
                    }
                }
            }
            for c in fe.chunks {
                if !cas.has_blob(&c.hash) {
                    rep.problems.push(Problem::MissingBlob {
                        snapshot: sid.clone(),
                        path: fe.path.clone(),
                        hash: c.hash.clone(),
                    });
                }
                unique.entry(c.hash).or_insert((c.length, false));
            }
        }
    }
    rep.unique_chunks = unique.len();
    if deep {
        let bad: Vec<Problem> = unique
            .par_iter()
            .filter_map(|(hash, (len, legacy))| {
                let comp = match cas.read_blob(hash) {
                    Ok(c) => c,
                    // 있다고 나오는데 읽지 못하면(팩 읽기 IO 오류·잘린 팩 등) 손상으로 보고한다
                    Err(e) if cas.has_blob(hash) => {
                        return Some(Problem::CorruptBlob {
                            hash: hash.clone(),
                            reason: format!("읽기 실패: {}", e),
                        })
                    }
                    Err(_) => return None, // 없는 청크는 위에서 이미 MissingBlob 으로 보고됨
                };
                let raw = match cas.decode_blob(&comp, hash) {
                    Ok(r) => r,
                    Err(e) => {
                        return Some(Problem::CorruptBlob {
                            hash: hash.clone(),
                            reason: format!("압축 해제 실패: {}", e),
                        })
                    }
                };
                if *len != 0 && raw.len() != *len {
                    return Some(Problem::CorruptBlob {
                        hash: hash.clone(),
                        reason: format!("길이 불일치: {} != {}", raw.len(), len),
                    });
                }
                // 구형 blob_id/sha256 은 해시 종류가 BLAKE3 또는 SHA-256 일 수 있다
                let sha_ok = || {
                    use sha2::Digest;
                    sha2::Sha256::digest(&raw)
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>()
                        .eq_ignore_ascii_case(hash)
                };
                if !hasher::hash_bytes(&raw).eq_ignore_ascii_case(hash) && !(*legacy && sha_ok()) {
                    return Some(Problem::CorruptBlob {
                        hash: hash.clone(),
                        reason: "해시 불일치".into(),
                    });
                }
                None
            })
            .collect();
        rep.problems.extend(bad);
    }
    Ok(rep)
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_engine::compressor;

    /// 청크 하나짜리 저장소를 엔진 라이브러리로 직접 만든다.
    fn make_repo(data: &[u8]) -> (tempfile::TempDir, String) {
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        let hash = hasher::hash_bytes(data);
        cas.save_blob(&hash, &compressor::compress(data, 3).unwrap())
            .unwrap();
        fs::write(
            d.path().join("snapshots").join("s1.json"),
            format!(
                r#"{{"id":"s1","files":[{{"path":"a.txt","size":{},"chunks":[{{"hash":"{}","length":{}}}]}}]}}"#,
                data.len(),
                hash,
                data.len()
            ),
        )
        .unwrap();
        (d, hash)
    }

    #[test]
    fn healthy_repo_passes_deep_verify() {
        let (d, _) = make_repo(b"kova verify test data");
        let r = verify_repo(d.path(), true).unwrap();
        assert!(r.ok(), "{:?}", r.problems);
        assert_eq!((r.snapshots, r.files, r.unique_chunks), (1, 1, 1));
    }

    #[test]
    fn missing_blob_is_reported() {
        let (d, hash) = make_repo(b"data");
        fs::remove_file(
            d.path()
                .join("blobs")
                .join(&hash[..2])
                .join(format!("{}.blob", hash)),
        )
        .unwrap();
        let r = verify_repo(d.path(), false).unwrap();
        assert!(matches!(r.problems[0], Problem::MissingBlob { .. }));
    }

    #[test]
    fn corrupt_blob_is_caught_only_by_deep_verify() {
        let (d, hash) = make_repo(b"original content");
        // 정상 압축본이지만 내용이 다른 데이터로 바꿔치기 (해시 불일치)
        let evil = compressor::compress(b"tampered content", 3).unwrap();
        fs::write(
            d.path()
                .join("blobs")
                .join(&hash[..2])
                .join(format!("{}.blob", hash)),
            evil,
        )
        .unwrap();
        assert!(verify_repo(d.path(), false).unwrap().ok());
        let r = verify_repo(d.path(), true).unwrap();
        assert!(
            matches!(r.problems[0], Problem::CorruptBlob { .. }),
            "{:?}",
            r.problems
        );
    }

    #[test]
    fn garbage_blob_is_caught() {
        let (d, hash) = make_repo(b"original content");
        fs::write(
            d.path()
                .join("blobs")
                .join(&hash[..2])
                .join(format!("{}.blob", hash)),
            b"not zstd",
        )
        .unwrap();
        let r = verify_repo(d.path(), true).unwrap();
        assert!(matches!(r.problems[0], Problem::CorruptBlob { .. }));
    }

    /// 청크 정보 없는 구형 항목은 blob_id/sha256 블롭을 검사한다(없으면 MissingBlob, SHA-256 해시도 인정).
    #[test]
    fn legacy_blob_id_and_sha256_entries_are_checked() {
        use sha2::Digest;
        let d = tempfile::tempdir().unwrap();
        let cas = CasStorage::new(d.path()).unwrap();
        let data = b"legacy whole-file blob";
        let sha: String = sha2::Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        cas.save_blob(&sha, &compressor::compress(data, 3).unwrap())
            .unwrap();
        let missing = "e".repeat(64);
        fs::write(
            d.path().join("snapshots").join("old.json"),
            format!(
                r#"{{"id":"old","files":[{{"path":"a","size":{},"sha256":"{sha}"}},{{"path":"b","size":5,"blob_id":"{missing}"}}]}}"#,
                data.len()
            ),
        )
        .unwrap();
        let r = verify_repo(d.path(), true).unwrap();
        assert_eq!(r.problems.len(), 1, "{:?}", r.problems);
        assert!(
            matches!(&r.problems[0], Problem::MissingBlob { hash, .. } if *hash == missing),
            "{:?}",
            r.problems
        );
    }

    /// 있다고 나오는 블롭을 읽지 못하면(IO 오류) 깊은 검사에서 손상으로 보고한다.
    #[test]
    fn unreadable_blob_is_reported_as_corrupt() {
        let (d, hash) = make_repo(b"data");
        let p = d
            .path()
            .join("blobs")
            .join(&hash[..2])
            .join(format!("{}.blob", hash));
        fs::remove_file(&p).unwrap();
        fs::create_dir_all(&p).unwrap(); // 파일 자리에 폴더 → 열기 실패
        let r = verify_repo(d.path(), true).unwrap();
        assert!(
            r.problems.iter().any(
                |x| matches!(x, Problem::CorruptBlob { reason, .. } if reason.contains("읽기 실패"))
            ),
            "{:?}",
            r.problems
        );
    }

    #[test]
    fn broken_manifest_is_reported() {
        let (d, _) = make_repo(b"data");
        fs::write(d.path().join("snapshots").join("bad.json"), "{{{").unwrap();
        let r = verify_repo(d.path(), false).unwrap();
        assert!(r
            .problems
            .iter()
            .any(|p| matches!(p, Problem::BadManifest { .. })));
    }
}
