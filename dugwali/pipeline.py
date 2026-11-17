"""입구(슬랙·통화·CLI)와 무관한 핵심 흐름: 원문 저장 → 추출 → 로그 갱신.

원문을 먼저 저장하고 그다음에 구조화한다. 구조화(LLM)가 실패해도 원문은 남고,
실패는 `failures`에 기록돼 나중에 다시 시도할 수 있다.

기록(메모·통화 1개)은 건(같은 일의 묶음)에 속한다. 새 기록은 새 건으로 시작하고,
관련 건은 제안만 한다(`data["related_case"]`). 합치는 것은 사용자가 정한다.
"""
import json
import logging
from datetime import date

from . import views
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
    """건의 할 일을 현재 기록 구성에 맞춘다(규칙은 `Store.sync_todos`).

    기록이 여럿이면 통합 정리의 할 일에 맞춰 기록마다 겹쳐 나온 할 일을 하나로 줄인다.
    통합 정리(LLM)가 실패해도 병합·수정 자체는 유지하고, 할 일은 그대로 둔다.
    """
    case = store.get_case(cid) if cid is not None else None
    if not case or case["deleted_at"]:
        return
    recs = [r for r in store.case_records(cid) if r["data"]]
    if len(recs) == 1:
        store.sync_todos(cid, recs[0]["data"]["todos"])
    elif len(recs) > 1:
        try:
            store.sync_todos(cid, case_summary(store, cid)["todos"])
        except LLMError:
            log.exception("건 #%s 통합 정리 실패, 할 일은 그대로 둡니다", cid)


def _keep_manual(new: dict, old: dict | None, manual: list[str]) -> dict:
    """사람이 고친 필드는 LLM이 다시 정리한 값으로 덮어쓰지 않는다."""
    return {**new, **{f: old[f] for f in manual if old and f in old}}


def _todo_hint(store: Store, cid: int) -> str:
    """다시 정리할 때 LLM이 같은 할 일을 다른 문구로 적어 중복이 생기지 않게 지금 목록을 알려 준다."""
    done = store.done_tasks(cid)
    live = [t["task"] for t in store.case_todos(cid) if not t["done"]]
    out = ""
    if live:
        out += "\n\n[지금 열려 있는 할 일 - 같은 일이면 이 문구를 그대로 쓸 것]\n" + "\n".join(f"- {t}" for t in live)
    if done:
        out += "\n\n[이미 완료 처리한 할 일 - todos에 넣지 말 것]\n" + "\n".join(f"- {t}" for t in done)
    return out


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
    store.update_record(rid, data=_keep_manual(data, rec["data"], rec["manual"]))
    store.resolve_failures(stage="extract", target=_target(rid))


def process(store: Store, raw_text: str, source: str = "slack", audio_path: str | None = None) -> dict:
    """새 기록을 만들고 구조화한다. 새 건으로 시작한다."""
    rid = store.add_record(source, raw_text, None, audio_path)
    _structure(store, rid)
    rec = store.get_record(rid)
    _reconcile(store, rec["case_id"])
    _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return rec


def hold(store: Store, raw_text: str, source: str, audio_path: str | None = None) -> dict:
    """LLM에 보내지 않고 원문만 보관한다(승인 전 통화 전사문)."""
    return store.get_record(store.add_record(source, raw_text, None, audio_path))


def import_transcript(store: Store, text: str, name: str | None = None, note: str = "") -> dict:
    """다른 도구에서 전사한 결과를 기록으로 받는다. 통화 녹음과 같은 게이트를 거친다.

    원문을 먼저 보관하고, SEND_CALLS_TO_LLM이 켜져 있을 때만 바로 정리한다(실패하면 ProcessError).
    """
    head = "\n".join(x for x in (note.strip(), f"파일명: {name}" if name else "") if x)
    rec = hold(store, f"{head}\n{text}".strip(), "import")
    return summarize(store, rec["id"]) if cfg.send_calls_to_llm else rec


def dismiss_failure(store: Store, fid: int) -> str:
    """실패 항목을 닫는다. 그 기록이 어떻게 됐는지 한 줄로 돌려준다."""
    f = store.get_failure(fid)
    if not f:
        return ""
    store.resolve_failures(fid=fid)
    rec = store.get_record(f["record_id"]) if f.get("record_id") else None
    if rec and not rec["data"]:
        if rec["raw_text"].strip():
            return "원문은 정리 안 된 기록에 남아 있습니다."
        delete(store, rec["id"])  # 받아쓰지 못한 음성 메모: 남길 내용이 없다
        return "받아쓴 내용이 없어 기록은 지웠습니다. 음성 파일은 그대로 있습니다."
    return ""


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
    data = _extract(store, raw + _todo_hint(store, rec["case_id"]), _label(rec["source"]))
    store.update_record(rid, raw_text=raw, data=_keep_manual(data, rec["data"], rec["manual"]))
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


def split(store: Store, rid: int) -> dict:
    """기록을 건에서 떼어 새 건으로 만든다(잘못 합친 것 되돌리기)."""
    rec = store.get_record(rid)
    if not rec:
        raise ValueError("기록을 찾지 못했습니다.")
    cid = store.new_case(rec["data"]["title"] if rec["data"] else UNTITLED)
    old = store.move_record(rid, cid)
    _reconcile(store, cid)
    _reconcile(store, old)
    _refresh(store, {rec["created_at"][:10]}, {old, cid})
    return store.get_record(rid)


def add_note(store: Store, cid: int, text: str, source: str = "web") -> dict:
    """이미 있는 건에 메모를 더한다. 새 기록으로 정리해 그 건에 붙인다."""
    case = store.get_case(cid)
    if not case or case["deleted_at"]:
        raise ValueError("건을 찾지 못했습니다.")
    rid = store.add_record(source, text, None, case_id=cid)
    if case["status"] != "open":
        store.reopen_case(cid)
    _structure(store, rid)
    _reconcile(store, cid)
    rec = store.get_record(rid)
    _refresh(store, {rec["created_at"][:10]}, {cid})
    return rec


def delete(store: Store, rid: int) -> dict | None:
    """기록을 휴지통으로 옮긴다. `restore`로 되살릴 수 있다."""
    rec = store.delete_record(rid)
    if rec:
        _reconcile(store, rec["case_id"])
        _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return rec


def delete_case(store: Store, cid: int) -> int:
    """건과 그 기록을 휴지통으로 옮긴다."""
    recs = store.delete_case(cid)
    _refresh(store, {r["created_at"][:10] for r in recs}, {cid})
    return len(recs)


def restore(store: Store, kind: str, target_id: int) -> str:
    """휴지통에서 되살린다. kind: case / record / todo. 돌아갈 화면 주소를 돌려준다."""
    if kind == "todo":
        todo = store.get_todo(target_id)
        if not todo:
            return "/trash"
        store.restore_todo(target_id)
        _refresh_todo(store, todo["case_id"])
        return f"/cases/{todo['case_id']}"
    recs = store.restore_case(target_id) if kind == "case" else [r for r in [store.restore_record(target_id)] if r]
    if not recs:
        return "/trash"
    cid = recs[0]["case_id"]
    _refresh(store, {r["created_at"][:10] for r in recs}, {cid})
    return f"/cases/{cid}"


def close(store: Store, cid: int) -> None:
    store.close_case(cid)
    _refresh(store, cases={cid})


def reopen(store: Store, cid: int) -> None:
    store.reopen_case(cid)
    _refresh(store, cases={cid})


# ---------- 사람이 직접 고치기 ----------

def _refresh_todo(store: Store, cid: int) -> None:
    """할 일이 바뀌면 그 건의 업무 로그를 다시 쓴다."""
    _refresh(store, {r["created_at"][:10] for r in store.case_records(cid)}, {cid})


def _apply(old: dict, changes: dict) -> tuple[dict, list[str]]:
    """고칠 수 있는 필드만 반영한다. (새 정리 결과, 실제로 바뀐 필드)를 돌려준다."""
    new, changed = dict(old), []
    for f in views.EDITABLE:
        if f not in changes:
            continue
        v = changes[f]
        if f in views.LIST_FIELDS:
            v = [x.strip() for x in (v.splitlines() if isinstance(v, str) else v) if x.strip()]
        else:
            v = (v or "").strip() or None
            if f in ("title", "conclusion") and not v:
                continue  # 제목과 결론은 비울 수 없다
        if v != old.get(f):
            new[f] = v
            changed.append(f)
    return new, changed


def edit_record(store: Store, rid: int, changes: dict) -> list[str]:
    """기록의 정리 결과를 사람이 고친다. 고친 필드는 이후 LLM이 다시 정리해도 유지된다. 바뀐 필드를 돌려준다."""
    rec = store.get_record(rid)
    if not rec or not rec["data"]:
        raise ValueError("고칠 수 있는 기록이 아닙니다.")
    new, changed = _apply(rec["data"], changes)
    if changed:
        for f in changed:
            store.add_correction("record", rid, f, rec["data"].get(f), new[f])
        store.update_record(rid, data=new, manual=sorted(set(rec["manual"]) | set(changed)))
        _refresh(store, {rec["created_at"][:10]}, {rec["case_id"]})
    return changed


def edit_case(store: Store, cid: int, changes: dict) -> list[str]:
    """건의 통합 정리를 사람이 고친다. 바뀐 필드를 돌려준다."""
    case = store.get_case(cid)
    if not case or not case["data"]:
        raise ValueError("통합 정리가 아직 없습니다.")
    new, changed = _apply(case["data"], changes)
    if changed:
        for f in changed:
            store.add_correction("case", cid, f, case["data"].get(f), new[f])
        store.set_case_summary(cid, new, manual=sorted(set(case["manual"]) | set(changed)))
        _refresh_todo(store, cid)
    return changed


def release(store: Store, kind: str, target_id: int) -> None:
    """사람이 고친 표시를 지운다. 다음에 다시 정리할 때 LLM 값으로 바뀔 수 있다."""
    if kind == "record":
        store.update_record(target_id, manual=[])
    else:
        case = store.get_case(target_id)
        if case and case["data"]:
            store.set_case_summary(target_id, case["data"], manual=[])


def add_todo(store: Store, cid: int, task: str, owner: str | None = None, due: str | None = None) -> int | None:
    tid = store.add_todo(cid, task.strip(), (owner or "").strip() or None, (due or "").strip() or None)
    if tid:
        store.add_correction("todo", tid, "added", None, task.strip())
        _refresh_todo(store, cid)
    return tid


def edit_todo(store: Store, tid: int, task: str, owner: str | None, due: str | None) -> bool:
    """할 일을 사람이 고친다. 이후 LLM이 다시 정리해도 바뀌지 않는다."""
    old = store.get_todo(tid)
    new = {"task": task.strip(), "owner": (owner or "").strip() or None, "due": (due or "").strip() or None}
    if not old or not new["task"]:
        return False
    changed = [f for f in new if new[f] != old[f]]
    for f in changed:
        store.add_correction("todo", tid, f, old[f], new[f])
    if changed:
        store.edit_todo(tid, **new)
        _refresh_todo(store, old["case_id"])
    return bool(changed)


def todo_action(store: Store, tid: int, action: str) -> dict | None:
    """할 일 상태 바꾸기: done / reopen / keep(완료 추정이 아님) / delete."""
    todo = store.get_todo(tid)
    if not todo:
        return None
    {"done": store.complete_todo, "reopen": store.reopen_todo, "keep": store.keep_todo,
     "delete": store.delete_todo}[action](tid)
    _refresh_todo(store, todo["case_id"])
    return todo


def case_summary(store: Store, cid: int) -> dict | None:
    """건 전체를 하나로 정리한 결과. 기록이 하나면 그 기록 그대로, 여럿이면 LLM으로 종합한다.

    LLM으로 보낸 적 없는 기록(요약 승인 전 통화 전사문)은 종합에서도 제외한다.
    """
    case = store.get_case(cid)
    if not case or case["deleted_at"]:
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
    data = _extract(store, text + _todo_hint(store, cid), "한 건의 여러 기록(시간순)")
    data = _keep_manual(data, case["data"], case["manual"])
    store.set_case_summary(cid, data)
    _refresh(store, {r["created_at"][:10] for r in recs}, {cid})
    return data
