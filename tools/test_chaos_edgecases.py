import json
import subprocess
import sys
import time
import os

def agent_browser(session, args):
    cmd = ['cmd.exe', '/c', 'agent-browser', '--session', session] + args
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = p.stdout.strip()
    lines = [l for l in out.splitlines() if not l.startswith('⚠') and not l.startswith('[agent-browser]') and not l.startswith('✓')]
    return p.returncode, '\n'.join(lines).strip(), p.stderr.strip()

def eval_js_code(session, js_code):
    # Ensure no raw newlines in command line arg
    clean_code = ' '.join(line.strip() for line in js_code.splitlines() if line.strip())
    ret, out, err = agent_browser(session, ['eval', clean_code])
    if out.startswith('"') and out.endswith('"'):
        try:
            out = json.loads(out)
        except Exception:
            pass
    try:
        return json.loads(out)
    except Exception:
        return out

def run_chaos_viewport_suite(html_path, session_name):
    print(f"\n=======================================================")
    print(f"  [CHAOS-1] RESPONSIVE VIEWPORT STRESS TEST (360px ~ 4K)")
    print(f"  Target: {os.path.basename(html_path)}")
    print(f"=======================================================")

    file_url = f"file:///{html_path.replace('\\', '/')}"
    agent_browser(session_name, ['open', file_url])
    time.sleep(1)

    viewports = [
        ("Mobile Narrow (360px)", 360, 640),
        ("Mobile Standard (414px)", 414, 896),
        ("Tablet Portrait (768px)", 768, 1024),
        ("Tablet Landscape (1024px)", 1024, 768),
        ("Desktop FHD (1920px)", 1920, 1080),
        ("Desktop 4K UHD (3840px)", 3840, 2160)
    ]

    all_passed = True
    results = []

    for label, w, h in viewports:
        agent_browser(session_name, ['set', 'viewport', str(w), str(h)])
        time.sleep(0.3)

        check_js = """
        (function() {
            var doc = document.documentElement;
            var body = document.body;
            var docOverflow = doc.scrollWidth > window.innerWidth + 2;
            var bodyOverflow = body.scrollWidth > window.innerWidth + 2;

            var overflowing = [];
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                var r = el.getBoundingClientRect();
                if (r.width > 0 && r.right > window.innerWidth + 3) {
                    overflowing.push({
                        tag: el.tagName,
                        id: el.id || '',
                        cls: (el.className || '').toString().slice(0, 30),
                        right: Math.round(r.right),
                        w: Math.round(r.width)
                    });
                }
            }

            var startBtn = document.getElementById('btn-start') || document.getElementById('quick-backup-start-btn');
            var startBtnVisible = startBtn ? (startBtn.offsetWidth > 0 && startBtn.offsetHeight > 0) : false;

            return JSON.stringify({
                w: window.innerWidth,
                h: window.innerHeight,
                docScrollWidth: doc.scrollWidth,
                docOverflow: docOverflow,
                bodyOverflow: bodyOverflow,
                overflowCount: overflowing.length,
                samples: overflowing.slice(0, 2),
                startBtnVisible: startBtnVisible
            });
        })()
        """
        res = eval_js_code(session_name, check_js)
        if isinstance(res, dict):
            doc_over = res.get('docOverflow', False)
            over_cnt = res.get('overflowCount', 0)
            btn_vis = res.get('startBtnVisible', False)
            passed = (not doc_over) and (over_cnt == 0) and btn_vis
            if not passed:
                all_passed = False
            status_str = "PASS" if passed else "FAIL"
            print(f"[{status_str}] {label:<24} ({w}x{h}): ScrollWidth={res.get('docScrollWidth')}px, "
                  f"OverflowItems={over_cnt}, StartBtnVisible={btn_vis}")
            if over_cnt > 0:
                print(f"   -> Samples: {res.get('samples')}")
            results.append((label, passed, res))
        else:
            print(f"[FAIL] {label} failed to eval: {res}")
            all_passed = False

    agent_browser(session_name, ['close'])
    return all_passed, results


def run_offline_and_error_suite(html_path, session_name):
    print(f"\n=======================================================")
    print(f"  [CHAOS-2] BACKEND OFFLINE & API TIMEOUT TEST")
    print(f"  Target: {os.path.basename(html_path)}")
    print(f"=======================================================")

    file_url = f"file:///{html_path.replace('\\', '/')}"
    agent_browser(session_name, ['open', file_url])
    time.sleep(1)

    test_js = """
    (function() {
        var results = {};

        // 1. Friendly Error Guidance Verification
        if (typeof friendlyError === 'function') {
            var fetchErr = new TypeError('Failed to fetch');
            var timeoutErr = new Error('Network timeout after 30000ms');
            var authErr = new Error('401 Unauthorized');
            var genericErr = new Error('Database locked');

            results.msgFetch = friendlyError(fetchErr);
            results.msgTimeout = friendlyError(timeoutErr);
            results.msgAuth = friendlyError(authErr);
            results.msgGeneric = friendlyError(genericErr);

            results.hasFriendlyOfflineGuidance = results.msgFetch.indexOf('연결이 끊겼') !== -1 || results.msgFetch.indexOf('확인한 뒤') !== -1;
            results.hasFriendlyTimeoutGuidance = results.msgTimeout.indexOf('오래 걸려') !== -1 || results.msgTimeout.indexOf('잠시 후') !== -1;
            results.hasFriendlyAuthGuidance = results.msgAuth.indexOf('비밀번호') !== -1 || results.msgAuth.indexOf('권한') !== -1;
        } else {
            results.friendlyErrorDefined = false;
        }

        // 2. Theme Switching Resilience
        var themeFn = (typeof changeFluentTheme === 'function') ? changeFluentTheme : ((typeof changeTheme === 'function') ? changeTheme : null);
        if (themeFn) {
            themeFn('dark');
            var isDark = document.documentElement.classList.contains('dark');
            themeFn('light');
            var isLight = !document.documentElement.classList.contains('dark') || document.documentElement.classList.contains('light');
            try {
                themeFn(null);
                themeFn('unknown_invalid_theme');
                results.themeChaosSurvives = true;
            } catch (e) {
                results.themeChaosSurvives = false;
            }
            results.themeDarkWorks = isDark;
            results.themeLightWorks = isLight;
        }

        // 3. Tab Navigation Resilience
        if (typeof switchTab === 'function') {
            try {
                switchTab('dashboard');
                switchTab('snapshots');
                switchTab('settings');
                switchTab('runner');
                switchTab('dashboard');
                results.tabSwitchingClean = true;
            } catch(e) {
                results.tabSwitchingClean = false;
                results.tabSwitchError = e.message;
            }
        }

        // 4. Mode Selection Resilience
        if (typeof selectFluentMode === 'function' || typeof selectMode === 'function') {
            var mFn = typeof selectFluentMode === 'function' ? selectFluentMode : selectMode;
            try {
                mFn('smart');
                mFn('full');
                mFn('custom');
                mFn('smart');
                results.modeSelectionClean = true;
            } catch(e) {
                results.modeSelectionClean = false;
                results.modeError = e.message;
            }
        }

        return JSON.stringify(results);
    })()
    """
    res = eval_js_code(session_name, test_js)
    agent_browser(session_name, ['close'])

    print("Offline & Fault-Tolerance Audit Results:")
    if isinstance(res, dict):
        for k, v in res.items():
            print(f"  * {k}: {v}")
    else:
        print(f"  Raw: {res}")

    return res

if __name__ == '__main__':
    lite_path = r'C:\Users\kksjmj\Desktop\ai\백업시스템\test_rendered_lite.html'
    mod_path = r'C:\Users\kksjmj\Desktop\ai\백업시스템\test_rendered_mod.html'

    print("=== STARTING FULL VERIFIER-CHAOS TEST SUITE ===")
    
    # 1. Candidate A (author-fluent-lite)
    print("\n-----------------------------------------------------------")
    print(">>> AUDITING CANDIDATE A (author-fluent-lite)")
    print("-----------------------------------------------------------")
    ok_l, _ = run_chaos_viewport_suite(lite_path, 'v_lite')
    res_l = run_offline_and_error_suite(lite_path, 'e_lite')

    # 2. Candidate B (author-fluent-mod)
    print("\n-----------------------------------------------------------")
    print(">>> AUDITING CANDIDATE B (author-fluent-mod)")
    print("-----------------------------------------------------------")
    ok_m, _ = run_chaos_viewport_suite(mod_path, 'v_mod')
    res_m = run_offline_and_error_suite(mod_path, 'e_mod')

    print("\n===========================================================")
    print("FINAL SUMMARY:")
    print(f"Candidate A (Lite) Viewports: {'PASS' if ok_l else 'FAIL'}")
    print(f"Candidate B (Mod) Viewports:  {'PASS' if ok_m else 'FAIL'}")
    print("===========================================================")
