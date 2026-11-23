"""6단계: 첫 설정, 설정 화면, 프로필 인터뷰, 규칙 승격, 설치 보완. LLM·슬랙 없이 돈다."""
import os
import threading
from pathlib import Path

import pytest

from dugwali import app, deps, envfile, onboard, pipeline, profile
from dugwali.agent import llm
from dugwali.config import cfg

from test_core import SAMPLE, dash, fake_llm, no_personal_profile, remote, store  # noqa: F401  (fixture 재사용)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """테스트마다 따로 쓰는 .env. 실제 .env와 환경 변수는 건드리지 않는다."""
    p = tmp_path / ".env"
    p.write_text("# 슬랙\nSLACK_BOT_TOKEN=\n# 마감\nDIGEST_TIME=17:30\nLLM_DAILY_LIMIT=200\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "env_path", p)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for attr in ("digest_time", "llm_daily_limit", "stt_provider", "stt_api_key", "send_calls_to_llm",
                 "slack_bot_token", "slack_app_token", "owner_user_id"):
        monkeypatch.setattr(cfg, attr, getattr(cfg, attr))
    return p


# ---------- .env ----------

def test_env_write_keeps_comments_and_order(env):
    envfile.write({"DIGEST_TIME": "18:00", "SLACK_BOT_TOKEN": "xoxb-1", "CALL_DIR": r"C:\My Calls"})
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# 슬랙\nSLACK_BOT_TOKEN=xoxb-1\n# 마감\nDIGEST_TIME=18:00\n")
    assert 'CALL_DIR="C:\\My Calls"' in text
    assert envfile.read()["CALL_DIR"] == r"C:\My Calls" and os.environ["DIGEST_TIME"] == "18:00"


def test_env_write_creates_file_from_example(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "env_path", tmp_path / "new.env")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    envfile.write({"OWNER_SLACK_USER_ID": "U123"})
    text = (tmp_path / "new.env").read_text(encoding="utf-8")
    assert "OWNER_SLACK_USER_ID=U123" in text and "LLM_PROVIDER=claude_code" in text   # 예시의 나머지 값도 들어 있다


def test_slack_placeholders_count_as_missing(monkeypatch):
    monkeypatch.setattr(cfg, "slack_bot_token", "xoxb-...")
    monkeypatch.setattr(cfg, "slack_app_token", "xapp-1-real")
    monkeypatch.setattr(cfg, "owner_user_id", "U0XXXXXXX")
    assert cfg.slack_missing() == ["SLACK_BOT_TOKEN", "OWNER_SLACK_USER_ID"]


# ---------- 설정 화면 ----------

def test_settings_apply_now_or_after_restart(dash, env):
    status, html = dash.get("/settings")
    assert status == 200 and "마감 정리 시각" in html and "다시 켜야 반영" in html
    _, html = dash.post("/settings", DIGEST_TIME="18:10", LLM_DAILY_LIMIT="200", STT_PROVIDER="local")
    assert "저장했습니다: 마감 정리 시각. 바로 반영했습니다." in html
    assert cfg.digest_time == "18:10" and "DIGEST_TIME=18:10" in env.read_text(encoding="utf-8")

    _, html = dash.post("/settings", DASHBOARD_URL="https://pc.tail1.ts.net")
    assert "다시 켜야 반영되는 것: 다른 기기에서 여는 주소" in html and "두괄이 창을 닫고 다시 실행하세요" in html
    assert cfg.dashboard_url == "" or cfg.dashboard_url != "https://pc.tail1.ts.net"   # 실행 중인 값은 그대로
    _, html = dash.get("/")
    assert "다시 켜야 반영됩니다" in html                                     # 다른 화면에도 알린다


def test_settings_validate_and_keep_secrets(dash, env):
    _, html = dash.post("/settings", DIGEST_TIME="25:00", LLM_DAILY_LIMIT="-1")
    assert "저장하지 않았습니다" in html and "17:30처럼" in html and cfg.digest_time != "25:00"
    _, html = dash.post("/settings", DASHBOARD_URL="http://pc.tail1.ts.net")
    assert "https:// 주소여야" in html

    dash.post("/settings", STT_API_KEY="sk-secret")
    assert cfg.stt_api_key == "sk-secret"
    _, html = dash.get("/settings")
    assert "sk-secret" not in html and "저장돼 있음" in html                     # 키는 화면에 다시 보여주지 않는다
    _, html = dash.post("/settings", STT_API_KEY="")
    assert cfg.stt_api_key == "sk-secret" and "바뀐 설정이 없습니다" in html     # 비워 두면 그대로


def test_flags_turn_off_when_unchecked(dash, env, monkeypatch):
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    dash.post("/settings", DIGEST_TIME=cfg.digest_time)                   # 체크 해제한 채 저장
    assert cfg.send_calls_to_llm is False and "SEND_CALLS_TO_LLM=false" in env.read_text(encoding="utf-8")


def test_settings_are_read_only_from_other_devices(remote, store, env):
    code = remote.login_link().split("code=")[1]
    session = remote.req("POST", "/login", code=code)[2].split(";")[0]
    status, html, _, _ = remote.req("GET", "/settings", cookie=session)
    assert status == 200 and "볼 수만 있습니다" in html and "disabled" in html
    status, _, _, loc = remote.req("POST", "/settings", cookie=session, token=remote.token, DIGEST_TIME="06:00")
    assert status == 303 and "PC%EC%97%90%EC%84%9C%EB%A7%8C" in loc and cfg.digest_time != "06:00"
    _, html, _, _ = remote.req("GET", "/setup", cookie=session)
    assert "켜진 PC에서만" in html and 'name="bot"' not in html               # 토큰 입력칸은 다른 기기에 없다


# ---------- 슬랙 연결 확인 ----------

class FakeSlack:
    bad: set = set()
    sent: list = []

    def __init__(self, token):
        self.token = token

    def _check(self, what):
        if self.token in self.bad or what in self.bad:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("x", {"ok": False, "error": "invalid_auth" if self.token in self.bad else "user_not_found"})

    def auth_test(self):
        self._check("auth")
        return {"team": "모두", "user": "dugwali"}

    def apps_connections_open(self):
        self._check("conn")
        return {"url": "wss://x"}

    def conversations_open(self, users):
        self._check(users)
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, channel, text):
        self.sent.append(text)


def test_check_slack_reports_each_value(monkeypatch):
    FakeSlack.bad, FakeSlack.sent = set(), []
    ok, out = onboard.check_slack("xoxb-1", "xapp-1", "U1", FakeSlack)
    assert ok and len(out) == 3 and all(x.startswith("✅") for x in out) and len(FakeSlack.sent) == 1
    assert onboard.check_slack("xapp-1", "xoxb-1", "U1", FakeSlack)[1][0].startswith("❌ 봇 토큰은 xoxb-")
    FakeSlack.bad = {"xoxb-bad"}
    assert "토큰이 맞지 않습니다" in onboard.check_slack("xoxb-bad", "xapp-1", "U1", FakeSlack)[1][0]
    FakeSlack.bad = {"U404"}
    ok, out = onboard.check_slack("xoxb-1", "xapp-1", "U404", FakeSlack)
    assert not ok and "멤버 ID를 찾지 못했습니다" in out[2]


def test_setup_page_saves_tokens_and_wakes_the_app(dash, env, monkeypatch):
    calls = []
    monkeypatch.setattr(onboard, "check_slack", lambda b, a, o: (calls.append((b, a, o)) or (b == "xoxb-ok"), ["결과"]))
    dash.setup_mode = True
    _, html = dash.get("/")
    assert "처음 설정이 남았습니다" in html and "슬랙 연결" in html
    _, html = dash.get("/setup")
    assert 'name="bot"' in html and "connections:write" in html and "socket_mode_enabled: true" in html
    _, html = dash.post("/setup/slack", bot="xoxb-no", app="xapp-1", owner="U1")
    assert "통과하지 못했습니다" in html and not dash.slack_ready.is_set() and "xoxb-no" not in env.read_text("utf-8")
    _, html = dash.post("/setup/slack", bot="xoxb-ok", app="xapp-1", owner="U1")
    assert "슬랙에 연결했습니다" in html and dash.slack_ready.is_set()
    assert "SLACK_BOT_TOKEN=xoxb-ok" in env.read_text("utf-8") and cfg.slack_bot_token == "xoxb-ok"
    assert "xoxb-ok" not in html                                            # 토큰을 화면에 되돌려 보여주지 않는다


def test_app_waits_in_setup_mode_until_slack_is_connected(store, env, monkeypatch):
    import dugwali.ingest.slack as slack_mod

    made, opened = [], []

    class Bot:
        def __init__(self, store):
            if cfg.slack_bot_token != "xoxb-good":
                raise RuntimeError("The server responded with: {'ok': False, 'error': 'invalid_auth'}")
            made.append(self)

    monkeypatch.setattr(slack_mod, "Bot", Bot)
    monkeypatch.setattr(cfg, "slack_bot_token", "xoxb-old")
    monkeypatch.setattr(cfg, "slack_app_token", "xapp-1")
    monkeypatch.setattr(cfg, "owner_user_id", "U1")
    d = app.Dashboard(store, port=0)
    url = d.start()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("bot", app.connect_slack(store, d, url, opened.append)))
    t.start()
    for _ in range(100):
        if d.setup_mode:
            break
        threading.Event().wait(0.02)
    assert d.setup_mode and opened == [url + "setup"] and "토큰이 맞지 않습니다" in d.slack_result[0]
    cfg.slack_bot_token = "xoxb-good"
    d.slack_ready.set()
    t.join(5)
    d.stop()
    assert result["bot"] is made[0] and d.setup_mode is False


def test_app_does_not_hide_network_errors_behind_setup(store, monkeypatch):
    import dugwali.ingest.slack as slack_mod

    def boom(store):
        raise ConnectionError("network down")

    monkeypatch.setattr(slack_mod, "Bot", boom)
    for k, v in (("slack_bot_token", "xoxb-1"), ("slack_app_token", "xapp-1"), ("owner_user_id", "U1")):
        monkeypatch.setattr(cfg, k, v)
    with pytest.raises(ConnectionError):          # run.bat이 잠시 뒤 다시 켠다
        app.connect_slack(store, None, None)


def test_app_without_dashboard_stops_on_missing_slack(store, monkeypatch):
    monkeypatch.setattr(cfg, "slack_bot_token", "")
    with pytest.raises(SystemExit) as err:
        app.connect_slack(store, None, None)
    assert err.value.code == 2


# ---------- 프로필 인터뷰 ----------

def test_profile_interview_creates_profile(dash, no_personal_profile):
    _, html = dash.get("/")
    assert "프로필 만들기" in html
    _, html = dash.get("/profile")
    assert "무슨 일을 하나요?" in html and 'value="sales-pm"' in html and "영업·사업개발 PM" in html
    _, html = dash.post("/profile/interview", role="기업교육 PM", boss="박팀장", base="sales-pm", tone="polite",
                        rules="금액은 부가세 포함 여부 함께 적기", terms="가견적 = 규모 파악용 견적", greeting="")
    assert "프로필을 만들었습니다" in html
    p = profile.load()
    assert p.exists and not p.warnings
    assert "- 역할: 기업교육 PM" in p.sections["나"] and "- 주 보고 대상: 박팀장" in p.sections["나"]
    assert "~했습니다" in p.sections["말투"] and "부가세 포함 여부" in p.sections["정리 기준"]
    assert "고객이 확답하지 않은 것" in p.sections["정리 기준"]                 # 고른 예시의 기준은 남는다
    assert p.slack_order == ["할 일", "결정", "확인 필요"]
    _, html = dash.post("/profile/interview", role="다른 사람")
    assert "이미 있습니다" in html and "기업교육 PM" in no_personal_profile.read_text("utf-8")


def test_profile_edit_keeps_backup(dash, no_personal_profile):
    dash.post("/profile/interview", base="default")
    _, html = dash.get("/profile")
    assert "프로필 고치기" in html and "<textarea name=\"text\"" in html
    old = no_personal_profile.read_text("utf-8")
    _, html = dash.post("/profile/save", text="## 양식\n- 슬랙 순서: 할 일, 이상한항목\n")
    assert "확인할 것 1개" in html and "모르는 항목" in html
    assert Path(str(no_personal_profile) + ".bak").read_text("utf-8") == old
    assert "비어 있는" in dash.post("/profile/save", text="  ")[1]


# ---------- 규칙 승격 ----------

_n = iter(range(1000))


def _edit_conclusion(store, n):
    for _ in range(n):
        i = next(_n)
        rec = pipeline.process(store, f"메모 {i}")
        pipeline.edit_record(store, rec["id"], {"conclusion": f"[기한 {i}일] " + SAMPLE["conclusion"]})


def test_three_edits_of_a_field_become_a_rule_candidate(dash, store, fake_llm, no_personal_profile, monkeypatch):
    _edit_conclusion(store, 2)
    assert pipeline.rule_candidates(store) == []
    _edit_conclusion(store, 1)
    (c,) = pipeline.rule_candidates(store)
    assert c["field"] == "conclusion" and len(c["items"]) == 3
    dash.post("/profile/interview", base="default")
    _, html = dash.get("/profile")
    assert "규칙으로 올릴까요? 1개" in html and "결론 · 3번 고침" in html and html.count("[기한 ") == 3
    assert '프로필 <span class="badge warn">1</span>' in html

    prompts = []
    monkeypatch.setattr(llm, "call_json", lambda p: prompts.append(p) or {"rule": "결론 첫머리에 기한 넣기",
                                                                         "reason": "모두 앞에 기한을 붙임"})
    _, html = dash.post("/rules/conclusion/draft")
    assert "초안을 만들었습니다" in html and "결론 첫머리에 기한 넣기</textarea>" in html and "모두 앞에 기한" in html
    assert prompts[0].count("[기한 ") == 3 and "내가 할 일과 상대가 해 줄 일을 구분" in prompts[0]   # 사례와 기존 규칙을 보낸다

    _, html = dash.post("/rules/conclusion/add", rule="결론 첫머리에 기한 넣기 (예: [D-3])")
    assert "정리 기준에 넣었습니다" in html and "규칙으로 올릴까요?" not in html
    assert profile.load().sections["정리 기준"].endswith("- 결론 첫머리에 기한 넣기 (예: [D-3])")
    assert pipeline.rule_candidates(store) == []
    _edit_conclusion(store, 2)
    assert pipeline.rule_candidates(store) == []                            # 처리한 뒤로 다시 3번 고쳐야 묻는다


def test_rule_candidate_can_be_dismissed_or_written_by_hand(dash, store, fake_llm, no_personal_profile):
    _edit_conclusion(store, 3)
    _, html = dash.post("/rules/conclusion/dismiss")
    assert "넘겼습니다" in html and pipeline.rule_candidates(store) == [] and not no_personal_profile.exists()
    _edit_conclusion(store, 3)
    _, html = dash.post("/rules/conclusion/add", rule="")
    assert "규칙이 비어 있습니다" in html and pipeline.rule_candidates(store)
    dash.post("/rules/conclusion/add", rule="- 기한을 결론 앞에")          # 프로필이 없으면 기본 예시로 만들고 더한다
    assert profile.load().sections["정리 기준"].endswith("- 기한을 결론 앞에")


def test_due_date_edits_are_not_style_rules(store, fake_llm):
    rec = pipeline.process(store, "메모")
    tid = store.case_todos(rec["case_id"])[0]["id"]
    for d in ("2026-12-01", "2026-12-02", "2026-12-03"):
        pipeline.edit_todo(store, tid, "수정 견적서 작성", None, d)
    assert pipeline.rule_candidates(store) == []


# ---------- 설치 ----------

def test_deps_install_only_what_settings_need(tmp_path):
    assert deps.wanted({}) == ["requirements.txt", "requirements-stt.txt"]
    assert deps.wanted({"STT_PROVIDER": "api", "LLM_PROVIDER": "api"}) == ["requirements.txt", "requirements-api.txt"]
    env, stamp, runs = tmp_path / ".env", tmp_path / "stamp", []
    env.write_text("STT_PROVIDER=api\n", encoding="utf-8")
    assert deps.main(env, stamp, lambda a: runs.append(a) or 0) == 0 and len(runs) == 1
    assert "requirements-stt.txt" not in " ".join(runs[0])
    deps.main(env, stamp, lambda a: runs.append(a) or 0)
    assert len(runs) == 1                                                   # 바뀐 게 없으면 pip를 돌리지 않는다
    env.write_text("STT_PROVIDER=local\n", encoding="utf-8")
    deps.main(env, stamp, lambda a: runs.append(a) or 0)
    assert len(runs) == 2 and "requirements-stt.txt" in " ".join(runs[1])
    env.write_text("STT_PROVIDER=api\n", encoding="utf-8")
    assert deps.main(env, stamp, lambda a: 1) == 1
    assert deps.main(env, stamp, lambda a: runs.append(a) or 0) == 0 and len(runs) == 3   # 실패하면 다음에 다시 시도
