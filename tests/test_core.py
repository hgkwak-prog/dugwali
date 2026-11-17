"""LLM·슬랙·오디오 없이 도는 테스트. 실행: pytest"""
import os
import time
from datetime import date, datetime

import pytest

from dugwali import app, digest, pipeline, profile
from dugwali.agent import extract, llm, render
from dugwali.config import cfg
from dugwali.ingest.watcher import CallWatcher
import sqlite3

from dugwali.ingest import slack as slack_ui
from dugwali.ingest import transcript
from dugwali.web import Dashboard
from dugwali.store import Store
from dugwali.stt.transcribe import assign_speakers, format_transcript

SAMPLE = {
    "kind": "통화",
    "title": "견적서 재요청",
    "conclusion": "A사가 수정 견적서를 금요일까지 요청함",
    "counterpart": "A사 김과장",
    "decisions": ["단가는 기존 유지", "수량만 200개로 변경"],
    "todos": [
        {"task": "수정 견적서 작성", "owner": "나", "due": "2026-11-20"},
        {"task": "납기 가능일 회신", "owner": "A사 김과장", "due": None},
    ],
    "open_questions": ["부가세 포함 여부"],
    "background": ["지난주 견적은 100개 기준"],
    "resume_point": None,
    "share_to": "박팀장",
    "ask_user": ["견적서는 누구 검토를 받아야 하나요?"],
}


@pytest.fixture(autouse=True)
def no_personal_profile(tmp_path, monkeypatch):
    """테스트는 이 PC의 실제 프로필과 무관하게 기본값으로 돈다."""
    monkeypatch.setattr(cfg, "_profile_path", str(tmp_path / "profile.md"))
    return tmp_path / "profile.md"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "data_dir", tmp_path)
    monkeypatch.setattr(cfg, "log_dir", tmp_path / "log")
    monkeypatch.setattr(cfg, "transcript_dir", tmp_path / "tr")
    return Store(tmp_path / "t.sqlite3")


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return dict(SAMPLE)

    monkeypatch.setattr(llm, "call_json", fake)
    return calls


def test_mask_hides_contacts_but_keeps_content():
    out = llm.mask("김과장 010-1234-5678, kim@a-corp.co.kr 로 300만원 견적, 주민 900101-1234567, 02-555-1234")
    assert "1234-5678" not in out and "kim@" not in out and "900101" not in out and "555-1234" not in out
    assert "김과장" in out and "300만원" in out


def test_mask_amounts_optional(monkeypatch):
    monkeypatch.setattr(cfg, "mask_amounts", True)
    assert "300" not in llm.mask("300만원 견적")


def test_parse_json_tolerates_fences():
    assert llm.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(llm.LLMError):
        llm.parse_json_object("죄송합니다")


def test_normalize_repairs_bad_output():
    d = extract.normalize({"kind": "잡담", "todos": ["자료 보내기", {"task": " "}], "ask_user": ["a", "b", "c"],
                           "counterpart": "null"})
    assert d["kind"] == "기타" and d["counterpart"] is None
    assert d["todos"] == [{"task": "자료 보내기", "owner": None, "due": None}]
    assert len(d["ask_user"]) == 2 and d["decisions"] == []


def test_extract_masks_before_sending(fake_llm):
    extract.extract("김과장 010-1234-5678 통화", now=datetime(2026, 11, 13))
    assert "010-1234-5678" not in fake_llm[0] and "2026-11-13 (금요일)" in fake_llm[0]


def test_renderers_lead_with_conclusion():
    d = extract.normalize(SAMPLE)
    s = render.slack(d)
    assert s.index("결론") < s.index("결정") < s.index("할 일")
    m = render.mail(d)
    assert m.splitlines()[0] == "제목: [통화] 견적서 재요청"
    assert "수정 견적서 작성 (나, 2026-11-20까지)" in m
    assert "박팀장님" in m
    v = render.verbal(d).splitlines()
    assert v[1].startswith("결론부터") and "2026-11-20까지 수정 견적서 작성" in render.verbal(d)
    d["todos"][0]["due"] = "금요일까지"
    assert "금요일까지까지" not in render.verbal(d) + render.slack(d)
    assert render.verbal({**d, "spoken": "말씀드리겠습니다."}) == "말씀드리겠습니다."


def test_process_amend_and_todos(store, fake_llm):
    rec = pipeline.process(store, "대충 쓴 메모")
    assert len(store.open_todos()) == 2
    log_file = cfg.log_dir / f"{rec['created_at'][:10]}.md"
    assert "견적서 재요청" in log_file.read_text(encoding="utf-8")

    mine = next(t for t in store.open_todos() if t["owner"] == "나")
    store.complete_todo(mine["id"])
    rec = pipeline.amend(store, rec["id"], "검토는 박팀장")
    assert "[작성자 보충] 검토는 박팀장" in rec["raw_text"]
    # 재추출해도 완료한 항목이 되살아나지 않고, 로그도 한 건만 유지
    assert [t["task"] for t in store.open_todos()] == ["납기 가능일 회신"]
    assert log_file.read_text(encoding="utf-8").count("## ") == 1


def test_hold_does_not_call_llm_until_summarize(store, fake_llm):
    rec = pipeline.hold(store, "화자1: 안녕하세요", "call")
    assert rec["data"] is None and fake_llm == []
    assert pipeline.summarize(store, rec["id"])["data"]["title"] == "견적서 재요청"
    assert len(fake_llm) == 1


def test_digest_flags_overdue_and_splits_owners(store, fake_llm):
    pipeline.process(store, "메모")
    text = digest.build(store, date.today())
    assert "오늘 다룬 건 1개 (기록 1건)" in text
    assert "◦ 수정 견적서 작성 ·" in text and "◦ A사 김과장: 납기 가능일 회신 · 기한 미정" in text
    assert digest.due_tag("2026-11-10", date(2026, 11, 13)).startswith("⚠️ 3일 지남")
    assert digest.due_tag("2026-11-14", date(2026, 11, 13)) == "내일까지"
    assert digest.due_tag("다음 주 중", date(2026, 11, 13)) == "다음 주 중"


def test_watcher_waits_for_stable_file_and_skips_old(store, tmp_path):
    folder = tmp_path / "calls"
    folder.mkdir()
    old = folder / "old.m4a"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    seen = []
    w = CallWatcher(folder, store, seen.append, since=time.time() - 60)
    new = folder / "new.m4a"
    new.write_bytes(b"abc")
    (folder / "note.txt").write_text("x")
    assert w.scan_once() == [] and seen == []      # 첫 관측: 크기만 기억
    assert w.scan_once() == [new] and seen == [new]  # 크기 안정 → 처리
    assert w.scan_once() == []                      # 중복 처리 없음


def test_speaker_assignment_and_merge():
    segs = [{"start": 0, "end": 2, "text": "여보세요"}, {"start": 2, "end": 4, "text": "네 안녕하세요"},
            {"start": 4, "end": 6, "text": "견적 건인데요"}]
    turns = [(0.0, 2.1, "SPEAKER_01"), (2.1, 6.0, "SPEAKER_00")]
    out = format_transcript(assign_speakers(segs, turns))
    assert out == "화자1: 여보세요\n화자2: 네 안녕하세요 견적 건인데요"


# ---------- 건(case) ----------

def test_old_db_is_migrated_one_case_per_record(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source TEXT NOT NULL,
            raw_text TEXT NOT NULL, data TEXT, channel TEXT, thread_ts TEXT, msg_ts TEXT, audio_path TEXT);
        INSERT INTO records (created_at, source, raw_text, data) VALUES
            ('2026-11-13T10:00:00', 'slack', 'a', '{"title": "옛 기록", "todos": []}'),
            ('2026-11-13T11:00:00', 'call', 'b', NULL);
    """)
    db.commit()
    db.close()
    store = Store(path)
    cases = store.open_cases()
    assert sorted(c["title"] for c in cases) == ["미정리 기록", "옛 기록"]
    assert all(c["n_records"] == 1 for c in cases)
    assert Store(path).open_cases() == cases  # 다시 열어도 중복 이전 없음


def test_new_record_suggests_but_never_auto_merges(store, monkeypatch):
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return {**SAMPLE, "related_case": 1 if len(prompts) > 1 else 99}

    monkeypatch.setattr(llm, "call_json", fake)
    first = pipeline.process(store, "첫 메모")
    assert "[진행 중인 건]\n#" not in prompts[0] and first["data"]["related_case"] is None  # 없는 번호는 버림
    second = pipeline.process(store, "후속 메모")
    assert "#1 견적서 재요청" in prompts[1]
    assert second["data"]["related_case"] == 1 and second["case_id"] != first["case_id"]
    assert len(store.open_cases()) == 2


def test_merge_moves_record_keeps_todos_and_drops_empty_case(store, fake_llm):
    a, b = pipeline.process(store, "하나"), pipeline.process(store, "둘")
    merged = pipeline.merge(store, b["id"], a["case_id"])
    assert merged["case_id"] == a["case_id"] and merged["data"]["related_case"] is None
    cases = store.open_cases()
    assert [c["id"] for c in cases] == [a["case_id"]]
    assert cases[0]["n_records"] == 2 and cases[0]["n_todos"] == 2   # 기록마다 겹친 할 일은 하나로 정리
    folder = cfg.log_dir / "건별"
    assert [f.name for f in folder.iterdir()] == [f"{a['case_id']:03d}-견적서-재요청.md"]


def test_case_summary_uses_llm_once_and_skips_unapproved_calls(store, fake_llm):
    a = pipeline.process(store, "첫 통화 내용")
    assert pipeline.case_summary(store, a["case_id"]) == a["data"] and len(fake_llm) == 1  # 기록 1개면 LLM 안 씀
    b = pipeline.process(store, "후속 지시 내용")
    held = pipeline.hold(store, "비밀 통화 전사문", "call")
    n = len(fake_llm)
    pipeline.merge(store, b["id"], a["case_id"])      # 합치면 바로 통합 정리해 할 일을 맞춘다
    assert len(fake_llm) == n + 1
    mine = next(t for t in store.open_todos() if t["owner"] == "나")
    store.complete_todo(mine["id"])
    pipeline.merge(store, held["id"], a["case_id"])
    n = len(fake_llm)
    assert "[이미 완료 처리한 할 일" in fake_llm[-1] and "- 수정 견적서 작성" in fake_llm[-1]
    assert [t["task"] for t in store.open_todos()] == ["납기 가능일 회신"]   # 완료한 항목은 되살아나지 않음
    assert "[기록 1]" in fake_llm[-1] and "첫 통화 내용" in fake_llm[-1] and "후속 지시 내용" in fake_llm[-1]
    assert "비밀 통화 전사문" not in fake_llm[-1]
    pipeline.case_summary(store, a["case_id"])
    assert len(fake_llm) == n                          # 캐시
    pipeline.amend(store, b["id"], "보충")
    pipeline.case_summary(store, a["case_id"])
    assert len(fake_llm) == n + 2                      # 보충 1회 + 다시 종합 1회


def test_delete_removes_todos_logs_and_empty_case(store, fake_llm):
    a, b = pipeline.process(store, "실제 업무"), pipeline.process(store, "테스트")
    day_file = cfg.log_dir / f"{a['created_at'][:10]}.md"
    assert day_file.read_text(encoding="utf-8").count("## ") == 2
    pipeline.delete(store, b["id"])
    assert len(store.open_todos()) == 2 and len(store.open_cases()) == 1
    assert day_file.read_text(encoding="utf-8").count("## ") == 1
    assert pipeline.delete_case(store, a["case_id"]) == 1
    assert store.open_cases() == [] and store.open_todos() == [] and not day_file.exists()
    assert list((cfg.log_dir / "건별").iterdir()) == []
    assert "오늘 남긴 기록이 없습니다" in digest.build(store)


def test_close_hides_case_from_digest_and_completes_todos(store, fake_llm):
    a = pipeline.process(store, "하나")
    pipeline.close(store, a["case_id"])
    assert store.open_cases() == [] and store.open_todos() == []
    assert "견적서 재요청" not in digest.build(store)
    b = pipeline.process(store, "후속")
    pipeline.merge(store, b["id"], a["case_id"])      # 종결한 건에 후속 기록을 붙이면 다시 열림
    assert [c["id"] for c in store.open_cases()] == [a["case_id"]]


def test_digest_groups_records_by_case(store, fake_llm):
    a, b = pipeline.process(store, "하나"), pipeline.process(store, "둘")
    pipeline.merge(store, b["id"], a["case_id"])
    text = digest.build(store)
    assert "오늘 다룬 건 1개 (기록 2건)" in text
    assert f"#{a['case_id']} 견적서 재요청* (기록 2건)" in text


def test_slack_blocks_offer_merge_and_delete(store, monkeypatch):
    n = []
    monkeypatch.setattr(llm, "call_json", lambda p: (n.append(1), {**SAMPLE, "related_case": 1})[1])
    a = pipeline.process(store, "하나")
    b = pipeline.process(store, "둘")
    ids = [e["action_id"] for blk in slack_ui.record_blocks(store, b) if blk["type"] == "actions"
           for e in blk["elements"]]
    assert ids == ["fmt_slack", "fmt_mail", "fmt_verbal", "merge_suggest", "merge_select", "delete_record"]
    solo = [e["action_id"] for blk in slack_ui.record_blocks(store, a) if blk["type"] == "actions"
            for e in blk["elements"]]
    assert "merge_suggest" not in solo and "merge_select" in solo   # 제안은 없어도 직접 고를 수는 있음
    listed = [e["action_id"] for blk in slack_ui.case_list_blocks(store) if blk["type"] == "actions"
              for e in blk["elements"]]
    assert listed == ["case_view", "case_close", "case_delete"] * 2
    view = slack_ui.case_view_blocks(store, a["case_id"], a["data"])
    assert view[-1]["elements"][0]["value"] == f"c{a['case_id']}"


# ---------- 조용히 사라지는 건 없애기 ----------

class FakeBot:
    """슬랙 없이 봇이 올린 것을 기록한다."""

    def __init__(self, store, fail=0):
        self.store, self.fail, self.sent = store, fail, []

    def _post(self, kind, payload):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("slack down")
        self.sent.append((kind, payload))

    def post_dm(self, text, blocks=None):
        self._post("dm", text)

    def post_record(self, rec, prefix=""):
        self._post("record", rec["id"])

    def post_held_call(self, rec, info):
        self._post("held", rec["id"])

    def post_failure(self, f):
        self._post("failure", f["id"])

    def send_digest(self, day=None):
        self._post("digest", day)


@pytest.fixture
def broken_llm(monkeypatch):
    state = {"broken": True}

    def fake(prompt):
        if state["broken"]:
            raise llm.LLMError("한도 초과")
        return dict(SAMPLE)

    monkeypatch.setattr(llm, "call_json", fake)
    return state


def test_raw_text_is_kept_when_llm_fails_and_retry_fills_it(store, broken_llm):
    with pytest.raises(pipeline.ProcessError) as e:
        pipeline.process(store, "중요한 메모", "cli")
    rec = e.value.record
    assert store.get_record(rec["id"])["raw_text"] == "중요한 메모" and store.get_record(rec["id"])["data"] is None
    with pytest.raises(pipeline.ProcessError):
        pipeline.summarize(store, rec["id"])
    (f,) = store.open_failures()                       # 같은 기록의 실패는 한 건으로 쌓인다
    assert f["stage"] == "extract" and f["record_id"] == rec["id"] and f["attempts"] == 2
    assert "처리 실패 1건" in digest.build(store) and "정리 안 된 기록" not in digest.build(store)

    broken_llm["broken"] = False
    assert pipeline.summarize(store, rec["id"])["data"]["title"] == "견적서 재요청"
    assert store.open_failures() == [] and len(store.open_todos()) == 2
    assert "조치 필요" not in digest.build(store)


def test_deleting_record_closes_its_failure(store, broken_llm):
    with pytest.raises(pipeline.ProcessError) as e:
        pipeline.process(store, "메모")
    pipeline.delete(store, e.value.record["id"])
    assert store.open_failures() == [] and digest.attention(store) == ([], [])


def test_held_call_shows_up_as_attention_in_digest(store, fake_llm):
    pipeline.hold(store, "화자1: 안녕하세요", "call")
    failures, pending = digest.attention(store)
    assert failures == [] and len(pending) == 1
    assert "정리 안 된 기록 1건" in digest.build(store)


def test_digest_puts_overdue_and_imminent_own_todos_first(store, monkeypatch):
    today = date(2026, 11, 17)
    monkeypatch.setattr(llm, "call_json", lambda p: {**SAMPLE, "todos": [
        {"task": "먼 일", "owner": "나", "due": "2026-11-30"},
        {"task": "내일 일", "owner": "나", "due": "2026-11-18"},
        {"task": "지난 일", "owner": None, "due": "2026-11-14"},
        {"task": "남의 지난 일", "owner": "김과장", "due": "2026-11-10"},
    ]})
    pipeline.process(store, "메모")
    lines = digest.build(store, today).splitlines()
    assert lines[1] == "*먼저 할 일 2건*"
    assert "3일 지남" in lines[2] and "지난 일 — #1 견적서 재요청" in lines[2]
    assert lines[3].startswith("• 내일까지 · 내일 일")
    rest = "\n".join(lines[4:])
    assert "지난 일 —" not in rest                       # 위에 올린 것은 아래에서 되풀이하지 않는다
    assert rest.index("◦ 먼 일") < rest.index("◦ 김과장: 남의 지난 일")   # 건 안에서는 내 할 일 먼저


def test_call_failure_is_recorded_notified_and_retryable(store, fake_llm, tmp_path, monkeypatch):
    import dugwali.stt.transcribe as stt

    state = {"broken": True}

    def fake_transcribe(path, speakers=2, diarize=None, provider=None):
        if state["broken"]:
            raise RuntimeError("모델 로드 실패")
        return {"text": "화자1: 견적 건입니다", "duration": 60.0, "elapsed": 5.0, "diarized": False}

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    cfg.ensure_dirs()
    bot, path = FakeBot(store), tmp_path / "call.m4a"
    f = app.handle_call(bot, store, path)
    assert f["stage"] == "call" and bot.sent == [("failure", f["id"])]
    assert app.handle_call(bot, store, path, notify=False)["attempts"] == 2 and len(bot.sent) == 1

    state["broken"] = False
    assert app.handle_call(bot, store, path) is None and store.open_failures() == []
    assert bot.sent[-1][0] == "held"                    # 기본 설정: 전사문은 보관만 하고 버튼을 낸다
    assert app.handle_call(bot, store, path) is None    # 이미 기록이 있으면 다시 전사하지 않는다
    assert len(store.unprocessed_records()) == 1


def test_call_is_not_lost_when_slack_post_fails(store, fake_llm, tmp_path, monkeypatch):
    import dugwali.stt.transcribe as stt

    calls = []
    monkeypatch.setattr(stt, "transcribe", lambda path, speakers=2, diarize=None, provider=None: calls.append(path) or {
        "text": "화자1: 견적 건입니다", "duration": 60.0, "elapsed": 5.0, "diarized": False})
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    cfg.ensure_dirs()
    bot, path = FakeBot(store, fail=2), tmp_path / "call.m4a"   # 초안 올리기도, 실패 알림도 실패
    f = app.handle_call(bot, store, path)
    assert f["stage"] == "call" and bot.sent == [] and len(store.open_failures()) == 1
    assert app.handle_call(bot, store, path, notify=False) is None
    assert bot.sent == [("record", 1)] and len(calls) == 1      # 다시 전사하지 않고 올리기만 한다


def test_digest_is_sent_once_per_day_and_retried_with_backoff(store, fake_llm):
    t = datetime(2026, 11, 17, 17, 30)                          # 화요일
    bot = FakeBot(store)
    app.digest_tick(bot, store, datetime(2026, 11, 17, 17, 29))
    app.digest_tick(bot, store, datetime(2026, 11, 14, 18, 0))  # 토요일
    assert bot.sent == []
    app.digest_tick(bot, store, t)
    app.digest_tick(bot, store, t.replace(hour=18))
    app.digest_tick(FakeBot(store), store, t.replace(hour=19))  # 재시작해도 다시 보내지 않는다
    assert [k for k, _ in bot.sent] == ["digest"]

    day2 = datetime(2026, 11, 18, 17, 30)
    down = FakeBot(store, fail=99)
    app.digest_tick(down, store, day2)
    app.digest_tick(down, store, day2.replace(minute=35))       # 10분이 안 지나면 다시 시도하지 않는다
    assert down.fail == 98
    app.digest_tick(down, store, day2.replace(minute=41))
    assert store.open_failures() == []
    app.digest_tick(down, store, day2.replace(minute=52))       # 세 번째 실패: 실패 항목으로 남긴다
    app.digest_tick(down, store, day2.replace(hour=19))         # 그 뒤로는 더 시도하지 않는다
    assert down.fail == 96
    (f,) = store.open_failures()
    assert f["stage"] == "digest" and f["target"] == "2026-11-18"

    app.digest_tick(bot, store, datetime(2026, 11, 19, 17, 30)) # 다음 날은 정상
    assert [k for k, _ in bot.sent] == ["digest", "digest"]


def test_watcher_reports_missed_recordings_instead_of_dropping(store, tmp_path):
    folder = tmp_path / "calls"
    folder.mkdir()
    now = time.time()

    def make(name, age):
        p = folder / name
        p.write_bytes(b"abc")
        os.utime(p, (now - age, now - age))
        return p

    make("before-install.m4a", 7200)
    missed = make("while-off.m4a", 1800)
    asked, seen = [], []
    w = CallWatcher(folder, store, seen.append, since=now - 60, baseline=now - 3600, on_missed=asked.append)
    w.scan_once()
    w.scan_once()
    w.scan_once()
    assert asked == [[missed]] and seen == []           # 한 번만 묻고, 자동으로 처리하지 않는다
    assert w.missed() == [missed]
    again = CallWatcher(folder, store, seen.append, since=now, baseline=now - 3600)
    assert again.missed() == [missed]                   # 답하지 않으면 다음에 켤 때 다시 나온다
    assert again.skip_missed() == 1 and again.missed() == [] and w.missed() == []


def test_slack_attention_blocks_list_failures_pending_and_missed(store, broken_llm, tmp_path):
    with pytest.raises(pipeline.ProcessError):
        pipeline.process(store, "메모")
    pipeline.hold(store, "전사문 내용", "call", str(tmp_path / "call.m4a"))
    blocks = slack_ui.attention_blocks(store, [tmp_path / "old.m4a"])
    ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["retry_failure", "dismiss_failure", "pending_summarize", "pending_delete",
                   "missed_process", "missed_skip"]
    text = " ".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "한도 초과" in text and "call.m4a" in text and "old.m4a" in text
    assert "전사문 내용" not in text                     # 승인 전 전사문은 슬랙에도 싣지 않는다
    store.resolve_failures(fid=store.open_failures()[0]["id"])
    pipeline.delete(store, 1)
    pipeline.delete(store, 2)
    clear = slack_ui.attention_blocks(store, None)[0]["text"]["text"]
    assert clear.startswith("막힌 건이 없습니다. (처리 실패 0건 · 정리 안 된 기록 0건 · 놓친 통화 녹음 확인 안 함(감시 꺼짐))")
    assert "놓친 통화 녹음 0개" in slack_ui.attention_blocks(store, [])[0]["text"]["text"]


class FakeClient:
    def __init__(self):
        self.n, self.messages = 0, {}   # ts → {"text", "blocks"}

    def chat_postMessage(self, channel, text=None, blocks=None, thread_ts=None):
        self.n += 1
        ts = f"{self.n}.0"
        self.messages[ts] = {"text": text, "blocks": blocks or []}
        return {"ts": ts, "channel": channel}

    def chat_update(self, channel, ts, text=None, blocks=None):
        self.messages[ts] = {"text": text, "blocks": blocks or []}

    def reactions_add(self, **kw):
        pass

    def conversations_open(self, users):
        return {"channel": {"id": "D1"}}

    def actions(self, ts):
        return {e["action_id"]: e["value"] for b in self.messages[ts]["blocks"] if b["type"] == "actions"
                for e in b["elements"] if "value" in e}


class FakeApp:
    def __init__(self, token=None):
        self.client, self.handlers = FakeClient(), {}

    def event(self, name):
        return lambda fn: self.handlers.setdefault(name, fn)

    action = event


@pytest.fixture
def bot(store, monkeypatch):
    monkeypatch.setattr(slack_ui, "App", FakeApp)
    monkeypatch.setattr(cfg, "owner_user_id", "U1")
    b = slack_ui.Bot(store)

    def say(text, ts="100.0"):
        b.app.handlers["message"]({"channel_type": "im", "user": "U1", "channel": "D1", "ts": ts, "text": text},
                                  b.app.client)

    def click(action_id, ts):
        client = b.app.client
        body = {"actions": [{"value": client.actions(ts)[action_id]}], "channel": {"id": "D1"},
                "message": {"ts": ts}}
        b.app.handlers[action_id](lambda: None, body, client)

    b.say, b.click = say, click
    return b


def test_slack_memo_failure_can_be_retried_in_place(bot, store, broken_llm):
    client = bot.app.client
    bot.say("급한 메모")
    assert set(client.actions("1.0")) == {"retry_failure", "dismiss_failure"}
    assert store.get_record(1)["raw_text"] == "급한 메모"
    bot.click("retry_failure", "1.0")                   # 아직 고장: 같은 자리에 실패가 다시 뜬다
    assert "retry_failure" in client.actions("1.0") and store.open_failures()[0]["attempts"] == 2
    broken_llm["broken"] = False
    bot.click("retry_failure", "1.0")
    assert "fmt_slack" in client.actions("1.0") and store.open_failures() == []   # 그 자리가 초안이 된다
    assert len(client.messages) == 1


def test_slack_attention_list_retries_and_refreshes(bot, store, broken_llm):
    client = bot.app.client
    bot.say("메모 하나")
    held = pipeline.hold(store, "전사문", "call")
    bot.post_held_call(held, "통화 전사 완료")           # 2.0
    bot.say("조치", ts="101.0")                          # 3.0
    assert "처리 실패 1건 · 정리 안 된 기록 1건" in client.messages["3.0"]["blocks"][0]["text"]["text"]
    broken_llm["broken"] = False
    bot.click("retry_failure", "3.0")
    assert "fmt_slack" in client.actions("1.0")         # 원래 자리에 초안이 그려지고
    assert set(client.actions("3.0")) == {"pending_summarize", "pending_delete"}   # 목록은 줄어든다
    bot.click("pending_summarize", "3.0")
    assert "fmt_slack" in client.actions("2.0")
    assert client.messages["3.0"]["blocks"][0]["text"]["text"].startswith("막힌 건이 없습니다.")


def test_slack_dismiss_keeps_raw_record_listed(bot, store, broken_llm):
    bot.say("메모")
    bot.click("dismiss_failure", "1.0")
    assert store.open_failures() == [] and "정리 안 된 기록" in bot.app.client.messages["1.0"]["text"]
    assert len(digest.attention(store)[1]) == 1


# ---------- 전사·LLM 방식 선택 ----------

def test_llm_retries_transient_failure_once_but_not_config_errors(monkeypatch):
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise llm.LLMError("일시 오류")
        return '{"ok": true}'

    monkeypatch.setitem(llm._PROVIDERS, "claude_code", flaky)
    assert llm.call_json("p") == {"ok": True} and len(calls) == 2

    def always(prompt):
        calls.append(prompt)
        raise llm.LLMError("계속 오류")

    monkeypatch.setitem(llm._PROVIDERS, "claude_code", always)
    with pytest.raises(llm.LLMError):
        llm.call_json("p")
    assert len(calls) == 4                              # 처음 + 재시도 1회

    def missing(prompt):
        calls.append(prompt)
        raise llm.LLMConfigError("설치 안 됨")

    monkeypatch.setitem(llm._PROVIDERS, "claude_code", missing)
    with pytest.raises(llm.LLMConfigError):
        llm.call_json("p")
    assert len(calls) == 5                              # 설정 문제는 다시 시도하지 않는다
    monkeypatch.setattr(cfg, "llm_provider", "gpt")
    with pytest.raises(llm.LLMConfigError):
        llm.call_json("p")


def test_llm_api_provider_builds_request_and_reads_text(monkeypatch):
    import sys
    import types

    seen = {}

    class Block:
        def __init__(self, type_, text=""):
            self.type, self.text = type_, text

    class Resp:
        stop_reason = "end_turn"
        content = [Block("thinking"), Block("text", '{"ok": true}')]

    class Messages:
        def create(self, **kw):
            seen["req"] = kw
            return Resp()

    class Client:
        def __init__(self, **opts):
            seen["opts"] = opts
            self.messages = Messages()
            self.beta = types.SimpleNamespace(messages=Messages())

    fake = types.ModuleType("anthropic")
    fake.Anthropic = Client
    for name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError",
                 "APIStatusError", "APIConnectionError", "AnthropicError"):
        setattr(fake, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(cfg, "llm_provider", "api")
    monkeypatch.setattr(cfg, "llm_api_key", "k")
    assert llm.call_json("프롬프트") == {"ok": True}
    req = seen["req"]
    assert seen["opts"]["api_key"] == "k" and req["model"] == "claude-opus-5-5"
    assert req["messages"] == [{"role": "user", "content": "프롬프트"}] and req["system"] == llm.SYSTEM_PROMPT
    assert req["output_config"] == {"effort": "low"} and req["fallbacks"] == "default"
    assert "temperature" not in req and "thinking" not in req

    monkeypatch.setattr(cfg, "llm_api_base_url", "https://llm.example.internal")   # 사내 게이트웨이
    llm.call_json("p")
    assert seen["opts"]["base_url"] == "https://llm.example.internal" and "fallbacks" not in seen["req"]

    Resp.stop_reason = "refusal"
    with pytest.raises(llm.LLMConfigError):
        llm.call_json("p")


def test_daily_llm_limit_keeps_raw_and_lists_it(store, fake_llm, monkeypatch):
    monkeypatch.setattr(cfg, "llm_daily_limit", 2)
    pipeline.process(store, "하나")
    pipeline.process(store, "둘")
    with pytest.raises(pipeline.ProcessError) as e:
        pipeline.process(store, "셋")
    assert "상한(2회)" in str(e.value) and len(fake_llm) == 2
    assert store.get_record(e.value.record["id"])["raw_text"] == "셋" and len(store.open_failures()) == 1
    store.set_state("llm_calls", '{"day": "2000-01-01", "n": 2}')   # 날짜가 바뀌면 다시 센다
    assert pipeline.summarize(store, e.value.record["id"])["data"] and len(fake_llm) == 3
    monkeypatch.setattr(cfg, "llm_daily_limit", 0)                  # 0이면 무제한
    pipeline.process(store, "넷")


def test_stt_api_sends_file_without_original_name(tmp_path, monkeypatch):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import dugwali.stt.transcribe as stt

    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"], got["auth"] = self.path, self.headers["Authorization"]
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            status, payload = (500, {"error": "boom"}) if got.get("fail") else (200, {"text": " 견적 건입니다 ",
                                                                                      "duration": 12.5})
            out = _json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        audio = tmp_path / "김과장_01012345678.m4a"
        audio.write_bytes(b"AUDIOBYTES")
        monkeypatch.setattr(cfg, "stt_provider", "api")
        monkeypatch.setattr(cfg, "stt_api_base_url", f"http://127.0.0.1:{server.server_port}/v1")
        with pytest.raises(RuntimeError, match="STT_API_KEY"):
            stt.transcribe(str(audio))
        monkeypatch.setattr(cfg, "stt_api_key", "sk-test")
        r = stt.transcribe(str(audio))
        assert r == {"text": "견적 건입니다", "duration": 12.5, "elapsed": r["elapsed"], "diarized": False,
                     "provider": "api"}
        assert got["path"] == "/v1/audio/transcriptions" and got["auth"] == "Bearer sk-test"
        body = got["body"]
        assert b"AUDIOBYTES" in body and b'name="model"\r\n\r\nwhisper-1' in body and b'name="language"' in body
        assert b'filename="audio.m4a"' in body and "김과장".encode() not in body and b"01012345678" not in body
        got["fail"] = True
        with pytest.raises(RuntimeError, match="전사 API 오류 500"):
            stt.transcribe(str(audio))
    finally:
        server.shutdown()
    with pytest.raises(ValueError):
        stt.transcribe(str(audio), provider="cloud")


@pytest.fixture
def voice(bot, monkeypatch, tmp_path):
    """음성 파일이 붙은 DM을 보낸다. state["error"]가 있으면 전사가 그 오류로 실패한다."""
    import dugwali.stt.transcribe as stt

    state = {"error": None, "providers": []}

    def fake_transcribe(path, speakers=2, diarize=None, provider=None):
        state["providers"].append(provider)
        if state["error"] and provider != "local":
            raise RuntimeError(state["error"])
        return {"text": "내일까지 견적서 보내기", "duration": 5.0, "elapsed": 1.0, "diarized": False,
                "provider": provider or "local"}

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(bot, "_download", lambda f: tmp_path / "memo.m4a")

    def send(text=""):
        bot.app.handlers["message"]({"channel_type": "im", "user": "U1", "channel": "D1", "ts": "100.0",
                                     "text": text, "files": [{"id": "F1", "name": "memo.m4a"}]}, bot.app.client)

    state["send"] = send
    return state


def test_voice_memo_waits_for_approval_by_default(bot, store, voice, fake_llm):
    voice["send"]("메모")
    assert fake_llm == [] and store.get_record(1)["raw_text"] == "메모\n내일까지 견적서 보내기"
    assert set(bot.app.client.actions("1.0")) == {"summarize_call", "delete_record"}   # 통화와 같은 게이트
    bot.click("summarize_call", "1.0")
    assert "fmt_slack" in bot.app.client.actions("1.0") and len(fake_llm) == 1


def test_voice_memo_goes_straight_through_when_sending_is_approved(bot, store, voice, fake_llm, monkeypatch):
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    voice["send"]()
    assert "fmt_slack" in bot.app.client.actions("1.0") and store.get_record(1)["source"] == "voice"


def test_voice_transcription_failure_is_listed_and_can_fall_back_to_local(bot, store, voice, fake_llm, monkeypatch):
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    monkeypatch.setattr(cfg, "stt_provider", "api")
    monkeypatch.setattr(slack_ui, "local_available", lambda: True)
    voice["error"] = "전사 API 오류 500"
    voice["send"]()
    client = bot.app.client
    assert set(client.actions("1.0")) == {"retry_failure", "retry_failure_local", "dismiss_failure"}
    (f,) = store.open_failures()
    assert f["stage"] == "voice" and digest.attention(store) == ([f], [])   # 조치 목록에 실패로 한 번만 나온다
    bot.click("retry_failure", "1.0")
    assert store.open_failures()[0]["attempts"] == 2
    bot.click("retry_failure_local", "1.0")
    assert voice["providers"] == [None, None, "local"]
    assert "fmt_slack" in client.actions("1.0") and store.open_failures() == []


def test_dismissing_untranscribed_voice_memo_drops_the_empty_record(bot, store, voice):
    voice["error"] = "모델 로드 실패"
    voice["send"]()
    assert "retry_failure_local" not in bot.app.client.actions("1.0")       # 로컬 방식이면 버튼이 하나뿐
    bot.click("dismiss_failure", "1.0")
    assert store.get_record(1) is None and digest.attention(store) == ([], [])
    assert "음성 파일은 그대로" in bot.app.client.messages["1.0"]["text"]


# ---------- 개인 프로필 ----------

PROFILE = """# 내 프로필
<!-- 이 주석은 LLM에 가지 않는다 -->
## 나
- 역할: 기업교육 PM

## 말투
- 문장은 "~합니다"로 끝내기

## 양식
- 슬랙 순서: 할 일, 결정
- 메일 순서 : 결론 → 할 일
- 메일 인사말: 안녕하세요, 비즈팀입니다. {대상}님께 공유드립니다.
- 메일 맺음말: 고맙습니다.
- 슬랙 색깔: 파랑
- 메일 순서: 요약
"""


def test_profile_defaults_keep_current_output(no_personal_profile):
    p = profile.load()
    assert not p.exists and p.warnings == [] and list(p.sections) == ["말투"]
    assert "명사형 종결" in p.prompt_text and "기본값 사용" in profile.describe()


def test_profile_file_changes_prompt_and_formats(no_personal_profile, fake_llm):
    no_personal_profile.write_text(PROFILE, encoding="utf-8")
    p = profile.load()
    assert list(p.sections) == ["말투", "나"] and "~합니다" in p.sections["말투"]      # 내 말투가 기본값을 대신한다
    assert p.slack_order == ["할 일", "결정"] and p.mail_order == ["할 일"]
    assert len(p.warnings) == 2 and "슬랙 색깔" in p.warnings[0] and "'요약'" in p.warnings[1]

    extract.extract("메모")
    prompt = fake_llm[0]
    assert "[작성자 프로필]" in prompt and "역할: 기업교육 PM" in prompt and "~합니다" in prompt
    assert "주석" not in prompt and "슬랙 순서" not in prompt and "인턴" not in prompt
    assert "명사형 종결" not in prompt

    d = extract.normalize(SAMPLE)
    s = render.slack(d)
    assert s.index("결론") < s.index("할 일") < s.index("결정") and "확인 필요" not in s
    m = render.mail(d).splitlines()
    assert m[2] == "안녕하세요, 비즈팀입니다. 박팀장님께 공유드립니다." and m[-1] == "고맙습니다."
    assert "2. 할 일" in m and not any("결정 사항" in x or "배경" in x for x in m)
    assert "**결정**" in render.markdown(d, "10:00", "슬랙 메모")       # 업무 로그는 항목을 빼지 않는다

    time.sleep(0.01)
    no_personal_profile.write_text("## 양식\n- 메일 맺음말: 수고하세요.\n", encoding="utf-8")
    os.utime(no_personal_profile, (time.time() + 5, time.time() + 5))
    assert render.mail(d).splitlines()[-1] == "수고하세요."            # 파일을 고치면 재시작 없이 반영된다


def test_profile_init_copies_example_once(no_personal_profile):
    assert {"default", "sales-pm", "developer"} <= set(profile.examples())
    assert profile.init("sales-pm") == no_personal_profile
    p = profile.load()
    assert p.exists and p.warnings == [] and p.slack_order == ["할 일", "결정", "확인 필요"]
    with pytest.raises(FileExistsError):
        profile.init("default")
    with pytest.raises(ValueError):
        profile.init("없는예시")
    for name in profile.examples():                                   # 예시는 모두 경고 없이 읽혀야 한다
        text = (profile.EXAMPLES_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert profile.parse(text, no_personal_profile).warnings == []


def test_slack_profile_command_shows_how_it_was_read(bot, no_personal_profile):
    bot.say("프로필")
    assert "기본값 사용" in bot.app.client.messages["1.0"]["text"]
    no_personal_profile.write_text(PROFILE, encoding="utf-8")
    bot.say("프로필", ts="101.0")
    text = bot.app.client.messages["2.0"]["text"]
    assert "슬랙 순서: 결론 → 할 일 → 결정" in text and "⚠️" in text


# ---------- 전사문 가져오기 ----------

VTT = """WEBVTT

NOTE 회의 녹음
이 줄은 메모라서 빠진다

1
00:00:01.000 --> 00:00:03.000
<v 김과장>안녕하세요</v>

2
00:00:03.000 --> 00:00:05.000
<v 김과장>견적 건으로 <b>연락</b>드렸습니다

3
00:00:05.000 --> 00:00:06.000
<v 나>네 말씀하세요
"""
SRT = "1\n00:00:01,000 --> 00:00:03,000\n화자1: 금요일까지 부탁드립니다\n\n2\n00:00:03,000 --> 00:00:04,000\n화자1: 꼭이요\n"


def test_transcript_clean_strips_cue_numbers_and_times():
    assert transcript.clean(VTT) == "김과장: 안녕하세요 견적 건으로 연락드렸습니다\n나: 네 말씀하세요"
    assert transcript.clean(SRT) == "화자1: 금요일까지 부탁드립니다 꼭이요"
    assert transcript.clean("  그냥 메모\n둘째 줄 12  ") == "그냥 메모\n둘째 줄 12"


def test_import_follows_the_same_gate_as_calls(store, fake_llm, monkeypatch):
    rec = pipeline.import_transcript(store, "화자1: 안녕하세요", "A사 통화", "후속 건")
    assert rec["data"] is None and fake_llm == [] and rec["source"] == "import"
    assert rec["raw_text"] == "후속 건\n파일명: A사 통화\n화자1: 안녕하세요"
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    assert pipeline.import_transcript(store, "화자1: 네")["data"]["title"] == "견적서 재요청"


def test_slack_transcript_file_is_held_until_approved(bot, store, fake_llm, monkeypatch, tmp_path):
    path = tmp_path / "meeting.vtt"
    path.write_text(VTT, encoding="utf-8")
    monkeypatch.setattr(bot, "_download", lambda f: path)
    bot.app.handlers["message"]({"channel_type": "im", "user": "U1", "channel": "D1", "ts": "100.0", "text": "",
                                 "files": [{"id": "F1", "name": "meeting.vtt"}]}, bot.app.client)
    rec = store.get_record(1)
    assert fake_llm == [] and rec["source"] == "import" and "김과장: 안녕하세요" in rec["raw_text"]
    assert "00:00" not in rec["raw_text"] and set(bot.app.client.actions("1.0")) == {"summarize_call", "delete_record"}


# ---------- 대시보드 ----------

@pytest.fixture
def dash(store):
    import http.client
    import urllib.parse

    d = Dashboard(store, port=0)
    d.start()

    def get(path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
        c.request("GET", path, headers={"Host": host} if host else {})
        r = c.getresponse()
        return r.status, r.read().decode("utf-8")

    def post(path, token=True, follow=True, **form):
        if token:
            form["token"] = d.token if token is True else token
        c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
        c.request("POST", path, urllib.parse.urlencode(form), {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        body, loc = r.read().decode("utf-8"), r.getheader("Location")
        if follow and r.status == 303:
            return get(loc)
        return r.status, loc or body

    d.get, d.post = get, post
    yield d
    d.stop()


def test_dashboard_shows_cases_todos_and_attention(dash, store, fake_llm):
    a = pipeline.process(store, "원문 <script>alert(1)</script> 메모")
    pipeline.hold(store, "비밀 전사문", "call", "/calls/call_1402.m4a")
    status, html = dash.get("/")
    assert status == 200 and "진행 중인 건 2개" in html and "#1 견적서 재요청" in html
    assert "A사가 수정 견적서를 금요일까지 요청함" in html and '<span class="badge warn">1</span>' in html
    _, html = dash.get(f"/cases/{a['case_id']}")
    assert "단가는 기존 유지" in html and "견적서는 누구 검토를 받아야 하나요?" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html   # 원문은 그대로 실행되지 않게
    _, html = dash.get("/todos")
    assert "미완료 할 일 2건" in html and "A사 김과장: </span>납기 가능일 회신" in html
    _, html = dash.get("/attention")
    assert "정리 안 된 기록 1건" in html and "call_1402.m4a" in html and "비밀 전사문" not in html
    assert "아직 답하지 않은 질문 1개" in html and "확인 안 함(감시 꺼짐)" in html
    assert dash.get("/profile")[0] == 200 and dash.get("/import")[0] == 200
    assert dash.get("/cases/999")[0] == 404 and dash.get("/nope")[0] == 404


def test_dashboard_only_answers_local_requests_with_the_run_token(dash, store, fake_llm):
    pipeline.process(store, "메모")
    assert dash.get("/", host="evil.example.com")[0] == 403            # 다른 이름으로 들어온 요청(DNS 리바인딩)
    assert dash.get("/", host=f"localhost:{dash.port}")[0] == 200
    tid = store.open_todos()[0]["id"]
    assert dash.post(f"/todos/{tid}/done", token=False)[0] == 403       # 토큰 없는 쓰기(다른 사이트의 몰래 요청)
    assert dash.post(f"/todos/{tid}/done", token="wrong")[0] == 403
    assert len(store.open_todos()) == 2
    status, loc = dash.post(f"/todos/{tid}/done", follow=False, next="//evil.example.com/x")
    assert status == 303 and loc.startswith("/?msg=") and len(store.open_todos()) == 1   # 밖으로 돌려보내지 않는다


def test_dashboard_todo_done_and_undo(dash, store, fake_llm):
    a = pipeline.process(store, "메모")
    tid = next(t["id"] for t in store.open_todos() if t["owner"] == "나")
    _, html = dash.post(f"/todos/{tid}/done", next="/todos")
    assert "완료 처리했습니다." in html and "미완료 할 일 1건" in html
    _, html = dash.get(f"/cases/{a['case_id']}")
    assert '<div class="done">수정 견적서 작성</div>' in html and f"/todos/{tid}/reopen" in html
    dash.post(f"/todos/{tid}/reopen", next=f"/cases/{a['case_id']}")
    assert len(store.open_todos()) == 2


def test_dashboard_attention_actions_without_slack(dash, store, broken_llm):
    with pytest.raises(pipeline.ProcessError):
        pipeline.process(store, "메모")
    held = pipeline.hold(store, "전사문", "call")
    store.record_failure("call", "/calls/a.m4a", "전사 실패")
    fid = store.open_failures()[0]["id"]
    _, html = dash.post(f"/failures/{fid}/retry", next="/attention")
    assert "다시 실패했습니다" in html and store.get_failure(fid)["attempts"] == 2
    _, html = dash.post(f"/failures/{store.open_failures()[1]['id']}/retry", next="/attention")
    assert "슬랙 봇과 함께 실행 중일 때만" in html
    broken_llm["broken"] = False
    _, html = dash.post(f"/failures/{fid}/retry", next="/attention")
    assert "다시 처리했습니다." in html and [f["stage"] for f in store.open_failures()] == ["call"]
    _, html = dash.post(f"/records/{held['id']}/summarize", next="/attention")
    assert "정리했습니다." in html and store.get_record(held["id"])["data"]
    _, html = dash.post(f"/failures/{store.open_failures()[0]['id']}/dismiss", next="/attention")
    assert "넘겼습니다." in html and "막힌 건이 없습니다." in html


def test_dashboard_import_and_combined_summary(dash, store, fake_llm, monkeypatch):
    _, html = dash.post("/import", text="   ")
    assert "전사문이 비어 있습니다." in html
    _, html = dash.post("/import", text=SRT, name="A사 통화")
    assert "이 PC에 저장했습니다" in html and "아직 정리하지 않은 기록입니다" in html and fake_llm == []
    assert store.get_record(1)["raw_text"] == "파일명: A사 통화\n화자1: 금요일까지 부탁드립니다 꼭이요"
    monkeypatch.setattr(cfg, "send_calls_to_llm", True)
    _, html = dash.post("/import", text="화자1: 네")
    assert "가져와 정리했습니다." in html and len(fake_llm) == 1

    a, b = pipeline.process(store, "하나"), pipeline.process(store, "둘")
    store.move_record(b["id"], a["case_id"])                    # 통합 정리를 아직 만들지 않은 상태
    n = len(fake_llm)
    _, html = dash.get(f"/cases/{a['case_id']}")
    assert "통합 정리를 다시 만들지 않았습니다" in html and len(fake_llm) == n   # 화면을 여는 것만으로는 LLM을 부르지 않는다
    _, html = dash.post(f"/cases/{a['case_id']}/summary", next=f"/cases/{a['case_id']}")
    assert "통합 정리를 만들었습니다." in html and len(fake_llm) == n + 1


def test_slack_dashboard_command(bot):
    bot.say("대시보드")
    assert "꺼져 있습니다" in bot.app.client.messages["1.0"]["text"]
    bot.dashboard_url = "http://127.0.0.1:8765/"
    bot.say("대시보드", ts="101.0")
    assert "http://127.0.0.1:8765/" in bot.app.client.messages["2.0"]["text"]
