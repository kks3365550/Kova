// web/static/js/backup_migration.js
// 1-Click 라이브 SSD 마이그레이션(클론 & 부트로더 주입) 프론트엔드 모듈

let migrationPollingInterval = null;
let migrationDrivesCache = [];

async function loadMigrationDrives() {
    const selectEl = document.getElementById("migration-target-select");
    const infoEl = document.getElementById("migration-drive-info");
    const btnStart = document.getElementById("btn-start-migration");

    if (!selectEl) return;

    selectEl.innerHTML = '<option value="">드라이브 목록 검색 중...</option>';
    selectEl.disabled = true;

    try {
        const res = await fetch("/api/migration/disks");
        const data = await res.json();

        if (!data.success || !data.drives || data.drives.length === 0) {
            selectEl.innerHTML = '<option value="">선택 가능한 대상 드라이브가 없습니다 (C: 외 물리 드라이브 필요)</option>';
            selectEl.disabled = true;
            if (infoEl) infoEl.innerHTML = '<span class="text-amber-400">⚠️ 복제할 추가 SSD/HDD가 감지되지 않았습니다. 새 SSD를 연결해 주세요.</span>';
            if (btnStart) btnStart.disabled = true;
            return;
        }

        migrationDrivesCache = data.drives;
        selectEl.innerHTML = "";

        data.drives.forEach(d => {
            const opt = document.createElement("option");
            opt.value = d.drive;
            const acceptTag = d.acceptable ? " [복제 가능]" : " [용량 부족]";
            opt.textContent = `${d.drive} (총 ${d.total_gb} GB / 여유 ${d.free_gb} GB)${acceptTag}`;
            if (!d.acceptable) {
                opt.classList.add("text-zinc-500");
            }
            selectEl.appendChild(opt);
        });

        selectEl.disabled = false;
        onMigrationDriveChange();
    } catch (err) {
        console.error("마이그레이션 드라이브 목록 로드 실패:", err);
        selectEl.innerHTML = '<option value="">드라이브 목록 조회 실패</option>';
        if (infoEl) infoEl.innerHTML = `<span class="text-rose-400">조회 실패: ${err.message}</span>`;
    }
}

function onMigrationDriveChange() {
    const selectEl = document.getElementById("migration-target-select");
    const infoEl = document.getElementById("migration-drive-info");
    const btnStart = document.getElementById("btn-start-migration");

    if (!selectEl || !infoEl) return;
    const selectedVal = selectEl.value;
    const drive = migrationDrivesCache.find(d => d.drive === selectedVal);

    if (!drive) {
        infoEl.innerHTML = '<span class="text-zinc-400">대상을 선택하세요.</span>';
        if (btnStart) btnStart.disabled = true;
        return;
    }

    if (drive.acceptable) {
        const espTag = drive.can_auto_esp 
            ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-950 text-purple-300 border border-purple-800">100% UEFI ESP 자동생성 지원</span>' 
            : '<span class="px-2 py-0.5 rounded text-[10px] font-medium bg-zinc-800 text-zinc-400">동일 디스크 (파티션 복제 모드)</span>';
        infoEl.innerHTML = `
            <div class="space-y-1">
                <div class="flex items-center gap-2 text-emerald-400 font-medium">
                    <i data-lucide="check-circle" class="w-4 h-4"></i>
                    <span>마이그레이션 적합: 총 ${drive.total_gb} GB (C: 사용량 ${drive.c_used_gb} GB 완벽 수용 가능)</span>
                </div>
                <div class="flex items-center gap-2 text-[11px] text-zinc-400">
                    <span>물리 디스크: Disk ${drive.disk_number !== null ? drive.disk_number : '감지됨'}</span>
                    <span>|</span>
                    ${espTag}
                </div>
            </div>
        `;
        if (btnStart) btnStart.disabled = false;
    } else {
        infoEl.innerHTML = `
            <div class="flex items-center gap-2 text-rose-400 font-medium">
                <i data-lucide="alert-triangle" class="w-4 h-4"></i>
                <span>${drive.reason}</span>
            </div>
        `;
        if (btnStart) btnStart.disabled = true;
    }

    if (window.lucide) {
        window.lucide.createIcons();
    }
}

function openMigrationConfirmModal() {
    const selectEl = document.getElementById("migration-target-select");
    const targetDrive = selectEl ? selectEl.value : "";
    if (!targetDrive) {
        alert("마이그레이션 대상 SSD 드라이브를 먼저 선택해주세요.");
        return;
    }

    const modalTargetText = document.getElementById("modal-migration-target-text");
    if (modalTargetText) {
        modalTargetText.textContent = targetDrive;
    }

    const drive = migrationDrivesCache.find(d => d.drive === targetDrive);
    const chkEsp = document.getElementById("chk-migration-auto-esp");
    if (chkEsp) {
        if (drive && drive.can_auto_esp) {
            chkEsp.checked = true;
            chkEsp.disabled = false;
        } else {
            chkEsp.checked = false;
            chkEsp.disabled = true;
        }
    }

    const chk = document.getElementById("chk-migration-danger-confirm");
    if (chk) chk.checked = false;

    const btnRun = document.getElementById("btn-run-migration-final");
    if (btnRun) btnRun.disabled = true;

    const modal = document.getElementById("migration-confirm-modal");
    if (modal) modal.classList.remove("hidden");
}

function onMigrationDangerCheckChange() {
    const chk = document.getElementById("chk-migration-danger-confirm");
    const btnRun = document.getElementById("btn-run-migration-final");
    if (btnRun && chk) {
        btnRun.disabled = !chk.checked;
    }
}

async function confirmAndRunMigration() {
    const selectEl = document.getElementById("migration-target-select");
    const targetDrive = selectEl ? selectEl.value : "";
    if (!targetDrive) return;

    const chkEsp = document.getElementById("chk-migration-auto-esp");
    const autoPrepareEsp = chkEsp ? chkEsp.checked : true;

    closeModal("migration-confirm-modal");

    try {
        const res = await fetch("/api/migration/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ 
                target_drive: targetDrive,
                auto_prepare_esp: autoPrepareEsp
            })
        });
        const data = await res.json();

        if (!res.ok || !data.success) {
            alert("마이그레이션 시작 실패: " + (data.detail || data.error || "알 수 없는 오류"));
            return;
        }

        appendMigrationConsoleLog(`[START] ${targetDrive} 드라이브로의 라이브 마이그레이션이 시작되었습니다.`);
        startMigrationPolling();
    } catch (err) {
        alert("네트워크 통신 오류: " + err.message);
    }
}

function startMigrationPolling() {
    if (migrationPollingInterval) {
        clearInterval(migrationPollingInterval);
    }

    pollMigrationStatus();
    migrationPollingInterval = setInterval(pollMigrationStatus, 1500);
}

async function pollMigrationStatus() {
    try {
        const [resSt, resLogs] = await Promise.all([
            fetch("/api/migration/status"),
            fetch("/api/migration/logs?limit=50")
        ]);

        const dataSt = await resSt.json();
        const dataLogs = await resLogs.json();

        if (dataSt.success && dataSt.status) {
            updateMigrationUI(dataSt.status);
        }

        if (dataLogs.success && dataLogs.logs) {
            renderMigrationLogs(dataLogs.logs);
        }
    } catch (err) {
        console.warn("마이그레이션 상태 폴링 실패:", err);
    }
}

function updateMigrationUI(status) {
    const btnStart = document.getElementById("btn-start-migration");
    const btnStop = document.getElementById("btn-stop-migration");
    const progressBar = document.getElementById("migration-progress-bar");
    const progressText = document.getElementById("migration-progress-percent");
    const phaseText = document.getElementById("migration-current-phase");
    const badgeEl = document.getElementById("migration-status-badge");
    const selectEl = document.getElementById("migration-target-select");

    const isRunning = status.is_running;

    if (btnStart) {
        if (isRunning) {
            btnStart.classList.add("hidden");
        } else {
            btnStart.classList.remove("hidden");
        }
    }

    if (btnStop) {
        if (isRunning) {
            btnStop.classList.remove("hidden");
        } else {
            btnStop.classList.add("hidden");
        }
    }

    if (selectEl) {
        selectEl.disabled = isRunning;
    }

    if (progressBar) {
        progressBar.style.width = `${status.progress_percent || 0}%`;
    }

    if (progressText) {
        progressText.textContent = `${status.progress_percent || 0}%`;
    }

    if (phaseText) {
        phaseText.textContent = status.current_phase || "대기 중";
    }

    if (badgeEl) {
        const st = status.status || "idle";
        badgeEl.className = "text-[11px] px-2.5 py-0.5 rounded-full font-bold uppercase tracking-wider ";
        if (st === "idle") {
            badgeEl.className += "bg-zinc-800 text-zinc-400 border border-zinc-700";
            badgeEl.textContent = "대기 중";
        } else if (st === "preparing") {
            badgeEl.className += "bg-blue-950 text-blue-300 border border-blue-800 animate-pulse";
            badgeEl.textContent = "준비 중 (VSS)";
        } else if (st === "copying") {
            badgeEl.className += "bg-indigo-950 text-indigo-300 border border-indigo-800 animate-pulse";
            badgeEl.textContent = "미러링 복제 중";
        } else if (st === "bootloading") {
            badgeEl.className += "bg-purple-950 text-purple-300 border border-purple-800 animate-pulse";
            badgeEl.textContent = "부트로더 주입 중";
        } else if (st === "completed") {
            badgeEl.className += "bg-emerald-950 text-emerald-300 border border-emerald-800";
            badgeEl.textContent = "🎉 마이그레이션 완료";
            if (migrationPollingInterval) {
                clearInterval(migrationPollingInterval);
                migrationPollingInterval = null;
            }
        } else if (st === "failed") {
            badgeEl.className += "bg-rose-950 text-rose-300 border border-rose-800";
            badgeEl.textContent = "❌ 실패";
            if (migrationPollingInterval) {
                clearInterval(migrationPollingInterval);
                migrationPollingInterval = null;
            }
        } else if (st === "cancelled") {
            badgeEl.className += "bg-amber-950 text-amber-300 border border-amber-800";
            badgeEl.textContent = "⚠️ 취소됨";
            if (migrationPollingInterval) {
                clearInterval(migrationPollingInterval);
                migrationPollingInterval = null;
            }
        }
    }
}

function renderMigrationLogs(logs) {
    const consoleEl = document.getElementById("migration-console");
    if (!consoleEl) return;

    if (!logs || logs.length === 0) {
        consoleEl.innerHTML = '<div class="text-zinc-500">[대기] 마이그레이션 시작 시 실시간 로그가 출력됩니다.</div>';
        return;
    }

    consoleEl.innerHTML = logs.map(l => {
        let cls = "text-zinc-300";
        if (l.includes("ERROR") || l.includes("❌") || l.includes("실패")) cls = "text-rose-400 font-bold";
        else if (l.includes("🎉") || l.includes("성공") || l.includes("완료")) cls = "text-emerald-400 font-bold";
        else if (l.includes("VSS") || l.includes("Robocopy") || l.includes("bcdboot")) cls = "text-cyan-300";
        else if (l.includes("💡") || l.includes("안내")) cls = "text-amber-300";
        return `<div class="${cls}">${escapeHtml(l)}</div>`;
    }).join("");

    consoleEl.scrollTop = consoleEl.scrollHeight;
}

function appendMigrationConsoleLog(msg) {
    const consoleEl = document.getElementById("migration-console");
    if (!consoleEl) return;
    const div = document.createElement("div");
    div.className = "text-cyan-300";
    div.textContent = msg;
    consoleEl.appendChild(div);
    consoleEl.scrollTop = consoleEl.scrollHeight;
}

async function stopLiveMigration() {
    if (!confirm("정말 진행 중인 마이그레이션 작업을 중단하시겠습니까?")) return;

    try {
        const res = await fetch("/api/migration/stop", { method: "POST" });
        const data = await res.json();
        if (data.success) {
            appendMigrationConsoleLog("[STOP] 중단 요청이 전달되었습니다.");
            pollMigrationStatus();
        } else {
            alert("중단 실패: " + (data.detail || data.error));
        }
    } catch (err) {
        alert("네트워크 오류: " + err.message);
    }
}

// 탭 전환 시 자동 드라이브 목록 로드
document.addEventListener("DOMContentLoaded", () => {
    // OS 백업 탭이 활성화될 때 또는 첫 로드 시 드라이브 로드
    setTimeout(() => {
        loadMigrationDrives();
        loadArchiveList();
    }, 1000);
});

// ==============================================================================
// 드라이브 통백업(WIM) & 4단계 실측 검증 프론트엔드 모듈
// ==============================================================================

let archivePollingInterval = null;

async function loadArchiveList() {
    const listEl = document.getElementById("archive-list-container");
    if (!listEl) return;

    try {
        const res = await fetch("/api/migration/archive/list");
        const data = await res.json();

        if (!data.success || !data.archives || data.archives.length === 0) {
            listEl.innerHTML = `
                <div class="text-zinc-500 text-xs py-4 text-center border border-dashed border-zinc-800 rounded-xl">
                    보관 중인 드라이브 통백업(VHDX/WIM) 아카이브가 없습니다.
                </div>
            `;
            return;
        }

        listEl.innerHTML = data.archives.map(a => {
            const verifyBadge = a.is_verified 
                ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-950 text-emerald-300 border border-emerald-800 flex items-center gap-1"><i data-lucide="check-check" class="w-3 h-3"></i> 4단계 실측 검증 완료 (부팅/라이선스 100% 보증)</span>'
                : '<span class="px-2 py-0.5 rounded text-[10px] font-medium bg-zinc-800 text-zinc-400 border border-zinc-700">미검증 아카이브</span>';

            return `
                <div class="p-3.5 bg-zinc-950/80 rounded-xl border border-zinc-800/80 flex flex-col xl:flex-row xl:items-center justify-between gap-3 hover:border-indigo-500/40 transition">
                    <div class="space-y-1">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-bold text-white text-xs">${escapeHtml(a.name)}</span>
                            <span class="text-[10px] px-1.5 py-0.2 rounded bg-indigo-950 text-indigo-300 font-mono border border-indigo-800">${a.size_gb} GB</span>
                            ${verifyBadge}
                        </div>
                        <div class="flex items-center gap-3 text-[11px] text-zinc-400">
                            <span>생성: ${escapeHtml(a.created_at)}</span>
                            <span>•</span>
                            <span>원본 VSN: <strong class="text-purple-300 font-mono">${escapeHtml(a.vsn)}</strong></span>
                        </div>
                    </div>
                    <div class="flex items-center gap-2 flex-wrap sm:flex-nowrap shrink-0">
                        <button onclick="uploadArchiveToGdrive('${encodeURIComponent(a.path)}')" class="px-2.5 py-1.5 bg-purple-900/60 hover:bg-purple-800 text-purple-200 rounded-lg text-xs font-medium border border-purple-700/80 transition flex items-center gap-1" title="구글 드라이브로 6청크 초고속 병렬 업로드">
                            <i data-lucide="cloud-upload" class="w-3.5 h-3.5 text-purple-300"></i> 구글 6청크 업로드
                        </button>
                        <button onclick="verifySingleArchive('${encodeURIComponent(a.path)}')" class="px-2.5 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white rounded-lg text-xs font-medium border border-zinc-700 transition flex items-center gap-1">
                            <i data-lucide="shield-check" class="w-3.5 h-3.5 text-emerald-400"></i> 실측 검증
                        </button>
                        <button onclick="openArchiveRestoreModal('${encodeURIComponent(a.path)}')" class="px-2.5 py-1.5 bg-indigo-600/80 hover:bg-indigo-600 text-white rounded-lg text-xs font-semibold transition flex items-center gap-1">
                            <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i> 이 백업으로 복원
                        </button>
                        <button onclick="deleteArchiveItem('${encodeURIComponent(a.path)}', '${escapeHtml(a.name)}', ${a.size_gb})" class="px-2.5 py-1.5 bg-rose-950/70 hover:bg-rose-900 text-rose-300 hover:text-white rounded-lg text-xs font-medium border border-rose-800/80 transition flex items-center gap-1" title="이 통백업 아카이브 삭제">
                            <i data-lucide="trash-2" class="w-3.5 h-3.5 text-rose-400"></i> 삭제
                        </button>
                    </div>
                </div>
            `;
        }).join("");

        if (window.lucide) window.lucide.createIcons();
    } catch (err) {
        console.error("아카이브 목록 조회 실패:", err);
    }
}

async function deleteArchiveItem(archivePathEncoded, archiveName, sizeGb) {
    const archivePath = decodeURIComponent(archivePathEncoded);
    if (!confirm(`이 통백업 아카이브를 완전히 삭제하시겠습니까?\n\n- 파일명: ${archiveName}\n- 확보 용량: 약 ${sizeGb} GB\n- 저장 경로: ${archivePath}\n\n삭제된 아카이브 파일은 복구할 수 없습니다.`)) {
        return;
    }

    try {
        const res = await fetch("/api/migration/archive", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ archive_path: archivePath })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            alert("아카이브 삭제 실패: " + (data.detail || data.error || "알 수 없는 오류"));
            return;
        }

        alert(`통백업 아카이브가 성공적으로 삭제되었습니다.\n(확보된 저장 공간: ${data.freed_gb || sizeGb} GB)`);
        loadArchiveList();
        if (typeof loadSystemImageStatus === "function") loadSystemImageStatus();
        if (typeof loadDashboard === "function") loadDashboard();
    } catch (err) {
        alert("통신 오류: " + err.message);
    }
}

async function uploadArchiveToGdrive(archivePathEncoded) {
    const archivePath = decodeURIComponent(archivePathEncoded);
    if (!confirm(`이 베어메탈 백업 아카이브를 구글 드라이브로 업로드하시겠습니까?\n\n- 대상: ${archivePath}\n- 전송 방식: Rclone Chunker (6개 병렬 스트림, 4GB 청크)\n- 로컬 원본 보존: 원본 훼손 및 용량 낭비 없음`)) {
        return;
    }
    appendArchiveConsoleLog(`[START] 구글 드라이브 6청크 병렬 업로드를 시작합니다: ${archivePath}`);
    try {
        const res = await fetch("/api/migration/archive/upload-gdrive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ archive_path: archivePath, transfers: 6 })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            alert("구글 드라이브 업로드 실패: " + (data.detail || data.reason || "오류"));
            appendArchiveConsoleLog(`[ERROR] 구글 드라이브 업로드 실패: ${data.detail || data.reason}`);
            return;
        }
        alert("구글 드라이브 6청크 병렬 업로드 완료!\n" + data.reason);
        appendArchiveConsoleLog(`[DONE] ${data.reason} (소요시간: ${Math.round(data.duration_sec || 0)}초)`);
    } catch (err) {
        alert("네트워크 오류: " + err.message);
    }
}

async function startDriveArchiveBackup() {
    const compressEl = document.getElementById("archive-compress-select");
    const autoVerifyEl = document.getElementById("chk-archive-auto-verify");
    const uploadGdriveEl = document.getElementById("chk-archive-upload-gdrive");

    const compress = compressEl ? compressEl.value : "fast";
    const autoVerify = autoVerifyEl ? autoVerifyEl.checked : true;
    const uploadGdrive = uploadGdriveEl ? uploadGdriveEl.checked : false;

    const driveSelect = document.getElementById("sysimg-drive-select");
    const targetDrive = (driveSelect && driveSelect.value) ? driveSelect.value : "D:";

    if (!confirm(`C: 드라이브 전체를 베어메탈 디스크 이미지로 통백업하시겠습니까?\n\n- 대상: C: 전체 (부팅 EFI + OS + 모든 파일)\n- 저장 위치: ${targetDrive}\\MyBackup_Repository\\archives\n- 4단계 실측 검증: ${autoVerify ? "포함 (권장)" : "미포함"}\n- 구글 드라이브 업로드: ${uploadGdrive ? "6청크 초고속 병렬 스트림 업로드" : "미포함 (로컬 보관)"}`)) {
        return;
    }

    try {
        const res = await fetch("/api/migration/archive/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_drive: "C:",
                target_drive: targetDrive,
                compress: compress,
                auto_verify: autoVerify,
                upload_gdrive: uploadGdrive
            })
        });
        const data = await res.json();

        if (!res.ok || !data.success) {
            alert("통백업 시작 실패: " + (data.detail || data.error || "알 수 없는 오류"));
            return;
        }

        appendArchiveConsoleLog(`[START] 베어메탈 블록 스트리밍 통백업이 시작되었습니다: ${data.target_wim || "진행 중"}`);
        startArchivePolling();
    } catch (err) {
        alert("네트워크 오류: " + err.message);
    }
}

function startArchivePolling() {
    if (archivePollingInterval) clearInterval(archivePollingInterval);
    pollArchiveStatus();
    archivePollingInterval = setInterval(pollArchiveStatus, 1500);
}

async function pollArchiveStatus() {
    try {
        const [resSt, resLogs] = await Promise.all([
            fetch("/api/migration/archive/status"),
            fetch("/api/migration/archive/logs?limit=50")
        ]);

        const dataSt = await resSt.json();
        const dataLogs = await resLogs.json();

        if (dataSt.success && dataSt.status) {
            updateArchiveUI(dataSt.status);
        }

        if (dataLogs.success && dataLogs.logs) {
            renderArchiveLogs(dataLogs.logs);
        }
    } catch (err) {
        console.warn("아카이브 상태 폴링 실패:", err);
    }
}

function updateArchiveUI(status) {
    const btnStart = document.getElementById("btn-start-archive");
    const btnStop = document.getElementById("btn-stop-archive");
    const progressBar = document.getElementById("archive-progress-bar");
    const progressText = document.getElementById("archive-progress-percent");
    const phaseText = document.getElementById("archive-current-phase");
    const badgeEl = document.getElementById("archive-status-badge");

    const isRunning = status.is_running;

    if (btnStart) {
        if (isRunning) btnStart.classList.add("hidden");
        else btnStart.classList.remove("hidden");
    }

    if (btnStop) {
        if (isRunning) btnStop.classList.remove("hidden");
        else btnStop.classList.add("hidden");
    }

    if (progressBar) progressBar.style.width = `${status.progress_percent || 0}%`;
    if (progressText) progressText.textContent = `${status.progress_percent || 0}%`;
    if (phaseText) phaseText.textContent = status.current_phase || "대기 중";

    if (badgeEl) {
        const s = status.status;
        badgeEl.textContent = s;
        badgeEl.className = "text-[11px] px-2.5 py-0.5 rounded-full font-bold uppercase tracking-wider ";
        if (s === "capturing" || s === "restoring") {
            badgeEl.classList.add("bg-indigo-900/60", "text-indigo-300", "border", "border-indigo-700", "animate-pulse");
        } else if (s === "verifying") {
            badgeEl.classList.add("bg-purple-900/60", "text-purple-300", "border", "border-purple-700", "animate-pulse");
        } else if (s === "completed") {
            badgeEl.classList.add("bg-emerald-900/60", "text-emerald-300", "border", "border-emerald-700");
        } else if (s === "failed") {
            badgeEl.classList.add("bg-rose-900/60", "text-rose-300", "border", "border-rose-700");
        } else {
            badgeEl.classList.add("bg-zinc-800", "text-zinc-400", "border", "border-zinc-700");
        }
    }

    if (!isRunning && (status.status === "completed" || status.status === "failed" || status.status === "cancelled")) {
        if (archivePollingInterval) {
            clearInterval(archivePollingInterval);
            archivePollingInterval = null;
        }
        loadArchiveList();
    }
}

function renderArchiveLogs(logs) {
    const consoleEl = document.getElementById("archive-console");
    if (!consoleEl) return;

    if (!logs || logs.length === 0) {
        consoleEl.innerHTML = '<div class="text-zinc-500">[대기] 드라이브 통백업 시작 시 실시간 로그가 출력됩니다.</div>';
        return;
    }

    consoleEl.innerHTML = logs.map(l => {
        let cls = "text-zinc-300";
        if (l.includes("ERROR") || l.includes("❌") || l.includes("실패")) cls = "text-rose-400 font-bold";
        else if (l.includes("🎉") || l.includes("성공") || l.includes("완료") || l.includes("✓")) cls = "text-emerald-400 font-bold";
        else if (l.includes("DISM") || l.includes("wbadmin") || l.includes("VHDX") || l.includes("Robocopy") || l.includes("VSN") || l.includes("검증")) cls = "text-cyan-300";
        else if (l.includes("경고") || l.includes("주의")) cls = "text-amber-300";
        return `<div class="${cls}">${escapeHtml(l)}</div>`;
    }).join("");

    consoleEl.scrollTop = consoleEl.scrollHeight;
}

function appendArchiveConsoleLog(msg) {
    const consoleEl = document.getElementById("archive-console");
    if (!consoleEl) return;
    const div = document.createElement("div");
    div.className = "text-cyan-300";
    div.textContent = msg;
    consoleEl.appendChild(div);
    consoleEl.scrollTop = consoleEl.scrollHeight;
}

async function stopDriveArchiveBackup() {
    if (!confirm("진행 중인 통백업/복원 작업을 중단하시겠습니까?")) return;
    try {
        const res = await fetch("/api/migration/archive/stop", { method: "POST" });
        const data = await res.json();
        if (data.success) {
            appendArchiveConsoleLog("[STOP] 중단 요청이 전달되었습니다.");
            pollArchiveStatus();
        } else {
            alert("중단 실패: " + (data.message || data.error));
        }
    } catch (err) {
        alert("네트워크 오류: " + err.message);
    }
}

async function verifySingleArchive(wimPath) {
    if (!wimPath) return;
    try {
        if (wimPath.includes("%")) wimPath = decodeURIComponent(wimPath);
    } catch (_) {}
    if (!confirm(`선택한 통백업 아카이브(VHDX/WIM)에 대해 4단계 실측 검증을 실행하시겠습니까?\n\n- 대상: ${wimPath}\n- 검증 내용: 가상 디스크 오프라인 마운트 + 5대 부팅/라이선스 아티팩트 전수 조사`)) {
        return;
    }

    appendArchiveConsoleLog(`[VERIFY] 단독 실측 검증 착수: ${wimPath}`);
    startArchivePolling();

    try {
        const res = await fetch("/api/migration/archive/verify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ wim_path: wimPath })
        });
        const data = await res.json();
        if (data.success && data.result) {
            const r = data.result;
            if (r.verified) {
                alert(`🎉 4단계 실측 검증 100% 통과!\n\n- OS 커널, winload.efi, 레지스트리, tokens.dat 모두 정상 무결합니다.\n- 슈뢰딩거의 백업 위험 0% 확인.`);
            } else {
                alert(`⚠️ 검증 경고: ${r.error || "일부 아티팩트 확인 실패"}`);
            }
            loadArchiveList();
        }
    } catch (err) {
        alert("검증 통신 오류: " + err.message);
    }
}

let activeRestoreWimPath = "";

function openArchiveRestoreModal(wimPath) {
    try {
        if (wimPath && wimPath.includes("%")) wimPath = decodeURIComponent(wimPath);
    } catch (_) {}
    activeRestoreWimPath = wimPath;
    const modal = document.getElementById("archive-restore-modal");
    const targetText = document.getElementById("modal-archive-restore-wim");
    if (targetText) targetText.textContent = wimPath;

    // 드라이브 목록 옵션 채우기
    const selectEl = document.getElementById("archive-restore-target-select");
    if (selectEl && migrationDrivesCache) {
        selectEl.innerHTML = migrationDrivesCache.map(d => `
            <option value="${d.drive}">${d.drive} (총 ${d.total_gb} GB / 여유 ${d.free_gb} GB)</option>
        `).join("");
    }

    const chk = document.getElementById("chk-archive-restore-danger");
    if (chk) chk.checked = false;
    const btn = document.getElementById("btn-run-archive-restore-final");
    if (btn) btn.disabled = true;

    if (modal) modal.classList.remove("hidden");
}

function onArchiveRestoreDangerCheckChange() {
    const chk = document.getElementById("chk-archive-restore-danger");
    const btn = document.getElementById("btn-run-archive-restore-final");
    if (btn && chk) btn.disabled = !chk.checked;
}

async function confirmAndRunArchiveRestore() {
    const selectEl = document.getElementById("archive-restore-target-select");
    const targetDrive = selectEl ? selectEl.value : "";
    if (!targetDrive || !activeRestoreWimPath) return;

    closeModal("archive-restore-modal");

    try {
        const res = await fetch("/api/migration/archive/restore", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                wim_path: activeRestoreWimPath,
                target_drive: targetDrive,
                auto_bcdboot: true,
                auto_restore_vsn: true
            })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            alert("복원 시작 실패: " + (data.detail || data.error));
            return;
        }

        appendArchiveConsoleLog(`[START] ${targetDrive} 드라이브로 통백업 복원/마이그레이션 시작: ${activeRestoreWimPath}`);
        startArchivePolling();
    } catch (err) {
        alert("네트워크 오류: " + err.message);
    }
}
