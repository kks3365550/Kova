use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

pub fn hash_bytes(data: &[u8]) -> String {
    let hash = blake3::hash(data);
    hash.to_hex().to_string()
}

pub fn hash_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = blake3::Hasher::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
    }
    Ok(hasher.finalize().to_hex().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn hash_is_deterministic_and_input_sensitive() {
        assert_eq!(hash_bytes(b"kova"), hash_bytes(b"kova"));
        assert_ne!(hash_bytes(b"kova"), hash_bytes(b"kovA"));
        assert_eq!(hash_bytes(b"kova").len(), 64);
    }

    #[test]
    fn hash_file_matches_hash_bytes() {
        let path = std::env::temp_dir().join(format!("kova_hasher_test_{}.bin", std::process::id()));
        let data: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        std::fs::File::create(&path).unwrap().write_all(&data).unwrap();
        let from_file = hash_file(&path).unwrap();
        std::fs::remove_file(&path).ok();
        assert_eq!(from_file, hash_bytes(&data));
    }
}
