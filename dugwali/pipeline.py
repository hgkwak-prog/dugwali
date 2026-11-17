"""입구(슬랙·통화·CLI)와 무관한 핵심 흐름: 원문 저장 → 추출 → 로그 갱신.

원문을 먼저 저장하고 그다음에 구조화한다. 구조화(LLM)가 실패해도 원문은 남고,
실패는 `failures`에 기록돼 나중에 다시 시도할 수 있다.

기록(메모·통화 1개)은 건(같은 일의 묶음)에 속한다. 새 기록은 새 건으로 시작하고,
관련 건은 제안만 한다(`data["related_case"]`). 합치는 것은 사용자가 정한다.
"""
import json
import logging
from datetime import date

from .agent import extract as ex
from .agent.llm import LLMError, LLMLimitError
from .config import cfg
from .labels import SOURCE_LABEL
from .sinks import markdown as md_sink
from .store import UNTITLED, Store

log = logging.getLogger(__name__)
SUGGEST_DAYS = 14   # 이 기간 안에 갱신된 건만 관련 건 후보로 보낸다
SUGGEST_MAX = 20


def _extract(store: Store, text: str, source: str, cases: list[tuple[int, str]] | None = None) -> dict:
    """LLM 호출은 모두 여기를 지난다. 하루 호출 상한(LLM_DAILY_LIMIT, 0이면 무제한)을 센다."""
    limit = cfg.llm_daily_limit
    if limit > 0:
        today = date.today().isoformat()
        used = json.loads(store.get_state("llm_calls") or "{}")
        n = used.get("n", 0) if used.get("day") == today else 0
        if n >= limit:
            raise LLMLimitError(f"오늘 LLM 호출 상한({limit}회)에 닿았습니다. 내일 다시 시도하거나 "
                                ".env의 LLM_DAILY_LIMIT을 올리세요.")
        store.set_state("llm_calls", json.dumps({"day": today, "n": n + 1}))
    return ex.extract(text, source, cases=cases)


def _label(source: str) -> str:
    return SOURCE_LABEL.get(source, source)


def _refresh(store: Store, days: set[str] = frozenset(), cases: set[int] = frozenset()) -> None:
    for day in days:
        md_sink.rewrite_day(store, day)
    for cid in cases:
        if cid is not None:
            md_sink.rewrite_case(store, cid)


def _candidates(store: Store, exclude: int | None = None) -> list[tuple[int, str]]:
    return [(c["id"], c["title"]) for c in store.open_cases(days=SUGGEST_DAYS)
            if c["id"] != exclude and c["title"] != UNTITLED][:SUGGEST_MAX]


def _reconcile(store: Store, cid: int | None) -> None:
    """건의 할 일을 현재 기록 구성에 맞춘다.

    기록이 여럿이면 통합 정리의 할 일로 바꿔 기록마다 겹쳐 나온 할 일을 하나로 줄인다.
    통합 정리(LLM)가 실패해도 병합·수정 자체는 유지하고, 할 일은 기록별 그대로 둔다.
    """
    if cid is None or not store.get_case(cid):
        return
    recs = [r for r in store.case_records(cid) if r["data"]]
    if len(recs) == 1:
        store.replace_case_todos(cid, recs[0]["data"]["todos"])
    elif len(recs) > 1:
        try:
            store.replace_case_todos(cid, case_summary(store, cid)["todos"])
        except LLMError:
            log.exception("건 #%s 통합 정리 실패, 할 일은 기록별로 둡니다", cid)


class ProcessError(LLMError):
    """원문은 저장했지만 구조화에 실패했다. record: 저장된 기록, failure: 남긴 실패 항목."""

    def __init__(self, record: dict, failure: dict, cause: Exception) -> None:
        super().__init__(str(cause))
        self.record, self.failure = record, failure


def _target(rid: int) -> str:
    return f"record:{rid}"


def _structure(store: Store, rid: int) -> None:
    """저장된 기록의 원문을 구조화해 채운다. 실패하면 실패 항목을 남기고 ProcessError를 던진다."""
    rec = store.get_record(rid)
    try:
        data = _extract(store, rec["raw_text"], _label(rec["source"]), _candidates(store, rec["case_id"]))
    except LLMError as e:
        log.warning("기록 %s 정리 실패: %s", rid, e)
        raise ProcessError(rec, store.record_failure("extract", _target(rid), str(e), rid), e) from e
    store.update_record(rid, data=data)
    store.resolve_failures(stage="extract", target=_target(rid))


def process(store: Store, raw_text: str, source: str = "slack", audio_path: str | None = None) -> dict:
    """새 기록을 만들고 구조화한다. 새 건으로 시작한다."""
    rid = store.add_record(source, raw_text, None, audio_path)
    _structure(store, rid)
    rec = store.get_record(rid)
    _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return rec


def hold(store: Store, raw_text: str, source: str, audio_path: str | None = None) -> dict:
    """LLM에 보내지 않고 원문만 보관한다(승인 전 통화 전사문)."""
    return store.get_record(store.add_record(source, raw_text, None, audio_path))


def summarize(store: Store, rid: int) -> dict:
    """원문만 있는 기록을 구조화한다(통화 요약 승인, 정리 실패 뒤 다시 시도)."""
    rec = store.get_record(rid)
    _structure(store, rid)
    _reconcile(store, rec["case_id"])
    _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return store.get_record(rid)


def amend(store: Store, rid: int, extra: str) -> dict:
    """스레드 답글로 들어온 보충 정보를 합쳐 다시 구조화한다."""
    rec = store.get_record(rid)
    raw = f"{rec['raw_text']}\n\n[작성자 보충] {extra}"
    data = _extract(store, raw, _label(rec["source"]))
    store.update_record(rid, raw_text=raw, data=data)
    _reconcile(store, rec["case_id"])
    _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return store.get_record(rid)


def merge(store: Store, rid: int, cid: int) -> dict:
    """기록을 다른 건으로 옮긴다(병합·사후 보강)."""
    rec, case = store.get_record(rid), store.get_case(cid)
    if not rec or not case:
        raise ValueError("기록 또는 건을 찾지 못했습니다.")
    old = rec["case_id"]
    store.move_record(rid, cid)
    if case["status"] != "open":
        store.reopen_case(cid)
    if rec["data"] and rec["data"].get("related_case") is not None:
        store.update_record(rid, data={**rec["data"], "related_case": None})  # 제안 버튼을 다시 띄우지 않는다
    _reconcile(store, cid)
    _reconcile(store, old)
    _refresh(store, {rec["created_at"][:10]}, {old, cid})
    return store.get_record(rid)


def delete(store: Store, rid: int) -> dict | None:
    rec = store.delete_record(rid)
    if rec:
        _reconcile(store, rec["case_id"])
        _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return rec


def delete_case(store: Store, cid: int) -> int:
    recs = store.delete_case(cid)
    _refresh(store, {r["created_at"][:10] for r in recs}, {cid})
    return len(recs)


def close(store: Store, cid: int) -> None:
    store.close_case(cid)
    _refresh(store, cases={cid})


def case_summary(store: Store, cid: int) -> dict | None:
    """건 전체를 하나로 정리한 결과. 기록이 하나면 그 기록 그대로, 여럿이면 LLM으로 종합한다.

    LLM으로 보낸 적 없는 기록(요약 승인 전 통화 전사문)은 종합에서도 제외한다.
    """
    case = store.get_case(cid)
    if not case:
        return None
    recs = [r for r in store.case_records(cid) if r["data"]]
    if not recs:
        return None
    if len(recs) == 1:
        return recs[0]["data"]
    if case["data"] and not case["dirty"]:
        return case["data"]
    text = "\n\n".join(
        f"[기록 {i}] {r['created_at'][:16].replace('T', ' ')} ({_label(r['source'])})\n{r['raw_text']}"
        for i, r in enumerate(recs, 1))
    done = store.done_tasks(cid)
    if done:
        text += "\n\n[이미 완료 처리한 할 일 - todos에 넣지 말 것]\n" + "\n".join(f"- {t}" for t in done)
    data = _extract(store, text, "한 건의 여러 기록(시간순)")
    store.set_case_summary(cid, data)
    _refresh(store, {r["created_at"][:10] for r in recs}, {cid})
    return data
