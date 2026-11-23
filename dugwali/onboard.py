"""처음 쓰는 사람의 설정: 슬랙 앱 만들기 안내, 토큰 확인, LLM 연결 확인.

대시보드의 `첫 설정` 화면이 쓴다. 슬랙 토큰이 없거나 틀리면 `app.py`가 봇 없이 대시보드만 띄우고
이 화면을 연다. 확인을 통과한 토큰만 `.env`에 저장한다.
"""
import logging

from .config import cfg

log = logging.getLogger(__name__)

MANIFEST = """display_information:
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
"""

_ERRORS = {
    "invalid_auth": "토큰이 맞지 않습니다. 복사할 때 앞뒤가 잘리지 않았는지 보세요.",
    "not_authed": "토큰이 비어 있습니다.",
    "account_inactive": "앱이 워크스페이스에서 삭제됐거나 비활성 상태입니다.",
    "token_revoked": "취소된 토큰입니다. 새로 만드세요.",
    "not_allowed_token_type": "토큰 종류가 다릅니다(봇 토큰과 앱 토큰 자리가 바뀌지 않았는지 보세요).",
    "user_not_found": "그 멤버 ID를 찾지 못했습니다.",
    "missing_scope": "앱 권한이 모자랍니다. 아래 매니페스트로 앱을 만들고 다시 설치하세요.",
}


def _why(err: Exception) -> str:
    code = getattr(getattr(err, "response", None), "get", lambda *_: None)("error")
    return _ERRORS.get(code, f"슬랙 오류: {code or err}")


def check_slack(bot_token: str, app_token: str, owner: str, client_cls=None) -> tuple[bool, list[str]]:
    """세 값을 실제로 슬랙에 확인하고, 통과하면 내 DM으로 확인 메시지를 보낸다. (통과 여부, 단계별 결과)."""
    if client_cls is None:
        from slack_sdk import WebClient as client_cls
    bot_token, app_token, owner = bot_token.strip(), app_token.strip(), owner.strip()
    out, ok = [], True
    if not bot_token.startswith("xoxb-"):
        return False, ["❌ 봇 토큰은 xoxb-로 시작합니다(Install App 화면의 Bot User OAuth Token)."]
    if not app_token.startswith("xapp-"):
        return False, ["❌ 앱 토큰은 xapp-로 시작합니다(Basic Information → App-Level Tokens)."]
    if not owner.startswith(("U", "W")):
        return False, ["❌ 멤버 ID는 U로 시작합니다(내 프로필 → 더보기 → 멤버 ID 복사)."]
    bot = client_cls(token=bot_token)
    try:
        team = bot.auth_test()
        out.append(f"✅ 봇 토큰: {team.get('team', '')} 워크스페이스의 {team.get('user', '봇')}")
    except Exception as err:
        return False, [f"❌ 봇 토큰: {_why(err)}"]
    try:
        client_cls(token=app_token).apps_connections_open()
        out.append("✅ 앱 토큰: 실시간 연결(Socket Mode) 가능")
    except Exception as err:
        ok = False
        out.append(f"❌ 앱 토큰: {_why(err)} connections:write 범위로 만들었는지도 보세요.")
    try:
        channel = bot.conversations_open(users=owner)["channel"]["id"]
        bot.chat_postMessage(channel=channel, text="두괄이 연결을 확인했습니다. 이 DM에 메모를 보내면 정리해 드립니다.")
        out.append("✅ 멤버 ID: 내 DM으로 확인 메시지를 보냈습니다")
    except Exception as err:
        ok = False
        out.append(f"❌ 멤버 ID: {_why(err)}")
    return ok, out


def check_llm() -> tuple[bool, str]:
    """LLM을 한 번 불러 본다(하루 호출 수에 들어간다)."""
    from .agent import llm

    try:
        r = llm.call_json('다음 JSON을 그대로 출력: {"ok": true}')
    except Exception as err:
        return False, str(err) or type(err).__name__
    if r.get("ok") is True:
        how = "Claude Code(구독 계정)" if cfg.llm_provider == "claude_code" else "Claude API"
        return True, f"{how}로 정리할 수 있습니다."
    return False, f"응답이 예상과 다릅니다: {r}"
