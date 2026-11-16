"""일일 다이제스트: 건 단위로 묶은 오늘 기록, 복귀 지점, 미완료 할 일. LLM을 쓰지 않는다."""
import re
from datetime import date

from .store import Store

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def build(store: Store, today: date | None = None) -> str:
    today = today or date.today()
    cases = {c["id"]: c for c in store.open_cases()}
    recs = [r for r in store.records_on(today.isoformat()) if r["data"] and r["case_id"] in cases]
    todos: dict[int, list[dict]] = {}
    for t in store.open_todos():
        todos.setdefault(t["case_id"], []).append(t)

    out = [f"*{today.isoformat()} 마감 정리*"]
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

    older = [(cid, ts) for cid, ts in todos.items() if cid not in touched and cid in cases]
    if older:
        out.append(f"*이전부터 열려 있는 건 {len(older)}개*")
        for cid, ts in older:
            out.append(f"• *#{cid} {cases[cid]['title']}*")
            out += [f"    ◦ {todo_text(t, today)}" for t in ts]
    if not todos:
        out.append("미완료 할 일 없음.")
    return "\n".join(out)
