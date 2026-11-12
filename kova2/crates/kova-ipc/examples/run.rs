//! 사용: run <profile_id> <backup|verify|gc> — 작업을 요청하고 끝날 때까지 이벤트를 출력한다(통합 검증용).
use kova_ipc::{Event, Request, PIPE_NAME};

#[tokio::main]
async fn main() {
    let mut a = std::env::args().skip(1);
    let (id, kind) = (a.next().expect("profile id"), a.next().expect("kind"));
    // 먼저 구독 연결을 열어 두고, 같은 순간 요청을 보낸다
    let sub = tokio::spawn(async move {
        let mut ok = false;
        kova_ipc::stream(PIPE_NAME, &Request::Subscribe, |e| {
            println!("{e:?}");
            if let Event::JobFinished { ok: o, .. } = &e {
                ok = *o;
                return false;
            }
            true
        })
        .await
        .unwrap();
        ok
    });
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    println!(
        "{:?}",
        kova_ipc::request(
            PIPE_NAME,
            &Request::RunNow {
                profile_id: id,
                kind
            }
        )
        .await
        .unwrap()
    );
    let ok = sub.await.unwrap();
    std::process::exit(if ok { 0 } else { 1 });
}
