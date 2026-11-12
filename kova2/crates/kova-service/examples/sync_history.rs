//! 시험용: 저장소의 snapshots 폴더를 이력 DB(metadata.db)에 반영한다. 사용: sync_history <repo>
fn main() {
    let repo = std::env::args().nth(1).expect("sync_history <repo>");
    let mut h = kova_core::history::History::open(std::path::Path::new(&repo)).unwrap();
    let r = h.sync_snapshots().unwrap();
    println!("{r:?}");
}
