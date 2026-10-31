// 쉬운 오류 안내: 기술 용어 대신 "무슨 일이 생겼는지 + 할 일"로 보여 준다.
function friendlyError(e) {
    var m = String((e && e.message) || e || '');
    if (/Failed to fetch|NetworkError|Load failed|ECONN/i.test(m)) {
        return '백업 프로그램과 연결이 끊겼어요. 프로그램이 켜져 있는지 확인한 뒤 다시 시도해 주세요.';
    }
    if (/401|403|비밀번호|password/i.test(m)) {
        return '비밀번호가 맞지 않거나 권한이 없어요. 비밀번호를 다시 확인해 주세요.';
    }
    if (/timeout|timed out|시간 초과/i.test(m)) {
        return '시간이 너무 오래 걸려 멈췄어요. 잠시 후 다시 시도해 주세요.';
    }
    return '문제가 생겼어요. 잠시 후 다시 시도해 주세요. (문제가 계속되면 담당자에게 아래 내용을 알려 주세요: ' + m + ')';
}

// =========================================================================
// [백업시스템] 메인 오케스트레이터 & 대시보드 뷰 / 프로필 관리
// 서브 모듈: /static/js/backup_utils.js, backup_snapshots.js, backup_custom.js, backup_sysimage.js
// =========================================================================

// Tab Switching
function switchTab(tabId) {
    if (tabId === 'migration') {
        switchTab('system-image');
        return;
    }
    if (tabId === 'custom') {
        switchTab('settings');
        switchSettingsSection('targets');
        return;
    }
    if (tabId === 'profiles') {
        switchTab('settings');
        switchSettingsSection('schedule');
        return;
    }

    state.activeTab = tabId;

    // Show/hide runner tab button in header if runner is active
    const runnerNavBtn = document.getElementById('nav-tab-runner');
    if (runnerNavBtn) {
        if (tabId === 'runner') {
            runnerNavBtn.classList.remove('hidden');
        }
    }

    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabId) {
            btn.classList.add('bg-white/10', 'text-white', 'shadow-sm');
            btn.classList.remove('text-zinc-400', 'hover:text-zinc-200', 'hover:bg-white/5', 'bg-blue-600');
        } else {
            btn.classList.remove('bg-white/10', 'text-white', 'shadow-sm', 'bg-blue-600');
            btn.classList.add('text-zinc-400', 'hover:text-zinc-200', 'hover:bg-white/5');
        }
    });

    document.querySelectorAll('.tab-content').forEach(view => {
        view.classList.toggle('hidden', view.id !== `tab-${tabId}`);
    });

    if (tabId === 'snapshots') loadSnapshots();
    if (tabId === 'dashboard') loadDashboard();
    if (tabId === 'settings') {
        loadProfiles();
        if (typeof loadCustomSelectionData === 'function') loadCustomSelectionData();
        if (typeof loadOffsiteStatus === 'function') loadOffsiteStatus();
        if (typeof loadWindowsTaskStatus === 'function') loadWindowsTaskStatus();
    }
    if (tabId === 'system-image') {
        loadSystemImageStatus();
        loadVhdxList();
        if (typeof loadMigrationDrives === 'function') loadMigrationDrives();
        if (typeof loadArchiveList === 'function') loadArchiveList();
        if (typeof startArchivePolling === 'function') startArchivePolling();
    }
}

// Settings Sub-Section Switching (targets, schedule, cloud, security)
function switchSettingsSection(sectionId) {
    const validSections = ['targets', 'schedule', 'cloud', 'security'];
    const activeSec = validSections.includes(sectionId) ? sectionId : 'targets';

    validSections.forEach(sec => {
        const secEl = document.getElementById(`settings-sec-${sec}`);
        const secBtn = document.getElementById(`btn-settings-${sec}`);
        if (secEl) {
            secEl.classList.toggle('hidden', sec !== activeSec);
        }
        if (secBtn) {
            if (sec === activeSec) {
                secBtn.classList.add('bg-white/10', 'text-white', 'shadow-sm');
                secBtn.classList.remove('text-zinc-400', 'hover:text-zinc-200', 'hover:bg-white/5');
            } else {
                secBtn.classList.remove('bg-white/10', 'text-white', 'shadow-sm');
                secBtn.classList.add('text-zinc-400', 'hover:text-zinc-200', 'hover:bg-white/5');
            }
        }
    });

    if (window.lucide && typeof lucide.createIcons === 'function') {
        lucide.createIcons();
    }
}

// API Calls

async function loadDashboard() {
    try {
        const [sys, stats, profiles, snaps, offsite, storageHealth] = await Promise.all([
            fetchAPI('/api/system-info'),
            fetchAPI('/api/storage-stats'),
            fetchAPI('/api/profiles'),
            fetchAPI('/api/snapshots'),
            fetchAPI('/api/offsite').catch(() => null),
            fetchAPI('/api/system/storage-health').catch(() => null)
        ]);

        state.systemInfo = sys;
        state.storageStats = stats;
        state.profiles = profiles;
        state.snapshots = snaps;
        state.offsite = offsite;
        state.storageHealth = storageHealth;

        renderDashboardStats();
        renderRecentSnapshots();
        renderSystemDrives();
        renderStorageHealth();
        renderOffsiteStatusWidget();
        checkOnboardingWizard();
        if (typeof loadSystemImageStatus === 'function') {
            loadSystemImageStatus();
        }
        if (typeof loadOffsiteStatus === 'function') {
            await loadOffsiteStatus();
        }
    } catch (err) {
        console.error('Failed to load dashboard:', err);
    }
}

function renderOffsiteStatusWidget() {
    const off = state.offsite;
    const sidebarStatus = document.getElementById('sidebar-offsite-status');
    const dashBadge = document.getElementById('dashboard-offsite-badge');
    const dashMode = document.getElementById('dashboard-offsite-mode');
    const dashTarget = document.getElementById('dashboard-offsite-target');
    const dashState = document.getElementById('dashboard-offsite-sync-state');
    const heroScoreBadge = document.getElementById('hero-protection-score-badge');
    const heroLocalRepo = document.getElementById('hero-local-repo-name');
    const heroOffsite = document.getElementById('hero-offsite-state-name');

    // Update Local Repo Name in Hero
    if (heroLocalRepo) {
        const activeProf = (state.profiles || []).find(p => p.auto_backup_enabled) || (state.profiles || [])[0];
        const rPath = activeProf?.repo_dir || '';
        const parts = rPath ? rPath.split(/[\\/]/).filter(Boolean) : [];
        heroLocalRepo.innerText = parts[parts.length - 1] || '로컬 백업 보관함';
        heroLocalRepo.title = rPath || '로컬 백업 보관함';
    }

    // Node C: Topology Offsite Pipeline Binding & Fluent Dash Pill
    const topoBadge = document.getElementById('topology-offsite-badge');
    const topoTarget = document.getElementById('topology-offsite-target');
    const topoStatus = document.getElementById('topology-offsite-status');
    const dashProtPill = document.getElementById('dash-protection-pill');

    if (!off || !off.enabled) {
        const noSnaps = !state.snapshots || state.snapshots.length === 0;
        if (sidebarStatus) {
            sidebarStatus.innerText = '미사용';
            sidebarStatus.className = 'text-slate-500 font-mono font-medium';
        }
        if (dashBadge) {
            dashBadge.innerText = '비활성화';
            dashBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
        }
        if (dashMode) dashMode.innerText = '로컬 전용';
        if (dashTarget) dashTarget.innerText = '설정 안 됨';
        if (dashState) {
            dashState.innerText = '로컬 백업만 수행';
            dashState.className = 'text-slate-400 font-medium';
        }
        if (heroScoreBadge) {
            heroScoreBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span><span>' + (noSnaps ? '아직 백업이 없어요 (먼저 백업을 해 주세요)' : '이 PC에 잠금 보호 중 (다른 장소 보관은 미설정)') + '</span>';
            heroScoreBadge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-950/80 text-amber-300 border border-amber-800 flex items-center gap-1';
        }
        if (heroOffsite) {
            heroOffsite.innerText = '미사용 (로컬 전용)';
            heroOffsite.className = 'font-bold text-slate-400 text-xs';
        }
        if (topoBadge) {
            topoBadge.innerText = '미사용';
            topoBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
        }
        if (topoTarget) {
            topoTarget.innerText = '미설정 (로컬 전용)';
            topoTarget.title = '';
        }
        if (topoStatus) {
            topoStatus.innerText = '로컬 전용';
            topoStatus.className = 'text-slate-400 font-medium';
        }
        if (dashProtPill) {
            dashProtPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span><span>' + (noSnaps ? '아직 백업이 없어요' : '이 PC에 잠금 보호 중') + '</span>';
            dashProtPill.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-950 text-amber-300 border border-amber-800 flex items-center gap-1.5';
        }
        return;
    }

    let modeText = '원격';
    let targetText = '-';
    if (off.provider === 'gdrive') {
        modeText = '구글 드라이브 (Cloud)';
        targetText = `GDrive: ${off.gdrive_folder_id || 'BackupSystem_Offsite'}`;
    } else {
        modeText = off.replication_type === 'robocopy' ? 'Robocopy (LAN/NAS)' : (off.replication_type === 'rclone' ? 'Rclone (Cloud/S3)' : (off.replication_type || '원격'));
        targetText = off.remote_path || '-';
    }

    if (sidebarStatus) {
        sidebarStatus.innerText = '안전 보관 중';
        sidebarStatus.className = 'text-blue-400 font-mono font-medium';
    }
    if (dashBadge) {
        dashBadge.innerText = '안전 보관 켜짐';
        dashBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium bg-blue-900/60 text-blue-300 border border-blue-700';
    }
    if (dashMode) dashMode.innerText = modeText;
    if (dashTarget) {
        dashTarget.innerText = targetText;
        dashTarget.title = targetText;
    }
    
    const hasSnaps = (state.snapshots || []).some(s => s.is_offsite_protected || s.offsite_status === 'COMMITTED');
    if (dashState) {
        if (hasSnaps) {
            dashState.innerHTML = '<span class="text-blue-400 font-semibold">🌐 최신 백업 시점 동기화 완료</span>';
        } else {
            dashState.innerHTML = '<span class="text-amber-400 font-semibold">⏳ 백업 시점 복제 대기/진행 중</span>';
        }
    }
    if (heroScoreBadge) {
        heroScoreBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span><span>100% 안전하게 보관 중</span>';
        heroScoreBadge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950 text-emerald-300 border border-emerald-800 flex items-center gap-1';
    }
    if (heroOffsite) {
        const shortTarget = targetText.length > 20 ? targetText.substring(0, 18) + '...' : targetText;
        heroOffsite.innerText = `${shortTarget} (${hasSnaps ? '동기화 완료' : '대기'})`;
        heroOffsite.title = targetText;
        heroOffsite.className = 'font-bold text-indigo-300 text-xs';
    }

    if (topoBadge) {
        topoBadge.innerText = '다른 장소와 동기화';
        topoBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium bg-indigo-950 text-indigo-300 border border-indigo-700';
    }
    if (topoTarget) {
        topoTarget.innerText = targetText;
        topoTarget.title = targetText;
    }
    if (topoStatus) {
        if (hasSnaps) {
            topoStatus.innerText = '동기화 완료';
            topoStatus.className = 'text-emerald-400 font-medium';
        } else {
            topoStatus.innerText = '동기화 대기 중';
            topoStatus.className = 'text-amber-400 font-medium';
        }
    }
    if (dashProtPill) {
        dashProtPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span><span>안전 보관 정상</span>';
        dashProtPill.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-800 flex items-center gap-1.5';
    }
}

function openOnboardingWizard(force = false) {
    initOnboardingDrives();
    openModal('onboarding-modal');
    if (window.lucide && typeof lucide.createIcons === 'function') {
        lucide.createIcons();
    }
}

function initOnboardingDrives() {
    const drives = state.systemInfo?.drives || [];
    const chipsContainer = document.getElementById('onboard-drive-chips');
    const badgeEl = document.getElementById('onboard-drive-badge');
    const repoInput = document.getElementById('onboard-repo-dir');

    if (!drives.length) {
        if (badgeEl) badgeEl.innerText = '기본 경로';
        return;
    }

    let recommended = null;
    const nonCDrives = drives.filter(d => !d.device.toUpperCase().startsWith('C:'));
    if (nonCDrives.length > 0) {
        nonCDrives.sort((a, b) => b.free - a.free);
        recommended = nonCDrives[0];
    } else {
        recommended = drives[0];
    }

    const driveLetter = recommended.device.replace(/\\$/, '');
    const recPath = `${driveLetter}\\BackupRepository`;

    if (badgeEl) {
        badgeEl.innerText = `추천: ${driveLetter} (${formatBytes(recommended.free)} 여유)`;
    }
    if (repoInput && (!repoInput.value || repoInput.value.includes('MyBackup_Repository') || repoInput.value === 'D:\\BackupRepository')) {
        repoInput.value = recPath;
    }

    if (chipsContainer) {
        chipsContainer.innerHTML = drives.map(d => {
            const letter = d.device.replace(/\\$/, '');
            const targetPath = `${letter}\\BackupRepository`;
            const isRec = (d.device === recommended.device);
            return `
                <button type="button" onclick="document.getElementById('onboard-repo-dir').value='${targetPath}'" 
                    class="px-2.5 py-1 rounded-lg border text-[11px] font-mono flex items-center gap-1 transition ${
                        isRec 
                        ? 'bg-blue-950/80 border-blue-600 text-blue-300 font-bold hover:bg-blue-900/80' 
                        : 'bg-slate-900/80 border-slate-700 text-slate-300 hover:border-slate-500'
                    }">
                    <span>${letter}</span>
                    <span class="text-[10px] text-slate-400">(${formatBytes(d.free)})</span>
                    ${isRec ? '<span class="text-[9px] bg-blue-600 text-white px-1 rounded ml-0.5">추천</span>' : ''}
                </button>
            `;
        }).join('');
    }
}

function checkOnboardingWizard() {
    const done = localStorage.getItem('onboarding_completed');
    if (!done && (!state.profiles || state.profiles.length === 0)) {
        setTimeout(() => openOnboardingWizard(false), 600);
    }
}

async function completeOnboardingWizard(startImmediate = true) {
    try {
        const desktopChecked = document.getElementById('onboard-src-desktop')?.checked ?? true;
        const documentsChecked = document.getElementById('onboard-src-documents')?.checked ?? true;
        const picturesChecked = document.getElementById('onboard-src-pictures')?.checked ?? false;
        const driversChecked = document.getElementById('onboard-src-drivers')?.checked ?? true;
        const repoDir = document.getElementById('onboard-repo-dir')?.value?.trim() || 'D:\\BackupRepository';
        const sched = document.getElementById('onboard-schedule-time')?.value || '09:00';

        const sources = [];
        if (desktopChecked) sources.push('Desktop');
        if (documentsChecked) sources.push('Documents');
        if (picturesChecked) sources.push('Pictures');

        if (sources.length === 0 && !driversChecked) {
            sources.push('Documents');
        }

        const newProfile = {
            id: 'prof_default',
            name: "내 PC 기본 백업",
            sources: sources,
            include_drivers: driversChecked,
            repo_dir: repoDir,
            schedule_type: (sched === 'manual') ? 'manual' : 'daily',
            schedule_value: (sched === 'manual') ? '' : sched,
            auto_backup_enabled: (sched !== 'manual'),
            retention_count: 30,
            retention_days: 60,
            compression_level: 6,
            min_free_disk_gb: 10,
            enable_ransomware_protection: true
        };

        await fetchAPI('/api/profiles', {
            method: 'POST',
            body: JSON.stringify(newProfile)
        });

        localStorage.setItem('onboarding_completed', 'true');
        closeModal('onboarding-modal');
        await loadDashboard();

        if (startImmediate) {
            // Trigger first backup immediately with zero friction
            if (typeof triggerQuickBackup === 'function') {
                await triggerQuickBackup();
            }
        } else {
            alert('🎉 설정이 완료되었습니다!\n설정하신 일정에 맞춰 자동으로 백업이 실행됩니다.');
        }
    } catch (err) {
        alert('설정을 저장하지 못했어요: ' + friendlyError(err));
    }
}

function renderDashboardStats() {
    const stats = state.storageStats || {};
    const totalSnapsEl = document.getElementById('stat-total-snaps');
    const storedBytesEl = document.getElementById('stat-stored-bytes');
    const logicalBytesEl = document.getElementById('stat-logical-bytes');
    const dedupSavedEl = document.getElementById('stat-dedup-saved');

    if (totalSnapsEl) totalSnapsEl.innerText = stats.total_snapshots || '0';
    if (storedBytesEl) storedBytesEl.innerText = formatBytes(stats.stored_bytes);
    if (logicalBytesEl) logicalBytesEl.innerText = formatBytes(stats.logical_bytes);
    if (dedupSavedEl) dedupSavedEl.innerText = `${formatBytes(stats.dedup_saved_bytes)} (${stats.savings_percentage || 0}%)`;

    // Active profiles count
    const activeProfCount = (state.profiles || []).filter(p => p.auto_backup_enabled).length;
    const activeProfEl = document.getElementById('stat-active-profiles');
    if (activeProfEl) activeProfEl.innerText = `${activeProfCount} / ${(state.profiles || []).length}`;

    // Node A & B Topology Pipeline Binding & Metric Card 1
    const topoSrcLog = document.getElementById('topology-source-logical');
    const topoVaultStored = document.getElementById('topology-vault-stored');
    const topoVaultSavings = document.getElementById('topology-vault-savings');
    const dedupBadge = document.getElementById('stat-dedup-pct-badge');
    const dedupProgress = document.getElementById('stat-dedup-progress');

    if (topoSrcLog) topoSrcLog.innerText = formatBytes(stats.logical_bytes);
    if (topoVaultStored) topoVaultStored.innerText = formatBytes(stats.stored_bytes);
    if (topoVaultSavings) topoVaultSavings.innerText = `${stats.savings_percentage || 0}% 절감`;
    if (dedupBadge) dedupBadge.innerText = `${stats.savings_percentage || 0}% 절약`;
    const dedupBadge2 = document.getElementById('stat-dedup-pct-badge-2');
    if (dedupBadge2) dedupBadge2.innerText = `${stats.savings_percentage || 0}% 절약`;
    if (dedupProgress) dedupProgress.style.width = `${Math.min(100, Math.max(0, stats.savings_percentage || 0))}%`;

    // Track 2-15: Chunking Telemetry
    const badgeEl = document.getElementById('stat-chunk-files-badge');
    const chunksEl = document.getElementById('stat-total-chunks');
    const stratEl = document.getElementById('stat-chunk-strategies');
    if (badgeEl) badgeEl.innerText = `${(stats.chunked_files_count || 0).toLocaleString()}개 분할`;
    const badgeEl2 = document.getElementById('stat-chunk-files-badge-2');
    if (badgeEl2) badgeEl2.innerText = String(stats.chunked_files_count || 0);
    if (chunksEl) chunksEl.innerText = `${(stats.total_chunks || 0).toLocaleString()}개`;
    if (stratEl) {
        const strats = stats.chunk_strategies || {};
        const fastcdcCount = strats.fastcdc || strats.fastcdc_v1 || 0;
        const fixedCount = strats.fixed || strats.fixed_block || 0;
        stratEl.innerHTML = `
            <span class="px-2 py-0.5 rounded bg-blue-950 text-blue-300 border border-blue-800/60 font-mono text-[11px]">쪼개 저장: ${fastcdcCount.toLocaleString()}</span>
            <span class="px-2 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-800/60 font-mono text-[11px]">통째 저장: ${fixedCount.toLocaleString()}</span>
        `;
    }
}

function renderSystemDrives() {
    const container = document.getElementById('system-drives-list');
    if (!container || !state.systemInfo) return;

    container.innerHTML = state.systemInfo.drives.map(d => `
        <div class="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/90 flex flex-col gap-2">
            <div class="flex justify-between items-center text-xs">
                <span class="font-bold text-zinc-200 font-mono">${d.device} (${d.mountpoint})</span>
                <span class="text-zinc-400 font-mono text-[11px]">${d.percent}% 사용 중</span>
            </div>
            <div class="w-full bg-zinc-900 rounded-full h-1.5 overflow-hidden border border-zinc-850">
                <div class="bg-gradient-to-r from-blue-500 to-indigo-500 h-full rounded-full" style="width: ${Math.min(100, d.percent)}%"></div>
            </div>
            <div class="flex justify-between text-[11px] text-zinc-400 font-mono">
                <span>사용: ${formatBytes(d.used)}</span>
                <span>여유: ${formatBytes(d.free)}</span>
            </div>
        </div>
    `).join('');
}

function renderStorageHealth() {
    const container = document.getElementById('physical-disks-list');
    const badge = document.getElementById('storage-overall-badge');
    if (!container || !state.storageHealth || !state.storageHealth.physical_disks) return;

    const data = state.storageHealth;
    if (badge) {
        if (data.overall_status === 'healthy') {
            badge.className = 'px-1.5 py-0.5 bg-emerald-950 text-emerald-300 text-[10px] rounded border border-emerald-800';
            badge.innerText = '정상';
        } else if (data.overall_status === 'warning') {
            badge.className = 'px-1.5 py-0.5 bg-amber-950 text-amber-300 text-[10px] rounded border border-amber-800';
            badge.innerText = '주의';
        } else {
            badge.className = 'px-1.5 py-0.5 bg-rose-950 text-rose-300 text-[10px] rounded border border-rose-800 animate-pulse';
            badge.innerText = '심각';
        }
    }

    if (data.physical_disks.length === 0) {
        container.innerHTML = '<div class="text-zinc-500 text-[11px] py-1">감지된 물리 디스크 정보가 없습니다.</div>';
        return;
    }

    container.innerHTML = data.physical_disks.map(disk => {
        const isHealthy = disk.health_status.toLowerCase() === 'healthy';
        const isWarning = disk.health_status.toLowerCase() === 'warning';
        const statusColor = isHealthy ? 'text-emerald-400 bg-emerald-950/60 border-emerald-800' :
                           isWarning ? 'text-amber-400 bg-amber-950/60 border-amber-800' :
                           'text-rose-400 bg-rose-950/60 border-rose-800';

        const busBadge = disk.bus_type === 'NVMe' 
            ? '<span class="px-1.5 py-0.5 text-[9px] font-bold bg-purple-950 text-purple-300 border border-purple-800 rounded">NVMe</span>'
            : (disk.bus_type === 'SATA' 
                ? '<span class="px-1.5 py-0.5 text-[9px] font-bold bg-blue-950 text-blue-300 border border-blue-800 rounded">SATA</span>'
                : `<span class="px-1.5 py-0.5 text-[9px] font-bold bg-zinc-800 text-zinc-300 border border-zinc-700 rounded">${disk.bus_type || 'DISK'}</span>`);

        return `
            <div class="p-2.5 bg-zinc-950/80 rounded-xl border border-zinc-800/90 flex flex-col gap-1.5">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-1.5 overflow-hidden">
                        <span class="text-zinc-400 font-mono text-[11px] font-bold">#${disk.device_id}</span>
                        <span class="font-bold text-zinc-200 text-xs truncate max-w-[160px]" title="${disk.name}">${disk.name}</span>
                        ${busBadge}
                    </div>
                    <span class="px-1.5 py-0.5 text-[10px] rounded border font-mono ${statusColor}">${disk.health_status}</span>
                </div>
                <div class="flex justify-between items-center text-[10px] text-zinc-400 font-mono">
                    <span>유형: ${disk.media_type || 'SSD'}</span>
                    <span>용량: ${disk.size_gb.toFixed(1)} GB</span>
                    <span class="text-zinc-500">상태: ${disk.operational_status}</span>
                </div>
            </div>
        `;
    }).join('');

    if (window.lucide) {
        lucide.createIcons();
    }
}

function applySmartExcludePreset() {
    const input = document.getElementById('prof-excludes');
    if (!input) return;
    const presets = [
        "pagefile.sys", "hiberfil.sys", "swapfile.sys", "DumpStack.log*",
        "$Recycle.Bin", "System Volume Information", "PerfLogs",
        "*.tmp", "*.temp", "*.log", "node_modules", ".git", ".cache",
        "AppData\\Local\\Temp", "AppData\\Local\\Microsoft\\Windows\\INetCache"
    ];
    let current = input.value.split(',').map(s => s.trim()).filter(Boolean);
    for (const p of presets) {
        if (!current.includes(p)) {
            current.push(p);
        }
    }
    input.value = current.join(', ');
    showToast('스마트 제외 프리셋(VSS/OS/임시파일)이 적용되었습니다.', 'info');
}

function renderRecentSnapshots() {
    const container = document.getElementById('recent-snapshots-list');
    if (!container) return;

    if (!state.snapshots || state.snapshots.length === 0) {
        container.innerHTML = `
            <div class="text-center py-6 text-slate-500 text-xs bg-slate-900/40 rounded-xl border border-slate-800">
                아직 백업한 기록이 없어요. 홈 화면의 파란 '지금 백업하기' 버튼을 눌러 첫 백업을 시작해 보세요.
            </div>
        `;
        return;
    }

    const recents = state.snapshots.slice(0, 4);
    container.innerHTML = recents.map(s => {
        const isFull = s.backup_type === 'full';
        const sum = s.summary || {};
        const formattedDate = formatDate(s.iso_time);
        const typeLabel = isFull ? '전체 백업' : '바뀐 것만 백업';
        const sizeText = formatBytes(sum.total_bytes);

        return `
            <div class="px-4 py-3 bg-zinc-950/70 hover:bg-zinc-900/70 rounded-xl border border-zinc-800/80 hover:border-zinc-700/80 flex items-center justify-between transition text-xs group">
                <div class="flex items-center gap-3">
                    <span class="w-2 h-2 rounded-full ${isFull ? 'bg-blue-400 shadow-sm shadow-blue-500/50' : 'bg-emerald-400 shadow-sm shadow-emerald-500/50'}"></span>
                    <span class="font-mono text-zinc-300 font-medium">${formattedDate}</span>
                    <span class="text-zinc-600">•</span>
                    <span class="text-zinc-300 font-medium">${s.profile_name || '기본 백업'}</span>
                    <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 border border-zinc-700/60">${typeLabel}</span>
                    <span class="text-zinc-600">•</span>
                    <span class="font-mono text-zinc-100 font-semibold">${sizeText}</span>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="openRestoreModal('${s.id}')" class="px-3 py-1 bg-zinc-800 hover:bg-blue-600 text-zinc-200 hover:text-white rounded-lg text-xs font-semibold transition border border-zinc-700/80 hover:border-blue-500 flex items-center gap-1 shadow-sm">
                        <i data-lucide="rotate-ccw" class="w-3 h-3"></i> 되돌리기
                    </button>
                    <button onclick="inspectSnapshot('${s.id}')" class="px-2.5 py-1 bg-zinc-800/60 hover:bg-zinc-700 text-zinc-400 hover:text-white rounded-lg text-xs font-medium transition border border-zinc-800">
                        탐색
                    </button>
                </div>
            </div>
        `;
    }).join('');

    lucide.createIcons();
    renderDashboardSnapshotChart();
}

function renderDashboardSnapshotChart() {
    const container = document.getElementById('dashboard-snapshot-chart');
    const metaSummary = document.getElementById('chart-meta-summary');
    const dateLabels = document.getElementById('chart-date-labels');
    if (!container) return;

    if (!state.snapshots || state.snapshots.length === 0) {
        container.innerHTML = `
            <div class="w-full h-full flex flex-col items-center justify-center text-zinc-500 text-xs font-mono space-y-1">
                <span>아직 백업한 기록이 없어요.</span>
                <span class="text-[10px] text-zinc-600">'지금 백업하기' 버튼을 눌러 첫 백업을 시작해 보세요.</span>
            </div>
        `;
        if (metaSummary) metaSummary.innerText = '| 기록된 백업 시점 없음';
        return;
    }

    // 시간순 오름차순 정렬 (과거 -> 최신)
    const sorted = [...state.snapshots].sort((a, b) => {
        const tA = Number(a.created_at || a.timestamp || 0);
        const tB = Number(b.created_at || b.timestamp || 0);
        return tA - tB;
    });

    // 최근 최대 10개 백업 시점 (차트 영역에 맞춤)
    const chartSnaps = sorted.slice(-10);
    const maxBytes = Math.max(...chartSnaps.map(s => (s.summary && s.summary.total_bytes) || 1), 1024 * 1024);
    const latest = chartSnaps[chartSnaps.length - 1];

    if (metaSummary && latest) {
        const latestBytes = (latest.summary && latest.summary.total_bytes) || 0;
        const latestFiles = (latest.summary && latest.summary.total_files) || 0;
        metaSummary.innerText = `| 실측 DB 백업 시점 ${state.snapshots.length}개 보관 (최신: ${formatBytes(latestBytes)}, ${latestFiles.toLocaleString()}개 파일)`;
    }

    const barsHtml = chartSnaps.map((s, idx) => {
        const isLatest = (idx === chartSnaps.length - 1);
        const sum = s.summary || {};
        const b = sum.total_bytes || 0;
        const f = sum.total_files || 0;
        // 최대 높이 68%로 제한하여 상단 라벨이 컨테이너 천장에 닿아 잘리지 않도록 안전 여백 확보
        const heightPct = Math.max(28, Math.min(68, Math.round((b / maxBytes) * 65)));
        
        let dateLabel = '';
        try {
            const d = new Date((s.created_at || s.timestamp) * 1000);
            if (!isNaN(d.getTime())) {
                const mm = String(d.getMonth() + 1).padStart(2, '0');
                const dd = String(d.getDate()).padStart(2, '0');
                const hh = String(d.getHours()).padStart(2, '0');
                const ii = String(d.getMinutes()).padStart(2, '0');
                dateLabel = `${mm}/${dd} ${hh}:${ii}`;
            } else {
                dateLabel = s.id ? s.id.substring(5, 15) : `#${idx+1}`;
            }
        } catch(e) {
            dateLabel = `#${idx+1}`;
        }

        const tooltip = `[백업 시점 #${idx+1}]\nID: ${s.id}\n일시: ${dateLabel}\n용량: ${formatBytes(b)}\n파일 수: ${f.toLocaleString()}개\n프로필: ${s.profile_name || '기본 백업'}`;

        return `
            <div class="flex-1 min-w-[2.2rem] max-w-[4.2rem] flex flex-col justify-end items-center h-full group relative cursor-pointer" title="${tooltip}" onclick="inspectSnapshot('${s.id}')">
                <!-- 상단 용량 배지 (항시 선명 표시 & 잘림 방지) -->
                <div class="text-[9px] font-mono font-bold text-zinc-200 whitespace-nowrap absolute -top-6 left-1/2 -translate-x-1/2 bg-zinc-900/90 px-1 py-0.5 rounded border border-zinc-700/80 shadow-md pointer-events-none group-hover:border-blue-400 group-hover:text-white transition">
                    ${formatBytes(b)}
                </div>
                
                <!-- 기둥 막대 (슬림 바 & 부드러운 그라데이션) -->
                <div class="w-full ${isLatest ? 'bg-gradient-to-t from-blue-600 via-cyan-500 to-emerald-400 shadow-md shadow-emerald-500/25 border-t border-emerald-300' : 'bg-gradient-to-t from-blue-700 to-blue-500 group-hover:from-blue-600 group-hover:to-cyan-400'} rounded-t-lg transition-all duration-300 relative" style="height: ${heightPct}%">
                    ${isLatest ? '<span class="absolute -top-1 left-1/2 -translate-x-1/2 w-1.5 h-1.5 bg-emerald-300 rounded-full animate-ping"></span>' : ''}
                </div>
                
                <!-- 하단 시간 라벨 -->
                <div class="text-[10px] font-mono text-zinc-400 group-hover:text-zinc-200 transition mt-2 text-center font-medium">
                    ${dateLabel.split(' ')[1] || dateLabel}
                </div>
            </div>
        `;
    }).join('');

    // 다음 백업 예약/대기 슬롯 (시각적 균형감 제공)
    const nextSlotHtml = `
        <div class="flex-1 min-w-[2.2rem] max-w-[4.2rem] flex flex-col justify-end items-center h-full opacity-35 hover:opacity-80 transition cursor-pointer group" onclick="triggerQuickBackup()" title="클릭하여 지금 즉시 새 백업 생성">
            <div class="text-[9px] font-mono text-zinc-400 -top-6 absolute whitespace-nowrap opacity-0 group-hover:opacity-100 transition">
                + 백업 시작
            </div>
            <div class="w-full border-2 border-dashed border-zinc-700 group-hover:border-blue-500 rounded-t-lg flex flex-col items-center justify-center text-zinc-500 group-hover:text-blue-400 transition" style="height: 30%">
                <i data-lucide="plus" class="w-3.5 h-3.5"></i>
            </div>
            <div class="text-[10px] font-mono text-zinc-500 group-hover:text-zinc-300 mt-2 transition">
                Next
            </div>
        </div>
    `;

    container.innerHTML = `
        <div class="absolute inset-0 grid grid-rows-3 pointer-events-none opacity-20">
            <div class="border-b border-zinc-700"></div>
            <div class="border-b border-zinc-700"></div>
            <div></div>
        </div>
        ${barsHtml}
        ${nextSlotHtml}
    `;

    lucide.createIcons();

    if (dateLabels && chartSnaps.length > 0) {
        const first = chartSnaps[0];
        const last = chartSnaps[chartSnaps.length - 1];
        const firstDate = formatDate(first.iso_time || first.created_at);
        const lastDate = formatDate(last.iso_time || last.created_at);
        dateLabels.innerHTML = `
            <span>첫 백업 시점: <b class="text-zinc-300">${firstDate}</b></span>
            <span class="text-emerald-400">최신 백업 시점: <b>${lastDate} (Live)</b></span>
        `;
    }
}

// =========================================================================
// 백업 프로필 관리 & Windows 작업 스케줄러 & 디렉토리 브라우저
// =========================================================================

async function loadProfiles() {
    try {
        state.profiles = await fetchAPI('/api/profiles');
        renderProfilesList();
        await loadWindowsTaskStatus();
        await loadOffsiteStatus();
    } catch (e) {
        console.error('Failed to load profiles:', e);
    }
}

function renderProfilesList() {
    const container = document.getElementById('profiles-list-container');
    if (!container) return;

    if (!state.profiles || state.profiles.length === 0) {
        container.innerHTML = `<div class="text-slate-500 text-sm">아직 만든 백업 설정이 없어요. '새 백업 설정 추가'를 눌러 만들어 보세요.</div>`;
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
                    <p class="text-xs text-slate-400 mt-1">백업 보관함: <code class="text-blue-400 font-mono">${p.repo_dir}</code></p>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="editProfile('${p.id}')" class="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg text-xs font-medium">수정</button>
                    <button onclick="triggerProfileBackup('${p.id}')" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-medium">백업 실행</button>
                    <button onclick="deleteProfile('${p.id}', '${p.name.replace(/'/g, "\\'")}')" class="px-3 py-1.5 bg-red-900/60 hover:bg-red-600 text-red-300 hover:text-white rounded-lg text-xs font-medium transition">삭제</button>
                </div>
            </div>

            <div class="grid grid-cols-2 gap-2.5 bg-slate-900/60 p-3 rounded-xl text-xs">
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

function getDefaultRepoDir() {
    if (state.config && state.config.repository_dir) {
        return state.config.repository_dir;
    }
    const nonCDrive = (state.systemInfo?.drives || []).find(d => !d.device.toUpperCase().startsWith('C:'));
    if (nonCDrive) {
        const dev = nonCDrive.device.endsWith('\\') ? nonCDrive.device : nonCDrive.device + '\\';
        return dev + 'MyBackup_Repository';
    }
    return 'C:\\BackupRepository';
}

function openCreateProfileModal() {
    document.getElementById('profile-modal-title').innerText = '새 백업 프로필 생성';
    document.getElementById('prof-id').value = '';
    document.getElementById('prof-name').value = '새 백업 프로필';
    document.getElementById('prof-sources').value = '';
    document.getElementById('prof-repo').value = getDefaultRepoDir();
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
    document.getElementById('prof-repo').value = prof.repo_dir || getDefaultRepoDir();
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

let _valSourcesTimer = null;
function debounceValidateModalSources() {
    clearTimeout(_valSourcesTimer);
    _valSourcesTimer = setTimeout(validateModalSources, 400);
}

async function validateModalSources() {
    const feedback = document.getElementById('prof-sources-validation-feedback');
    if (!feedback) return;
    const sourcesStr = document.getElementById('prof-sources').value.trim();
    if (!sourcesStr) {
        feedback.innerHTML = '';
        return;
    }
    const lines = sourcesStr.split('\n').map(s => s.trim()).filter(s => s.length > 0);
    if (lines.length === 0) {
        feedback.innerHTML = '';
        return;
    }

    feedback.innerHTML = '<span class="text-zinc-400">경로 유효성 확인 중...</span>';
    const results = await Promise.all(lines.map(p => fetchAPI('/api/validate-path', {
        method: 'POST',
        body: JSON.stringify({ path: p })
    }).catch(() => ({ path: p, exists: false, is_dir: false, readable: false }))));

    feedback.innerHTML = results.map(r => {
        if (!r.exists) {
            return `<div class="text-rose-400 flex items-center gap-1.5"><i data-lucide="alert-circle" class="w-3 h-3"></i><span>${r.path || '경로'} - 존재하지 않음</span></div>`;
        }
        if (!r.readable) {
            return `<div class="text-amber-400 flex items-center gap-1.5"><i data-lucide="shield-alert" class="w-3 h-3"></i><span>${r.path} - 읽기 권한 없음</span></div>`;
        }
        return `<div class="text-emerald-400 flex items-center gap-1.5"><i data-lucide="check" class="w-3 h-3"></i><span>${r.path} - 정상 (${r.is_dir ? '폴더' : '파일'})</span></div>`;
    }).join('');
    lucide.createIcons();
}

let _valRepoTimer = null;
function debounceValidateModalRepo() {
    clearTimeout(_valRepoTimer);
    _valRepoTimer = setTimeout(validateModalRepo, 400);
}

async function validateModalRepo() {
    const feedback = document.getElementById('prof-repo-validation-feedback');
    if (!feedback) return;
    const repo = document.getElementById('prof-repo').value.trim();
    if (!repo) {
        feedback.innerHTML = '';
        return;
    }

    try {
        const res = await fetchAPI('/api/validate-path', {
            method: 'POST',
            body: JSON.stringify({ path: repo })
        });
        const freeGb = (res.free_bytes / (1024 * 1024 * 1024)).toFixed(1);
        if (res.exists) {
            feedback.innerHTML = `<span class="text-emerald-400 flex items-center gap-1"><i data-lucide="check" class="w-3 h-3"></i>백업 보관함 확인 완료 (여유 공간: ${freeGb} GB)</span>`;
        } else {
            feedback.innerHTML = `<span class="text-blue-400 flex items-center gap-1"><i data-lucide="info" class="w-3 h-3"></i>새 백업 보관함 폴더 생성 예정 (여유 공간: ${freeGb} GB)</span>`;
        }
        lucide.createIcons();
    } catch (e) {
        feedback.innerHTML = `<span class="text-zinc-500">경로 상태 확인 불가</span>`;
    }
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
        alert('프로필 이름, 백업 대상 경로, 백업 보관함 경로를 모두 입력하세요.');
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
        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/profiles/${id}` : '/api/profiles';
        await fetchAPI(url, {
            method: method,
            body: JSON.stringify(profileData)
        });
        closeModal('profile-modal');
        await loadProfiles();
        await loadDashboard();
    } catch (e) {
        alert('프로필 저장 실패: ' + friendlyError(e));
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
        alert('백업 실행 실패: ' + friendlyError(e));
    }
}

async function deleteProfile(profileId, profileName) {
    if (!confirm(`'${profileName || profileId}' 백업 프로필을 정말 삭제하시겠습니까?`)) return;

    try {
        await fetchAPI(`/api/profiles/${profileId}`, { method: 'DELETE' });
        await loadProfiles();
        await loadDashboard();
    } catch (e) {
        alert('프로필 삭제 실패: ' + friendlyError(e));
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
            badge.innerText = '등록 안 됨 (프로그램이 켜져 있는 동안 계속 동작)';
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
        alert('스케줄러 등록 오류: ' + friendlyError(e));
    }
}

async function unregisterWindowsTaskFromUI() {
    if (!confirm('Windows 작업 스케줄러 등록을 해제하시겠습니까?')) return;
    try {
        const res = await fetchAPI('/api/windows-task/unregister', { method: 'POST' });
        alert(res.message || '등록 해제되었습니다.');
        await loadWindowsTaskStatus();
    } catch (e) {
        alert('해제 오류: ' + friendlyError(e));
    }
}

// =========================================================================
// 3-2-1 다른 장소 보관 원격 복제 UI 핸들러
// =========================================================================

function toggleOffsiteProviderUI(provider, autoSave = false) {
    const localFields = document.getElementById('offsite-local-fields');
    const gdriveFields = document.getElementById('offsite-gdrive-fields');
    const isGdrive = provider === 'gdrive';
    if (localFields) localFields.classList.toggle('hidden', isGdrive);
    if (gdriveFields) gdriveFields.classList.toggle('hidden', !isGdrive);
    const radLocal = document.getElementById('offsite-provider-local');
    const radGdrive = document.getElementById('offsite-provider-gdrive');
    if (radLocal) radLocal.checked = !isGdrive;
    if (radGdrive) radGdrive.checked = isGdrive;

    if (autoSave && typeof saveOffsiteConfigFromUI === 'function') {
        saveOffsiteConfigFromUI(false);
    }
}

async function loadOffsiteStatus() {
    try {
        const res = await fetchAPI('/api/offsite');
        const badge = document.getElementById('offsite-status-badge');
        const pathInput = document.getElementById('offsite-remote-path');
        const gdriveCreds = document.getElementById('offsite-gdrive-credentials');
        const gdriveFolder = document.getElementById('offsite-gdrive-folder');
        const enabledCheck = document.getElementById('offsite-enabled');
        const autoCheck = document.getElementById('offsite-auto-sync');
        const detailInfo = document.getElementById('offsite-detail-info');

        const provider = res.provider || 'local';
        toggleOffsiteProviderUI(provider);

        if (pathInput) pathInput.value = res.remote_path || '';
        if (gdriveCreds) gdriveCreds.value = res.gdrive_credentials_file || '';
        if (gdriveFolder) gdriveFolder.value = res.gdrive_folder_id || 'BackupSystem_Offsite';
        if (enabledCheck) enabledCheck.checked = !!res.enabled;
        if (autoCheck) autoCheck.checked = res.auto_sync !== false;

        if (!badge) return;

        const st = res.status || {};
        if (provider === 'gdrive') {
            if (!res.gdrive_credentials_file) {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
                badge.innerText = '미설정 (서비스 계정 키 필요)';
                if (detailInfo) detailInfo.classList.add('hidden');
            } else if (st.reachable) {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-emerald-950/80 text-emerald-300 border border-emerald-700/60 flex items-center gap-1';
                badge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> Google Drive 연결됨 (여유: ${st.free_gb || 0} GB)`;
                if (detailInfo) {
                    detailInfo.classList.remove('hidden');
                    detailInfo.innerHTML = `구글 드라이브 계정: <span class="text-cyan-400 font-semibold">${st.user_email || 'Service Account'}</span> | 전체 ${st.total_gb || 0} GB 중 ${st.used_gb || 0} GB 사용 (${st.free_gb || 0} GB 여유) | 폴더 ID: <span class="text-slate-300 font-mono">${st.folder_id || '-'}</span>`;
                }
            } else {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-amber-950/80 text-amber-300 border border-amber-700/60 flex items-center gap-1';
                badge.innerText = '구글 드라이브 연결 확인 필요';
                if (detailInfo) {
                    detailInfo.classList.remove('hidden');
                    detailInfo.innerHTML = `<span class="text-amber-400">알림: ${st.error || '구글 드라이브 인증에 실패했습니다.'}</span>`;
                }
            }
        } else {
            if (!res.remote_path) {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-slate-400 border border-slate-700';
                badge.innerText = '미설정 (로컬 전용)';
                if (detailInfo) detailInfo.classList.add('hidden');
            } else if (st.reachable) {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-emerald-950/80 text-emerald-300 border border-emerald-700/60 flex items-center gap-1';
                badge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> 정상 연결 (여유: ${st.free_gb} GB / ${st.free_percent}%)`;
                if (detailInfo) {
                    detailInfo.classList.remove('hidden');
                    detailInfo.innerHTML = `원격 백업 보관함 상태: 정상 접근 가능 | 전체 ${st.total_gb} GB 중 ${st.used_gb} GB 사용 (${st.free_gb} GB 여유)`;
                }
            } else {
                badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-medium bg-amber-950/80 text-amber-300 border border-amber-700/60 flex items-center gap-1';
                badge.innerText = '원격지 접근 대기 중';
                if (detailInfo) {
                    detailInfo.classList.remove('hidden');
                    detailInfo.innerHTML = `<span class="text-amber-400">알림: ${st.error || '원격 경로에 접근할 수 없습니다.'}</span>`;
                }
            }
        }
    } catch (e) {
        console.error('Failed to load offsite status:', e);
    }
}

async function saveOffsiteConfigFromUI(showAlert = true) {
    const isGdrive = document.getElementById('offsite-provider-gdrive')?.checked;
    const provider = isGdrive ? 'gdrive' : 'local';
    const pathInput = document.getElementById('offsite-remote-path');
    const gdriveCreds = document.getElementById('offsite-gdrive-credentials');
    const gdriveFolder = document.getElementById('offsite-gdrive-folder');
    const enabledCheck = document.getElementById('offsite-enabled');
    const autoCheck = document.getElementById('offsite-auto-sync');

    const remote_path = (pathInput ? pathInput.value : '').trim().replace(/^["']|["']$/g, '');
    const gdrive_credentials_file = (gdriveCreds ? gdriveCreds.value : '').trim().replace(/^["']|["']$/g, '');
    const gdrive_folder_id = (gdriveFolder ? gdriveFolder.value : '').trim().replace(/^["']|["']$/g, '');
    const enabled = enabledCheck ? enabledCheck.checked : false;
    const auto_sync = autoCheck ? autoCheck.checked : true;

    try {
        const res = await fetchAPI('/api/offsite', {
            method: 'POST',
            body: JSON.stringify({
                provider,
                remote_path,
                gdrive_credentials_file,
                gdrive_folder_id,
                enabled,
                auto_sync
            })
        });
        if (res.success) {
            if (showAlert) {
                alert('🎉 다른 장소 보관 설정이 성공적으로 저장되었습니다.');
            }
            await loadOffsiteStatus();
            if (res.data && typeof renderOffsiteStatusWidget === 'function') {
                state.offsite = res.data;
                renderOffsiteStatusWidget();
            }
        } else {
            if (showAlert) {
                alert('설정 저장 실패: ' + (res.error || '알 수 없는 오류'));
            }
        }
    } catch (e) {
        if (showAlert) {
            alert('설정 저장 오류: ' + friendlyError(e));
        }
    }
}

async function uploadGdriveCredFile(input) {
    if (!input.files || input.files.length === 0) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetch('/api/offsite/upload_creds', {
            method: 'POST',
            body: formData
        });
        const data = await res.json();
        if (data.success && data.file_path) {
            const credsInput = document.getElementById('offsite-gdrive-credentials');
            if (credsInput) credsInput.value = data.file_path;
            alert(`✅ [키 파일 업로드 완료]\n파일명: ${data.filename}\n\n[연결 테스트] 버튼을 눌러 연동을 확인해 주십시오.`);
        } else {
            alert('키 파일 업로드 실패: ' + (data.error || '알 수 없는 오류'));
        }
    } catch(err) {
        alert('키 파일 업로드 통신 오류: ' + friendlyError(err));
    }
}

async function testOffsiteFromUI() {
    const isGdrive = document.getElementById('offsite-provider-gdrive')?.checked;
    const provider = isGdrive ? 'gdrive' : 'local';
    const pathInput = document.getElementById('offsite-remote-path');
    const gdriveCreds = document.getElementById('offsite-gdrive-credentials');
    const gdriveFolder = document.getElementById('offsite-gdrive-folder');

    const remote_path = (pathInput ? pathInput.value : '').trim().replace(/^["']|["']$/g, '');
    const gdrive_credentials_file = (gdriveCreds ? gdriveCreds.value : '').trim().replace(/^["']|["']$/g, '');
    const gdrive_folder_id = (gdriveFolder ? gdriveFolder.value : '').trim().replace(/^["']|["']$/g, '');
    if (provider === 'local' && !remote_path) {
        alert('테스트할 원격 대상 경로를 입력하세요.');
        return;
    }

    const badge = document.getElementById('offsite-status-badge');
    if (badge) badge.innerText = (provider === 'gdrive') ? '브라우저 로그인 대기 중...' : '연결 확인 중...';

    try {
        const res = await fetchAPI('/api/offsite/test', {
            method: 'POST',
            body: JSON.stringify({
                provider,
                remote_path,
                gdrive_credentials_file,
                gdrive_folder_id
            })
        });

        // OAuth 웹 팝업 인증이 필요한 경우: 사용자의 실제 브라우저에서 100% 팝업 오픈
        if (res.needs_oauth && res.auth_url) {
            if (badge) badge.innerText = '구글 로그인 팝업 창 대기 중...';
            const authWin = window.open(res.auth_url, 'gdrive_oauth', 'width=600,height=720,menubar=no,toolbar=no');
            if (!authWin || authWin.closed || typeof authWin.closed === 'undefined') {
                window.open(res.auth_url, '_blank');
            }

            // 토큰 저장 완료 여부를 2초 주기로 자동 감지 (최대 120초)
            let elapsedSec = 0;
            const poller = setInterval(async () => {
                elapsedSec += 2;
                if (elapsedSec > 120) {
                    clearInterval(poller);
                    if (badge) badge.innerText = '로그인 대기 시간 초과';
                    return;
                }
                try {
                    const stCheck = await fetchAPI('/api/offsite');
                    if (stCheck && stCheck.status && stCheck.status.reachable) {
                        clearInterval(poller);
                        try { if (authWin && !authWin.closed) authWin.close(); } catch(e){}
                        alert(`🎉 [Google Drive 연동 성공!]\n\n- 인증 계정: ${stCheck.status.user_email}\n- 가용 여유 공간: ${stCheck.status.free_gb} GB\n- 대상 폴더: ${stCheck.status.target_folder_id || 'kova'}`);
                        await saveOffsiteConfigFromUI(false);
                    }
                } catch(e) {}
            }, 2000);
            return;
        }

        const st = res.status || {};
        if (res.success && st.reachable) {
            if (provider === 'gdrive') {
                alert(`🎉 [Google Drive 연결 성공!]\n\n- 인증 계정: ${st.user_email || 'Service Account'}\n- 가용 여유 공간: ${st.free_gb} GB\n- 대상 폴더 ID: ${st.target_folder_id || st.folder_id}`);
            } else {
                alert(`🎉 [연결 성공!]\n\n- 원격 대상: ${remote_path}\n- 가용 여유 공간: ${st.free_gb} GB (${st.free_percent}% 여유)`);
            }
            await saveOffsiteConfigFromUI(false);
        } else {
            alert(`⚠️ [연결 실패]\n\n원격 대상에 접근할 수 없습니다:\n${st.error || '접근 권한이 없거나 설정이 올바르지 않습니다.'}`);
            await loadOffsiteStatus();
        }
    } catch (e) {
        alert('연결 테스트 오류: ' + friendlyError(e));
        await loadOffsiteStatus();
    }
}

async function triggerOffsiteSyncFromUI() {
    const isGdrive = document.getElementById('offsite-provider-gdrive')?.checked;
    const provider = isGdrive ? 'gdrive' : 'local';
    const pathInput = document.getElementById('offsite-remote-path');
    const remote_path = (pathInput ? pathInput.value : '').trim();

    const engineMsg = isGdrive 
        ? '(Rclone 멀티스레드 128MB 저장 조각 초고속 클라우드 엔진 가동)'
        : '(Windows 순정 robocopy /MT:32 고속 백그라운드 엔진 가동)';

    if (!confirm(`현재 로컬 백업 보관함 전체를 원격지로 지금 즉시 동기화(복제)하시겠습니까?\n${engineMsg}`)) return;

    try {
        const res = await fetchAPI('/api/offsite/sync', {
            method: 'POST',
            body: JSON.stringify({
                provider,
                remote_path: isGdrive ? undefined : (remote_path || undefined)
            })
        });
        if (res.success) {
            alert(res.message);
            switchTab('runner');
        } else {
            alert('복제 실행 실패: ' + (res.detail || res.error || '알 수 없는 오류'));
        }
    } catch (e) {
        alert('복제 실행 오류: ' + friendlyError(e));
    }
}

// Maintenance
async function runGarbageCollection() {
    if (!confirm('백업 보관함에서 참조되지 않는 쓰지 않는 저장 조각을 정리하고 디스크 공간을 회수하시겠습니까?')) return;
    try {
        const res = await fetchAPI('/api/maintenance/prune', { method: 'POST' });
        alert(`🧹 정리 완료!\n삭제된 쓰지 않는 저장 조각: ${res.deleted_blobs}개\n회수된 용량: ${formatBytes(res.freed_bytes)}`);
        await loadDashboard();
    } catch (e) {
        alert('정리 실패: ' + friendlyError(e));
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
// =========================================================================
// 시스템 종료 & 앱 초기화 진입점
// =========================================================================

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


// --- Universal Toast Notification ---
function showToast(message, type = 'info') {
    let toast = document.getElementById('app-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'app-toast';
        toast.className = 'fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-xl text-xs font-semibold shadow-2xl transition-all duration-300 transform translate-y-10 opacity-0 pointer-events-none flex items-center gap-2 border';
        document.body.appendChild(toast);
    }
    const bgMap = {
        success: 'bg-emerald-950 border-emerald-500/50 text-emerald-300',
        error: 'bg-red-950 border-red-500/50 text-red-300',
        warning: 'bg-amber-950 border-amber-500/50 text-amber-300',
        info: 'bg-zinc-900 border-zinc-700 text-zinc-200'
    };
    toast.className = `fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-xl text-xs font-semibold shadow-2xl transition-all duration-300 transform flex items-center gap-2 border ${bgMap[type] || bgMap.info}`;
    toast.textContent = message;
    toast.style.opacity = '1';
    toast.style.transform = 'translateY(0)';
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
    }, 3500);
}

// --- Software Auto-Update Client Logic ---
let latestUpdateInfo = null;

async function checkForSoftwareUpdate(forceCheck = false, showToastIfLatest = false) {
    const btnCheck = document.getElementById('btn-check-update');
    if (btnCheck && forceCheck) {
        btnCheck.disabled = true;
        btnCheck.innerHTML = `<i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i> 확인 중...`;
        if (window.lucide) lucide.createIcons();
    }

    try {
        const resp = await fetch(`/api/update/status?force_check=${forceCheck ? 'true' : 'false'}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        latestUpdateInfo = data;

        const headerBadge = document.getElementById('header-update-badge');
        const headerText = document.getElementById('header-update-text');
        const settingsDot = document.getElementById('settings-update-status-dot');
        const settingsText = document.getElementById('settings-update-status-text');
        const settingsTargetVer = document.getElementById('settings-update-target-version');
        const btnApply = document.getElementById('btn-apply-update');

        if (data && data.update_available) {
            // Update available!
            if (headerBadge) {
                headerBadge.classList.remove('hidden');
                headerBadge.classList.add('flex');
                if (headerText) headerText.textContent = `v${data.latest_version} 업데이트`;
            }
            if (settingsDot) {
                settingsDot.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse';
            }
            if (settingsText) {
                settingsText.className = 'text-amber-300 font-medium';
                settingsText.textContent = `새 버전 (v${data.latest_version}) 업데이트가 준비되었습니다!`;
            }
            if (settingsTargetVer) {
                settingsTargetVer.textContent = `최신: v${data.latest_version}`;
            }
            if (btnApply) {
                btnApply.disabled = false;
                btnApply.classList.remove('opacity-40', 'cursor-not-allowed');
                btnApply.classList.add('animate-pulse');
            }
            updateModalUI(data);
        } else {
            // Up to date
            if (headerBadge) {
                headerBadge.classList.remove('flex');
                headerBadge.classList.add('hidden');
            }
            if (settingsDot) {
                settingsDot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400';
            }
            if (settingsText) {
                settingsText.className = 'text-zinc-300 font-medium';
                settingsText.textContent = '현재 최신 버전을 안전하게 사용 중입니다.';
            }
            if (settingsTargetVer) {
                settingsTargetVer.textContent = data.current_version ? `v${data.current_version}` : '';
            }
            if (btnApply) {
                btnApply.disabled = true;
                btnApply.classList.add('opacity-40', 'cursor-not-allowed');
                btnApply.classList.remove('animate-pulse');
            }
            if (showToastIfLatest) {
                showToast(`현재 최신 버전(v${data.current_version || '최신'})입니다.`, 'info');
            }
        }
    } catch (e) {
        console.warn('Update check failed:', e);
        if (showToastIfLatest) {
            showToast('업데이트 확인 실패: 네트워크 상태를 확인하세요.', 'error');
        }
    } finally {
        if (btnCheck && forceCheck) {
            btnCheck.disabled = false;
            btnCheck.innerHTML = `<i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400"></i> 새 버전 확인`;
            if (window.lucide) lucide.createIcons();
        }
    }
}

function updateModalUI(info) {
    if (!info) return;
    const curElem = document.getElementById('update-modal-current-ver');
    const targetElem = document.getElementById('update-modal-target-ver');
    const changelogElem = document.getElementById('update-modal-changelog');
    const installerLink = document.getElementById('update-modal-installer-link');

    if (curElem && info.current_version) curElem.textContent = `v${info.current_version}`;
    if (targetElem && info.latest_version) targetElem.textContent = `v${info.latest_version}`;
    if (changelogElem) {
        changelogElem.textContent = info.changelog || '주요 보안 패치 및 안정성 향상, 최신 백업 엔진 최적화가 적용되었습니다.';
    }
    if (installerLink && info.installer_download_url) {
        installerLink.href = info.installer_download_url;
    }
}

function openSoftwareUpdateModal() {
    const modal = document.getElementById('software-update-modal');
    if (!modal) return;
    if (latestUpdateInfo) {
        updateModalUI(latestUpdateInfo);
    }
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    if (window.lucide) lucide.createIcons();
}

function closeSoftwareUpdateModal() {
    const modal = document.getElementById('software-update-modal');
    if (modal) {
        modal.classList.remove('flex');
        modal.classList.add('hidden');
    }
}

async function applySoftwareUpdate() {
    const btnModalApply = document.getElementById('btn-modal-apply-update');
    const progressBox = document.getElementById('update-modal-progress-box');
    const progressText = document.getElementById('update-modal-progress-text');

    if (progressBox) progressBox.classList.remove('hidden');
    if (btnModalApply) {
        btnModalApply.disabled = true;
        btnModalApply.innerHTML = `<i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i> 업데이트 진행 중...`;
    }
    if (window.lucide) lucide.createIcons();

    try {
        const resp = await fetch('/api/update/apply', { method: 'POST' });
        const res = await resp.json();
        if (!resp.ok || !res.success) {
            throw new Error(res.message || '업데이트 적용 요청 실패');
        }

        if (progressText) {
            progressText.textContent = '업데이트 패키지 교체 및 서비스 재시작 중입니다. 잠시 후 자동 새로고침됩니다...';
        }
        showToast('자동 업데이트가 시작되었습니다. 재시작 후 자동 재접속됩니다.', 'success');

        // Poll server health until it restarts and comes back online
        let pollCount = 0;
        const maxPoll = 45; // 90 seconds
        const pollInterval = setInterval(async () => {
            pollCount++;
            try {
                const health = await fetch('/api/system/storage-health', { cache: 'no-store' });
                if (health.ok && pollCount > 3) {
                    clearInterval(pollInterval);
                    if (progressText) progressText.textContent = '업데이트 완료! 페이지를 새로고침합니다...';
                    setTimeout(() => {
                        window.location.reload(true);
                    }, 1200);
                }
            } catch (err) {
                // Expected while server is restarting
                if (progressText) {
                    progressText.textContent = `서비스 재시작 대기 중... (${pollCount}초)`;
                }
            }
            if (pollCount >= maxPoll) {
                clearInterval(pollInterval);
                if (progressText) {
                    progressText.textContent = '재시작 시간이 지연되고 있습니다. 수동으로 새로고침(F5) 해주세요.';
                }
            }
        }, 2000);

    } catch (err) {
        console.error('Update apply error:', err);
        showToast(`업데이트 실패: ${err.message}`, 'error');
        if (progressBox) progressBox.classList.add('hidden');
        if (btnModalApply) {
            btnModalApply.disabled = false;
            btnModalApply.innerHTML = `<i data-lucide="zap" class="w-3.5 h-3.5 fill-current"></i> 다시 시도`;
        }
        if (window.lucide) lucide.createIcons();
    }
}

// Init
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    loadDashboard();
    if (typeof loadSystemImageStatus === 'function') {
        loadSystemImageStatus();
    }

    const driveSelect = document.getElementById('sysimg-drive-select');
    if (driveSelect) {
        driveSelect.addEventListener('change', () => {
            loadSystemImageStatus();
            loadVhdxList(driveSelect.value);
        });
    }

    // Start polling every 1 second
    state.pollInterval = setInterval(() => {
        pollTaskStatus();
    }, 1000);

    // Software auto-update silent background check
    setTimeout(() => {
        checkForSoftwareUpdate(false, false);
    }, 3000);
});
