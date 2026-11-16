"""LLM·슬랙·오디오 없이 도는 테스트. 실행: pytest"""
import os
import time
from datetime import date, datetime

import pytest

from dugwali import digest, pipeline
from dugwali.agent import extract, llm, render
from dugwali.config import cfg
from dugwali.ingest.watcher import CallWatcher
import sqlite3

from dugwali.ingest import slack as slack_ui
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
