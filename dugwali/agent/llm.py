"""LLM 호출부. 다른 모델(API, 사내 LLM)로 바꿀 때는 이 파일의 `call_json`만 교체한다.

기본 구현은 구독 계정으로 로그인된 Claude Code를 헤드리스(`claude -p`)로 호출한다.
프롬프트는 표준 입력으로 넘겨 윈도우의 명령줄 따옴표·한글 인코딩 문제를 피한다.
"""
import json
import re
import shutil
import subprocess

from ..config import cfg

SYSTEM_PROMPT = (
    "You are a JSON extraction engine. Do not use tools. "
    "Reply with exactly one JSON object and nothing else."
)

_MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"\b\d{6}\s?-\s?[1-4]\d{6}\b"), "[주민번호]"),
    (re.compile(r"(?<!\d)(?:\+82[\s-]?)?0?1[016789][\s.-]?\d{3,4}[\s.-]?\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"(?<!\d)0\d{1,2}[\s.-]\d{3,4}[\s.-]\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"(?<!\d)\d{2,6}-\d{2,6}-\d{2,8}(?:-\d{1,6})?(?!\d)"), "[계좌/번호]"),
]
_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?\s?(?:억|천만|백만|만)?\s?원")


class LLMError(RuntimeError):
    pass


def mask(text: str) -> str:
    """LLM으로 보내기 전에 연락처·식별번호를 가린다."""
    for pattern, label in _MASKS:
        text = pattern.sub(label, text)
    if cfg.mask_amounts:
        text = _AMOUNT.sub("[금액]", text)
    return text


def parse_json_object(text: str) -> dict:
    """응답 문자열에서 JSON 객체 하나를 꺼낸다(코드펜스·앞뒤 설명 허용)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"JSON을 찾지 못했습니다: {text[:200]!r}")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("JSON 객체가 아닙니다.")
    return obj


def call_json(prompt: str) -> dict:
    """프롬프트를 보내고 JSON 객체를 돌려받는다."""
    exe = shutil.which(cfg.claude_bin)
    if not exe:
        raise LLMError(
            f"'{cfg.claude_bin}' 실행 파일을 찾지 못했습니다. Claude Code를 설치하고 "
            "구독 계정으로 로그인했는지 확인하세요."
        )
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]
    cfg.ensure_dirs()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg.llm_timeout,
            cwd=str(cfg.data_dir),  # 프로젝트 설정·CLAUDE.md가 섞이지 않는 빈 폴더에서 실행
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"LLM 응답 시간 초과({cfg.llm_timeout}초)") from e
    if proc.returncode != 0:
        raise LLMError(f"claude 종료 코드 {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude 출력 파싱 실패: {proc.stdout[:300]!r}") from e
    if outer.get("is_error"):
        raise LLMError(f"claude 오류: {str(outer.get('result'))[:300]}")
    return parse_json_object(str(outer.get("result", "")))
