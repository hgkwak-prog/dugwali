"""Markdown 업무 로그. DB에서 매번 다시 써서 수정·병합·삭제에도 어긋나지 않는다.

- `YYYY-MM-DD.md`: 날짜별 기록
- `건별/003-제목.md`: 건마다 통합 정리 + 속한 기록
"""
import re

from ..agent import render
from ..config import cfg
from ..labels import SOURCE_LABEL

CASE_DIR = "건별"


def case_label(case: dict | None) -> str | None:
    return f"#{case['id']} {case['title']}" if case else None


def rewrite_day(store, day: str) -> str:
    cfg.ensure_dirs()
    recs = [r for r in store.records_on(day) if r["data"]]
    path = cfg.log_dir / f"{day}.md"
    if not recs:
        path.unlink(missing_ok=True)
        return str(path)
    body = [f"# {day} 업무 로그", ""]
    for r in recs:
        body.append(render.markdown(
            r["data"], r["created_at"][11:16], SOURCE_LABEL.get(r["source"], r["source"]),
            case_label(store.get_case(r["case_id"]))))
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path)


def rewrite_case(store, cid: int) -> str | None:
    """건 파일을 다시 쓴다. 건이 없어졌으면 파일을 지운다."""
    folder = cfg.log_dir / CASE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{cid:03d}-*.md"):  # 제목이 바뀌면 파일명도 바뀌므로 먼저 지운다
        old.unlink()
    case = store.get_case(cid)
    if not case:
        return None
    recs = [r for r in store.case_records(cid) if r["data"]]
    if not recs:
        return None
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", case["title"]).strip("-")[:40] or "건"
    state = "진행 중" if case["status"] == "open" else "종결"
    body = [f"# #{cid} {case['title']}", "", f"상태: {state} / 기록 {len(recs)}건", ""]
    if len(recs) > 1 and case["data"] and not case["dirty"]:
        body.append(render.markdown(case["data"], "통합 정리", f"기록 {len(recs)}건 종합"))
    body += ["## 기록", ""]
    for r in recs:
        body.append(render.markdown(
            r["data"], r["created_at"][:16].replace("T", " "),
            SOURCE_LABEL.get(r["source"], r["source"]), level=3))
    path = folder / f"{cid:03d}-{slug}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path)
