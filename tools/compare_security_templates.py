# -*- coding: utf-8 -*-
import os
from jinja2 import Environment, FileSystemLoader

lite_dir = r"C:\Users\kksjmj\.gemini\antigravity\worktrees\d31c01fc-bb7a-4958-af74-81c13a642b40\swarm-author-fluent-lite-9ef4d706\web\templates"
mod_dir = r"C:\Users\kksjmj\.gemini\antigravity\worktrees\d31c01fc-bb7a-4958-af74-81c13a642b40\swarm-author-fluent-mod-d0402a83\web\templates"

ctx = {
    "version": "2.17.60",
    "v_ts": 1234567,
    "initial_chart_html": '<div class="chart"></div>',
    "initial_chart_meta": "meta",
    "initial_chart_dates": "<span>dates</span>",
    "initial_recents_html": "<div>recents</div>",
    "initial_snapshots_json": "[]",
    "initial_total_snaps": "0",
    "initial_target_profile_name": "테스트 프로필",
    "initial_last_backup_time": "방금 전",
    "initial_copies_desc": "보관 정상"
}

required_elements = [
    'id="auth-modal-overlay"',
    'id="modal-auth-setup"',
    'id="modal-auth-login"',
    'id="modal-auth-change"',
    'id="setup-password"',
    'id="setup-password-confirm"',
    'id="login-password"',
    'id="change-current"',
    'id="change-new"',
    'id="change-new-confirm"',
    'id="header-worm-status-badge"',
    'id="dash-worm-status-tag"',
    'id="dash-worm-integrity-tag"',
    'backup_auth.js'
]

for name, tdir in [("author-fluent-lite", lite_dir), ("author-fluent-mod", mod_dir)]:
    env = Environment(loader=FileSystemLoader(tdir), autoescape=True)
    tmpl = env.get_template("index.html")
    rendered = tmpl.render(ctx)
    print(f"=== {name} ===")
    print(f"Rendered size: {len(rendered):,} bytes")
    missing = []
    for elem in required_elements:
        if elem not in rendered:
            missing.append(elem)
    if missing:
        print(f"  FAILED: Missing elements: {missing}")
    else:
        print("  PASSED: All 14 security DOM elements verified!")

print("Template security comparison complete.")
