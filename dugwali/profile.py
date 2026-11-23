"""개인 프로필: 정리 기준을 코드 밖 파일(`profile.md`)로 둔다.

- `## 양식` 절은 여기서 읽어 템플릿(`agent/render.py`)에 적용한다.
- 나머지 절(나, 정리 기준, 말투, 용어 등)은 그대로 프롬프트에 붙인다. LLM 호출마다 함께 전송된다.
- 파일이 없거나 절이 빠져 있으면 기본값을 쓴다. 파일을 고치면 다음 정리부터 바로 반영된다.

스키마(JSON 필드), 마스킹, 승인 게이트는 프로필로 바꿀 수 없다. 그것들은 코드가 강제한다.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import cfg

SECTIONS = ["결정", "할 일", "확인 필요", "배경", "복귀 지점"]  # 결론은 항상 맨 위에 온다
FORMAT_HEADING = "양식"
DEFAULT_SLACK_ORDER = ["결정", "할 일", "확인 필요", "복귀 지점"]
DEFAULT_MAIL_ORDER = ["결정", "할 일", "확인 필요", "배경"]
DEFAULT_GREETING = "{대상}님, 안녕하세요."
DEFAULT_CLOSING = "감사합니다."
DEFAULT_SECTIONS = {
    "말투": '- 모든 문장은 짧은 개조식 명사형 종결("~함", "~예정", "~필요")\n'
            "- 구두 보고 대본은 존댓말 구어체 3~4문장",
}
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "profiles"

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)
_SETTING = re.compile(r"^\s*[-*]\s*(.+?)\s*[:：]\s*(.+?)\s*$")


@dataclass
class Profile:
    path: Path
    exists: bool = False
    sections: dict[str, str] = field(default_factory=dict)   # 프롬프트로 가는 절(제목 → 본문)
    slack_order: list[str] = field(default_factory=lambda: list(DEFAULT_SLACK_ORDER))
    mail_order: list[str] = field(default_factory=lambda: list(DEFAULT_MAIL_ORDER))
    mail_greeting: str = DEFAULT_GREETING
    mail_closing: str = DEFAULT_CLOSING
    warnings: list[str] = field(default_factory=list)

    @property
    def prompt_text(self) -> str:
        return "\n\n".join(f"## {name}\n{body}" for name, body in self.sections.items())

    def greeting(self, to: str | None) -> str:
        return self.mail_greeting.replace("{대상}", to or "○○○")


def _order(value: str, key: str, warnings: list[str]) -> list[str] | None:
    names = [x.strip() for x in re.split(r"[,，→>]", value) if x.strip()]
    order = []
    for n in names:
        if n == "결론":
            continue  # 결론은 항상 맨 위
        if n not in SECTIONS:
            warnings.append(f"'{key}'의 '{n}'은(는) 모르는 항목입니다. 쓸 수 있는 항목: {', '.join(SECTIONS)}")
        elif n not in order:
            order.append(n)
    return order or None


def parse(text: str, path: Path, exists: bool = True) -> Profile:
    p = Profile(path=path, exists=exists, sections=dict(DEFAULT_SECTIONS))
    text = _COMMENT.sub("", text)
    parts = _HEADING.split(text)  # [머리말, 제목1, 본문1, 제목2, 본문2, ...]
    for name, body in zip(parts[1::2], parts[2::2]):
        body = body.strip()
        if not body:
            continue
        if name != FORMAT_HEADING:
            p.sections[name] = body
            continue
        for line in body.splitlines():
            m = _SETTING.match(line)
            if not m:
                if line.strip():
                    p.warnings.append(f"양식 절에서 읽지 못한 줄: {line.strip()}")
                continue
            key, value = re.sub(r"\s+", " ", m.group(1)), m.group(2)
            if key == "슬랙 순서":
                p.slack_order = _order(value, key, p.warnings) or p.slack_order
            elif key == "메일 순서":
                p.mail_order = _order(value, key, p.warnings) or p.mail_order
            elif key == "메일 인사말":
                p.mail_greeting = value
            elif key == "메일 맺음말":
                p.mail_closing = value
            else:
                p.warnings.append(f"양식 절의 '{key}'은(는) 모르는 설정입니다. "
                                  "쓸 수 있는 설정: 슬랙 순서, 메일 순서, 메일 인사말, 메일 맺음말")
    return p


_cache: tuple[Path, float, Profile] | None = None


def load() -> Profile:
    """현재 프로필. 파일이 바뀌었을 때만 다시 읽는다."""
    global _cache
    path = cfg.profile_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return parse("", path, exists=False)
    if _cache and _cache[0] == path and _cache[1] == mtime:
        return _cache[2]
    p = parse(path.read_text(encoding="utf-8"), path)
    _cache = (path, mtime, p)
    return p


def examples() -> list[str]:
    return sorted(f.stem for f in EXAMPLES_DIR.glob("*.md")) if EXAMPLES_DIR.is_dir() else []


def init(name: str = "default") -> Path:
    """예시 프로필을 내 프로필 자리에 복사한다. 이미 있으면 덮어쓰지 않는다."""
    src = EXAMPLES_DIR / f"{name}.md"
    if not src.is_file():
        raise ValueError(f"'{name}' 예시가 없습니다. 있는 예시: {', '.join(examples()) or '(없음)'}")
    dest = cfg.profile_path
    if dest.exists():
        raise FileExistsError(f"프로필이 이미 있습니다: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def describe(p: Profile | None = None) -> str:
    """두괄이가 프로필을 어떻게 읽었는지 사람이 확인할 수 있게 보여준다."""
    p = p or load()
    lines = [f"프로필 파일: {p.path}" + ("" if p.exists else " (없음: 기본값 사용)"),
             f"LLM에 보내는 절: {', '.join(p.sections) or '(없음)'} · {len(p.prompt_text)}자",
             f"슬랙 순서: 결론 → {' → '.join(p.slack_order)}",
             f"메일 순서: 결론 → {' → '.join(p.mail_order)}",
             f"메일 인사말: {p.mail_greeting}",
             f"메일 맺음말: {p.mail_closing}"]
    lines += [f"⚠️ {w}" for w in p.warnings]
    return "\n".join(lines)


# ---------- 대시보드에서 만들고 고치기 ----------
RULES_HEADING = "정리 기준"
TONES = {
    "short": '- 모든 문장은 짧은 개조식 명사형 종결("~함", "~예정", "~필요")\n- 구두 보고 대본은 존댓말 구어체 3~4문장',
    "polite": '- 문장은 짧은 존댓말 평서문("~했습니다", "~예정입니다")\n- 구두 보고 대본은 존댓말 구어체 3~4문장',
}


def _lines(text: str) -> list[str]:
    return [f"- {x.strip().lstrip('-*• ').strip()}" for x in (text or "").splitlines() if x.strip().lstrip("-*• ").strip()]


def set_section(text: str, name: str, body: str) -> str:
    """`## name` 절의 본문을 바꾼다. 없으면 `## 양식` 앞(없으면 끝)에 새로 만든다."""
    block = f"## {name}\n{body.strip()}\n"
    m = re.search(rf"^##\s+{re.escape(name)}\s*$.*?(?=^##\s|\Z)", text, re.M | re.S)
    if m:
        return text[: m.start()] + block + ("\n" if text[m.end():] else "") + text[m.end():]
    f = re.search(rf"^##\s+{FORMAT_HEADING}\s*$", text, re.M)
    if f:
        return text[: f.start()] + block + "\n" + text[f.start():]
    return text.rstrip() + "\n\n" + block


def section_body(text: str, name: str) -> str:
    m = re.search(rf"^##\s+{re.escape(name)}\s*$\n?(.*?)(?=^##\s|\Z)", _COMMENT.sub("", text), re.M | re.S)
    return m.group(1).strip() if m else ""


def from_interview(answers: dict, base: str = "default") -> str:
    """첫 실행 인터뷰 답으로 프로필 본문을 만든다. 고른 예시를 바탕으로 답한 칸만 바꾼다."""
    src = EXAMPLES_DIR / f"{base}.md"
    if base not in examples():
        raise ValueError(f"'{base}' 예시가 없습니다.")
    text = src.read_text(encoding="utf-8")
    role, boss = (answers.get("role") or "").strip(), (answers.get("boss") or "").strip()
    me = [f"- 역할: {role or '(내 역할)'}", f"- 주 보고 대상: {boss or '(보고 받는 사람)'}"]
    text = set_section(text, "나", "\n".join(me))
    if answers.get("tone") in TONES:
        text = set_section(text, "말투", TONES[answers["tone"]])
    extra = _lines(answers.get("rules", ""))
    if extra:
        rules = [x for x in section_body(text, RULES_HEADING).splitlines() if x.strip()]
        text = set_section(text, RULES_HEADING, "\n".join(rules + extra))
    terms = _lines(answers.get("terms", ""))
    if terms:
        text = set_section(text, "용어", "\n".join(terms))
    greeting = (answers.get("greeting") or "").strip()
    if greeting:
        fmt = [x for x in section_body(text, FORMAT_HEADING).splitlines() if x.strip() and "메일 인사말" not in x]
        text = set_section(text, FORMAT_HEADING, "\n".join(fmt + [f"- 메일 인사말: {greeting}"]))
    return text


def save(text: str) -> Path:
    """프로필을 통째로 저장한다. 덮어쓰기 전 내용은 profile.md.bak에 한 벌 남긴다."""
    dest = cfg.profile_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.with_name(dest.name + ".bak").write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
    dest.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return dest


def add_rule(rule: str) -> Path:
    """`## 정리 기준`에 규칙 한 줄을 더한다. 프로필이 없으면 기본 예시로 만든 뒤 더한다."""
    rule = rule.strip().lstrip("-*• ").strip()
    if not rule:
        raise ValueError("규칙이 비어 있습니다.")
    dest = cfg.profile_path
    text = dest.read_text(encoding="utf-8") if dest.exists() else (EXAMPLES_DIR / "default.md").read_text(encoding="utf-8")
    rules = [x for x in section_body(text, RULES_HEADING).splitlines() if x.strip()]
    return save(set_section(text, RULES_HEADING, "\n".join(rules + [f"- {rule}"])))
