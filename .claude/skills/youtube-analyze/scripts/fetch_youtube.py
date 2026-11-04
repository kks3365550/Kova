#!/usr/bin/env python3
"""YouTube 영상의 메타데이터와 자막을 받아 분석용 텍스트로 저장한다 (yt-dlp 사용)."""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

LANG_PREF = ["ko", "en"]


def run_ytdlp(args):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def pick_lang(info):
    """수동 자막 우선, 없으면 자동 자막. ko → en → 첫 번째."""
    for kind in ("subtitles", "automatic_captions"):
        langs = list((info.get(kind) or {}).keys())
        if not langs:
            continue
        for pref in LANG_PREF:
            for lang in langs:
                if lang == pref or lang.startswith(pref + "-"):
                    return kind, lang
        if kind == "subtitles":
            return kind, langs[0]
    return None, None


def vtt_to_text(vtt):
    """VTT를 '[mm:ss] 문장' 형태로 바꾸고, 자동자막의 반복 줄을 없앤다."""
    out, last, stamp = [], None, None
    for line in vtt.splitlines():
        m = re.match(r"(\d+):(\d\d):(\d\d)\.\d+\s+-->", line) or re.match(r"(\d\d):(\d\d)\.\d+\s+-->", line)
        if m:
            g = [int(x) for x in m.groups()]
            secs = g[0] * 3600 + g[1] * 60 + g[2] if len(g) == 3 else g[0] * 60 + g[1]
            stamp = f"[{secs // 60:02d}:{secs % 60:02d}]"
            continue
        if not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.strip().isdigit():
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text and text != last:
            out.append(f"{stamp} {text}" if stamp else text)
            last = text
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="yt_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    r = run_ytdlp(["-J", a.url])
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        if re.search(r"403|CONNECT tunnel|Name or service not known|getaddrinfo|Unable to connect", r.stderr):
            sys.stderr.write("\n[네트워크 차단] 환경 설정에서 www.youtube.com, youtu.be, i.ytimg.com, *.googlevideo.com 허용 필요\n")
        sys.exit(2)
    info = json.loads(r.stdout)

    meta = {k: info.get(k) for k in ("id", "title", "channel", "upload_date", "duration_string",
                                      "view_count", "like_count", "description", "tags", "webpage_url")}
    meta["chapters"] = [{"start": int(c.get("start_time", 0)), "title": c.get("title")} for c in info.get("chapters") or []]

    kind, lang = pick_lang(info)
    meta["subtitle"] = {"kind": kind, "lang": lang}
    transcript = ""
    if lang:
        flag = "--write-subs" if kind == "subtitles" else "--write-auto-subs"
        run_ytdlp(["--skip-download", flag, "--sub-langs", lang, "--sub-format", "vtt",
                   "-o", os.path.join(a.out, "sub.%(ext)s"), a.url])
        files = glob.glob(os.path.join(a.out, "sub*.vtt"))
        if files:
            with open(files[0], encoding="utf-8") as f:
                transcript = vtt_to_text(f.read())

    with open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(os.path.join(a.out, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(transcript)

    print(f"제목: {meta['title']}\n채널: {meta['channel']}\n길이: {meta['duration_string']}")
    print(f"자막: {lang or '없음'} ({kind or '-'}), {len(transcript.splitlines())}줄")
    print(f"저장: {a.out}/meta.json, {a.out}/transcript.txt")


if __name__ == "__main__":
    main()
