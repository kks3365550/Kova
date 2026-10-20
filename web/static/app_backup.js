// State Management
const state = {
    activeTab: 'dashboard',
    systemInfo: null,
    storageStats: null,
    profiles: [],
    selectedProfileId: null,
    snapshots: [],
    selectedSnapshot: null,
    selectedSnapshotTree: null,
    taskStatus: { running: false, type: null, progress: {} },
    pollInterval: null,
    browseModalCallback: null,
    currentBrowsePath: "",
    installedApps: [],
    selectedAppPaths: new Set(),
    aiProjects: [],
    selectedProjectPaths: new Set(),
    customFolders: []
};

// Utilities
function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatDate(isoStr) {
    if (!isoStr) return '-';
    const d = new Date(isoStr);
    return d.toLocaleString('ko-KR', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
}

// Tab Switching
function switchTab(tabId) {
    state.activeTab = tabId;
    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabId) {
            btn.classList.add('bg-blue-600', 'text-white');
            btn.classList.remove('text-slate-400', 'hover:bg-slate-800');
        } else {
            btn.classList.remove('bg-blue-600', 'text-white');
            btn.classList.add('text-slate-400', 'hover:bg-slate-800');
        }
    });

    document.querySelectorAll('.tab-content').forEach(view => {
        view.classList.toggle('hidden', view.id !== `tab-${tabId}`);
    });

    if (tabId === 'snapshots') loadSnapshots();
    if (tabId === 'profiles') loadProfiles();
    if (tabId === 'dashboard') loadDashboard();
    if (tabId === 'custom') loadCustomSelectionData();
    if (tabId === 'system-image') loadSystemImageStatus();
}

// API Calls
async function fetchAPI(url, options = {}) {
    try {
        const res = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'API request failed');
        }
        return await res.json();
    } catch (e) {
        console.error(`API Error (${url}):`, e);
        throw e;
    }
}

// Load Dashboard Data
async function loadDashboard() {
    try {
        const [sys, stats, profiles, snaps] = await Promise.all([
            fetchAPI('/api/system-info'),
            fetchAPI('/api/storage-stats'),
            fetchAPI('/api/profiles'),
            fetchAPI('/api/snapshots')
        ]);

        state.systemInfo = sys;
        state.storageStats = stats;
        state.profiles = profiles;
        state.snapshots = snaps;

        renderDashboardStats();
        renderRecentSnapshots();
        renderSystemDrives();
    } catch (err) {
        console.error('Failed to load dashboard:', err);
    }
}

function renderDashboardStats() {
    const stats = state.storageStats || {};
    document.getElementById('stat-total-snaps').innerText = stats.total_snapshots || '0';
    document.getElementById('stat-stored-bytes').innerText = formatBytes(stats.stored_bytes);
    document.getElementById('stat-logical-bytes').innerText = formatBytes(stats.logical_bytes);
    document.getElementById('stat-dedup-saved').innerText = `${formatBytes(stats.dedup_saved_bytes)} (${stats.savings_percentage || 0}%)`;

    // Active profiles count
    const activeProfCount = (state.profiles || []).filter(p => p.auto_backup_enabled).length;
    document.getElementById('stat-active-profiles').innerText = `${activeProfCount} / ${state.profiles.length}`;
}

function renderSystemDrives() {
    const container = document.getElementById('system-drives-list');
    if (!container || !state.systemInfo) return;

    container.innerHTML = state.systemInfo.drives.map(d => `
        <div class="p-3 bg-slate-800/80 rounded-xl border border-slate-700/60 flex flex-col gap-2">
            <div class="flex justify-between items-center text-xs">
                <span class="font-bold text-slate-200">${d.device} (${d.mountpoint})</span>
                <span class="text-slate-400">${d.percent}% 사용 중</span>
            </div>
            <div class="w-full bg-slate-900 rounded-full h-2 overflow-hidden">
                <div class="bg-gradient-to-r from-blue-500 to-indigo-500 h-2 rounded-full" style="width: ${Math.min(100, d.percent)}%"></div>
            </div>
            <div class="flex justify-between text-[11px] text-slate-400">
                <span>사용: ${formatBytes(d.used)}</span>
                <span>여유: ${formatBytes(d.free)}</span>
            </div>
        </div>
    `).join('');
}

function renderRecentSnapshots() {
    const container = document.getElementById('recent-snapshots-list');
    if (!container) return;

    if (!state.snapshots || state.snapshots.length === 0) {
        container.innerHTML = `
            <div class="text-center py-8 text-slate-500 text-sm">
                생성된 백업 스냅샷이 없습니다. 상단의 '지금 백업 시작' 버튼을 눌러 첫 백업을 생성하세요.
            </div>
        `;
        return;
    }

    const recents = state.snapshots.slice(0, 5);
    container.innerHTML = recents.map(s => {
        const isFull = s.backup_type === 'full';
        const sum = s.summary || {};
        return `
            <div class="p-4 bg-slate-800/60 hover:bg-slate-800 rounded-xl border border-slate-700/60 flex items-center justify-between transition-all">
                <div class="flex items-center gap-3">
                    <div class="p-2.5 rounded-lg ${isFull ? 'bg-blue-500/20 text-blue-400' : 'bg-emerald-500/20 text-emerald-400'}">
                        <i data-lucide="${isFull ? 'database' : 'layers'}" class="w-5 h-5"></i>
                    </div>
                    <div>
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-semibold text-sm text-slate-200">${s.profile_name || '백업'}</span>
                            <span class="text-[10px] px-2 py-0.5 rounded-full font-medium ${isFull ? 'bg-blue-900/60 text-blue-300 border border-blue-700' : 'bg-emerald-900/60 text-emerald-300 border border-emerald-700'}">
                                ${isFull ? 'FULL' : 'INCREMENTAL'}
                            </span>
                            ${s.vss_enabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800 font-semibold" title="Windows Volume Shadow Copy(VSS) 일관성 백업">VSS</span>' : ''}
                        </div>
                        <div class="text-xs text-slate-400 mt-0.5">
                            ${formatDate(s.iso_time)} · 파일 ${sum.total_files || 0}개 (${formatBytes(sum.total_bytes)})
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="openRestoreModal('${s.id}')" class="px-3 py-1.5 bg-slate-700 hover:bg-blue-600 text-slate-200 hover:text-white rounded-lg text-xs font-medium transition flex items-center gap-1.5">
                        <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i> 복원
                    </button>
                    <button onclick="inspectSnapshot('${s.id}')" class="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg text-xs font-medium transition">
                        탐색
                    </button>
                </div>
            </div>
        `;
    }).join('');

    lucide.createIcons();
}

// Snapshots Tab
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
        return `
            <tr class="border-b border-slate-800 hover:bg-slate-800/40 text-sm transition">
                <td class="py-3 px-4 font-mono text-xs text-blue-400">
                    <div class="flex items-center gap-1.5 flex-wrap">
                        <span>${s.id}</span>
                        ${s.vss_enabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800 font-semibold" title="Windows Volume Shadow Copy(VSS) 일관성 백업">VSS</span>' : ''}
                        ${s.is_local_protected !== false ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-semibold" title="로컬 복원 검증 & WORM 무결성 완료">🛡️ 로컬보호</span>' : ''}
                        ${s.is_offsite_protected ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-950 text-blue-400 border border-blue-800 font-semibold" title="원격 저장소 CAS 복제 및 해시 커밋 완료">🌐 원격보호</span>' : (s.offsite_status === 'TRANSFERRING' || s.offsite_status === 'PENDING' ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800 font-semibold animate-pulse" title="원격 저장소 증분 복제 진행 중">⏳ 원격동기화</span>' : '')}
                    </div>
                </td>
                <td class="py-3 px-4 font-medium text-slate-200">${s.profile_name || '-'}</td>
                <td class="py-3 px-4">
                    <span class="text-[11px] px-2 py-0.5 rounded-full font-medium ${isFull ? 'bg-blue-900/60 text-blue-300 border border-blue-700' : 'bg-emerald-900/60 text-emerald-300 border border-emerald-700'}">
                        ${isFull ? '전체 (Full)' : '증분 (Incremental)'}
                    </span>
                </td>
                <td class="py-3 px-4 text-slate-300">${formatDate(s.iso_time)}</td>
                <td class="py-3 px-4 text-slate-300">${sum.total_files || 0}개 <span class="text-xs text-slate-500">(${formatBytes(sum.total_bytes)})</span></td>
                <td class="py-3 px-4 text-emerald-400 font-medium">${formatBytes(sum.dedup_saved_bytes || 0)}</td>
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
        document.getElementById('explorer-modal-meta').innerHTML = `
            <span>생성 일시: <b>${formatDate(snapData.iso_time)}</b></span> · 
            <span>총 파일: <b>${sum.total_files}개 (${formatBytes(sum.total_bytes)})</b></span> · 
            <span>신규/수정: <b>${(sum.new_files || 0) + (sum.modified_files || 0)}개</b></span>
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

// Profiles Tab
async function loadProfiles() {
    try {
        state.profiles = await fetchAPI('/api/profiles');
        renderProfilesList();
        await loadWindowsTaskStatus();
    } catch (e) {
        console.error('Failed to load profiles:', e);
    }
}

function renderProfilesList() {
    const container = document.getElementById('profiles-list-container');
    if (!container) return;

    if (!state.profiles || state.profiles.length === 0) {
        container.innerHTML = `<div class="text-slate-500 text-sm">등록된 프로필이 없습니다.</div>`;
        return;
    }

    container.innerHTML = state.profiles.map(p => {
        let nextRunText = '수동 실행';
        if (p.auto_backup_enabled) {
            if (p.last_run) {
                if (p.schedule_type === 'daily') {
                    nextRunText = `매일 ${p.schedule_value}`;
                } else {
                    const nextTs = p.last_run + (parseFloat(p.schedule_value || 24) * 3600);
                    nextRunText = formatDate(new Date(nextTs * 1000).toISOString());
                }
            } else {
                nextRunText = '대기 중 (즉시 실행)';
            }
        }

        return `
        <div class="p-5 bg-slate-800/70 rounded-2xl border border-slate-700/80 flex flex-col gap-4">
            <div class="flex justify-between items-start">
                <div>
                    <h3 class="text-base font-bold text-slate-100">${p.name}</h3>
                    <p class="text-xs text-slate-400 mt-1">저장소: <code class="text-blue-400 font-mono">${p.repo_dir}</code></p>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="editProfile('${p.id}')" class="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg text-xs font-medium">수정</button>
                    <button onclick="triggerProfileBackup('${p.id}')" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-medium">백업 실행</button>
                    <button onclick="deleteProfile('${p.id}', '${p.name.replace(/'/g, "\\'")}')" class="px-3 py-1.5 bg-red-900/60 hover:bg-red-600 text-red-300 hover:text-white rounded-lg text-xs font-medium transition">삭제</button>
                </div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 bg-slate-900/60 p-3 rounded-xl text-xs">
                <div>
                    <span class="text-slate-500 block text-[11px]">스케줄 / 다음 예정</span>
                    <span class="font-medium text-slate-300">${p.auto_backup_enabled ? (p.schedule_type === 'daily' ? `매일 ${p.schedule_value}` : `${p.schedule_value}시간 주기`) : '수동 실행'}</span>
                    ${p.auto_backup_enabled ? `<span class="text-[10px] text-blue-400 font-mono block mt-0.5">다음: ${nextRunText}</span>` : ''}
                </div>
                <div>
                    <span class="text-slate-500 block text-[11px]">보관 개수</span>
                    <span class="font-medium text-slate-300">최근 ${p.retention_count}개 유지</span>
                </div>
                <div>
                    <span class="text-slate-500 block text-[11px]">압축 레벨</span>
                    <span class="font-medium text-slate-300">초고속 압축 (Level ${p.compression_level || 3})</span>
                </div>
                <div>
                    <span class="text-slate-500 block text-[11px]">최근 백업</span>
                    <span class="font-medium ${p.last_status === 'success' ? 'text-emerald-400' : p.last_status === 'failed' ? 'text-red-400' : 'text-slate-400'}">
                        ${p.last_run ? formatDate(new Date(p.last_run * 1000).toISOString()) : '기록 없음'}
                    </span>
                </div>
            </div>

            <details class="text-xs text-slate-400 bg-slate-900/40 p-3 rounded-xl border border-slate-800/80">
                <summary class="font-semibold text-slate-300 cursor-pointer flex items-center justify-between hover:text-white transition">
                    <span><i data-lucide="layers" class="w-3.5 h-3.5 inline text-blue-400 mr-1.5"></i> 백업 대상 경로 (${(p.sources || []).length}개 항목)</span>
                    <span class="text-[11px] text-slate-500 font-mono">클릭하여 경로 목록 보기</span>
                </summary>
                <ul class="list-disc list-inside mt-2.5 text-slate-400 font-mono text-[11px] max-h-36 overflow-y-auto space-y-1 pl-1">
                    ${(p.sources || []).map(s => `<li class="truncate hover:text-slate-200" title="${s}">${s}</li>`).join('')}
                </ul>
            </details>
        </div>
        `;
    }).join('');
    lucide.createIcons();
}

function onScheduleTypeChange() {
    const type = document.getElementById('prof-schedule-type').value;
    const intervalInput = document.getElementById('prof-interval');
    const dailyInput = document.getElementById('prof-daily-time');
    const label = document.getElementById('prof-schedule-label');

    if (type === 'daily') {
        label.innerText = '매일 백업할 시각 (HH:MM)';
        intervalInput.classList.add('hidden');
        dailyInput.classList.remove('hidden');
    } else {
        label.innerText = '반복 간격 (시간 단위)';
        dailyInput.classList.add('hidden');
        intervalInput.classList.remove('hidden');
    }
}

function openCreateProfileModal() {
    document.getElementById('profile-modal-title').innerText = '새 백업 프로필 생성';
    document.getElementById('prof-id').value = '';
    document.getElementById('prof-name').value = '새 백업 프로필';
    document.getElementById('prof-sources').value = '';
    document.getElementById('prof-repo').value = 'D:\\MyBackup_Repository';
    document.getElementById('prof-excludes').value = '*.tmp, *.log, .git, __pycache__, node_modules';
    document.getElementById('prof-schedule-type').value = 'interval_hours';
    document.getElementById('prof-interval').value = '24';
    document.getElementById('prof-daily-time').value = '03:00';
    document.getElementById('prof-auto-enable').checked = true;
    document.getElementById('prof-retention').value = '30';
    document.getElementById('prof-compression').value = '3';
    onScheduleTypeChange();
    openModal('profile-modal');
}

function editProfile(profileId) {
    const prof = state.profiles.find(p => p.id === profileId);
    if (!prof) return;

    document.getElementById('profile-modal-title').innerText = '백업 프로필 수정';
    document.getElementById('prof-id').value = prof.id;
    document.getElementById('prof-name').value = prof.name;
    document.getElementById('prof-sources').value = (prof.sources || []).join('\n');
    document.getElementById('prof-repo').value = prof.repo_dir || 'D:\\MyBackup_Repository';
    document.getElementById('prof-excludes').value = (prof.exclude_patterns || []).join(', ');
    
    const schedType = prof.schedule_type || 'interval_hours';
    document.getElementById('prof-schedule-type').value = schedType;
    if (schedType === 'daily') {
        document.getElementById('prof-daily-time').value = prof.schedule_value || '03:00';
    } else {
        document.getElementById('prof-interval').value = prof.schedule_value || '24';
    }
    document.getElementById('prof-auto-enable').checked = prof.auto_backup_enabled !== false;
    document.getElementById('prof-retention').value = prof.retention_count || 30;
    document.getElementById('prof-compression').value = prof.compression_level || 6;
    onScheduleTypeChange();
    openModal('profile-modal');
}

async function saveProfileFromModal() {
    const id = document.getElementById('prof-id').value.trim();
    const name = document.getElementById('prof-name').value.trim();
    const sourcesStr = document.getElementById('prof-sources').value.trim();
    const repo = document.getElementById('prof-repo').value.trim();
    const excludesStr = document.getElementById('prof-excludes').value.trim();
    const schedType = document.getElementById('prof-schedule-type').value;
    const schedValue = schedType === 'daily' 
        ? document.getElementById('prof-daily-time').value.trim() || '03:00' 
        : document.getElementById('prof-interval').value.trim() || '24';
    const autoEnable = document.getElementById('prof-auto-enable').checked;
    const retention = parseInt(document.getElementById('prof-retention').value.trim(), 10) || 30;
    const compression = parseInt(document.getElementById('prof-compression').value.trim(), 10) || 6;

    if (!name || !sourcesStr || !repo) {
        alert('프로필 이름, 백업 대상 경로, 저장소 경로를 모두 입력하세요.');
        return;
    }

    const sources = sourcesStr.split('\n').map(s => s.trim()).filter(s => s.length > 0);
    const excludes = excludesStr.split(',').map(s => s.trim()).filter(s => s.length > 0);

    const profileData = {
        id: id || undefined,
        name: name,
        sources: sources,
        repo_dir: repo,
        exclude_patterns: excludes,
        schedule_type: schedType,
        schedule_value: schedValue,
        auto_backup_enabled: autoEnable,
        retention_count: retention,
        compression_level: compression
    };

    try {
        await fetchAPI('/api/profiles', {
            method: 'POST',
            body: JSON.stringify(profileData)
        });
        closeModal('profile-modal');
        await loadProfiles();
        await loadDashboard();
    } catch (e) {
        alert('프로필 저장 실패: ' + e.message);
    }
}

async function triggerProfileBackup(profileId) {
    switchTab('runner');
    try {
        await fetchAPI('/api/backup/run', {
            method: 'POST',
            body: JSON.stringify({ profile_id: profileId })
        });
    } catch (e) {
        alert('백업 실행 실패: ' + e.message);
    }
}

async function deleteProfile(profileId, profileName) {
    if (!confirm(`'${profileName || profileId}' 백업 프로필을 정말 삭제하시겠습니까?`)) return;

    try {
        await fetchAPI(`/api/profiles/${profileId}`, { method: 'DELETE' });
        await loadProfiles();
        await loadDashboard();
    } catch (e) {
        alert('프로필 삭제 실패: ' + e.message);
    }
}

// Windows Task Scheduler Smart On/Off Handlers
async function loadWindowsTaskStatus() {
    try {
        const res = await fetchAPI('/api/windows-task/status');
        const badge = document.getElementById('windows-task-status-badge');
        const nextEl = document.getElementById('windows-task-next-run');
        if (!badge) return;

        if (res.registered) {
            badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-emerald-950/80 text-emerald-300 border border-emerald-700/60 flex items-center gap-1';
            badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> Windows OS 등록됨 (스마트 On/Off 활성)';
            if (nextEl) nextEl.innerText = `다음 자동 실행 예정: ${res.next_run || '-'}`;
        } else {
            badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
            badge.innerText = '미등록 (상시 데몬 모드)';
            if (nextEl) nextEl.innerText = '';
        }
    } catch (e) {
        console.error('Failed to load windows task status:', e);
    }
}

async function registerWindowsTaskFromUI() {
    const profiles = state.profiles || [];
    const activeProf = profiles.find(p => p.auto_backup_enabled) || (profiles.length > 0 ? profiles[0] : null);
    const profId = activeProf ? activeProf.id : 'prof_custom_selected';
    const schedType = activeProf ? activeProf.schedule_type || 'daily' : 'daily';
    const schedVal = activeProf ? activeProf.schedule_value || '03:00' : '03:00';

    try {
        const res = await fetchAPI('/api/windows-task/register', {
            method: 'POST',
            body: JSON.stringify({
                profile_id: profId,
                schedule_type: schedType,
                schedule_value: schedVal
            })
        });
        if (res.success) {
            alert(`🎉 Windows OS 작업 스케줄러에 등록 완료!\n\n- 스케줄: ${schedType === 'daily' ? '매일 ' + schedVal : schedVal + '시간 주기'}\n\n이제 파이썬 대시보드를 완전히 종료해 두셔도, 지정된 시간에 윈도우 OS가 백그라운드에서 1초 켜서 백업 후 즉시 자동 종료합니다.`);
            await loadWindowsTaskStatus();
        } else {
            alert('등록 실패: ' + res.error);
        }
    } catch (e) {
        alert('스케줄러 등록 오류: ' + e.message);
    }
}

async function unregisterWindowsTaskFromUI() {
    if (!confirm('Windows 작업 스케줄러 등록을 해제하시겠습니까?')) return;
    try {
        const res = await fetchAPI('/api/windows-task/unregister', { method: 'POST' });
        alert(res.message || '등록 해제되었습니다.');
        await loadWindowsTaskStatus();
    } catch (e) {
        alert('해제 오류: ' + e.message);
    }
}

// Maintenance
async function runGarbageCollection() {
    if (!confirm('저장소에서 참조되지 않는 고아 블롭을 정리하고 디스크 공간을 회수하시겠습니까?')) return;
    try {
        const res = await fetchAPI('/api/maintenance/prune', { method: 'POST' });
        alert(`🧹 정리 완료!\n삭제된 고아 블롭: ${res.deleted_blobs}개\n회수된 용량: ${formatBytes(res.freed_bytes)}`);
        await loadDashboard();
    } catch (e) {
        alert('정리 실패: ' + e.message);
    }
}

// Directory Browser Modal
function openDirectoryPicker(targetInputId) {
    state.browseModalCallback = (selectedPath) => {
        const input = document.getElementById(targetInputId);
        if (input) {
            if (input.tagName === 'TEXTAREA') {
                input.value = input.value ? `${input.value}\n${selectedPath}` : selectedPath;
            } else {
                input.value = selectedPath;
            }
        }
    };
    browseToDirectory("");
    openModal('browse-modal');
}

async function browseToDirectory(path) {
    try {
        const res = await fetchAPI('/api/browse-dir', {
            method: 'POST',
            body: JSON.stringify({ current_path: path })
        });

        state.currentBrowsePath = res.current_path;
        document.getElementById('browse-current-path').innerText = res.current_path || "시스템 드라이브 선택";

        const listContainer = document.getElementById('browse-items-list');
        let html = '';

        if (res.parent_path !== null && res.parent_path !== undefined) {
            html += `
                <div onclick="browseToDirectory('${res.parent_path.replace(/\\/g, '\\\\')}')" class="flex items-center gap-2 p-2 hover:bg-slate-800 rounded-lg cursor-pointer text-xs text-amber-400 font-semibold">
                    <i data-lucide="corner-left-up" class="w-4 h-4"></i> [상위 폴더로 이동]
                </div>
            `;
        }

        html += res.items.map(item => `
            <div class="flex items-center justify-between p-2 hover:bg-slate-800 rounded-lg text-xs transition">
                <div onclick="browseToDirectory('${item.path.replace(/\\/g, '\\\\')}')" class="flex items-center gap-2 cursor-pointer flex-1 overflow-hidden">
                    <i data-lucide="${item.name.includes(':') ? 'hard-drive' : 'folder'}" class="w-4 h-4 text-amber-400 shrink-0"></i>
                    <span class="truncate text-slate-200">${item.name}</span>
                </div>
                <button onclick="selectBrowsePath('${item.path.replace(/\\/g, '\\\\')}')" class="px-2.5 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded text-[11px] font-medium shrink-0 ml-2">
                    선택
                </button>
            </div>
        `).join('');

        listContainer.innerHTML = html;
        lucide.createIcons();
    } catch (e) {
        console.error('Browse error:', e);
    }
}

function selectBrowsePath(path) {
    if (state.browseModalCallback) {
        state.browseModalCallback(path);
    }
    closeModal('browse-modal');
}

function selectCurrentBrowsePath() {
    if (state.currentBrowsePath && state.browseModalCallback) {
        state.browseModalCallback(state.currentBrowsePath);
    }
    closeModal('browse-modal');
}

// Custom Selection Tab Logic
async function loadCustomSelectionData() {
    try {
        const containerApps = document.getElementById('installed-apps-container');
        if (containerApps) containerApps.innerHTML = `<div class="text-slate-400 text-center py-6 flex items-center justify-center gap-2"><i data-lucide="loader-2" class="w-4 h-4 animate-spin text-blue-400"></i> 설치된 프로그램 목록을 분석 중입니다...</div>`;
        lucide.createIcons();

        const [apps, projects] = await Promise.all([
            fetchAPI('/api/apps/installed'),
            fetchAPI('/api/projects/list')
        ]);
        
        // Add index to each item
        state.installedApps = (apps || []).map((a, idx) => ({ ...a, idx }));
        state.aiProjects = (projects || []).map((p, idx) => ({ ...p, idx }));

        // Restore previously saved selection from local memory/profile
        restoreCustomSelectionState();

        renderInstalledApps();
        renderAiProjects();
        renderCustomFoldersList();
        updateCustomSummary();
    } catch (e) {
        console.error('Failed to load custom selection data:', e);
        const containerApps = document.getElementById('installed-apps-container');
        if (containerApps) containerApps.innerHTML = `<div class="text-red-400 text-center py-6">프로그램 목록을 불러오지 못했습니다: ${e.message}</div>`;
    }
}

function renderInstalledApps(filterText = '') {
    const container = document.getElementById('installed-apps-container');
    if (!container) return;

    const q = filterText.toLowerCase().trim();
    const filtered = state.installedApps.filter(app => {
        return app.name.toLowerCase().includes(q) || (app.publisher && app.publisher.toLowerCase().includes(q));
    });

    if (filtered.length === 0) {
        container.innerHTML = `<div class="text-slate-500 text-center py-6">일치하는 설치 프로그램이 없습니다.</div>`;
        return;
    }

    container.innerHTML = filtered.map(app => {
        const loc = app.location || '';
        const isChecked = loc && state.selectedAppPaths.has(loc);
        const hasLoc = app.has_location;

        return `
            <div class="p-3 bg-slate-900/70 hover:bg-slate-800/90 rounded-xl border border-slate-800/90 flex items-center justify-between transition cursor-pointer ${isChecked ? 'border-blue-500/60 bg-blue-950/20' : ''}" onclick="toggleAppSelectionByIndex(${app.idx})">
                <div class="flex items-center gap-3 overflow-hidden flex-1">
                    <input type="checkbox" ${isChecked ? 'checked' : ''} ${!hasLoc ? 'disabled' : ''} onclick="event.stopPropagation(); toggleAppSelectionByIndex(${app.idx})" class="rounded border-slate-700 bg-slate-800 text-blue-600 focus:ring-0 w-4 h-4 shrink-0 cursor-pointer">
                    <div class="overflow-hidden flex-1">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-bold text-slate-100 text-xs">${app.name}</span>
                            ${app.publisher ? `<span class="text-[10px] px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 font-medium">${app.publisher}</span>` : ''}
                            ${app.version ? `<span class="text-[10px] text-slate-500 font-mono">v${app.version}</span>` : ''}
                        </div>
                        <div class="text-[11px] text-slate-400 truncate mt-1 font-mono">
                            ${hasLoc ? `<span class="text-emerald-400/90 flex items-center gap-1"><i data-lucide="check" class="w-3 h-3 inline"></i> ${loc}</span>` : '<span class="text-slate-500">(기본 설치 폴더 자동 지정)</span>'}
                        </div>
                    </div>
                </div>
                <div class="shrink-0 ml-3">
                    <span class="text-[11px] px-2.5 py-1 rounded-lg font-medium ${isChecked ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'}">
                        ${isChecked ? '선택됨' : '선택'}
                    </span>
                </div>
            </div>
        `;
    }).join('');

    lucide.createIcons();
}

function filterAppsList() {
    const input = document.getElementById('app-search-input');
    renderInstalledApps(input ? input.value : '');
}

// Auto-Save and Restore Selection State (Persistent Memory)
function saveCustomSelectionState() {
    try {
        const config = {
            includeDrivers: document.getElementById('custom-include-drivers')?.checked ?? true,
            selectedApps: Array.from(state.selectedAppPaths),
            selectedProjects: Array.from(state.selectedProjectPaths),
            customFolders: state.customFolders || [],
            repoDir: document.getElementById('custom-repo-dir')?.value || 'D:\\MyBackup_Repository',
            profileName: document.getElementById('custom-profile-name')?.value || '내 맞춤형 선택 백업'
        };
        localStorage.setItem('my_backup_custom_config', JSON.stringify(config));
    } catch (e) {
        console.error('Failed to save custom selection state:', e);
    }
}

function restoreCustomSelectionState() {
    try {
        let config = null;
        const savedStr = localStorage.getItem('my_backup_custom_config');
        if (savedStr) {
            config = JSON.parse(savedStr);
        } else {
            // Fallback to backend profile if first time
            const prof = (state.profiles || []).find(p => p.id === 'prof_custom_selected' || p.id === 'prof_main_custom');
            if (prof && prof.sources) {
                config = {
                    includeDrivers: true,
                    selectedApps: [],
                    selectedProjects: [],
                    customFolders: [],
                    repoDir: prof.repo_dir || 'D:\\MyBackup_Repository',
                    profileName: prof.name || '내 맞춤형 선택 백업'
                };
                prof.sources.forEach(s => {
                    if (s.includes('Windows_Drivers')) config.includeDrivers = true;
                    else if (s.includes('Desktop\\ai\\')) config.selectedProjects.push(s);
                    else if (s.includes('Program Files') || s.includes('AppData')) config.selectedApps.push(s);
                    else config.customFolders.push(s);
                });
            }
        }

        if (config) {
            if (config.selectedApps) state.selectedAppPaths = new Set(config.selectedApps);
            if (config.selectedProjects) state.selectedProjectPaths = new Set(config.selectedProjects);
            if (config.customFolders) state.customFolders = config.customFolders;

            const driverCheckbox = document.getElementById('custom-include-drivers');
            if (driverCheckbox && config.includeDrivers !== undefined) {
                driverCheckbox.checked = config.includeDrivers;
            }

            const repoInput = document.getElementById('custom-repo-dir');
            if (repoInput && config.repoDir) repoInput.value = config.repoDir;

            const profInput = document.getElementById('custom-profile-name');
            if (profInput && config.profileName) profInput.value = config.profileName;
        }
    } catch (e) {
        console.error('Failed to restore custom selection state:', e);
    }
}

function toggleAppSelectionByIndex(idx) {
    const app = state.installedApps.find(a => a.idx === idx);
    if (!app || !app.location) return;
    const path = app.location;
    if (state.selectedAppPaths.has(path)) {
        state.selectedAppPaths.delete(path);
    } else {
        state.selectedAppPaths.add(path);
    }
    const input = document.getElementById('app-search-input');
    renderInstalledApps(input ? input.value : '');
    updateCustomSummary();
    saveCustomSelectionState();
}

function toggleAllApps(select) {
    if (select) {
        state.installedApps.forEach(a => {
            if (a.has_location && a.location) state.selectedAppPaths.add(a.location);
        });
    } else {
        state.selectedAppPaths.clear();
    }
    filterAppsList();
    updateCustomSummary();
    saveCustomSelectionState();
}

function renderAiProjects() {
    const container = document.getElementById('ai-projects-container');
    if (!container) return;

    if (!state.aiProjects || state.aiProjects.length === 0) {
        container.innerHTML = `<div class="text-slate-500 text-center py-4 col-span-2">프로젝트 폴더가 없습니다.</div>`;
        return;
    }

    container.innerHTML = state.aiProjects.map(proj => {
        const isChecked = state.selectedProjectPaths.has(proj.path);
        return `
            <div class="p-2.5 bg-slate-900/60 hover:bg-slate-800/80 rounded-xl border border-slate-800 flex items-center justify-between transition cursor-pointer" onclick="toggleProjectSelectionByIndex(${proj.idx})">
                <div class="flex items-center gap-2.5 overflow-hidden">
                    <input type="checkbox" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation(); toggleProjectSelectionByIndex(${proj.idx})" class="rounded border-slate-700 bg-slate-800 text-blue-600 focus:ring-0 w-4 h-4 shrink-0 cursor-pointer">
                    <div class="overflow-hidden">
                        <span class="font-semibold text-slate-200 truncate text-xs block">${proj.name}</span>
                        <span class="text-[10px] text-slate-500">${proj.item_count || 0}개 항목</span>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function toggleProjectSelectionByIndex(idx) {
    const proj = state.aiProjects.find(p => p.idx === idx);
    if (!proj) return;
    const path = proj.path;
    if (state.selectedProjectPaths.has(path)) {
        state.selectedProjectPaths.delete(path);
    } else {
        state.selectedProjectPaths.add(path);
    }
    renderAiProjects();
    updateCustomSummary();
    saveCustomSelectionState();
}

function toggleAllProjects(select) {
    if (select) {
        state.aiProjects.forEach(p => state.selectedProjectPaths.add(p.path));
    } else {
        state.selectedProjectPaths.clear();
    }
    renderAiProjects();
    updateCustomSummary();
    saveCustomSelectionState();
}

function openDirectoryPickerForCustom() {
    state.browseModalCallback = (selectedPath) => {
        if (selectedPath && !state.customFolders.includes(selectedPath)) {
            state.customFolders.push(selectedPath);
            renderCustomFoldersList();
            updateCustomSummary();
            saveCustomSelectionState();
        }
    };
    browseToDirectory("");
    openModal('browse-modal');
}

function removeCustomFolder(idx) {
    state.customFolders.splice(idx, 1);
    renderCustomFoldersList();
    updateCustomSummary();
    saveCustomSelectionState();
}

function renderCustomFoldersList() {
    const container = document.getElementById('custom-folders-list');
    if (!container) return;

    if (state.customFolders.length === 0) {
        container.innerHTML = `<div class="text-slate-500 text-xs py-1">추가된 별도 폴더가 없습니다.</div>`;
        return;
    }

    container.innerHTML = state.customFolders.map((f, idx) => `
        <div class="p-2.5 bg-slate-900/80 rounded-xl border border-slate-800 flex items-center justify-between text-xs">
            <div class="flex items-center gap-2 overflow-hidden">
                <i data-lucide="folder" class="w-4 h-4 text-purple-400 shrink-0"></i>
                <span class="truncate font-mono text-slate-300 text-[11px]">${f}</span>
            </div>
            <button onclick="removeCustomFolder(${idx})" class="p-1 hover:bg-slate-800 text-slate-400 hover:text-red-400 rounded-lg">
                <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
            </button>
        </div>
    `).join('');

    lucide.createIcons();
}

function updateCustomSummary() {
    const includeDrivers = document.getElementById('custom-include-drivers')?.checked ?? true;
    const badgeDrivers = document.getElementById('summary-drivers-badge');
    if (badgeDrivers) {
        badgeDrivers.innerText = includeDrivers ? '포함 (43개)' : '미포함';
        badgeDrivers.className = includeDrivers ? 'text-emerald-400 font-medium' : 'text-slate-500 font-medium';
    }

    const appsCountEl = document.getElementById('summary-apps-count');
    if (appsCountEl) appsCountEl.innerText = `${state.selectedAppPaths.size}개`;

    const projCountEl = document.getElementById('summary-projects-count');
    if (projCountEl) projCountEl.innerText = `${state.selectedProjectPaths.size}개`;

    const customCountEl = document.getElementById('summary-custom-count');
    if (customCountEl) customCountEl.innerText = `${state.customFolders.length}개`;
}

// Watch input changes
document.addEventListener('change', (e) => {
    if (e.target && (e.target.id === 'custom-include-drivers' || e.target.id === 'custom-repo-dir' || e.target.id === 'custom-profile-name')) {
        updateCustomSummary();
        saveCustomSelectionState();
    }
});

async function startCustomSelectionBackup() {
    const includeDrivers = document.getElementById('custom-include-drivers')?.checked ?? true;
    const selectedApps = Array.from(state.selectedAppPaths);
    const selectedProjects = Array.from(state.selectedProjectPaths);
    const customFolders = state.customFolders;
    const repoDir = document.getElementById('custom-repo-dir').value.trim() || 'D:\\MyBackup_Repository';
    const profileName = document.getElementById('custom-profile-name').value.trim() || '내 맞춤형 선택 백업';
    const saveProfile = document.getElementById('custom-save-profile')?.checked ?? true;

    if (!includeDrivers && selectedApps.length === 0 && selectedProjects.length === 0 && customFolders.length === 0) {
        alert('백업할 항목을 최소 1개 이상 선택해 주세요.');
        return;
    }

    saveCustomSelectionState();
    switchTab('runner');

    try {
        await fetchAPI('/api/backup/custom-selection', {
            method: 'POST',
            body: JSON.stringify({
                include_drivers: includeDrivers,
                selected_projects: selectedProjects,
                selected_apps: selectedApps,
                custom_folders: customFolders,
                repo_dir: repoDir,
                profile_name: profileName,
                save_as_profile: saveProfile
            })
        });
    } catch (e) {
        alert('선택 백업 시작 실패: ' + e.message);
    }
}

// --- Windows System Image (Bare-Metal) Logic ---
let sysImageLogTimer = null;

async function loadSystemImageStatus() {
    const driveSelect = document.getElementById('sysimg-drive-select');
    const targetDrive = driveSelect ? driveSelect.value : 'D:';

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
            if (data.backup_info.exists) {
                lastStatus.innerText = `${data.backup_info.size_gb} GB`;
                lastStatus.className = 'text-2xl font-black text-emerald-400 font-mono';
                lastTime.innerText = `최근 백업: ${data.backup_info.last_modified || '-'}`;
            } else {
                lastStatus.innerText = '미생성';
                lastStatus.className = 'text-2xl font-black text-slate-100 font-mono';
                lastTime.innerText = `${data.backup_info.path} 에 이미지 없음`;
            }
        }

        // Running state
        updateSystemImageRunningUI(data.is_running);

        if (data.is_running) {
            startPollingSystemImageLogs();
        }
    } catch (e) {
        console.error('Failed to load system image status:', e);
    }
}

function updateSystemImageRunningUI(isRunning) {
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
            badge.innerText = '대기 중';
            badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
        }
    }
    lucide.createIcons();
}

async function startSystemImageBackup() {
    const driveSelect = document.getElementById('sysimg-drive-select');
    const targetDrive = driveSelect ? driveSelect.value : 'D:';

    const msg = `윈도우 베어메탈(전체) 시스템 이미지 백업을 시작하시겠습니까?\n\n` +
        `• 대상: C: 전체 (부팅 EFI 파티션, 복구 파티션, OS 전체)\n` +
        `• 저장 위치: ${targetDrive}\\WindowsImageBackup\n\n` +
        `※ 시스템 권한을 위해 윈도우 관리자(UAC) 확인 창이 뜨면 '예'를 눌러주세요.`;

    if (!confirm(msg)) return;

    updateSystemImageRunningUI(true);

    const consoleBox = document.getElementById('sysimg-console');
    if (consoleBox) {
        consoleBox.innerHTML = '<div class="text-blue-400">[시작] 윈도우 wbadmin 시스템 이미지 백업 명령을 호출합니다...</div>';
    }

    try {
        const res = await fetchAPI('/api/system-image/start', {
            method: 'POST',
            body: JSON.stringify({ target_drive: targetDrive })
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

            const consoleBox = document.getElementById('sysimg-console');
            if (consoleBox && data.logs) {
                consoleBox.innerHTML = data.logs.map(line => {
                    let color = 'text-slate-300';
                    if (line.includes('[SUCCESS]') || line.includes('성공적으로 완료')) color = 'text-emerald-400 font-bold';
                    if (line.includes('[ERROR]') || line.includes('오류') || line.includes('실패')) color = 'text-red-400 font-bold';
                    if (line.includes('[WARNING]')) color = 'text-amber-400';
                    return `<div class="${color}">${line}</div>`;
                }).join('');
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }

            if (!data.is_running) {
                clearInterval(sysImageLogTimer);
                sysImageLogTimer = null;
                updateSystemImageRunningUI(false);
                loadSystemImageStatus();
            }
        } catch (e) {
            console.error('Error polling system image logs:', e);
        }
    }, 2000);
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

async function verifySnapshot(snapshotId) {
    if (!confirm(`스냅샷 [${snapshotId}]의 zstandard 압축 블롭 및 SHA-256 해시 무결성을 정밀 검증하시겠습니까?`)) return;
    try {
        const res = await fetchAPI('/api/verify/run', {
            method: 'POST',
            body: JSON.stringify({ snapshot_id: snapshotId })
        });
        if (res.success) {
            alert(`✅ 무결성 검증 완료: 100% 정상!\n- 검사된 블롭: ${res.verified_count}개\n- 오류: 0개\n모든 백업 데이터가 완벽하게 보존되어 있습니다.`);
        } else {
            alert(`⚠️ 무결성 검증 실패: 손상 블롭 ${res.error_count}개 감지됨!`);
        }
        await loadSnapshots();
    } catch (e) {
        alert('무결성 검증 중 오류 발생: ' + e.message);
    }
}

// Modal Helpers
function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('hidden');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('hidden');
}

async function shutdownServer() {
    if (!confirm('백업 대시보드 및 백그라운드 서비스를 완전히 종료하시겠습니까?\n(종료 후에는 start_backup_system.bat으로 언제든 다시 켤 수 있습니다)')) return;

    try {
        await fetchAPI('/api/system/shutdown', { method: 'POST' });
        alert('백업 서비스가 완전히 종료되었습니다. 이 브라우저 창을 닫으셔도 됩니다.');
        window.close();
    } catch (e) {
        alert('종료 완료: 브라우저 창을 닫아주세요.');
    }
}

// Init
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    loadDashboard();

    const driveSelect = document.getElementById('sysimg-drive-select');
    if (driveSelect) {
        driveSelect.addEventListener('change', () => {
            loadSystemImageStatus();
        });
    }

    // Start polling every 1 second
    state.pollInterval = setInterval(() => {
        pollTaskStatus();
    }, 1000);
});
