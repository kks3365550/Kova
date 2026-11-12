fn main() {
    slint_build::compile("ui/main.slint").unwrap();
    // 탐색기·바로가기·작업 표시줄에 보이는 exe 아이콘
    #[cfg(windows)]
    {
        println!("cargo:rerun-if-changed=assets/kova.ico");
        let mut res = winresource::WindowsResource::new();
        res.set_icon("assets/kova.ico");
        res.compile().unwrap();
    }
}
