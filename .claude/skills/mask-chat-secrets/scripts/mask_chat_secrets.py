# -*- coding: utf-8 -*-
"""
mask_chat_secrets.py - Claude Code / Antigravity 대화 기록 속 평문 비밀 마스킹

대화 기록(jsonl, md, pb 등)에 평문으로 남은 API 키, 토큰, 비밀번호를 찾아
같은 길이의 '*' 로 덮어쓴다. 길이를 유지하므로 JSON 줄이 깨지지 않고,
Antigravity 의 .pb(protobuf) 같은 바이너리도 길이 필드가 어긋나지 않는다.

기본은 미리보기(dry-run)다. 실제로 바꾸려면 --apply 를 붙인다.

    python mask_chat_secrets.py                     # 기본 위치 미리보기
    python mask_chat_secrets.py --apply             # 기본 위치 실제 마스킹
    python mask_chat_secrets.py --apply --literal "내비번123"   # 정확한 값도 지정
    python mask_chat_secrets.py D:/some/folder --apply          # 경로 직접 지정

종료 코드: 0 = 발견 없음 / 1 = 발견(또는 마스킹함) / 2 = 오류
"""

import argparse
import datetime
import os
import re
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", "")

# ── 기본 검사 위치 ───────────────────────────────────────────────────────
DEFAULT_ROOTS = [
    # Claude Code
    os.path.join(HOME, ".claude", "projects"),        # 세션 대화 jsonl
    os.path.join(HOME, ".claude", "history.jsonl"),   # 입력 프롬프트 기록
    os.path.join(HOME, ".claude", "file-history"),
    os.path.join(HOME, ".claude", "paste-cache"),
    os.path.join(HOME, ".claude", "shell-snapshots"),
    os.path.join(HOME, ".claude", "todos"),
    os.path.join(HOME, ".claude", "sessions"),
    # Antigravity
    os.path.join(HOME, ".gemini", "antigravity", "brain"),          # 작업 문서 md
    os.path.join(HOME, ".gemini", "antigravity", "conversations"),  # 대화 pb
    os.path.join(HOME, ".gemini", "antigravity", "implicit"),
    os.path.join(HOME, ".gemini", "antigravity", "code_tracker"),
]
if APPDATA:
    DEFAULT_ROOTS.append(os.path.join(APPDATA, "Antigravity", "logs"))

# 지금 쓰고 있는 설정/인증 파일. 마스킹하면 도구가 동작하지 않으므로 절대 건드리지 않는다.
PROTECTED_NAMES = {
    "settings.json", "settings.local.json", ".credentials.json", "credentials.json",
    "oauth_creds.json", "google_accounts.json", "mcp_config.json", ".mcp.json",
    "config.json", ".env", "installation_id", "user_id",
}

TEXT_EXT = {
    ".jsonl", ".json", ".md", ".txt", ".log", ".yaml", ".yml", ".sh", ".ps1",
    ".bat", ".cmd", ".py", ".js", ".ts", ".html", ".ini", ".cfg", ".toml", "",
}
BINARY_EXT = {".pb"}
DB_EXT = {".vscdb", ".db", ".sqlite"}   # --include-db 일 때만
MAX_SIZE = 200 * 1024 * 1024

MASK = "*"

# ── 탐지 규칙 ───────────────────────────────────────────────────────────
# (이름, 정규식, 앞에서 남길 글자 수)
# 정규식에 'v' 그룹이 있으면 그 그룹만 마스킹하고, 없으면 일치 전체를 마스킹한다.
# JSON 안에서는 따옴표가 \" 로 들어있으므로 값에서 \ " ' 는 끊는다.
VAL = r"(?P<v>[^\s\"'\\`,;}\]<>]{%d,})"

RULES = [
    ("pem_private_key",
     r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----(?P<v>.{20,}?)-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", 0),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}", 10),
    ("openai_key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}", 6),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}", 6),
    ("google_oauth_secret", r"\bGOCSPX-[A-Za-z0-9_\-]{20,}", 7),
    ("google_oauth_token", r"\bya29\.[A-Za-z0-9_\-]{20,}", 5),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", 4),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}", 6),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 4),
    ("slack_token", r"\bxox[abposr]-[0-9A-Za-z\-]{10,}", 5),
    ("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}", 8),
    ("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}", 3),
    ("telegram_bot_token", r"\b\d{8,10}:AA[0-9A-Za-z_\-]{33}\b", 0),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", 3),
    ("url_password", r"\b[a-z][a-z0-9+.\-]*://[^\s:/@\"'\\]+:(?P<v>[^\s@/\"'\\]{3,})@", 0),
    ("bearer_token", r"(?i)\bbearer\s+(?P<v>[A-Za-z0-9_\-.=+/]{20,})", 0),
    # 키=값 형태 (영문 키). "password": "x", password=x, \"api_key\": \"x\" 모두.
    ("password_kv",
     r"(?i)\b(?:password|passwd|passphrase|pwd|pass|db_pass(?:word)?)\b\\?[\"']?\s*[:=]\s*\\?[\"']?" + VAL % 4, 0),
    ("secret_kv",
     r"(?i)\b(?:api[_\-]?key|apikey|secret(?:[_\-]?key)?|client[_\-]?secret|access[_\-]?token|"
     r"refresh[_\-]?token|auth[_\-]?token|private[_\-]?key|token)\b\\?[\"']?\s*[:=]\s*\\?[\"']?" + VAL % 12, 0),
    # 한국어 대화체: "비밀번호는 abcd1234", "비번: qwer!", "암호 = xxx"
    ("password_ko",
     r"(?:비밀\s?번호|비번|패스워드|암호|마스터\s?키|API\s?키|api\s?키|토큰)\s*(?:는|은|:|=|->|→)\s*\\?[\"'`]?" + VAL % 4, 0),
]

# 값이 이런 것이면 실제 비밀이 아닐 가능성이 높다 (오탐 억제)
PLACEHOLDER_HINTS = (
    "example", "placeholder", "changeme", "your", "xxxx", "dummy", "sample",
    "redacted", "masked", "<", "${", "{{", "process.env", "os.environ", "getenv",
    "none", "null", "true", "false", "undefined", "required", "string",
    "password", "secret", "token", "****",
)


def compile_rules(literals):
    compiled = [(n, re.compile(p, re.DOTALL if n == "pem_private_key" else 0), k)
                for n, p, k in RULES]
    for lit in literals:
        if lit:
            compiled.append(("literal", re.compile(re.escape(lit)), 0))
    return compiled


def looks_placeholder(value):
    low = value.lower()
    if set(value) <= {"*", "x", "X", ".", "-", "_"}:
        return True
    return any(h in low for h in PLACEHOLDER_HINTS)


def make_mask(value, keep, binary):
    """같은 길이(바이너리는 같은 바이트 수)의 마스크."""
    prefix = value[:keep]
    rest = value[keep:]
    # 개행과 JSON 개행 이스케이프(\\n)는 남겨서 줄 구조를 유지한다 (PEM 블록용)
    def one(m):
        if m.group(1):
            return m.group(1)
        ch = m.group(0)
        return MASK * (len(ch.encode("utf-8", "surrogateescape")) if binary else 1)
    return prefix + re.sub(r"(\\n|\n|\r)|.", one, rest, flags=re.DOTALL)


def mask_text(text, rules, binary):
    """text 를 마스킹한 결과와 발견 목록을 돌려준다."""
    hits = []
    for name, rx, keep in rules:
        def repl(m, name=name, keep=keep):
            if "v" in rx.groupindex and m.group("v") is not None:
                s, e = m.span("v")
                k = 0
            else:
                s, e = m.span()
                k = keep
            value = m.string[s:e]
            if name != "literal" and looks_placeholder(value):
                return m.group(0)
            masked = make_mask(value, k, binary)
            if masked == value:
                return m.group(0)
            line = m.string.count("\n", 0, s) + 1
            preview = (value[:k] + "…(%d자)" % len(value)) if k else "(%d자)" % len(value)
            hits.append((name, line, preview))
            ms, me = m.span()
            return m.string[ms:s] + masked + m.string[e:me]
        text = rx.sub(repl, text)
    return text, hits


def iter_files(roots, include_db):
    exts = TEXT_EXT | BINARY_EXT | (DB_EXT if include_db else set())
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in (".git", "node_modules", "plugins", "skills")]
            for fn in fns:
                if fn in PROTECTED_NAMES:
                    continue
                if ".bak-mask" in fn:
                    continue
                if os.path.splitext(fn)[1].lower() in exts:
                    yield os.path.join(dp, fn)


def process_file(path, rules, apply, backup_dir):
    if os.path.basename(path) in PROTECTED_NAMES:
        return []
    try:
        if os.path.getsize(path) > MAX_SIZE:
            return []
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        print("  [건너뜀] %s (%s)" % (path, e))
        return []

    ext = os.path.splitext(path)[1].lower()
    binary = ext in BINARY_EXT or ext in DB_EXT or b"\x00" in raw[:8192]
    text = raw.decode("utf-8", "surrogateescape")
    new_text, hits = mask_text(text, rules, binary)
    if not hits:
        return []

    if apply:
        new_raw = new_text.encode("utf-8", "surrogateescape")
        if binary and len(new_raw) != len(raw):
            print("  [중단] 길이가 달라져 바이너리를 쓰지 않음: %s" % path)
            return hits
        if backup_dir:
            rel = os.path.splitdrive(os.path.abspath(path))[1].lstrip("\\/")
            dst = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
        tmp = path + ".tmp-mask"
        with open(tmp, "wb") as f:
            f.write(new_raw)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    return hits


def main():
    ap = argparse.ArgumentParser(description="대화 기록 평문 비밀 마스킹")
    ap.add_argument("paths", nargs="*", help="검사할 파일/폴더 (생략 시 Claude Code·Antigravity 기본 위치)")
    ap.add_argument("--apply", action="store_true", help="실제로 마스킹 (생략 시 미리보기)")
    ap.add_argument("--literal", action="append", default=[], help="정확히 이 값도 마스킹 (여러 번 가능)")
    ap.add_argument("--literal-file", help="마스킹할 값을 한 줄에 하나씩 적은 파일 (다 쓰면 지울 것)")
    ap.add_argument("--no-backup", action="store_true", help="원본 백업을 만들지 않음")
    ap.add_argument("--backup-dir", help="원본 백업 위치 (기본: ~/.mask-chat-secrets-backup/<시각>)")
    ap.add_argument("--include-db", action="store_true",
                    help="Antigravity state.vscdb 같은 DB 파일도 포함 (앱을 반드시 종료한 뒤)")
    args = ap.parse_args()

    literals = list(args.literal)
    if args.literal_file:
        with open(args.literal_file, encoding="utf-8") as f:
            literals += [l.rstrip("\r\n") for l in f if l.strip()]
    literals = [l for l in literals if len(l) >= 4]

    roots = args.paths or DEFAULT_ROOTS
    rules = compile_rules(literals)

    backup_dir = None
    if args.apply and not args.no_backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = args.backup_dir or os.path.join(HOME, ".mask-chat-secrets-backup", stamp)

    print("모드: %s" % ("마스킹 적용" if args.apply else "미리보기 (변경 없음)"))
    print("검사 위치:")
    for r in roots:
        print("  %s %s" % ("O" if os.path.exists(r) else "-", r))
    print()

    total_files = 0
    hit_files = 0
    by_rule = {}
    for path in iter_files(roots, args.include_db):
        total_files += 1
        hits = process_file(path, rules, args.apply, backup_dir)
        if not hits:
            continue
        hit_files += 1
        print(path)
        for name, line, preview in hits[:20]:
            by_rule[name] = by_rule.get(name, 0) + 1
            print("    %d행  %-20s %s" % (line, name, preview))
        for name, _, _ in hits[20:]:
            by_rule[name] = by_rule.get(name, 0) + 1
        if len(hits) > 20:
            print("    ... 외 %d건" % (len(hits) - 20))

    print()
    print("=" * 60)
    print("검사 파일 %d개 / 발견 파일 %d개 / 발견 %d건"
          % (total_files, hit_files, sum(by_rule.values())))
    for name, n in sorted(by_rule.items(), key=lambda x: -x[1]):
        print("  %-22s %d" % (name, n))
    if hit_files and not args.apply:
        print("\n미리보기였습니다. 실제로 마스킹하려면 --apply 를 붙여 다시 실행하세요.")
    if hit_files and args.apply:
        if backup_dir:
            print("\n원본 백업: %s" % backup_dir)
            print("  ※ 백업에도 평문 비밀이 그대로 있습니다. 확인 후 반드시 삭제하세요.")
        print("\n※ 마스킹은 기록만 지웁니다. 노출된 키/비밀번호는 발급처에서 폐기·교체하세요.")
    return 1 if hit_files else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
