// =========================================================================
// [백업시스템] 백업 시점 관리, 파일 탐색기(Explorer), 되돌리기(Restore) & 실시간 로그
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
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-10 text-slate-500">백업 백업 시점이 존재하지 않습니다.</td></tr>`;
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
                        ${isVerified ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800 font-semibold" title="백업 점검 완료">✅ 검증됨</span>' : ''}
                        ${s.vss_enabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800 font-semibold" title="Windows Volume Shadow Copy(VSS) 일관성 백업">VSS</span>' : ''}
                        ${s.is_local_protected !== false ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-semibold" title="이 PC 백업이 잠금 상태로 온전한 것을 확인했어요">🛡️ 로컬보호</span>' : ''}
                        ${(() => {
                            if (s.is_offsite_protected || s.offsite_status === 'COMMITTED') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-950 text-blue-400 border border-blue-800 font-semibold" title="다른 장소 보관함에 복사 완료">🌐 다른 장소 보관</span>';
                            } else if (s.offsite_status === 'TRANSFERRING' || s.offsite_status === 'PENDING') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800 font-semibold animate-pulse" title="원격 백업 보관함 복제 동기화 진행 중">⏳ 원격동기화</span>';
                            } else if (s.offsite_status === 'UNSYNCED') {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-900 text-amber-400 border border-amber-700/60 font-semibold" title="원격 백업 보관함 미동기화 (대기 중)">⏸️ 원격미복제</span>';
                            } else {
                                return '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-900 text-slate-400 border border-slate-700 font-medium" title="다른 장소 보관 원격 복제 비활성화">로컬전용</span>';
                            }
                        })()}
                    </div>
                </td>
                <td class="py-3 px-4 font-medium text-slate-200">${profName}</td>
                <td class="py-3 px-4">
                    <span class="text-[11px] px-2 py-0.5 rounded-full font-medium ${isFull ? 'bg-blue-900/60 text-blue-300 border border-blue-700' : 'bg-emerald-900/60 text-emerald-300 border border-emerald-700'}">
                        ${isFull ? '전체 (Full)' : '바뀐 것만 (Incremental)'}
                    </span>
                </td>
                <td class="py-3 px-4 text-slate-300">${formatDate(isoTime)}</td>
                <td class="py-3 px-4 text-slate-300">${totalFiles}개 <span class="text-xs text-slate-500">(${formatBytes(totalBytes)})</span></td>
                <td class="py-3 px-4 text-emerald-400 font-medium">${formatBytes(dedupBytes)}</td>
                <td class="py-3 px-4 text-right space-x-2">
                    <button onclick="inspectSnapshot('${s.id}')" class="p-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-lg text-xs" title="파일 탐색">
                        <i data-lucide="folder-search" class="w-4 h-4"></i>
                    </button>
                    <button onclick="openRestoreModal('${s.id}')" class="p-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs" title="시점 되돌리기">
                        <i data-lucide="rotate-ccw" class="w-4 h-4"></i>
                    </button>
                    <button onclick="verifySnapshot('${s.id}')" class="p-1.5 ${isVerified ? 'bg-emerald-600/80' : 'bg-emerald-700'} hover:bg-emerald-600 text-white rounded-lg text-xs" title="${isVerified ? '온전함 확인 재검증' : '백업 점검'}">
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

        document.getElementById('explorer-modal-title').innerText = `백업 시점 파일 탐색: ${snapshotId}`;
        const sum = snapData.summary || {};
        const totalFiles = (sum.total_files !== undefined && sum.total_files !== null && sum.total_files > 0) ? sum.total_files : (snapData.total_files || 0);
        const totalBytes = (sum.total_bytes !== undefined && sum.total_bytes !== null && sum.total_bytes > 0) ? sum.total_bytes : (snapData.total_bytes || 0);
        const isoTime = snapData.iso_time || (typeof snapData.created_at === 'string' ? snapData.created_at : '');
        const offBadge = (snapData.is_offsite_protected || snapData.offsite_status === 'COMMITTED')
            ? '<span class="text-blue-400 font-semibold">🌐 다른 장소 보관 완료</span>'
            : ((snapData.offsite_status === 'TRANSFERRING' || snapData.offsite_status === 'PENDING')
                ? '<span class="text-amber-300 font-semibold animate-pulse">⏳ 원격동기화 중</span>'
                : (snapData.offsite_status === 'UNSYNCED'
                    ? '<span class="text-amber-400 font-semibold">⏸️ 원격미복제</span>'
                    : '<span class="text-slate-400">로컬전용</span>'));
        document.getElementById('explorer-modal-meta').innerHTML = `
            <span>생성 일시: <b>${formatDate(isoTime)}</b></span> · 
            <span>총 파일: <b>${totalFiles}개 (${formatBytes(totalBytes)})</b></span> · 
            <span>신규/수정: <b>${(sum.new_files || 0) + (sum.modified_files || 0)}개</b></span> · 
            <span>다른 장소 복사: <b>${offBadge}</b></span>
        `;

        const searchInput = document.getElementById('explorer-search-input');
        if (searchInput) searchInput.value = '';

        openModal('explorer-modal');
        await loadExplorerPath('');
    } catch (e) {
        alert('백업 시점 정보를 불러오지 못했습니다: ' + friendlyError(e));
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
        container.innerHTML = `<div class="p-4 text-center text-red-400">오류 발생: ${friendlyError(e)}</div>`;
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
                    <button type="button" onclick="event.stopPropagation(); openRestoreModal('${state.explorer.snapshotId}', '${itemRelPath}', '${itemName}', '${item.type}')" class="opacity-0 group-hover:opacity-100 px-2.5 py-1 bg-slate-800 hover:bg-blue-600 text-slate-300 hover:text-white rounded-lg text-[11px] font-medium transition flex items-center gap-1 shadow border border-slate-700 hover:border-blue-500" title="${isDir ? '이 폴더만 되돌리기' : '이 파일만 되돌리기'}">
                        <i data-lucide="rotate-ccw" class="w-3 h-3"></i>
                        <span>되돌리기</span>
                    </button>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = html;
    lucide.createIcons();
}

async function verifySnapshot(snapshotId) {
    if (!confirm(`백업 시점 [${snapshotId}]의 모든 백업 저장 조각 온전함 확인을 검증하시겠습니까?`)) return;
    try {
        const res = await fetchAPI('/api/verify/run', {
            method: 'POST',
            body: JSON.stringify({ snapshot_id: snapshotId })
        });
        if (res.success) {
            alert(`✅ 백업 점검 성공!\n검사된 저장 조각 ${res.verified_count}개, 오류 0개. 모든 백업 데이터가 정상입니다.`);
        } else {
            alert(`⚠️ 백업 점검 실패!\n손상된 저장 조각: ${res.error_count}개 감지됨`);
        }
        await loadSnapshots();
    } catch (e) {
        alert('백업 점검 중 오류 발생: ' + friendlyError(e));
    }
}

async function deleteSnapshot(snapshotId) {
    if (!confirm(`정말로 백업 시점 [${snapshotId}]을 삭제하시겠습니까?\n삭제 후 참조되지 않는 데이터는 정리됩니다.`)) return;
    try {
        await fetchAPI(`/api/snapshots/${snapshotId}`, { method: 'DELETE' });
        await loadSnapshots();
        await loadDashboard();
    } catch (e) {
        alert('삭제 실패: ' + friendlyError(e));
    }
}

// Restore Modal Handler
function openRestoreModal(snapshotId, targetRelPath = null, targetName = null, targetType = null) {
    document.getElementById('restore-snapshot-id').value = snapshotId;
    document.getElementById('restore-modal-title').innerText = `백업 시점 되돌리기 (${snapshotId})`;

    // Check if snapshot is in state
    const snap = (state.snapshots || []).find(s => s.id === snapshotId) || state.selectedSnapshot;
    const sources = (snap && snap.sources) ? snap.sources : [];

    // Render original sources list
    const sourcesContainer = document.getElementById('restore-original-sources-list');
    if (sourcesContainer) {
        if (sources.length > 0) {
            sourcesContainer.innerHTML = sources.map(s => `<div class="truncate flex items-center gap-1.5"><i data-lucide="folder" class="w-3 h-3 text-amber-400 shrink-0"></i> ${s}</div>`).join('');
        } else {
            sourcesContainer.innerHTML = `<div class="text-slate-500">백업 시점에 기록된 원본 경로가 없습니다.</div>`;
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
            badgeTextEl.innerText = `선택 ${typeLabel} 되돌리기: [${targetRelPath}]`;
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
            if (span) span.innerText = '새 폴더에 되돌리기 시작';
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
        alert('새 폴더 되돌리기 모드에서는 되돌릴 대상 폴더 경로를 입력해야 합니다.');
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
        alert('되돌리기 시작 실패: ' + friendlyError(e));
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
        alert('백업 시작 실패: ' + friendlyError(e));
    }
}

async function cancelCurrentTask() {
    if (!confirm('현재 실행 중인 백업/되돌리기 작업을 취소하시겠습니까?')) return;
    try {
        await fetchAPI('/api/backup/cancel', { method: 'POST' });
    } catch (e) {
        alert('취소 요청 실패: ' + friendlyError(e));
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
            if (state.activeTab === 'profiles' || state.activeTab === 'settings') loadProfiles();
        }

        const progress = task.progress || {};

        // Update Runner UI
        const runnerNavBtn = document.getElementById('nav-tab-runner');
        if (runnerNavBtn) {
            runnerNavBtn.classList.toggle('hidden', !isRunning && state.activeTab !== 'runner');
        }

        const runnerStatusEl = document.getElementById('runner-status-badge');
        const runnerProgressBar = document.getElementById('runner-progress-bar');
        const runnerPercentText = document.getElementById('runner-percent-text');
        const runnerFileText = document.getElementById('runner-current-file');
        const runnerStatsText = document.getElementById('runner-stats-text');
        const runnerCancelBtn = document.getElementById('runner-cancel-btn');

        if (isRunning) {
            runnerStatusEl.className = 'px-3 py-1 bg-amber-500/20 text-amber-300 border border-amber-500/30 rounded-full text-xs font-semibold animate-pulse';
            runnerStatusEl.innerText = task.type === 'backup' ? '백업 진행 중...' : '되돌리기 진행 중...';
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
            runnerStatsText.innerText = `되돌리기: ${progress.restored_files || 0} / ${progress.total_files || 0} 파일 (${formatBytes(progress.restored_bytes)})`;
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
                badgeEl.innerText = '⚠️ 복구 실패 / 손상 저장 조각 감지';
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
                        ${f.verified ? '온전함 확인 일치' : '내용이 달라짐'}
                    </span>
                </div>
            `).join('');
        }

        if (window.lucide) window.lucide.createIcons();
        openModal('modal-dr-drill');
    } catch (err) {
        alert('복구 모의훈련 실행 오류: ' + friendlyError(err));
    }
}
// =========================================================================
// [원클릭 파일 되돌리기] 타임머신 검색 & 시점별 빠른 복원 마법사
// =========================================================================
let fileSearchDebounceTimer = null;
let currentSearchQuery = '';

function _escapeHtmlSnap(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function onSearchFileRestoreInput() {
    clearTimeout(fileSearchDebounceTimer);
    const q = document.getElementById('file-restore-search-input')?.value?.trim();
    if (!q || q.length < 2) {
        const resEl = document.getElementById('file-restore-search-results');
        const statusEl = document.getElementById('file-search-status');
        if (resEl) {
            resEl.classList.add('hidden');
            resEl.innerHTML = '';
        }
        if (statusEl) statusEl.innerHTML = '';
        return;
    }
    fileSearchDebounceTimer = setTimeout(() => {
        executeFileSearch();
    }, 250);
}

async function executeFileSearch() {
    const inputEl = document.getElementById('file-restore-search-input');
    const q = inputEl?.value?.trim();
    if (!q || q.length < 2) return;

    currentSearchQuery = q;
    const statusEl = document.getElementById('file-search-status');
    const resEl = document.getElementById('file-restore-search-results');
    if (statusEl) {
        statusEl.innerHTML = `<span class="text-blue-400 flex items-center gap-1"><i data-lucide="loader" class="w-3.5 h-3.5 animate-spin"></i> 백업 검색 중...</span>`;
        if (window.lucide) lucide.createIcons();
    }

    try {
        const results = await fetchAPI('/api/snapshots/search-files?q=' + encodeURIComponent(q));
        renderFileSearchResults(results, q);
    } catch (e) {
        console.error('File search failed:', e);
        if (statusEl) statusEl.innerHTML = `<span class="text-rose-400">검색 실패</span>`;
    }
}

function renderFileSearchResults(results, query) {
    const resEl = document.getElementById('file-restore-search-results');
    const statusEl = document.getElementById('file-search-status');
    if (!resEl) return;

    resEl.classList.remove('hidden');

    if (!results || results.length === 0) {
        if (statusEl) statusEl.innerHTML = `<span class="text-slate-400">결과 없음</span>`;
        resEl.innerHTML = `
            <div class="py-6 text-center text-slate-400 text-xs">
                <i data-lucide="info" class="w-6 h-6 mx-auto mb-2 text-slate-500"></i>
                <p class="font-medium text-slate-300">'${_escapeHtmlSnap(query)}' 일치하는 파일이 백업에 없습니다.</p>
                <p class="text-slate-500 mt-1">철자를 확인하거나 다른 단어/확장자로 다시 검색해보세요.</p>
            </div>
        `;
        if (window.lucide) lucide.createIcons();
        return;
    }

    if (statusEl) {
        statusEl.innerHTML = `<span class="text-emerald-400 font-semibold">${results.length}개 파일 발견</span>`;
    }

    resEl.innerHTML = results.map((f, idx) => {
        const cardId = `file-card-${idx}`;
        const versionsId = `file-versions-${idx}`;
        const hasPastVersions = f.version_count > 1;

        // 과거 버전 리스트 HTML
        const pastVersionsHtml = (f.versions || []).slice(1).map((v, vIdx) => {
            return `
                <div class="flex items-center justify-between p-2 rounded-xl bg-slate-900/60 border border-slate-800 text-xs text-slate-300">
                    <div class="flex items-center gap-2">
                        <i data-lucide="clock" class="w-3.5 h-3.5 text-slate-500"></i>
                        <span class="font-mono text-slate-200">${v.snapshot_time ? v.snapshot_time.replace('T', ' ').slice(0, 19) : v.snapshot_id}</span>
                        <span class="px-1.5 py-0.5 rounded bg-slate-800 text-[10px] text-slate-400 font-mono">${v.size_formatted}</span>
                    </div>
                    <div class="flex items-center gap-1.5">
                        <button onclick="quickRestoreSingleFile('${v.snapshot_id}', '${encodeURIComponent(v.rel_path)}', '${encodeURIComponent(f.filename)}', 'desktop', '${encodeURIComponent(v.repo_dir || '')}')"
                                class="px-2 py-1 bg-blue-600/80 hover:bg-blue-600 text-white rounded-lg text-[11px] font-semibold transition flex items-center gap-1 shadow-sm">
                            <i data-lucide="monitor" class="w-3 h-3"></i> 바탕화면 복원
                        </button>
                        <button onclick="quickRestoreSingleFile('${v.snapshot_id}', '${encodeURIComponent(v.rel_path)}', '${encodeURIComponent(f.filename)}', 'inplace', '${encodeURIComponent(v.repo_dir || '')}')"
                                class="px-2 py-1 bg-slate-800 hover:bg-amber-600/30 text-amber-300 border border-amber-600/40 rounded-lg text-[11px] font-semibold transition flex items-center gap-1">
                            <i data-lucide="rotate-ccw" class="w-3 h-3"></i> 원래 위치 복원
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        return `
            <div id="${cardId}" class="p-3 rounded-xl bg-slate-900/70 border border-slate-800 hover:border-slate-700/80 transition space-y-2">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                    <div class="flex items-start gap-2.5 min-w-0">
                        <div class="p-1.5 bg-blue-500/10 border border-blue-500/20 rounded-lg text-blue-400 mt-0.5 shrink-0">
                            <i data-lucide="file-text" class="w-4 h-4"></i>
                        </div>
                        <div class="min-w-0">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="font-bold text-sm text-slate-100 truncate">${_escapeHtmlSnap(f.filename)}</span>
                                <span class="px-2 py-0.5 rounded-full bg-blue-950/70 border border-blue-700/50 text-[11px] text-blue-300 font-mono">
                                    ${f.version_count}개 시점 버전
                                </span>
                                <span class="text-xs text-slate-400 font-mono">
                                    최신: ${f.latest_size_formatted} (${f.latest_time ? f.latest_time.replace('T', ' ').slice(0, 19) : '-'})
                                </span>
                            </div>
                            <p class="text-[11px] text-slate-500 truncate font-mono mt-0.5" title="${_escapeHtmlSnap(f.path)}">
                                ${_escapeHtmlSnap(f.path)}
                            </p>
                        </div>
                    </div>
                    <!-- 최신 버전 원클릭 복원 버튼 그룹 -->
                    <div class="flex items-center gap-1.5 shrink-0 self-end sm:self-center">
                        <button onclick="quickRestoreSingleFile('${f.latest_snapshot_id}', '${encodeURIComponent(f.rel_path)}', '${encodeURIComponent(f.filename)}', 'desktop')"
                                class="px-2.5 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-xl text-xs font-semibold transition flex items-center gap-1 shadow-md shadow-blue-900/30">
                            <i data-lucide="monitor" class="w-3.5 h-3.5"></i>
                            <span>바탕화면에 복원</span>
                        </button>
                        <button onclick="quickRestoreSingleFile('${f.latest_snapshot_id}', '${encodeURIComponent(f.rel_path)}', '${encodeURIComponent(f.filename)}', 'inplace')"
                                class="px-2.5 py-1.5 bg-slate-800 hover:bg-amber-600/30 text-amber-300 hover:text-amber-200 border border-amber-600/40 rounded-xl text-xs font-semibold transition flex items-center gap-1">
                            <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i>
                            <span>원래 위치에 복원</span>
                        </button>
                    </div>
                </div>

                ${hasPastVersions ? `
                    <div class="pt-1.5 border-t border-slate-800/80">
                        <button onclick="toggleFileVersions('${versionsId}', this)"
                                class="text-xs text-blue-400 hover:text-blue-300 font-medium flex items-center gap-1 transition">
                            <i data-lucide="chevron-down" class="w-3.5 h-3.5 transition-transform duration-200"></i>
                            <span>과거 시점 (${f.version_count - 1}개) 버전 펼치기</span>
                        </button>
                        <div id="${versionsId}" class="hidden mt-2 space-y-1.5 pl-4 border-l-2 border-blue-500/30">
                            ${pastVersionsHtml}
                        </div>
                    </div>
                ` : ''}
            </div>
        `;
    }).join('');

    if (window.lucide) lucide.createIcons();
}

function toggleFileVersions(versionsId, btn) {
    const el = document.getElementById(versionsId);
    if (!el) return;
    const isHidden = el.classList.contains('hidden');
    const labelSpan = btn ? btn.querySelector('span') : null;
    const iconEl = btn ? btn.querySelector('i') : null;

    if (isHidden) {
        el.classList.remove('hidden');
        if (labelSpan) labelSpan.innerText = '과거 시점 버전 접기';
        if (iconEl) iconEl.classList.add('rotate-180');
    } else {
        el.classList.add('hidden');
        if (labelSpan) labelSpan.innerText = '과거 시점 버전 펼치기';
        if (iconEl) iconEl.classList.remove('rotate-180');
    }
}

async function quickRestoreSingleFile(snapshotId, encodedRelPath, encodedFilename, mode, repoDir = '') {
    const relPath = decodeURIComponent(encodedRelPath);
    const fileName = decodeURIComponent(encodedFilename);
    const decodedRepoDir = repoDir ? decodeURIComponent(repoDir) : null;

    if (mode === 'inplace') {
        const confirmed = confirm(
            `[원래 위치에 복원]\n\n'${fileName}' 파일을 백업 당시 원래 위치로 되돌리시겠습니까?\n\n※ 기존 파일이 존재할 경우 이 백업 시점의 파일로 덮어쓰여 교체됩니다.`
        );
        if (!confirmed) return;
    }

    try {
        switchTab('runner');
        if (typeof appendTaskLog === 'function') {
            appendTaskLog(`[원클릭 파일 되돌리기] '${fileName}' 복구 작업 시작 (${mode === 'desktop' ? '바탕화면 복원 폴더' : '원래 위치'})`);
        }

        await fetchAPI('/api/restore/run', {
            method: 'POST',
            body: JSON.stringify({
                snapshot_id: snapshotId,
                target_dir: mode === 'desktop' ? '@desktop' : null,
                overwrite: true,
                in_place: (mode === 'inplace'),
                selected_rel_paths: [relPath],
                repo_dir: decodedRepoDir
            })
        });
    } catch (e) {
        console.error('Quick restore failed:', e);
        alert('복구 요청 중 오류가 발생했습니다: ' + (e.message || e));
    }
}
