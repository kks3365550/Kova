// web/static/js/rescue_usb.js - 긴급 재해 복구(Rescue DR) UEFI 부팅 USB 2클릭 무개입 마법사

let rescueUsbPollTimer = null;
let rescueUsbAutoDetectTimer = null;
let cachedRescueTargets = [];

async function openRescueUsbModal() {
    openModal('rescue-usb-modal');

    // UI 상태 리셋
    const completedBox = document.getElementById('rescue-usb-completed');
    const progressBox = document.getElementById('rescue-usb-progress-container');
    if (completedBox) completedBox.classList.add('hidden');
    if (progressBox) progressBox.classList.add('hidden');

    await loadRescueUsbTargets(false);
    checkRescueUsbStatus();

    // USB 연결 실시간 자동 감지 폴링 (1.5초마다 꽂힌 USB 스캔)
    if (rescueUsbAutoDetectTimer) clearInterval(rescueUsbAutoDetectTimer);
    rescueUsbAutoDetectTimer = setInterval(async () => {
        const modal = document.getElementById('rescue-usb-modal');
        if (!modal || modal.classList.contains('hidden')) {
            clearInterval(rescueUsbAutoDetectTimer);
            rescueUsbAutoDetectTimer = null;
            return;
        }

        // 제작 진행 중이거나 완료된 상태일 때는 자동 재스캔 건너뜀
        const pContainer = document.getElementById('rescue-usb-progress-container');
        const cContainer = document.getElementById('rescue-usb-completed');
        if ((pContainer && !pContainer.classList.contains('hidden')) ||
            (cContainer && !cContainer.classList.contains('hidden'))) {
            return;
        }

        await loadRescueUsbTargets(true);
    }, 1500);
}

// 모달 닫기 시 자동 감지 타이머 해제
const origCloseModal = window.closeModal;
window.closeModal = function(id) {
    if (id === 'rescue-usb-modal') {
        if (rescueUsbAutoDetectTimer) {
            clearInterval(rescueUsbAutoDetectTimer);
            rescueUsbAutoDetectTimer = null;
        }
        if (rescueUsbPollTimer) {
            clearInterval(rescueUsbPollTimer);
            rescueUsbPollTimer = null;
        }
    }
    if (typeof origCloseModal === 'function') {
        origCloseModal(id);
    }
};

async function loadRescueUsbTargets(isSilent = false) {
    const select = document.getElementById('rescue-usb-select');
    const startBtn = document.getElementById('btn-start-make-usb');
    const startBtnText = document.getElementById('btn-start-make-usb-text');
    const scanBadge = document.getElementById('rescue-usb-scan-badge');
    const waitingBox = document.getElementById('rescue-usb-waiting');
    const readyBox = document.getElementById('rescue-usb-ready');
    const multiContainer = document.getElementById('rescue-usb-multi-container');

    if (!select) return;

    if (!isSilent && scanBadge) {
        scanBadge.textContent = '스캔 중...';
    }

    try {
        const res = await fetch('/api/rescue/usb-targets');
        const data = await res.json();

        cachedRescueTargets = data.targets || [];

        // 1. USB 미감지 상태
        if (!data.success || !data.targets || data.targets.length === 0) {
            if (waitingBox) waitingBox.classList.remove('hidden');
            if (readyBox) readyBox.classList.add('hidden');
            if (multiContainer) multiContainer.classList.add('hidden');

            select.innerHTML = '<option value="">연결된 USB가 없습니다</option>';
            select.disabled = true;

            if (scanBadge) scanBadge.textContent = 'USB 연결 대기';
            if (startBtn) {
                startBtn.disabled = true;
            }
            if (startBtnText) {
                startBtnText.textContent = 'USB를 PC에 꽂아주세요';
            }
            if (window.lucide) lucide.createIcons();
            return;
        }

        // 2. USB 감지 성공 상태
        if (waitingBox) waitingBox.classList.add('hidden');
        if (readyBox) readyBox.classList.remove('hidden');

        select.innerHTML = '';
        data.targets.forEach((t, idx) => {
            const opt = document.createElement('option');
            opt.value = t.DriveLetter;
            opt.textContent = `${t.DriveLetter} [${t.Label}] (${t.SizeGB} GB, ${t.FileSystem}) - 여유: ${t.FreeGB} GB`;
            if (idx === 0) opt.selected = true;
            select.appendChild(opt);
        });
        select.disabled = false;

        if (data.targets.length > 1) {
            if (multiContainer) multiContainer.classList.remove('hidden');
        } else {
            if (multiContainer) multiContainer.classList.add('hidden');
        }

        if (scanBadge) scanBadge.textContent = `${data.targets.length}개 USB 감지`;
        if (startBtn) startBtn.disabled = false;

        const currentChosen = data.targets.find(x => x.DriveLetter === select.value) || data.targets[0];
        updateSelectedUsbCard(currentChosen, data.iso_ready);

        select.onchange = () => {
            const chosen = data.targets.find(x => x.DriveLetter === select.value) || data.targets[0];
            updateSelectedUsbCard(chosen, data.iso_ready);
        };

        if (window.lucide) lucide.createIcons();

    } catch (e) {
        if (!isSilent) console.error('USB 스캔 오류:', e);
    }
}

function updateSelectedUsbCard(target, isoReady) {
    if (!target) return;

    const titleEl = document.getElementById('rescue-usb-target-title');
    const descEl = document.getElementById('rescue-usb-target-desc');
    const startBtnText = document.getElementById('btn-start-make-usb-text');

    if (titleEl) {
        titleEl.textContent = `${target.DriveLetter} [${target.Label}] (${target.SizeGB} GB)`;
    }
    if (descEl) {
        const isoNotice = isoReady ? '순정 Win11 복구 시스템 탑재 준비 완료' : 'Win11 기본 복구 환경 적용';
        descEl.textContent = `전체 ${target.SizeGB} GB (여유: ${target.FreeGB} GB, ${target.FileSystem}) · ${isoNotice}`;
    }
    if (startBtnText) {
        startBtnText.textContent = `🚀 ${target.DriveLetter} 드라이브에 지금 복구 USB 만들기`;
    }
}

async function startRescueUsbCreation() {
    const select = document.getElementById('rescue-usb-select');
    const driveLetter = select && select.value ? select.value : (cachedRescueTargets[0]?.DriveLetter || '');

    if (!driveLetter) {
        alert('연결된 USB 드라이브를 찾을 수 없습니다. USB를 꽂은 후 다시 시도해주세요.');
        return;
    }

    const formatCheck = document.getElementById('rescue-usb-auto-format');
    let autoFormat = formatCheck ? formatCheck.checked : true;

    const chosen = cachedRescueTargets.find(x => x.DriveLetter === driveLetter);
    if (chosen && !chosen.IsFat32 && !autoFormat) {
        const ok = confirm(`선택하신 ${driveLetter} 드라이브는 ${chosen.FileSystem} 형식입니다.\nUEFI 표준 부팅을 위해선 FAT32 초기화가 필요합니다.\n\n안전하게 초기화(포맷) 후 계속 진행하시겠습니까?`);
        if (!ok) return;
        autoFormat = true;
    }

    const startBtn = document.getElementById('btn-start-make-usb');
    const readyBox = document.getElementById('rescue-usb-ready');
    const progressContainer = document.getElementById('rescue-usb-progress-container');

    if (startBtn) {
        startBtn.disabled = true;
        const btnText = document.getElementById('btn-start-make-usb-text');
        if (btnText) btnText.textContent = '제작 준비 중...';
    }

    try {
        const res = await fetch('/api/rescue/make-usb', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                drive_letter: driveLetter,
                auto_format: autoFormat
            })
        });
        const data = await res.json();

        if (!res.ok || !data.success) {
            alert('USB 제작 시작 실패: ' + (data.detail || data.error || '알 수 없는 오류'));
            if (startBtn) {
                startBtn.disabled = false;
                const btnText = document.getElementById('btn-start-make-usb-text');
                if (btnText) btnText.textContent = `🚀 ${driveLetter} 드라이브에 지금 복구 USB 만들기`;
            }
            return;
        }

        // 제작 상태 전환
        if (readyBox) readyBox.classList.add('hidden');
        if (progressContainer) progressContainer.classList.remove('hidden');

        startRescueUsbPolling();

    } catch (e) {
        alert('요청 전송 실패: ' + (e.message || e));
        if (startBtn) {
            startBtn.disabled = false;
            const btnText = document.getElementById('btn-start-make-usb-text');
            if (btnText) btnText.textContent = `🚀 ${driveLetter} 드라이브에 지금 복구 USB 만들기`;
        }
    }
}

function startRescueUsbPolling() {
    if (rescueUsbPollTimer) clearInterval(rescueUsbPollTimer);
    checkRescueUsbStatus();
    rescueUsbPollTimer = setInterval(checkRescueUsbStatus, 1000);
}

function updateStepTracker(phase, progress) {
    const s1 = document.getElementById('usb-step-1');
    const s2 = document.getElementById('usb-step-2');
    const s3 = document.getElementById('usb-step-3');
    if (!s1 || !s2 || !s3) return;

    // Reset styles
    [s1, s2, s3].forEach(el => {
        el.className = 'p-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-500 flex flex-col items-center gap-1 transition';
    });

    const activeStyle = 'p-2 rounded-xl bg-blue-950/40 border border-blue-500/60 text-blue-400 flex flex-col items-center gap-1 shadow-sm transition';
    const doneStyle = 'p-2 rounded-xl bg-emerald-950/30 border border-emerald-500/40 text-emerald-400 flex flex-col items-center gap-1 transition';

    if (phase === 'VALIDATING' || phase === 'MOUNTING' || progress < 25) {
        s1.className = activeStyle;
    } else if (phase === 'COPYING_BOOT' || (progress >= 25 && progress < 70)) {
        s1.className = doneStyle;
        s2.className = activeStyle;
    } else if (phase === 'INJECTING_PE' || phase === 'COPYING_TOOLS' || (progress >= 70 && progress < 100)) {
        s1.className = doneStyle;
        s2.className = doneStyle;
        s3.className = activeStyle;
    } else if (phase === 'COMPLETED' || progress >= 100) {
        s1.className = doneStyle;
        s2.className = doneStyle;
        s3.className = doneStyle;
    }
}

async function checkRescueUsbStatus() {
    try {
        const res = await fetch('/api/rescue/status');
        const st = await res.json();

        const progressContainer = document.getElementById('rescue-usb-progress-container');
        const progressBar = document.getElementById('rescue-usb-progress-bar');
        const progressPct = document.getElementById('rescue-usb-progress-pct');
        const phaseText = document.getElementById('rescue-usb-phase-text');
        const consoleBox = document.getElementById('rescue-usb-console');
        const startBtn = document.getElementById('btn-start-make-usb');
        const startBtnText = document.getElementById('btn-start-make-usb-text');
        const completedBox = document.getElementById('rescue-usb-completed');
        const readyBox = document.getElementById('rescue-usb-ready');

        if (!st || st.phase === 'IDLE') {
            return;
        }

        if (st.is_running) {
            if (readyBox) readyBox.classList.add('hidden');
            if (progressContainer) progressContainer.classList.remove('hidden');
            if (progressBar) progressBar.style.width = `${st.progress}%`;
            if (progressPct) progressPct.textContent = `${st.progress}%`;
            if (phaseText) {
                phaseText.innerHTML = `<i data-lucide="loader-2" class="w-3.5 h-3.5 text-blue-400 animate-spin"></i> <span>${escapeHtml(st.phase_text || '제작 진행 중...')}</span>`;
            }
            updateStepTracker(st.phase, st.progress);
        }

        if (consoleBox && st.log) {
            consoleBox.innerHTML = st.log.map(line => `<div>${escapeHtml(line)}</div>`).join('');
            consoleBox.scrollTop = consoleBox.scrollHeight;
        }

        if (startBtn && startBtnText) {
            if (st.is_running) {
                startBtn.disabled = true;
                startBtnText.textContent = `제작 중 (${st.progress}%)`;
            }
        }

        // 제작 완료 처리
        if (!st.is_running && st.phase === 'COMPLETED') {
            if (rescueUsbPollTimer) {
                clearInterval(rescueUsbPollTimer);
                rescueUsbPollTimer = null;
            }

            updateStepTracker('COMPLETED', 100);
            if (progressContainer) progressContainer.classList.add('hidden');
            if (completedBox) completedBox.classList.remove('hidden');

            if (startBtn && startBtnText) {
                startBtn.disabled = true;
                startBtnText.textContent = '✅ 제작 완료';
            }
            if (window.lucide) lucide.createIcons();
        }

        // 제작 에러 처리
        if (!st.is_running && st.phase === 'ERROR') {
            if (rescueUsbPollTimer) {
                clearInterval(rescueUsbPollTimer);
                rescueUsbPollTimer = null;
            }
            if (phaseText) {
                phaseText.innerHTML = `<i data-lucide="alert-triangle" class="w-3.5 h-3.5 text-red-400"></i> <span class="text-red-400 font-bold">제작 실패: ${escapeHtml(st.error || '오류 발생')}</span>`;
            }
            if (startBtn) {
                startBtn.disabled = false;
                if (startBtnText) startBtnText.textContent = '다시 시도';
            }
            if (window.lucide) lucide.createIcons();
        }

    } catch (e) {
        console.error('USB 상태 확인 오류:', e);
    }
}

async function openRescueGuideModal() {
    openModal('rescue-guide-modal');
    try {
        const res = await fetch('/api/rescue/guide');
        const guide = await res.json();
        if (!guide.success) return;

        const stepsContainer = document.getElementById('rescue-guide-steps');
        const keysContainer = document.getElementById('rescue-guide-boot-keys');

        if (stepsContainer && guide.steps) {
            stepsContainer.innerHTML = guide.steps.map(s => `
                <div class="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800/80 space-y-1">
                    <div class="flex items-center gap-2">
                        <span class="w-6 h-6 rounded-lg bg-blue-600/30 text-blue-400 font-bold text-xs flex items-center justify-center font-mono">${s.step}</span>
                        <span class="text-xs font-bold text-slate-100">${escapeHtml(s.title)}</span>
                    </div>
                    <p class="text-[11px] text-slate-400 pl-8 leading-relaxed">${escapeHtml(s.desc)}</p>
                </div>
            `).join('');
        }

        if (keysContainer && guide.boot_keys) {
            keysContainer.innerHTML = guide.boot_keys.map(k => `
                <div class="flex items-center justify-between p-2 rounded-lg bg-slate-950/70 border border-slate-800/60 text-xs font-mono">
                    <span class="text-slate-300 font-medium">${escapeHtml(k.brand)}</span>
                    <span class="px-2 py-0.5 rounded bg-blue-950/60 text-blue-300 border border-blue-800/50 font-bold">${escapeHtml(k.key)}</span>
                </div>
            `).join('');
        }

        if (window.lucide) lucide.createIcons();

    } catch (e) {
        console.error('가이드 로드 오류:', e);
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}


// ============================================================================
// BitLocker Status & Recovery Key Management
// ============================================================================
let cachedBitLockerData = null;

async function loadBitLockerStatus() {
    try {
        const res = await fetch('/api/system/bitlocker-status');
        const json = await res.json();
        if (!json.ok || !json.data) return;
        cachedBitLockerData = json.data;

        const badge = document.getElementById('bitlocker-badge');
        const statusText = document.getElementById('bitlocker-status-text');
        const keyPreview = document.getElementById('bitlocker-key-preview');

        if (json.data.is_encrypted) {
            if (badge) {
                badge.className = 'inline-block text-[10px] px-2.5 py-0.5 rounded-full bg-amber-950/80 text-amber-300 font-bold border border-amber-600/60 font-mono';
                badge.innerText = '⚠️ BitLocker 켜짐 (복구키 보관 필수)';
            }
            if (statusText) {
                statusText.className = 'text-amber-400 font-bold';
                statusText.innerText = json.data.protection_label || '보호 켜짐 (암호화 활성)';
            }
            if (keyPreview) {
                if (json.data.recovery_key) {
                    const k = json.data.recovery_key;
                    keyPreview.className = 'text-amber-300 font-bold font-mono tracking-wider';
                    keyPreview.innerText = k.substring(0, 13) + '-******-******';
                } else {
                    keyPreview.className = 'text-amber-400 font-semibold';
                    keyPreview.innerText = 'Microsoft 계정 확인 필요';
                }
            }
        } else {
            if (badge) {
                badge.className = 'inline-block text-[10px] px-2.5 py-0.5 rounded-full bg-emerald-950/80 text-emerald-300 font-bold border border-emerald-600/60 font-mono';
                badge.innerText = '🟢 BitLocker 꺼짐 (암호화 없음)';
            }
            if (statusText) {
                statusText.className = 'text-emerald-400 font-bold';
                statusText.innerText = '암호화 안 됨 (WinPE 즉시 복구 가능)';
            }
            if (keyPreview) {
                keyPreview.className = 'text-slate-400 font-mono';
                keyPreview.innerText = '필요 없음 (잠금 해제)';
            }
        }
    } catch (e) {
        console.warn('BitLocker 상태 조회 건너뜀:', e);
    }
}

async function openBitLockerModal() {
    openModal('bitlocker-modal');
    if (!cachedBitLockerData) {
        await loadBitLockerStatus();
    }
    const data = cachedBitLockerData || {};
    const keyBox = document.getElementById('bitlocker-modal-key');
    const idBox = document.getElementById('bitlocker-modal-key-id');
    const guideBox = document.getElementById('bitlocker-modal-guide-text');

    if (keyBox) {
        if (data.recovery_key) {
            keyBox.innerText = data.recovery_key;
        } else {
            keyBox.innerText = 'Windows 권한 정책으로 직접 키가 조회되지 않습니다.\n아래 마이크로소프트 계정 웹페이지에서 48자리를 즉시 확인하실 수 있습니다.';
        }
    }
    if (idBox) {
        idBox.innerText = data.key_id ? `(식별자 ID: ${data.key_id})` : '';
    }
    if (guideBox) {
        guideBox.value = data.guide_text || '';
    }
    if (window.lucide) lucide.createIcons();
}

async function saveBitLockerGuideToFile() {
    try {
        const res = await fetch('/api/system/bitlocker-save-guide', { method: 'POST' });
        const json = await res.json();
        if (json.ok) {
            if (typeof showToast === 'function') {
                showToast('바탕화면에 BitLocker 복구키 보관 파일이 저장되었습니다!', 'success');
            } else {
                alert(json.message || '저장되었습니다.');
            }
        } else {
            alert('저장 실패: ' + (json.error || '알 수 없는 오류'));
        }
    } catch (e) {
        alert('저장 중 오류가 발생했습니다: ' + e);
    }
}

function copyBitLockerKey() {
    if (cachedBitLockerData && cachedBitLockerData.recovery_key) {
        navigator.clipboard.writeText(cachedBitLockerData.recovery_key).then(() => {
            if (typeof showToast === 'function') {
                showToast('48자리 복구키가 클립보드에 복사되었습니다.', 'info');
            } else {
                alert('48자리 복구키가 클립보드에 복사되었습니다.');
            }
        });
    } else {
        alert('복구키가 직접 조회되지 않았습니다. 마이크로소프트 계정 링크에서 확인하세요.');
    }
}
