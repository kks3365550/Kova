// =========================================================================
// [백업시스템] 마스터 비밀번호 인증 / 세션 관리 / 모달 제어 서브시스템
// =========================================================================

let _authStatus = {
    configured: false,
    authenticated: false,
    bypassed: false,
    allow_localhost_bypass: true
};

async function checkAuthStatus() {
    try {
        const res = await fetch('/api/auth/status');
        if (!res.ok) return;
        const data = await res.json();
        if (data.success) {
            _authStatus = data.data;
            updateAuthBadgeUI();
            if (!_authStatus.configured) {
                openAuthSetupModal();
            } else if (!_authStatus.authenticated && !_authStatus.bypassed) {
                openAuthLoginModal();
            }
        }
    } catch (e) {
        console.warn('Auth status check error:', e);
    }
}

function updateAuthBadgeUI() {
    const label = document.getElementById('auth-badge-label');
    const icon = document.getElementById('auth-badge-icon');
    const bypassTag = document.getElementById('auth-bypass-status-tag');
    const pwBtnText = document.getElementById('auth-pw-btn-text');
    const pwBtnIcon = document.getElementById('auth-pw-btn-icon');
    
    if (bypassTag) {
        bypassTag.innerText = _authStatus.allow_localhost_bypass ? 'ON' : 'OFF';
        bypassTag.className = _authStatus.allow_localhost_bypass 
            ? 'text-[10px] px-1.5 py-0.5 rounded bg-blue-900/60 text-blue-300 font-mono'
            : 'text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono';
    }

    // 드롭다운 메뉴 내 비밀번호 관리 버튼 동적 전환
    if (pwBtnText && pwBtnIcon) {
        if (!_authStatus.configured) {
            pwBtnText.innerText = '마스터 비밀번호 설정';
            pwBtnIcon.setAttribute('data-lucide', 'key');
            pwBtnIcon.className = 'w-3.5 h-3.5 text-amber-400 animate-pulse';
        } else {
            pwBtnText.innerText = '비밀번호 변경';
            pwBtnIcon.setAttribute('data-lucide', 'key-round');
            pwBtnIcon.className = 'w-3.5 h-3.5 text-amber-400';
        }
    }

    if (!label || !icon) return;

    if (!_authStatus.configured) {
        label.innerText = '미설정 (설정 필요)';
        icon.setAttribute('data-lucide', 'shield-alert');
        icon.className = 'w-3.5 h-3.5 text-amber-400';
    } else if (_authStatus.authenticated) {
        label.innerText = '보안 인증됨';
        icon.setAttribute('data-lucide', 'shield-check');
        icon.className = 'w-3.5 h-3.5 text-emerald-400';
    } else if (_authStatus.bypassed) {
        label.innerText = '로컬 자동인증';
        icon.setAttribute('data-lucide', 'laptop');
        icon.className = 'w-3.5 h-3.5 text-blue-400';
    } else {
        label.innerText = '인증 필요';
        icon.setAttribute('data-lucide', 'lock');
        icon.className = 'w-3.5 h-3.5 text-amber-400';
    }
    lucide.createIcons();
}

function toggleAuthDropdown() {
    const dd = document.getElementById('auth-dropdown');
    if (dd) dd.classList.toggle('hidden');
}

// 외부 클릭 시 드롭다운 닫기
document.addEventListener('click', (e) => {
    const wrapper = document.getElementById('auth-badge-wrapper');
    const dd = document.getElementById('auth-dropdown');
    if (wrapper && dd && !wrapper.contains(e.target)) {
        dd.classList.add('hidden');
    }
});

function handleAuthPasswordClick(e) {
    if (e && typeof e.stopPropagation === 'function') {
        e.stopPropagation();
    }
    const dd = document.getElementById('auth-dropdown');
    if (dd) dd.classList.add('hidden');

    if (!_authStatus.configured) {
        openAuthSetupModal();
    } else {
        openAuthChangeModal();
    }
}

function openAuthSetupModal() {
    const p1 = document.getElementById('setup-password');
    const p2 = document.getElementById('setup-password-confirm');
    const err = document.getElementById('setup-error');
    if (p1) p1.value = '';
    if (p2) p2.value = '';
    if (err) {
        err.innerText = '';
        err.classList.add('hidden');
    }

    const overlay = document.getElementById('auth-modal-overlay');
    const setupModal = document.getElementById('modal-auth-setup');
    const loginModal = document.getElementById('modal-auth-login');
    const changeModal = document.getElementById('modal-auth-change');
    if (overlay) {
        overlay.classList.remove('hidden');
        overlay.classList.add('flex');
    }
    if (setupModal) setupModal.classList.remove('hidden');
    if (loginModal) loginModal.classList.add('hidden');
    if (changeModal) changeModal.classList.add('hidden');
    setTimeout(() => p1?.focus(), 100);
}

function openAuthLoginModal() {
    const pw = document.getElementById('login-password');
    const err = document.getElementById('login-error');
    if (pw) pw.value = '';
    if (err) {
        err.innerText = '';
        err.classList.add('hidden');
    }

    const overlay = document.getElementById('auth-modal-overlay');
    const setupModal = document.getElementById('modal-auth-setup');
    const loginModal = document.getElementById('modal-auth-login');
    const changeModal = document.getElementById('modal-auth-change');
    if (overlay) {
        overlay.classList.remove('hidden');
        overlay.classList.add('flex');
    }
    if (setupModal) setupModal.classList.add('hidden');
    if (loginModal) loginModal.classList.remove('hidden');
    if (changeModal) changeModal.classList.add('hidden');
    setTimeout(() => pw?.focus(), 100);
}

function openAuthChangeModal() {
    const cur = document.getElementById('change-current');
    const n1 = document.getElementById('change-new');
    const n2 = document.getElementById('change-new-confirm');
    const err = document.getElementById('change-error');
    if (cur) cur.value = '';
    if (n1) n1.value = '';
    if (n2) n2.value = '';
    if (err) {
        err.innerText = '';
        err.classList.add('hidden');
    }

    const overlay = document.getElementById('auth-modal-overlay');
    const setupModal = document.getElementById('modal-auth-setup');
    const loginModal = document.getElementById('modal-auth-login');
    const changeModal = document.getElementById('modal-auth-change');
    if (overlay) {
        overlay.classList.remove('hidden');
        overlay.classList.add('flex');
    }
    if (setupModal) setupModal.classList.add('hidden');
    if (loginModal) loginModal.classList.add('hidden');
    if (changeModal) changeModal.classList.remove('hidden');
    setTimeout(() => cur?.focus(), 100);
}

function closeAuthModal() {
    const overlay = document.getElementById('auth-modal-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
        overlay.classList.remove('flex');
    }
}

async function submitAuthSetup(e) {
    e.preventDefault();
    const p1 = document.getElementById('setup-password').value;
    const p2 = document.getElementById('setup-password-confirm').value;
    const bypass = document.getElementById('setup-remember').checked;
    const errDiv = document.getElementById('setup-error');

    if (p1 !== p2) {
        errDiv.innerText = '비밀번호가 일치하지 않습니다.';
        errDiv.classList.remove('hidden');
        return;
    }

    if (p1.length < 8) {
        errDiv.innerText = '비밀번호는 최소 8자리 이상이어야 합니다.';
        errDiv.classList.remove('hidden');
        return;
    }

    try {
        const res = await fetch('/api/auth/setup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: p1, allow_localhost_bypass: bypass })
        });
        const data = await res.json();
        if (!res.ok) {
            errDiv.innerText = extractApiError(data) || '설정 중 오류가 발생했습니다.';
            errDiv.classList.remove('hidden');
            return;
        }
        await submitAuthLoginDirect(p1);
    } catch (err) {
        errDiv.innerText = '통신 오류가 발생했습니다.';
        errDiv.classList.remove('hidden');
    }
}

async function submitAuthLogin(e) {
    e.preventDefault();
    const pw = document.getElementById('login-password').value;
    await submitAuthLoginDirect(pw);
}

async function submitAuthLoginDirect(password) {
    const errDiv = document.getElementById('login-error');
    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password })
        });
        const data = await res.json();
        if (!res.ok) {
            if (errDiv) {
                errDiv.innerText = data.error || '비밀번호가 올바르지 않습니다.';
                errDiv.classList.remove('hidden');
            }
            return;
        }
        closeAuthModal();
        await checkAuthStatus();
        if (typeof loadDashboard === 'function') loadDashboard();
    } catch (err) {
        if (errDiv) {
            errDiv.innerText = '로그인 중 오류가 발생했습니다.';
            errDiv.classList.remove('hidden');
        }
    }
}

async function submitAuthLogout() {
    toggleAuthDropdown();
    if (!confirm('로그아웃 하시겠습니까?')) return;
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
        await checkAuthStatus();
        openAuthLoginModal();
    } catch (e) {
        console.error('Logout error:', e);
    }
}

async function submitAuthChangePassword(e) {
    e.preventDefault();
    const oldP = document.getElementById('change-current').value;
    const newP1 = document.getElementById('change-new').value;
    const newP2 = document.getElementById('change-new-confirm').value;
    const errDiv = document.getElementById('change-error');

    if (newP1 !== newP2) {
        errDiv.innerText = '새 비밀번호가 서로 일치하지 않습니다.';
        errDiv.classList.remove('hidden');
        return;
    }

    if (newP1.length < 8) {
        errDiv.innerText = '새 비밀번호는 최소 8자리 이상이어야 합니다.';
        errDiv.classList.remove('hidden');
        return;
    }

    try {
        const res = await fetch('/api/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldP, new_password: newP1 })
        });
        const data = await res.json();
        if (!res.ok) {
            // 422 검증 오류는 {detail: [...]} 형태라 error 필드가 없다.
            // 그대로 두면 '비밀번호 변경 실패' 라는 막연한 문구만 나온다.
            errDiv.innerText = extractApiError(data) || '비밀번호 변경 실패';
            errDiv.classList.remove('hidden');
            return;
        }
        
        // 새 비밀번호로 세션 자동 갱신 및 알림
        await submitAuthLoginDirect(newP1);
        alert('🎉 마스터 비밀번호가 성공적으로 변경되었습니다.');
    } catch (err) {
        errDiv.innerText = '통신 오류가 발생했습니다.';
        errDiv.classList.remove('hidden');
    }
}

async function toggleLocalhostBypass() {
    toggleAuthDropdown();
    const newState = !_authStatus.allow_localhost_bypass;
    try {
        const res = await fetch('/api/auth/toggle-bypass', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: newState })
        });
        if (res.ok) {
            await checkAuthStatus();
        }
    } catch (e) {
        console.error('Toggle bypass error:', e);
    }
}

// 자동 초기화: DOM 로드 시 인증 상태 점검
document.addEventListener('DOMContentLoaded', () => {
    checkAuthStatus();
});
