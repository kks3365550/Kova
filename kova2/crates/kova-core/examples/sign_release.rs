//! 릴리즈 패키지 서명 도구.
//!   sign_release keygen <private.pem 출력> <public.pem 출력>   (새 키쌍. 개인키는 저장소 밖에 보관할 것)
//!   sign_release sign <package.zip> <private.pem> <tag>         (<package.zip>.sig(옛 설치본 호환) 와 태그에 묶인 .sig2 에 hex 서명 저장)
//!   sign_release verify <package.zip> <public.pem> [tag]        (태그를 주면 .sig2·.sig 둘 다, 없으면 .sig 만 검증. 통과하면 종료 코드 0)
use std::fs;
use std::process::exit;

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let usage = || -> ! {
        eprintln!("usage: sign_release keygen <priv_out> <pub_out> | sign <zip> <priv> <tag> | verify <zip> <pub> [tag]");
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
        ["sign", zip, key, tag] => {
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
            let sig2 = kova_core::update::sign_package_v2(&data, tag, &pem).unwrap_or_else(|e| {
                eprintln!("{e}");
                exit(1)
            });
            fs::write(format!("{zip}.sig"), &sig).unwrap();
            fs::write(format!("{zip}.sig2"), &sig2).unwrap();
            println!("signed: {zip}.sig, {zip}.sig2 (tag {tag})");
        }
        ["verify", zip, pubkey, rest @ ..] if rest.len() <= 1 => {
            let data = fs::read(zip).unwrap();
            let sig = fs::read_to_string(format!("{zip}.sig")).unwrap();
            let pem = fs::read_to_string(pubkey).unwrap();
            let r = kova_core::update::verify_package(&data, "", sig.trim(), &[pem.as_str()]);
            match r {
                Ok(()) => println!("signature OK: {zip}.sig"),
                Err(e) => {
                    eprintln!("signature FAIL: {zip}.sig: {e}");
                    exit(1)
                }
            }
            if let [tag] = rest {
                let sig2 = fs::read_to_string(format!("{zip}.sig2")).unwrap_or_else(|e| {
                    eprintln!("read {zip}.sig2: {e}");
                    exit(1)
                });
                let r =
                    kova_core::update::verify_package_v2(&data, tag, sig2.trim(), &[pem.as_str()]);
                match r {
                    Ok(()) => println!("signature OK: {zip}.sig2 (tag {tag})"),
                    Err(e) => {
                        eprintln!("signature FAIL: {zip}.sig2 (tag {tag}): {e}");
                        exit(1)
                    }
                }
            }
        }
        _ => usage(),
    }
}
