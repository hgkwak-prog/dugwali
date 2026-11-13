"""LLM·슬랙·오디오 없이 도는 테스트. 실행: pytest"""
import os
import time
from datetime import date, datetime

import pytest

from dugwali import digest, pipeline
from dugwali.agent import extract, llm, render
from dugwali.config import cfg
from dugwali.ingest.watcher import CallWatcher
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
    assert "오늘 기록 1건" in text and "내가 할 일 1건" in text and "받아야 할 것 1건" in text
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
