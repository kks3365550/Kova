//! 서비스 본체: 스케줄러 틱 → 작업 큐 → 엔진 실행 → 프로파일 상태 갱신 → 이벤트 푸시.
//! 시각을 인자로 받아 서비스 루프와 시험이 같은 코드를 쓴다.
use chrono::{DateTime, Local};
use kova_core::config::ConfigStore;
use kova_core::history::History;
use kova_core::jobs::{Enqueue, Job, JobQueue, Kind};
use kova_core::schedule::{should_run, MAX_AUTO_RETRIES};
use kova_engine::ops::{run_backup, BackupOptions, EngineError, Event as EngineEvent};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};

/// 화면(IPC)으로 푸시되는 이벤트.
#[derive(Debug, Clone, PartialEq)]
pub enum SvcEvent {
    JobStarted { job_id: u64, profile_id: String, kind: Kind },
    Progress { job_id: u64, percent: u64, current: String },
    JobFinished { job_id: u64, profile_id: String, ok: bool, message: String, snapshot_id: Option<String> },
}

struct Shared {
    queue: Mutex<JobQueue>,
    subs: Mutex<Vec<Sender<SvcEvent>>>,
    store: ConfigStore,
    max_concurrent: usize,
    config_dir: PathBuf,
}

#[derive(Clone)]
pub struct Supervisor(Arc<Shared>);

impl Supervisor {
    pub fn new(config_dir: impl Into<PathBuf>, max_concurrent: usize) -> Self {
        let config_dir: PathBuf = config_dir.into();
        Self(Arc::new(Shared {
            queue: Mutex::new(JobQueue::new()),
            subs: Mutex::new(vec![]),
            store: ConfigStore::new(&config_dir),
            max_concurrent: max_concurrent.max(1),
            config_dir,
        }))
    }

    pub fn subscribe(&self) -> Receiver<SvcEvent> {
        let (tx, rx) = channel();
        self.0.subs.lock().unwrap().push(tx);
        rx
    }

    fn emit(&self, e: SvcEvent) {
        self.0.subs.lock().unwrap().retain(|s| s.send(e.clone()).is_ok());
    }

    /// 수동 작업 요청 (화면에서 "지금 백업").
    pub fn enqueue(&self, profile_id: &str, kind: Kind) -> Enqueue {
        let r = self.0.queue.lock().unwrap().enqueue(profile_id, kind);
        self.pump();
        r
    }

    pub fn cancel(&self, job_id: u64) -> bool {
        self.0.queue.lock().unwrap().cancel(job_id)
    }

    /// 스케줄이 도래한 프로파일을 큐에 넣고 시작 가능한 작업을 시작한다. 새로 큐에 넣은 수를 반환.
    pub fn tick(&self, now: DateTime<Local>) -> usize {
        let mut n = 0;
        for p in self.0.store.profiles() {
            if should_run(&p, now) {
                let mut q = self.0.queue.lock().unwrap();
                if !q.is_profile_busy(&p.id) {
                    q.enqueue(&p.id, Kind::Backup);
                    n += 1;
                }
            }
        }
        self.pump();
        n
    }

    /// (대기, 실행 중)
    pub fn counts(&self) -> (usize, usize) {
        self.0.queue.lock().unwrap().counts()
    }

    fn pump(&self) {
        loop {
            let started = self.0.queue.lock().unwrap().start_next(self.0.max_concurrent);
            let Some((job, flag)) = started else { break };
            let me = self.clone();
            std::thread::spawn(move || me.run_job(job, flag));
        }
    }

    fn run_job(&self, job: Job, cancel: Arc<AtomicBool>) {
        self.emit(SvcEvent::JobStarted { job_id: job.id, profile_id: job.profile_id.clone(), kind: job.kind });
        let _awake = SleepGuard::new(); // 작업 중 절전 진입 방지(이 작업 스레드에만 적용, 끝나면 자동 해제)
        let (ok, message, snapshot_id) = match job.kind {
            Kind::Backup => self.run_backup_job(&job, &cancel),
            Kind::Verify => self.run_verify_job(&job),
            Kind::Gc => self.run_gc_job(&job),
        };
        // 실패·경고는 알림 기록에 남긴다(화면이 없는 서비스는 기록만, 표시는 앱이 한다). 사용자 취소는 알리지 않는다.
        if message != "취소됨" && (!ok || message.contains("복제 실패")) {
            let _ = kova_core::alerts::AlertLog::new(&self.0.config_dir).push(kova_core::alerts::Alert {
                at: Local::now().timestamp() as f64,
                profile_id: job.profile_id.clone(),
                level: if ok { "warning" } else { "error" }.into(),
                message: message.clone(),
                acknowledged: false,
            });
        }
        // 이벤트를 먼저 보낸 뒤 큐에서 빼야 "큐가 비었다 = 모든 이벤트가 나갔다"가 성립한다
        self.emit(SvcEvent::JobFinished { job_id: job.id, profile_id: job.profile_id.clone(), ok, message, snapshot_id });
        self.0.queue.lock().unwrap().finish(job.id);
        self.pump();
    }

    fn run_backup_job(&self, job: &Job, cancel: &AtomicBool) -> (bool, String, Option<String>) {
        let Some(prof) = self.0.store.get(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        let mut opts = BackupOptions {
            sources: prof.sources.iter().map(PathBuf::from).collect(),
            repo: PathBuf::from(&prof.repo_dir),
            session_id: None,
            zstd_level: prof.compression_level,
            exclude: prof.exclude_patterns.clone(),
            profile_id: Some(prof.id.clone()),
        };
        // 원본이 없으면(드라이브 분리 등) 엔진은 빈 백업을 "성공"으로 끝내므로 여기서 실패로 막는다
        let missing: Vec<String> = opts.sources.iter().filter(|s| !s.exists()).map(|s| s.display().to_string()).collect();
        // 일부만 없으면(임시 폴더·바로가기 등) 있는 것만 백업하고 메시지에 남긴다. 전부 없으면 실패로 막는다
        let partial_missing = !missing.is_empty() && missing.len() < opts.sources.len();
        if partial_missing { opts.sources.retain(|s| s.exists()); }
        let last_pct = Mutex::new(u64::MAX);
        let id = job.id;
        let on_event = |e: EngineEvent| {
            if let EngineEvent::Progress(p) = e {
                let mut l = last_pct.lock().unwrap();
                if *l != p.percent {
                    *l = p.percent;
                    self.emit(SvcEvent::Progress { job_id: id, percent: p.percent, current: p.current_file });
                }
            }
        };
        let res = if missing.is_empty() || partial_missing {
            run_backup(&opts, &on_event, cancel)
        } else {
            Err(EngineError::Msg(format!("원본 경로를 찾을 수 없음: {}", missing.join(", "))))
        };
        let now = Local::now().timestamp() as f64;
        // 결과 반영은 최신 파일을 다시 읽어 이 프로파일만 갱신한다 (실행 중 화면에서 바꾼 설정을 덮어쓰지 않음)
        let mut prof = self.0.store.get(&job.profile_id).unwrap_or(prof);
        let out = match res {
            Ok(s) => {
                prof.last_status = "success".into();
                prof.last_snapshot_id = Some(s.session_id.clone());
                prof.extra.insert("retry_count".into(), 0.into());
                // 보존 정책: 이 프로파일의 스냅샷에만 적용(방금 만든 최신본은 항상 보호됨)
                let policy = kova_core::retention::Policy {
                    retention_count: Some(prof.retention_count as usize),
                    retention_days: Some(prof.retention_days),
                };
                let ret = kova_core::retention::apply_for_profile(&opts.repo, &prof.id, policy);
                if let Ok(mut h) = History::open(&opts.repo) {
                    let _ = h.sync_snapshots();
                }
                let mut msg = if s.skipped_files.is_empty() {
                    format!("완료: {}개 항목, {}바이트", s.total_files, s.total_bytes)
                } else {
                    format!("완료(건너뜀 {}개): {}개 항목", s.skipped_files.len(), s.total_files)
                };
                // 오프사이트(폴더/SMB) 복제: 설정돼 있으면 백업 직후 이어서 복제. 실패해도 로컬 백업은 성공으로 두고 메시지에 남긴다
                if let Some(dest) = prof.extra.get("offsite_dir").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                    match kova_core::offsite::replicate(&opts.repo, std::path::Path::new(dest), cancel) {
                        Ok(r) => msg.push_str(&format!(", 오프사이트 복제 {}개 스냅샷/{}개 블롭", r.snapshots_copied, r.blobs_copied)),
                        Err(e) => msg.push_str(&format!(", 오프사이트 복제 실패: {e}")),
                    }
                }
                if partial_missing {
                    msg.push_str(&format!(", 원본 {}개 없어 제외: {}", missing.len(), missing.join(", ")));
                }
                if !ret.deleted_snapshots.is_empty() {
                    msg.push_str(&format!(", 보존 정책으로 옛 스냅샷 {}개 정리", ret.deleted_snapshots.len()));
                }
                (true, msg, Some(s.session_id))
            }
            Err(e) => {
                prof.last_status = "failed".into();
                // 사용자 취소는 재시도하지 않는다
                let rc = if matches!(e, EngineError::Cancelled) {
                    MAX_AUTO_RETRIES as u64
                } else {
                    prof.extra.get("retry_count").and_then(|v| v.as_u64()).unwrap_or(0) + 1
                };
                prof.extra.insert("retry_count".into(), rc.into());
                (false, e.to_string(), None)
            }
        };
        prof.last_run = Some(now);
        let _ = self.0.store.upsert(prof);
        out
    }

    fn run_gc_job(&self, job: &Job) -> (bool, String, Option<String>) {
        let Some(prof) = self.0.store.get(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        // 1시간 미만의 새 블롭은 진행 중 백업 것일 수 있어 보호된다
        match kova_core::gc::prune_orphans(std::path::Path::new(&prof.repo_dir), kova_core::gc::DEFAULT_MIN_AGE_SECS) {
            Ok(r) => (true, format!("정리 완료: 고아 블롭 {}개, {}바이트 확보", r.deleted_blobs, r.freed_bytes), None),
            Err(e) => (false, e.to_string(), None),
        }
    }

    fn run_verify_job(&self, job: &Job) -> (bool, String, Option<String>) {
        let Some(prof) = self.0.store.get(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        match kova_core::verify::verify_repo(std::path::Path::new(&prof.repo_dir), false) {
            Ok(r) if r.ok() => (true, format!("검증 통과: 스냅샷 {}개, 파일 {}개", r.snapshots, r.files), None),
            Ok(r) => (false, format!("검증 문제 {}건", r.problems.len()), None),
            Err(e) => (false, e.to_string(), None),
        }
    }
}

/// 살아 있는 동안 시스템이 절전으로 들어가지 않게 한다(스레드 단위, Drop 시 해제).
pub struct SleepGuard;
#[cfg(windows)]
extern "system" {
    fn SetThreadExecutionState(flags: u32) -> u32;
}
impl SleepGuard {
    pub fn new() -> Self {
        #[cfg(windows)]
        unsafe {
            // ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            SetThreadExecutionState(0x8000_0000 | 0x0000_0001);
        }
        SleepGuard
    }
}
impl Drop for SleepGuard {
    fn drop(&mut self) {
        #[cfg(windows)]
        unsafe {
            SetThreadExecutionState(0x8000_0000); // ES_CONTINUOUS 만 → 요구 해제
        }
    }
}

/// kova-ipc 의 요청을 Supervisor 로 연결한다.
pub struct IpcHandler(pub Supervisor);

fn to_wire(e: SvcEvent) -> kova_ipc::Event {
    let kind_name = |k: Kind| match k { Kind::Backup => "backup", Kind::Verify => "verify", Kind::Gc => "gc" }.to_string();
    match e {
        SvcEvent::JobStarted { job_id, profile_id, kind } => kova_ipc::Event::JobStarted { job_id, profile_id, kind: kind_name(kind) },
        SvcEvent::Progress { job_id, percent, current } => kova_ipc::Event::Progress { job_id, percent, current },
        SvcEvent::JobFinished { job_id, profile_id, ok, message, snapshot_id } =>
            kova_ipc::Event::JobFinished { job_id, profile_id, ok, message, snapshot_id },
    }
}

impl kova_ipc::Handler for IpcHandler {
    fn handle(&self, req: kova_ipc::Request) -> kova_ipc::Reply {
        use kova_ipc::{Event, Reply, Request};
        match req {
            Request::Ping => Reply::Once(vec![Event::Pong { pid: std::process::id() }]),
            Request::Status => {
                let (waiting, running) = self.0.counts();
                Reply::Once(vec![Event::Status { waiting, running }])
            }
            Request::RunNow { profile_id, kind } => {
                let k = match kind.as_str() {
                    "backup" => Kind::Backup,
                    "verify" => Kind::Verify,
                    "gc" => Kind::Gc,
                    other => return Reply::Once(vec![Event::Error { message: format!("알 수 없는 작업 종류: {other}") }]),
                };
                Reply::Once(vec![match self.0.enqueue(&profile_id, k) {
                    Enqueue::Queued(job_id) => Event::Queued { job_id, merged: false },
                    Enqueue::Merged(job_id) => Event::Queued { job_id, merged: true },
                }])
            }
            Request::Cancel { job_id } => Reply::Once(vec![Event::Cancelled { job_id, found: self.0.cancel(job_id) }]),
            Request::Subscribe => {
                let rx = self.0.subscribe();
                let (tx, out) = channel();
                std::thread::spawn(move || {
                    while let Ok(e) = rx.recv() {
                        if tx.send(to_wire(e)).is_err() {
                            break; // 연결이 끝남
                        }
                    }
                });
                Reply::Stream(out)
            }
        }
    }
}

/// 설정 폴더: %ProgramData%\Kova2\config (없으면 현재 폴더 아래)
pub fn default_config_dir() -> PathBuf {
    // 시험·통합 검증용 재지정(실제 설정 폴더를 건드리지 않기 위함)
    if let Some(d) = std::env::var_os("KOVA2_CONFIG_DIR") {
        return PathBuf::from(d);
    }
    std::env::var_os("ProgramData").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
        .join("Kova2").join("config")
}

/// 스케줄러 틱 스레드: 30초마다 도래한 프로파일을 큐에 넣는다. 시작 직후 한 번 돌아 놓친 백업을 따라잡는다.
pub fn spawn_scheduler(sup: Supervisor, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        while !stop.load(std::sync::atomic::Ordering::Relaxed) {
            sup.tick(Local::now());
            for _ in 0..60 {
                if stop.load(std::sync::atomic::Ordering::Relaxed) { return; }
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use kova_core::config::Profile;
    use std::fs;
    use std::time::{Duration, Instant};

    fn wait_idle(s: &Supervisor) {
        let t = Instant::now();
        while s.counts() != (0, 0) {
            assert!(t.elapsed() < Duration::from_secs(60), "작업이 끝나지 않음");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn setup(src_exists: bool) -> (tempfile::TempDir, Supervisor) {
        let d = tempfile::tempdir().unwrap();
        let src = d.path().join("원본");
        if src_exists {
            fs::create_dir_all(&src).unwrap();
            fs::write(src.join("a.txt"), "안녕 kova").unwrap();
        }
        let mut p = Profile::new("p1", "시험");
        p.sources = vec![src.to_string_lossy().into()];
        p.repo_dir = d.path().join("repo").to_string_lossy().into();
        p.auto_backup_enabled = true;
        let cfg = d.path().join("cfg");
        ConfigStore::new(&cfg).upsert(p).unwrap();
        (d, Supervisor::new(cfg, 2))
    }

    #[test]
    fn tick_runs_backup_updates_profile_and_pushes_events() {
        let (d, s) = setup(true);
        let rx = s.subscribe();
        assert_eq!(s.tick(Local::now()), 1);
        wait_idle(&s);
        let p = ConfigStore::new(d.path().join("cfg")).get("p1").unwrap();
        assert_eq!(p.last_status, "success");
        assert!(p.last_snapshot_id.is_some() && p.last_run.is_some());
        let evs: Vec<SvcEvent> = rx.try_iter().collect();
        assert!(matches!(evs.first(), Some(SvcEvent::JobStarted { .. })));
        assert!(matches!(evs.last(), Some(SvcEvent::JobFinished { ok: true, .. })));
        // 이력 DB 가 스냅샷을 반영했다
        let h = History::open(&d.path().join("repo")).unwrap();
        assert_eq!(h.list_snapshots().unwrap().len(), 1);
        // 방금 돌았으니 바로 다시 큐잉되지 않는다
        assert_eq!(s.tick(Local::now()), 0);
    }

    #[test]
    fn failure_is_recorded_with_retry_count() {
        let (d, s) = setup(false);
        let rx = s.subscribe();
        s.tick(Local::now());
        wait_idle(&s);
        let p = ConfigStore::new(d.path().join("cfg")).get("p1").unwrap();
        assert_eq!(p.last_status, "failed");
        assert_eq!(kova_core::schedule::retry_count(&p), 1);
        assert!(rx.try_iter().any(|e| matches!(e, SvcEvent::JobFinished { ok: false, .. })));
    }

    #[test]
    fn partially_missing_sources_are_skipped_with_a_note() {
        let (d, s) = setup(true);
        let mut p = ConfigStore::new(d.path().join("cfg")).get("p1").unwrap();
        let gone = d.path().join("없는폴더").to_string_lossy().to_string();
        p.sources.push(gone.clone());
        ConfigStore::new(d.path().join("cfg")).upsert(p).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let done = rx.try_iter().find_map(|e| match e { SvcEvent::JobFinished { ok, message, .. } => Some((ok, message)), _ => None }).unwrap();
        assert!(done.0, "{}", done.1);
        assert!(done.1.contains("원본 1개 없어 제외") && done.1.contains(&gone), "{}", done.1);
    }

    #[test]
    fn gc_job_removes_only_old_orphans() {
        let (d, s) = setup(true);
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let blobs = d.path().join("repo").join("blobs").join("zz");
        fs::create_dir_all(&blobs).unwrap();
        let young = blobs.join("young.blob");
        fs::write(&young, b"x").unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Gc);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(e, SvcEvent::JobFinished { ok: true, .. })));
        assert!(young.exists(), "1시간 미만 블롭은 지우지 않는다");
    }

    #[test]
    fn backup_replicates_to_offsite_dir_when_configured() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut p = store.get("p1").unwrap();
        let off = d.path().join("offsite");
        p.extra.insert("offsite_dir".into(), off.to_string_lossy().to_string().into());
        store.upsert(p).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let msg = rx.try_iter().find_map(|e| if let SvcEvent::JobFinished { ok: true, message, .. } = e { Some(message) } else { None }).expect("성공 이벤트");
        assert!(msg.contains("오프사이트 복제 1개 스냅샷"), "{msg}");
        assert!(kova_core::verify::verify_repo(&off, true).unwrap().ok());
    }

    #[test]
    fn failure_leaves_an_alert_but_success_does_not() {
        let (d, s) = setup(false);
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let log = kova_core::alerts::AlertLog::new(&d.path().join("cfg"));
        let v = log.list();
        assert_eq!(v.len(), 1);
        assert_eq!((v[0].level.as_str(), v[0].profile_id.as_str()), ("error", "p1"));
        let (d2, s2) = setup(true);
        s2.enqueue("p1", Kind::Backup);
        wait_idle(&s2);
        assert!(kova_core::alerts::AlertLog::new(&d2.path().join("cfg")).list().is_empty());
    }

    #[test]
    fn sleep_guard_can_be_created_and_dropped() {
        drop(SleepGuard::new());
    }

    #[test]
    fn manual_enqueue_and_unknown_profile() {
        let (_d, s) = setup(true);
        let rx = s.subscribe();
        assert!(matches!(s.enqueue("없음", Kind::Backup), Enqueue::Queued(_)));
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(e, SvcEvent::JobFinished { ok: false, .. })));
    }

    #[test]
    fn verify_job_passes_after_backup() {
        let (_d, s) = setup(true);
        s.tick(Local::now());
        wait_idle(&s);
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Verify);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(e, SvcEvent::JobFinished { ok: true, .. })));
    }

    #[tokio::test]
    async fn run_now_over_pipe_streams_events_to_subscriber() {
        let (_d, s) = setup(true);
        let name = format!(r"\\.\pipe\kova-svc-test-{}", std::process::id());
        let h: Arc<dyn kova_ipc::Handler> = Arc::new(IpcHandler(s));
        let n2 = name.clone();
        tokio::spawn(async move { let _ = kova_ipc::serve(&n2, h).await; });
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        let n3 = name.clone();
        let sub = tokio::spawn(async move {
            kova_ipc::request_until(&n3, &kova_ipc::Request::Subscribe, |e| matches!(e, kova_ipc::Event::JobFinished { .. })).await
        });
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let q = kova_ipc::request(&name, &kova_ipc::Request::RunNow { profile_id: "p1".into(), kind: "backup".into() }).await.unwrap();
        assert!(matches!(q[0], kova_ipc::Event::Queued { merged: false, .. }));
        let evs = sub.await.unwrap().unwrap();
        assert!(matches!(evs.first(), Some(kova_ipc::Event::JobStarted { .. })));
        assert!(matches!(evs.last(), Some(kova_ipc::Event::JobFinished { ok: true, .. })));
        let bad = kova_ipc::request(&name, &kova_ipc::Request::RunNow { profile_id: "p1".into(), kind: "format".into() }).await.unwrap();
        assert!(matches!(bad[0], kova_ipc::Event::Error { .. }));
    }

    #[test]
    fn retention_runs_after_backup_for_this_profile_only() {
        let (d, s) = setup(true);
        let cfg = ConfigStore::new(d.path().join("cfg"));
        let mut p = cfg.get("p1").unwrap();
        p.retention_count = 1;
        cfg.upsert(p).unwrap();
        // 다른 프로파일이 같은 저장소에 만든 스냅샷은 건드리면 안 된다
        let snaps = d.path().join("repo/snapshots");
        fs::create_dir_all(&snaps).unwrap();
        fs::write(snaps.join("other1.json"), r#"{"id":"other1","created_at":1,"profile_id":"zzz"}"#).unwrap();
        fs::write(snaps.join("other2.json"), r#"{"id":"other2","created_at":2,"profile_id":"zzz"}"#).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        std::thread::sleep(Duration::from_millis(1100)); // 세션 ID 가 초 단위라 겹치지 않게
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let mine: Vec<_> = fs::read_dir(&snaps).unwrap().flatten().map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| !n.starts_with("other")).collect();
        assert_eq!(mine.len(), 1, "retention_count=1 이면 내 스냅샷은 1개만 남아야 함: {mine:?}");
        assert!(snaps.join("other1.json").exists() && snaps.join("other2.json").exists());
        assert!(rx.try_iter().any(|e| matches!(e, SvcEvent::JobFinished { ref message, .. } if message.contains("정리"))));
    }
}
