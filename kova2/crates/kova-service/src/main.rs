//! KovaSvc: `--console` 로 콘솔 실행, 인자 없으면 서비스 디스패처로 실행.
//! `--install` / `--uninstall`: 서비스 등록·제거(관리자 권한 필요, 사용자가 직접 승인·실행한다).
use kova_service::{default_config_dir, spawn_scheduler, IpcHandler, Supervisor};
use std::ffi::OsString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use windows_service::{
    define_windows_service,
    service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    },
    service_control_handler::{self, ServiceControlHandlerResult},
    service_dispatcher,
};

const SERVICE_NAME: &str = "KovaSvc";
/// 일반 사용자(AU)는 상태 조회만 허용하고, 서비스 중지·설정 변경·삭제는 SYSTEM(SY)·Administrators(BA)만 허용한다.
const SERVICE_HARDENED_SDDL: &str =
    "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWLOCRRC;;;AU)";

define_windows_service!(ffi_service_main, service_main);

/// 중지 요청 뒤 실행 중 작업(취소 신호를 받은)이 끝나기를 기다리는 최대 시간.
const STOP_WAIT: Duration = Duration::from_secs(25);

fn start_core(rt: &tokio::runtime::Runtime) -> (Supervisor, Arc<AtomicBool>) {
    let sup = Supervisor::new(default_config_dir(), 2);
    sup.report_leftover_vss();
    let stop = Arc::new(AtomicBool::new(false));
    spawn_scheduler(sup.clone(), stop.clone());
    let handler: Arc<dyn kova_ipc::Handler> = Arc::new(IpcHandler(sup.clone()));
    rt.spawn(async move {
        if let Err(e) = kova_ipc::serve(kova_ipc::PIPE_NAME, handler).await {
            kova_core::applog::write(
                &default_config_dir(),
                "ERROR",
                &format!("IPC 파이프를 열 수 없음: {e}"),
            );
        }
    });
    (sup, stop)
}

fn service_main(_args: Vec<OsString>) {
    let cfg = default_config_dir();
    let fail = |what: &str, e: &dyn std::fmt::Display| {
        kova_core::applog::write(&cfg, "ERROR", &format!("서비스 시작 실패({what}): {e}"));
    };
    let rt = match tokio::runtime::Runtime::new() {
        Ok(rt) => rt,
        Err(e) => return fail("비동기 런타임", &e),
    };
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let handler = move |c| match c {
        ServiceControl::Stop | ServiceControl::Shutdown => {
            let _ = tx.send(());
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = match service_control_handler::register(SERVICE_NAME, handler) {
        Ok(s) => s,
        Err(e) => return fail("제어 처리기 등록", &e),
    };
    let set = |s: ServiceState, accept: ServiceControlAccept, checkpoint: u32, wait: Duration| {
        let _ = status.set_service_status(ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: s,
            controls_accepted: accept,
            exit_code: ServiceExitCode::Win32(0),
            checkpoint,
            wait_hint: wait,
            process_id: None,
        });
    };
    set(
        ServiceState::Running,
        ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
        0,
        Duration::default(),
    );
    let (sup, stop) = start_core(&rt);
    let _ = rx.recv();
    stop.store(true, Ordering::Relaxed);
    // 중지 중임을 알리고, 실행 중 작업을 취소시킨 뒤(섀도 복사본·잠금 정리) 끝날 때까지 기다린다
    let mut checkpoint = 1;
    set(
        ServiceState::StopPending,
        ServiceControlAccept::empty(),
        checkpoint,
        STOP_WAIT + Duration::from_secs(5),
    );
    let done = sup.shutdown(STOP_WAIT, || {
        checkpoint += 1;
        set(
            ServiceState::StopPending,
            ServiceControlAccept::empty(),
            checkpoint,
            Duration::from_secs(5),
        );
    });
    if !done {
        kova_core::applog::write(
            &cfg,
            "WARN",
            &format!(
                "서비스 중지: {}초 안에 끝나지 않은 작업을 두고 종료합니다",
                STOP_WAIT.as_secs()
            ),
        );
    }
    set(
        ServiceState::Stopped,
        ServiceControlAccept::empty(),
        0,
        Duration::default(),
    );
}

fn install() -> Result<(), Box<dyn std::error::Error>> {
    use windows_service::service::{
        ServiceAccess, ServiceErrorControl, ServiceInfo, ServiceStartType,
    };
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    let mgr = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
    )?;
    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from("Kova 백업 서비스"),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: std::env::current_exe()?,
        launch_arguments: vec![],
        dependencies: vec![],
        account_name: None, // LocalSystem
        account_password: None,
    };
    let svc = mgr.create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START)?;
    svc.set_description("Kova 자동 백업·검증·오프사이트 복제 서비스")?;
    harden_service(SERVICE_NAME);
    println!("서비스 {SERVICE_NAME} 등록 완료(자동 시작·자기보호·장애 자동재시작). 시작: sc start {SERVICE_NAME}");
    Ok(())
}

fn harden_service(name: &str) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    // 업그레이드로 설치 폴더가 바뀌어도 서비스가 새 실행 파일을 가리키게 한다(경로는 따옴표로 감싸 공백 경로 문제를 막는다)
    if let Ok(exe) = std::env::current_exe() {
        let _ = std::process::Command::new("sc.exe")
            .args(["config", name, "binPath="])
            .arg(format!("\"{}\"", exe.display()))
            .creation_flags(CREATE_NO_WINDOW)
            .output();
    }
    let _ = std::process::Command::new("sc.exe")
        .args([
            "failure",
            name,
            "reset=",
            "86400",
            "actions=",
            "restart/5000/restart/15000/restart/60000",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
    let _ = std::process::Command::new("sc.exe")
        .args(["sdset", name, SERVICE_HARDENED_SDDL])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
}

fn uninstall() -> Result<(), Box<dyn std::error::Error>> {
    use windows_service::service::ServiceAccess;
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    let mgr = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
    let svc = mgr.open_service(
        SERVICE_NAME,
        ServiceAccess::DELETE | ServiceAccess::STOP | ServiceAccess::QUERY_STATUS,
    )?;
    let _ = svc.stop(); // 실행 중이 아니면 오류가 나도 무시
    svc.delete()?;
    println!("서비스 {SERVICE_NAME} 제거 표시 완료(중지 후 완전히 사라집니다)");
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().any(|a| a == "--install") {
        return install();
    }
    if std::env::args().any(|a| a == "--harden") {
        // 업그레이드 설치에서는 --install 을 건너뛰므로, 자기보호(SDDL·장애 재시작)만 다시 적용한다.
        harden_service(SERVICE_NAME);
        return Ok(());
    }
    if std::env::args().any(|a| a == "--uninstall") {
        return uninstall();
    }
    if std::env::args().any(|a| a == "--console") {
        let rt = tokio::runtime::Runtime::new()?;
        println!(
            "KovaSvc console, pipe {}, config {}",
            kova_ipc::PIPE_NAME,
            default_config_dir().display()
        );
        let _core = start_core(&rt);
        rt.block_on(std::future::pending::<()>());
        return Ok(());
    }
    service_dispatcher::start(SERVICE_NAME, ffi_service_main)?;
    Ok(())
}
