//! KovaSvc: `--console` 로 콘솔 실행, 인자 없으면 서비스 디스패처로 실행.
//! `--install` / `--uninstall`: 서비스 등록·제거(관리자 권한 필요, 사용자가 직접 승인·실행한다).
use kova_service::{default_config_dir, spawn_scheduler, IpcHandler, Supervisor};
use std::ffi::OsString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use windows_service::{
    define_windows_service,
    service::{ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus, ServiceType},
    service_control_handler::{self, ServiceControlHandlerResult},
    service_dispatcher,
};

const SERVICE_NAME: &str = "KovaSvc";

define_windows_service!(ffi_service_main, service_main);

fn start_core(rt: &tokio::runtime::Runtime) -> Arc<AtomicBool> {
    let sup = Supervisor::new(default_config_dir(), 2);
    let stop = Arc::new(AtomicBool::new(false));
    spawn_scheduler(sup.clone(), stop.clone());
    let handler: Arc<dyn kova_ipc::Handler> = Arc::new(IpcHandler(sup));
    rt.spawn(async move {
        let _ = kova_ipc::serve(kova_ipc::PIPE_NAME, handler).await;
    });
    stop
}

fn service_main(_args: Vec<OsString>) {
    let rt = tokio::runtime::Runtime::new().unwrap();
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let handler = move |c| match c {
        ServiceControl::Stop => {
            let _ = tx.send(());
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = service_control_handler::register(SERVICE_NAME, handler).unwrap();
    let set = |s: ServiceState, accept: ServiceControlAccept| {
        let _ = status.set_service_status(ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: s,
            controls_accepted: accept,
            exit_code: ServiceExitCode::Win32(0),
            checkpoint: 0,
            wait_hint: Duration::default(),
            process_id: None,
        });
    };
    set(ServiceState::Running, ServiceControlAccept::STOP);
    let stop = start_core(&rt);
    let _ = rx.recv();
    stop.store(true, Ordering::Relaxed);
    set(ServiceState::Stopped, ServiceControlAccept::empty());
}

fn install() -> Result<(), Box<dyn std::error::Error>> {
    use windows_service::service::{ServiceAccess, ServiceErrorControl, ServiceInfo, ServiceStartType};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    let mgr = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE)?;
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
    println!("서비스 {SERVICE_NAME} 등록 완료(자동 시작). 시작: sc start {SERVICE_NAME}");
    Ok(())
}

fn uninstall() -> Result<(), Box<dyn std::error::Error>> {
    use windows_service::service::ServiceAccess;
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    let mgr = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
    let svc = mgr.open_service(SERVICE_NAME, ServiceAccess::DELETE | ServiceAccess::STOP | ServiceAccess::QUERY_STATUS)?;
    let _ = svc.stop(); // 실행 중이 아니면 오류가 나도 무시
    svc.delete()?;
    println!("서비스 {SERVICE_NAME} 제거 표시 완료(중지 후 완전히 사라집니다)");
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().any(|a| a == "--install") {
        return install();
    }
    if std::env::args().any(|a| a == "--uninstall") {
        return uninstall();
    }
    if std::env::args().any(|a| a == "--console") {
        let rt = tokio::runtime::Runtime::new()?;
        println!("KovaSvc console, pipe {}, config {}", kova_ipc::PIPE_NAME, default_config_dir().display());
        let _stop = start_core(&rt);
        rt.block_on(std::future::pending::<()>());
        return Ok(());
    }
    service_dispatcher::start(SERVICE_NAME, ffi_service_main)?;
    Ok(())
}
