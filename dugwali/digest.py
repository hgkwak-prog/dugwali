"""일일 다이제스트. LLM을 쓰지 않는다.

순서: 먼저 할 일(기한이 지났거나 오늘·내일인 내 할 일) → 조치 필요(처리 실패, 정리 안 된 기록)
→ 오늘 다룬 건 → 복귀 지점 → 이전부터 열려 있는 건.
"""
import re
from datetime import date

from .store import Store

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URGENT_DAYS = 1  # 기한이 이 일수 안으로 남았거나 지난 내 할 일을 맨 위로 올린다


def due_tag(due: str | None, today: date) -> str:
    if not due:
        return "기한 미정"
    if _ISO.match(due):
        days = (date.fromisoformat(due) - today).days
        if days < 0:
            return f"⚠️ {-days}일 지남 ({due})"
        if days == 0:
            return "오늘까지"
        if days == 1:
            return "내일까지"
        return f"{due} (D-{days})"
    return due


def todo_text(t: dict, today: date) -> str:
    who = "" if t.get("owner") in (None, "나") else f"{t['owner']}: "
    return f"{who}{t['task']} · {due_tag(t['due'], today)}"


def is_mine(t: dict) -> bool:
    return t.get("owner") in (None, "나")


def is_urgent(t: dict, today: date) -> bool:
    """내 할 일 중 기한이 지났거나 오늘·내일까지인 것."""
    return (is_mine(t) and bool(t.get("due")) and bool(_ISO.match(t["due"]))
            and (date.fromisoformat(t["due"]) - today).days <= URGENT_DAYS)


def attention(store: Store) -> tuple[list[dict], list[dict]]:
    """사람이 조치해야 하는 것: (처리 실패 목록, 실패가 아닌데 아직 정리 안 된 기록 목록)."""
    failures = store.open_failures()
    failed = {f["record_id"] for f in failures}
    return failures, [r for r in store.unprocessed_records() if r["id"] not in failed]


def build(store: Store, today: date | None = None) -> str:
    today = today or date.today()
    cases = {c["id"]: c for c in store.open_cases()}
    recs = [r for r in store.records_on(today.isoformat()) if r["data"] and r["case_id"] in cases]
    open_todos = [t for t in store.open_todos() if t["case_id"] in cases]
    urgent = [t for t in open_todos if is_urgent(t, today)]  # 기한순으로 온다
    todos: dict[int, list[dict]] = {}
    for t in sorted(open_todos, key=lambda t: not is_mine(t)):  # 건 안에서는 내 할 일 먼저
        if t not in urgent:
            todos.setdefault(t["case_id"], []).append(t)

    out = [f"*{today.isoformat()} 마감 정리*"]
    if urgent:
        out.append(f"*먼저 할 일 {len(urgent)}건*")
        out += [f"• {due_tag(t['due'], today)} · {t['task']} — #{t['case_id']} {cases[t['case_id']]['title']}"
                for t in urgent]

    failures, pending = attention(store)
    if failures or pending:
        parts = ([f"처리 실패 {len(failures)}건"] if failures else []) + \
                ([f"정리 안 된 기록 {len(pending)}건"] if pending else [])
        out.append(f"*조치 필요* {' · '.join(parts)} — `조치`를 보내면 목록이 나옵니다.")

    touched: list[int] = []
    for r in recs:
        if r["case_id"] not in touched:
            touched.append(r["case_id"])

    if touched:
        out.append(f"*오늘 다룬 건 {len(touched)}개 (기록 {len(recs)}건)*")
        for cid in touched:
            c = cases[cid]
            latest = [r for r in recs if r["case_id"] == cid][-1]["data"]
            if c["n_records"] > 1 and c["data"] and not c["dirty"]:
                latest = c["data"]  # 여러 기록을 합친 건은 통합 정리의 결론을 쓴다
            count = f" (기록 {c['n_records']}건)" if c["n_records"] > 1 else ""
            out.append(f"• *#{cid} {c['title']}*{count}: {latest['conclusion']}")
            out += [f"    ◦ {todo_text(t, today)}" for t in todos.get(cid, [])]
    else:
        out.append("오늘 남긴 기록이 없습니다. 지금 한 줄만 던져 주세요.")

    resume = [r["data"]["resume_point"] for r in recs if r["data"].get("resume_point")]
    if resume:
        out.append("*하다 만 일(복귀 지점)*")
        out += [f"• {x}" for x in resume]

    older = [(cid, ts) for cid, ts in todos.items() if cid not in touched]
    if older:
        out.append(f"*이전부터 열려 있는 건 {len(older)}개*")
        for cid, ts in older:
            out.append(f"• *#{cid} {cases[cid]['title']}*")
            out += [f"    ◦ {todo_text(t, today)}" for t in ts]
    if not open_todos:
        out.append("미완료 할 일 없음.")
    return "\n".join(out)
