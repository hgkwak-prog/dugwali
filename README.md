# 두괄이 — 던지면 보고가 되는 업무 기록 에이전트

슬랙 봇 DM에 대충 적어 보내거나 통화가 끝나면, 두괄식·개조식 초안이 돌아오고 업무 로그가 자동으로 남습니다.

- **입구**: 슬랙 봇 DM(텍스트·음성 파일), 안드로이드 통화 녹음 폴더
- **출구**: 슬랙 스레드 초안 + 버튼(슬랙 공유용 / 메일 양식 / 구두 보고 대본), 날짜별·건별 Markdown 업무 로그, 매일 17:30 미완료 리마인드
- **건 단위 관리**: 같은 일에 대한 통화·지시·후속 메모를 한 건으로 묶어 통합 정리
- **LLM**: 구독 계정으로 로그인한 Claude Code를 헤드리스(`claude -p`)로 호출. 기록 1건당 1회
- **음성**: 랩탑에서 로컬 전사. 음성 파일은 밖으로 나가지 않음

## 1. 설치와 실행 (윈도우)

`run.bat`을 더블클릭하면 됩니다. 가상환경을 켤 필요가 없습니다.

- 처음 실행하면 가상환경을 만들고 패키지를 설치합니다(몇 분 걸림).
- `.env`가 없으면 예시에서 만들어 메모장으로 엽니다. 2번의 슬랙 토큰을 채우고 다시 실행하세요.
- 오류로 멈추면 10초 뒤 자동으로 다시 켭니다. 끄려면 창을 닫으세요.
- 로그는 창과 `data\dugwali.log`에 함께 남습니다.

**윈도우 로그인 시 자동 시작**: `autostart-on.bat`을 한 번 실행하면 다음 로그인부터 최소화된 창으로 켜집니다. 해제는 `autostart-off.bat`입니다. 두괄이가 꺼져 있는 동안의 통화는 처리되지 않으니 켜 두는 것을 권합니다.

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

슬랙에서 두괄이 앱 → 메시지 탭에 아무렇게나 적어 보내면 됩니다. `도움말`, `건`, `할일`, `오늘` 명령이 있습니다.

### 건으로 묶기

메모 하나는 기록 하나이고, 같은 일에 속한 기록을 묶은 것이 **건**입니다. 새 메모는 새 건으로 시작합니다.

- **합치기**: 진행 중인 건과 같은 일로 보이면 초안에 `↪ #3 …에 합치기` 버튼이 뜹니다. 자동으로 합치지는 않습니다. 제안이 없어도 `다른 건에 합치기` 메뉴에서 직접 고를 수 있습니다.
- **사후 보강**: 일이 끝난 뒤의 후속 내용도 새 메모로 던지고 같은 건에 합치면 됩니다. 종결한 건에 합치면 다시 열립니다. 한 기록의 내용만 고칠 때는 그 스레드에 답글을 답니다.
- **통합 정리**: `건` → `정리 보기`. 건에 속한 기록 전체를 최신 상태 기준으로 하나의 두괄식 정리로 만들고, 양식 3종도 그대로 쓸 수 있습니다. 합칠 때 기록마다 겹쳐 나온 할 일도 하나로 정리됩니다.
- **삭제·종결**: 초안의 `삭제`는 그 기록만, `건` 목록의 `삭제`는 건 전체를 지웁니다. `종결`은 건을 목록과 마감 정리에서 숨기고 남은 할 일을 완료 처리합니다.

요약 승인 전의 통화 전사문은 건에 합쳐도 통합 정리(LLM 전송)에 포함되지 않습니다.

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

- 앱을 켠 뒤에 생긴 녹음만 처리합니다(기존 파일은 건드리지 않음).
- `SEND_CALLS_TO_LLM=false`(기본)면 전사문은 로컬에만 저장되고, 슬랙에 뜨는 **"이 통화 요약하기"** 버튼을 눌러야 LLM으로 갑니다. 회사 승인이 나면 `true`로 바꾸세요.
- 화자 구분은 `pyannote.audio`와 `torch`를 설치하고 `HF_TOKEN`을 넣으면 켜집니다(Hugging Face에서 `pyannote/speaker-diarization-3.1` 약관 동의 필요). 없으면 화자 표기 없이 전사하고 LLM이 문맥으로 구분합니다.

## 4. 구조

| 파일 | 역할 |
|---|---|
| `dugwali/agent/llm.py` | `claude -p` 호출, 전송 전 마스킹. **다른 LLM으로 바꿀 때 여기만 수정** |
| `dugwali/agent/extract.py` | 프롬프트와 공통 스키마 추출 |
| `dugwali/agent/render.py` | 스키마 → 슬랙/메일/구두/Markdown 양식 (LLM 미사용) |
| `dugwali/pipeline.py` | 추출 → 저장 → 로그 갱신, 건 병합·통합 정리·삭제 |
| `dugwali/ingest/slack.py` | 슬랙 봇 |
| `dugwali/ingest/watcher.py` | 통화 녹음 폴더 감시 |
| `dugwali/stt/transcribe.py` | 로컬 전사, 화자 구분 |
| `dugwali/digest.py` | 일일 정리 |
| `dugwali/store.py` | SQLite (건·기록·할 일). 이전 버전 DB는 시작할 때 자동 이전 |

## 5. 테스트

```bash
pytest
```

## 아직 없는 것

- 노션 저장(인테그레이션 승인 후 `sinks/notion.py` 추가), Gmail 임시보관함 생성(지금은 메일 양식을 복사해서 사용)
- 슬랙 채널로 직접 전송하는 버튼(지금은 복사용 텍스트 제공)
