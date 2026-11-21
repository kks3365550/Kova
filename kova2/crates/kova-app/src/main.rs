#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
slint::include_modules!();

use tray_icon::menu::{Menu, MenuEvent, MenuItem};
use tray_icon::{Icon, TrayIconBuilder};

fn make_icon() -> Icon {
    // 32x32 RGBA(assets/kova_32.rgba)
    Icon::from_rgba(include_bytes!("../assets/kova_32.rgba").to_vec(), 32, 32).unwrap()
}

/// 시작 마법사용 폴더 위치. 문서·사진 등은 OneDrive로 옮겨져 있으면 그쪽을 쓴다.
fn known_folder(kind: &str) -> String {
    let home = std::env::var_os("USERPROFILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_default();
    let pick = |names: &[&str]| -> String {
        for n in names {
            let p = home.join(n);
            if p.is_dir() {
                return p.to_string_lossy().into_owned();
            }
        }
        home.join(names[names.len() - 1])
            .to_string_lossy()
            .into_owned()
    };
    match kind {
        "documents" => pick(&["OneDrive\\Documents", "OneDrive\\문서", "Documents"]),
        "pictures" => pick(&["OneDrive\\Pictures", "OneDrive\\사진", "Pictures"]),
        "desktop" => pick(&["OneDrive\\Desktop", "OneDrive\\바탕 화면", "Desktop"]),
        "downloads" => pick(&["Downloads"]),
        // 저장 위치 제안: 다른 드라이브가 있으면 그쪽, 없으면 내 폴더 안
        "backup" => {
            for l in 'D'..='Z' {
                let root = format!("{l}:\\");
                if std::path::Path::new(&root).exists() {
                    return format!("{root}Kova백업");
                }
            }
            home.join("Kova백업").to_string_lossy().into_owned()
        }
        "restore" => home
            .join("Desktop")
            .join("Kova 복원")
            .to_string_lossy()
            .into_owned(),
        _ => String::new(),
    }
}

/// 마법사가 새 백업에 넣는 제외 목록: 임시·캐시·개발 부산물은 백업할 필요가 없다.
const DEFAULT_EXCLUDES: &str =
    "*.tmp;*.temp;~$*;Thumbs.db;desktop.ini;$RECYCLE.BIN;node_modules;__pycache__;.venv";

/// 마법사: 이름·보존 기본값으로 프로파일을 만들고 매일 02:00 자동 백업을 켠 뒤 바로 한 번 백업한다.
fn quick_start(srcs: &str, repo: &str) -> Result<String, String> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let all = store.profiles();
    let id = format!(
        "prof_{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let p = kova_core::profile_form::build(None, &id, "내 백업", srcs, repo, "30", "60", &all)
        .and_then(|p| {
            kova_core::profile_form::apply_schedule(p, true, true, "02:00", DEFAULT_EXCLUDES)
        })
        .and_then(|p| kova_core::profile_form::apply_keep(p, "0", "0"))?;
    store
        .upsert(p.clone())
        .map_err(|e| format!("저장 실패: {e}"))?;
    send("backup", p.id.clone());
    Ok(p.id)
}

/// 마법사의 '고른 폴더' 목록을 화면(목록 + 이어 붙인 문자열)에 반영한다.
fn set_wizard_sources(w: &MainWindow, list: &[String]) {
    w.set_wz_src(list.join(";").into());
    let model: Vec<slint::SharedString> = list.iter().map(|s| s.as_str().into()).collect();
    w.set_wz_list(std::rc::Rc::new(slint::VecModel::from(model)).into());
}

/// 현재 고른 폴더 목록(이어 붙인 문자열에서 복원).
fn wizard_sources(w: &MainWindow) -> Vec<String> {
    w.get_wz_src()
        .split(';')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// 프로그램 이름 비교용 키: 글자·숫자만 소문자로, 끝의 'updater'는 뗀다(같은 프로그램의 갱신 폴더).
fn prog_norm(name: &str) -> String {
    let s: String = name
        .chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    match s.strip_suffix("updater") {
        Some(t) if !t.is_empty() => t.to_string(),
        _ => s,
    }
}

/// 경로가 어떤 프로그램의 것이면 (비교 키, 표시 이름), 일반 폴더면 None. 경로 모양만 본다.
fn program_of(path: &str) -> Option<(String, String)> {
    let p = path.trim().trim_end_matches('\\').replace('/', "\\");
    let low = p.to_ascii_lowercase(); // ASCII만 바꾸므로 바이트 위치가 p와 같다
    fn after<'a>(p: &'a str, low: &str, marker: &str) -> Option<&'a str> {
        let i = low.find(marker)? + marker.len();
        p.get(i..)
    }
    let first = |rest: &str| rest.split('\\').next().unwrap_or("").to_string();
    let name = if low.ends_with(".lnk") {
        let stem = |s: &str| s[..s.len() - 4].to_string();
        match after(&p, &low, "\\start menu\\programs\\") {
            Some(rest) if rest.contains('\\') => first(rest),
            Some(rest) => stem(rest),
            None => stem(p.rsplit('\\').next().unwrap_or("")),
        }
    } else if let Some(r) =
        after(&p, &low, "\\program files (x86)\\").or_else(|| after(&p, &low, "\\program files\\"))
    {
        first(r)
    } else if let Some(r) = after(&p, &low, "\\appdata\\local\\programs\\") {
        first(r)
    } else if let Some(r) = after(&p, &low, "\\appdata\\local\\microsoft\\winget\\packages\\") {
        first(r).split('_').next().unwrap_or("").to_string()
    } else if low.contains("\\appdata\\local\\temp") {
        return None;
    } else {
        let r = after(&p, &low, "\\appdata\\roaming\\")
            .or_else(|| after(&p, &low, "\\appdata\\local\\"))
            .or_else(|| after(&p, &low, "\\programdata\\"))?;
        first(r)
    };
    let key = prog_norm(&name);
    if key.is_empty() {
        None
    } else {
        Some((key, name))
    }
}

/// 백업 대상 경로를 화면 줄로 바꾼다: 0=구분 제목, 1=폴더/파일, 2=프로그램.
/// 반환: (종류, 제목, 보조 설명, 빼기 키). 같은 프로그램의 경로들은 프로그램 이름 한 줄로 묶는다.
fn source_rows(joined: &str) -> Vec<(i32, String, String, String)> {
    source_rows_with(joined, |p| std::path::Path::new(p).exists())
}

/// `source_rows`와 같되, 경로가 실제로 있는지 보는 방법을 받는다(시험용). 없는 경로는 '찾을 수 없음'으로 표시한다.
fn source_rows_with(
    joined: &str,
    exists: impl Fn(&str) -> bool,
) -> Vec<(i32, String, String, String)> {
    let mut folders = vec![];
    let mut programs: Vec<(String, String, usize, usize)> = vec![];
    let mut seen: std::collections::HashSet<String> = Default::default();
    for p in joined.split(';').map(str::trim).filter(|s| !s.is_empty()) {
        if !seen.insert(p.to_lowercase()) {
            continue;
        }
        if let Some((key, name)) = program_of(p) {
            let gone = usize::from(!exists(p));
            match programs.iter_mut().find(|x| x.0 == key) {
                Some(x) => {
                    x.2 += 1;
                    x.3 += gone;
                }
                None => programs.push((key, name, 1, gone)),
            }
        } else {
            let name = p
                .trim_end_matches(['\\', '/'])
                .rsplit(['\\', '/'])
                .next()
                .unwrap_or(p)
                .to_string();
            let sub = if exists(p) {
                p.to_string()
            } else {
                format!("찾을 수 없음 · {p}")
            };
            folders.push((1, name, sub, p.to_string()));
        }
    }
    let mut out = vec![];
    if !folders.is_empty() {
        out.push((
            0,
            format!("폴더·파일 ({}개)", folders.len()),
            String::new(),
            String::new(),
        ));
        out.extend(folders);
    }
    if !programs.is_empty() {
        out.push((
            0,
            format!("프로그램 ({}개)", programs.len()),
            String::new(),
            String::new(),
        ));
        for (key, name, n, gone) in programs {
            let sub = if gone > 0 {
                format!("{n}개 항목 · 찾을 수 없는 항목 {gone}개")
            } else {
                format!("{n}개 항목")
            };
            out.push((2, name, sub, format!("prog:{key}")));
        }
    }
    out
}

/// 이 PC에 설치된 프로그램(이름, 백업할 경로들). 시작 메뉴·Program Files·AppData 폴더 이름으로 찾는다.
fn installed_programs() -> Vec<(String, Vec<String>)> {
    let env = |k: &str| std::env::var_os(k).map(std::path::PathBuf::from);
    let mut map: std::collections::BTreeMap<String, (String, Vec<String>)> = Default::default();
    let mut add = |path: &std::path::Path, only_known: bool| {
        let s = path.to_string_lossy().into_owned();
        let Some((key, name)) = program_of(&s) else {
            return;
        };
        if only_known && !map.contains_key(&key) {
            return;
        }
        let e = map.entry(key).or_insert((name, vec![]));
        if !e.1.iter().any(|x| x.eq_ignore_ascii_case(&s)) {
            e.1.push(s);
        }
    };
    let dirs = |root: &std::path::Path| -> Vec<std::path::PathBuf> {
        std::fs::read_dir(root)
            .map(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .filter(|p| p.is_dir())
                    .collect()
            })
            .unwrap_or_default()
    };
    fn lnks(root: &std::path::Path, depth: u32, out: &mut Vec<std::path::PathBuf>) {
        let Ok(rd) = std::fs::read_dir(root) else {
            return;
        };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                if depth > 0 {
                    lnks(&p, depth - 1, out);
                }
            } else if p.extension().is_some_and(|x| x.eq_ignore_ascii_case("lnk")) {
                out.push(p);
            }
        }
    }
    // 1단계: 프로그램이 확실한 곳(설치 폴더, 시작 메뉴 바로가기)
    for k in ["ProgramFiles", "ProgramFiles(x86)"] {
        if let Some(r) = env(k) {
            for d in dirs(&r) {
                add(&d, false);
            }
        }
    }
    if let Some(r) = env("LOCALAPPDATA") {
        for d in dirs(&r.join("Programs")) {
            add(&d, false);
        }
    }
    let mut menu = vec![];
    if let Some(r) = env("ProgramData") {
        lnks(
            &r.join(r"Microsoft\Windows\Start Menu\Programs"),
            3,
            &mut menu,
        );
    }
    if let Some(r) = env("APPDATA") {
        lnks(
            &r.join(r"Microsoft\Windows\Start Menu\Programs"),
            3,
            &mut menu,
        );
    }
    for l in &menu {
        add(l, false);
    }
    // 2단계: 설정·데이터 폴더는 1단계에서 찾은 프로그램에만 붙인다
    for k in ["APPDATA", "LOCALAPPDATA", "ProgramData"] {
        if let Some(r) = env(k) {
            for d in dirs(&r) {
                add(&d, true);
            }
        }
    }
    // 윈도 기본 폴더·메뉴 묶음은 프로그램이 아니므로 고르기 목록에서 뺀다
    const SYSTEM: &[&str] = &[
        "common",
        "commonfiles",
        "accessibility",
        "accessories",
        "administrativetools",
        "systemtools",
        "startup",
        "windowspowershell",
        "windowsapps",
        "modifiablewindowsapps",
        "windowsnt",
        "windowsmail",
        "windowsdefender",
        "windowsmediaplayer",
        "windowsportabledevices",
        "windowssecurity",
        "internetexplorer",
        "uninstallinformation",
        "referenceassemblies",
        "microsoft",
        "packages",
    ];
    map.retain(|k, _| !SYSTEM.contains(&k.as_str()));
    let mut v: Vec<(String, Vec<String>)> = map.into_values().collect();
    v.sort_by_key(|x| x.0.to_lowercase());
    v
}

fn rows() -> Vec<ProfileRow> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    store
        .profiles()
        .into_iter()
        .map(|p| ProfileRow {
            id: p.id.clone().into(),
            name: p.name.clone().into(),
            detail: format!(
                "원본 {} → {} · 보존 {}개/{}일 · {}",
                p.sources.join(", "),
                p.repo_dir,
                p.retention_count,
                p.retention_days,
                kova_core::profile_form::schedule_text(&p)
            )
            .into(),
            size: "".into(),
            next: kova_core::profile_form::schedule_text(&p).into(),
            stale: p.last_run.is_some_and(|t| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64() - t > 3.0 * 86400.0)
                    .unwrap_or(false)
            }),
            last: match (p.last_status.as_str(), p.last_run) {
                ("", _) | (_, None) => "기록 없음".to_string(),
                (s, Some(t)) => format!(
                    "{} · {}",
                    if s == "success" { "성공" } else { "실패" },
                    chrono_fmt(t)
                ),
            }
            .into(),
        })
        .collect()
}

/// 알림 한 줄 요약: 길면 앞부분만 보이고 "…"로 줄인다(전체는 행을 눌러 펼친다).
fn shorten_alert(s: &str) -> String {
    const MAX: usize = 90;
    if s.chars().count() <= MAX {
        return s.to_string();
    }
    let head: String = s.chars().take(MAX).collect();
    format!("{}… (눌러서 펼치기)", head.trim_end())
}

/// 용량 표시: 1GB 이상이면 GB, 아니면 MB.
fn human_mb(bytes: u64) -> String {
    let mb = bytes as f64 / 1_048_576.0;
    if mb >= 1024.0 {
        format!("{:.1}GB", mb / 1024.0)
    } else {
        format!("{mb:.1}MB")
    }
}

fn alert_rows() -> Vec<AlertRow> {
    let mut v = kova_core::alerts::AlertLog::new(&kova_service::default_config_dir()).list();
    v.reverse();
    v.into_iter()
        .map(|a| AlertRow {
            when: chrono_fmt(a.at).into(),
            level: a.level.into(),
            message: shorten_alert(&format!("{}: {}", a.profile_id, a.message)).into(),
            full: format!("{}: {}", a.profile_id, a.message).into(),
            ack: a.acknowledged,
        })
        .collect()
}

fn offsite_rows() -> Vec<OffsiteRow> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    store
        .profiles()
        .into_iter()
        .map(|p| OffsiteRow {
            id: p.id.clone().into(),
            name: p.name.clone().into(),
            dir: p
                .extra
                .get("offsite_dir")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .into(),
        })
        .collect()
}

/// 해당 프로파일의 오프사이트 폴더만 바꾼다(다른 필드는 최신 파일을 다시 읽어 보존). 빈 값이면 설정을 지운다.
fn save_offsite(id: &str, dir: &str) {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    if let Some(mut p) = store.get(id) {
        let dir = dir.trim();
        if dir.is_empty() {
            p.extra.remove("offsite_dir");
        } else {
            p.extra.insert("offsite_dir".into(), dir.into());
        }
        let _ = store.upsert(p);
    }
}

/// 기본 브라우저로 주소를 연다(셸 해석 없이 rundll32 사용).
fn open_url(url: &str) {
    use std::os::windows::process::CommandExt;
    let _ = std::process::Command::new("rundll32.exe")
        .args(["url.dll,FileProtocolHandler", url])
        .creation_flags(0x0800_0000)
        .spawn();
}

/// 오프사이트 제공자를 바꾼다(서비스가 gdrive 일 때만 드라이브로 복제).
fn set_offsite_provider(p: &str) {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let mut s = store.settings();
    s.offsite_provider = p.into();
    let _ = store.save_settings(&s);
}

/// 홈 화면의 "진행 중" 목록 한 줄(서비스 이벤트로 갱신).
struct RunInfo {
    id: u64,
    name: String,
    pct: u64,
    current: String,
    cancellable: bool,
}

fn profile_name(id: &str) -> String {
    kova_core::config::ConfigStore::new(kova_service::default_config_dir())
        .get(id)
        .map(|p| p.name)
        .unwrap_or_else(|| id.to_string())
}

/// 이벤트 하나를 진행 중 목록에 반영한다. 검증·정리는 서비스가 취소 신호를 보지 않으므로 취소 버튼을 주지 않는다.
fn update_running(rows: &mut Vec<RunInfo>, e: &kova_ipc::Event, name_of: &dyn Fn(&str) -> String) {
    match e {
        kova_ipc::Event::JobStarted {
            job_id,
            profile_id,
            kind,
        } => {
            rows.retain(|r| r.id != *job_id);
            rows.push(RunInfo {
                id: *job_id,
                name: name_of(profile_id),
                pct: 0,
                current: String::new(),
                cancellable: kind == "backup" || kind.starts_with("image"),
            });
        }
        kova_ipc::Event::Progress {
            job_id,
            percent,
            current,
        } => {
            if let Some(r) = rows.iter_mut().find(|r| r.id == *job_id) {
                r.pct = (*percent).min(100);
                r.current = current.clone();
            }
        }
        kova_ipc::Event::JobFinished { job_id, .. } => rows.retain(|r| r.id != *job_id),
        _ => {}
    }
}

fn cancel_job(id: i32) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _ = rt.block_on(kova_ipc::request(
            kova_ipc::PIPE_NAME,
            &kova_ipc::Request::Cancel { job_id: id as u64 },
        ));
    });
}

/// 이번 실행에서 마스터 비밀번호로 잠금을 푼 상태인가.
static UNLOCKED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
const LOCKED_MSG: &str = "마스터 비밀번호로 잠겨 있습니다. 설정 화면에서 잠금을 먼저 해제하세요";

/// 마스터 비밀번호가 설정돼 있고 아직 풀지 않았으면 true.
fn app_locked() -> bool {
    kova_core::applock::is_set(&kova_service::default_config_dir())
        && !UNLOCKED.load(std::sync::atomic::Ordering::SeqCst)
}

fn lock_status() -> String {
    if !kova_core::applock::is_set(&kova_service::default_config_dir()) {
        String::new()
    } else if app_locked() {
        "잠김: 마스터 비밀번호를 넣고 [잠금 해제]를 누르세요".into()
    } else {
        "해제됨: 이번 실행 동안 잠긴 동작을 할 수 있습니다".into()
    }
}

/// 화면에서 고친 전역 설정을 저장한다. 최신 파일을 다시 읽어 바꾼 칸만 덮어쓰므로 나머지 값(알 수 없는 키 포함)은 보존된다.
fn apply_settings(
    store: &kova_core::config::ConfigStore,
    min_free_gb: &str,
    ransomware: bool,
    encrypt: bool,
    use_vss: bool,
    pack_blobs: bool,
) -> Result<String, String> {
    let gb: u32 = min_free_gb
        .trim()
        .parse()
        .map_err(|_| "최소 여유 공간은 0 이상의 정수(GB)로 입력하세요".to_string())?;
    if gb > 100_000 {
        return Err("최소 여유 공간이 너무 큽니다 (최대 100000GB)".into());
    }
    // 암호화를 켜면 프로파일별 저장소에 키를 만든다. 하나라도 실패하면 설정을 저장하지 않는다(다시 눌러도 안전: 이미 켠 저장소는 건너뜀)
    let mut codes = Vec::new();
    if encrypt {
        let mut done = std::collections::HashSet::new();
        for p in store.profiles() {
            if p.repo_dir.is_empty() || !done.insert(p.repo_dir.clone()) {
                continue;
            }
            match kova_core::repocrypto::enable(std::path::Path::new(&p.repo_dir)) {
                Ok(Some(code)) => codes.push(format!("{} 복구 코드: {}", p.name, code)),
                Ok(None) => {}
                Err(e) => return Err(format!("{} 저장소 암호화 실패: {}", p.name, e)),
            }
        }
    }
    let mut s = store.settings();
    s.min_free_disk_gb = gb;
    s.enable_ransomware_protection = ransomware;
    s.encrypt_backups = encrypt;
    s.use_vss = use_vss;
    s.pack_blobs = pack_blobs;
    store
        .save_settings(&s)
        .map_err(|e| format!("저장 실패: {e}"))?;
    if codes.is_empty() {
        Ok("저장했습니다".into())
    } else {
        Ok(format!(
            "저장했습니다. '복구 키트 저장' 버튼으로 파일로 보관하세요.\n{}",
            codes.join("\n")
        ))
    }
}

/// 이 PC에 키가 있는 암호화 저장소들의 복구 코드를 모아 복구 키트 파일로 저장한다.
fn export_recovery_kit_to(
    store: &kova_core::config::ConfigStore,
    out_path: &std::path::Path,
) -> Result<String, String> {
    let mut entries = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for p in store.profiles() {
        if p.repo_dir.is_empty() || !seen.insert(p.repo_dir.clone()) {
            continue;
        }
        let repo = std::path::Path::new(&p.repo_dir);
        if !kova_core::repocrypto::is_enabled(repo) {
            continue;
        }
        let code = kova_core::repocrypto::export_recovery_code(repo)
            .map_err(|e| format!("{} 키 읽기 실패: {e}", p.name))?;
        entries.push((p.name, p.repo_dir, code));
    }
    if entries.is_empty() {
        return Err("암호화된 저장소가 없습니다. 먼저 저장소 암호화를 켜고 저장하세요".into());
    }
    kova_core::repocrypto::write_recovery_kit(out_path, &entries)?;
    Ok(format!(
        "복구 키트 저장 완료 (암호화 저장소 {}곳): {}",
        entries.len(),
        out_path.display()
    ))
}

/// 운영 자동화 설정(웹훅·CPU 스레드·오프사이트 대역폭·배터리 일시정지)을 검증하고 저장한다.
fn apply_ops_settings(
    store: &kova_core::config::ConfigStore,
    webhook: &str,
    max_threads_str: &str,
    bw_limit_str: &str,
    pause_on_battery: bool,
) -> Result<String, String> {
    let wh = webhook.trim();
    if !wh.is_empty() {
        kova_core::notify::validate_webhook_url(wh)?;
    }
    let threads: u32 = max_threads_str
        .trim()
        .parse()
        .map_err(|_| "CPU 스레드 수는 0 이상의 정수로 입력하세요".to_string())?;
    if threads > 64 {
        return Err("CPU 스레드 수는 0~64 범위로 입력하세요".into());
    }
    let bw: u32 = bw_limit_str
        .trim()
        .parse()
        .map_err(|_| "전송 속도(MB/s)는 0 이상의 정수로 입력하세요".to_string())?;
    if bw > 10_000 {
        return Err("전송 속도(MB/s)는 0~10000 범위로 입력하세요".into());
    }
    let mut s = store.settings();
    s.webhook_url = wh.to_string();
    s.max_cpu_threads = threads;
    s.offsite_bw_limit_mbps = bw;
    s.pause_on_battery = pause_on_battery;
    store
        .save_settings(&s)
        .map_err(|e| format!("운영 설정 저장 실패: {e}"))?;
    Ok("운영 설정을 저장했습니다".into())
}

fn find_make_pe_script() -> Option<(std::path::PathBuf, std::path::PathBuf)> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let candidates = [
        (dir.join("make_pe.ps1"), dir.join("kova-pe.exe")),
        (
            dir.join("installer").join("make_pe.ps1"),
            dir.join("kova-pe.exe"),
        ),
        (
            dir.join("..")
                .join("..")
                .join("installer")
                .join("make_pe.ps1"),
            dir.join("kova-pe.exe"),
        ),
    ];
    candidates
        .into_iter()
        .find(|(script, pe)| script.is_file() && pe.is_file())
}

fn build_pe_ps_args(
    script: &std::path::Path,
    out_iso: &std::path::Path,
    pe_exe: &std::path::Path,
    drivers_dir: &str,
) -> String {
    let q = |s: &str| s.replace('\'', "''");
    let mut args = format!(
        "-NoProfile -ExecutionPolicy Bypass -File '{}' -Out '{}' -Exe '{}'",
        q(&script.to_string_lossy()),
        q(&out_iso.to_string_lossy()),
        q(&pe_exe.to_string_lossy()),
    );
    if !drivers_dir.trim().is_empty() {
        args.push_str(&format!(" -DriversDir '{}'", q(drivers_dir.trim())));
    }
    args
}

fn run_build_pe_iso(iso_out: &str, drivers_dir: &str) -> String {
    let adk = std::path::Path::new(
        r"C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\DandISetEnv.bat",
    );
    if !adk.is_file() {
        return "Windows ADK(및 WinPE 애드온)가 설치되어 있지 않습니다. Microsoft Windows ADK를 먼저 설치하세요.".into();
    }
    let Some((script, pe_exe)) = find_make_pe_script() else {
        return "make_pe.ps1 또는 kova-pe.exe를 찾지 못했습니다. 릴리즈 패키지에 포함돼 있는지 확인하세요.".into();
    };
    let out_iso = if iso_out.is_empty() {
        std::env::var_os("USERPROFILE")
            .map(|u| std::path::PathBuf::from(u).join("Desktop"))
            .filter(|d| d.is_dir())
            .unwrap_or_else(std::env::temp_dir)
            .join("Kova2_WinPE_Recovery.iso")
    } else {
        std::path::PathBuf::from(iso_out)
    };
    let effective_drivers_dir = if drivers_dir.trim().is_empty() {
        let def = kova_pe::plan::default_driver_export_dir();
        if let Some(valid) = kova_pe::plan::find_collected_drivers(&def) {
            valid.to_string_lossy().to_string()
        } else {
            String::new()
        }
    } else {
        drivers_dir.trim().to_string()
    };
    if !effective_drivers_dir.is_empty() && !std::path::Path::new(&effective_drivers_dir).is_dir() {
        return format!("추가 드라이버 폴더를 찾을 수 없습니다: {effective_drivers_dir}");
    }
    let inner_args = build_pe_ps_args(&script, &out_iso, &pe_exe, &effective_drivers_dir);
    let q = |s: &str| s.replace('\'', "''");
    let cmd = format!(
        "Start-Process -FilePath 'powershell.exe' -ArgumentList '{}' -Verb RunAs -Wait",
        q(&inner_args)
    );
    use std::os::windows::process::CommandExt;
    match std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &cmd])
        .creation_flags(0x0800_0000)
        .output()
    {
        Ok(o) if o.status.success() && out_iso.is_file() => {
            let mb = std::fs::metadata(&out_iso)
                .map(|m| m.len() / (1024 * 1024))
                .unwrap_or(0);
            format!(
                "WinPE 복구 ISO 생성 완료 ({mb}MB): {} — Rufus로 USB에 구워 사용하세요.",
                out_iso.display()
            )
        }
        Ok(o) if !o.status.success() => {
            "관리자 권한 승인이 취소되었거나 제작이 중단되었습니다.".into()
        }
        Ok(_) => "ISO 파일이 생성되지 않았습니다. ADK WinPE 애드온 설치 상태를 확인하세요.".into(),
        Err(e) => format!("제작 실행 실패: {e}"),
    }
}

/// 현재 PC의 서드파티(NVMe, 네트워크) 드라이버를 자동 수집(덤프)한다.
fn run_export_drivers(dest_override: Option<&std::path::Path>) -> String {
    let dest = dest_override
        .map(std::path::PathBuf::from)
        .unwrap_or_else(kova_pe::plan::default_driver_export_dir);
    match kova_pe::plan::export_drivers(&dest) {
        Ok(msg) => msg,
        Err(e) => format!("드라이버 수집 실패: {e}"),
    }
}

/// 모든 프로파일의 저장소를 가로질러 파일명/초성으로 통합 검색하고 시점별 버전 수를 집계한다.
fn search_all_repos(
    store: &kova_core::config::ConfigStore,
    query: &str,
) -> Vec<(String, String, String, String, usize, String)> {
    let q = query.trim();
    if q.is_empty() {
        return Vec::new();
    }
    let mut seen_repos = std::collections::HashSet::new();
    let mut out = Vec::new();
    for p in store.profiles() {
        if p.repo_dir.is_empty() || !seen_repos.insert(p.repo_dir.clone()) {
            continue;
        }
        let repo = std::path::Path::new(&p.repo_dir);
        let Ok(items) = kova_engine::search::search_snapshots(repo, q, 50, 40, true) else {
            continue;
        };
        for item in items {
            let info = format!(
                "{} · {} · {}",
                item.latest_size_formatted, item.latest_mtime_formatted, item.latest_snapshot_id
            );
            out.push((
                item.filename,
                item.rel_path,
                p.repo_dir.clone(),
                item.latest_snapshot_id,
                item.version_count.max(1),
                info,
            ));
        }
    }
    out.truncate(60);
    out
}

/// 복구 코드로 이 PC의 키 파일을 다시 만든다. 암호화된 프로파일 저장소 중 코드가 맞는 곳마다 복원한다.
fn restore_keys(store: &kova_core::config::ConfigStore, code: &str) -> Result<String, String> {
    let mut ok = 0;
    let mut seen = std::collections::HashSet::new();
    for p in store.profiles() {
        let repo = std::path::Path::new(&p.repo_dir);
        if p.repo_dir.is_empty()
            || !seen.insert(p.repo_dir.clone())
            || !kova_core::repocrypto::is_enabled(repo)
        {
            continue;
        }
        if kova_core::repocrypto::restore_key(repo, code).is_ok() {
            ok += 1;
        }
    }
    if ok == 0 {
        Err("맞는 암호화 저장소를 찾지 못했습니다. 코드를 다시 확인하고, 저장소 드라이브가 연결돼 있는지 보세요".into())
    } else {
        Ok(format!("키를 복원했습니다 (저장소 {ok}곳)"))
    }
}

fn setting_lines() -> Vec<slint::SharedString> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let s = store.settings();
    let mut v = vec![
        format!(
            "오프사이트: {} ({}) {}",
            if s.offsite_enabled {
                "사용"
            } else {
                "사용 안 함"
            },
            s.offsite_provider,
            s.offsite_remote_path
        ),
        format!(
            "랜섬웨어 보호: {}",
            if s.enable_ransomware_protection {
                "켜짐"
            } else {
                "꺼짐"
            }
        ),
        format!("최소 여유 공간: {}GB", s.min_free_disk_gb),
        "프로파일".into(),
    ];
    for p in store.profiles() {
        v.push(format!(
            "· {} — 원본 {}곳 → {} · 보존 {}개/{}일 · 압축 {}",
            p.name,
            p.sources.len(),
            p.repo_dir,
            p.retention_count,
            p.retention_days,
            p.compression_level
        ));
    }
    v.into_iter().map(Into::into).collect()
}

fn snaps() -> Vec<SnapRow> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let mut all: Vec<(f64, SnapRow)> = vec![];
    // 프로파일 둘이 같은 저장소를 쓰면 같은 스냅샷이 두 번 보이므로 (저장소, 번호)당 한 줄만 둔다
    let mut seen: std::collections::HashSet<(String, String)> = Default::default();
    let ids: std::collections::HashSet<String> =
        store.profiles().into_iter().map(|p| p.id).collect();
    for p in store.profiles() {
        let repo = std::path::PathBuf::from(&p.repo_dir);
        if !repo.join("metadata.db").exists() {
            continue; // 읽기만 한다: 없는 저장소에 DB 를 새로 만들지 않는다
        }
        let Ok(h) = kova_core::history::History::open(&repo) else {
            continue;
        };
        for r in h.list_snapshots().unwrap_or_default() {
            if !r.profile_id.is_empty() && r.profile_id != p.id && ids.contains(&r.profile_id) {
                continue; // 이 스냅샷은 다른 프로파일이 만든 것(같은 저장소 공유)
            }
            if !seen.insert((p.repo_dir.to_lowercase(), r.id.clone())) {
                continue;
            }
            let v = if r.verify_error_count > 0 {
                "오류 있음"
            } else if r.is_verified {
                "검증됨"
            } else {
                "미검증"
            };
            all.push((
                r.created_at,
                SnapRow {
                    id: r.id.clone().into(),
                    repo: p.repo_dir.clone().into(),
                    when: chrono_fmt(r.created_at).into(),
                    name: p.name.clone().into(),
                    summary: format!(
                        "파일 {}개 · {}",
                        r.total_files,
                        human_mb(r.total_bytes as u64)
                    )
                    .into(),
                    verified: v.into(),
                },
            ));
        }
    }
    all.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    all.into_iter().take(50).map(|x| x.1).collect()
}

/// 홈 화면의 "내 드라이브와 백업 보관함": 존재하는 드라이브별 사용률. 백업 저장소가 있는 드라이브를 표시한다.
fn drive_rows() -> Vec<DriveRow> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let repos: Vec<String> = store
        .profiles()
        .iter()
        .map(|p| p.repo_dir.to_uppercase())
        .collect();
    let gb = |b: u64| format!("{:.1} GB", b as f64 / 1_073_741_824.0);
    let mut v = Vec::new();
    for l in b'C'..=b'Z' {
        let root = format!("{}:\\", l as char);
        if !std::path::Path::new(&root).exists() {
            continue;
        }
        let Some((total, avail)) = kova_core::diskspace::usage(std::path::Path::new(&root)) else {
            continue;
        };
        let used = total.saturating_sub(avail);
        v.push(DriveRow {
            name: format!("{}:", l as char).into(),
            pct: (used as f64 * 100.0 / total as f64).round() as i32,
            used: gb(used).into(),
            free: gb(avail).into(),
            repo: repos
                .iter()
                .any(|r| r.starts_with(&format!("{}:", l as char))),
        });
    }
    v
}

fn chrono_fmt(ts: f64) -> String {
    // 외부 의존 없이 "N분/시간/일 전"으로 표시
    let secs = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
        - ts)
        .max(0.0) as u64;
    match secs {
        0..=59 => "방금".into(),
        60..=3599 => format!("{}분 전", secs / 60),
        3600..=86399 => format!("{}시간 전", secs / 3600),
        _ => format!("{}일 전", secs / 86400),
    }
}

/// 백업 보관함의 images 폴더에서 저장된 디스크 이미지(.qblk)를 최신순으로 찾는다.
fn image_rows() -> Vec<ImageRow> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let mut seen = std::collections::HashSet::new();
    let mut all: Vec<(std::time::SystemTime, ImageRow)> = Vec::new();
    for p in store.profiles() {
        let dir = std::path::Path::new(&p.repo_dir).join("images");
        let Ok(rd) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in rd.flatten() {
            let path = e.path();
            if path.extension().and_then(|x| x.to_str()) != Some("qblk")
                || !seen.insert(path.clone())
            {
                continue;
            }
            let Ok(md) = e.metadata() else { continue };
            let mtime = md.modified().unwrap_or(std::time::UNIX_EPOCH);
            let ts = mtime
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            all.push((
                mtime,
                ImageRow {
                    name: e.file_name().to_string_lossy().to_string().into(),
                    path: path.to_string_lossy().to_string().into(),
                    size: format!("{:.1} GB", md.len() as f64 / 1_073_741_824.0).into(),
                    when: chrono_fmt(ts).into(),
                },
            ));
        }
    }
    all.sort_by_key(|x| std::cmp::Reverse(x.0));
    all.into_iter().take(20).map(|x| x.1).collect()
}

/// 변환할 만한 디스크 이미지(.img/.raw/.bin/.vhd, 100MB 이상)를 사용자 폴더와 각 드라이브 앞쪽 폴더에서 찾는다.
fn convert_candidates() -> Vec<ImageRow> {
    const SKIP: [&str; 9] = [
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "system volume information",
        "appdata",
        "node_modules",
        ".git",
    ];
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let repos: Vec<std::path::PathBuf> = store
        .profiles()
        .iter()
        .map(|p| std::path::PathBuf::from(&p.repo_dir))
        .collect();
    let mut roots: Vec<(std::path::PathBuf, u32)> = Vec::new();
    if let Some(h) = std::env::var_os("USERPROFILE") {
        let h = std::path::PathBuf::from(h);
        for d in ["Downloads", "Desktop", "Documents"] {
            roots.push((h.join(d), 3));
        }
    }
    for l in b'C'..=b'Z' {
        let r = std::path::PathBuf::from(format!("{}:\\", l as char));
        if r.exists() {
            roots.push((r, 2));
        }
    }
    let mut seen = std::collections::HashSet::new();
    let mut out: Vec<(std::time::SystemTime, ImageRow)> = Vec::new();
    let mut stack: Vec<(std::path::PathBuf, u32)> = roots;
    let mut visited = 0usize;
    while let Some((dir, depth)) = stack.pop() {
        visited += 1;
        if visited > 20000 || out.len() >= 40 {
            break;
        }
        let Ok(rd) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in rd.flatten() {
            let path = e.path();
            let Ok(md) = e.metadata() else { continue };
            if md.is_dir() {
                let name = e.file_name().to_string_lossy().to_lowercase();
                if depth > 1 && !SKIP.contains(&name.as_str()) && !repos.iter().any(|r| r == &path)
                {
                    stack.push((path, depth - 1));
                }
                continue;
            }
            let ext = path
                .extension()
                .and_then(|x| x.to_str())
                .map(|x| x.to_ascii_lowercase())
                .unwrap_or_default();
            if !matches!(ext.as_str(), "img" | "raw" | "bin" | "vhd")
                || md.len() < 100 * 1024 * 1024
                || !seen.insert(path.clone())
            {
                continue;
            }
            let mtime = md.modified().unwrap_or(std::time::UNIX_EPOCH);
            out.push((
                mtime,
                ImageRow {
                    name: e.file_name().to_string_lossy().to_string().into(),
                    path: path.to_string_lossy().to_string().into(),
                    size: format!("{:.1} GB", md.len() as f64 / 1_073_741_824.0).into(),
                    when: "".into(),
                },
            ));
        }
    }
    out.sort_by_key(|x| std::cmp::Reverse(x.0));
    out.into_iter().take(20).map(|x| x.1).collect()
}

fn scan_convert_async(weak: slint::Weak<MainWindow>) {
    std::thread::spawn(move || {
        let rows = convert_candidates();
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = weak.upgrade() {
                w.set_conv_cands(std::rc::Rc::new(slint::VecModel::from(rows)).into());
            }
        });
    });
}

/// 저장해 둔 이미지(.qblk) 한 개를 지운다. 백업 보관함의 images 폴더 안 .qblk 파일만 허용한다.
fn delete_image_file(path: &str) -> Result<(), String> {
    if app_locked() {
        return Err(LOCKED_MSG.into());
    }
    let target = std::path::Path::new(path);
    if target.extension().and_then(|x| x.to_str()) != Some("qblk") {
        return Err("이미지(.qblk) 파일만 지울 수 있습니다".into());
    }
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let canon = std::fs::canonicalize(target).map_err(|e| format!("파일을 찾을 수 없음: {e}"))?;
    let allowed = store.profiles().iter().any(|p| {
        std::fs::canonicalize(std::path::Path::new(&p.repo_dir).join("images"))
            .map(|d| canon.parent() == Some(d.as_path()))
            .unwrap_or(false)
    });
    if !allowed {
        return Err("백업 보관함의 images 폴더 밖 파일은 지우지 않습니다".into());
    }
    if let Err(e) = std::fs::remove_file(&canon) {
        // 읽기 전용(랜섬웨어 보호)이면 속성을 풀고 한 번 더 시도
        if let Ok(md) = std::fs::metadata(&canon) {
            let mut perm = md.permissions();
            #[allow(clippy::permissions_set_readonly_false)]
            // 윈도우 전용 앱: 읽기 전용 속성만 푼다
            perm.set_readonly(false);
            let _ = std::fs::set_permissions(&canon, perm);
        }
        std::fs::remove_file(&canon).map_err(|e2| format!("지우지 못했습니다: {e2} ({e})"))?;
    }
    Ok(())
}

/// 변환 결과를 둘 위치: 첫 프로파일 저장소의 images 폴더에 `<원본이름>.qblk`(있으면 번호)
fn auto_convert_target(src: &str) -> Option<String> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    let p = store.profiles().into_iter().next()?;
    let dir = std::path::Path::new(&p.repo_dir).join("images");
    std::fs::create_dir_all(&dir).ok()?;
    let stem = std::path::Path::new(src)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("image");
    let mut cand = dir.join(format!("{stem}.qblk"));
    let mut n = 2;
    while cand.exists() {
        cand = dir.join(format!("{stem}_{n}.qblk"));
        n += 1;
    }
    Some(cand.to_string_lossy().to_string())
}

/// 직전과 같으면 None(화면을 건드리지 않음), 달라졌으면 Some(새 값)을 돌려주고 기억한다.
fn changed_only<T: PartialEq + Clone>(last: &mut Option<Vec<T>>, new: Vec<T>) -> Option<Vec<T>> {
    if last.as_ref() == Some(&new) {
        None
    } else {
        *last = Some(new.clone());
        Some(new)
    }
}

/// 서비스와 통신하는 백그라운드 스레드: 상태 폴링 + 이벤트 구독. 화면 갱신은 이벤트 루프로 넘긴다.
fn spawn_ipc(weak: slint::Weak<MainWindow>) {
    let rt = std::sync::Arc::new(
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap(),
    );
    let (w1, rt1) = (weak.clone(), rt.clone());
    std::thread::spawn(move || {
        rt1.block_on(async move {
            let mut last_off: Option<Vec<(String, String, String)>> = None;
            let mut last_profiles: Option<Vec<ProfileRow>> = None;
            let mut last_snaps: Option<Vec<SnapRow>> = None;
            let mut last_drives: Option<Vec<DriveRow>> = None;
            let mut last_images: Option<Vec<ImageRow>> = None;
            let mut last_alerts: Option<Vec<AlertRow>> = None;
            let mut last_lines: Option<Vec<slint::SharedString>> = None;
            loop {
                let text = match kova_ipc::request(kova_ipc::PIPE_NAME, &kova_ipc::Request::Status)
                    .await
                {
                    Ok(evs) => match evs.first() {
                        Some(kova_ipc::Event::Status { waiting, running }) => {
                            format!("서비스 연결됨 · 실행 중 {running} · 대기 {waiting}")
                        }
                        _ => "서비스 응답 이상".into(),
                    },
                    Err(_) => "서비스에 연결할 수 없음 (Kova 서비스가 꺼져 있습니다)".into(),
                };
                let w = w1.clone();
                let mut profiles = rows();
                let snapshots = snaps();
                // 대상별 크기: 가장 최근 스냅샷 요약("파일 N개 · 크기")의 크기 부분
                for p in profiles.iter_mut() {
                    if let Some(s) = snapshots.iter().find(|s| s.name == p.name) {
                        if let Some((_, sz)) = s.summary.split_once(" · ") {
                            p.size = sz.into();
                        }
                    }
                }
                let (alerts, lines, off) = (alert_rows(), setting_lines(), offsite_rows());
                // 입력 중인 칸이 2초마다 초기화되지 않도록, 내용이 바뀐 때만 모델을 교체한다
                let key: Vec<(String, String, String)> = off
                    .iter()
                    .map(|o| (o.id.to_string(), o.name.to_string(), o.dir.to_string()))
                    .collect();
                let off = if last_off.as_ref() == Some(&key) {
                    None
                } else {
                    last_off = Some(key);
                    Some(off)
                };
                let profiles = changed_only(&mut last_profiles, profiles);
                let snapshots = changed_only(&mut last_snaps, snapshots);
                let drives = changed_only(&mut last_drives, drive_rows());
                let images = changed_only(&mut last_images, image_rows());
                let alerts = changed_only(&mut last_alerts, alerts);
                let lines = changed_only(&mut last_lines, lines);
                let n = kova_core::alerts::AlertLog::new(&kova_service::default_config_dir())
                    .unacknowledged();
                let alert = if n > 0 {
                    format!("확인하지 않은 알림 {n}건 (백업 실패 등)")
                } else {
                    String::new()
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = w.upgrade() {
                        w.set_service_ok(text.starts_with("서비스 연결됨"));
                        w.set_service_status(text.into());
                        w.set_alert_text(alert.into());
                        if let Some(images) = images {
                            if w.get_img_path().is_empty() {
                                if let Some(f) = images.first() {
                                    w.set_img_path(f.path.clone());
                                }
                            }
                            w.set_images(std::rc::Rc::new(slint::VecModel::from(images)).into());
                        }
                        if let Some(drives) = drives {
                            w.set_drives(std::rc::Rc::new(slint::VecModel::from(drives)).into());
                        }
                        if let Some(off) = off {
                            w.set_offsite_rows(std::rc::Rc::new(slint::VecModel::from(off)).into());
                        }
                        if let Some(alerts) = alerts {
                            w.set_alerts(std::rc::Rc::new(slint::VecModel::from(alerts)).into());
                        }
                        if let Some(lines) = lines {
                            w.set_setting_lines(
                                std::rc::Rc::new(slint::VecModel::from(lines)).into(),
                            );
                        }
                        if let Some(profiles) = profiles {
                            w.set_profiles(
                                std::rc::Rc::new(slint::VecModel::from(profiles)).into(),
                            );
                        }
                        if let Some(snapshots) = snapshots {
                            w.set_snapshots(
                                std::rc::Rc::new(slint::VecModel::from(snapshots)).into(),
                            );
                        }
                    }
                });
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        });
    });
    std::thread::spawn(move || {
        rt.block_on(async move {
            let running: std::sync::Mutex<Vec<RunInfo>> = std::sync::Mutex::new(vec![]);
            loop {
                let w = weak.clone();
                let _ = kova_ipc::stream(kova_ipc::PIPE_NAME, &kova_ipc::Request::Subscribe, |e| {
                    let rows: Vec<RunRow> = {
                        let mut r = running.lock().unwrap();
                        update_running(&mut r, &e, &profile_name);
                        r.iter()
                            .map(|x| RunRow {
                                id: x.id as i32,
                                name: x.name.clone().into(),
                                pct: x.pct as i32,
                                current: x.current.clone().into(),
                                cancellable: x.cancellable,
                            })
                            .collect()
                    };
                    {
                        let w = w.clone();
                        let _ = slint::invoke_from_event_loop(move || {
                            if let Some(w) = w.upgrade() {
                                w.set_running(std::rc::Rc::new(slint::VecModel::from(rows)).into());
                            }
                        });
                    }
                    let line = match e {
                        kova_ipc::Event::JobStarted {
                            profile_id, kind, ..
                        } => format!("시작: {profile_id} ({kind})"),
                        kova_ipc::Event::Progress {
                            percent, current, ..
                        } => format!("진행 {percent}% {current}"),
                        kova_ipc::Event::JobFinished {
                            profile_id,
                            ok,
                            message,
                            ..
                        } => {
                            if !ok && message != "취소됨" {
                                toast(
                                    "Kova 백업 실패",
                                    &format!("{}: {message}", profile_name(&profile_id)),
                                );
                            }
                            format!(
                                "{} {profile_id}: {message}",
                                if ok { "완료" } else { "실패" }
                            )
                        }
                        _ => return true,
                    };
                    let w = w.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(w) = w.upgrade() {
                            // 최신 기록이 맨 위: 길어져도 아래가 잘려 최신 줄이 안 보이는 일이 없다
                            let old = w.get_log_text().to_string();
                            let keep: Vec<&str> = old.lines().take(11).collect();
                            w.set_log_text((line + "\n" + &keep.join("\n")).trim_end().into());
                        }
                    });
                    true
                })
                .await;
                // 연결이 끊기면 진행 중 표시를 비운다(서비스가 죽었는데 취소 버튼이 남지 않도록)
                running.lock().unwrap().clear();
                let w = weak.clone();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = w.upgrade() {
                        w.set_running(
                            std::rc::Rc::new(slint::VecModel::<RunRow>::default()).into(),
                        );
                    }
                });
                tokio::time::sleep(std::time::Duration::from_secs(3)).await; // 서비스가 꺼졌다 켜져도 다시 붙는다
            }
        });
    });
}

/// 폴더 선택 창용 C# 어셈블리를 캐시하는 경로(처음 한 번만 컴파일하고 이후에는 읽기만 한다).
fn folder_picker_dll() -> std::path::PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    base.join("Kova2")
        .join(format!("fp_{}.dll", FOLDER_PICKER_CS.len()))
}

/// 선택 창을 띄우는 PowerShell 스크립트. 폴더 선택은 WinForms 없이 C# 어셈블리(캐시)만 쓰고,
/// 소유 창은 PowerShell 이 직접 만든 투명 최상위 창이다. Kova 창을 소유자로 잡으면 선택 창이 열려 있는 동안
/// Kova UI 스레드가 이 호출을 기다리므로(교착) 선택 창이 보이지 않고, 현재 전경 창을 잡으면 엉뚱한 창에 붙는다.
fn build_pick_script(kind: &str) -> String {
    // 폴더 선택은 탐색기와 같은 최신 선택 창(IFileOpenDialog). "folders" 는 여러 폴더를 한 번에 고른다.
    if kind == "folder" || kind == "folders" {
        let dll = folder_picker_dll().to_string_lossy().replace('\'', "''");
        return format!(
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='Stop';$dll='{dll}';\
             if(-not (Test-Path -LiteralPath $dll)){{New-Item -ItemType Directory -Force -Path (Split-Path $dll) | Out-Null;\
             Add-Type -TypeDefinition @'\n{FOLDER_PICKER_CS}\n'@ -OutputAssembly $dll}};\
             Add-Type -Path $dll;Add-Type -AssemblyName System.Windows.Forms;\
             $f=New-Object Windows.Forms.Form;$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.FormBorderStyle='None';\
             $f.StartPosition='CenterScreen';$f.Opacity=0;$f.Size=New-Object Drawing.Size(1,1);$f.Show();\
             [FP]::Pick(${multi},$f.Handle) | ForEach-Object {{ $_ }}",
            multi = if kind == "folders" { "true" } else { "false" }
        );
    }
    let (cls, filter, ext) = match kind {
        "qblk-open" => (
            "OpenFileDialog",
            "Kova 이미지 (*.qblk)|*.qblk|모든 파일|*.*",
            "",
        ),
        "qblk-save" => ("SaveFileDialog", "Kova 이미지 (*.qblk)|*.qblk", "qblk"),
        "img-save" => (
            "SaveFileDialog",
            "디스크 이미지|*.img;*.raw;*.vhd;*.vhdx;*.bin",
            "img",
        ),
        _ => (
            "OpenFileDialog",
            "디스크 이미지|*.img;*.raw;*.bin;*.vhd|모든 파일|*.*",
            "",
        ),
    };
    format!(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Windows.Forms;$f=New-Object Windows.Forms.Form;$f.TopMost=$true;\
         $d=New-Object Windows.Forms.{cls};$d.Filter='{filter}';$d.DefaultExt='{ext}';if($d.ShowDialog($f)-eq'OK'){{$d.FileName}}"
    )
}

/// PowerShell 이 낸 여러 줄(여러 폴더)을 ';' 로 이어 한 칸에 넣는다. 줄 끝 공백·빈 줄·맨 앞 BOM(U+FEFF)은 버린다.
fn parse_picked(stdout: &str) -> String {
    stdout
        .lines()
        .map(|l| l.trim_matches(|c: char| c.is_whitespace() || c == '\u{feff}'))
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join(";")
}

/// Windows 폴더/파일 선택 창(PowerShell, 창 없이 실행). 취소하면 빈 문자열, 창을 못 열면 오류 문구.
fn pick_path(kind: &str) -> Result<String, String> {
    use std::os::windows::process::CommandExt;
    let script = build_pick_script(kind);
    let utf16: Vec<u8> = script
        .encode_utf16()
        .flat_map(|u| u.to_le_bytes())
        .collect();
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut enc = String::new();
    for c in utf16.chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                enc.push(T[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                enc.push('=');
            }
        }
    }
    let out = std::process::Command::new("powershell.exe")
        .args([
            "-STA",
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            &enc,
        ])
        .creation_flags(0x0800_0000)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        // 캐시된 어셈블리가 깨졌을 수 있으니 지워서 다음 번에 다시 만들게 한다
        let _ = std::fs::remove_file(folder_picker_dll());
        let err = String::from_utf8_lossy(&out.stderr);
        let first = err.lines().map(str::trim).find(|l| !l.is_empty());
        return Err(first
            .unwrap_or("PowerShell 실행 실패")
            .chars()
            .take(80)
            .collect());
    }
    Ok(parse_picked(&String::from_utf8_lossy(&out.stdout)))
}

const FOLDER_PICKER_CS: &str = r#"using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class FP {
  [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")] class FOD {}
  [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IFileOpenDialog {
    [PreserveSig] int Show(IntPtr h);
    void SetFileTypes(uint c, IntPtr f);
    void SetFileTypeIndex(uint i);
    void GetFileTypeIndex(out uint i);
    void Advise(IntPtr p, out uint c);
    void Unadvise(uint c);
    void SetOptions(uint o);
    void GetOptions(out uint o);
    void SetDefaultFolder(IShellItem s);
    void SetFolder(IShellItem s);
    void GetFolder(out IShellItem s);
    void GetCurrentSelection(out IShellItem s);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string n);
    void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string n);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string t);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string t);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string t);
    void GetResult(out IShellItem s);
    void AddPlace(IShellItem s, int f);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string e);
    void Close(int hr);
    void SetClientGuid(ref Guid g);
    void ClearClientData();
    void SetFilter(IntPtr f);
    void GetResults(out IShellItemArray a);
    void GetSelectedItems(out IShellItemArray a);
  }
  [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItem {
    void BindToHandler(IntPtr p, ref Guid b, ref Guid r, out IntPtr o);
    void GetParent(out IShellItem s);
    void GetDisplayName(uint n, [MarshalAs(UnmanagedType.LPWStr)] out string s);
    void GetAttributes(uint m, out uint a);
    void Compare(IShellItem s, uint h, out int o);
  }
  [ComImport, Guid("b63ea76d-1f85-456f-a19c-48159efa858b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItemArray {
    void BindToHandler(IntPtr p, ref Guid b, ref Guid r, out IntPtr o);
    void GetPropertyStore(int f, ref Guid r, out IntPtr o);
    void GetPropertyDescriptionList(IntPtr k, ref Guid r, out IntPtr o);
    void GetAttributes(int f, uint m, out uint a);
    void GetCount(out uint c);
    void GetItemAt(uint i, out IShellItem s);
    void EnumItems(out IntPtr e);
  }
  public static string[] Pick(bool multi, IntPtr owner) {
    var d = (IFileOpenDialog)new FOD();
    uint o; d.GetOptions(out o);
    d.SetOptions(o | 0x20u | 0x40u | (multi ? 0x200u : 0u));
    d.SetTitle(multi ? "폴더 선택 (여러 개 선택 가능)" : "폴더 선택");
    int hr = d.Show(owner);
    if (hr == unchecked((int)0x800704C7)) return new string[0]; // 사용자가 취소
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
    var l = new List<string>();
    IShellItemArray a = null;
    try { d.GetResults(out a); } catch (Exception) { a = null; }
    if (a != null) {
      uint c; a.GetCount(out c);
      for (uint i = 0; i < c; i++) { IShellItem s; a.GetItemAt(i, out s); string p; s.GetDisplayName(0x80058000u, out p); l.Add(p); }
    } else {
      IShellItem s1; d.GetResult(out s1); string p1; s1.GetDisplayName(0x80058000u, out p1); l.Add(p1);
    }
    return l.ToArray();
  }
}"#;

/// 윈도우 알림(토스트)을 띄운다. 제목·내용은 환경 변수로 넘겨 스크립트에 끼어들지 못하게 한다. 실패해도 조용히 넘어간다.
fn toast(title: &str, body: &str) {
    use std::os::windows::process::CommandExt;
    const SCRIPT: &str = "$t=$env:KOVA_TOAST_T;$b=$env:KOVA_TOAST_B;\
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;\
$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);\
$n=$x.GetElementsByTagName('text');\
$n.Item(0).AppendChild($x.CreateTextNode($t))|Out-Null;\
$n.Item(1).AppendChild($x.CreateTextNode($b))|Out-Null;\
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))";
    let (t, b) = (
        title.to_string(),
        body.chars().take(200).collect::<String>(),
    );
    std::thread::spawn(move || {
        let _ = std::process::Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", SCRIPT])
            .env("KOVA_TOAST_T", t)
            .env("KOVA_TOAST_B", b)
            .creation_flags(0x0800_0000)
            .status();
    });
}

fn send(kind: impl Into<String>, id: String) {
    let kind: String = kind.into();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _ = rt.block_on(kova_ipc::request(
            kova_ipc::PIPE_NAME,
            &kova_ipc::Request::RunNow {
                profile_id: id,
                kind,
            },
        ));
    });
}

const UPDATE_BASE: &str = "https://github.com";
const UPDATE_REPO: &str = "kks3365550/Kova";

/// 최신 버전 조회만 한다(다운로드·설치는 서명 검증과 사용자 승인 절차가 정해질 때까지 하지 않는다).
fn check_update(weak: slint::Weak<MainWindow>) {
    std::thread::spawn(move || {
        let current = env!("CARGO_PKG_VERSION");
        let text = match kova_core::fetch::latest_tag(UPDATE_BASE, UPDATE_REPO) {
            Ok(tag) if kova_core::update::is_newer(&tag, current) => {
                format!("새 버전 {tag} 이(가) 있습니다 (현재 {current})")
            }
            Ok(tag) => format!("최신입니다 (최신 {tag}, 현재 {current})"),
            Err(e) => format!("확인 실패: {e}"),
        };
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = weak.upgrade() {
                w.set_update_text(text.into());
            }
        });
    });
}

/// 앱 시작 시 1회 백그라운드 업데이트 확인: 새 버전이 있을 때만 조용히 알리고, 최신이거나 오프라인이면 무시한다.
fn check_update_silent(weak: slint::Weak<MainWindow>) {
    std::thread::spawn(move || {
        let current = env!("CARGO_PKG_VERSION");
        if let Ok(tag) = kova_core::fetch::latest_tag(UPDATE_BASE, UPDATE_REPO) {
            if kova_core::update::is_newer(&tag, current) {
                let msg = format!("새 버전 {tag} 이(가) 출시되었습니다 (현재 {current}) — 보호·업데이트 탭에서 설치 가능");
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_update_text(msg.into());
                    }
                });
            }
        }
    });
}

/// 최신 조회 → 서명 검증된 패키지 → 설치 폴더에 적용(실패 시 롤백). 설치 폴더(Program Files)에 쓰려면 관리자 권한이 필요하다.
/// 반환값 `(안내 문구, 실제로 설치했는지)`. 재시작 여부는 문구가 아니라 이 값으로 정한다.
fn do_install() -> Result<(String, bool), String> {
    let current = env!("CARGO_PKG_VERSION");
    let tag = kova_core::fetch::latest_tag(UPDATE_BASE, UPDATE_REPO).map_err(|e| e.to_string())?;
    if !kova_core::update::is_newer(&tag, current) {
        return Ok((format!("이미 최신입니다 ({current})"), false));
    }
    let pkg = kova_core::fetch::fetch_verified(
        UPDATE_BASE,
        UPDATE_REPO,
        &tag,
        kova_core::fetch::TRUSTED_KEYS,
        500 << 20,
    )
    .map_err(|e| e.to_string())?;
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let dir = exe.parent().ok_or("설치 폴더를 알 수 없음")?.to_path_buf();
    let backup = dir.join("update_backup");
    let r = kova_core::install::apply(&pkg, &dir, &backup, &["kova-app.exe"])
        .map_err(|e| e.to_string())?;
    // 서비스는 옛 파일로 계속 돌고 있으므로 완전히 정지될 때까지 확인한 뒤 새 파일로 다시 시작한다
    let svc = |a: &str| {
        use std::os::windows::process::CommandExt;
        std::process::Command::new("sc.exe")
            .args([a, "KovaSvc"])
            .creation_flags(0x0800_0000)
            .output()
    };
    let _ = svc("stop");
    let stopped = stop_service_wait("KovaSvc", std::time::Duration::from_secs(30));
    let _ = svc("start");
    let svc_note = if stopped {
        "서비스 재시작"
    } else {
        "서비스 정지 대기 초과 후 재시작 시도"
    };
    Ok((
        format!(
            "{tag} 설치 완료({}개 파일, {svc_note}). 앱을 다시 시작합니다",
            r.files
        ),
        true,
    ))
}

fn parse_service_stopped(sc_query_out: &str) -> bool {
    sc_query_out.contains("STOPPED") || sc_query_out.contains("1060")
}

fn stop_service_wait(service_name: &str, timeout: std::time::Duration) -> bool {
    use std::os::windows::process::CommandExt;
    let deadline = std::time::Instant::now() + timeout;
    loop {
        let out = std::process::Command::new("sc.exe")
            .args(["query", service_name])
            .creation_flags(0x0800_0000)
            .output();
        if let Ok(o) = out {
            let text = format!(
                "{}\n{}",
                String::from_utf8_lossy(&o.stdout),
                String::from_utf8_lossy(&o.stderr)
            );
            if parse_service_stopped(&text) || o.status.code() == Some(1060) {
                return true;
            }
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
}

/// 관리자 권한 도우미가 결과 파일에 적는 형식: 첫 줄 `installed`/`noop`, 이어서 안내 문구.
fn encode_install_result(installed: bool, text: &str) -> String {
    format!("{}\n{text}", if installed { "installed" } else { "noop" })
}

/// `encode_install_result` 의 반대. 첫 줄 표식이 없으면(옛 버전이 쓴 파일) 설치 안 한 것으로 보고 전체를 문구로 쓴다.
fn decode_install_result(raw: &str) -> (bool, String) {
    match raw.split_once('\n') {
        Some(("installed", t)) => (true, t.to_string()),
        Some(("noop", t)) => (false, t.to_string()),
        _ => (false, raw.to_string()),
    }
}

/// 사용자가 "업데이트 설치"를 눌렀을 때만 실행. 설치 폴더 쓰기에 관리자 권한이 필요하므로
/// 자기 자신을 관리자 권한(UAC 승인)으로 `--install-update <결과파일>` 모드로 다시 실행해 결과를 받는다.
/// 패키지에 kova-app.exe 가 없으면(예: 파이썬판 릴리즈) 설치하지 않는다.
fn install_update(weak: slint::Weak<MainWindow>) {
    let exe_path = std::env::current_exe().unwrap_or_default();
    std::thread::spawn(move || {
        let run = || -> Result<(bool, String), String> {
            use std::os::windows::process::CommandExt;
            let exe = std::env::current_exe().map_err(|e| e.to_string())?;
            let res = std::env::temp_dir().join(format!("kova_update_{}.txt", std::process::id()));
            let _ = std::fs::remove_file(&res);
            let q = |p: &std::path::Path| p.to_string_lossy().replace('\'', "''");
            let cmd = format!(
                "Start-Process -FilePath '{}' -ArgumentList '--install-update','\"{}\"' -Verb RunAs -Wait",
                q(&exe),
                q(&res)
            );
            let st = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-NonInteractive", "-Command", &cmd])
                .creation_flags(0x0800_0000)
                .output()
                .map_err(|e| e.to_string())?;
            let text = std::fs::read_to_string(&res).ok();
            let _ = std::fs::remove_file(&res);
            match text {
                Some(t) => Ok(decode_install_result(&t)),
                None if !st.status.success() => Err("관리자 권한 승인이 취소되었습니다".into()),
                None => Err("설치 결과를 받지 못했습니다".into()),
            }
        };
        let (installed, mut text) =
            run().unwrap_or_else(|e| (false, format!("설치하지 않음: {e}")));
        // 설치가 끝났으면 새 앱을 띄운다(설치 전에 알아 둔 경로 사용: 교체 후 current_exe 는 옛 파일을 가리킬 수 있다).
        // 새 앱이 살아 있는 것을 확인한 뒤에만 이 앱을 닫아, 새 앱이 죽으면 앱이 통째로 사라지지 않게 한다.
        let mut restarted = false;
        if installed {
            use std::os::windows::process::CommandExt;
            match std::process::Command::new(&exe_path)
                .creation_flags(0x0800_0000)
                .spawn()
            {
                Ok(mut child) => {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                    if matches!(child.try_wait(), Ok(None)) {
                        restarted = true;
                    } else {
                        text.push_str(" — 새 앱이 시작되지 않았습니다. Kova 를 직접 실행하세요");
                    }
                }
                Err(e) => text.push_str(&format!(
                    " — 자동 재시작 실패({e}). Kova 를 직접 실행하세요"
                )),
            }
        }
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = weak.upgrade() {
                w.set_update_text(text.into());
            }
            if restarted {
                let _ = slint::quit_event_loop();
            }
        });
    });
}

/// 이미지 검증/복원: 오래 걸리므로 별도 스레드에서 돌리고 진행률을 화면에 흘린다. 읽기 전용으로 열며 복원은 새 파일에만 쓴다.
fn image_job(weak: slint::Weak<MainWindow>, image: String, target: Option<String>) {
    std::thread::spawn(move || {
        let show = |w: &slint::Weak<MainWindow>, t: String| {
            let w = w.clone();
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(w) = w.upgrade() {
                    w.set_image_text(t.into());
                }
            });
        };
        let cancel = std::sync::atomic::AtomicBool::new(false);
        let last =
            std::sync::Mutex::new(std::time::Instant::now() - std::time::Duration::from_secs(5));
        let w2 = weak.clone();
        let progress = |s: &kova_image::Stats| {
            let mut l = last.lock().unwrap();
            if l.elapsed() >= std::time::Duration::from_secs(1) {
                *l = std::time::Instant::now();
                show(
                    &w2,
                    format!("진행 중… {:.1}GB 처리", s.raw_bytes as f64 / 1e9),
                );
            }
        };
        let res: Result<String, String> = (|| {
            let img = std::path::Path::new(&image);
            match &target {
                None => {
                    let f = std::fs::File::open(img).map_err(|e| e.to_string())?;
                    let (h, s) = kova_image::verify(
                        std::io::BufReader::with_capacity(8 << 20, f),
                        &cancel,
                        &progress,
                    )
                    .map_err(|e| e.to_string())?;
                    Ok(format!(
                        "검증 통과: 청크 {}개, 원본 {:.2}GB, 클러스터 {}/{}{}",
                        s.chunks,
                        s.raw_bytes as f64 / 1e9,
                        s.clusters,
                        h.used_clusters,
                        if s.empty_chunks > 0 {
                            format!(
                                " (비어 있는 청크 {}개: 그 구간은 복원되지 않음)",
                                s.empty_chunks
                            )
                        } else {
                            String::new()
                        }
                    ))
                }
                Some(t) => {
                    let (_, s) = kova_image::restore::restore_image_to_file(
                        img,
                        std::path::Path::new(t),
                        &cancel,
                        &progress,
                    )
                    .map_err(|e| e.to_string())?;
                    Ok(format!(
                        "복원 완료: {t} (청크 {}개, {:.2}GB)",
                        s.chunks,
                        s.raw_bytes as f64 / 1e9
                    ))
                }
            }
        })();
        show(&weak, res.unwrap_or_else(|e| format!("실패: {e}")));
    });
}

/// 파일 원본을 .qblk 로 만든다(읽기 전용 원본, 새 파일만 출력). 오래 걸리므로 별도 스레드에서 진행률을 흘린다.
fn capture_job(weak: slint::Weak<MainWindow>, source: String, out: String) {
    std::thread::spawn(move || {
        let show = |w: &slint::Weak<MainWindow>, t: String| {
            let w = w.clone();
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(w) = w.upgrade() {
                    w.set_image_text(t.into());
                }
            });
        };
        let clean = |s: &str| s.trim().trim_matches('"').trim().to_string();
        let (source, out) = (clean(&source), clean(&out));
        if source.is_empty() || out.is_empty() {
            show(
                &weak,
                "원본 파일과 만들 .qblk 경로를 모두 입력하세요".into(),
            );
            return;
        }
        let cancel = std::sync::atomic::AtomicBool::new(false);
        let last =
            std::sync::Mutex::new(std::time::Instant::now() - std::time::Duration::from_secs(5));
        let w2 = weak.clone();
        let progress = |s: &kova_image::Stats| {
            let mut l = last.lock().unwrap();
            if l.elapsed() >= std::time::Duration::from_secs(1) {
                *l = std::time::Instant::now();
                show(
                    &w2,
                    format!("만드는 중… 사용 구간 {:.2}GB", s.raw_bytes as f64 / 1e9),
                );
            }
        };
        show(&weak, "만들기 시작…".into());
        let label = std::path::Path::new(&source)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let text = match kova_image::capture::capture_file(
            std::path::Path::new(&source),
            std::path::Path::new(&out),
            4096,
            3,
            &label,
            &cancel,
            &progress,
        ) {
            Ok((h, s)) => format!(
                "만들기 완료: {out} (전체 {}클러스터 중 사용 {}, 청크 {}개)",
                h.total_clusters, h.used_clusters, s.chunks
            ),
            Err(e) => format!("만들지 않음: {e}"),
        };
        show(&weak, text);
    });
}

/// 스냅샷 복원: 새 폴더(또는 빈 폴더)로만 푼다. 대상은 먼저 검사하고, 복원은 별도 스레드에서 진행률을 흘린다.
/// 스냅샷 파일 탐색 패널의 상태. 목록은 백그라운드에서 읽고, 선택은 경로(복원 폴더 기준 상대 경로) 집합으로 들고 있다.
#[derive(Default)]
struct BrowseState {
    repo: String,
    sid: String,
    dir: String,
    query: String,
    items: Vec<kova_engine::ops::BrowseItem>,
    sel: std::collections::BTreeSet<String>,
    seq: u64,
}
type Browse = std::sync::Arc<std::sync::Mutex<BrowseState>>;

const BROWSE_LIMIT: usize = 500;

fn browse_info(it: &kova_engine::ops::BrowseItem) -> String {
    if it.is_dir {
        format!("파일 {}개 · {}", it.files, human_mb(it.size))
    } else {
        human_mb(it.size)
    }
}

fn browse_render(weak: &slint::Weak<MainWindow>, st: &Browse, status: String) {
    let (rows, dir, n) = {
        let s = st.lock().unwrap();
        let rows: Vec<BrowseRow> = s
            .items
            .iter()
            .map(|it| BrowseRow {
                name: if it.is_dir {
                    format!("▸ {}", it.name)
                } else {
                    it.name.clone()
                }
                .into(),
                path: it.path.clone().into(),
                is_dir: it.is_dir,
                info: browse_info(it).into(),
                selected: s.sel.contains(&it.path),
            })
            .collect();
        let dir = if s.query.is_empty() {
            format!("/{}", s.dir)
        } else {
            format!("검색: {}", s.query)
        };
        (rows, dir, s.sel.len() as i32)
    };
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(w) = w.upgrade() {
            w.set_browse_items(std::rc::Rc::new(slint::VecModel::from(rows)).into());
            w.set_browse_dir(dir.into());
            w.set_browse_selected(n);
            w.set_browse_status(status.into());
        }
    });
}

/// 폴더(또는 검색어)를 읽어 목록을 갱신한다. 큰 스냅샷은 시간이 걸리므로 별도 스레드에서 한다.
fn browse_load(weak: slint::Weak<MainWindow>, st: Browse, dir: String, query: String) {
    let (repo, sid, seq) = {
        let mut s = st.lock().unwrap();
        s.seq += 1;
        (s.repo.clone(), s.sid.clone(), s.seq)
    };
    browse_render(&weak, &st, "읽는 중…".into());
    std::thread::spawn(move || {
        let res = kova_engine::ops::browse_snapshot(
            std::path::Path::new(&repo),
            &sid,
            &dir,
            &query,
            BROWSE_LIMIT,
        );
        let status = {
            let mut s = st.lock().unwrap();
            if s.seq != seq {
                return; // 그 사이 다른 폴더를 열었다
            }
            match res {
                Ok(items) => {
                    let status = if items.is_empty() {
                        "항목이 없습니다".to_string()
                    } else if items.len() >= BROWSE_LIMIT {
                        format!("처음 {BROWSE_LIMIT}개만 표시합니다. 검색으로 좁혀 보세요")
                    } else {
                        String::new()
                    };
                    s.dir = dir;
                    s.query = query;
                    s.items = items;
                    status
                }
                Err(e) => format!("목록을 읽지 못했습니다: {e}"),
            }
        };
        browse_render(&weak, &st, status);
    });
}

fn parent_dir(dir: &str) -> String {
    dir.rsplit_once('/')
        .map(|(a, _)| a.to_string())
        .unwrap_or_default()
}

/// 스냅샷을 원래 위치로 되돌린다(같은 이름의 파일은 덮어쓴다). 화면에서 두 번 확인한 뒤에만 호출된다.
fn restore_inplace_job(
    weak: slint::Weak<MainWindow>,
    repo: String,
    sid: String,
    only: Option<Vec<String>>,
) {
    let show = |w: &slint::Weak<MainWindow>, t: String| {
        let w = w.clone();
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = w.upgrade() {
                w.set_restore_text(t.into());
            }
        });
    };
    show(&weak, "원래 위치로 복원 중…".into());
    std::thread::spawn(move || {
        let repo_path = std::path::PathBuf::from(&repo);
        let _lock = match kova_core::lock::RepoLock::acquire_wait(
            &repo_path,
            std::time::Duration::from_secs(5),
        ) {
            Ok(l) => l,
            Err(kova_core::lock::LockError::Busy) => {
                show(
                    &weak,
                    "복원 실패: 다른 작업이 저장소를 사용 중입니다. 잠시 후 다시 시도하세요".into(),
                );
                return;
            }
            Err(e) => {
                show(&weak, format!("복원 실패: 저장소 잠금 오류({e})"));
                return;
            }
        };
        let opts = kova_engine::ops::RestoreOptions {
            repo: repo_path,
            session_id: sid,
            only,
            target: None,
        };
        let cancel = std::sync::atomic::AtomicBool::new(false);
        let text = match kova_engine::ops::run_restore(&opts, &|_| {}, &cancel) {
            Ok(s) => format!(
                "원래 위치로 복원 완료 (항목 {}개, {:.0}초)",
                s.total_files, s.elapsed_seconds
            ),
            Err(e) => format!("복원 실패: {e}"),
        };
        show(&weak, text);
    });
}

fn restore_snapshot_job(
    weak: slint::Weak<MainWindow>,
    repo: String,
    sid: String,
    target: String,
    only: Option<Vec<String>>,
) {
    let show = |w: &slint::Weak<MainWindow>, t: String| {
        let w = w.clone();
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = w.upgrade() {
                w.set_restore_text(t.into());
            }
        });
    };
    let repo_path = std::path::PathBuf::from(&repo);
    let target = match kova_core::restore_target::check(&target, &repo_path) {
        Ok(t) => t,
        Err(e) => {
            show(&weak, format!("복원하지 않음: {e}"));
            return;
        }
    };
    show(&weak, "복원 시작…".into());
    std::thread::spawn(move || {
        let _lock = match kova_core::lock::RepoLock::acquire_wait(
            &repo_path,
            std::time::Duration::from_secs(5),
        ) {
            Ok(l) => l,
            Err(kova_core::lock::LockError::Busy) => {
                show(
                    &weak,
                    "복원 실패: 다른 작업이 저장소를 사용 중입니다. 잠시 후 다시 시도하세요".into(),
                );
                return;
            }
            Err(e) => {
                show(&weak, format!("복원 실패: 저장소 잠금 오류({e})"));
                return;
            }
        };
        let last =
            std::sync::Mutex::new(std::time::Instant::now() - std::time::Duration::from_secs(5));
        let w2 = weak.clone();
        let on_event = move |ev: kova_engine::ops::Event| {
            if let kova_engine::ops::Event::RestoreProgress { done, total } = ev {
                let mut l = last.lock().unwrap();
                if l.elapsed() >= std::time::Duration::from_secs(1) {
                    *l = std::time::Instant::now();
                    let w = w2.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(w) = w.upgrade() {
                            w.set_restore_text(format!("복원 중… {done}/{total}").into());
                        }
                    });
                }
            }
        };
        let opts = kova_engine::ops::RestoreOptions {
            repo: repo_path,
            session_id: sid,
            only,
            target: Some(target.clone()),
        };
        let cancel = std::sync::atomic::AtomicBool::new(false);
        let text = match kova_engine::ops::run_restore(&opts, &on_event, &cancel) {
            Ok(s) => format!(
                "복원 완료: {} (항목 {}개, {:.0}초)",
                target.display(),
                s.total_files,
                s.elapsed_seconds
            ),
            Err(e) => format!("복원 실패: {e}"),
        };
        show(&weak, text);
    });
}

fn show_volume(weak: &slint::Weak<MainWindow>, t: String) {
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(w) = w.upgrade() {
            w.set_volume_text(t.into());
        }
    });
}

/// 볼륨 직접 복원에서 대상이 될 수 없는 드라이브: 모든 프로파일의 백업 저장소가 있는 드라이브.
#[cfg(windows)]
fn protected_drive_letters() -> Vec<char> {
    let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
    store
        .profiles()
        .iter()
        .filter_map(|p| {
            let b = p.repo_dir.as_bytes();
            (b.len() >= 2 && b[1] == b':' && b[0].is_ascii_alphabetic()).then(|| b[0] as char)
        })
        .collect()
}

/// 대상 볼륨을 조회해 사용자가 직접 입력해야 할 확인 문구를 보여 준다(아무것도 쓰지 않는다).
#[cfg(windows)]
fn volume_check(weak: &slint::Weak<MainWindow>, letter: &str) {
    let text = match letter.trim().chars().next() {
        None => "드라이브 문자를 입력하세요.".to_string(),
        Some(c) => match kova_image::volume_restore::volume_info(c) {
            Ok(v) => format!(
                "{}: 볼륨 '{}' 의 내용이 전부 덮어써집니다. 계속하려면 아래 칸에 그대로 입력하세요:\n{}",
                v.letter,
                v.label,
                kova_image::volume_restore::confirmation_phrase(&v)
            ),
            Err(e) => format!("대상으로 사용할 수 없음: {e}"),
        },
    };
    show_volume(weak, text);
}

/// 볼륨 직접 복원: 검증·정책·확인 문구·잠금을 모두 통과해야 쓴다. 오래 걸리므로 별도 스레드에서 진행률을 흘린다.
#[cfg(windows)]
fn restore_volume_job(weak: slint::Weak<MainWindow>, image: String, letter: String, typed: String) {
    let Some(c) = letter.trim().chars().next() else {
        show_volume(&weak, "드라이브 문자를 입력하세요.".into());
        return;
    };
    show_volume(
        &weak,
        "이미지 검증 중… (검증이 끝나기 전에는 볼륨에 쓰지 않습니다)".into(),
    );
    std::thread::spawn(move || {
        let cancel = std::sync::atomic::AtomicBool::new(false);
        let last =
            std::sync::Mutex::new(std::time::Instant::now() - std::time::Duration::from_secs(5));
        let w2 = weak.clone();
        let progress = |s: &kova_image::Stats| {
            let mut l = last.lock().unwrap();
            if l.elapsed() >= std::time::Duration::from_secs(1) {
                *l = std::time::Instant::now();
                show_volume(
                    &w2,
                    format!("복원 중… {:.1}GB 기록", s.raw_bytes as f64 / 1e9),
                );
            }
        };
        let r = kova_image::volume_restore::restore_image_to_volume(
            std::path::Path::new(image.trim().trim_matches('"')),
            c,
            &typed,
            &protected_drive_letters(),
            &cancel,
            &progress,
        );
        let text = match r {
            Ok((_, s)) => format!(
                "복원 완료: {}: ({:.2}GB 기록). 볼륨이 마운트 해제됐으므로 탐색기에서 다시 열어 확인하세요.",
                c.to_ascii_uppercase(),
                s.raw_bytes as f64 / 1e9
            ),
            Err(kova_image::volume_restore::VolumeError::Io(e)) if e.raw_os_error() == Some(5) => {
                "복원하지 않음: 액세스가 거부됐습니다. Kova 앱을 관리자 권한으로 실행해야 합니다.".into()
            }
            Err(e) => format!("복원하지 않음: {e}"),
        };
        show_volume(&weak, text);
    });
}

fn main() -> Result<(), slint::PlatformError> {
    kova_core::repocrypto::install_provider(); // 암호화된 저장소의 탐색·복원에 필요
                                               // 관리자 권한으로 다시 실행된 업데이트 설치 도우미: 설치하고 결과를 파일에 적은 뒤 끝낸다
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--install-update") {
        let (text, installed) =
            do_install().unwrap_or_else(|e| (format!("설치하지 않음: {e}"), false));
        if let Some(f) = args.get(i + 1) {
            let _ = std::fs::write(f, encode_install_result(installed, &text));
        }
        return Ok(());
    }

    let w = MainWindow::new()?;
    #[cfg(windows)]
    {
        let weak = w.as_weak();
        w.on_volume_check(move |l| volume_check(&weak, l.as_str()));
        let weak = w.as_weak();
        w.on_restore_volume(move |img, l, t| {
            restore_volume_job(weak.clone(), img.to_string(), l.to_string(), t.to_string())
        });
    }
    w.set_app_version(env!("CARGO_PKG_VERSION").into());
    w.set_wz_repo(known_folder("backup").into());
    w.set_default_restore(known_folder("restore").into());
    w.on_known_folder(|k| known_folder(&k).into());
    {
        let weak = w.as_weak();
        w.on_wz_add(move |paths| {
            let Some(w) = weak.upgrade() else { return };
            let mut list = wizard_sources(&w);
            for p in paths.split(';').map(str::trim).filter(|s| !s.is_empty()) {
                if !list.iter().any(|x| x.eq_ignore_ascii_case(p)) {
                    list.push(p.to_string());
                }
            }
            set_wizard_sources(&w, &list);
        });
        let weak = w.as_weak();
        w.on_sync_form_list(move |s| {
            let Some(w) = weak.upgrade() else { return };
            let list: Vec<SrcRow> = source_rows(&s)
                .into_iter()
                .map(|(kind, title, sub, key)| SrcRow {
                    kind,
                    title: title.into(),
                    sub: sub.into(),
                    key: key.into(),
                })
                .collect();
            w.set_form_list(std::rc::Rc::new(slint::VecModel::from(list)).into());
            let missing = s
                .split(';')
                .map(str::trim)
                .filter(|x| !x.is_empty() && !std::path::Path::new(x).exists())
                .count();
            w.set_form_missing(missing as i32);
        });
        let weak = w.as_weak();
        w.on_form_clean(move || {
            let Some(w) = weak.upgrade() else { return };
            let keep: Vec<String> = w
                .get_form_sources()
                .split(';')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty() && std::path::Path::new(s).exists())
                .collect();
            w.set_form_sources(keep.join(";").into());
        });
        let weak = w.as_weak();
        w.on_form_add(move |paths| {
            let Some(w) = weak.upgrade() else { return };
            let mut list: Vec<String> = w
                .get_form_sources()
                .split(';')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            for p in paths.split(';').map(str::trim).filter(|s| !s.is_empty()) {
                if !list.iter().any(|x| x.eq_ignore_ascii_case(p)) {
                    list.push(p.to_string());
                }
            }
            w.set_form_sources(list.join(";").into());
        });
        let weak = w.as_weak();
        w.on_form_remove(move |key| {
            let Some(w) = weak.upgrade() else { return };
            let list: Vec<String> = w
                .get_form_sources()
                .split(';')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .filter(|s| match key.strip_prefix("prog:") {
                    Some(k) => program_of(s).map(|x| x.0).as_deref() != Some(k),
                    None => !s.eq_ignore_ascii_case(&key),
                })
                .collect();
            w.set_form_sources(list.join(";").into());
        });
        type Picks = std::rc::Rc<std::cell::RefCell<Vec<(String, Vec<String>, bool)>>>;
        let picks: Picks = Default::default();
        let show_picks = |w: &MainWindow, p: &[(String, Vec<String>, bool)]| {
            let rows: Vec<ProgPick> = p
                .iter()
                .map(|(n, paths, c)| ProgPick {
                    name: n.as_str().into(),
                    sub: format!("{}개 항목", paths.len()).into(),
                    checked: *c,
                })
                .collect();
            w.set_prog_count(p.iter().filter(|x| x.2).count() as i32);
            w.set_prog_list(std::rc::Rc::new(slint::VecModel::from(rows)).into());
        };
        let (weak, pk) = (w.as_weak(), picks.clone());
        w.on_prog_open(move || {
            let Some(w) = weak.upgrade() else { return };
            let have: std::collections::HashSet<String> = w
                .get_form_sources()
                .split(';')
                .filter_map(|s| program_of(s).map(|x| x.0))
                .collect();
            let list: Vec<(String, Vec<String>, bool)> = installed_programs()
                .into_iter()
                .filter(|(n, _)| !have.contains(&prog_norm(n)))
                .map(|(n, p)| (n, p, false))
                .collect();
            show_picks(&w, &list);
            *pk.borrow_mut() = list;
            w.set_prog_shown(true);
        });
        let (weak, pk) = (w.as_weak(), picks.clone());
        w.on_prog_toggle(move |i| {
            let Some(w) = weak.upgrade() else { return };
            let mut list = pk.borrow_mut();
            if let Some(x) = list.get_mut(i as usize) {
                x.2 = !x.2;
            }
            show_picks(&w, &list);
        });
        let (weak, pk) = (w.as_weak(), picks.clone());
        w.on_prog_apply(move || {
            let Some(w) = weak.upgrade() else { return };
            let mut cur: Vec<String> = w
                .get_form_sources()
                .split(';')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            for (_, paths, checked) in pk.borrow().iter() {
                if *checked {
                    for p in paths {
                        if !cur.iter().any(|x| x.eq_ignore_ascii_case(p)) {
                            cur.push(p.clone());
                        }
                    }
                }
            }
            w.set_form_sources(cur.join(";").into());
            w.set_prog_shown(false);
        });
        let weak = w.as_weak();
        w.on_prog_close(move || {
            if let Some(w) = weak.upgrade() {
                w.set_prog_shown(false);
            }
        });
        let weak = w.as_weak();
        w.on_wz_remove(move |i| {
            let Some(w) = weak.upgrade() else { return };
            let mut list = wizard_sources(&w);
            if (i as usize) < list.len() {
                list.remove(i as usize);
            }
            set_wizard_sources(&w, &list);
        });
    }
    {
        let weak = w.as_weak();
        w.on_quick_start(move |srcs, repo| {
            let text = match quick_start(srcs.trim(), repo.trim()) {
                Ok(_) => {
                    "설정했습니다. 백업을 시작합니다. 진행 상황은 아래에 표시됩니다.".to_string()
                }
                Err(e) => format!("시작하지 못했습니다: {e}"),
            };
            if let Some(w) = weak.upgrade() {
                w.set_wz_text(text.into());
            }
        });
    }
    w.on_run_backup(|id| send("backup", id.to_string()));
    w.on_run_verify(|id| send("verify", id.to_string()));
    w.on_open_repo_dir(|dir| {
        let dir_str = dir.to_string();
        std::thread::spawn(move || {
            let target = if dir_str.is_empty() {
                ".".to_string()
            } else {
                dir_str
            };
            #[cfg(windows)]
            {
                let _ = std::process::Command::new("explorer.exe")
                    .arg(&target)
                    .spawn();
            }
        });
    });
    w.on_save_offsite(|id, dir| save_offsite(&id, &dir));
    w.set_gdrive_connected(kova_core::gdrive::is_connected(
        &kova_service::default_config_dir(),
    ));
    {
        let weak = w.as_weak();
        w.on_connect_gdrive(move || {
            let Some(win) = weak.upgrade() else { return };
            if app_locked() {
                win.set_gdrive_text(LOCKED_MSG.into());
                return;
            }
            let dir = kova_service::default_config_dir();
            let creds = match kova_core::gdrive::load_client(&dir) {
                Ok(c) => c,
                Err(e) => {
                    win.set_gdrive_text(e.into());
                    return;
                }
            };
            win.set_gdrive_text("브라우저에서 구글 로그인을 완료해 주세요(5분 안에).".into());
            let weak = weak.clone();
            std::thread::spawn(move || {
                let cancel = std::sync::atomic::AtomicBool::new(false);
                let r = kova_core::gdrive::login(
                    &kova_core::gdrive::Endpoints::default(),
                    &creds,
                    &dir,
                    open_url,
                    &cancel,
                    std::time::Duration::from_secs(300),
                );
                if r.is_ok() {
                    set_offsite_provider("gdrive");
                }
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        match r {
                            Ok(()) => {
                                w.set_gdrive_connected(true);
                                w.set_gdrive_text(String::new().into());
                            }
                            Err(e) => w.set_gdrive_text(format!("연결 실패: {e}").into()),
                        }
                    }
                });
            });
        });
    }
    {
        let weak = w.as_weak();
        w.on_disconnect_gdrive(move || {
            let Some(win) = weak.upgrade() else { return };
            if app_locked() {
                win.set_gdrive_text(LOCKED_MSG.into());
                return;
            }
            kova_core::gdrive::disconnect(
                &kova_service::default_config_dir(),
                &kova_core::gdrive::Endpoints::default(),
            );
            set_offsite_provider("local");
            win.set_gdrive_connected(false);
            win.set_gdrive_text("연결을 해제했습니다.".into());
        });
    }
    w.on_cancel_job(cancel_job);
    {
        let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
        let s = store.settings();
        w.set_form_min_free(s.min_free_disk_gb.to_string().into());
        w.set_form_ransom(s.enable_ransomware_protection);
        w.set_form_encrypt(s.encrypt_backups);
        w.set_form_vss(s.use_vss);
        w.set_form_pack(s.pack_blobs);
        w.set_form_webhook(s.webhook_url.into());
        w.set_form_max_threads(s.max_cpu_threads.to_string().into());
        w.set_form_bw_limit(s.offsite_bw_limit_mbps.to_string().into());
        w.set_form_pause_battery(s.pause_on_battery);
        w.set_lock_text(lock_status().into());
    }
    {
        let weak = w.as_weak();
        w.on_save_settings(move |gb, ransom, enc, vss, pack| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let msg = if app_locked() {
                LOCKED_MSG.to_string()
            } else {
                match apply_settings(&store, &gb, ransom, enc, vss, pack) {
                    Ok(m) | Err(m) => m,
                }
            };
            if let Some(w) = weak.upgrade() {
                w.set_settings_text(msg.into());
            }
        });
    }
    {
        let weak = w.as_weak();
        w.on_export_sysinfo(move || {
            let weak = weak.clone();
            if let Some(w) = weak.upgrade() {
                w.set_diag_text("설치된 프로그램·드라이버 목록을 모으는 중…".into());
            }
            std::thread::spawn(move || {
                let base = std::env::var_os("USERPROFILE")
                    .map(|u| std::path::PathBuf::from(u).join("Desktop"))
                    .filter(|d| d.is_dir())
                    .unwrap_or_else(std::env::temp_dir);
                let secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let text = match kova_core::sysinfo::export(
                    &base.join(format!("Kova2_시스템정보_{secs}")),
                ) {
                    Ok(m) | Err(m) => m,
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_diag_text(text.into());
                    }
                });
            });
        });
    }
    {
        let weak = w.as_weak();
        w.on_run_drill(move || {
            let weak = weak.clone();
            if let Some(w) = weak.upgrade() {
                w.set_diag_text(
                    "복구 훈련 중… (최근 스냅샷을 임시 폴더에 복원해 확인합니다)".into(),
                );
            }
            std::thread::spawn(move || {
                let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
                let scratch = std::env::temp_dir();
                let lines: Vec<String> = store
                    .profiles()
                    .iter()
                    .map(|p| kova_core::drill::run(p, &scratch))
                    .collect();
                let text = if lines.is_empty() {
                    "프로파일이 없습니다".to_string()
                } else {
                    lines.join("\n")
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_diag_text(text.into());
                    }
                });
            });
        });
    }
    {
        let weak = w.as_weak();
        w.on_restore_key(move |code| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let msg = if app_locked() {
                LOCKED_MSG.to_string()
            } else {
                match restore_keys(&store, &code) {
                    Ok(m) | Err(m) => m,
                }
            };
            if let Some(w) = weak.upgrade() {
                w.set_settings_text(msg.into());
                w.set_form_recovery("".into());
            }
        });
        let weak = w.as_weak();
        w.on_export_recovery_kit(move || {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let msg = if app_locked() {
                LOCKED_MSG.to_string()
            } else {
                let base = std::env::var_os("USERPROFILE")
                    .map(|u| std::path::PathBuf::from(u).join("Desktop"))
                    .filter(|d| d.is_dir())
                    .unwrap_or_else(std::env::temp_dir);
                let secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let out = base.join(format!("Kova2_Recovery_Kit_{secs}.txt"));
                match export_recovery_kit_to(&store, &out) {
                    Ok(m) | Err(m) => m,
                }
            };
            if let Some(w) = weak.upgrade() {
                w.set_settings_text(msg.into());
            }
        });
        let weak = w.as_weak();
        w.on_save_ops_settings(move |wh, th, bw, bat| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let msg = if app_locked() {
                LOCKED_MSG.to_string()
            } else {
                match apply_ops_settings(&store, &wh, &th, &bw, bat) {
                    Ok(m) | Err(m) => m,
                }
            };
            if let Some(w) = weak.upgrade() {
                w.set_settings_text(msg.into());
            }
        });
        let weak = w.as_weak();
        w.on_test_webhook(move |wh| {
            let url = wh.to_string();
            let weak = weak.clone();
            if let Some(w) = weak.upgrade() {
                w.set_settings_text("웹훅 테스트 발송 중…".into());
            }
            std::thread::spawn(move || {
                let text = match kova_core::notify::send_webhook(
                    &url,
                    "info",
                    "테스트 알림",
                    "Kova2 원격 알림이 정상적으로 연결되었습니다.",
                ) {
                    Ok(()) => "웹훅 테스트 발송 성공".to_string(),
                    Err(e) => format!("웹훅 테스트 실패: {e}"),
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_settings_text(text.into());
                    }
                });
            });
        });
        let weak = w.as_weak();
        w.on_build_pe_iso(move |iso_out, drivers_dir| {
            let iso_out = iso_out.trim().to_string();
            let drivers_dir = drivers_dir.trim().to_string();
            let weak = weak.clone();
            if let Some(w) = weak.upgrade() {
                w.set_pe_text(
                    "WinPE 복구 ISO 제작 준비 중… (관리자 권한 승인이 필요합니다)".into(),
                );
            }
            std::thread::spawn(move || {
                let text = run_build_pe_iso(&iso_out, &drivers_dir);
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_pe_text(text.into());
                    }
                });
            });
        });
        let weak = w.as_weak();
        w.on_export_drivers(move || {
            let weak = weak.clone();
            if let Some(w) = weak.upgrade() {
                w.set_pe_text("현재 PC 드라이버 자동 수집(덤프) 중… (DISM)".into());
            }
            std::thread::spawn(move || {
                let text = run_export_drivers(None);
                let def = kova_pe::plan::default_driver_export_dir();
                let def_str = def.to_string_lossy().to_string();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_pe_text(text.clone().into());
                        w.set_diag_text(text.into());
                        w.set_pe_drivers_dir(def_str.into());
                    }
                });
            });
        });
        let weak = w.as_weak();
        w.on_global_search(move |q| {
            let query = q.trim().to_string();
            if query.is_empty() {
                if let Some(w) = weak.upgrade() {
                    w.set_global_results(slint::ModelRc::new(slint::VecModel::from(Vec::new())));
                    w.set_global_status("검색어를 입력하세요 (파일명 또는 초성)".into());
                }
                return;
            }
            if let Some(w) = weak.upgrade() {
                w.set_global_status("전체 스냅샷 검색 중…".into());
            }
            let weak = weak.clone();
            std::thread::spawn(move || {
                let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
                let found = search_all_repos(&store, &query);
                let count = found.len();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        let rows: Vec<GlobalFileRow> = found
                            .into_iter()
                            .map(|(filename, rel_path, repo, sid, vc, info)| GlobalFileRow {
                                filename: filename.into(),
                                rel_path: rel_path.into(),
                                repo: repo.into(),
                                snapshot_id: sid.into(),
                                versions: format!("{vc}개 버전").into(),
                                info: info.into(),
                            })
                            .collect();
                        w.set_global_results(slint::ModelRc::new(slint::VecModel::from(rows)));
                        w.set_global_status(
                            if count == 0 {
                                format!("'{query}' 에 일치하는 백업 파일이 없습니다")
                            } else {
                                format!("'{query}' 검색 결과 {count}건 (최신 시점 기준)")
                            }
                            .into(),
                        );
                    }
                });
            });
        });
        let weak = w.as_weak();
        w.on_global_restore(move |repo, sid, rel_path, target| {
            let only = Some(vec![rel_path.to_string()]);
            if target.as_str() == "@원래자리" {
                restore_inplace_job(weak.clone(), repo.to_string(), sid.to_string(), only);
            } else {
                restore_snapshot_job(
                    weak.clone(),
                    repo.to_string(),
                    sid.to_string(),
                    target.to_string(),
                    only,
                );
            }
        });
        // 앱 시작 시 1회 백그라운드로 새 버전 확인(최신이거나 오프라인이면 조용히 무시)
        check_update_silent(w.as_weak());
        let weak = w.as_weak();
        w.on_unlock_app(move |pw| {
            let ok = kova_core::applock::verify(&kova_service::default_config_dir(), &pw);
            if ok {
                UNLOCKED.store(true, std::sync::atomic::Ordering::SeqCst);
            }
            if let Some(w) = weak.upgrade() {
                w.set_form_lock_cur("".into());
                w.set_lock_text(
                    if ok {
                        lock_status()
                    } else {
                        "마스터 비밀번호가 맞지 않습니다".to_string()
                    }
                    .into(),
                );
            }
        });
        let weak = w.as_weak();
        w.on_set_master(move |cur, new| {
            let r = kova_core::applock::change(&kova_service::default_config_dir(), &cur, &new);
            // 새로 정했으면 방금 정한 사람이 쓰는 중이므로 이번 실행은 풀린 상태로 둔다
            let ok = r.is_ok();
            if ok {
                UNLOCKED.store(true, std::sync::atomic::Ordering::SeqCst);
            }
            if let Some(w) = weak.upgrade() {
                if ok {
                    w.set_form_lock_cur("".into());
                    w.set_form_lock_new("".into());
                }
                let msg = match r {
                    Ok(m) | Err(m) => m,
                };
                w.set_lock_text(msg.into());
            }
        });
    }
    {
        let weak = w.as_weak();
        w.on_make_diag(move || {
            let weak = weak.clone();
            std::thread::spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                let svc = match rt.block_on(kova_ipc::request(
                    kova_ipc::PIPE_NAME,
                    &kova_ipc::Request::Status,
                )) {
                    Ok(_) => "연결됨",
                    Err(_) => "연결 안 됨",
                };
                let dir = std::env::var_os("USERPROFILE")
                    .map(|u| std::path::PathBuf::from(u).join("Desktop"))
                    .filter(|d| d.is_dir())
                    .unwrap_or_else(std::env::temp_dir);
                let secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let path = dir.join(format!("Kova2_진단_{secs}.zip"));
                let msg = match kova_core::diag::bundle(
                    &kova_service::default_config_dir(),
                    env!("CARGO_PKG_VERSION"),
                    svc,
                    &path,
                ) {
                    Ok(()) => format!("저장했습니다: {}", path.display()),
                    Err(e) => format!("저장 실패: {e}"),
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_diag_text(msg.into());
                    }
                });
            });
        });
    }
    {
        let weak = w.as_weak();
        w.on_edit_profile(move |id| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            if let (Some(p), Some(w)) = (store.get(&id), weak.upgrade()) {
                w.set_form_id(p.id.clone().into());
                w.set_form_name(p.name.clone().into());
                w.set_form_sources(p.sources.join(";").into());
                w.set_form_repo(p.repo_dir.clone().into());
                w.set_form_count(p.retention_count.to_string().into());
                w.set_form_days(p.retention_days.to_string().into());
                w.set_form_auto(p.auto_backup_enabled);
                w.set_form_daily(p.schedule_type == "daily");
                w.set_form_sched(p.schedule_value.clone().into());
                w.set_form_exclude(p.exclude_patterns.join(";").into());
                let keep = |k: &str| p.extra.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
                w.set_form_weekly(keep("keep_weekly").to_string().into());
                w.set_form_monthly(keep("keep_monthly").to_string().into());
                w.set_form_text("".into());
            }
        });
        let weak = w.as_weak();
        w.on_estimate_size(move |id| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let Some(p) = store.get(&id) else {
                if let Some(w) = weak.upgrade() {
                    w.set_form_text("저장된 프로파일만 예측할 수 있습니다. 목록에서 수정을 누르거나 먼저 저장하세요".into());
                }
                return;
            };
            if let Some(w) = weak.upgrade() {
                w.set_form_text("원본을 훑는 중입니다…".into());
            }
            let weak = weak.clone();
            std::thread::spawn(move || {
                let text = kova_core::estimate::describe(&kova_core::estimate::estimate(&p));
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_form_text(text.into());
                    }
                });
            });
        });
        let weak = w.as_weak();
        w.on_save_profile(
            move |id,
                  name,
                  srcs,
                  repo,
                  count,
                  days,
                  auto,
                  daily,
                  sched,
                  exclude,
                  weekly,
                  monthly| {
                let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
                let all = store.profiles();
                let base = all.iter().find(|p| p.id == id.as_str());
                let new_id = format!(
                    "prof_{}",
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_millis())
                        .unwrap_or(0)
                );
                let built = kova_core::profile_form::build(
                    base, &new_id, &name, &srcs, &repo, &count, &days, &all,
                )
                .and_then(|p| {
                    kova_core::profile_form::apply_schedule(p, auto, daily, &sched, &exclude)
                })
                .and_then(|p| kova_core::profile_form::apply_keep(p, &weekly, &monthly));
                let msg = match built {
                    Ok(p) => match store.upsert(p.clone()) {
                        Ok(()) => {
                            if let Some(w) = weak.upgrade() {
                                w.set_form_id(p.id.into());
                            }
                            "저장했습니다".to_string()
                        }
                        Err(e) => format!("저장 실패: {e}"),
                    },
                    Err(e) => format!("저장하지 않음: {e}"),
                };
                if let Some(w) = weak.upgrade() {
                    w.set_form_text(msg.into());
                }
            },
        );
        let weak = w.as_weak();
        w.on_delete_profile(move |id| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let locked = app_locked();
            let msg = match if locked {
                Err(())
            } else {
                store.delete(&id).map_err(|_| ())
            } {
                Err(()) if locked => LOCKED_MSG,
                Ok(true) => "프로파일 설정을 삭제했습니다 (저장소의 백업 파일은 그대로입니다)",
                Ok(false) => "이미 없는 프로파일입니다",
                Err(_) => "삭제하지 못했습니다",
            };
            if let Some(w) = weak.upgrade() {
                if !locked && w.get_form_id() == id {
                    w.set_form_id("".into());
                }
                w.set_form_text(msg.into());
            }
        });
    }
    {
        let weak = w.as_weak();
        w.on_check_update(move || check_update(weak.clone()));
    }
    {
        let weak = w.as_weak();
        w.on_install_update(move || install_update(weak.clone()));
    }
    {
        let weak = w.as_weak();
        w.on_verify_image(move |p| image_job(weak.clone(), p.to_string(), None));
        let weak = w.as_weak();
        w.on_capture_image(move |s, o| {
            let (src, mut out) = (s.to_string(), o.to_string());
            if src.trim().is_empty() {
                if let Some(w) = weak.upgrade() {
                    w.set_image_text("위 목록에서 변환할 이미지를 먼저 고르세요".into());
                }
                return;
            }
            if out.trim().is_empty() {
                match auto_convert_target(&src) {
                    Some(t) => out = t,
                    None => {
                        if let Some(w) = weak.upgrade() {
                            w.set_image_text(
                                "프로파일이 없어 저장 위치를 정할 수 없습니다. 프로파일 탭에서 먼저 만드세요".into(),
                            );
                        }
                        return;
                    }
                }
            }
            capture_job(weak.clone(), src, out);
        });
        let weak = w.as_weak();
        w.on_scan_convert(move || scan_convert_async(weak.clone()));
        scan_convert_async(w.as_weak());
        let weak = w.as_weak();
        w.on_delete_image(move |p| {
            let msg = match delete_image_file(p.as_str()) {
                Ok(()) => "이미지를 삭제했습니다".to_string(),
                Err(e) => e,
            };
            if let Some(w) = weak.upgrade() {
                if w.get_img_path() == p {
                    w.set_img_path("".into());
                }
                w.set_image_text(msg.into());
            }
        });
        let weak = w.as_weak();
        w.on_capture_volume(move |d| {
            let d = d.trim().trim_end_matches([':', '\\']).to_string();
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let text = match (d.len() == 1 && d.as_bytes()[0].is_ascii_alphabetic(), store.profiles().first()) {
                (false, _) => "드라이브 문자 한 글자를 입력하세요 (예: F)".to_string(),
                (_, None) => "프로파일이 없습니다. 프로파일 탭에서 먼저 만드세요".to_string(),
                (true, Some(p)) => {
                    send(format!("image:{}", d.to_ascii_uppercase()), p.id.clone());
                    format!("{}: 이미지 작업을 서비스에 요청했습니다. 진행·결과는 홈의 작업 기록에 표시됩니다", d.to_ascii_uppercase())
                }
            };
            if let Some(w) = weak.upgrade() {
                w.set_image_text(text.into());
            }
        });
        let weak = w.as_weak();
        w.on_restore_image(move |p, t| {
            let (p, mut t) = (p.to_string(), t.trim().to_string());
            if t.is_empty() {
                // 비워 두면 이미지 옆에 `<이름>_restored.img` 로 자동 지정(기존 파일은 덮어쓰지 않으므로 번호를 붙인다)
                let src = std::path::Path::new(&p);
                let stem = src.file_stem().and_then(|s| s.to_str()).unwrap_or("image");
                let dir = src.parent().unwrap_or(std::path::Path::new("."));
                let mut cand = dir.join(format!("{stem}_restored.img"));
                let mut n = 2;
                while cand.exists() {
                    cand = dir.join(format!("{stem}_restored_{n}.img"));
                    n += 1;
                }
                t = cand.to_string_lossy().to_string();
            }
            image_job(weak.clone(), p, Some(t));
        });
    }
    {
        let weak = w.as_weak();
        w.on_restore_snapshot(move |repo, sid, t| {
            restore_snapshot_job(
                weak.clone(),
                repo.to_string(),
                sid.to_string(),
                t.to_string(),
                None,
            )
        });
    }
    {
        let weak = w.as_weak();
        w.on_browse(move |k| {
            let (path, err) = match pick_path(k.as_str()) {
                Ok(p) => (p, String::new()),
                Err(e) => (String::new(), format!("선택 창을 열지 못했습니다: {e}")),
            };
            if let Some(w) = weak.upgrade() {
                w.set_pick_error(err.into());
            }
            path.into()
        });
    }
    {
        let st: Browse = Default::default();
        let weak = w.as_weak();
        let s2 = st.clone();
        w.on_open_browse(move |repo, sid, title| {
            {
                let mut s = s2.lock().unwrap();
                let seq = s.seq;
                *s = BrowseState {
                    repo: repo.to_string(),
                    sid: sid.to_string(),
                    seq,
                    ..Default::default()
                };
            }
            if let Some(w) = weak.upgrade() {
                w.set_browse_title(title);
            }
            browse_load(weak.clone(), s2.clone(), String::new(), String::new());
        });
        let (weak, s2) = (w.as_weak(), st.clone());
        w.on_browse_go(move |d| {
            browse_load(weak.clone(), s2.clone(), d.to_string(), String::new())
        });
        let (weak, s2) = (w.as_weak(), st.clone());
        w.on_browse_up(move || {
            let d = parent_dir(&s2.lock().unwrap().dir);
            browse_load(weak.clone(), s2.clone(), d, String::new());
        });
        let (weak, s2) = (w.as_weak(), st.clone());
        w.on_browse_search(move |q| {
            let dir = s2.lock().unwrap().dir.clone();
            browse_load(weak.clone(), s2.clone(), dir, q.trim().to_string());
        });
        let (weak, s2) = (w.as_weak(), st.clone());
        w.on_browse_toggle(move |p| {
            {
                let mut s = s2.lock().unwrap();
                if !s.sel.remove(p.as_str()) {
                    s.sel.insert(p.to_string());
                }
            }
            browse_render(&weak, &s2, String::new());
        });
        let (weak, s2) = (w.as_weak(), st);
        w.on_browse_restore(move |t| {
            let (repo, sid, sel) = {
                let s = s2.lock().unwrap();
                (
                    s.repo.clone(),
                    s.sid.clone(),
                    s.sel.iter().cloned().collect::<Vec<_>>(),
                )
            };
            if sel.is_empty() {
                if let Some(w) = weak.upgrade() {
                    w.set_restore_text("복원할 파일이나 폴더를 먼저 선택하세요".into());
                }
                return;
            }
            // 대상 폴더가 비어 있으면 "원래 자리로 되돌리기"(화면에서 덮어쓰기를 두 번 확인한 뒤에만 비워 보낸다)
            if t.as_str() == "@원래자리" {
                if app_locked() {
                    if let Some(w) = weak.upgrade() {
                        w.set_restore_text(LOCKED_MSG.into());
                    }
                    return;
                }
                restore_inplace_job(weak.clone(), repo, sid, Some(sel));
            } else {
                restore_snapshot_job(weak.clone(), repo, sid, t.to_string(), Some(sel));
            }
        });
    }
    w.on_run_gc(|id| send("gc", id.to_string()));
    {
        let weak = w.as_weak();
        w.on_restore_inplace(move |repo, sid| {
            if app_locked() {
                if let Some(w) = weak.upgrade() {
                    w.set_restore_text(LOCKED_MSG.into());
                }
                return;
            }
            restore_inplace_job(weak.clone(), repo.to_string(), sid.to_string(), None)
        });
        let weak = w.as_weak();
        w.on_delete_snapshot(move |repo, sid| {
            if app_locked() {
                if let Some(w) = weak.upgrade() {
                    w.set_restore_text(LOCKED_MSG.into());
                }
                return;
            }
            let (repo, sid) = (repo.to_string(), sid.to_string());
            let weak = weak.clone();
            std::thread::spawn(move || {
                // 지우기는 서비스(SYSTEM)에 맡긴다: 백업 파일에는 일반 사용자가 풀 수 없는 보호(ACL)가 걸려 있다
                let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
                let pid = store
                    .profiles()
                    .into_iter()
                    .find(|p| p.repo_dir.eq_ignore_ascii_case(&repo))
                    .map(|p| p.id);
                let via_service = pid.and_then(|pid| {
                    let rt = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .ok()?;
                    rt.block_on(kova_ipc::request(
                        kova_ipc::PIPE_NAME,
                        &kova_ipc::Request::RunNow {
                            profile_id: pid,
                            kind: format!("delsnap:{sid}"),
                        },
                    ))
                    .ok()
                });
                let text = if via_service.is_some() {
                    "삭제를 요청했습니다. 잠시 뒤 목록에서 사라집니다(실패하면 알림에 표시됩니다)"
                        .to_string()
                } else {
                    // 서비스가 꺼져 있으면 이 창에서 직접 시도한다(보호가 걸려 있으면 거부될 수 있다)
                    match kova_core::retention::delete_snapshot(std::path::Path::new(&repo), &sid) {
                        Ok(m) | Err(m) => m,
                    }
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = weak.upgrade() {
                        w.set_restore_text(text.into());
                    }
                });
            });
        });
    }
    w.on_ack_alerts(|| {
        let _ =
            kova_core::alerts::AlertLog::new(&kova_service::default_config_dir()).acknowledge_all();
    });
    spawn_ipc(w.as_weak());

    let menu = Menu::new();
    let open = MenuItem::new("열기", true, None);
    let backup_all = MenuItem::new("지금 백업 (모든 프로파일)", true, None);
    let quit = MenuItem::new("종료", true, None);
    menu.append_items(&[&open, &backup_all, &quit]).unwrap();
    let _tray = match TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_tooltip("Kova")
        .with_icon(make_icon())
        .build()
    {
        Ok(t) => Some(t),
        Err(e) => {
            eprintln!("트레이 아이콘 생성 건너뜀: {e}");
            None
        }
    };

    // 트레이 메뉴 이벤트를 Slint 이벤트 루프에서 주기적으로 처리
    let weak = w.as_weak();
    let (open_id, quit_id, backup_id) = (
        open.id().clone(),
        quit.id().clone(),
        backup_all.id().clone(),
    );
    let timer = slint::Timer::default();
    timer.start(
        slint::TimerMode::Repeated,
        std::time::Duration::from_millis(100),
        move || {
            while let Ok(ev) = MenuEvent::receiver().try_recv() {
                if ev.id == open_id {
                    if let Some(w) = weak.upgrade() {
                        let _ = w.show();
                    }
                } else if ev.id == backup_id {
                    let store =
                        kova_core::config::ConfigStore::new(kova_service::default_config_dir());
                    for p in store.profiles() {
                        send("backup", p.id);
                    }
                } else if ev.id == quit_id {
                    let _ = slint::quit_event_loop();
                }
            }
        },
    );

    // --tray: 로그인 자동 실행용. 창을 띄우지 않고 트레이로만 시작하며, 창을 닫아도 종료하지 않는다(트레이 "종료"로 끝낸다)
    if std::env::args().any(|a| a == "--tray") {
        slint::run_event_loop_until_quit()
    } else {
        let _ = w.show();
        w.run()
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn source_rows_groups_programs_by_name_and_keeps_folders() {
        let s = "C:\\Users\\a\\Desktop;C:\\Users\\a\\Desktop\\sub;C:\\Users\\a\\AppData\\Local\\Temp\\keep;\
                 C:\\Program Files\\Tailscale;C:\\Users\\a\\AppData\\Local\\Tailscale;C:\\Users\\a\\AppData\\Roaming\\tailscale;\
                 C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Tailscale.lnk;\
                 C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Git\\Git Bash.lnk;\
                 C:\\Users\\a\\AppData\\Local\\antigravity-updater;C:\\Users\\a\\AppData\\Local\\Programs\\antigravity;c:\\users\\a\\desktop";
        let r = super::source_rows_with(s, |p| !p.contains("keep"));
        let titles: Vec<(i32, &str)> = r.iter().map(|x| (x.0, x.1.as_str())).collect();
        assert_eq!(
            titles,
            vec![
                (0, "폴더·파일 (3개)"),
                (1, "Desktop"),
                (1, "sub"),
                (1, "keep"),
                (0, "프로그램 (3개)"),
                (2, "Tailscale"),
                (2, "Git"),
                (2, "antigravity-updater"),
            ]
        );
        assert_eq!(
            r[3].2,
            "찾을 수 없음 · C:\\Users\\a\\AppData\\Local\\Temp\\keep"
        );
        assert_eq!(r[5].2, "4개 항목");
        assert_eq!(r[7].2, "2개 항목");
        assert_eq!(r[5].3, "prog:tailscale");
    }

    #[test]
    fn apply_settings_validates_and_preserves_other_fields() {
        let dir = std::env::temp_dir().join(format!("kova_set_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = kova_core::config::ConfigStore::new(&dir);
        let mut s = store.settings();
        s.offsite_remote_path = "keep".into();
        s.extra.insert("unknown_key".into(), 7.into());
        store.save_settings(&s).unwrap();

        assert!(apply_settings(&store, "abc", true, false, true, false).is_err());
        assert!(apply_settings(&store, "-1", true, false, true, false).is_err());
        assert!(apply_settings(&store, "999999", true, false, true, false).is_err());
        assert_eq!(store.settings().offsite_remote_path, "keep"); // 실패 시 변경 없음

        assert!(apply_settings(&store, " 25 ", false, false, false, true).is_ok());
        let after = store.settings();
        assert_eq!(after.min_free_disk_gb, 25);
        assert!(!after.enable_ransomware_protection);
        assert!(!after.use_vss);
        assert!(after.pack_blobs);
        assert_eq!(after.offsite_remote_path, "keep");
        assert_eq!(after.extra.get("unknown_key"), Some(&7.into()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn restore_keys_rebuilds_the_key_file_only_for_the_matching_repo() {
        let base = std::env::temp_dir().join(format!("kova_rk_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let keys = base.join("keys");
        std::fs::create_dir_all(&keys).unwrap();
        std::env::set_var("KOVA2_KEY_DIR", &keys);
        let repo = base.join("repo");
        let store = kova_core::config::ConfigStore::new(base.join("cfg"));
        let mut p = kova_core::config::Profile::new("p1", "시험");
        p.repo_dir = repo.to_string_lossy().into();
        store.upsert(p).unwrap();
        let code = kova_core::repocrypto::enable(&repo).unwrap().unwrap();
        for f in std::fs::read_dir(&keys).unwrap() {
            std::fs::remove_file(f.unwrap().path()).unwrap();
        }
        assert!(restore_keys(&store, "00000000").is_err());
        assert!(restore_keys(&store, &code).is_ok());
        assert_eq!(std::fs::read_dir(&keys).unwrap().count(), 1);
        let kit = base.join("kit.txt");
        let msg = export_recovery_kit_to(&store, &kit).unwrap();
        assert!(msg.contains("1곳"));
        let kit_body = std::fs::read_to_string(&kit).unwrap();
        assert!(kit_body.contains(&code) && kit_body.contains("시험"));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn apply_ops_settings_validates_and_saves() {
        let dir = std::env::temp_dir().join(format!("kova_ops_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = kova_core::config::ConfigStore::new(&dir);
        assert!(apply_ops_settings(&store, "ftp://bad", "2", "10", false).is_err());
        assert!(apply_ops_settings(&store, "", "99", "10", false).is_err());
        assert!(apply_ops_settings(&store, "", "2", "99999", false).is_err());
        assert!(apply_ops_settings(&store, "https://hooks.example.com/1", "4", "50", true).is_ok());
        let s = store.settings();
        assert_eq!(s.webhook_url, "https://hooks.example.com/1");
        assert_eq!(s.max_cpu_threads, 4);
        assert_eq!(s.offsite_bw_limit_mbps, 50);
        assert!(s.pause_on_battery);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn build_pe_ps_args_includes_drivers_only_when_given() {
        let a = build_pe_ps_args(
            std::path::Path::new("C:\\Kova2\\make_pe.ps1"),
            std::path::Path::new("D:\\out.iso"),
            std::path::Path::new("C:\\Kova2\\kova-pe.exe"),
            "",
        );
        assert!(a.contains("-Out 'D:\\out.iso'") && !a.contains("-DriversDir"));
        let b = build_pe_ps_args(
            std::path::Path::new("C:\\Kova2\\make_pe.ps1"),
            std::path::Path::new("D:\\out.iso"),
            std::path::Path::new("C:\\Kova2\\kova-pe.exe"),
            "C:\\Drivers\\NVMe",
        );
        assert!(b.contains("-DriversDir 'C:\\Drivers\\NVMe'"));
    }

    #[test]
    fn run_export_drivers_handles_custom_destination() {
        let tmp = std::env::temp_dir().join(format!("kova_drv_test_{}", std::process::id()));
        let res = run_export_drivers(Some(&tmp));
        // 관리자 권한이 없으면 "실패", 있으면 "완료", 비-Windows면 "모의 완료" -> 크래시 없이 결과 메시지 반환
        assert!(!res.is_empty());
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn search_all_repos_finds_files_across_snapshots_with_chosung() {
        let base = std::env::temp_dir().join(format!("kova_gsearch_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let src = base.join("src");
        let repo = base.join("repo");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("품질보고서.txt"), "v1").unwrap();
        let no = std::sync::atomic::AtomicBool::new(false);
        kova_engine::ops::run_backup(
            &kova_engine::ops::BackupOptions {
                sources: vec![src.clone()],
                repo: repo.clone(),
                session_id: Some("20261119_010000".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: Some("p1".into()),
                scan_from: None,
            },
            &|_| {},
            &no,
        )
        .unwrap();
        std::fs::write(src.join("품질보고서.txt"), "v2-updated").unwrap();
        kova_engine::ops::run_backup(
            &kova_engine::ops::BackupOptions {
                sources: vec![src],
                repo: repo.clone(),
                session_id: Some("20261119_020000".into()),
                zstd_level: 3,
                exclude: vec![],
                profile_id: Some("p1".into()),
                scan_from: None,
            },
            &|_| {},
            &no,
        )
        .unwrap();

        let store = kova_core::config::ConfigStore::new(base.join("cfg"));
        let mut p = kova_core::config::Profile::new("p1", "문서");
        p.repo_dir = repo.to_string_lossy().into();
        store.upsert(p).unwrap();

        let res = search_all_repos(&store, "ㅍㅈㅂㄱㅅ");
        assert_eq!(res.len(), 1);
        assert_eq!(res[0].0, "품질보고서.txt");
        assert_eq!(res[0].4, 2, "2개 시점 버전이 집계되어야 함");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn parse_service_stopped_detects_stopped_and_missing() {
        assert!(parse_service_stopped(
            "        STATE              : 1  STOPPED"
        ));
        assert!(parse_service_stopped(
            "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:"
        ));
        assert!(!parse_service_stopped(
            "        STATE              : 3  STOP_PENDING"
        ));
        assert!(!parse_service_stopped(
            "        STATE              : 4  RUNNING"
        ));
    }

    #[test]
    fn running_list_tracks_events() {
        use kova_ipc::Event as E;
        let name = |id: &str| format!("P-{id}");
        let mut rows: Vec<RunInfo> = vec![];
        let start = |job_id, kind: &str| E::JobStarted {
            job_id,
            profile_id: "a".into(),
            kind: kind.into(),
        };
        update_running(&mut rows, &start(1, "backup"), &name);
        update_running(&mut rows, &start(2, "verify"), &name);
        assert_eq!(rows.len(), 2);
        assert!(rows[0].cancellable && !rows[1].cancellable);
        assert_eq!(rows[0].name, "P-a");
        update_running(
            &mut rows,
            &E::Progress {
                job_id: 1,
                percent: 250,
                current: "f.txt".into(),
            },
            &name,
        );
        assert_eq!((rows[0].pct, rows[0].current.as_str()), (100, "f.txt"));
        update_running(
            &mut rows,
            &E::JobFinished {
                job_id: 1,
                profile_id: "a".into(),
                ok: true,
                message: String::new(),
                snapshot_id: None,
            },
            &name,
        );
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, 2);
        update_running(&mut rows, &start(3, "image:C"), &name);
        assert!(rows[1].cancellable);
    }

    #[test]
    fn alert_and_size_formatting() {
        assert_eq!(shorten_alert("짧은 알림"), "짧은 알림");
        let long = "가".repeat(300);
        let out = shorten_alert(&long);
        assert!(out.starts_with(&"가".repeat(90)) && out.contains("눌러서 펼치기"));
        assert_eq!(human_mb(5 * 1_048_576), "5.0MB");
        assert_eq!(human_mb(3 * 1_073_741_824), "3.0GB");
    }

    use super::*;

    #[test]
    fn parse_picked_joins_lines_and_strips_bom() {
        assert_eq!(
            parse_picked("\u{feff}C:\\a\r\nD:\\b b\r\n\r\n"),
            "C:\\a;D:\\b b"
        );
        assert_eq!(parse_picked(""), "");
        assert_eq!(parse_picked("  \r\n"), "");
    }

    #[test]
    fn install_result_roundtrip_and_legacy() {
        assert_eq!(
            decode_install_result(&encode_install_result(true, "v1 설치 완료\n둘째 줄")),
            (true, "v1 설치 완료\n둘째 줄".into())
        );
        assert_eq!(
            decode_install_result(&encode_install_result(false, "이미 최신입니다")),
            (false, "이미 최신입니다".into())
        );
        // 옛 버전은 표식 없이 문구만 적는다 -> 설치 문구가 들어 있어도 재시작하지 않는다
        assert_eq!(
            decode_install_result("v3 설치 완료(2개 파일)"),
            (false, "v3 설치 완료(2개 파일)".into())
        );
    }

    #[test]
    fn pick_script_folder_vs_file() {
        let one = build_pick_script("folder");
        let many = build_pick_script("folders");
        assert!(
            one.contains("[FP]::Pick($false,$f.Handle)")
                && many.contains("[FP]::Pick($true,$f.Handle)")
        );
        assert!(
            one.contains("$f.TopMost=$true"),
            "소유 창은 PowerShell 프로세스 안의 TopMost 폼이어야 한다"
        );
        assert!(one.contains("-OutputAssembly"));
        let f = build_pick_script("qblk-open");
        assert!(f.contains("OpenFileDialog") && f.contains("*.qblk"));
        assert!(build_pick_script("qblk-save").contains("SaveFileDialog"));
    }
}
