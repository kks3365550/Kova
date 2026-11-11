---
name: mask-chat-secrets
description: Claude Code와 Antigravity 대화 기록(~/.claude/projects 의 jsonl, ~/.gemini/antigravity 의 brain md·conversations pb 등)에 평문으로 남은 API 키, 토큰, 비밀번호, 개인키를 찾아 같은 길이의 '*' 로 마스킹한다. "대화 기록 비밀번호 마스킹", "채팅 로그에서 API 키 지워줘", "평문 키 가려줘", "secret redact", "mask secrets in transcripts" 같은 요청에 사용한다.
---

# 대화 기록 평문 비밀 마스킹

Claude Code·Antigravity 대화 기록 속 평문 API 키와 비밀번호를 찾아 마스킹한다.
작업은 `scripts/mask_chat_secrets.py` 가 한다. 표준 라이브러리만 쓰므로 설치할 것이 없다.

## 검사하는 곳 (경로를 주지 않으면)

| 도구 | 위치 |
|---|---|
| Claude Code | `~/.claude/projects/**.jsonl`, `~/.claude/history.jsonl`, `file-history`, `paste-cache`, `shell-snapshots`, `todos`, `sessions` |
| Antigravity | `~/.gemini/antigravity/brain` (작업 문서 md), `conversations` (대화 pb), `implicit`, `code_tracker`, `%APPDATA%/Antigravity/logs` |

지금 쓰는 설정·인증 파일(`settings.json`, `.credentials.json`, `oauth_creds.json`, `mcp_config.json`, `.env` 등)은 마스킹하면 도구가 멈추므로 **항상 건너뛴다**.

## 잡아내는 것

- 형식이 정해진 키: Anthropic `sk-ant-`, OpenAI `sk-`, Google `AIza`·`GOCSPX-`·`ya29.`, GitHub `ghp_`·`github_pat_`, GitLab, AWS `AKIA`, Slack `xox*`, Stripe, Hugging Face, Telegram 봇 토큰, JWT
- PEM 개인키 블록 (`-----BEGIN ... PRIVATE KEY-----`)
- URL 속 비밀번호 (`https://user:비번@host`), `Bearer` 토큰
- 키=값 형태: `password`, `pwd`, `api_key`, `secret`, `token`, `client_secret` 등 (JSON 안의 `\"password\": \"...\"` 포함)
- 한국어 대화체: `비밀번호는 xxx`, `비번: xxx`, `패스워드 = xxx`, `암호는`, `마스터키:`, `API 키:`, `토큰:`
- `--literal` 로 넘긴 정확한 값 (규칙에 안 걸리는 짧은 비밀번호용)

`your-password`, `example`, `${VAR}`, `os.environ` 같은 자리표시자와 이미 `****` 인 값은 건너뛴다.

## 마스킹 방식

값을 **같은 길이의 `*`** 로 덮는다. 형식 키는 앞 몇 글자(`sk-ant-api`, `ghp_` 등)를 남겨 어떤 키였는지 알 수 있게 한다.
길이가 같으므로 jsonl 줄이 깨지지 않고, Antigravity `.pb`(protobuf) 같은 바이너리도 길이 필드가 어긋나지 않는다.
바이너리는 바이트 길이가 달라지면 쓰지 않고 중단한다.

## 진행 순서

1. **Claude Code·Antigravity 를 종료하라고 안내한다.** 실행 중이면 파일을 다시 덮어쓰거나 잠가 둘 수 있다.
   (지금 이 Claude Code 세션의 기록도 대상이므로, 현재 세션 jsonl 은 세션이 끝난 뒤 한 번 더 돌리는 것을 권한다.)
2. **미리보기**로 무엇이 걸리는지 보여준다. 출력에는 비밀 값 자체가 나오지 않고 규칙 이름과 길이만 나온다.
   ```bash
   python .claude/skills/mask-chat-secrets/scripts/mask_chat_secrets.py
   ```
   사용자가 따로 기억하는 비밀번호가 있으면 `--literal` 로 추가한다. 값이 대화에 남지 않도록,
   여러 개면 사용자가 직접 텍스트 파일에 적게 하고 `--literal-file 경로` 로 넘긴 뒤 그 파일을 지우게 한다.
3. 결과를 사용자에게 요약해 보여주고 **확인을 받은 뒤** `--apply` 로 실제 마스킹한다.
   ```bash
   python .claude/skills/mask-chat-secrets/scripts/mask_chat_secrets.py --apply
   ```
4. 같은 명령을 `--apply` 없이 다시 돌려 **발견 0건**인지 확인한다.
5. 마무리 안내:
   - 원본 백업 위치(`~/.mask-chat-secrets-backup/<시각>`)를 알려준다. **백업에도 평문이 그대로 있으므로** 대화 기록이 정상 열리는지 확인한 뒤 지우게 한다. 백업이 필요 없으면 `--no-backup`.
   - 마스킹은 기록만 지운다. **노출된 키·비밀번호는 발급처에서 폐기하고 새로 발급**하라고 반드시 말한다.
   - claude.ai 웹/앱 대화는 서버에 저장되므로 이 스크립트로 지울 수 없다. 해당 대화를 직접 삭제하라고 안내한다.

## 옵션

| 옵션 | 뜻 |
|---|---|
| `경로 ...` | 기본 위치 대신 이 파일·폴더만 검사 |
| `--apply` | 실제로 바꾼다 (없으면 미리보기) |
| `--literal 값` | 이 값도 마스킹 (여러 번 가능, 4자 이상) |
| `--literal-file 파일` | 한 줄에 하나씩 적은 값 목록 |
| `--backup-dir 폴더` / `--no-backup` | 원본 백업 위치 지정 / 백업 안 함 |
| `--include-db` | Antigravity `state.vscdb` 같은 DB 파일도 포함. 앱을 꼭 끈 뒤에만 쓴다 |

종료 코드: `0` 발견 없음, `1` 발견(또는 마스킹함), `2` 오류.

## 주의

- 이 저장소 코드 자체를 검사하려면 이 스킬이 아니라 `tools/scan_secrets.py` 를 쓴다.
- 오탐으로 일반 문장이 가려질 수 있으므로 미리보기 결과를 꼭 먼저 보여준다.
- Windows 에서는 `python` 대신 `py` 일 수 있다. 경로의 `~` 는 `%USERPROFILE%` 로 풀린다.
