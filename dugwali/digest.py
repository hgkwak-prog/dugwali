"""일일 다이제스트: 오늘 기록, 복귀 지점, 미완료 할 일. LLM을 쓰지 않는다."""
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


def split_todos(todos: list[dict]) -> tuple[list[dict], list[dict]]:
    """(내가 할 일, 남에게 받을 일)"""
    mine = [t for t in todos if t.get("owner") in (None, "나")]
    others = [t for t in todos if t.get("owner") not in (None, "나")]
    return mine, others


def build(store: Store, today: date | None = None) -> str:
    today = today or date.today()
    recs = [r for r in store.records_on(today.isoformat()) if r["data"]]
    mine, others = split_todos(store.open_todos())
    out = [f"*{today.isoformat()} 마감 정리*"]

    if recs:
        out.append(f"*오늘 기록 {len(recs)}건*")
        out += [f"• {r['created_at'][11:16]} [{r['data']['kind']}] {r['data']['title']}: {r['data']['conclusion']}"
                for r in recs]
    else:
        out.append("오늘 남긴 기록이 없습니다. 지금 한 줄만 던져 주세요.")

    resume = [r["data"]["resume_point"] for r in recs if r["data"].get("resume_point")]
    if resume:
        out.append("*하다 만 일(복귀 지점)*")
        out += [f"• {x}" for x in resume]

    if mine:
        out.append(f"*내가 할 일 {len(mine)}건*")
        out += [f"• {t['task']} · {due_tag(t['due'], today)}" for t in mine]
    if others:
        out.append(f"*받아야 할 것 {len(others)}건*")
        out += [f"• {t['owner']}: {t['task']} · {due_tag(t['due'], today)}" for t in others]
    if not mine and not others:
        out.append("미완료 할 일 없음.")
    return "\n".join(out)
