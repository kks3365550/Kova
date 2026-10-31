// =========================================================================
// [백업시스템] 사용자 커스텀 선택 백업 (설치 프로그램, AI 프로젝트, 커스텀 폴더)
// =========================================================================

async function loadCustomSelectionData(retryCount = 0) {
    try {
        const containerApps = document.getElementById('installed-apps-container');
        if (containerApps && !containerApps.querySelector('.app-item')) {
            containerApps.innerHTML = `<div class="text-zinc-400 text-center py-6 flex items-center justify-center gap-2"><i data-lucide="loader-2" class="w-4 h-4 animate-spin text-blue-400"></i> 설치된 프로그램 목록을 분석 중입니다...</div>`;
            lucide.createIcons();
        }

        const apps = await fetchAPI('/api/apps/installed');
        
        // Add index to each item
        state.installedApps = (apps || []).map((a, idx) => ({ ...a, idx }));

        // Restore previously saved selection from local memory/profile
        restoreCustomSelectionState();

        renderInstalledApps();
        renderCustomFoldersList();
        updateCustomSummary();
    } catch (e) {
        console.error('Failed to load custom selection data:', e);
        if (retryCount < 1) {
            setTimeout(() => loadCustomSelectionData(retryCount + 1), 800);
            return;
        }
        const containerApps = document.getElementById('installed-apps-container');
        if (containerApps) containerApps.innerHTML = `
            <div class="text-center py-6 space-y-2">
                <div class="text-red-400 text-xs">일시적인 통신 지연 (${friendlyError(e)})</div>
                <button onclick="loadCustomSelectionData()" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-lg text-xs font-medium border border-zinc-700 transition">
                    목록 다시 불러오기
                </button>
            </div>
        `;
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
        const hasLoc = app.has_location && loc;

        return `
            <div class="p-3 bg-slate-900/70 hover:bg-slate-800/90 rounded-xl border border-slate-800/90 flex items-center justify-between transition cursor-pointer ${isChecked ? 'border-blue-500/60 bg-blue-950/20' : ''}" onclick="toggleAppSelectionByIndex(${app.idx})">
                <div class="flex items-center gap-3 overflow-hidden flex-1">
                    <input type="checkbox" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation(); toggleAppSelectionByIndex(${app.idx})" class="rounded border-slate-700 bg-slate-800 text-blue-600 focus:ring-0 w-4 h-4 shrink-0 cursor-pointer">
                    <div class="overflow-hidden flex-1">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-bold text-slate-100 text-xs">${app.name}</span>
                            ${app.publisher ? `<span class="text-[10px] px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 font-medium">${app.publisher}</span>` : ''}
                            ${app.version ? `<span class="text-[10px] text-slate-500 font-mono">v${app.version}</span>` : ''}
                        </div>
                        <div class="text-[11px] text-slate-400 truncate mt-1 font-mono">
                            ${hasLoc 
                                ? `<span class="text-emerald-400/90 flex items-center gap-1"><i data-lucide="check" class="w-3 h-3 inline"></i> ${loc}</span>` 
                                : `<span class="text-amber-400/90 hover:text-amber-300 flex items-center gap-1" onclick="event.stopPropagation(); openDirectoryPickerForApp(${app.idx})"><i data-lucide="folder-search" class="w-3 h-3 inline"></i> 폴더 위치 수동 지정하기</span>`
                            }
                        </div>
                    </div>
                </div>
                <div class="shrink-0 ml-3">
                    <span class="text-[11px] px-2.5 py-1 rounded-lg font-medium transition ${isChecked ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}">
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
                    customFolders: [],
                    repoDir: prof.repo_dir || 'D:\\MyBackup_Repository',
                    profileName: prof.name || '내 맞춤형 선택 백업'
                };
                prof.sources.forEach(s => {
                    if (s.includes('Windows_Drivers')) config.includeDrivers = true;
                    else if (s.includes('Program Files') || s.includes('AppData')) config.selectedApps.push(s);
                    else config.customFolders.push(s);
                });
            }
        }

        if (config) {
            if (config.selectedApps) state.selectedAppPaths = new Set(config.selectedApps);
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

function openDirectoryPickerForApp(idx) {
    const app = state.installedApps.find(a => a.idx === idx);
    if (!app) return;
    state.browseModalCallback = (selectedPath) => {
        if (selectedPath) {
            app.location = selectedPath;
            app.has_location = true;
            state.selectedAppPaths.add(selectedPath);
            filterAppsList();
            updateCustomSummary();
            saveCustomSelectionState();
        }
    };
    browseToDirectory("C:\\Program Files");
    openModal('browse-modal');
}

function toggleAppSelectionByIndex(idx) {
    const app = state.installedApps.find(a => a.idx === idx);
    if (!app) return;
    
    // If location is missing, open folder picker so user can pick it easily
    if (!app.location) {
        openDirectoryPickerForApp(idx);
        return;
    }

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
    const customFolders = state.customFolders;
    const repoDir = document.getElementById('custom-repo-dir').value.trim() || 'D:\\MyBackup_Repository';
    const profileName = document.getElementById('custom-profile-name').value.trim() || '내 맞춤형 선택 백업';
    const saveProfile = document.getElementById('custom-save-profile')?.checked ?? true;

    if (!includeDrivers && selectedApps.length === 0 && customFolders.length === 0) {
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
                selected_apps: selectedApps,
                custom_folders: customFolders,
                repo_dir: repoDir,
                profile_name: profileName,
                save_as_profile: saveProfile
            })
        });
    } catch (e) {
        alert('선택 백업 시작 실패: ' + friendlyError(e));
    }
}

// --- Pre-flight Space Estimator ---
async function estimateCustomBackupSpace(btnEl) {
    const selectedApps = Array.from(state.selectedAppPaths || []);
    const customFolders = state.customFolders || [];
    const allSources = [...selectedApps, ...customFolders];

    const repoDir = document.getElementById('custom-repo-dir') ? document.getElementById('custom-repo-dir').value.trim() : "";
    const resultBox = document.getElementById('estimate-result-box');

    if (!allSources.length) {
        alert('용량을 예측할 백업 대상(프로그램, 프로젝트, 폴더)을 최소 1개 이상 선택해 주세요.');
        return;
    }

    if (btnEl) {
        btnEl.disabled = true;
        btnEl.innerHTML = `<span class="text-cyan-400">⏳ 대상 폴더 용량 고속 스캔 중...</span>`;
    }

    try {
        const res = await fetchAPI('/api/backup/estimate', {
            method: 'POST',
            body: JSON.stringify({
                sources: allSources,
                repo_dir: repoDir || null
            })
        });

        if (resultBox && res) {
            resultBox.classList.remove('hidden');
            const isOk = res.is_sufficient;
            resultBox.innerHTML = `
                <div class="flex items-center justify-between font-semibold">
                    <span class="text-slate-300">예상 원본 크기:</span>
                    <span class="text-cyan-400 font-mono">${formatBytes(res.total_bytes)} (${res.scanned_files.toLocaleString()}개 파일)</span>
                </div>
                <div class="flex items-center justify-between font-semibold">
                    <span class="text-slate-300">백업 보관함 여유 공간:</span>
                    <span class="font-mono ${isOk ? 'text-emerald-400' : 'text-red-400'}">${res.repo_free_gb} GB</span>
                </div>
                <div class="pt-1 mt-1 border-t border-slate-800 flex items-center justify-between font-bold">
                    <span class="text-slate-400">사전 안전 진단:</span>
                    <span class="${isOk ? 'text-emerald-400' : 'text-red-400'}">${isOk ? '✅ 백업 안전 (공간 충분)' : '⚠️ 백업 보관함 공간 부족 경고'}</span>
                </div>
            `;
        }
    } catch (err) {
        alert('용량 예측 오류: ' + friendlyError(err));
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.innerHTML = `<i data-lucide="scale" class="w-3.5 h-3.5 text-cyan-400"></i><span>📊 백업 전 용량 사전 예측 (Pre-flight Check)</span>`;
            if (window.lucide) window.lucide.createIcons();
        }
    }
}

