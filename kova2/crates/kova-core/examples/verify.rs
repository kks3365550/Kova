//! 사용법: verify <저장소 경로> [--deep]
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let repo = std::path::PathBuf::from(&args[1]);
    let deep = args.iter().any(|a| a == "--deep");
    let r = kova_core::verify::verify_repo(&repo, deep).expect("verify failed");
    println!(
        "snapshots={} files={} unique_chunks={} problems={} deep={}",
        r.snapshots, r.files, r.unique_chunks, r.problems.len(), deep
    );
    for p in &r.problems {
        println!("{:?}", p);
    }
    std::process::exit(if r.ok() { 0 } else { 1 });
}
