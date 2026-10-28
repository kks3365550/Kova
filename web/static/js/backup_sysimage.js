// =========================================================================
// [백업시스템] Windows 베어메탈 시스템 이미지 (전체 C드라이브) 백업 로직
// =========================================================================
var sysImageLogTimer = null;

async function loadSystemImageStatus() {
    const driveSelect = document.getElementById('sysimg-drive-select');
    
    // Dynamically populate available target drives (excluding C:)
    if (driveSelect && (driveSelect.options.length <= 1 || driveSelect.options[0].disabled)) {
        try {
            const sysInfo = (typeof state !== 'undefined' && state.systemInfo) ? state.systemInfo : await fetchAPI('/api/system-info');
            const availableDrives = (sysInfo?.drives || []).filter(d => !d.device.toUpperCase().startsWith('C:'));
            if (availableDrives.length > 0) {
                const currentVal = driveSelect.value;
                driveSelect.innerHTML = availableDrives.map(d => {
                    const ltr = d.device.substring(0, 2).toUpperCase();
                    const freeGb = Math.round(d.free / (1024**3));
                    return `<option value="${ltr}">${ltr} 드라이브 (${freeGb} GB 여유)</option>`;
                }).join('');

                if (currentVal && availableDrives.some(d => d.device.toUpperCase().startsWith(currentVal))) {
                    driveSelect.value = currentVal;
                } else {
                    const hasD = availableDrives.some(d => d.device.toUpperCase().startsWith('D:'));
                    driveSelect.value = hasD ? 'D:' : availableDrives[0].device.substring(0, 2).toUpperCase();
                }
            }
        } catch (e) {
            console.warn('Failed to populate drives dynamically:', e);
        }
    }

    const targetDrive = (driveSelect && driveSelect.value) ? driveSelect.value : 'D:';

    try {
        const data = await fetchAPI(`/api/system-image/status?target_drive=${targetDrive}`);
        if (!data) return;

        // C: drive
        const cUsed = document.getElementById('sysimg-c-used');
        const cTotal = document.getElementById('sysimg-c-total');
        if (cUsed) cUsed.innerText = `${data.c_drive.used_gb} GB`;
        if (cTotal) cTotal.innerText = `전체 ${data.c_drive.total_gb} GB 중 사용 중`;

        // Target drive
        const dTitle = document.getElementById('sysimg-target-title');
        const dFree = document.getElementById('sysimg-d-free');
        const dBadge = document.getElementById('sysimg-space-badge');
        if (dTitle) dTitle.innerText = `백업 저장 드라이브 (${data.target_drive})`;
        if (dFree) dFree.innerText = `${data.target_drive_info.free_gb} GB`;
        if (dBadge) {
            if (data.target_drive_info.sufficient_space) {
                dBadge.innerHTML = '<span class="text-emerald-400 font-semibold">✓ 백업 가능 (여유 공간 충분)</span>';
            } else {
                dBadge.innerHTML = '<span class="text-amber-400 font-semibold">! 여유 공간 부족 주의</span>';
            }
        }

        // Last backup
        const lastStatus = document.getElementById('sysimg-last-status');
        const lastTime = document.getElementById('sysimg-last-time');
        if (lastStatus && lastTime) {
            if (data.backup_info && data.backup_info.exists) {
                lastStatus.innerText = `${data.backup_info.size_gb} GB`;
                lastStatus.className = 'text-2xl font-black text-emerald-400 font-mono';
                const verifiedTag = data.backup_info.is_verified ? ' (4단계 실측 검증 완료)' : '';
                lastTime.innerText = `최근 백업: ${data.backup_info.last_modified || '-'}${verifiedTag}`;
            } else {
                lastStatus.innerText = '미생성';
                lastStatus.className = 'text-2xl font-black text-slate-100 font-mono';
                lastTime.innerText = '저장소에 생성된 통백업 아카이브 없음';
            }
        }

        // Running state & Progress Bar
        updateSystemImageRunningUI(data.is_running, data);

        const pBar = document.getElementById('sysimg-progress-bar');
        const pPct = document.getElementById('sysimg-progress-percent');
        const pPhase = document.getElementById('sysimg-current-phase');
        if (pBar && typeof data.progress_percent === 'number') {
            pBar.style.width = `${data.progress_percent}%`;
        }
        if (pPct && typeof data.progress_percent === 'number') {
            pPct.innerText = `${data.progress_percent.toFixed(1)}%`;
        }
        if (pPhase && data.current_phase) {
            pPhase.innerText = data.current_phase;
        }

        if (data.is_running) {
            startPollingSystemImageLogs();
        }
    } catch (e) {
        console.error('Failed to load system image status:', e);
        const dBadge = document.getElementById('sysimg-space-badge');
        if (dBadge) {
            dBadge.innerHTML = `<span class="text-rose-400">오류: ${e.message || '상태 조회 실패'}</span>`;
        }
    }
}

function updateSystemImageRunningUI(isRunning, data = null) {
    const btnStart = document.getElementById('btn-start-sysimg');
    const btnStop = document.getElementById('btn-stop-sysimg');
    const badge = document.getElementById('sysimg-running-badge');

    if (isRunning) {
        if (btnStart) {
            btnStart.disabled = true;
            btnStart.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i> 백업 생성 진행 중...';
            btnStart.classList.add('opacity-60', 'cursor-not-allowed');
        }
        if (btnStop) btnStop.classList.remove('hidden');
        if (badge) {
            badge.innerText = '백업 실행 중';
            badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-amber-950/80 text-amber-300 border border-amber-700/60 animate-pulse';
        }
    } else {
        if (btnStart) {
            btnStart.disabled = false;
            btnStart.innerHTML = '<i data-lucide="save" class="w-4 h-4"></i> 지금 OS 전체 백업 시작';
            btnStart.classList.remove('opacity-60', 'cursor-not-allowed');
        }
        if (btnStop) btnStop.classList.add('hidden');
        if (badge) {
            if (data && data.status === 'completed') {
                badge.innerText = '백업 완료';
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-emerald-950/80 text-emerald-300 border border-emerald-700/60';
            } else if (data && data.status === 'failed') {
                badge.innerText = '백업 실패';
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-rose-950/80 text-rose-300 border border-rose-700/60';
            } else {
                badge.innerText = '대기 중';
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
            }
        }
    }
    if (window.lucide) lucide.createIcons();
}

async function startSystemImageBackup() {
    const driveSelect = document.getElementById('sysimg-drive-select');
    const targetDrive = driveSelect ? driveSelect.value : 'D:';
    const engineSelect = document.getElementById('sysimg-engine-select');
    const engine = engineSelect && engineSelect.value === 'kimg' ? 'kimg' : 'wbadmin';

    const msg = `윈도우 베어메탈(전체) 시스템 이미지 백업을 시작하시겠습니까?\n\n` +
        `• 대상: C: 전체 (부팅 EFI 파티션, 복구 파티션, OS 전체)\n` +
        (engine === 'kimg'
            ? `• 저장 위치: ${targetDrive}\\KovaImages\n\n※ kimg 고속 엔진(실험)입니다. 검증에 실패하면 wbadmin으로 자동 대체됩니다.\n※ 부팅 불가 시 복원 도구(WinPE)가 아직 없으므로 wbadmin 백업을 함께 유지하세요.`
            : `• 저장 위치: ${targetDrive}\\WindowsImageBackup\n\n※ 초고속 블록 스트림(wbadmin) 엔진으로 5~8분 내에 생성됩니다.`);

    if (!confirm(msg)) return;

    updateSystemImageRunningUI(true);

    const pBar = document.getElementById('sysimg-progress-bar');
    const pPct = document.getElementById('sysimg-progress-percent');
    const pPhase = document.getElementById('sysimg-current-phase');
    if (pBar) pBar.style.width = '0%';
    if (pPct) pPct.innerText = '0.0%';
    if (pPhase) pPhase.innerText = '백업 프로세스 호출 중...';

    const consoleBox = document.getElementById('sysimg-console');
    if (consoleBox) {
        consoleBox.innerHTML = '<div class="text-blue-400">[시작] 윈도우 wbadmin 시스템 이미지 백업 명령을 호출합니다...</div>';
    }

    try {
        const res = await fetchAPI('/api/system-image/start', {
            method: 'POST',
            body: JSON.stringify({ target_drive: targetDrive, engine: engine })
        });

        if (res && res.success) {
            startPollingSystemImageLogs();
        } else {
            alert('백업 시작 실패: ' + (res ? res.error : '알 수 없는 오류'));
            updateSystemImageRunningUI(false);
        }
    } catch (e) {
        alert('백업 요청 오류: ' + e.message);
        updateSystemImageRunningUI(false);
    }
}

function startPollingSystemImageLogs() {
    if (sysImageLogTimer) clearInterval(sysImageLogTimer);

    sysImageLogTimer = setInterval(async () => {
        try {
            const data = await fetchAPI('/api/system-image/logs');
            if (!data) return;

            // Update Progress Bar & Phase
            const pBar = document.getElementById('sysimg-progress-bar');
            const pPct = document.getElementById('sysimg-progress-percent');
            const pPhase = document.getElementById('sysimg-current-phase');
            if (pBar && typeof data.progress_percent === 'number') {
                pBar.style.width = `${data.progress_percent}%`;
            }
            if (pPct && typeof data.progress_percent === 'number') {
                pPct.innerText = `${data.progress_percent.toFixed(1)}%`;
            }
            if (pPhase && data.current_phase) {
                pPhase.innerText = data.current_phase;
            }

            const consoleBox = document.getElementById('sysimg-console');
            if (consoleBox && data.logs) {
                consoleBox.innerHTML = data.logs.map(line => {
                    let color = 'text-slate-300';
                    if (line.includes('[SUCCESS]') || line.includes('성공적으로 완료')) color = 'text-emerald-400 font-bold';
                    if (line.includes('[ERROR]') || line.includes('오류') || line.includes('실패')) color = 'text-red-400 font-bold';
                    if (line.includes('[WARNING]')) color = 'text-amber-400';
                    if (line.includes('진행률:')) color = 'text-cyan-300 font-semibold';
                    return `<div class="${color}">${line}</div>`;
                }).join('');
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }

            if (!data.is_running) {
                clearInterval(sysImageLogTimer);
                sysImageLogTimer = null;
                updateSystemImageRunningUI(false, data);
                loadSystemImageStatus();
            }
        } catch (e) {
            console.error('Error polling system image logs:', e);
        }
    }, 1000);
}

async function stopSystemImageBackup() {
    if (!confirm('진행 중인 윈도우 시스템 이미지 백업을 중단하시겠습니까?')) return;
    try {
        await fetchAPI('/api/system-image/stop', { method: 'POST' });
        alert('백업 중단 명령이 전송되었습니다.');
    } catch (e) {
        alert('중단 실패: ' + e.message);
    }
}

async function loadVhdxList(drive = null) {
    const driveSelect = document.getElementById('sysimg-drive-select');
    let targetDrive = drive || (driveSelect ? driveSelect.value : '');
    if (!targetDrive) targetDrive = 'D:';
    const container = document.getElementById('vhdx-list-container');
    if (!container) return;

    try {
        const res = await fetchAPI(`/api/system-image/vhdx-list?target_drive=${targetDrive}`);
        if (!res || !res.success || !res.vhdx_list || res.vhdx_list.length === 0) {
            container.innerHTML = '';
            return;
        }

        container.innerHTML = res.vhdx_list.map(item => {
            const isMounted = item.is_mounted;
            const driveBadge = isMounted && item.mount_drive 
                ? `<span class="px-2 py-0.5 bg-cyan-950 text-cyan-300 border border-cyan-800 font-bold text-xs rounded">${item.mount_drive} 드라이브로 마운트됨</span>`
                : (isMounted ? `<span class="px-2 py-0.5 bg-cyan-950 text-cyan-300 border border-cyan-800 text-xs rounded">마운트됨</span>` : '');

            const actionBtn = isMounted
                ? `<button onclick="dismountVhdx('${encodeURIComponent(item.path)}')" class="px-3 py-1.5 bg-rose-900/60 hover:bg-rose-800 text-rose-200 border border-rose-700/60 rounded-xl text-xs font-semibold transition flex items-center gap-1">
                    <i data-lucide="disc-3" class="w-3.5 h-3.5"></i> 언마운트 (해제)
                   </button>`
                : `<button onclick="mountVhdx('${encodeURIComponent(item.path)}')" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-xl text-xs font-semibold transition shadow-md shadow-blue-600/30 flex items-center gap-1">
                    <i data-lucide="folder-check" class="w-3.5 h-3.5"></i> 읽기전용 마운트
                   </button>`;

            const deleteBtn = `<button onclick="deleteVhdx('${encodeURIComponent(item.path)}', '${encodeURIComponent(item.filename)}', ${item.size_gb})" class="px-2.5 py-1.5 bg-rose-950/70 hover:bg-rose-900 text-rose-300 hover:text-white rounded-xl text-xs font-medium border border-rose-800/80 transition flex items-center gap-1" title="이 VHDX 이미지 완전 삭제">
                <i data-lucide="trash-2" class="w-3.5 h-3.5 text-rose-400"></i> 삭제
               </button>`;

            return `
                <div class="p-3 bg-slate-950/70 hover:bg-slate-900/90 rounded-xl border border-slate-800/90 flex flex-col md:flex-row md:items-center justify-between gap-3 transition">
                    <div class="flex items-center gap-3 overflow-hidden">
                        <div class="w-8 h-8 rounded-lg ${isMounted ? 'bg-cyan-950 text-cyan-400 border border-cyan-800' : 'bg-slate-800 text-slate-400'} flex items-center justify-center shrink-0">
                            <i data-lucide="${isMounted ? 'hard-drive' : 'file'}" class="w-4 h-4"></i>
                        </div>
                        <div class="overflow-hidden">
                            <div class="flex items-center gap-2">
                                <span class="font-bold text-slate-200 text-xs truncate font-mono">${item.filename}</span>
                                ${driveBadge}
                            </div>
                            <div class="text-[11px] text-slate-400 font-mono mt-0.5 flex gap-3">
                                <span>용량: <strong class="text-slate-300">${item.size_gb} GB</strong></span>
                                <span>백업일: ${item.modified_at}</span>
                            </div>
                        </div>
                    </div>
                    <div class="flex items-center gap-2 shrink-0">
                        ${actionBtn}
                        ${deleteBtn}
                    </div>
                </div>
            `;
        }).join('');

        if (window.lucide) lucide.createIcons();
    } catch (e) {
        console.error('Failed to load VHDX list:', e);
        container.innerHTML = `<div class="text-red-400 text-xs py-2">VHDX 목록 로딩 실패: ${e.message}</div>`;
    }
}

async function mountVhdx(encodedPath) {
    const vhdxPath = decodeURIComponent(encodedPath);
    try {
        const res = await fetchAPI('/api/system-image/mount', {
            method: 'POST',
            body: JSON.stringify({ vhdx_path: vhdxPath, read_only: true })
        });
        if (res && res.success) {
            showToast(res.message || 'VHDX가 마운트되었습니다.', 'success');
            loadVhdxList();
            if (typeof loadDashboard === 'function') loadDashboard();
        } else {
            alert('VHDX 마운트 실패: ' + (res ? res.error : '알 수 없는 오류'));
        }
    } catch (e) {
        alert('마운트 요청 오류: ' + e.message);
    }
}

async function dismountVhdx(encodedPath) {
    const vhdxPath = decodeURIComponent(encodedPath);
    try {
        const res = await fetchAPI('/api/system-image/dismount', {
            method: 'POST',
            body: JSON.stringify({ vhdx_path: vhdxPath })
        });
        if (res && res.success) {
            showToast('VHDX가 성공적으로 언마운트되었습니다.', 'info');
            loadVhdxList();
            if (typeof loadDashboard === 'function') loadDashboard();
        } else {
            alert('VHDX 언마운트 실패: ' + (res ? res.error : '알 수 없는 오류'));
        }
    } catch (e) {
        alert('언마운트 요청 오류: ' + e.message);
    }
}

async function deleteVhdx(encodedPath, encodedFilename, sizeGb) {
    const vhdxPath = decodeURIComponent(encodedPath);
    const filename = decodeURIComponent(encodedFilename);
    const msg = `이 VHDX 시스템 이미지를 완전히 삭제하시겠습니까?\n\n` +
        `• 파일명: ${filename}\n` +
        `• 용량: 약 ${sizeGb} GB\n` +
        `• 경로: ${vhdxPath}\n\n` +
        `※ 마운트되어 있는 경우 자동으로 언마운트 후 안전하게 삭제됩니다. 삭제된 이미지는 복구할 수 없습니다.`;

    if (!confirm(msg)) return;

    try {
        const res = await fetchAPI('/api/system-image/delete', {
            method: 'DELETE',
            body: JSON.stringify({ vhdx_path: vhdxPath })
        });
        if (res && res.success) {
            alert(res.message || 'VHDX 이미지가 성공적으로 삭제되었습니다.');
            loadVhdxList();
            if (typeof loadArchiveList === 'function') loadArchiveList();
            if (typeof loadSystemImageStatus === 'function') loadSystemImageStatus();
            if (typeof loadDashboard === 'function') loadDashboard();
        } else {
            alert('VHDX 삭제 실패: ' + (res ? res.error : '알 수 없는 오류'));
        }
    } catch (e) {
        alert('VHDX 삭제 요청 오류: ' + e.message);
    }
}

// Global export & automatic self-initialization
window.loadSystemImageStatus = loadSystemImageStatus;
window.loadVhdxList = loadVhdxList;

document.addEventListener('DOMContentLoaded', () => {
    loadSystemImageStatus();
});

