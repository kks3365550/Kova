// web/static/js/rescue_usb.js - 긴급 재해 복구(Rescue DR) UEFI 부팅 USB 제작 프론트엔드 모듈

let rescueUsbPollTimer = null;
let cachedRescueTargets = [];

async function openRescueUsbModal() {
    openModal('rescue-usb-modal');
    await loadRescueUsbTargets();
    checkRescueUsbStatus();
}

async function loadRescueUsbTargets() {
    const select = document.getElementById('rescue-usb-select');
    const infoBox = document.getElementById('rescue-usb-target-info');
    const startBtn = document.getElementById('btn-start-make-usb');
    const scanBadge = document.getElementById('rescue-usb-scan-badge');

    if (!select) return;

    select.innerHTML = '<option value="">USB 드라이브 검색 중...</option>';
    select.disabled = true;
    if (startBtn) startBtn.disabled = true;
    if (scanBadge) scanBadge.textContent = '스캔 중...';

    try {
        const res = await fetch('/api/rescue/usb-targets');
        const data = await res.json();

        select.innerHTML = '';

        cachedRescueTargets = data.targets || [];

        if (!data.success || !data.targets || data.targets.length === 0) {
            select.innerHTML = '<option value="">감지된 이동식 USB가 없습니다</option>';
            select.disabled = true;
            if (scanBadge) scanBadge.textContent = 'USB 없음';
            if (infoBox) {
                infoBox.innerHTML = `
                    <div class="p-3 rounded-lg bg-amber-950/40 border border-amber-800/50 text-xs text-amber-300 flex items-start gap-2">
                        <i data-lucide="alert-circle" class="w-4 h-4 shrink-0 mt-0.5 text-amber-400"></i>
                        <div>
                            <div class="font-bold">연결된 이동식 USB를 찾지 못했습니다.</div>
                            <div class="text-[11px] text-amber-400/80 mt-0.5">PC에 8GB 이상의 USB 드라이브를 연결한 후 [새로고침] 버튼을 눌러주세요.</div>
                        </div>
                    </div>
                `;
                if (window.lucide) lucide.createIcons();
            }
            return;
        }

        data.targets.forEach((t, idx) => {
            const opt = document.createElement('option');
            opt.value = t.DriveLetter;
            opt.textContent = `${t.DriveLetter} [${t.Label}] (${t.SizeGB} GB, ${t.FileSystem}) - 여유: ${t.FreeGB} GB`;
            if (idx === 0) opt.selected = true;
            select.appendChild(opt);
        });

        select.disabled = false;
        if (scanBadge) scanBadge.textContent = `${data.targets.length}개 감지됨`;
        if (startBtn) startBtn.disabled = false;

        updateSelectedUsbInfo(data.targets[0], data.iso_ready);

        select.onchange = () => {
            const chosen = data.targets.find(x => x.DriveLetter === select.value) || data.targets[0];
            updateSelectedUsbInfo(chosen, data.iso_ready);
        };

    } catch (e) {
        select.innerHTML = '<option value="">스캔 실패</option>';
        if (scanBadge) scanBadge.textContent = '오류';
        console.error('USB 스캔 오류:', e);
    }
}

function updateSelectedUsbInfo(target, isoReady) {
    const infoBox = document.getElementById('rescue-usb-target-info');
    if (!infoBox || !target) return;

    const fsColor = target.IsFat32 ? 'text-emerald-400' : 'text-amber-400';
    const fsNotice = target.IsFat32 ? 'UEFI 부팅 표준 규격(FAT32)' : 'FAT32 권장 (UEFI 전용)';
    const isoBadge = isoReady ? 
        '<span class="text-emerald-400 font-semibold">준비 완료 (D:\\Rufus\\Win11 ISO)</span>' :
        '<span class="text-amber-400 font-semibold">ISO 경로 확인 필요</span>';

    infoBox.innerHTML = `
        <div class="p-3.5 rounded-xl bg-zinc-950/80 border border-zinc-800/80 space-y-2 text-xs">
            <div class="flex items-center justify-between pb-2 border-b border-zinc-800/60 font-mono">
                <span class="text-zinc-400">선택된 대상 드라이브:</span>
                <span class="text-white font-bold text-sm text-blue-400">${target.DriveLetter} (${target.Label})</span>
            </div>
            <div class="grid grid-cols-2 gap-2 text-[11px] font-mono">
                <div>파일시스템: <b class="${fsColor}">${target.FileSystem}</b> (${fsNotice})</div>
                <div>전체 / 여유 용량: <b class="text-zinc-200">${target.SizeGB} GB / ${target.FreeGB} GB</b></div>
                <div class="col-span-2">Windows 11 순정 소스: ${isoBadge}</div>
            </div>
            <div class="text-[11px] text-zinc-400 leading-relaxed bg-zinc-900/60 p-2 rounded-lg border border-zinc-800/50">
                🛡️ <b>안전 보장</b>: 시스템 C/D 드라이브는 절대 건드리지 않으며, 오직 선택된 <b>${target.DriveLetter}</b> 에만 
                UEFI 부트로더, WinPE 램디스크(boot.wim), 자동 실행 스크립트, 무설치 Python 엔진을 안전하게 복사 및 주입합니다.
            </div>
        </div>
    `;
    if (window.lucide) lucide.createIcons();
}

async function startRescueUsbCreation() {
    const select = document.getElementById('rescue-usb-select');
    const driveLetter = select ? select.value : '';

    if (!driveLetter) {
        alert('대상 USB 드라이브를 먼저 선택해주세요.');
        return;
    }

    const chosen = cachedRescueTargets.find(x => x.DriveLetter === driveLetter);
    let autoFormat = false;

    if (chosen && !chosen.IsFat32) {
        const msg = `[⚠️ FAT32 포맷 필요 안내]\n\n선택하신 ${driveLetter} 드라이브는 현재 [${chosen.FileSystem}] 포맷입니다.\nUEFI 표준 부팅 USB로 동작하려면 반드시 [FAT32] 포맷이어야 합니다.\n\n지금 ${driveLetter} 드라이브를 FAT32로 포맷하고 복구 USB 제작을 계속 진행하시겠습니까?\n\n🚨 주의: 포맷 시 ${driveLetter} 드라이브 안의 모든 기존 파일이 완전히 삭제됩니다!`;
        if (!confirm(msg)) {
            return;
        }
        autoFormat = true;
    } else {
        if (!confirm(`[확인] ${driveLetter} 드라이브로 긴급 재해 복구 UEFI 부팅 USB를 제작하시겠습니까?\n\n- 소요 시간: 약 30초~1분\n- 부팅 시 0초 만에 복구 콘솔이 자동 실행되도록 구성됩니다.`)) {
            return;
        }
    }

    const startBtn = document.getElementById('btn-start-make-usb');
    if (startBtn) {
        startBtn.disabled = true;
        startBtn.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i> <span>제작 시작 중...</span>';
        if (window.lucide) lucide.createIcons();
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
                startBtn.innerHTML = '<i data-lucide="play" class="w-4 h-4 fill-current"></i> <span>긴급 복구 USB 제작 시작</span>';
                if (window.lucide) lucide.createIcons();
            }
            return;
        }

        // 실시간 모니터링 폴링 시작
        startRescueUsbPolling();

    } catch (e) {
        alert('서버 통신 실패: ' + friendlyError(e));
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.innerHTML = '<i data-lucide="play" class="w-4 h-4 fill-current"></i> <span>긴급 복구 USB 제작 시작</span>';
            if (window.lucide) lucide.createIcons();
        }
    }
}

function startRescueUsbPolling() {
    if (rescueUsbPollTimer) clearInterval(rescueUsbPollTimer);
    checkRescueUsbStatus();
    rescueUsbPollTimer = setInterval(checkRescueUsbStatus, 1000);
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
        const select = document.getElementById('rescue-usb-select');

        if (!st || st.phase === 'IDLE') {
            if (progressContainer) progressContainer.classList.add('hidden');
            return;
        }

        if (progressContainer) progressContainer.classList.remove('hidden');
        if (progressBar) progressBar.style.width = `${st.progress}%`;
        if (progressPct) progressPct.textContent = `${st.progress}%`;
        if (phaseText) phaseText.textContent = st.phase_text || '진행 중...';

        if (select && st.is_running) select.disabled = true;

        if (consoleBox && st.log) {
            consoleBox.innerHTML = st.log.map(line => `<div>${escapeHtml(line)}</div>`).join('');
            consoleBox.scrollTop = consoleBox.scrollHeight;
        }

        if (startBtn) {
            if (st.is_running) {
                startBtn.disabled = true;
                startBtn.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i> <span>제작 진행 중...</span>';
            } else if (st.phase === 'COMPLETED') {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i data-lucide="check-circle" class="w-4 h-4 text-emerald-400"></i> <span>제작 완료 (다시 제작)</span>';
            } else if (st.phase === 'ERROR') {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i data-lucide="alert-triangle" class="w-4 h-4 text-red-400"></i> <span>오류 발생 (다시 시도)</span>';
            }
            if (window.lucide) lucide.createIcons();
        }

        // 제작 완료 감지
        if (!st.is_running && st.phase === 'COMPLETED') {
            if (rescueUsbPollTimer) {
                clearInterval(rescueUsbPollTimer);
                rescueUsbPollTimer = null;
            }
            // 완료 성공 축하 및 안내 모달 자동 팝업
            setTimeout(() => {
                openRescueGuideModal();
            }, 600);
        }

        if (!st.is_running && st.phase === 'ERROR') {
            if (rescueUsbPollTimer) {
                clearInterval(rescueUsbPollTimer);
                rescueUsbPollTimer = null;
            }
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
                <div class="p-3.5 rounded-xl bg-zinc-900/80 border border-zinc-800/80 space-y-1">
                    <div class="flex items-center gap-2">
                        <span class="w-6 h-6 rounded-lg bg-blue-600/30 text-blue-400 font-bold text-xs flex items-center justify-center font-mono">${s.step}</span>
                        <span class="text-xs font-bold text-zinc-100">${escapeHtml(s.title)}</span>
                    </div>
                    <p class="text-[11px] text-zinc-400 pl-8 leading-relaxed">${escapeHtml(s.desc)}</p>
                </div>
            `).join('');
        }

        if (keysContainer && guide.boot_keys) {
            keysContainer.innerHTML = guide.boot_keys.map(k => `
                <div class="flex items-center justify-between p-2 rounded-lg bg-zinc-950/70 border border-zinc-800/60 text-xs font-mono">
                    <span class="text-zinc-300 font-medium">${escapeHtml(k.brand)}</span>
                    <span class="px-2 py-0.5 rounded bg-blue-950/60 text-blue-300 border border-blue-800/50 font-bold">${escapeHtml(k.key)}</span>
                </div>
            `).join('');
        }

        if (window.lucide) lucide.createIcons();

    } catch (e) {
        console.error('가이드 로드 실패:', e);
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>"']/g, function(m) {
        switch (m) {
            case '&': return '&amp;';
            case '<': return '&lt;';
            case '>': return '&gt;';
            case '"': return '&quot;';
            case "'": return '&#39;';
            default: return m;
        }
    });
}
