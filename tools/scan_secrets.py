# -*- coding: utf-8 -*-
"""
tools/scan_secrets.py - 저장소 비밀 유출 스캐너

왜 이 파일이 있는가
------------------
2026-10-07, 공개 저장소 `kks3365550/BackupSystem` 의 git 이력을 전수 조사한
결과 실제 credential 5종이 공개된 상태였다.

    1. keys/release_ed25519.key (배포 서명 Ed25519 개인키, 지금 사용 중)
    2. data/auth_config.json    (마스터 비밀번호 PBKDF2 해시 + salt, 지금 사용 중)
    3. core/firebase_sync.py    (살아있는 Firebase API 키)
    4. backup/v2.9.10/keys/release_admin_cred.json (사내 계정)
    5. test_sandbox/.../backup_ed25519.key

전부 2026-08 이후 커밋에 들어갔고, 익명 `git clone` 으로 읽을 수 있었다.
`.gitignore` 는 추적만 막지 이력을 지우지 않는다.

이 스캐너는 그 원인을 자동 차단한다.

사용법
------
    python tools/scan_secrets.py                 # 추적 파일 검사
    python tools/scan_secrets.py --history       # 이력 전체 검사 (느림)
    python tools/scan_secrets.py --staged        # 스테이징된 파일만

종료 코드
--------
    0 : 비밀 없음
    1 : 발견 (CI 를 실패시킨다)
    2 : 오류
"""

import os
import re
import subprocess
import sys
import argparse

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 표준출력 인코딩을 UTF-8 로 고정한다.
#
# 2026-10-08, 이 스캐너의 첫 CI 실행이 UnicodeEncodeError 로 죽었다.
# windows-latest 러너의 표준출력은 cp1252 인데 한국어를 출력하려 했기 때문이다.
#   UnicodeEncodeError: 'charmap' codec can't encode characters
#
# exit code 1 이 "비밀 발견" 과 구분되지 않는 실패가 된다.
# 게이트가 조용히 통과한 것보다 나쁘다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 검사 대상 확장자 (텍스트로 볼 수 있는 것만)
TEXT_EXT = (
    ".py", ".json", ".bat", ".cmd", ".vbs", ".ps1", ".js", ".html",
    ".txt", ".md", ".yml", ".yaml", ".cfg", ".ini", ".env", ".sh",
)

# 검사에서 제외할 경로 (오탐이 많고 의미 없음)
SKIP_PATH = (
    ".venv/", "node_modules/", "site-packages/", "__pycache__/",
    "dist/", "build/", ".git/", "installer/runtime/",
)

# ── 탐지 규칙 ────────────────────────────────────────────────────────────
# (규칙 이름, 정규식, 심각도, 설명)
RULES = [
    (
        "pem_private_key",
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        "CRITICAL",
        "PEM 개인키. 공개 저장소에 있으면 서명 위조가 가능하다.",
    ),
    (
        "firebase_api_key",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        "CRITICAL",
        "Firebase 클라이언트 API 키. 프로젝트 식별자로 바로 쓰인다.",
    ),
    (
        "github_token",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        "CRITICAL",
        "GitHub personal access token.",
    ),
    (
        "aws_access_key",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        "CRITICAL",
        "AWS access key id.",
    ),
    (
        "slack_token",
        r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b",
        "CRITICAL",
        "Slack token.",
    ),
    (
        "password_literal",
        # JSON 은 "password": "값" 형태라 키와 콜론 사이에 따옴표가 들어간다.
        # 그걸 놓치면 .json 자격증명 파일을 통째로 통과한다.
        r"(?i)\b(?:password|passwd|pwd)\b[\"']?\s*[=:]\s*[\"'][^\"'\s]{6,}[\"']",
        "HIGH",
        "하드코딩된 비밀번호. 예외로 필요한지 확인해야 한다.",
    ),
    (
        "secret_literal",
        r"(?i)\b(?:api[_-]?key|apikey|secret|auth[_-]?token|client[_-]?secret)\b[\"']?\s*[=:]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']",
        "HIGH",
        "하드코딩된 키/토큰/시크릿.",
    ),
]

# 코드에서 흔한 무해한 패턴 (오탐 억제)
FALSE_POSITIVE_HINTS = (
    "example", "placeholder", "changeme", "your-", "xxx",
    "<", "dummy", "sample", "test_key", "redacted", "todo",
)

# 경로 기반 예외.
#
# 테스트 픽스처 암호("masterpassword123", "wrongpassword")는 실제 비밀이다.
# 하지만 'masterpassword' 라는 문자열은 auth 플로우를 검증하려면 반드시 필요하다.
# authenticate() 는 어떤 입력이든 받아들이므로, 테스트가 실제 암호를 쓸 수는 없다.
# 즉 tests/ 아래 password_literal 은 구조적으로 항상 오탐이다.
#
# 주의: 이 예외는 tests/ 에 실제 비밀을 커밋하는 우회를 만든다.
# tests/ 는 애초에 제품 배포물에 포함되지 않는다.
PATH_ALLOWLIST = (
    ("password_literal", ("tests/", "test_")),
    ("secret_literal", ("tests/", "test_")),
)

COMPILED = [(name, re.compile(pat), sev, desc) for name, pat, sev, desc in RULES]


def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, cwd=BASE_DIR)
    return r.stdout


def iter_tracked_files(rev=None):
    """추적 파일 목록. rev 가 있으면 그 커밋 트리에서."""
    if rev:
        out = git("ls-tree", "-r", "--name-only", rev)
    else:
        out = git("ls-files")
    for name in out.decode("utf-8", "replace").splitlines():
        name = name.strip()
        if not name:
            continue
        if any(s in name for s in SKIP_PATH):
            continue
        yield name


def read_blob(name, rev=None):
    if rev:
        return git("show", "%s:%s" % (rev, name))
    path = os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):
        return b""
    with open(path, "rb") as f:
        return f.read()


def is_allowlisted(rule, source):
    """경로 예외에 해당하는지 확인."""
    for r, prefixes in PATH_ALLOWLIST:
        if r != rule:
            continue
        # source 형식: "HEAD:tests/xxx.py" 또는 "1813a21:tests/xxx.py"
        path = source.split(":", 1)[1] if ":" in source else source
        for p in prefixes:
            if p.endswith("/"):
                if path.startswith(p):
                    return True
            elif p in os.path.basename(path):
                return True
    return False


def scan_text(text, source):
    hits = []
    for name, rx, sev, desc in COMPILED:
        for m in rx.finditer(text):
            start = max(0, m.start() - 60)
            ctx = text[start:m.end() + 60]
            low = ctx.lower()
            if any(h in low for h in FALSE_POSITIVE_HINTS):
                continue
            if is_allowlisted(name, source):
                continue
            line = text.count("\n", 0, m.start()) + 1
            hits.append({
                "rule": name,
                "severity": sev,
                "desc": desc,
                "source": source,
                "line": line,
                "match": m.group(0)[:90].replace("\n", " "),
            })
    return hits


def scan_files(files, label):
    findings = []
    count = 0
    for name in files:
        count += 1
        if not name.endswith(TEXT_EXT):
            continue
        blob = read_blob(name)
        if not blob or len(blob) > 2_000_000:
            continue
        if b"\x00" in blob[:4096]:
            continue
        text = blob.decode("utf-8", "replace")
        findings.extend(scan_text(text, "%s:%s" % (label, name)))
        if count % 200 == 0:
            sys.stdout.write("  ... %d개 검사\r" % count)
            sys.stdout.flush()
    sys.stdout.write(" " * 40 + "\r")
    return findings, count


def report(findings):
    if not findings:
        return 0

    sev_order = {"CRITICAL": 0, "HIGH": 1}
    findings.sort(key=lambda f: (sev_order.get(f["severity"], 9), f["source"]))

    print("=" * 78)
    print("비밀 스캔 실패: %d건 발견" % len(findings))
    print("=" * 78)

    current = None
    for f in findings:
        if f["severity"] != current:
            current = f["severity"]
            print()
            print("[%s]" % current)
        print("  %s" % f["source"])
        print("     규칙 : %s" % f["rule"])
        print("     설명 : %s" % f["desc"])
        print("     일치 : %s" % f["match"])
        print()

    print("-" * 78)
    print("조치")
    print("-" * 78)
    print("1. 위 값을 즉시 폐기/회전시킨다. git 으로는 지워지지 않는다.")
    print("2. 커밋한 사람이 본인 PC 에서 같은 값을 다 쓰고 있는지 확인한다.")
    print("3. 커밋을 되돌리려면 git filter-repo 가 필요하다 (docs/SECURITY_20261008.md)")
    return 1


def main():
    ap = argparse.ArgumentParser(description="저장소 비밀 스캐너")
    ap.add_argument("--history", action="store_true",
                    help="이력 전체 커밋 검사 (매우 느림)")
    ap.add_argument("--staged", action="store_true",
                    help="스테이징된 파일만 검사")
    ap.add_argument("--max-commits", type=int, default=0,
                    help="--history 에서 검사할 최대 커밋 수 (0 = 전체)")
    args = ap.parse_args()

    os.chdir(BASE_DIR)

    if args.history:
        # 커밋별로 '그 커밋이 추가/변경한 파일만' 검사한다.
        #
        # 전체 트리를 커밋마다 훑으면 O(커밋 x 파일)이라 느리고,
        # max-commits 로 자르면 유출이 잘려 통과해버린다.
        # 통과하는 스캔은 스캔 없는 것보다 나쁘다 (2026-10-08 확인).
        # 변경 파일만 보��면 커밋 수와 무관하게 완전하고 빠르다.
        raw = git(
            "log", "--all",
            "--name-only", "--diff-filter=AM",
            "--pretty=format:@@%H",
        ).decode("utf-8", "replace")

        # @@<sha>\n<file>\n<file>... 형태를 파싱
        commits = []
        current = None
        for line in raw.splitlines():
            line = line.rstrip()
            if line.startswith("@@"):
                current = [line[2:].strip(), []]
                commits.append(current)
            elif line.strip() and current is not None:
                current[1].append(line.strip())

        if args.max_commits:
            commits = commits[: args.max_commits]

        print("이력 검사: %d개 커밋 (변경 파일만)" % len(commits))
        all_findings = []
        for i, (rev, files) in enumerate(commits):
            files = [f for f in files if f and not any(s in f for s in SKIP_PATH)]
            for name in files:
                if not name.endswith(TEXT_EXT):
                    continue
                blob = read_blob(name, rev)
                if not blob or len(blob) > 2_000_000:
                    continue
                if b"\x00" in blob[:4096]:
                    continue
                text = blob.decode("utf-8", "replace")
                all_findings.extend(scan_text(text, "%s:%s" % (rev[:7], name)))
            if i % 50 == 0:
                sys.stdout.write("  ... %d/%d\r" % (i, len(commits)))
                sys.stdout.flush()
        sys.stdout.write(" " * 40 + "\r")
        findings = all_findings
    elif args.staged:
        names = git("diff", "--cached", "--name-only").decode("utf-8", "replace").splitlines()
        print("스테이징 검사: %d개 파일" % len(names))
        findings, _ = scan_files([n for n in names if n.strip()], "staged")
    else:
        files = list(iter_tracked_files())
        print("추적 파일 검사: %d개" % len(files))
        findings, _ = scan_files(files, "HEAD")

    return report(findings)


if __name__ == "__main__":
    sys.exit(main())