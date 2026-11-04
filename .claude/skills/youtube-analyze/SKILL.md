---
name: youtube-analyze
description: YouTube 영상 분석. 사용자가 YouTube 링크(youtube.com/watch, youtu.be, shorts)를 주거나 "유튜브 영상 분석/요약/정리"를 요청하면 사용. 제목·채널·설명·챕터·자막(한국어/영어, 자동자막 포함)을 가져와 요약하고, 필요하면 Kova 프로젝트에 적용할 점을 정리한다.
---

# YouTube 영상 분석

## 1. 영상 정보와 자막 가져오기

```bash
pip install -q yt-dlp   # 없으면 설치 (pypi는 기본 허용)
python3 .claude/skills/youtube-analyze/scripts/fetch_youtube.py "<URL>" --out <scratchpad>/yt
```

출력 파일:
- `<out>/meta.json`: 제목, 채널, 업로드일, 길이, 조회수, 설명, 챕터
- `<out>/transcript.txt`: 타임스탬프가 붙은 자막 텍스트(중복 줄 제거)

자막 언어 우선순위: `ko` → `en` → 그 외 첫 번째 자막. 수동 자막이 없으면 자동 생성 자막을 사용한다.

## 2. 실패할 때

- `403`, `CONNECT tunnel failed`, `ENOTFOUND` 같은 오류: 클라우드 환경의 네트워크 정책이 YouTube를 막고 있는 것이다.
  사용자에게 환경 설정 → Network access에서 아래 도메인을 허용해 달라고 안내한다.
  `www.youtube.com`, `youtube.com`, `youtu.be`, `m.youtube.com`, `i.ytimg.com`, `*.googlevideo.com`
  (안내: https://code.claude.com/docs/en/cloud-environments#network-access)
- 자막이 아예 없는 영상: meta.json(제목·설명·챕터)만으로 분석하고, 자막이 없어서 내용 분석이 제한적이라고 분명히 밝힌다.
- 내용을 지어내지 않는다. 가져온 데이터에 없는 내용은 말하지 않는다.

## 3. 분석 결과 형식 (기본: 한국어)

1. **한 줄 요약**
2. **영상 정보**: 제목 / 채널 / 길이 / 업로드일
3. **핵심 내용**: 5~10개 항목, 각 항목에 근거 타임스탬프 `[mm:ss]`
4. **챕터별 정리** (챕터가 있으면)
5. **Kova에 적용할 점** (사용자가 원하거나 영상이 백업/복구/Windows/개발 관련일 때): 관련 파일 경로와 함께 구체적 제안

사용자가 다른 형식(전체 번역, 특정 부분 질문 등)을 원하면 그에 맞춘다.
