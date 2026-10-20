// =========================================================================
// [백업시스템] 공통 상태 저장소 & 포맷팅 유틸리티 & API 통신 & 모달 제어
// =========================================================================

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

async function fetchAPI(url, options = {}) {
    try {
        const res = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
        if (res.status === 401) {
            if (typeof openAuthLoginModal === 'function' && !url.includes('/api/auth/status')) {
                openAuthLoginModal();
            }
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || '인증이 필요합니다.');
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || err.error || 'API request failed');
        }
        return await res.json();
    } catch (e) {
        console.error(`API Error (${url}):`, e);
        throw e;
    }
}

// --- 공통 모달 제어 함수 ---
function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('hidden');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('hidden');
}

// --- API 오류 메시지 추출 ---
// 성공이 아닌 응답은 두 가지 형태다:
//   1) {success: false, error: "..."}        (직접 만든 엔드포인트)
//   2) {detail: [{type, loc, msg, ...}]}     (FastAPI/Pydantic 검증 오류, HTTP 422)
// 2번에는 error 필드가 없어서 그대로 두면 '실패' 라는 막연한 문구만 나온다.
function extractApiError(data) {
    if (!data) return '';
    if (typeof data.error === 'string' && data.error) return data.error;
    if (Array.isArray(data.detail) && data.detail.length) {
        const first = data.detail[0];
        const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '';
        if (first.type === 'string_too_short') {
            const need = first.ctx && first.ctx.min_length ? first.ctx.min_length : 8;
            return (field === 'new_password' ? '새 비밀번호는' : '입력값은') +
                ' 최소 ' + need + '자리 이상이어야 합니다.';
        }
        if (typeof first.msg === 'string' && first.msg) return first.msg;
    }
    if (typeof data.detail === 'string' && data.detail) return data.detail;
    return '';
}

// --- 스케줄러 백업 로그 보기 ---
// 작업 스케줄러는 pythonw.exe 로 cli_backup.py 를 실행한다.
// 콘솔이 없어 표준출력이 버려지므로, 로그는 logs/backup.log 에만 남는다.
// 이 함수가 그 파일을 서버를 통해 읽는다.
async function showScheduledBackupLogs() {
    const body = document.getElementById('backup-log-body');
    const meta = document.getElementById('backup-log-meta');
    if (!body) return;

    body.textContent = '불러오는 중...';
    meta.textContent = '';
    openModal('backup-log-modal');

    try {
        const d = await fetchAPI('/api/backup/logs?limit=400');
        meta.textContent = `${d.path}  (${d.exists ? (d.size_kb + ' KB') : '파일 없음'})`;

        if (d.notice) {
            body.textContent = d.notice;
            return;
        }
        if (!d.lines || !d.lines.length) {
            body.textContent = '기록된 로그가 없습니다.';
            return;
        }

        let out = d.lines.join('\n');
        if (d.rotated && d.rotated.length) {
            out += '\n\n--- 회전된 이전 로그 ---\n' +
                d.rotated.map(r => `${r.file}  (${r.size_kb} KB, ${r.lines}줄)`).join('\n');
        }
        body.textContent = out;
    } catch (e) {
        body.textContent = '로그를 불러오지 못했습니다: ' + (e.message || e);
    }
}

// --- 진단 로그 원클릭 복사 유틸리티 (Silent Failure 방지 체계) ---
async function copyToClipboardSafe(text) {
    if (!text || !text.trim()) {
        alert('복사할 로그 내용이 없습니다.');
        return false;
    }
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (_) {}
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.left = "-999999px";
    textArea.style.top = "-999999px";
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    let success = false;
    try {
        success = document.execCommand('copy');
    } catch (_) {}
    document.body.removeChild(textArea);
    return success;
}

async function copyDiagnosticLogs(btnElement) {
    const termBox = document.getElementById('terminal-log-box');
    let logText = "";
    if (termBox) {
        logText = termBox.innerText || termBox.textContent || "";
    }

    if (!logText || logText.includes("엔진 준비 완료")) {
        try {
            const d = await fetchAPI('/api/backup/logs?limit=100');
            if (d && d.lines && d.lines.length) {
                logText = d.lines.join('\n');
            }
        } catch (_) {}
    }

    if (!logText || !logText.trim()) {
        alert('복사할 진단 로그가 없습니다.');
        return;
    }

    const copied = await copyToClipboardSafe(logText);
    if (copied) {
        if (btnElement) {
            const originalHTML = btnElement.innerHTML;
            btnElement.innerHTML = `<span class="text-emerald-400 font-bold">✅ 복사 완료!</span>`;
            setTimeout(() => { btnElement.innerHTML = originalHTML; }, 2000);
        } else {
            alert('📋 진단 로그가 클립보드에 복사되었습니다.\n문제 분석을 위해 바로 붙여넣기(Ctrl+V)하세요.');
        }
    }
}

async function copyModalLogs(btnElement) {
    const body = document.getElementById('backup-log-body');
    const logText = body ? (body.innerText || body.textContent || "") : "";
    if (!logText || !logText.trim()) {
        alert('복사할 로그가 없습니다.');
        return;
    }
    const copied = await copyToClipboardSafe(logText);
    if (copied) {
        if (btnElement) {
            const originalHTML = btnElement.innerHTML;
            btnElement.innerHTML = `<span class="text-emerald-400 font-bold">✅ 복사 완료!</span>`;
            setTimeout(() => { btnElement.innerHTML = originalHTML; }, 2000);
        } else {
            alert('📋 로그 내용이 클립보드에 복사되었습니다.');
        }
    }
}

// --- 글로벌 경보 배너 시스템 (Red Alert System) ---
let _bannerDismissedUntil = 0;

async function checkGlobalAlerts() {
    if (Date.now() < _bannerDismissedUntil) return;

    try {
        const res = await fetchAPI('/api/alerts/summary');
        const banner = document.getElementById('global-alert-banner');
        if (!banner || !res || !res.success) return;

        const data = res.data || {};
        const status = data.status || 'ok';
        const alerts = data.alerts || [];

        const badge = document.getElementById('alert-banner-badge');
        const statusText = document.getElementById('alert-banner-status');
        const msgText = document.getElementById('alert-banner-message');

        if (status === 'critical' && alerts.length > 0) {
            banner.className = 'shrink-0 border-b border-red-700/80 bg-red-950/90 px-6 py-2.5 flex items-center justify-between text-xs z-30 transition-all duration-300';
            if (badge) badge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-bold uppercase tracking-wider flex items-center gap-1.5 shadow-sm bg-red-600 text-white animate-pulse';
            if (statusText) statusText.innerText = '심각 경보 (CRITICAL)';
            const topAlert = alerts[0];
            const extraCount = alerts.length > 1 ? ` (외 ${alerts.length - 1}건)` : '';
            if (msgText) msgText.innerText = `[${topAlert.title || '경보'}] ${topAlert.message}${extraCount}`;
            banner.classList.remove('hidden');
        } else if (status === 'warning' && alerts.length > 0) {
            banner.className = 'shrink-0 border-b border-amber-700/80 bg-amber-950/90 px-6 py-2.5 flex items-center justify-between text-xs z-30 transition-all duration-300';
            if (badge) badge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-bold uppercase tracking-wider flex items-center gap-1.5 shadow-sm bg-amber-500 text-slate-950 font-bold';
            if (statusText) statusText.innerText = '주의 경보 (WARNING)';
            const topAlert = alerts[0];
            const extraCount = alerts.length > 1 ? ` (외 ${alerts.length - 1}건)` : '';
            if (msgText) msgText.innerText = `[${topAlert.title || '주의'}] ${topAlert.message}${extraCount}`;
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
        if (window.lucide) lucide.createIcons();
    } catch (e) {
        // 알림 조회 실패 시 무시
    }
}

function dismissAlertBanner() {
    const banner = document.getElementById('global-alert-banner');
    if (banner) banner.classList.add('hidden');
    _bannerDismissedUntil = Date.now() + 5 * 60 * 1000;
}

// --- 원격 마스터 릴리즈 동기화 (One-Click Remote Sync) ---
let _remoteReleaseDismissed = false;
let _installerDownloadUrl = null;

async function checkRemoteRelease() {
    try {
        const res = await fetchAPI('/api/system/check-remote-release');
        const banner = document.getElementById('remote-release-banner');
        const statusBadge = document.getElementById('header-release-status');
        const statusText = document.getElementById('header-release-text');
        const statusIcon = document.getElementById('header-release-icon');

        if (!res || !res.success) return;
        const data = res.data || {};

        // 설치 파일 다운로드 URL 캐싱
        if (data.installer_download_url) {
            _installerDownloadUrl = data.installer_download_url;
        }

        if (data.current_version) {
            const versionBadge = document.getElementById('header-version-badge');
            if (versionBadge) {
                const versionStr = data.current_version.startsWith('v') ? data.current_version : 'v' + data.current_version;
                versionBadge.innerText = versionStr;
            }
        }

        if (data.error) {
            // 통신 오류 시 배지 조용히 숨김 (최신 버전 오인 방지)
            if (statusBadge) statusBadge.classList.add('hidden');
            if (banner) banner.classList.add('hidden');
            return;
        }

        if (data.update_available) {
            // GitHub 공식 새 릴리즈 감지
            if (statusBadge && statusText && statusIcon) {
                statusBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium flex items-center gap-1 border bg-indigo-500/20 text-indigo-300 border-indigo-500/40 animate-pulse';
                statusIcon.setAttribute('data-lucide', 'sparkles');
                statusText.innerText = `새 버전 ${data.remote_version}`;
                statusBadge.title = `GitHub 공식 릴리즈에 새로운 버전 ${data.remote_version}이 있습니다.`;
                statusBadge.classList.remove('hidden');
            }
            if (banner && !_remoteReleaseDismissed) {
                const textElem = document.getElementById('remote-release-text');
                if (textElem) {
                    textElem.innerText = `GitHub 공식 릴리즈에 최신 버전 ${data.remote_version}이 감지되었습니다. (현재: v${data.current_version})`;
                }
                banner.classList.remove('hidden');
            }
        } else {
            // 버전 동일 (GitHub 공식 릴리즈 기준 최신 상태)
            if (statusBadge && statusText && statusIcon) {
                statusBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-medium flex items-center gap-1 border bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
                statusIcon.setAttribute('data-lucide', 'check-check');
                statusText.innerText = '최신 버전';
                statusBadge.title = 'GitHub 공식 릴리즈와 일치하는 최신 상태입니다.';
                statusBadge.classList.remove('hidden');
            }
            if (banner) banner.classList.add('hidden');
        }
        if (window.lucide) lucide.createIcons();
    } catch (e) {
        // 원격 조회 실패 시 조용히 스킵
    }
}

function dismissRemoteReleaseBanner() {
    const banner = document.getElementById('remote-release-banner');
    if (banner) banner.classList.add('hidden');
    _remoteReleaseDismissed = true;
}

function triggerRemoteReleaseSync() {
    window.open('https://github.com/kks3365550/Kova/releases/latest', '_blank');
}

function downloadInstaller() {
    const url = _installerDownloadUrl || '/api/system/download-installer';
    const a = document.createElement('a');
    a.href = url;
    a.download = '';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// 10초마다 자동 경보 점검 및 60초마다 원격 릴리즈 점검
document.addEventListener('DOMContentLoaded', () => {
    checkGlobalAlerts();
    setInterval(checkGlobalAlerts, 10000);

    checkRemoteRelease();
    setInterval(checkRemoteRelease, 60000);
});
