//! 릴리즈 패키지 서명 도구.
//!   sign_release keygen <private.pem 출력> <public.pem 출력>   (새 키쌍. 개인키는 저장소 밖에 보관할 것)
//!   sign_release sign <package.zip> <private.pem>               (<package.zip>.sig 에 hex 서명 저장)
//!   sign_release verify <package.zip> <public.pem>              (서명 파일로 검증, 통과하면 종료 코드 0)
use std::fs;
use std::process::exit;

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let usage = || -> ! {
        eprintln!("usage: sign_release keygen <priv_out> <pub_out> | sign <zip> <priv> | verify <zip> <pub>");
        exit(2)
    };
    match a.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["keygen", priv_out, pub_out] => {
            for p in [priv_out, pub_out] {
                if std::path::Path::new(p).exists() {
                    eprintln!("already exists, refusing to overwrite: {p}");
                    exit(1);
                }
            }
            let (sk, pk) = kova_core::sign::generate_key_pair().unwrap_or_else(|e| {
                eprintln!("{e}");
                exit(1)
            });
            fs::write(priv_out, sk).unwrap();
            fs::write(pub_out, pk).unwrap();
            println!("keys written (keep the private key outside the repository)");
        }
        ["sign", zip, key] => {
            let data = fs::read(zip).unwrap_or_else(|e| {
                eprintln!("read zip: {e}");
                exit(1)
            });
            let pem = fs::read_to_string(key).unwrap_or_else(|e| {
                eprintln!("read key: {e}");
                exit(1)
            });
            let sig = kova_core::sign::sign_bytes(&data, &pem).unwrap_or_else(|e| {
                eprintln!("{e}");
                exit(1)
            });
            fs::write(format!("{zip}.sig"), &sig).unwrap();
            println!("signed: {zip}.sig");
        }
        ["verify", zip, pubkey] => {
            let data = fs::read(zip).unwrap();
            let sig = fs::read_to_string(format!("{zip}.sig")).unwrap();
            let pem = fs::read_to_string(pubkey).unwrap();
            let r = kova_core::update::verify_package(&data, "", sig.trim(), &[pem.as_str()]);
            match r {
                Ok(()) => println!("signature OK"),
                Err(e) => {
                    eprintln!("signature FAIL: {e}");
                    exit(1)
                }
            }
        }
        _ => usage(),
    }
}
