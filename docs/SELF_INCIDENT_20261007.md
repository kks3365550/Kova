# 자기 실수 기록: CRLF 변환이 web/ 디렉터리 전체를 날렸다

**일시**: 2026-10-07 23:13
**영향 파일**: `web/*.py` 9개 전부 0바이트
**복구**: `git checkout HEAD -- web/` — 완전 복구됨

## 내가 쓴 명령

```powershell
python -c "import glob; [open(p,'wb').write(open(p,'rb').read().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')) for p in glob.glob('web/*.py')]"
```

## 왜 파일이 날아갔는가

이 한 줄에는 두 개의 `open` 이 있다.

```python
open(p, 'wb').write( open(p, 'rb').read() )
^^^^^^^^^^^^^^^     ^^^^^^^^^^^^^^^^^^^^^
메서드를 찾으려고 먼저 평가        인자는 그 다음에 평가
```

Python 은 `obj.method(args)` 를 평가할 때 **먼저 `obj` 를 평가**한다.
그래서 순서가 이렇게 된다:

1. `open(p, 'wb')` 실행 → **파일이 즉시 빈 상태로 잘린다** (`'wb'` = write + truncate)
2. `open(p, 'rb').read()` 실행 → 이미 비어 있는 파일을 읽는다 → `b''`
3. `write(b'')` → 파일은 0바이트로 남는다

`open(p,'wb')` 는 **호출하는 순간** truncate 합니다. `.write()` 가 호출될 때가 아닙니다.

## 재현 (최소)

```powershell
'ORIGINAL_CONTENT_12345' | Out-File -Encoding ascii probe.txt
python -c "open('probe.txt','wb').write(open('probe.txt','rb').read())"
Get-Item probe.txt   # Length: 0
```

위임을 확인했고 실제 실행 결과도 0바이트였습니다.

## 안전한 방법

```python
# 방법 1: 읽기를 먼저 끝낸다
data = open(p, 'rb').read()
open(p, 'wb').write(data)

# 방법 2: 하나의 핸들
with open(p, 'r+b') as f:
    d = f.read()
    f.seek(0)
    f.truncate()
    f.write(d)

# 방법 3: 개행을 아예 건드리지 않는다 (가장 안전)
# CRLF 은 git autocrlf 과 .gitattributes 로 관리한다.
```

## 이 사고가 일어난 배경

작업 수순이 잘못됐을 뿐입니다.

1. `git checkout` 으로 파일 내용을 날렸다
2. 수정이 필요해서 편집 도구를 썼다
3. "CRLF 규칙에 맞게" 한 줄 명령으로 개행을 정리했다 ← 여기서 전부 날아감
4. 30초 뒤 pyflakes 는 통과했는데, 서버가 안 떴다
5. 파고들어 보니 파일이 0바이트였다

**개행 정리 자체가 필요 없었습니다.** 편집 도구는 CRLF 를 유지했고,
pyflakes · pytest · 서버 기동 전부 정상 있었습니다.
규칙 준수를 위해 필요하지 않은 위험을引入한 것입니다.

## 규칙 (이후)

### 1. 여러 파일을 한 줄 명령으로 쓰지 않는다

리스트 컴프리헨션 안에서 파일을 쓰면
**몇 개가 죽었는지 즉시 알 수 없다.**

```python
# 금지
[transform(p) for p in glob.glob('**/*.py')]

# 허용: 하나씩, 실패 시 즉시 중단
for p in paths:
    transform(p)
```

### 2. 파일을 연 직후 그 크기를 확인한다

```powershell
Get-ChildItem web\*.py | Where-Object { $_.Length -eq 0 }
```

자기 수정 스크립트를 실행한 뒤에는 이것이 습관이 되어야 한다.

### 3. 개행은 파일 편집 도구의 책임이다

`edit` 도구는 CRLF 를 유지한다. 개행을 억지로 맞추려 하지 않는다.
개선이 필요하다면 `git config core.autocrlf` 또는 `.gitattributes` 로 처리한다.

### 4. git 이 유일한 복구 수단이다

이 사고가 복구 가능했던 이유는 **커밋되어 있었기** 때문이었다.
큰 편집 전에는 반드시 커밋해둔다. Qwen 이 v2.13.7 을 커밋해 둔 덕에
9개 파일이 1초 만에 복구됐다.

## 같이 고친 실제 결함 3건

### 1. `api_backup.py` — 예외 타입 import 중복

두 개의 다른 except 핸들러가 같은 네 개 예외를 각각 import 한다.

```python
except Exception as e:
    from core.lock import BackupAlreadyRunningError
    from core.storage import InsufficientDiskSpaceError
    from core.vss_manager import VSSRequiredError
    from core.verify import RestoreVerificationError
```

동작에는 문제없지만 한쪽만 수정하면 조용히 어긋난다.
모듈 상단으로 올려 한 곳에만 두었다.

### 2. `api_update.py` — 릴리즈 확인 함수 import 중복

`check_for_update` / `get_current_installed_version` 가 세 함수에서 각각 import.
마찬가지로 상단으로 통합.

### 3. `app.py` — FastAPI 버전이 구버전

```python
app = FastAPI(version="2.9.22")     # VERSION 파일은 2.13.7
```

릴리즈마다 수동으로 올려야 했고 v2.13.7 까지 방치돼 있었다.
`web/version.py` 의 `VERSION` 을 쓰도록 바꿨다.

검증:

```
OpenAPI info.version : 2.13.7
VERSION 파일         : 2.13.7
일치                 : True
```

## 내가 잘못 판단한 것

중복 import 를 찾다가 `app.py` 의
`ReplicationQueueManager` (라인 72, 467) 도 중복이라 생각해 제거하려 했습니다.

실제 범위를 확인하니:

```
lifespan            67-90     <- 라인 72
get_alerts_summary  390-497   <- 라인 467
```

**서로 다른 함수 스코프입니다.** 중복이 아닙니다.
제 스코프 판정 스크립트가 `ast.walk` 안에서 함수를 순회하는 바람에
마지막으로 방문한 함수를 스코프로 기록했고, 그래서 둘이 같은 스코프인 것처럼
나왔습니다. 제거했다가 되돌렸습니다.

**AST 도구도 검증 없이 믿으면 안 됩니다.**
오늘 하루에 이목을 세 번 배웠습니다 (pyflakes 미검증 신뢰, CI 로그 미확인,
AST 스코프 오판).