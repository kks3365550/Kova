//! 서비스 본체: 스케줄러 틱 → 작업 큐 → 엔진 실행 → 프로파일 상태 갱신 → 이벤트 푸시.
//! 시각을 인자로 받아 서비스 루프와 시험이 같은 코드를 쓴다.
use chrono::{DateTime, Local};
use kova_core::config::{ConfigStore, Profile};
use kova_core::history::History;
use kova_core::jobs::{Enqueue, Job, JobQueue, Kind};
use kova_core::schedule::{should_run, should_run_maintenance, MAX_AUTO_RETRIES};
use kova_engine::ops::{run_backup_with_threads, BackupOptions, EngineError, Event as EngineEvent};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const REPO_LOCK_WAIT: Duration = Duration::from_secs(3);
/// 저장소 잠금 충돌(Busy) 메시지의 공통 머리말. 이 결과는 알림·웹훅을 보내지 않는다.
const BUSY_PREFIX: &str = "다른 작업이 저장소를 사용 중";
/// 잠금 충돌 뒤 이 시간 동안은 정기 스케줄이 같은 프로파일을 다시 큐에 넣지 않는다(30초마다 재큐 방지).
const LOCK_BUSY_COOLDOWN_SEC: f64 = 300.0;
/// 구독자 한 명이 쌓아 둘 수 있는 이벤트 수. 넘치면(읽지 않는 구독자) 그 구독을 끊는다.
const SUB_CAPACITY: usize = 1024;
/// 이미지를 만드는 중간 파일 폴더(`images` 아래). 끝나면 `images` 로 옮겨 미완성 파일이 이미지 목록에 보이지 않게 한다.
const IMAGE_PARTIAL_DIR: &str = "_partial";
/// 이 시간보다 오래 손대지 않은 중간 이미지 파일은 이전 실행이 남긴 것으로 보고 지운다.
const STALE_PARTIAL_SECS: u64 = 3600;
/// 화면(IPC)에 보이는 작업 번호의 시작값. 서비스를 다시 시작해도 옛 번호와 겹치지 않게 시각에서 만든다.
/// 앱이 번호를 i32 로 다루므로 i32::MAX(약 21.4억) 아래에 머문다(약 23일 주기, 회차마다 1000개 여유).
fn wire_id_base(unix_secs: i64) -> u64 {
    (unix_secs.rem_euclid(2_000_000) as u64) * 1000
}

/// 일반 사용자도 요청할 수 있는 작업인가(읽기·상태·백업·검증). 나머지(삭제·정리·이미지)는 관리자/SYSTEM 만.
fn allowed_for_unprivileged(k: Kind) -> bool {
    matches!(k, Kind::Backup | Kind::Verify)
}

/// 최근 잠금 충돌 뒤 쿨다운 중인가.
fn lock_busy_recently(p: &Profile, now_ts: f64) -> bool {
    p.extra
        .get("lock_busy_at")
        .and_then(|v| v.as_f64())
        .is_some_and(|t| now_ts - t >= 0.0 && now_ts - t < LOCK_BUSY_COOLDOWN_SEC)
}

/// 재시도가 아니라 정기 스케줄 자체가 도래했는가(재시도 한도를 다 쓴 것으로 보고 판정).
fn regular_slot_due(p: &Profile, now: DateTime<Local>) -> bool {
    let mut probe = p.clone();
    probe
        .extra
        .insert("retry_count".into(), (MAX_AUTO_RETRIES as u64).into());
    should_run(&probe, now)
}

/// 저장소 안의 이력 DB 를 이미 있을 때만 연다(조회 때문에 저장소 폴더·DB 를 새로 만들지 않는다).
fn open_history_if_exists(repo: &Path) -> Option<History> {
    if repo.join("metadata.db").is_file() {
        History::open(repo).ok()
    } else {
        None
    }
}

fn is_fixed_drive(d: char) -> bool {
    #[cfg(windows)]
    {
        extern "system" {
            fn GetDriveTypeW(root: *const u16) -> u32;
        }
        let w: Vec<u16> = format!("{d}:\\").encode_utf16().chain(Some(0)).collect();
        // SAFETY: 널 종료 UTF-16 문자열
        unsafe { GetDriveTypeW(w.as_ptr()) == 3 } // DRIVE_FIXED
    }
    #[cfg(not(windows))]
    {
        let _ = d;
        false
    }
}

/// 이미지 폴더는 볼륨 전체 내용을 담으므로 SYSTEM·Administrators 만 접근하게 한다(상속 끊기).
fn restrict_to_admins(dir: &Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let o = std::process::Command::new("icacls.exe")
            .arg(dir)
            .args([
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "/Q",
            ])
            .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
            .output()
            .map_err(|e| format!("icacls 실행 실패: {e}"))?;
        if !o.status.success() {
            return Err(format!(
                "icacls 실패({:?}): {}",
                o.status.code(),
                String::from_utf8_lossy(&o.stdout).trim()
            ));
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = dir;
        Ok(())
    }
}

/// 이전 실행이 남긴 중간 이미지 파일(오래 손대지 않은 것)을 지운다. 진행 중인 다른 작업의 파일은 계속 쓰이므로 남는다.
fn remove_stale_partials(dir: &Path, older_than: Duration) -> usize {
    let mut n = 0;
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            let stale = e
                .metadata()
                .ok()
                .filter(|m| m.is_file())
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.elapsed().ok())
                .is_some_and(|age| age >= older_than);
            if stale && std::fs::remove_file(&p).is_ok() {
                n += 1;
            }
        }
    }
    n
}

/// 화면(IPC)으로 푸시되는 이벤트.
#[derive(Debug, Clone, PartialEq)]
pub enum SvcEvent {
    JobStarted {
        job_id: u64,
        profile_id: String,
        kind: Kind,
    },
    Progress {
        job_id: u64,
        percent: u64,
        current: String,
    },
    JobFinished {
        job_id: u64,
        profile_id: String,
        ok: bool,
        message: String,
        snapshot_id: Option<String>,
    },
}

struct Shared {
    queue: Mutex<JobQueue>,
    subs: Mutex<Vec<SyncSender<SvcEvent>>>,
    store: ConfigStore,
    max_concurrent: usize,
    config_dir: PathBuf,
    /// 실행 중 작업의 취소 플래그(서비스 중지 때 모두 세운다). 이 잠금 안에서 `stopping` 을 확인하고 작업을 시작한다.
    running_flags: Mutex<HashMap<u64, Arc<AtomicBool>>>,
    /// 서비스 중지 중: 새 작업을 시작하지 않는다.
    stopping: AtomicBool,
    /// 배터리 때문에 스케줄을 멈춘 상태(로그는 상태가 바뀔 때만 남긴다).
    on_battery: AtomicBool,
    /// IPC 로 보이는 작업 번호 = 내부 번호 + id_base
    id_base: u64,
}

#[derive(Clone)]
pub struct Supervisor(Arc<Shared>);

impl Supervisor {
    pub fn new(config_dir: impl Into<PathBuf>, max_concurrent: usize) -> Self {
        let config_dir: PathBuf = config_dir.into();
        kova_core::repocrypto::install_provider(); // 암호화된 저장소를 열 때 쓸 키 제공자
        Self(Arc::new(Shared {
            queue: Mutex::new(JobQueue::new()),
            subs: Mutex::new(vec![]),
            store: ConfigStore::new(&config_dir),
            max_concurrent: max_concurrent.max(1),
            config_dir,
            running_flags: Mutex::new(HashMap::new()),
            stopping: AtomicBool::new(false),
            on_battery: AtomicBool::new(false),
            id_base: wire_id_base(Local::now().timestamp()),
        }))
    }

    pub fn subscribe(&self) -> Receiver<SvcEvent> {
        let (tx, rx) = sync_channel(SUB_CAPACITY);
        self.0.subs.lock().unwrap().push(tx);
        rx
    }

    /// 구독자마다 유한한 채널로 보낸다. 가득 찬(읽지 않는) 구독자나 끊긴 구독자는 목록에서 뺀다.
    fn emit(&self, e: SvcEvent) {
        self.0
            .subs
            .lock()
            .unwrap()
            .retain(|s| s.try_send(e.clone()).is_ok());
    }

    /// 서비스 중지: 새 작업 시작을 막고 실행 중 작업에 취소를 알린 뒤 모두 끝날 때까지(최대 `limit`) 기다린다.
    /// 기다리는 동안 `on_wait` 를 주기적으로 부른다(서비스 상태 체크포인트 갱신용). 다 끝났으면 true.
    pub fn shutdown(&self, limit: Duration, mut on_wait: impl FnMut()) -> bool {
        {
            let flags = self.0.running_flags.lock().unwrap();
            self.0.stopping.store(true, Ordering::SeqCst);
            for f in flags.values() {
                f.store(true, Ordering::SeqCst);
            }
        }
        let t = std::time::Instant::now();
        loop {
            if self.counts().1 == 0 {
                return true;
            }
            if t.elapsed() >= limit {
                return false;
            }
            on_wait();
            std::thread::sleep(Duration::from_millis(250));
        }
    }

    fn vss_marker(&self, job_id: u64) -> PathBuf {
        self.0.config_dir.join(format!("vss_inflight_{job_id}.txt"))
    }

    /// 섀도 복사본을 만들기 직전에 기록을 남기고, 지운 뒤 기록을 지운다. 비정상 종료로 기록이 남으면 다음 시작 때 알린다.
    fn mark_vss(&self, job_id: u64, what: &str) {
        let _ = std::fs::create_dir_all(&self.0.config_dir);
        let _ = std::fs::write(
            self.vss_marker(job_id),
            format!("{} {what}", Local::now().format("%Y-%m-%d %H:%M:%S")),
        );
    }

    fn unmark_vss(&self, job_id: u64) {
        let _ = std::fs::remove_file(self.vss_marker(job_id));
    }

    /// 시작 시 호출: 이전 실행이 지우지 못했을 수 있는 VSS 섀도 복사본 기록을 경고로 남기고 기록을 치운다.
    /// 섀도 ID 는 kova-core 가 공개하지 않아 기록에 없으므로, 다른 프로그램의 섀도를 지울 위험 때문에 자동 삭제는 하지 않는다.
    pub fn report_leftover_vss(&self) -> usize {
        let mut n = 0;
        let Ok(rd) = std::fs::read_dir(&self.0.config_dir) else {
            return 0;
        };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if !(name.starts_with("vss_inflight_") && name.ends_with(".txt")) {
                continue;
            }
            let what = std::fs::read_to_string(e.path()).unwrap_or_default();
            kova_core::applog::write(
                &self.0.config_dir,
                "WARN",
                &format!(
                    "이전 실행이 VSS 섀도 복사본을 정리하지 못했을 수 있습니다({}). 'vssadmin list shadows' 로 확인하세요",
                    what.trim()
                ),
            );
            let _ = std::fs::remove_file(e.path());
            n += 1;
        }
        n
    }

    /// 수동 작업 요청 (화면에서 "지금 백업").
    pub fn enqueue(&self, profile_id: &str, kind: Kind) -> Enqueue {
        self.enqueue_with(profile_id, kind, "")
    }

    pub fn enqueue_with(&self, profile_id: &str, kind: Kind, arg: &str) -> Enqueue {
        let r = self
            .0
            .queue
            .lock()
            .unwrap()
            .enqueue_with(profile_id, kind, arg);
        self.pump();
        r
    }

    pub fn cancel(&self, job_id: u64) -> bool {
        self.0.queue.lock().unwrap().cancel(job_id)
    }

    /// 대기·실행 중인 작업의 종류(권한 판단용).
    pub fn job_kind(&self, job_id: u64) -> Option<Kind> {
        self.0.queue.lock().unwrap().kind_of(job_id)
    }

    /// 스케줄이 도래한 프로파일을 큐에 넣고 시작 가능한 작업을 시작한다. 새로 큐에 넣은 수를 반환.
    pub fn tick(&self, now: DateTime<Local>) -> usize {
        let mut n = 0;
        if self.0.stopping.load(Ordering::SeqCst) {
            return 0;
        }
        let settings = self.0.store.settings();
        if settings.pause_on_battery && kova_core::schedule::is_on_battery() {
            if !self.0.on_battery.swap(true, Ordering::SeqCst) {
                kova_core::applog::write(
                    &self.0.config_dir,
                    "INFO",
                    "배터리 구동 중이라 자동 스케줄 백업을 일시정지합니다",
                );
            }
            return 0;
        }
        if self.0.on_battery.swap(false, Ordering::SeqCst) {
            kova_core::applog::write(
                &self.0.config_dir,
                "INFO",
                "배터리 일시정지를 풀고 자동 스케줄을 다시 시작합니다",
            );
        }
        let now_ts = now.timestamp() as f64;
        for p in self.0.store.profiles() {
            if lock_busy_recently(&p, now_ts) {
                continue;
            }
            if should_run(&p, now) {
                // 정기 회차가 오면 재시도 횟수를 0으로 되돌린다(성공 때만 되돌리면 한 번 한도를 다 쓴 뒤 다시는 재시도하지 않음)
                if kova_core::schedule::retry_count(&p) > 0 && regular_slot_due(&p, now) {
                    if let Some(mut fresh) = self.0.store.get(&p.id) {
                        fresh.extra.insert("retry_count".into(), 0.into());
                        let _ = self.0.store.upsert(fresh);
                    }
                }
                let mut q = self.0.queue.lock().unwrap();
                if !q.is_profile_busy(&p.id) {
                    q.enqueue(&p.id, Kind::Backup);
                    n += 1;
                    continue;
                }
            }
            // 이미 스냅샷이 있는 프로파일에 대해 정기 Deep Scrub / 자동 GC 스케줄을 점검한다
            if p.last_snapshot_id.is_some() {
                let last_verify = p.extra.get("last_verify_at").and_then(|v| v.as_f64());
                let last_gc = p.extra.get("last_gc_at").and_then(|v| v.as_f64());
                let mut q = self.0.queue.lock().unwrap();
                if !q.is_profile_busy(&p.id) {
                    if should_run_maintenance(
                        last_verify,
                        p.last_run,
                        settings.auto_verify_days,
                        now,
                    ) {
                        q.enqueue(&p.id, Kind::Verify);
                        kova_core::applog::write(
                            &self.0.config_dir,
                            "INFO",
                            &format!("정기 무결성 검증(Deep Scrub) 예약: profile={}", p.id),
                        );
                        n += 1;
                    } else if should_run_maintenance(
                        last_gc,
                        p.last_run,
                        settings.auto_gc_days,
                        now,
                    ) {
                        q.enqueue(&p.id, Kind::Gc);
                        kova_core::applog::write(
                            &self.0.config_dir,
                            "INFO",
                            &format!("정기 고아 블롭 정리(GC) 예약: profile={}", p.id),
                        );
                        n += 1;
                    }
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
            let started = {
                let mut flags = self.0.running_flags.lock().unwrap();
                if self.0.stopping.load(Ordering::SeqCst) {
                    break;
                }
                let s = self
                    .0
                    .queue
                    .lock()
                    .unwrap()
                    .start_next(self.0.max_concurrent);
                if let Some((job, flag)) = &s {
                    flags.insert(job.id, flag.clone());
                }
                s
            };
            let Some((job, flag)) = started else { break };
            let me = self.clone();
            std::thread::spawn(move || me.run_job(job, flag));
        }
    }

    fn run_job(&self, job: Job, cancel: Arc<AtomicBool>) {
        kova_core::applog::write(
            &self.0.config_dir,
            "INFO",
            &format!(
                "작업 시작: id={} profile={} kind={:?}",
                job.id, job.profile_id, job.kind
            ),
        );
        self.emit(SvcEvent::JobStarted {
            job_id: job.id,
            profile_id: job.profile_id.clone(),
            kind: job.kind,
        });
        let _awake = SleepGuard::new(); // 작업 중 절전 진입 방지(이 작업 스레드에만 적용, 끝나면 자동 해제)
        let (ok, message, snapshot_id) = match job.kind {
            Kind::Backup => self.run_backup_job(&job, &cancel),
            Kind::Verify => self.run_verify_job(&job),
            Kind::Gc => self.run_gc_job(&job),
            Kind::Image(d) => self.run_image_job(&job, d, &cancel),
            Kind::DeleteSnapshot => self.run_delete_snapshot_job(&job),
        };
        kova_core::applog::write(
            &self.0.config_dir,
            if ok { "INFO" } else { "ERROR" },
            &format!(
                "작업 종료: id={} profile={} kind={:?} ok={} msg={}",
                job.id, job.profile_id, job.kind, ok, message
            ),
        );
        // 실패·경고는 알림 기록에 남긴다(화면이 없는 서비스는 기록만, 표시는 앱이 한다). 사용자 취소는 알리지 않는다.
        // 저장소 잠금 충돌(다른 작업이 사용 중)은 일시적이라 알림·웹훅을 보내지 않는다.
        let busy = !ok && message.starts_with(BUSY_PREFIX);
        let is_alert = !busy
            && message != "취소됨"
            && (!ok || message.contains("복제 실패") || message.contains("이상 징후"));
        if is_alert {
            let _ = kova_core::alerts::AlertLog::new(&self.0.config_dir).push(
                kova_core::alerts::Alert {
                    at: Local::now().timestamp() as f64,
                    profile_id: job.profile_id.clone(),
                    level: if ok { "warning" } else { "error" }.into(),
                    message: message.clone(),
                    acknowledged: false,
                },
            );
        }
        // 원격 웹훅 알림 발송 (실패·경고 시 기본 발송, notify_on_success가 켜져 있으면 성공 시에도 발송)
        let st = self.0.store.settings();
        let should_notify = !st.webhook_url.trim().is_empty()
            && !busy
            && message != "취소됨"
            && (is_alert || (st.notify_on_success && ok));
        if should_notify {
            let url = st.webhook_url.clone();
            let level = if !ok {
                "error"
            } else if is_alert {
                "warning"
            } else {
                "info"
            };
            let title = format!("프로파일 {} ({:?})", job.profile_id, job.kind);
            let msg = message.clone();
            let cfg_dir = self.0.config_dir.clone();
            std::thread::spawn(move || {
                if let Err(e) = kova_core::notify::send_webhook(&url, level, &title, &msg) {
                    kova_core::applog::write(&cfg_dir, "WARN", &format!("웹훅 발송 실패: {e}"));
                }
            });
        }
        // 이벤트를 먼저 보낸 뒤 큐에서 빼야 "큐가 비었다 = 모든 이벤트가 나갔다"가 성립한다
        self.emit(SvcEvent::JobFinished {
            job_id: job.id,
            profile_id: job.profile_id.clone(),
            ok,
            message,
            snapshot_id,
        });
        if busy {
            self.note_lock_busy(&job.profile_id);
        }
        self.0.running_flags.lock().unwrap().remove(&job.id);
        self.0.queue.lock().unwrap().finish(job.id);
        self.pump();
    }

    /// 잠금 충돌을 프로파일에 기록해 정기 스케줄이 쿨다운 동안 다시 큐에 넣지 않게 한다(없어진 프로파일은 되살리지 않음).
    fn note_lock_busy(&self, profile_id: &str) {
        if let Some(mut p) = self.0.store.get(profile_id) {
            p.extra.insert(
                "lock_busy_at".into(),
                (Local::now().timestamp() as f64).into(),
            );
            let _ = self.0.store.upsert(p);
        }
    }

    /// 프로파일 ID, 저장소 경로, 스냅샷 ID 또는 "default"로부터 프로파일을 찾는다.
    fn resolve_profile(&self, id_or_target: &str) -> Option<Profile> {
        let t = id_or_target.trim();
        if t.is_empty() {
            return self.0.store.profiles().into_iter().next();
        }
        if let Some(p) = self.0.store.get(t) {
            return Some(p);
        }
        let profiles = self.0.store.profiles();
        if let Some(p) = profiles.iter().find(|p| p.repo_dir.eq_ignore_ascii_case(t)) {
            return Some(p.clone());
        }
        for p in &profiles {
            let repo = std::path::PathBuf::from(&p.repo_dir);
            let snap_json = repo.join("snapshots").join(format!("{t}.json"));
            let snap_enc = repo.join("snapshots").join(format!("{t}.json.enc"));
            if snap_json.exists() || snap_enc.exists() {
                if let Ok(cas) = kova_engine::storage::CasStorage::new(&repo) {
                    if let Ok(m) = cas.load_manifest(t) {
                        if let Some(ref pid) = m.profile_id {
                            if let Some(target) = profiles.iter().find(|pr| pr.id == *pid) {
                                return Some(target.clone());
                            }
                        }
                    }
                }
                if let Some(h) = open_history_if_exists(&repo) {
                    if let Ok(snaps) = h.list_snapshots() {
                        if let Some(s) = snaps.iter().find(|s| s.id == t) {
                            if !s.profile_id.is_empty() {
                                if let Some(target) =
                                    profiles.iter().find(|pr| pr.id == s.profile_id)
                                {
                                    return Some(target.clone());
                                }
                            }
                        }
                    }
                }
                return Some(p.clone());
            }
            if let Some(h) = open_history_if_exists(&repo) {
                if let Ok(snaps) = h.list_snapshots() {
                    if let Some(s) = snaps.iter().find(|s| s.id == t) {
                        if !s.profile_id.is_empty() {
                            if let Some(target) = profiles.iter().find(|pr| pr.id == s.profile_id) {
                                return Some(target.clone());
                            }
                        }
                        return Some(p.clone());
                    }
                }
            }
        }
        if t.eq_ignore_ascii_case("default") {
            return profiles.into_iter().next();
        }
        None
    }

    fn run_backup_job(&self, job: &Job, cancel: &AtomicBool) -> (bool, String, Option<String>) {
        let Some(prof) = self.resolve_profile(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        let mut opts = BackupOptions {
            sources: prof.sources.iter().map(PathBuf::from).collect(),
            repo: PathBuf::from(&prof.repo_dir),
            session_id: None,
            zstd_level: prof.compression_level,
            exclude: prof.exclude_patterns.clone(),
            profile_id: Some(prof.id.clone()),
            scan_from: None,
        };
        // 같은 저장소에 백업·GC·검증·복원이 동시에 쓰지 못하게 OS 파일 잠금을 먼저 잡는다
        let _repo_lock = match kova_core::lock::RepoLock::acquire_wait(&opts.repo, REPO_LOCK_WAIT) {
            Ok(l) => l,
            Err(kova_core::lock::LockError::Busy) => {
                return (
                    false,
                    format!("{BUSY_PREFIX}입니다. 잠시 후 다시 시도하세요"),
                    None,
                );
            }
            Err(e) => return (false, format!("저장소 잠금 실패: {e}"), None),
        };
        // 원본이 없으면(드라이브 분리 등) 엔진은 빈 백업을 "성공"으로 끝내므로 여기서 실패로 막는다
        let missing: Vec<String> = opts
            .sources
            .iter()
            .filter(|s| !s.exists())
            .map(|s| s.display().to_string())
            .collect();
        // 일부만 없으면(임시 폴더·바로가기 등) 있는 것만 백업하고 메시지에 남긴다. 전부 없으면 실패로 막는다
        let partial_missing = !missing.is_empty() && missing.len() < opts.sources.len();
        if partial_missing {
            opts.sources.retain(|s| s.exists());
        }
        let last_pct = Mutex::new(u64::MAX);
        let id = job.id;
        let on_event = |e: EngineEvent| {
            if let EngineEvent::Progress(p) = e {
                let mut l = last_pct.lock().unwrap();
                // 파일 백업 단계는 0~95%로 매핑하여 후속 정리(팩·보호·복제) 단계와 진행률이 겹치거나 뒤로 가지 않게 한다
                let scaled = (p.percent * 95 / 100).min(95);
                if *l == u64::MAX || scaled > *l {
                    *l = scaled;
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent: scaled,
                        current: p.current_file,
                    });
                }
            }
        };
        // 저장소 드라이브 여유 공간이 설정의 최소 기준보다 적으면 시작하지 않는다(디스크가 가득 차 백업이 중간에 깨지는 것을 막는다)
        let space_check = kova_core::diskspace::check_min_free(
            kova_core::diskspace::free_bytes(&opts.repo),
            self.0.store.settings().min_free_disk_gb,
        );
        // 암호화가 켜져 있는데 이 저장소가 아직 암호화되지 않았으면 평문으로 쓰지 않고 멈춘다(복구 코드는 설정 화면에서 켤 때만 보여 준다)
        let encrypt_missing = self.0.store.settings().encrypt_backups
            && !kova_core::repocrypto::is_enabled(&opts.repo);
        // VSS: 원본 드라이브를 스냅샷해 잠긴 파일도 같은 시점으로 읽는다. 못 만들면(권한·파일시스템) 직접 읽고 메시지에 남긴다
        let mut vss_note = String::new();
        let shadows = if self.0.store.settings().use_vss && space_check.is_ok() && !encrypt_missing
        {
            self.mark_vss(
                job.id,
                &format!("파일 백업 profile={} sources={:?}", prof.id, opts.sources),
            );
            match kova_core::vss::ShadowSet::create_for(&opts.sources) {
                Ok(set) => {
                    opts.scan_from = Some(opts.sources.iter().map(|p| set.map(p)).collect());
                    vss_note = format!(", VSS 스냅샷 {}개 사용", set.count());
                    Some(set)
                }
                Err(e) => {
                    vss_note = format!(", VSS 사용 못 해 직접 읽음({e})");
                    None
                }
            }
        } else {
            None
        };
        let res = if let Err(why) = space_check {
            Err(EngineError::Msg(why))
        } else if encrypt_missing {
            Err(EngineError::Msg(
                "백업 암호화가 켜져 있지만 이 저장소는 아직 암호화되지 않았습니다. 설정에서 저장을 눌러 암호화를 시작하세요"
                    .into(),
            ))
        } else if missing.is_empty() || partial_missing {
            let max_threads = self.0.store.settings().max_cpu_threads as usize;
            run_backup_with_threads(&opts, max_threads, &on_event, cancel)
        } else {
            Err(EngineError::Msg(format!(
                "원본 경로를 찾을 수 없음: {}",
                missing.join(", ")
            )))
        };
        drop(shadows); // 백업이 끝났으니 섀도 복사본 삭제
        self.unmark_vss(job.id);
        let now = Local::now().timestamp() as f64;
        // 결과 반영은 최신 파일을 다시 읽어 이 프로파일만 갱신한다 (실행 중 화면에서 바꾼 설정을 덮어쓰지 않음).
        // 작업 중에 프로파일이 지워졌으면 결과를 다시 써서 되살리지 않는다.
        let latest = self.0.store.get(&prof.id);
        let deleted = latest.is_none();
        let mut prof = latest.unwrap_or(prof);
        let out = match res {
            Ok(s) => {
                let had_previous = prof.last_snapshot_id.is_some();
                let manifest_path = opts
                    .repo
                    .join("snapshots")
                    .join(format!("{}.json", s.session_id));
                let suspect_cnt = kova_core::anomaly::count_suspect_in_manifest(&manifest_path);
                let anomaly = kova_core::anomaly::detect(
                    s.total_files,
                    s.new_files,
                    s.modified_files,
                    suspect_cnt,
                    had_previous,
                    &kova_core::anomaly::AnomalyConfig::default(),
                );
                prof.last_status = "success".into();
                prof.last_snapshot_id = Some(s.session_id.clone());
                prof.extra.insert("retry_count".into(), 0.into());
                prof.extra
                    .entry("last_verify_at")
                    .or_insert_with(|| now.into());
                prof.extra.entry("last_gc_at").or_insert_with(|| now.into());
                // 보존 정책: 랜섬웨어 이상 징후가 감지되면 정상 옛 스냅샷이 밀려 삭제되지 않도록 자동 보류한다
                let ret = if let Some(ref a) = anomaly {
                    kova_core::applog::write(
                        &self.0.config_dir,
                        "WARN",
                        &format!(
                            "랜섬웨어 이상 징후 감지로 보존 정리 보류: profile={} reason={}",
                            prof.id,
                            a.summary()
                        ),
                    );
                    kova_core::retention::RetentionResult::default()
                } else {
                    let policy = kova_core::retention::Policy {
                        retention_count: Some(prof.retention_count as usize),
                        retention_days: Some(prof.retention_days),
                        keep_weekly: prof
                            .extra
                            .get("keep_weekly")
                            .and_then(|v| v.as_u64())
                            .unwrap_or(0) as u32,
                        keep_monthly: prof
                            .extra
                            .get("keep_monthly")
                            .and_then(|v| v.as_u64())
                            .unwrap_or(0) as u32,
                    };
                    kova_core::retention::apply_for_profile(&opts.repo, &prof.id, policy)
                };
                if let Ok(mut h) = History::open(&opts.repo) {
                    let _ = h.sync_snapshots();
                }
                // 팩 컨테이너: 1시간 이상 된 낱개 블롭을 묶는다(이미 상단에서 _repo_lock 보유 중).
                let pack_note = if self.0.store.settings().pack_blobs {
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent: 96,
                        current: "블롭 팩 압축 정리 중...".into(),
                    });
                    match kova_core::packer::compact(
                        &opts.repo,
                        kova_core::gc::DEFAULT_MIN_AGE_SECS,
                        kova_core::packer::DEFAULT_PACK_BYTES,
                    ) {
                        Ok(r) if r.packed_blobs > 0 => {
                            format!(", 블롭 {}개를 팩 {}개로 묶음", r.packed_blobs, r.packs)
                        }
                        Ok(_) => String::new(),
                        Err(e) => format!(", 팩 묶기 실패: {e}"),
                    }
                } else {
                    String::new()
                };
                // 랜섬웨어 보호: 이번 백업으로 생긴 매니페스트·블롭을 읽기 전용(+매니페스트는 삭제 거부 ACL)으로 잠근다.
                // 삭제는 보존 정책·정리가 승인 경로(worm::remove_protected)로만 한다
                let protected = if self.0.store.settings().enable_ransomware_protection {
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent: 97,
                        current: "랜섬웨어 변조 차단 보호 적용 중...".into(),
                    });
                    let st = kova_core::worm::protect_new_files(&opts.repo);
                    Some(st.blobs + st.snapshots)
                } else {
                    None
                };
                let mut msg = if s.skipped_files.is_empty() {
                    format!("완료: {}개 항목, {}바이트", s.total_files, s.total_bytes)
                } else {
                    // 건너뛴 파일은 경로만 보인다(운영체제 오류 원문은 로그에 있으므로 메시지에서 뺀다)
                    let denied = s
                        .skipped_files
                        .iter()
                        .filter(|f| f.contains("os error 5") || f.contains("os error 32"))
                        .count();
                    format!(
                        "완료(건너뜀 {}개{}: {}{}): {}개 항목",
                        s.skipped_files.len(),
                        if denied > 0 {
                            format!(", 시스템이 잠근 파일 {denied}개")
                        } else {
                            String::new()
                        },
                        s.skipped_files
                            .iter()
                            .take(3)
                            .map(|f| f.split(" (").next().unwrap_or(f).to_string())
                            .collect::<Vec<_>>()
                            .join("; "),
                        if s.skipped_files.len() > 3 {
                            " 외"
                        } else {
                            ""
                        },
                        s.total_files
                    )
                };
                // 오프사이트(폴더/SMB) 복제: 설정돼 있으면 백업 직후 이어서 복제. 실패해도 로컬 백업은 성공으로 두고 메시지에 남긴다
                if let Some(dest) = prof
                    .extra
                    .get("offsite_dir")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                {
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent: 98,
                        current: "오프사이트 백업 복제 중...".into(),
                    });
                    let bw_limit = (self.0.store.settings().offsite_bw_limit_mbps as u64)
                        .saturating_mul(1024 * 1024);
                    match kova_core::offsite::replicate_with_limit(
                        &opts.repo,
                        std::path::Path::new(dest),
                        cancel,
                        bw_limit,
                    ) {
                        Ok(r) => {
                            msg.push_str(&format!(
                                ", 오프사이트 복제 {}개 스냅샷/{}개 블롭",
                                r.snapshots_copied, r.blobs_copied
                            ));
                            if let Some(w) = r.warning() {
                                msg.push_str(&format!(" (경고: {w})"));
                            }
                        }
                        Err(e) => msg.push_str(&format!(", 오프사이트 복제 실패: {e}")),
                    }
                }
                // 구글 드라이브 오프사이트: 앱에서 연결(로그인)돼 있고 제공자가 gdrive 일 때만. 실패해도 로컬 백업은 성공으로 둔다
                if self.0.store.settings().offsite_enabled
                    && self.0.store.settings().offsite_provider == "gdrive"
                    && kova_core::gdrive::is_connected(&self.0.store.dir)
                {
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent: 99,
                        current: "구글 드라이브 클라우드 복제 중...".into(),
                    });
                    let r = kova_core::gdrive::load_client(&self.0.store.dir).and_then(|creds| {
                        let mut c = kova_core::gdrive::DriveClient::open(
                            &self.0.store.dir,
                            kova_core::gdrive::Endpoints::default(),
                            creds,
                        )?;
                        kova_core::gdrive::replicate_to_drive(
                            &opts.repo,
                            &mut c,
                            &["Kova2 Backup", prof.id.as_str()],
                            cancel,
                        )
                    });
                    match r {
                        Ok(r) => {
                            msg.push_str(&format!(
                                ", 구글 드라이브 복제 {}개 스냅샷/{}개 블롭",
                                r.snapshots_uploaded, r.blobs_uploaded
                            ));
                            if let Some(w) = r.warning() {
                                msg.push_str(&format!(" (경고: {w})"));
                            }
                        }
                        Err(e) => msg.push_str(&format!(", 구글 드라이브 복제 실패: {e}")),
                    }
                }
                msg.push_str(&vss_note);
                msg.push_str(&pack_note);
                if let Some(n) = protected.filter(|n| *n > 0) {
                    msg.push_str(&format!(", 랜섬웨어 보호 {n}개 잠금"));
                }
                if partial_missing {
                    msg.push_str(&format!(
                        ", 원본 {}개 없어 제외: {}",
                        missing.len(),
                        missing.join(", ")
                    ));
                }
                if !ret.deleted_snapshots.is_empty() {
                    msg.push_str(&format!(
                        ", 보존 정책으로 옛 스냅샷 {}개 정리",
                        ret.deleted_snapshots.len()
                    ));
                }
                if let Some(a) = anomaly {
                    msg.push_str(&format!(
                        ", 랜섬웨어 이상 징후 감지(보존 정리 보류): {}",
                        a.summary()
                    ));
                }
                self.emit(SvcEvent::Progress {
                    job_id: id,
                    percent: 100,
                    current: "백업 완료".into(),
                });
                (true, msg, Some(s.session_id))
            }
            Err(e) => {
                prof.last_status = "failed".into();
                // 사용자 취소는 재시도하지 않는다(서비스 중지로 인한 취소는 다음 시작 후 재시도한다)
                let rc = if matches!(e, EngineError::Cancelled)
                    && !self.0.stopping.load(Ordering::SeqCst)
                {
                    MAX_AUTO_RETRIES as u64
                } else {
                    prof.extra
                        .get("retry_count")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0)
                        + 1
                };
                prof.extra.insert("retry_count".into(), rc.into());
                (false, e.to_string(), None)
            }
        };
        prof.last_run = Some(now);
        if deleted {
            kova_core::applog::write(
                &self.0.config_dir,
                "INFO",
                &format!(
                    "작업 중 프로파일이 삭제되어 결과를 저장하지 않음: {}",
                    prof.id
                ),
            );
        } else {
            let _ = self.0.store.upsert(prof);
        }
        out
    }

    /// 스냅샷 삭제: 저장소는 프로파일 설정에서만 찾는다(요청자가 경로를 정하지 못한다).
    fn run_delete_snapshot_job(&self, job: &Job) -> (bool, String, Option<String>) {
        let Some(prof) = self.resolve_profile(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        match kova_core::retention::delete_snapshot(std::path::Path::new(&prof.repo_dir), &job.arg)
        {
            Ok(m) => (true, m, None),
            Err(m) => (false, m, None),
        }
    }

    fn run_gc_job(&self, job: &Job) -> (bool, String, Option<String>) {
        let Some(prof) = self.resolve_profile(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        let repo = std::path::Path::new(&prof.repo_dir);
        let _repo_lock = match kova_core::lock::RepoLock::acquire_wait(repo, REPO_LOCK_WAIT) {
            Ok(l) => l,
            Err(kova_core::lock::LockError::Busy) => {
                return (
                    false,
                    format!("{BUSY_PREFIX}이라 정리를 건너뛰었습니다"),
                    None,
                );
            }
            Err(e) => return (false, format!("저장소 잠금 실패: {e}"), None),
        };
        self.emit(SvcEvent::Progress {
            job_id: job.id,
            percent: 50,
            current: "고아 블롭 및 저장소 정리 중...".into(),
        });
        // 1시간 미만의 새 블롭은 진행 중 백업 것일 수 있어 보호된다
        match kova_core::gc::prune_orphans(repo, kova_core::gc::DEFAULT_MIN_AGE_SECS) {
            Ok(r) => {
                let now = Local::now().timestamp() as f64;
                // 최신 프로파일을 다시 읽어 갱신한다(작업 중 바뀐 설정을 덮어쓰거나 지운 프로파일을 되살리지 않음)
                if let Some(mut p) = self.0.store.get(&prof.id) {
                    p.extra.insert("last_gc_at".into(), now.into());
                    let _ = self.0.store.upsert(p);
                }
                self.emit(SvcEvent::Progress {
                    job_id: job.id,
                    percent: 100,
                    current: "정리 완료".into(),
                });
                (
                    true,
                    format!(
                        "정리 완료: 고아 블롭 {}개, {}바이트 확보",
                        r.deleted_blobs, r.freed_bytes
                    ),
                    None,
                )
            }
            Err(e) => (false, e.to_string(), None),
        }
    }

    /// 볼륨 이미지: 저장 위치는 프로파일 저장소 아래 `images` 폴더로 고정하고 이름도 서비스가 정한다
    /// (이 서비스는 SYSTEM 권한이라 요청자가 출력 경로를 고르게 하지 않는다).
    fn run_image_job(
        &self,
        job: &Job,
        drive: char,
        cancel: &AtomicBool,
    ) -> (bool, String, Option<String>) {
        let Some(prof) = self.resolve_profile(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        // 폴더를 만들기 전에 거부할 수 있는 것은 먼저 거부한다(없는·이동식 드라이브)
        if !is_fixed_drive(drive) {
            return (
                false,
                format!("{drive}: 는 내장(고정) 드라이브가 아니라 이미지로 만들 수 없습니다"),
                None,
            );
        }
        let dir = std::path::Path::new(&prof.repo_dir).join("images");
        if let Err(e) = std::fs::create_dir_all(&dir) {
            return (false, format!("이미지 폴더를 만들 수 없음: {e}"), None);
        }
        // 볼륨 전체(다른 사용자 파일 포함)가 담기므로 SYSTEM·관리자만 읽게 한다. 못 하면 만들지 않는다.
        if let Err(e) = restrict_to_admins(&dir) {
            return (false, format!("이미지 폴더 권한 설정 실패: {e}"), None);
        }
        let partial_dir = dir.join(IMAGE_PARTIAL_DIR);
        if let Err(e) = std::fs::create_dir_all(&partial_dir) {
            return (false, format!("이미지 폴더를 만들 수 없음: {e}"), None);
        }
        let stale = remove_stale_partials(&partial_dir, Duration::from_secs(STALE_PARTIAL_SECS));
        if stale > 0 {
            kova_core::applog::write(
                &self.0.config_dir,
                "INFO",
                &format!("이전 실행이 남긴 미완성 이미지 {stale}개 정리"),
            );
        }
        if let Err(why) = kova_core::diskspace::check_min_free(
            kova_core::diskspace::free_bytes(&dir),
            self.0.store.settings().min_free_disk_gb,
        ) {
            return (false, why, None);
        }
        let name = format!("{}_{}.qblk", drive, Local::now().format("%Y%m%d_%H%M%S"));
        let out = dir.join(&name);
        if out.exists() {
            return (
                false,
                format!("같은 이름의 이미지가 이미 있음: {name}"),
                None,
            );
        }
        // 미완성 파일은 `_partial` 에 쓰고 끝난 뒤 옮긴다(중간에 서비스가 죽어도 완성 이미지처럼 보이지 않게)
        let part = partial_dir.join(&name);
        let id = job.id;
        let last = Mutex::new(u64::MAX);
        self.mark_vss(job.id, &format!("볼륨 이미지 {drive}:"));
        let res =
            kova_image::volume::capture_volume(&format!("{drive}:"), &part, 3, cancel, &|s| {
                let gb = s.raw_bytes >> 30;
                let mut l = last.lock().unwrap();
                if *l != gb {
                    *l = gb;
                    let total_gb = s.total_raw_bytes >> 30;
                    let percent = (s.raw_bytes * 100)
                        .checked_div(s.total_raw_bytes)
                        .map_or(0, |p| p.min(99));
                    self.emit(SvcEvent::Progress {
                        job_id: id,
                        percent,
                        current: format!("{gb}GB / {total_gb}GB 읽음"),
                    });
                }
            });
        self.unmark_vss(job.id);
        let res = res.map_err(|e| e.to_string()).and_then(|r| {
            std::fs::rename(&part, &out).map(|_| r).map_err(|e| {
                let _ = std::fs::remove_file(&part);
                format!("완성된 이미지를 옮기지 못함: {e}")
            })
        });
        match res {
            Ok((h, s)) => {
                let mut worm_note = "";
                if self.0.store.settings().enable_ransomware_protection
                    && kova_core::worm::protect_file(&out, true)
                {
                    worm_note = ", 랜섬웨어 보호 잠금";
                }
                (
                    true,
                    format!(
                        "이미지 완료: {} (사용 클러스터 {}, 압축 {}바이트{worm_note})",
                        out.display(),
                        h.used_clusters,
                        std::fs::metadata(&out)
                            .map(|m| m.len())
                            .unwrap_or(s.compressed_bytes)
                    ),
                    None,
                )
            }
            Err(e) => (false, e, None),
        }
    }

    fn run_verify_job(&self, job: &Job) -> (bool, String, Option<String>) {
        let Some(prof) = self.resolve_profile(&job.profile_id) else {
            return (false, format!("프로파일 없음: {}", job.profile_id), None);
        };
        let repo = std::path::Path::new(&prof.repo_dir);
        let _repo_lock = match kova_core::lock::RepoLock::acquire_wait(repo, REPO_LOCK_WAIT) {
            Ok(l) => l,
            Err(kova_core::lock::LockError::Busy) => {
                return (
                    false,
                    format!("{BUSY_PREFIX}이라 검증을 건너뛰었습니다"),
                    None,
                );
            }
            Err(e) => return (false, format!("저장소 잠금 실패: {e}"), None),
        };
        let deep = match job.arg.as_str() {
            "deep" => true,
            "quick" => false,
            _ => self.0.store.settings().verify_deep,
        };
        let mode = if deep {
            "정밀 검증"
        } else {
            "빠른 검증"
        };
        self.emit(SvcEvent::Progress {
            job_id: job.id,
            percent: 50,
            current: format!("{mode} 수행 중..."),
        });
        let res = match kova_core::verify::verify_repo(repo, deep) {
            Ok(r) if r.ok() => {
                self.emit(SvcEvent::Progress {
                    job_id: job.id,
                    percent: 100,
                    current: "검증 완료".into(),
                });
                (
                    true,
                    format!(
                        "{mode} 통과: 스냅샷 {}개, 파일 {}개, 청크 {}개",
                        r.snapshots, r.files, r.unique_chunks
                    ),
                    None,
                )
            }
            Ok(r) => (
                false,
                format!(
                    "{mode} 문제 {}건 (첫 항목: {:?})",
                    r.problems.len(),
                    r.problems[0]
                ),
                None,
            ),
            Err(e) => (false, e.to_string(), None),
        };
        let now = Local::now().timestamp() as f64;
        if let Some(mut p) = self.0.store.get(&prof.id) {
            p.extra.insert("last_verify_at".into(), now.into());
            let _ = self.0.store.upsert(p);
        }
        res
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

impl Default for SleepGuard {
    fn default() -> Self {
        Self::new()
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

fn kind_name(k: Kind) -> String {
    match k {
        Kind::Backup => "backup",
        Kind::Verify => "verify",
        Kind::Gc => "gc",
        Kind::Image(d) => return format!("image:{d}"),
        Kind::DeleteSnapshot => "delsnap",
    }
    .to_string()
}

/// 내부 이벤트를 IPC 이벤트로 바꾼다. 작업 번호에는 `base`(시각 기반 시작값)를 더한다.
fn to_wire(e: SvcEvent, base: u64) -> kova_ipc::Event {
    match e {
        SvcEvent::JobStarted {
            job_id,
            profile_id,
            kind,
        } => kova_ipc::Event::JobStarted {
            job_id: job_id + base,
            profile_id,
            kind: kind_name(kind),
        },
        SvcEvent::Progress {
            job_id,
            percent,
            current,
        } => kova_ipc::Event::Progress {
            job_id: job_id + base,
            percent,
            current,
        },
        SvcEvent::JobFinished {
            job_id,
            profile_id,
            ok,
            message,
            snapshot_id,
        } => kova_ipc::Event::JobFinished {
            job_id: job_id + base,
            profile_id,
            ok,
            message,
            snapshot_id,
        },
    }
}

const PRIVILEGE_REQUIRED: &str = "관리자 권한이 필요한 요청입니다(스냅샷 삭제·저장소 정리·볼륨 이미지·작업 취소). Kova2 앱을 관리자 권한으로 실행한 뒤 다시 시도하세요";

impl IpcHandler {
    fn refuse(&self, what: &str) -> kova_ipc::Reply {
        kova_core::applog::write(
            &self.0 .0.config_dir,
            "WARN",
            &format!("권한 없는 IPC 요청 거부: {what}"),
        );
        kova_ipc::Reply::Once(vec![kova_ipc::Event::Error {
            message: PRIVILEGE_REQUIRED.into(),
        }])
    }
}

impl kova_ipc::Handler for IpcHandler {
    fn on_error(&self, message: &str) {
        kova_core::applog::write(&self.0 .0.config_dir, "WARN", &format!("IPC: {message}"));
    }

    fn handle(&self, req: kova_ipc::Request, caller: kova_ipc::Caller) -> kova_ipc::Reply {
        use kova_ipc::{Event, Reply, Request};
        let base = self.0 .0.id_base;
        match req {
            Request::Ping => Reply::Once(vec![Event::Pong {
                pid: std::process::id(),
            }]),
            Request::Status => {
                let (waiting, running) = self.0.counts();
                Reply::Once(vec![Event::Status { waiting, running }])
            }
            Request::RunNow { profile_id, kind } => {
                // 빈 ID 는 "첫 프로파일"로 풀리므로 엉뚱한 프로파일에 작업이 걸리지 않게 거부한다
                if profile_id.trim().is_empty() {
                    return Reply::Once(vec![Event::Error {
                        message: "프로파일을 지정하지 않은 작업 요청은 처리하지 않습니다".into(),
                    }]);
                }
                let mut arg = String::new();
                let k = match kind.as_str() {
                    "backup" => Kind::Backup,
                    s if s.starts_with("delsnap:") => {
                        arg = s["delsnap:".len()..].to_string();
                        Kind::DeleteSnapshot
                    }
                    "verify" => Kind::Verify,
                    "gc" => Kind::Gc,
                    s if s.len() == 7
                        && s.starts_with("image:")
                        && s.as_bytes()[6].is_ascii_alphabetic() =>
                    {
                        Kind::Image(s.as_bytes()[6].to_ascii_uppercase() as char)
                    }
                    other => {
                        return Reply::Once(vec![Event::Error {
                            message: format!("알 수 없는 작업 종류: {other}"),
                        }])
                    }
                };
                // 일반 사용자 프로그램도 파이프에 접속할 수 있으므로 파괴적 작업은 관리자/SYSTEM 호출자만 받는다
                if !allowed_for_unprivileged(k) && !caller.privileged {
                    return self.refuse(&format!("RunNow {} profile={profile_id}", kind_name(k)));
                }
                let pid = self
                    .0
                    .resolve_profile(&profile_id)
                    .map(|p| p.id)
                    .unwrap_or(profile_id);
                Reply::Once(vec![match self.0.enqueue_with(&pid, k, &arg) {
                    Enqueue::Queued(job_id) => Event::Queued {
                        job_id: job_id + base,
                        merged: false,
                    },
                    Enqueue::Merged(job_id) => Event::Queued {
                        job_id: job_id + base,
                        merged: true,
                    },
                }])
            }
            Request::Cancel { job_id } => {
                // 일반 사용자 앱의 취소 버튼이 동작하도록 백업·검증 취소는 허용하고, 이미지·정리·삭제 취소는 관리자만 한다
                let light = job_id
                    .checked_sub(base)
                    .filter(|i| *i > 0)
                    .and_then(|i| self.0.job_kind(i))
                    .is_some_and(|k| matches!(k, Kind::Backup | Kind::Verify));
                if !caller.privileged && !light {
                    return self.refuse(&format!("Cancel job={job_id}"));
                }
                let found = job_id
                    .checked_sub(base)
                    .filter(|i| *i > 0)
                    .is_some_and(|i| self.0.cancel(i));
                Reply::Once(vec![Event::Cancelled { job_id, found }])
            }
            Request::Subscribe => {
                let rx = self.0.subscribe();
                // 유한 채널: 연결 쪽이 막히면 여기서 멈추고, 연결이 쓰기 시간 초과로 끊기면 send 가 실패해 끝난다
                let (tx, out) = sync_channel(256);
                std::thread::spawn(move || {
                    while let Ok(e) = rx.recv() {
                        if tx.send(to_wire(e, base)).is_err() {
                            break; // 연결이 끝남
                        }
                    }
                });
                Reply::Stream(out)
            }
            // 구글 드라이브 토큰: 파일은 SYSTEM·관리자만 열 수 있으므로 일반 권한 앱의 연결·상태·해제를 대신한다.
            // 토큰 내용을 돌려주는 요청은 없고, 토큰은 기록하지 않는다.
            Request::GdriveStatus => Reply::Once(vec![Event::GdriveStatus {
                connected: kova_core::gdrive::is_connected(&self.0 .0.config_dir),
            }]),
            Request::GdriveStoreToken { token } => {
                let cfg = &self.0 .0.config_dir;
                Reply::Once(vec![match kova_core::gdrive::store_token(cfg, &token.0) {
                    Ok(()) => {
                        kova_core::applog::write(cfg, "INFO", "구글 드라이브 연결: 토큰 저장");
                        Event::Done {
                            message: "구글 드라이브를 연결했습니다".into(),
                        }
                    }
                    Err(e) => Event::Error { message: e },
                }])
            }
            Request::GdriveDisconnect => {
                // 토큰 폐기는 네트워크 요청이라 IPC 작업 스레드를 막지 않게 따로 돌린다
                let cfg = self.0 .0.config_dir.clone();
                let (tx, rx) = sync_channel(1);
                std::thread::spawn(move || {
                    kova_core::gdrive::disconnect(&cfg, &kova_core::gdrive::Endpoints::default());
                    kova_core::applog::write(&cfg, "INFO", "구글 드라이브 연결 해제");
                    let _ = tx.send(Event::Done {
                        message: "연결을 해제했습니다.".into(),
                    });
                });
                Reply::Stream(rx)
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
    std::env::var_os("ProgramData")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Kova2")
        .join("config")
}

/// 스케줄러 틱 스레드: 30초마다 도래한 프로파일을 큐에 넣는다. 시작 직후 한 번 돌아 놓친 백업을 따라잡는다.
pub fn spawn_scheduler(sup: Supervisor, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        while !stop.load(std::sync::atomic::Ordering::Relaxed) {
            sup.tick(Local::now());
            for _ in 0..60 {
                if stop.load(std::sync::atomic::Ordering::Relaxed) {
                    return;
                }
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
        let store = ConfigStore::new(&cfg);
        store.upsert(p).unwrap();
        // 시험은 섀도 복사본을 만들지 않는다(실제 VSS 시험은 따로 --ignored 로 돌린다)
        let mut st = store.settings();
        st.use_vss = false;
        store.save_settings(&st).unwrap();
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
        assert!(matches!(
            evs.last(),
            Some(SvcEvent::JobFinished { ok: true, .. })
        ));
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
        assert!(rx
            .try_iter()
            .any(|e| matches!(e, SvcEvent::JobFinished { ok: false, .. })));
    }

    #[test]
    #[cfg(windows)]
    fn backup_is_refused_when_free_space_is_below_the_minimum() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.min_free_disk_gb = 100_000_000; // 어떤 디스크에도 없는 크기
        store.save_settings(&st).unwrap();
        let rx = s.subscribe();
        s.tick(Local::now());
        wait_idle(&s);
        let evs: Vec<SvcEvent> = rx.try_iter().collect();
        let msg = evs.iter().find_map(|e| match e {
            SvcEvent::JobFinished {
                ok: false, message, ..
            } => Some(message.clone()),
            _ => None,
        });
        assert!(
            msg.is_some_and(|m| m.contains("여유 공간이 부족")),
            "{evs:?}"
        );
        // 백업이 시작되지 않았으니 스냅샷이 없다
        assert_eq!(store.get("p1").unwrap().last_status, "failed");
        assert!(store.get("p1").unwrap().last_snapshot_id.is_none());
        // 기준을 0(검사 끔)으로 낮추면 같은 프로파일이 정상 백업된다
        st.min_free_disk_gb = 0;
        store.save_settings(&st).unwrap();
        let mut p = store.get("p1").unwrap();
        p.last_run = None;
        p.extra.insert("retry_count".into(), 0.into());
        store.upsert(p).unwrap();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert_eq!(store.get("p1").unwrap().last_status, "success");
    }

    /// 실제 VSS 로 백업한다(관리자 권한 필요): `cargo test -p kova-service vss -- --ignored`
    #[test]
    #[ignore]
    #[cfg(windows)]
    fn vss_backup_reads_from_shadow_and_records_original_paths() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.use_vss = true;
        st.min_free_disk_gb = 0;
        st.enable_ransomware_protection = false;
        store.save_settings(&st).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let evs: Vec<SvcEvent> = rx.try_iter().collect();
        let msg = evs.iter().find_map(|e| match e {
            SvcEvent::JobFinished {
                ok: true, message, ..
            } => Some(message.clone()),
            _ => None,
        });
        assert!(
            msg.as_ref()
                .is_some_and(|m| m.contains("VSS 스냅샷 1개 사용")),
            "{evs:?}"
        );
        // 매니페스트에는 섀도 경로가 아니라 원래 원본 경로가 기록되고, 복원하면 내용이 같다
        let sid = store.get("p1").unwrap().last_snapshot_id.unwrap();
        let manifest = fs::read_to_string(
            d.path()
                .join("repo")
                .join("snapshots")
                .join(format!("{sid}.json")),
        )
        .unwrap();
        assert!(!manifest.contains("GLOBALROOT"), "{manifest}");
        let out = d.path().join("out");
        let ro = kova_engine::ops::RestoreOptions {
            repo: d.path().join("repo"),
            session_id: sid,
            target: Some(out.clone()),
            only: None,
        };
        kova_engine::ops::run_restore(&ro, &|_| {}, &AtomicBool::new(false)).unwrap();
        let found = fs::read_dir(&out)
            .unwrap()
            .flatten()
            .any(|e| e.path().join("a.txt").is_file() || e.file_name() == "a.txt");
        assert!(found);
    }

    #[test]
    #[cfg(windows)]
    fn encryption_refuses_plain_backup_then_writes_only_ciphertext() {
        let keys = tempfile::tempdir().unwrap();
        std::env::set_var("KOVA2_KEY_DIR", keys.path());
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.min_free_disk_gb = 0;
        st.enable_ransomware_protection = false;
        st.encrypt_backups = true;
        store.save_settings(&st).unwrap();
        let repo = d.path().join("repo");
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let p = store.get("p1").unwrap();
        assert_eq!(p.last_status, "failed");
        assert!(!repo.join("blobs").exists() || kova_core::repocrypto::is_enabled(&repo));
        kova_core::repocrypto::enable(&repo).unwrap();
        let mut p = store.get("p1").unwrap();
        p.last_run = None;
        p.extra.insert("retry_count".into(), 0.into());
        store.upsert(p).unwrap();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert_eq!(store.get("p1").unwrap().last_status, "success");
        // 백업은 새 블롭을 팩 파일로 쓴다
        let blob = fs::read_dir(repo.join("packs"))
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|x| x == "pack"))
            .unwrap();
        assert_eq!(&fs::read(&blob).unwrap()[..4], b"ENC");
    }

    #[test]
    fn ransomware_protection_locks_new_files_and_retention_still_deletes() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.enable_ransomware_protection = true;
        st.min_free_disk_gb = 0;
        store.save_settings(&st).unwrap();
        let repo = d.path().join("repo");
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert_eq!(store.get("p1").unwrap().last_status, "success");
        let first = store.get("p1").unwrap().last_snapshot_id.unwrap();
        let manifest = repo.join("snapshots").join(format!("{first}.json"));
        let ro = |p: &std::path::Path| fs::metadata(p).unwrap().permissions().readonly();
        assert!(ro(&manifest), "매니페스트가 잠겨야 함");
        // 백업은 새 블롭을 팩 파일로 쓴다
        let blob = fs::read_dir(repo.join("packs"))
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|x| x == "pack"))
            .unwrap();
        assert!(ro(&blob) && fs::write(&blob, b"x").is_err());
        // 보존 개수를 1로 줄이고 새 백업을 만들면 잠긴 옛 스냅샷도 서비스가 승인 경로로 지운다
        let mut p = store.get("p1").unwrap();
        p.retention_count = 1;
        p.last_run = None;
        store.upsert(p).unwrap();
        fs::write(d.path().join("원본").join("b.txt"), "새 파일").unwrap();
        std::thread::sleep(Duration::from_millis(1200)); // 스냅샷 ID·시각이 초 단위라 겹치지 않게
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert!(!manifest.exists(), "옛 스냅샷이 정리되어야 함");
        // 임시 폴더를 지울 수 있도록 잠금 해제
        let _ = kova_core::worm::unprotect_directory(&repo, true);
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
        let done = rx
            .try_iter()
            .find_map(|e| match e {
                SvcEvent::JobFinished { ok, message, .. } => Some((ok, message)),
                _ => None,
            })
            .unwrap();
        assert!(done.0, "{}", done.1);
        assert!(
            done.1.contains("원본 1개 없어 제외") && done.1.contains(&gone),
            "{}",
            done.1
        );
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
        assert!(rx
            .try_iter()
            .any(|e| matches!(e, SvcEvent::JobFinished { ok: true, .. })));
        assert!(young.exists(), "1시간 미만 블롭은 지우지 않는다");
    }

    /// 실계정 시험(수동): 앱에서 구글 드라이브를 연결한 뒤 `cargo test -p kova-service real_drive -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn real_drive_backup_replicates_via_service() {
        let (d, s) = setup(true);
        let real = std::path::PathBuf::from(std::env::var("ProgramData").unwrap())
            .join("Kova2")
            .join("config");
        let cfg = d.path().join("cfg");
        for f in ["gdrive_client.json", "gdrive_token.bin"] {
            std::fs::copy(real.join(f), cfg.join(f)).unwrap();
        }
        let store = ConfigStore::new(cfg);
        let mut st = store.settings();
        st.offsite_provider = "gdrive".into();
        store.save_settings(&st).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let msg = rx
            .try_iter()
            .find_map(|e| {
                if let SvcEvent::JobFinished { message, .. } = e {
                    Some(message)
                } else {
                    None
                }
            })
            .expect("완료 이벤트");
        println!("MSG: {msg}");
        assert!(msg.contains("구글 드라이브 복제 1개 스냅샷"), "{msg}");
    }

    #[test]
    fn backup_replicates_to_offsite_dir_when_configured() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut p = store.get("p1").unwrap();
        let off = d.path().join("offsite");
        p.extra.insert(
            "offsite_dir".into(),
            off.to_string_lossy().to_string().into(),
        );
        store.upsert(p).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let msg = rx
            .try_iter()
            .find_map(|e| {
                if let SvcEvent::JobFinished {
                    ok: true, message, ..
                } = e
                {
                    Some(message)
                } else {
                    None
                }
            })
            .expect("성공 이벤트");
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
        assert_eq!(
            (v[0].level.as_str(), v[0].profile_id.as_str()),
            ("error", "p1")
        );
        let (d2, s2) = setup(true);
        s2.enqueue("p1", Kind::Backup);
        wait_idle(&s2);
        assert!(kova_core::alerts::AlertLog::new(&d2.path().join("cfg"))
            .list()
            .is_empty());
    }

    #[test]
    fn sleep_guard_can_be_created_and_dropped() {
        drop(SleepGuard::new());
    }

    #[test]
    fn resolve_profile_handles_id_repo_and_default() {
        let (d, s) = setup(true);
        // 1. Direct ID
        assert_eq!(s.resolve_profile("p1").map(|p| p.id), Some("p1".into()));
        // 2. Repo path
        let repo_str = d.path().join("repo").to_string_lossy().to_string();
        assert_eq!(
            s.resolve_profile(&repo_str).map(|p| p.id),
            Some("p1".into())
        );
        // 3. "default" and empty string
        assert_eq!(
            s.resolve_profile("default").map(|p| p.id),
            Some("p1".into())
        );
        assert_eq!(s.resolve_profile("").map(|p| p.id), Some("p1".into()));
        assert_eq!(s.resolve_profile("   ").map(|p| p.id), Some("p1".into()));
        // 4. Snapshot ID file check
        let snap_dir = d.path().join("repo").join("snapshots");
        std::fs::create_dir_all(&snap_dir).unwrap();
        std::fs::write(snap_dir.join("20261121_103641.json"), "{}").unwrap();
        assert_eq!(
            s.resolve_profile("20261121_103641").map(|p| p.id),
            Some("p1".into())
        );
        // 5. Unknown string
        assert_eq!(s.resolve_profile("nonexistent"), None);
    }

    #[test]
    fn manual_enqueue_and_unknown_profile() {
        let (_d, s) = setup(true);
        let rx = s.subscribe();
        assert!(matches!(
            s.enqueue("없음", Kind::Backup),
            Enqueue::Queued(_)
        ));
        wait_idle(&s);
        assert!(rx
            .try_iter()
            .any(|e| matches!(e, SvcEvent::JobFinished { ok: false, .. })));
    }

    #[test]
    fn verify_job_passes_after_backup() {
        let (d, s) = setup(true);
        s.tick(Local::now());
        wait_idle(&s);
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Verify);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobFinished { ok: true, ref message, .. } if message.contains("정밀 검증 통과")
        )));
        let log = fs::read_to_string(kova_core::applog::log_path(&d.path().join("cfg"))).unwrap();
        assert!(log.contains("작업 시작") && log.contains("작업 종료"));
    }

    #[test]
    fn repo_lock_blocks_concurrent_backup_and_verify() {
        let (d, s) = setup(true);
        let repo = d.path().join("repo");
        let _held = kova_core::lock::RepoLock::try_acquire(&repo).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobFinished { ok: false, ref message, .. } if message.contains("다른 작업이 저장소를 사용 중")
        )));
    }

    #[test]
    fn maintenance_tick_queues_deep_verify_and_gc_after_interval() {
        let (d, s) = setup(true);
        let now = Local::now();
        s.tick(now);
        wait_idle(&s);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut p = store.get("p1").unwrap();
        p.auto_backup_enabled = false;
        let eight_days_ago = now.timestamp() as f64 - 8.0 * 86_400.0;
        p.extra
            .insert("last_verify_at".into(), eight_days_ago.into());
        p.extra.insert("last_gc_at".into(), eight_days_ago.into());
        store.upsert(p).unwrap();

        let rx = s.subscribe();
        assert_eq!(s.tick(now), 1);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobStarted {
                kind: Kind::Verify,
                ..
            }
        )));
        // 검증이 끝난 직후 다음 틱에서는 밀린 GC가 예약된다
        let rx2 = s.subscribe();
        assert_eq!(s.tick(now), 1);
        wait_idle(&s);
        assert!(rx2
            .try_iter()
            .any(|e| matches!(e, SvcEvent::JobStarted { kind: Kind::Gc, .. })));
    }

    #[test]
    fn ransomware_anomaly_pauses_retention_and_emits_alert() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.enable_ransomware_protection = false;
        store.save_settings(&st).unwrap();
        let mut p = store.get("p1").unwrap();
        p.retention_count = 1;
        store.upsert(p).unwrap();

        // 1차 정상 백업
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let first_sid = store.get("p1").unwrap().last_snapshot_id.unwrap();

        // 랜섬웨어 의심 확장자(.locked) 파일 6개 생성 후 2차 백업
        let src = d.path().join("원본");
        for i in 0..6 {
            fs::write(src.join(format!("doc{i}.locked")), "encrypted").unwrap();
        }
        std::thread::sleep(Duration::from_millis(1200));
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);

        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobFinished { ok: true, ref message, .. } if message.contains("이상 징후")
        )));
        // retention_count=1 이더라도 이상 징후 감지 시 1차 정상 스냅샷이 삭제되지 않고 보존된다
        let first_manifest = d
            .path()
            .join("repo")
            .join("snapshots")
            .join(format!("{first_sid}.json"));
        assert!(first_manifest.exists(), "정상 옛 스냅샷이 보존되어야 함");
        // 경고 알림도 기록되어야 함
        let alerts = kova_core::alerts::AlertLog::new(&d.path().join("cfg")).list();
        assert!(alerts
            .iter()
            .any(|a| a.level == "warning" && a.message.contains("이상 징후")));
    }

    #[test]
    fn image_job_rejects_non_fixed_drive_and_leaves_no_output() {
        let (d, s) = setup(true);
        let rx = s.subscribe();
        // 존재하지 않는 드라이브(Q:)는 고정 드라이브가 아니라 섀도 복사본을 만들기 전에 거부된다
        s.enqueue("p1", Kind::Image('Q'));
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobFinished { ok: false, ref message, .. } if message.contains("고정")
        )));
        let imgs = d.path().join("repo").join("images");
        assert!(!imgs.exists() || std::fs::read_dir(imgs).unwrap().count() == 0);
    }

    #[tokio::test]
    async fn run_now_over_pipe_streams_events_to_subscriber() {
        let (_d, s) = setup(true);
        let name = format!(r"\\.\pipe\kova-svc-test-{}", std::process::id());
        let h: Arc<dyn kova_ipc::Handler> = Arc::new(IpcHandler(s));
        let n2 = name.clone();
        tokio::spawn(async move {
            let _ = kova_ipc::serve(&n2, h).await;
        });
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        let n3 = name.clone();
        let sub = tokio::spawn(async move {
            kova_ipc::request_until(&n3, &kova_ipc::Request::Subscribe, |e| {
                matches!(e, kova_ipc::Event::JobFinished { .. })
            })
            .await
        });
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let q = kova_ipc::request(
            &name,
            &kova_ipc::Request::RunNow {
                profile_id: "p1".into(),
                kind: "backup".into(),
            },
        )
        .await
        .unwrap();
        assert!(matches!(
            q[0],
            kova_ipc::Event::Queued { merged: false, .. }
        ));
        let evs = sub.await.unwrap().unwrap();
        assert!(matches!(
            evs.first(),
            Some(kova_ipc::Event::JobStarted { .. })
        ));
        assert!(matches!(
            evs.last(),
            Some(kova_ipc::Event::JobFinished { ok: true, .. })
        ));
        let bad = kova_ipc::request(
            &name,
            &kova_ipc::Request::RunNow {
                profile_id: "p1".into(),
                kind: "format".into(),
            },
        )
        .await
        .unwrap();
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
        fs::write(
            snaps.join("other1.json"),
            r#"{"id":"other1","created_at":1,"profile_id":"zzz"}"#,
        )
        .unwrap();
        fs::write(
            snaps.join("other2.json"),
            r#"{"id":"other2","created_at":2,"profile_id":"zzz"}"#,
        )
        .unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        std::thread::sleep(Duration::from_millis(1100)); // 세션 ID 가 초 단위라 겹치지 않게
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        let mine: Vec<_> = fs::read_dir(&snaps)
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| !n.starts_with("other"))
            .collect();
        assert_eq!(
            mine.len(),
            1,
            "retention_count=1 이면 내 스냅샷은 1개만 남아야 함: {mine:?}"
        );
        assert!(snaps.join("other1.json").exists() && snaps.join("other2.json").exists());
        assert!(rx.try_iter().any(
            |e| matches!(e, SvcEvent::JobFinished { ref message, .. } if message.contains("정리"))
        ));
    }

    #[test]
    fn resolve_profile_disambiguates_shared_repo_by_manifest_profile_id() {
        let (d, s) = setup(false);
        let cfg = ConfigStore::new(d.path().join("cfg"));
        let mut p2 = Profile::new("p2", "프로파일 2");
        p2.repo_dir = d.path().join("repo").to_string_lossy().to_string();
        cfg.upsert(p2).unwrap();
        // p1 and p2 share repo_dir.
        let snaps = d.path().join("repo/snapshots");
        fs::create_dir_all(&snaps).unwrap();
        fs::write(
            snaps.join("snap_for_p2.json"),
            r#"{"id":"snap_for_p2","profile_id":"p2","files":[{"path":"test","size":10}]}"#,
        )
        .unwrap();
        let resolved = s.resolve_profile("snap_for_p2");
        assert_eq!(resolved.as_ref().map(|p| p.id.as_str()), Some("p2"));
    }

    #[test]
    fn resolve_profile_disambiguates_shared_repo_encrypted_snapshot_by_history() {
        let (d, s) = setup(false);
        let cfg = ConfigStore::new(d.path().join("cfg"));
        let mut p2 = Profile::new("p2", "프로파일 2");
        p2.repo_dir = d.path().join("repo").to_string_lossy().to_string();
        cfg.upsert(p2).unwrap();

        let repo = d.path().join("repo");
        let snaps = repo.join("snapshots");
        fs::create_dir_all(&snaps).unwrap();
        // Encrypted snapshot file (.enc cannot be loaded as plaintext json by CasStorage)
        fs::write(snaps.join("snap_enc_p2.json.enc"), b"cipher_data").unwrap();

        // Record in SQLite History
        let _ = History::open(&repo).unwrap();
        let conn = kova_core::rusqlite::Connection::open(repo.join("metadata.db")).unwrap();
        conn.execute(
            "INSERT INTO snapshots_meta (id, profile_id) VALUES (?1, ?2)",
            kova_core::rusqlite::params!["snap_enc_p2", "p2"],
        )
        .unwrap();

        let resolved = s.resolve_profile("snap_enc_p2");
        assert_eq!(resolved.as_ref().map(|p| p.id.as_str()), Some("p2"));
    }

    fn first_error(r: kova_ipc::Reply) -> Option<String> {
        match r {
            kova_ipc::Reply::Once(v) => v.into_iter().find_map(|e| match e {
                kova_ipc::Event::Error { message } => Some(message),
                _ => None,
            }),
            kova_ipc::Reply::Stream(_) => None,
        }
    }

    fn run_now(kind: &str, pid: &str) -> kova_ipc::Request {
        kova_ipc::Request::RunNow {
            profile_id: pid.into(),
            kind: kind.into(),
        }
    }

    #[test]
    fn ipc_refuses_destructive_requests_from_unprivileged_callers() {
        use kova_ipc::{Caller, Handler};
        let (_d, s) = setup(true);
        let h = IpcHandler(s.clone());
        let user = Caller { privileged: false };
        for kind in ["delsnap:20261121_000000", "gc", "image:C"] {
            let msg = first_error(h.handle(run_now(kind, "p1"), user));
            assert!(
                msg.as_deref().is_some_and(|m| m.contains("관리자 권한")),
                "{kind}: {msg:?}"
            );
        }
        assert!(
            first_error(h.handle(kova_ipc::Request::Cancel { job_id: 1 }, user))
                .is_some_and(|m| m.contains("관리자 권한"))
        );
        assert_eq!(s.counts(), (0, 0), "거부된 요청은 큐에 들어가면 안 됨");
        // 백업·검증·상태는 일반 사용자도 된다
        assert!(first_error(h.handle(run_now("backup", "p1"), user)).is_none());
        wait_idle(&s);
        assert!(first_error(h.handle(run_now("verify", "p1"), user)).is_none());
        wait_idle(&s);
        assert!(first_error(h.handle(kova_ipc::Request::Status, user)).is_none());
        // 관리자 호출자는 정리 요청을 낼 수 있다
        let admin = Caller { privileged: true };
        assert!(first_error(h.handle(run_now("gc", "p1"), admin)).is_none());
        wait_idle(&s);
        // 모르는 종류는 권한과 무관하게 "알 수 없는 작업"
        assert!(first_error(h.handle(run_now("format", "p1"), admin))
            .is_some_and(|m| m.contains("알 수 없는")));
    }

    /// 일반 사용자 앱이 받은 구글 토큰을 서비스가 저장하고, 상태는 연결 여부만 돌려준다.
    #[test]
    fn gdrive_token_is_stored_by_service_and_status_reveals_only_connected() {
        use kova_ipc::{Caller, Event, Handler, Request, Secret};
        let (d, s) = setup(true);
        let h = IpcHandler(s.clone());
        let user = Caller { privileged: false };
        let status = |h: &IpcHandler| match h.handle(Request::GdriveStatus, user) {
            kova_ipc::Reply::Once(v) => v,
            kova_ipc::Reply::Stream(_) => panic!("Once 여야 함"),
        };
        assert_eq!(status(&h), vec![Event::GdriveStatus { connected: false }]);
        let bad = Request::GdriveStoreToken {
            token: Secret("garbage".into()),
        };
        assert!(first_error(h.handle(bad, user)).is_some());
        assert_eq!(status(&h), vec![Event::GdriveStatus { connected: false }]);
        let tok = r#"{"refresh":"RT-secret","access":"AT-secret","expires_at":0}"#;
        let ok = h.handle(
            Request::GdriveStoreToken {
                token: Secret(tok.into()),
            },
            user,
        );
        assert!(
            matches!(ok, kova_ipc::Reply::Once(ref v) if matches!(v[..], [Event::Done { .. }]))
        );
        assert_eq!(status(&h), vec![Event::GdriveStatus { connected: true }]);
        // 토큰은 로그에 남지 않는다
        let log = fs::read_to_string(kova_core::applog::log_path(&d.path().join("cfg")))
            .unwrap_or_default();
        assert!(log.contains("토큰 저장"), "{log}");
        assert!(!log.contains("RT-secret") && !log.contains("AT-secret"));
    }

    #[test]
    fn run_now_with_empty_profile_id_is_refused() {
        use kova_ipc::{Caller, Handler};
        let (_d, s) = setup(true);
        let h = IpcHandler(s.clone());
        for pid in ["", "   "] {
            assert!(first_error(h.handle(run_now("backup", pid), Caller::default())).is_some());
        }
        assert_eq!(s.counts(), (0, 0));
    }

    #[test]
    fn wire_job_ids_start_from_time_and_fit_the_apps_i32() {
        for secs in [0i64, 1, 1_999_999, 2_000_000, 1_763_700_000, i64::MAX] {
            let b = wire_id_base(secs);
            assert!(b + 1_000_000 < i32::MAX as u64, "{secs} -> {b}");
        }
        assert_ne!(wire_id_base(1_763_700_000), wire_id_base(1_763_700_001));
    }

    #[test]
    fn cancel_over_ipc_translates_the_wire_job_id() {
        use kova_ipc::{Caller, Handler};
        let (d, s) = setup(true);
        let _held = kova_core::lock::RepoLock::try_acquire(&d.path().join("repo")).unwrap();
        let h = IpcHandler(s.clone());
        let admin = Caller { privileged: true };
        let kova_ipc::Reply::Once(v) = h.handle(run_now("backup", "p1"), admin) else {
            panic!()
        };
        let Some(kova_ipc::Event::Queued { job_id, .. }) = v.first().cloned() else {
            panic!("{v:?}")
        };
        assert!(job_id > s.0.id_base);
        // 잠금 대기(3초) 중이라 아직 실행 중이다
        let kova_ipc::Reply::Once(c) = h.handle(kova_ipc::Request::Cancel { job_id }, admin) else {
            panic!()
        };
        assert_eq!(
            c,
            vec![kova_ipc::Event::Cancelled {
                job_id,
                found: true
            }]
        );
        // 내부 번호(1)를 그대로 보내면 다른 작업으로 취급되지 않는다
        let kova_ipc::Reply::Once(c) = h.handle(kova_ipc::Request::Cancel { job_id: 1 }, admin)
        else {
            panic!()
        };
        assert!(matches!(
            c[0],
            kova_ipc::Event::Cancelled { found: false, .. }
        ));
        wait_idle(&s);
    }

    #[test]
    fn regular_schedule_slot_resets_exhausted_retry_count() {
        let (d, s) = setup(false); // 원본이 없어 백업은 실패한다
        let store = ConfigStore::new(d.path().join("cfg"));
        let now = Local::now();
        let mut p = store.get("p1").unwrap();
        p.schedule_type = "interval_hours".into();
        p.schedule_value = "1".into();
        p.last_status = "failed".into();
        p.last_run = Some(now.timestamp() as f64 - 2.0 * 3600.0);
        p.extra
            .insert("retry_count".into(), (MAX_AUTO_RETRIES as u64).into());
        store.upsert(p.clone()).unwrap();
        assert!(regular_slot_due(&p, now));
        assert_eq!(s.tick(now), 1);
        wait_idle(&s);
        // 0 으로 되돌린 뒤 이번 실패가 1회로 기록된다(한도 위로 계속 쌓이지 않음)
        assert_eq!(
            kova_core::schedule::retry_count(&store.get("p1").unwrap()),
            1
        );

        // 재시도 회차는 정기 회차가 아니다
        let mut r = store.get("p1").unwrap();
        r.schedule_value = "24".into();
        r.last_run = Some(now.timestamp() as f64 - 3600.0);
        assert!(should_run(&r, now) && !regular_slot_due(&r, now));
    }

    #[test]
    fn lock_busy_is_not_alerted_cools_down_and_does_not_resurrect_profile() {
        let (d, s) = setup(true);
        let store = ConfigStore::new(d.path().join("cfg"));
        let mut st = store.settings();
        st.webhook_url = String::new();
        store.save_settings(&st).unwrap();
        let held = kova_core::lock::RepoLock::try_acquire(&d.path().join("repo")).unwrap();
        let rx = s.subscribe();
        s.enqueue("p1", Kind::Backup);
        wait_idle(&s);
        assert!(rx.try_iter().any(|e| matches!(
            e,
            SvcEvent::JobFinished { ok: false, ref message, .. } if message.starts_with(BUSY_PREFIX)
        )));
        assert!(
            kova_core::alerts::AlertLog::new(&d.path().join("cfg"))
                .list()
                .is_empty(),
            "잠금 충돌은 알림을 남기지 않는다"
        );
        let p = store.get("p1").unwrap();
        assert!(lock_busy_recently(&p, Local::now().timestamp() as f64));
        // 스케줄이 도래했어도(last_run 없음) 쿨다운 동안은 다시 큐에 넣지 않는다
        assert_eq!(s.tick(Local::now()), 0);

        // 잠금 대기 중에 프로파일이 지워지면 결과 기록으로 되살아나지 않는다
        s.enqueue("p1", Kind::Backup);
        std::thread::sleep(Duration::from_millis(300));
        store.delete("p1").unwrap();
        wait_idle(&s);
        assert!(store.get("p1").is_none());
        drop(held);
    }

    #[test]
    fn shutdown_cancels_running_jobs_and_starts_no_new_ones() {
        let (d, s) = setup(true);
        let held = kova_core::lock::RepoLock::try_acquire(&d.path().join("repo")).unwrap();
        s.enqueue("p1", Kind::Backup);
        assert_eq!(s.counts().1, 1);
        let mut waits = 0;
        assert!(s.shutdown(Duration::from_secs(20), || waits += 1));
        assert_eq!(s.counts(), (0, 0));
        s.enqueue("p1", Kind::Backup);
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(s.counts(), (1, 0), "중지 중에는 새 작업을 시작하지 않는다");
        assert_eq!(s.tick(Local::now()), 0);
        drop(held);
    }

    #[test]
    fn leftover_vss_markers_are_reported_once() {
        let (d, s) = setup(true);
        let cfg = d.path().join("cfg");
        fs::write(
            cfg.join("vss_inflight_7.txt"),
            "2026-11-21 10:00:00 파일 백업",
        )
        .unwrap();
        assert_eq!(s.report_leftover_vss(), 1);
        assert!(!cfg.join("vss_inflight_7.txt").exists());
        assert_eq!(s.report_leftover_vss(), 0);
        let log = fs::read_to_string(kova_core::applog::log_path(&cfg)).unwrap();
        assert!(log.contains("VSS 섀도 복사본을 정리하지 못했을 수"));
    }

    #[test]
    fn resolve_profile_does_not_create_repo_folders() {
        let (d, s) = setup(true);
        let repo = d.path().join("repo");
        assert!(!repo.exists());
        assert_eq!(s.resolve_profile("없는스냅샷"), None);
        assert!(!repo.exists(), "조회만으로 저장소 폴더·DB 를 만들면 안 됨");
    }

    #[test]
    fn stale_partial_images_are_removed_but_fresh_ones_kept() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("C_20261121_000000.qblk");
        fs::write(&f, b"part").unwrap();
        assert_eq!(
            remove_stale_partials(d.path(), Duration::from_secs(3600)),
            0
        );
        assert!(f.exists());
        assert_eq!(remove_stale_partials(d.path(), Duration::ZERO), 1);
        assert!(!f.exists());
    }

    #[test]
    fn subscriber_that_never_reads_is_dropped_when_full() {
        let (_d, s) = setup(true);
        let rx = s.subscribe();
        for i in 0..(SUB_CAPACITY as u64 + 5) {
            s.emit(SvcEvent::Progress {
                job_id: i,
                percent: 0,
                current: String::new(),
            });
        }
        assert!(s.0.subs.lock().unwrap().is_empty());
        assert_eq!(rx.try_iter().count(), SUB_CAPACITY);
    }
}
