"""`.env`을 대시보드에서 고친다. 주석과 줄 순서는 그대로 두고 값만 바꾼다.

설정 화면(`web.py`)과 첫 설정(슬랙 토큰)이 함께 쓴다. 바로 반영할 수 있는 값은 `cfg`에도 넣고,
다시 켜야 반영되는 값(슬랙 토큰, 경로, 대시보드 주소)은 파일에만 쓰고 그렇다고 알려준다.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, cfg

EXAMPLE = ROOT / "config.example.env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str = "text"            # text | secret | choice | flag | number | time | url
    attr: str | None = None       # 바로 반영할 cfg 속성. None이면 다시 켜야 반영된다
    options: tuple = ()           # choice: ((값, 표시), ...)
    help: str = ""
    group: str = ""


SETTINGS = [
    Setting("LLM_PROVIDER", "정리에 쓰는 LLM", "choice", "llm_provider",
            (("claude_code", "Claude Code (구독 계정으로 로그인)"), ("api", "Claude API 키")),
            "API 키 방식은 처음 바꿀 때 패키지를 하나 더 설치하므로 다시 켜야 합니다.", "정리(LLM)"),
    Setting("LLM_DAILY_LIMIT", "하루 호출 상한", "number", "llm_daily_limit",
            help="닿으면 원문만 보관하고 조치 필요에 올립니다. 0이면 무제한.", group="정리(LLM)"),
    Setting("CLAUDE_MODEL", "Claude Code 모델", "text", "claude_model",
            help="비우면 Claude Code 기본 모델.", group="정리(LLM)"),
    Setting("LLM_API_KEY", "Claude API 키", "secret", "llm_api_key", group="정리(LLM)"),
    Setting("LLM_API_MODEL", "Claude API 모델", "text", "llm_api_model", group="정리(LLM)"),
    Setting("STT_PROVIDER", "음성 전사 방식", "choice", "stt_provider",
            (("local", "이 PC에서 전사 (음성이 밖으로 나가지 않음)"), ("api", "외부 전사 API (음성 파일을 보냄)")),
            "외부 API는 회사가 승인한 제공자만 연결하세요. 이 PC 전사로 처음 바꿀 때는 패키지 설치 때문에 다시 켜야 합니다.",
            "전사"),
    Setting("STT_LANGUAGE", "말하는 언어", "text", "stt_language", help="ko, en 등. 비우면 자동 감지.", group="전사"),
    Setting("STT_API_BASE_URL", "전사 API 주소", "url", "stt_api_base_url",
            help="OpenAI 호환 형식: {주소}/audio/transcriptions", group="전사"),
    Setting("STT_API_KEY", "전사 API 키", "secret", "stt_api_key", group="전사"),
    Setting("STT_API_MODEL", "전사 API 모델", "text", "stt_api_model", group="전사"),
    Setting("SEND_CALLS_TO_LLM", "통화·음성 전사문을 바로 LLM에 보내기", "flag", "send_calls_to_llm",
            help="끄면 건마다 '정리하기'를 눌러야 보냅니다. 회사가 승인했을 때만 켜세요.", group="보안"),
    Setting("MASK_AMOUNTS", "금액도 가리고 보내기", "flag", "mask_amounts",
            help="전화번호·이메일·주민번호·계좌류는 항상 가립니다.", group="보안"),
    Setting("CALL_DIR", "통화 녹음 폴더", "text", None,
            help="안드로이드 통화 녹음이 동기화되는 폴더. 비우면 통화 감시를 끕니다.", group="통화·마감"),
    Setting("DIGEST_TIME", "마감 정리 시각", "time", "digest_time", help="평일 이 시각에 슬랙으로 보냅니다.",
            group="통화·마감"),
    Setting("DASHBOARD_URL", "다른 기기에서 여는 주소", "url", None,
            help="Tailscale serve 주소(https://…ts.net). 비우면 이 PC에서만 열립니다.", group="대시보드"),
]
BY_KEY = {s.key: s for s in SETTINGS}
SLACK_KEYS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OWNER_SLACK_USER_ID")


def path() -> Path:
    return cfg.env_path


def read() -> dict[str, str]:
    """지금 `.env`에 적힌 값. 파일이 없으면 빈 dict."""
    out = {}
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = _unquote(m.group(2).strip())
    return out


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #")[0].strip()


def _quote(v: str) -> str:
    return f'"{v}"' if re.search(r"[\s#\"']", v) else v


def write(values: dict[str, str]) -> None:
    """키마다 값을 바꾼다. 있던 줄은 그 자리에서 바꾸고, 없던 키는 끝에 더한다. 파일이 없으면 예시에서 만든다."""
    p = path()
    if p.exists():
        text = p.read_text(encoding="utf-8")
    else:
        text = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.exists() else ""
    lines, done = text.splitlines(), set()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in values and m.group(1) not in done:
            lines[i] = f"{m.group(1)}={_quote(values[m.group(1)])}"
            done.add(m.group(1))
    lines += [f"{k}={_quote(v)}" for k, v in values.items() if k not in done]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    for k, v in values.items():
        os.environ[k] = v   # 다시 읽을 때(load_dotenv는 이미 있는 값을 덮지 않는다) 새 값이 쓰이게


def parse(s: Setting, raw: str):
    """화면에서 받은 값을 검사해 cfg에 넣을 값으로 바꾼다. 맞지 않으면 ValueError."""
    raw = (raw or "").strip()
    if s.kind == "choice":
        if raw not in {v for v, _ in s.options}:
            raise ValueError(f"{s.label}: 고를 수 없는 값입니다.")
        return raw
    if s.kind == "flag":
        return raw in ("1", "true", "on", "yes")
    if s.kind == "number":
        if not raw.isdigit():
            raise ValueError(f"{s.label}: 0 이상의 숫자를 적으세요.")
        return int(raw)
    if s.kind == "time":
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
            raise ValueError(f"{s.label}: 17:30처럼 적으세요.")
        return raw
    if s.kind == "url":
        raw = raw.rstrip("/")
        if raw and not re.match(r"^https?://[^\s/]+", raw):
            raise ValueError(f"{s.label}: http:// 또는 https://로 시작하는 주소를 적으세요.")
        if s.key == "DASHBOARD_URL" and raw and not raw.startswith("https://"):
            raise ValueError(f"{s.label}: 로그인 쿠키를 지키려면 https:// 주소여야 합니다.")
        return raw
    return raw


def as_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def current(s: Setting) -> str:
    """화면에 보여줄 지금 값. 다시 켜야 반영되는 값은 파일에 적힌 것을 보여준다."""
    if s.attr:
        return as_text(getattr(cfg, s.attr))
    return read().get(s.key, os.getenv(s.key, ""))
