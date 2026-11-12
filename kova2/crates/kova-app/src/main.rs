slint::include_modules!();

use tray_icon::menu::{Menu, MenuEvent, MenuItem};
use tray_icon::{Icon, TrayIconBuilder};

fn make_icon() -> Icon {
    // 32x32 단색 아이콘(스파이크용)
    let mut rgba = Vec::with_capacity(32 * 32 * 4);
    for _ in 0..(32 * 32) {
        rgba.extend_from_slice(&[0x1f, 0x6f, 0xeb, 0xff]);
    }
    Icon::from_rgba(rgba, 32, 32).unwrap()
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
                "원본 {} → {} · 보존 {}개/{}일",
                p.sources.join(", "),
                p.repo_dir,
                p.retention_count,
                p.retention_days
            )
            .into(),
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

fn alert_rows() -> Vec<AlertRow> {
    let mut v = kova_core::alerts::AlertLog::new(&kova_service::default_config_dir()).list();
    v.reverse();
    v.into_iter()
        .map(|a| AlertRow {
            when: chrono_fmt(a.at).into(),
            level: a.level.into(),
            message: format!("{}: {}", a.profile_id, a.message).into(),
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
        String::new(),
        "프로파일".into(),
    ];
    for p in store.profiles() {
        v.push(format!(
            "· {} — 원본 {} → {} · 보존 {}개/{}일 · 압축 {}",
            p.name,
            p.sources.join(", "),
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
    for p in store.profiles() {
        let repo = std::path::PathBuf::from(&p.repo_dir);
        if !repo.join("metadata.db").exists() {
            continue; // 읽기만 한다: 없는 저장소에 DB 를 새로 만들지 않는다
        }
        let Ok(h) = kova_core::history::History::open(&repo) else {
            continue;
        };
        for r in h.list_snapshots().unwrap_or_default() {
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
                    summary: format!(
                        "{} · 파일 {}개 · {:.1}MB",
                        p.name,
                        r.total_files,
                        r.total_bytes as f64 / 1_048_576.0
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
                let profiles = rows();
                let snapshots = snaps();
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
                let n = kova_core::alerts::AlertLog::new(&kova_service::default_config_dir())
                    .unacknowledged();
                let alert = if n > 0 {
                    format!("확인하지 않은 알림 {n}건 (백업 실패 등)")
                } else {
                    String::new()
                };
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(w) = w.upgrade() {
                        w.set_service_status(text.into());
                        w.set_alert_text(alert.into());
                        if let Some(off) = off {
                            w.set_offsite_rows(std::rc::Rc::new(slint::VecModel::from(off)).into());
                        }
                        w.set_alerts(std::rc::Rc::new(slint::VecModel::from(alerts)).into());
                        w.set_setting_lines(std::rc::Rc::new(slint::VecModel::from(lines)).into());
                        w.set_profiles(std::rc::Rc::new(slint::VecModel::from(profiles)).into());
                        w.set_snapshots(std::rc::Rc::new(slint::VecModel::from(snapshots)).into());
                    }
                });
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        });
    });
    std::thread::spawn(move || {
        rt.block_on(async move {
            loop {
                let w = weak.clone();
                let _ = kova_ipc::stream(kova_ipc::PIPE_NAME, &kova_ipc::Request::Subscribe, |e| {
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
                        } => format!(
                            "{} {profile_id}: {message}",
                            if ok { "완료" } else { "실패" }
                        ),
                        _ => return true,
                    };
                    let w = w.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(w) = w.upgrade() {
                            let old = w.get_log_text().to_string();
                            let keep: Vec<&str> = old
                                .lines()
                                .rev()
                                .take(11)
                                .collect::<Vec<_>>()
                                .into_iter()
                                .rev()
                                .collect();
                            w.set_log_text((keep.join("\n") + "\n" + &line).trim_start().into());
                        }
                    });
                    true
                })
                .await;
                tokio::time::sleep(std::time::Duration::from_secs(3)).await; // 서비스가 꺼졌다 켜져도 다시 붙는다
            }
        });
    });
}

fn send(kind: &'static str, id: String) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _ = rt.block_on(kova_ipc::request(
            kova_ipc::PIPE_NAME,
            &kova_ipc::Request::RunNow {
                profile_id: id,
                kind: kind.into(),
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

/// 사용자가 "업데이트 설치"를 눌렀을 때만 실행: 최신 조회 → 서명 검증된 패키지 → 설치 폴더에 적용(실패 시 롤백).
/// 패키지에 kova-app.exe 가 없으면(예: 파이썬판 릴리즈) 설치하지 않는다. 설치 후 재시작은 사용자가 한다.
fn install_update(weak: slint::Weak<MainWindow>) {
    std::thread::spawn(move || {
        let current = env!("CARGO_PKG_VERSION");
        let run = || -> Result<String, String> {
            let tag = kova_core::fetch::latest_tag(UPDATE_BASE, UPDATE_REPO)
                .map_err(|e| e.to_string())?;
            if !kova_core::update::is_newer(&tag, current) {
                return Ok(format!("이미 최신입니다 ({current})"));
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
            Ok(format!(
                "{tag} 설치 완료({}개 파일). 앱을 다시 시작하세요",
                r.files
            ))
        };
        let text = run().unwrap_or_else(|e| format!("설치하지 않음: {e}"));
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(w) = weak.upgrade() {
                w.set_update_text(text.into());
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
fn restore_snapshot_job(weak: slint::Weak<MainWindow>, repo: String, sid: String, target: String) {
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

fn main() -> Result<(), slint::PlatformError> {
    let w = MainWindow::new()?;
    w.on_run_backup(|id| send("backup", id.to_string()));
    w.on_run_verify(|id| send("verify", id.to_string()));
    w.on_save_offsite(|id, dir| save_offsite(&id, &dir));
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
                let text = kova_core::diag::report(
                    &kova_service::default_config_dir(),
                    env!("CARGO_PKG_VERSION"),
                    svc,
                );
                let dir = std::env::var_os("USERPROFILE")
                    .map(|u| std::path::PathBuf::from(u).join("Desktop"))
                    .filter(|d| d.is_dir())
                    .unwrap_or_else(std::env::temp_dir);
                let secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let path = dir.join(format!("Kova2_진단_{secs}.txt"));
                let msg = match std::fs::write(&path, text) {
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
                w.set_form_text("".into());
            }
        });
        let weak = w.as_weak();
        w.on_save_profile(move |id, name, srcs, repo, count, days| {
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
            let msg = match kova_core::profile_form::build(
                base, &new_id, &name, &srcs, &repo, &count, &days, &all,
            ) {
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
        });
        let weak = w.as_weak();
        w.on_delete_profile(move |id| {
            let store = kova_core::config::ConfigStore::new(kova_service::default_config_dir());
            let msg = match store.delete(&id) {
                Ok(true) => "프로파일 설정을 삭제했습니다 (저장소의 백업 파일은 그대로입니다)",
                Ok(false) => "이미 없는 프로파일입니다",
                Err(_) => "삭제하지 못했습니다",
            };
            if let Some(w) = weak.upgrade() {
                if w.get_form_id() == id {
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
        w.on_capture_image(move |s, o| capture_job(weak.clone(), s.to_string(), o.to_string()));
        let weak = w.as_weak();
        w.on_restore_image(move |p, t| image_job(weak.clone(), p.to_string(), Some(t.to_string())));
    }
    {
        let weak = w.as_weak();
        w.on_restore_snapshot(move |repo, sid, t| {
            restore_snapshot_job(
                weak.clone(),
                repo.to_string(),
                sid.to_string(),
                t.to_string(),
            )
        });
    }
    w.on_run_gc(|id| send("gc", id.to_string()));
    w.on_ack_alerts(|| {
        let _ =
            kova_core::alerts::AlertLog::new(&kova_service::default_config_dir()).acknowledge_all();
    });
    spawn_ipc(w.as_weak());

    let menu = Menu::new();
    let open = MenuItem::new("열기", true, None);
    let quit = MenuItem::new("종료", true, None);
    menu.append_items(&[&open, &quit]).unwrap();
    let _tray = TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_tooltip("Kova")
        .with_icon(make_icon())
        .build()
        .unwrap();

    // 트레이 메뉴 이벤트를 Slint 이벤트 루프에서 주기적으로 처리
    let weak = w.as_weak();
    let (open_id, quit_id) = (open.id().clone(), quit.id().clone());
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
                } else if ev.id == quit_id {
                    let _ = slint::quit_event_loop();
                }
            }
        },
    );

    w.run()
}
