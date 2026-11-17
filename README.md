# 두괄이 — 던지면 보고가 되는 업무 기록 에이전트

슬랙 봇 DM에 대충 적어 보내거나 통화가 끝나면, 두괄식·개조식 초안이 돌아오고 업무 로그가 자동으로 남습니다.

- **입구**: 슬랙 봇 DM(텍스트·음성 파일), 안드로이드 통화 녹음 폴더
- **출구**: 슬랙 스레드 초안 + 버튼(슬랙 공유용 / 메일 양식 / 구두 보고 대본), 날짜별·건별 Markdown 업무 로그, 매일 17:30 미완료 리마인드
- **건 단위 관리**: 같은 일에 대한 통화·지시·후속 메모를 한 건으로 묶어 통합 정리
- **LLM**: 구독 계정으로 로그인한 Claude Code(기본) 또는 Claude API 키 중 선택. 기록 1건당 1회, 하루 상한 있음
- **음성**: 로컬 전사(기본, 음성 파일이 밖으로 나가지 않음) 또는 외부 전사 API 중 선택

## 1. 설치와 실행 (윈도우)

`run.bat`을 더블클릭하면 됩니다. 가상환경을 켤 필요가 없습니다.

- 처음 실행하면 가상환경을 만들고 패키지를 설치합니다(몇 분 걸림).
- `.env`가 없으면 예시에서 만들어 메모장으로 엽니다. 2번의 슬랙 토큰을 채우고 다시 실행하세요.
- 오류로 멈추면 10초 뒤 자동으로 다시 켭니다. 끄려면 창을 닫으세요.
- 로그는 창과 `data\dugwali.log`에 함께 남습니다.

**윈도우 로그인 시 자동 시작**: `autostart-on.bat`을 한 번 실행하면 다음 로그인부터 최소화된 창으로 켜집니다. 해제는 `autostart-off.bat`입니다. 두괄이가 꺼져 있는 동안 생긴 통화 녹음은 자동으로 처리하지 않고, 다시 켰을 때 처리할지 묻습니다.

### 직접 실행 (명령줄)

`run.bat`이 만든 가상환경의 파이썬을 직접 부르면 활성화 없이 CLI를 쓸 수 있습니다. 아래 `python`은 모두 `.venv\Scripts\python.exe`로 바꿔 써도 됩니다.

Claude Code를 설치하고 구독 계정으로 한 번 로그인해 둔 뒤 점검합니다.

```bash
python -m dugwali.cli check
```

`{'ok': True}`가 나오면 LLM 연결은 끝입니다. 슬랙 없이 바로 써 볼 수 있습니다.

```bash
python -m dugwali.cli note "방금 김과장님 통화함. 견적서 금요일까지 다시 달라고 함"
```

정리에 실패하면 원문은 저장되고 기록 번호가 나옵니다. 실패한 것은 `attention`으로 보고 `retry 번호`로 다시 정리합니다.

```bash
python -m dugwali.cli attention
```

## 2. 슬랙 앱 만들기

1. https://api.slack.com/apps → Create New App → **From a manifest** → 아래 붙여넣기
2. Basic Information → App-Level Tokens → `connections:write` 범위로 생성 → `SLACK_APP_TOKEN` (xapp-)
3. Install App → 워크스페이스에 설치 → `SLACK_BOT_TOKEN` (xoxb-)
4. 내 프로필 → 더보기 → 멤버 ID 복사 → `OWNER_SLACK_USER_ID`

```yaml
display_information:
  name: 두괄이
features:
  app_home:
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: dugwali
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - im:history
      - im:read
      - im:write
      - files:read
      - reactions:write
settings:
  event_subscriptions:
    bot_events:
      - message.im
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

실행: `run.bat` 더블클릭 (또는 `python -m dugwali.app`)

슬랙에서 두괄이 앱 → 메시지 탭에 아무렇게나 적어 보내면 됩니다. `도움말`, `건`, `할일`, `오늘`, `조치` 명령이 있습니다.

### 건으로 묶기

메모 하나는 기록 하나이고, 같은 일에 속한 기록을 묶은 것이 **건**입니다. 새 메모는 새 건으로 시작합니다.

- **합치기**: 진행 중인 건과 같은 일로 보이면 초안에 `↪ #3 …에 합치기` 버튼이 뜹니다. 자동으로 합치지는 않습니다. 제안이 없어도 `다른 건에 합치기` 메뉴에서 직접 고를 수 있습니다.
- **사후 보강**: 일이 끝난 뒤의 후속 내용도 새 메모로 던지고 같은 건에 합치면 됩니다. 종결한 건에 합치면 다시 열립니다. 한 기록의 내용만 고칠 때는 그 스레드에 답글을 답니다.
- **통합 정리**: `건` → `정리 보기`. 건에 속한 기록 전체를 최신 상태 기준으로 하나의 두괄식 정리로 만들고, 양식 3종도 그대로 쓸 수 있습니다. 합칠 때 기록마다 겹쳐 나온 할 일도 하나로 정리됩니다.
- **삭제·종결**: 초안의 `삭제`는 그 기록만, `건` 목록의 `삭제`는 건 전체를 지웁니다. `종결`은 건을 목록과 마감 정리에서 숨기고 남은 할 일을 완료 처리합니다.

요약 승인 전의 통화 전사문은 건에 합쳐도 통합 정리(LLM 전송)에 포함되지 않습니다.

### 처리에 실패했을 때

어떤 건이 막혀도 조용히 사라지지 않습니다.

- **원문 먼저 저장**: 메모와 전사문은 받자마자 저장하고 그다음에 정리합니다. 정리(LLM)가 실패해도 원문은 남고, 그 자리에 `다시 시도` 버튼이 뜹니다.
- **통화 녹음**: 전사나 전송이 실패하면 DM으로 알리고 `다시 시도` 버튼을 냅니다. 이미 전사한 파일은 다시 전사하지 않습니다.
- **음성 메모**: 받아쓰기에 실패해도 기록이 남아 `다시 시도`할 수 있습니다.
- **`조치`**: 처리 실패, 아직 정리 안 된 기록(요약 버튼을 안 누른 통화 포함), 놓친 통화 녹음을 한 번에 보여줍니다. 마감 정리에도 건수가 나옵니다. AI의 판단이 아니라 DB와 녹음 폴더를 조회한 결과이고, "막힌 건이 없습니다"는 이 세 가지가 0건이라는 뜻입니다. 해야 할 업무는 `할일`에서 봅니다.
- **마감 정리**: 하루 한 번만 보냅니다(재시작해도 다시 보내지 않음). 전송에 실패하면 10분 간격으로 3번까지 다시 시도하고, 그래도 안 되면 `조치` 목록에 남깁니다.
- **마감 정리 순서**: 기한이 지났거나 오늘·내일까지인 내 할 일 → 조치 필요 → 오늘 다룬 건 → 복귀 지점 → 이전부터 열려 있는 건.

## 3. 통화 녹음 연결 (선택)

```bash
pip install -r requirements-stt.txt
```

1. 폰의 통화 녹음 폴더를 Syncthing(또는 FolderSync)으로 NAS 폴더에 동기화합니다.
2. 랩탑에서 그 NAS 폴더를 Tailscale로 붙여 드라이브로 연결하고 `.env`의 `CALL_DIR`에 적습니다.
3. 먼저 속도를 잽니다. 5분 통화가 10분 안에 끝나면 그대로 쓰고, 느리면 `WHISPER_MODEL=small`로 낮춥니다.

```bash
python -m dugwali.cli transcribe 녹음파일.m4a
```

- 앱을 켠 뒤에 생긴 녹음만 자동으로 처리합니다. 처음 설치하기 전의 파일은 건드리지 않고, 설치 뒤에 생겼는데 놓친 파일(꺼져 있었거나 늦게 동기화됨)은 `모두 처리` / `건너뛰기`를 묻습니다. 답하지 않으면 다음에 켤 때 다시 묻습니다.
- `SEND_CALLS_TO_LLM=false`(기본)면 전사문은 로컬에만 저장되고, 슬랙에 뜨는 **"요약하기"** 버튼을 눌러야 LLM으로 갑니다. 회사 승인이 나면 `true`로 바꾸세요.
- 화자 구분은 `pyannote.audio`와 `torch`를 설치하고 `HF_TOKEN`을 넣으면 켜집니다(Hugging Face에서 `pyannote/speaker-diarization-3.1` 약관 동의 필요). 없으면 화자 표기 없이 전사하고 LLM이 문맥으로 구분합니다.

## 4. 전사·LLM 방식 고르기

`.env`에서 바꿉니다. 기본값은 가장 보수적인 조합입니다(전사는 로컬, 전사문은 버튼을 눌러야 LLM으로 감).

| 설정 | 값 | 밖으로 나가는 것 |
|---|---|---|
| `STT_PROVIDER` | `local`(기본) | 없음 |
| | `api` | **음성 파일**이 `STT_API_BASE_URL`의 제공자로 감 |
| `LLM_PROVIDER` | `claude_code`(기본) | 마스킹한 **텍스트**가 구독 계정의 Claude로 감 |
| | `api` | 마스킹한 **텍스트**가 Claude API로 감 |
| `SEND_CALLS_TO_LLM` | `false`(기본) | 통화·음성 메모 전사문은 건마다 버튼을 눌러야 LLM으로 감 |
| | `true` | 전사문이 자동으로 LLM으로 감 |

- **외부 전사 API**: OpenAI 호환 형식(`POST {주소}/audio/transcriptions`)이면 연결됩니다. `STT_API_BASE_URL`, `STT_API_KEY`, `STT_API_MODEL`을 채웁니다. 파일 이름은 보내지 않습니다. 화자 구분은 하지 않고, 실패하면 `다시 시도`와 함께 `로컬 전사로 다시 시도` 버튼이 뜹니다(로컬 전사 패키지가 설치돼 있을 때).
- **Claude API**: `pip install -r requirements-api.txt` 후 `LLM_PROVIDER=api`, `LLM_API_KEY`를 채웁니다. 기본 모델은 `claude-opus-5-5`이고, 비용을 줄이려면 `LLM_API_MODEL`을 `claude-sonnet-5-5`나 `claude-haiku-5-5`로 바꿉니다. 모델이 안전 분류로 처리를 거절하면 서버가 대체 모델로 이어 처리하도록 켜 두었습니다(`LLM_API_BASE_URL`을 쓰면 꺼짐).
- **하루 호출 상한**: `LLM_DAILY_LIMIT`(기본 200회). 닿으면 원문만 보관하고 `조치` 목록에 올립니다. 일시적인 LLM 오류는 한 번 자동으로 다시 시도합니다.
- **세션 기록**: Claude Code 방식은 호출 내용을 PC에 세션 기록으로 남기지 않는 옵션으로 호출합니다. 이 옵션이 없는 구버전이면 로그에 경고를 남기고 그대로 호출합니다.
- **음성 메모도 같은 게이트**: 슬랙에 올린 음성 파일의 전사문도 `SEND_CALLS_TO_LLM`을 따릅니다. 회사 승인이 있으면 `true`로 두세요.

## 5. 구조

| 파일 | 역할 |
|---|---|
| `dugwali/agent/llm.py` | LLM 호출(Claude Code / Claude API), 전송 전 마스킹. **다른 LLM을 붙일 때 여기만 수정** |
| `dugwali/agent/extract.py` | 프롬프트와 공통 스키마 추출 |
| `dugwali/agent/render.py` | 스키마 → 슬랙/메일/구두/Markdown 양식 (LLM 미사용) |
| `dugwali/pipeline.py` | 원문 저장 → 추출 → 로그 갱신, 건 병합·통합 정리·삭제 |
| `dugwali/ingest/slack.py` | 슬랙 봇 |
| `dugwali/ingest/watcher.py` | 통화 녹음 폴더 감시 |
| `dugwali/stt/transcribe.py` | 전사 방식 고르기, 로컬 전사, 화자 구분 |
| `dugwali/stt/api.py` | 외부 전사 API 호출 |
| `dugwali/digest.py` | 일일 정리 |
| `dugwali/store.py` | SQLite (건·기록·할 일·처리 실패·앱 상태). 이전 버전 DB는 시작할 때 자동 이전 |

## 6. 테스트

```bash
pytest
```

## 아직 없는 것

- 노션 저장(인테그레이션 승인 후 `sinks/notion.py` 추가), Gmail 임시보관함 생성(지금은 메일 양식을 복사해서 사용)
- 슬랙 채널로 직접 전송하는 버튼(지금은 복사용 텍스트 제공)
