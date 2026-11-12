#[tokio::main]
async fn main() {
    let ev = kova_ipc::request(kova_ipc::PIPE_NAME, &kova_ipc::Request::Status).await.unwrap();
    println!("{:?}", ev);
}
