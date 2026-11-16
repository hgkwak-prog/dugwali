"""공통 스키마 → 용도별 양식. LLM을 쓰지 않는 순수 템플릿이라 구독 사용량을 먹지 않는다."""


def _todo_line(t: dict) -> str:
    owner = t.get("owner") or "담당 미정"
    due = _due(t.get("due")) or "기한 미정"
    return f"{t['task']} ({owner}, {due}{'까지' if t.get('due') else ''})"


def _due(due: str | None) -> str | None:
    """'금요일까지' → '금요일' (템플릿이 '까지'를 붙이므로 중복 방지)"""
    return due.removesuffix("까지").strip() if due else None


def _bullets(items: list[str], bullet: str = "•") -> str:
    return "\n".join(f"{bullet} {x}" for x in items)


def slack(d: dict) -> str:
    """슬랙 공유용: 결론 한 줄 + 개조식."""
    head = f"*[{d['kind']}] {d['title']}*"
    if d.get("counterpart"):
        head += f" · {d['counterpart']}"
    parts = [head, f"*결론* {d['conclusion']}"]
    if d["decisions"]:
        parts.append("*결정*\n" + _bullets(d["decisions"]))
    if d["todos"]:
        parts.append("*할 일*\n" + _bullets([_todo_line(t) for t in d["todos"]]))
    if d["open_questions"]:
        parts.append("*확인 필요*\n" + _bullets(d["open_questions"]))
    if d.get("resume_point"):
        parts.append(f"*복귀 지점* {d['resume_point']}")
    return "\n".join(parts)


def mail(d: dict) -> str:
    """메일 양식: 제목에 결론, 본문 두괄식."""
    to = d.get("share_to") or "○○○"
    lines = [
        f"제목: [{d['kind']}] {d['title']}",
        "",
        f"{to}님, 안녕하세요.",
        "",
        f"{d['title']} 건 공유드립니다.",
        "",
        "1. 결론",
        f"  - {d['conclusion']}",
    ]
    n = 2
    for label, items in (
        ("결정 사항", d["decisions"]),
        ("할 일", [_todo_line(t) for t in d["todos"]]),
        ("확인 필요 사항", d["open_questions"]),
        ("배경", d["background"]),
    ):
        if items:
            lines += ["", f"{n}. {label}"] + [f"  - {x}" for x in items]
            n += 1
    lines += ["", "감사합니다."]
    return "\n".join(lines)


def verbal(d: dict) -> str:
    """구두 보고 대본(약 30초). LLM이 쓴 구어체 대본이 있으면 그것을, 없으면 템플릿을 쓴다."""
    if d.get("spoken"):
        return d["spoken"]
    lines = [f"{d['title']} 건 보고드립니다.", f"결론부터 말씀드리면, {d['conclusion']}"]
    for i, x in enumerate((d["decisions"] or d["background"])[:2]):
        lines.append(f"{'첫째' if i == 0 else '둘째'}, {x}")
    mine = [t for t in d["todos"] if t.get("owner") in (None, "나")]
    if mine:
        t = mine[0]
        due = f" {_due(t['due'])}까지" if t.get("due") else ""
        lines.append(f"저는{due} {t['task']} 진행하겠습니다.")
    if d["open_questions"]:
        lines.append(f"확인 부탁드릴 점은, {d['open_questions'][0]}")
    return "\n".join(lines)


def markdown(d: dict, when: str, source: str, case: str | None = None, level: int = 2) -> str:
    """업무 로그(Markdown) 기록 하나. case: "#3 제목" 형태의 건 표시."""
    lines = [f"{'#' * level} {when} [{d['kind']}] {d['title']}", ""]
    meta = ([f"건: {case}"] if case else []) + [f"경로: {source}"]
    if d.get("counterpart"):
        meta.append(f"상대: {d['counterpart']}")
    if d.get("share_to"):
        meta.append(f"공유 대상: {d['share_to']}")
    lines += [" / ".join(meta), "", f"**결론**: {d['conclusion']}"]
    for label, items in (
        ("결정", d["decisions"]),
        ("할 일", [_todo_line(t) for t in d["todos"]]),
        ("확인 필요", d["open_questions"]),
        ("배경", d["background"]),
    ):
        if items:
            lines += ["", f"**{label}**"] + [f"- {x}" for x in items]
    if d.get("resume_point"):
        lines += ["", f"**복귀 지점**: {d['resume_point']}"]
    return "\n".join(lines) + "\n"
