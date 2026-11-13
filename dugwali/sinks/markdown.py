"""날짜별 Markdown 업무 로그. DB에서 매번 다시 써서 수정·재추출에도 중복이 생기지 않는다."""
from ..agent import render
from ..config import cfg
from ..labels import SOURCE_LABEL


def rewrite_day(store, day: str) -> str:
    cfg.ensure_dirs()
    recs = [r for r in store.records_on(day) if r["data"]]
    body = [f"# {day} 업무 로그", ""]
    for r in recs:
        body.append(render.markdown(r["data"], r["created_at"][11:16], SOURCE_LABEL.get(r["source"], r["source"])))
    path = cfg.log_dir / f"{day}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path)
