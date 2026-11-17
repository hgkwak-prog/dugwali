"""LLM 호출부. 방식은 `LLM_PROVIDER`로 고른다. 다른 모델을 붙일 때는 `_PROVIDERS`에 함수 하나를 더한다.

- `claude_code`(기본): 구독 계정으로 로그인된 Claude Code를 헤드리스(`claude -p`)로 호출한다.
  프롬프트는 표준 입력으로 넘겨 윈도우의 명령줄 따옴표·한글 인코딩 문제를 피한다.
- `api`: Claude API를 공식 SDK(`anthropic`)로 호출한다. `pip install -r requirements-api.txt` 필요.

마스킹(`mask`)은 방식과 무관하게 호출하는 쪽(`extract.py`)에서 항상 먼저 적용한다.
"""
import json
import logging
import re
import shutil
import subprocess

from ..config import cfg

log = logging.getLogger(__name__)
LLM_RETRIES = 1          # 일시적 실패일 때 한 번 더 시도한다
API_MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_persist_flag = True     # 설치된 Claude Code가 --no-session-persistence를 모르면 끈다

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
    retryable = True


class LLMConfigError(LLMError):
    """설치·설정 문제. 다시 시도해도 같은 결과다."""
    retryable = False


class LLMLimitError(LLMError):
    """하루 호출 상한에 닿았다."""
    retryable = False


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


def _claude_code(prompt: str) -> str:
    """구독 계정으로 로그인된 Claude Code를 헤드리스로 호출한다."""
    global _persist_flag
    exe = shutil.which(cfg.claude_bin)
    if not exe:
        raise LLMConfigError(
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
    if _persist_flag:
        cmd.append("--no-session-persistence")  # 호출 내용을 PC에 세션 기록으로 남기지 않는다
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
        err = LLMError(f"LLM 응답 시간 초과({cfg.llm_timeout}초)")
        err.retryable = False  # 한 번 더 기다리게 하지 않는다
        raise err from e
    if proc.returncode != 0:
        if _persist_flag and "no-session-persistence" in (proc.stderr or ""):
            log.warning("이 Claude Code는 세션 기록 끄기를 지원하지 않습니다. 업데이트를 권합니다.")
            _persist_flag = False
            return _claude_code(prompt)
        raise LLMError(f"claude 종료 코드 {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude 출력 파싱 실패: {proc.stdout[:300]!r}") from e
    if outer.get("is_error"):
        raise LLMError(f"claude 오류: {str(outer.get('result'))[:300]}")
    return str(outer.get("result", ""))


def _api(prompt: str) -> str:
    """Claude API를 공식 SDK로 호출한다. 429·5xx·연결 오류는 SDK가 알아서 다시 시도한다."""
    try:
        import anthropic
    except ImportError as e:
        raise LLMConfigError(
            "LLM_PROVIDER=api에는 anthropic 패키지가 필요합니다: pip install -r requirements-api.txt") from e

    opts = {"timeout": float(cfg.llm_timeout)}
    if cfg.llm_api_key:
        opts["api_key"] = cfg.llm_api_key  # 비우면 SDK가 ANTHROPIC_API_KEY 등 환경에서 찾는다
    if cfg.llm_api_base_url:
        opts["base_url"] = cfg.llm_api_base_url
    req = {
        "model": cfg.llm_api_model,
        "max_tokens": API_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    if cfg.llm_api_effort:
        req["output_config"] = {"effort": cfg.llm_api_effort}
    try:
        client = anthropic.Anthropic(**opts)
        if cfg.llm_api_base_url:  # 사내 게이트웨이는 베타 기능을 받지 않을 수 있다
            resp = client.messages.create(**req)
        else:  # 모델이 안전 분류로 거절하면 서버가 대체 모델로 같은 요청을 이어 처리한다
            resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **req)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise LLMConfigError(f"API 키를 확인하세요(LLM_API_KEY): {e.message}") from e
    except anthropic.NotFoundError as e:
        raise LLMConfigError(f"모델 이름을 확인하세요(LLM_API_MODEL={cfg.llm_api_model}): {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMConfigError(f"API 사용량 한도 초과: {e.message}") from e
    except anthropic.APIStatusError as e:
        cls = LLMError if e.status_code >= 500 else LLMConfigError
        raise cls(f"API 오류 {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMConfigError(f"API에 연결하지 못했습니다: {e}") from e
    except anthropic.AnthropicError as e:  # 키를 어디서도 찾지 못한 경우 등
        raise LLMConfigError(f"API 설정 오류: {e}") from e
    if resp.stop_reason == "refusal":
        raise LLMConfigError("모델이 이 내용의 처리를 거절했습니다.")
    if resp.stop_reason == "max_tokens":
        raise LLMConfigError("응답이 길이 상한에서 잘렸습니다.")
    return "".join(b.text for b in resp.content if b.type == "text")


_PROVIDERS = {"claude_code": _claude_code, "api": _api}


def call_json(prompt: str) -> dict:
    """프롬프트를 보내고 JSON 객체를 돌려받는다. 일시적 실패는 LLM_RETRIES번 더 시도한다."""
    provider = _PROVIDERS.get(cfg.llm_provider)
    if not provider:
        raise LLMConfigError(f"LLM_PROVIDER는 {' 또는 '.join(_PROVIDERS)} 중 하나여야 합니다: {cfg.llm_provider!r}")
    for attempt in range(LLM_RETRIES + 1):
        try:
            return parse_json_object(provider(prompt))
        except LLMError as e:
            if not e.retryable or attempt == LLM_RETRIES:
                raise
            log.warning("LLM 호출 실패, 다시 시도합니다: %s", e)
