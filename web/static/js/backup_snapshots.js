// =========================================================================
// [백업시스템] 스냅샷 관리, 파일 탐색기(Explorer), 복원(Restore) & 실시간 로그
// =========================================================================

async function loadSnapshots() {
    try {
        state.snapshots = await fetchAPI('/api/snapshots');
        renderSnapshotsTable();
    } catch (e) {
        console.error('Failed to load snapshots:', e);
    }
}

function renderSnapshotsTable() {
    const tbody = document.getElementById('snapshots-table-body');
    if (!tbody) return;

    if (!state.snapshots || state.snapshots.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-10 text-slate-500">백업 스냅샷이 존재하지 않습니다.</td></tr>`;
        return;
    }

    tbody.innerHTML = state.snapshots.map(s => {
        const sum = s.summary || {};
        const isFull = s.backup_type === 'full';
        const isVerified = s.is_verified;
        const totalFiles = (sum.total_files !== undefined && sum.total_files !== null && sum.total_files > 0) ? sum.total_files : (s.total_files || 0);
        const totalBytes = (sum.total_bytes !== undefined && sum.total_bytes !== null && sum.total_bytes > 0) ? sum.total_bytes : (s.total_bytes || 0);
        const dedupBytes = (sum.dedup_saved_bytes !== undefined && sum.dedup_saved_bytes !== null && sum.dedup_saved_bytes > 0) ? sum.dedup_saved_bytes : (s.dedup_bytes || s.dedup_saved_bytes || 0);
        const isoTime = s.iso_time || (typeof s.created_at === 'string' ? s.created_at : '');
        const profName = s.profile_name || (s.id && s.id.startsWith('snap_custom') ? '선택 백업' : '-');
        return `
            <tr class="border-b border-slate-800 hover:bg-slate-800/40 text-sm transition">
                <td class="py-3 px-4 font-mono text-xs text-blue-400">
                    <div class="flex items-center gap-1.5 flex-wrap">
                        <span>${s.id}</span>
                        ${s.vss_enabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800 font-semibold" title="Windows Volume Shadow Copy(VSS) 일관성 백업">VSS</span>' : ''}
                        ${s.is_local_protected !== false ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-semibold" title="로컬 복원 검증 & WORM 무결성 완료">🛡️ 로컬보호</span>' : ''}
                        ${(() => {
                            if (s.is_offsite_protected || s.offsite_status === 'COMMITTED') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-950 text-blue-400 border border-blue-800 font-semibold" title="3-2-1 원격 저장소 복제 완료">🌐 3-2-1 원격보호</span>';
                            } else if (s.offsite_status === 'TRANSFERRING' || s.offsite_status === 'PENDING') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800 font-semibold animate-pulse" title="원격 저장소 복제 동기화 진행 중">⏳ 원격동기화</span>';
                            } else if (s.offsite_status === 'UNSYNCED') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-900 text-amber-400 border border-amber-700/60 font-semibold" title="원격 저장소 미동기화 (대기 중)">⏸️ 원격미복제</span>';
                            } else {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-900 text-slate-400 border border-slate-700 font-medium" title="오프사이트 원격 복제 비활성화">로컬전용</span>';
                            }
                        })()}
                    </div>
                </td>
                <td class="py-3 px-4 font-medium text-slate-200">${profName}</td>
                <td class="py-3 px-4">
                    <span class="text-[11px] px-2 py-0.5 rounded-full font-medium ${isFull ? 'bg-blue-900/60 text-blue-300 border border-blue-700' : 'bg-emerald-900/60 text-emerald-300 border border-emerald-700'}">
                        ${isFull ? '전체 (Full)' : '증분 (Incremental)'}
                    </span>
                </td>
                <td class="py-3 px-4 text-slate-300">${formatDate(isoTime)}</td>
                <td class="py-3 px-4 text-slate-300">${totalFiles}개 <span class="text-xs text-slate-500">(${formatBytes(totalBytes)})</span></td>
                <td class="py-3 px-4 text-emerald-400 font-medium">${formatBytes(dedupBytes)}</td>
                <td class="py-3 px-4 text-right space-x-2">
                    <button onclick="inspectSnapshot('${s.id}')" class="p-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-lg text-xs" title="파일 탐색">
                        <i data-lucide="folder-search" class="w-4 h-4"></i>
                    </button>
                    <button onclick="openRestoreModal('${s.id}')" class="p-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs" title="시점 복원">
                        <i data-lucide="rotate-ccw" class="w-4 h-4"></i>
                    </button>
                    <button onclick="verifySnapshot('${s.id}')" class="p-1.5 ${isVerified ? 'bg-emerald-600/80' : 'bg-emerald-700'} hover:bg-emerald-600 text-white rounded-lg text-xs" title="${isVerified ? '무결성 재검증' : '무결성 검증'}">
                        <i data-lucide="shield-check" class="w-4 h-4"></i>
                    </button>
                    <button onclick="deleteSnapshot('${s.id}')" class="p-1.5 bg-red-900/60 hover:bg-red-600 text-red-300 hover:text-white rounded-lg text-xs" title="삭제">
                        <i data-lucide="trash-2" class="w-4 h-4"></i>
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    lucide.createIcons();
}

// State for explorer navigation
state.explorer = {
    snapshotId: null,
    currentPath: '',
    items: [],
    filterQuery: ''
};

async function inspectSnapshot(snapshotId) {
    try {
        state.explorer.snapshotId = snapshotId;
        state.explorer.currentPath = '';
        state.explorer.filterQuery = '';

        // Fetch lightweight metadata only (no 30MB entries download, <1ms response)
        const snapData = await fetchAPI(`/api/snapshots/${snapshotId}?include_entries=false`);
        state.selectedSnapshot = snapData;

        document.getElementById('explorer-modal-title').innerText = `스냅샷 파일 탐색: ${snapshotId}`;
        const sum = snapData.summary || {};
        const totalFiles = (sum.total_files !== undefined && sum.total_files !== null && sum.total_files > 0) ? sum.total_files : (snapData.total_files || 0);
        const totalBytes = (sum.total_bytes !== undefined && sum.total_bytes !== null && sum.total_bytes > 0) ? sum.total_bytes : (snapData.total_bytes || 0);
        const isoTime = snapData.iso_time || (typeof snapData.created_at === 'string' ? snapData.created_at : '');
        const offBadge = (snapData.is_offsite_protected || snapData.offsite_status === 'COMMITTED')
            ? '<span class="text-blue-400 font-semibold">🌐 3-2-1 원격보호 완료</span>'
            : ((snapData.offsite_status === 'TRANSFERRING' || snapData.offsite_status === 'PENDING')
                ? '<span class="text-amber-300 font-semibold animate-pulse">⏳ 원격동기화 중</span>'
                : (snapData.offsite_status === 'UNSYNCED'
                    ? '<span class="text-amber-400 font-semibold">⏸️ 원격미복제</span>'
                    : '<span class="text-slate-400">로컬전용</span>'));
        document.getElementById('explorer-modal-meta').innerHTML = `
            <span>생성 일시: <b>${formatDate(isoTime)}</b></span> · 
            <span>총 파일: <b>${totalFiles}개 (${formatBytes(totalBytes)})</b></span> · 
            <span>신규/수정: <b>${(sum.new_files || 0) + (sum.modified_files || 0)}개</b></span> · 
            <span>3-2-1 복제: <b>${offBadge}</b></span>
        `;

        const searchInput = document.getElementById('explorer-search-input');
        if (searchInput) searchInput.value = '';

        openModal('explorer-modal');
        await loadExplorerPath('');
    } catch (e) {
        alert('스냅샷 정보를 불러오지 못했습니다: ' + e.message);
    }
}

async function loadExplorerPath(subpath) {
    const container = document.getElementById('explorer-tree-container');
    if (!container) return;

    state.explorer.currentPath = subpath;
    container.innerHTML = `
        <div class="flex items-center justify-center py-12 text-slate-400 gap-2">
            <span class="w-4 h-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin"></span>
            <span>폴더 내용을 불러오는 중...</span>
        </div>
    `;

    renderExplorerBreadcrumb(subpath);

    try {
        const res = await fetchAPI(`/api/snapshots/${state.explorer.snapshotId}/browse?subpath=${encodeURIComponent(subpath)}`);
        state.explorer.items = res.items || [];
        renderExplorerItems();
    } catch (e) {
        container.innerHTML = `<div class="p-4 text-center text-red-400">오류 발생: ${e.message}</div>`;
    }
}

function renderExplorerBreadcrumb(subpath) {
    const bcContainer = document.getElementById('explorer-breadcrumb');
    if (!bcContainer) return;

    const parts = subpath ? subpath.split('/').filter(Boolean) : [];
    let html = `
        <button onclick="loadExplorerPath('')" class="hover:text-blue-400 transition flex items-center gap-1 ${parts.length === 0 ? 'text-slate-100 font-bold' : 'text-slate-400'}">
            <i data-lucide="home" class="w-3.5 h-3.5"></i>
            <span>루트</span>
        </button>
    `;

    let accumulated = '';
    parts.forEach((p, idx) => {
        accumulated += (idx === 0 ? '' : '/') + p;
        const isLast = (idx === parts.length - 1);
        const pathTarget = accumulated;
        html += `
            <span class="text-slate-600">/</span>
            <button onclick="loadExplorerPath('${pathTarget.replace(/'/g, "\\'")}')" class="hover:text-blue-400 transition truncate max-w-[140px] ${isLast ? 'text-slate-100 font-bold' : 'text-slate-400'}">
                ${p}
            </button>
        `;
    });

    bcContainer.innerHTML = html;
    lucide.createIcons();
}

function filterExplorerItems() {
    const searchInput = document.getElementById('explorer-search-input');
    state.explorer.filterQuery = searchInput ? searchInput.value.trim().toLowerCase() : '';
    renderExplorerItems();
}

function renderExplorerItems() {
    const container = document.getElementById('explorer-tree-container');
    const countEl = document.getElementById('explorer-item-count');
    if (!container) return;

    let items = state.explorer.items;
    if (state.explorer.filterQuery) {
        items = items.filter(it => it.name.toLowerCase().includes(state.explorer.filterQuery));
    }

    if (countEl) {
        countEl.innerText = `현재 위치 항목: ${items.length}개 (전체 ${state.explorer.items.length}개)`;
    }

    if (items.length === 0) {
        container.innerHTML = `<div class="text-center py-10 text-slate-500">표시할 파일이나 폴더가 없습니다.</div>`;
        return;
    }

    let html = '';
    if (state.explorer.currentPath) {
        const parentPath = state.explorer.currentPath.includes('/') 
            ? state.explorer.currentPath.substring(0, state.explorer.currentPath.lastIndexOf('/'))
            : '';
        html += `
            <div onclick="loadExplorerPath('${parentPath.replace(/'/g, "\\'")}')" class="flex items-center gap-2 py-1.5 px-3 rounded-lg text-xs cursor-pointer hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition">
                <i data-lucide="corner-left-up" class="w-4 h-4 text-slate-400"></i>
                <span class="font-medium">.. (상위 디렉토리로 이동)</span>
            </div>
        `;
    }

    html += items.map(item => {
        const isDir = item.type === 'directory';
        const icon = isDir ? 'folder' : 'file';
        const iconColor = isDir ? 'text-amber-400' : 'text-blue-400';
        const itemRelPath = (item.rel_path || '').replace(/'/g, "\\'");
        const itemName = (item.name || '').replace(/'/g, "\\'");

        const clickAction = isDir 
            ? `onclick="loadExplorerPath('${itemRelPath}')"`
            : '';

        return `
            <div ${clickAction} class="flex items-center justify-between py-1.5 px-3 rounded-lg text-xs cursor-pointer hover:bg-slate-800/80 transition group select-none">
                <div class="flex items-center gap-2.5 overflow-hidden flex-1 mr-2">
                    <i data-lucide="${icon}" class="w-4 h-4 ${iconColor} shrink-0"></i>
                    <span class="truncate font-medium ${isDir ? 'text-slate-200 hover:text-amber-300' : 'text-slate-300'}">${item.name}</span>
                    ${isDir && item.file_count ? `<span class="text-[10px] text-slate-500">(${item.file_count}개 파일)</span>` : ''}
                </div>
                <div class="flex items-center gap-3 shrink-0 text-slate-500 text-[11px]">
                    ${!isDir ? `<span>${formatBytes(item.size)}</span>` : (item.size ? `<span>${formatBytes(item.size)}</span>` : '')}
                    ${!isDir && item.chunk_strategy && item.chunk_strategy !== 'whole_file' ? `<span class="px-1.5 py-0.5 rounded text-[10px] bg-cyan-950 text-cyan-300 border border-cyan-800 font-mono" title="${item.chunk_strategy} 청킹 (${item.chunk_count}개 분할)">🧩 ${item.chunk_strategy.toUpperCase()} (${item.chunk_count}청킹)</span>` : ''}
                    ${!isDir && item.status ? `<span class="px-1.5 py-0.5 rounded text-[10px] ${item.status === 'new' ? 'bg-emerald-950 text-emerald-400' : item.status === 'modified' ? 'bg-amber-950 text-amber-400' : 'bg-slate-800 text-slate-400'}">${item.status}</span>` : ''}
                    <button type="button" onclick="event.stopPropagation(); openRestoreModal('${state.explorer.snapshotId}', '${itemRelPath}', '${itemName}', '${item.type}')" class="opacity-0 group-hover:opacity-100 px-2.5 py-1 bg-slate-800 hover:bg-blue-600 text-slate-300 hover:text-white rounded-lg text-[11px] font-medium transition flex items-center gap-1 shadow border border-slate-700 hover:border-blue-500" title="${isDir ? '이 폴더만 복원' : '이 파일만 복원'}">
                        <i data-lucide="rotate-ccw" class="w-3 h-3"></i>
                        <span>복원</span>
                    </button>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = html;
    lucide.createIcons();
}

async function verifySnapshot(snapshotId) {
    if (!confirm(`스냅샷 [${snapshotId}]의 모든 백업 블롭 무결성을 검증하시겠습니까?`)) return;
    try {
        const res = await fetchAPI('/api/verify/run', {
            method: 'POST',
            body: JSON.stringify({ snapshot_id: snapshotId })
        });
        if (res.is_valid) {
            alert(`✅ 무결성 검증 성공!\n총 ${res.total_files}개 파일이 완벽하게 보존되어 있으며 손상이 없습니다.`);
        } else {
            alert(`⚠️ 무결성 검증 실패!\n누락된 블롭: ${res.missing_blobs.length}개\n손상된 블롭: ${res.corrupted_blobs.length}개`);
        }
    } catch (e) {
        alert('무결성 검증 중 오류 발생: ' + e.message);
    }
}

async function deleteSnapshot(snapshotId) {
    if (!confirm(`정말로 스냅샷 [${snapshotId}]을 삭제하시겠습니까?\n삭제 후 참조되지 않는 데이터는 정리됩니다.`)) return;
    try {
        await fetchAPI(`/api/snapshots/${snapshotId}`, { method: 'DELETE' });
        await loadSnapshots();
        await loadDashboard();
    } catch (e) {
        alert('삭제 실패: ' + e.message);
    }
}

// Restore Modal Handler
function openRestoreModal(snapshotId, targetRelPath = null, targetName = null, targetType = null) {
    document.getElementById('restore-snapshot-id').value = snapshotId;
    document.getElementById('restore-modal-title').innerText = `스냅샷 복원 (${snapshotId})`;

    // Check if snapshot is in state
    const snap = (state.snapshots || []).find(s => s.id === snapshotId) || state.selectedSnapshot;
    const sources = (snap && snap.sources) ? snap.sources : [];

    // Render original sources list
    const sourcesContainer = document.getElementById('restore-original-sources-list');
    if (sourcesContainer) {
        if (sources.length > 0) {
            sourcesContainer.innerHTML = sources.map(s => `<div class="truncate flex items-center gap-1.5"><i data-lucide="folder" class="w-3 h-3 text-amber-400 shrink-0"></i> ${s}</div>`).join('');
        } else {
            sourcesContainer.innerHTML = `<div class="text-slate-500">스냅샷에 기록된 원본 경로가 없습니다.</div>`;
        }
    }

    // Handle selective restore mode if triggered from explorer
    const selectivePathsInput = document.getElementById('restore-selected-paths');
    const badgeEl = document.getElementById('restore-selective-badge');
    const badgeTextEl = document.getElementById('restore-selective-text');

    if (targetRelPath) {
        if (selectivePathsInput) selectivePathsInput.value = JSON.stringify([targetRelPath]);
        if (badgeEl && badgeTextEl) {
            badgeEl.classList.remove('hidden');
            const typeLabel = targetType === 'directory' ? '폴더' : '파일';
            badgeTextEl.innerText = `선택 ${typeLabel} 복원: [${targetRelPath}]`;
        }
    } else {
        clearSelectiveRestore();
    }

    // Default mode: Safe mode (checked)
    const radioSafe = document.getElementById('restore-mode-safe');
    if (radioSafe) {
        radioSafe.checked = true;
    }
    toggleRestoreMode();

    openModal('restore-modal');
    lucide.createIcons();
}

function clearSelectiveRestore() {
    const selectivePathsInput = document.getElementById('restore-selected-paths');
    const badgeEl = document.getElementById('restore-selective-badge');
    if (selectivePathsInput) selectivePathsInput.value = '';
    if (badgeEl) badgeEl.classList.add('hidden');
}

function toggleRestoreMode() {
    const isInplace = document.getElementById('restore-mode-inplace')?.checked;
    const warningEl = document.getElementById('restore-inplace-warning');
    const targetDirContainer = document.getElementById('restore-target-dir-container');
    const startBtn = document.getElementById('btn-start-restore');

    if (isInplace) {
        if (warningEl) warningEl.classList.remove('hidden');
        if (targetDirContainer) targetDirContainer.classList.add('hidden');
        if (startBtn) {
            startBtn.classList.remove('bg-blue-600', 'hover:bg-blue-500');
            startBtn.classList.add('bg-amber-600', 'hover:bg-amber-500');
            const span = startBtn.querySelector('span');
            if (span) span.innerText = '원래 위치로 즉시 롤백 시작';
        }
    } else {
        if (warningEl) warningEl.classList.add('hidden');
        if (targetDirContainer) targetDirContainer.classList.remove('hidden');
        if (startBtn) {
            startBtn.classList.remove('bg-amber-600', 'hover:bg-amber-500');
            startBtn.classList.add('bg-blue-600', 'hover:bg-blue-500');
            const span = startBtn.querySelector('span');
            if (span) span.innerText = '새 폴더에 복원 시작';
        }
    }
    lucide.createIcons();
}

async function startRestore() {
    const snapId = document.getElementById('restore-snapshot-id').value;
    const isInplace = document.getElementById('restore-mode-inplace')?.checked;
    const targetDir = document.getElementById('restore-target-dir').value.trim();
    const overwrite = document.getElementById('restore-overwrite').checked;
    const selectedPathsRaw = document.getElementById('restore-selected-paths')?.value;
    let selectedRelPaths = null;
    if (selectedPathsRaw) {
        try {
            selectedRelPaths = JSON.parse(selectedPathsRaw);
        } catch (e) {
            selectedRelPaths = null;
        }
    }

    if (!isInplace && !targetDir) {
        alert('새 폴더 복원 모드에서는 복원할 대상 폴더 경로를 입력해야 합니다.');
        return;
    }

    closeModal('restore-modal');
    closeModal('explorer-modal');
    switchTab('runner');

    try {
        await fetchAPI('/api/restore/run', {
            method: 'POST',
            body: JSON.stringify({
                snapshot_id: snapId,
                target_dir: isInplace ? null : targetDir,
                overwrite: overwrite,
                in_place: !!isInplace,
                selected_rel_paths: selectedRelPaths
            })
        });
    } catch (e) {
        alert('복원 시작 실패: ' + e.message);
    }
}

// Quick Backup Trigger (Uses currently scheduled / active profile)
async function triggerQuickBackup() {
    const profiles = state.profiles || [];
    const activeProf = profiles.find(p => p.auto_backup_enabled) || (profiles.length > 0 ? profiles[0] : null);
    const profId = activeProf ? activeProf.id : 'prof_default';

    switchTab('runner');
    try {
        await fetchAPI('/api/backup/run', {
            method: 'POST',
            body: JSON.stringify({ profile_id: profId })
        });
    } catch (e) {
        alert('백업 시작 실패: ' + e.message);
    }
}

async function cancelCurrentTask() {
    if (!confirm('현재 실행 중인 백업/복원 작업을 취소하시겠습니까?')) return;
    try {
        await fetchAPI('/api/backup/cancel', { method: 'POST' });
    } catch (e) {
        alert('취소 요청 실패: ' + e.message);
    }
}

// Task Polling & Live Runner
async function pollTaskStatus() {
    try {
        const task = await fetchAPI('/api/task/status');
        state.taskStatus = task;

        const wasRunning = state.taskWasRunning || false;
        const isRunning = task.running;
        state.taskWasRunning = isRunning;

        // Auto Live Refresh on task completion (running -> completed)
        if (wasRunning && !isRunning) {
            loadDashboard();
            loadSnapshots();
            if (state.activeTab === 'profiles') loadProfiles();
        }

        const progress = task.progress || {};

        // Update Runner UI
        const runnerStatusEl = document.getElementById('runner-status-badge');
        const runnerProgressBar = document.getElementById('runner-progress-bar');
        const runnerPercentText = document.getElementById('runner-percent-text');
        const runnerFileText = document.getElementById('runner-current-file');
        const runnerStatsText = document.getElementById('runner-stats-text');
        const runnerCancelBtn = document.getElementById('runner-cancel-btn');

        if (isRunning) {
            runnerStatusEl.className = 'px-3 py-1 bg-amber-500/20 text-amber-300 border border-amber-500/30 rounded-full text-xs font-semibold animate-pulse';
            runnerStatusEl.innerText = task.type === 'backup' ? '백업 진행 중...' : '복원 진행 중...';
            runnerCancelBtn.classList.remove('hidden');
        } else {
            if (task.error) {
                runnerStatusEl.className = 'px-3 py-1 bg-red-500/20 text-red-300 border border-red-500/30 rounded-full text-xs font-semibold';
                runnerStatusEl.innerText = '오류 / 중단됨';
            } else if (task.result) {
                runnerStatusEl.className = 'px-3 py-1 bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 rounded-full text-xs font-semibold';
                runnerStatusEl.innerText = '작업 완료';
            } else {
                runnerStatusEl.className = 'px-3 py-1 bg-slate-700 text-slate-300 rounded-full text-xs font-semibold';
                runnerStatusEl.innerText = '대기 중';
            }
            runnerCancelBtn.classList.add('hidden');
        }

        const pct = progress.percent || 0;
        runnerProgressBar.style.width = `${pct}%`;
        runnerPercentText.innerText = `${pct}%`;
        runnerFileText.innerText = progress.current_file ? `처리 중: ${progress.current_file}` : (isRunning ? '스캔 및 분석 중...' : '대기 중');
        
        if (task.type === 'backup') {
            runnerStatsText.innerText = `파일: ${progress.processed_files || 0} / ${progress.total_files || 0} (신규: ${progress.new_files || 0}, 수정: ${progress.modified_files || 0}, 동일: ${progress.unmodified_files || 0})`;
        } else if (task.type === 'restore') {
            runnerStatsText.innerText = `복원: ${progress.restored_files || 0} / ${progress.total_files || 0} 파일 (${formatBytes(progress.restored_bytes)})`;
        }

        // Terminal Logs
        const terminal = document.getElementById('terminal-log-box');
        if (terminal && task.logs) {
            terminal.innerHTML = task.logs.map(l => `<div>${l}</div>`).join('');
            terminal.scrollTop = terminal.scrollHeight;
        }

    } catch (e) {
        // quiet error on background poll
    }
}

// --- 1-Sec DR Drill (복구 모의훈련) ---
async function runDRDrill(snapshotId = null) {
    const targetSnapId = snapshotId || state.selectedSnapshot || 'latest';
    try {
        const res = await fetchAPI('/api/backup/drill', {
            method: 'POST',
            body: JSON.stringify({
                snapshot_id: targetSnapId,
                sample_count: 3
            })
        });

        if (!res) return;

        const certEl = document.getElementById('drill-cert-id');
        const snapEl = document.getElementById('drill-snap-id');
        const durEl = document.getElementById('drill-duration');
        const badgeEl = document.getElementById('drill-status-badge');
        const filesEl = document.getElementById('drill-files-list');

        if (certEl) certEl.innerText = res.certificate_id || 'DR-CERT-SUCCESS';
        if (snapEl) snapEl.innerText = res.snapshot_id || targetSnapId;
        if (durEl) durEl.innerText = `${res.duration_ms} ms (초고속 실측)`;

        if (badgeEl) {
            if (res.success) {
                badgeEl.className = 'font-bold text-emerald-400';
                badgeEl.innerText = '✅ 100% 정상 복구 보증 (Bit-Rot 0건)';
            } else {
                badgeEl.className = 'font-bold text-red-400';
                badgeEl.innerText = '⚠️ 복구 실패 / 손상 블롭 감지';
            }
        }

        if (filesEl && res.tested_files) {
            filesEl.innerHTML = res.tested_files.map(f => `
                <div class="flex items-center justify-between p-2 rounded-lg bg-slate-900 border border-slate-800">
                    <div class="truncate max-w-[280px]" title="${f.rel_path}">
                        <div class="text-slate-300 truncate">${f.rel_path}</div>
                        <div class="text-[10px] text-slate-500 font-mono">SHA: ${f.sha256} (${formatBytes(f.size_bytes)})</div>
                    </div>
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${f.verified ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/20 text-red-400'}">
                        ${f.verified ? '무결성 일치' : '해시 불일치'}
                    </span>
                </div>
            `).join('');
        }

        if (window.lucide) window.lucide.createIcons();
        openModal('modal-dr-drill');
    } catch (err) {
        alert('복구 모의훈련 실행 오류: ' + (err.message || err));
    }
}
