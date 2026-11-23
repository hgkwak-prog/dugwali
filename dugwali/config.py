"""환경 변수 기반 설정. `cfg` 하나를 전역으로 공유한다."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("DUGWALI_ENV") or ROOT / ".env")
load_dotenv(ENV_PATH)


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _placeholder(v: str) -> bool:
    """예시 파일에 있던 자리표시자(xoxb-..., U0XXXXXXX)는 아직 안 채운 것으로 본다."""
    return not v or "..." in v or "XXXX" in v


class Config:
    def __init__(self) -> None:
        self.env_path = ENV_PATH
        self.slack_bot_token = os.getenv("SLACK_BOT_TOKEN", "")
        self.slack_app_token = os.getenv("SLACK_APP_TOKEN", "")
        self.owner_user_id = os.getenv("OWNER_SLACK_USER_ID", "")

        self.data_dir = Path(os.getenv("DATA_DIR", "./data"))
        self.log_dir = Path(os.getenv("LOG_DIR", str(self.data_dir / "worklog")))
        self.transcript_dir = Path(
            os.getenv("TRANSCRIPT_DIR", str(self.data_dir / "transcripts"))
        )
        self._profile_path = os.getenv("PROFILE_PATH", "").strip()
        call_dir = os.getenv("CALL_DIR", "").strip()
        self.call_dir = Path(call_dir) if call_dir else None

        self.llm_provider = os.getenv("LLM_PROVIDER", "claude_code").strip().lower()
        self.llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        self.llm_api_model = os.getenv("LLM_API_MODEL", "claude-opus-5-5").strip()
        self.llm_api_base_url = os.getenv("LLM_API_BASE_URL", "").strip()
        self.llm_api_effort = os.getenv("LLM_API_EFFORT", "low").strip().lower()
        self.llm_daily_limit = int(os.getenv("LLM_DAILY_LIMIT", "200"))
        self.claude_bin = os.getenv("CLAUDE_BIN", "claude")
        self.claude_model = os.getenv("CLAUDE_MODEL", "").strip()
        self.llm_timeout = int(os.getenv("LLM_TIMEOUT", "180"))
        self.send_calls_to_llm = _flag("SEND_CALLS_TO_LLM")
        self.mask_amounts = _flag("MASK_AMOUNTS")

        self.stt_provider = os.getenv("STT_PROVIDER", "local").strip().lower()
        self.stt_language = os.getenv("STT_LANGUAGE", "ko").strip()
        self.stt_api_base_url = os.getenv("STT_API_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        self.stt_api_key = os.getenv("STT_API_KEY", "").strip()
        self.stt_api_model = os.getenv("STT_API_MODEL", "whisper-1").strip()
        self.stt_api_timeout = int(os.getenv("STT_API_TIMEOUT", "600"))
        self.whisper_model = os.getenv("WHISPER_MODEL", "large-v3-turbo")
        self.whisper_compute = os.getenv("WHISPER_COMPUTE", "int8")
        self.hf_token = os.getenv("HF_TOKEN", "").strip()

        self.digest_time = os.getenv("DIGEST_TIME", "17:30")
        self.dashboard_port = int(os.getenv("DASHBOARD_PORT", "8765"))  # 0이면 대시보드를 띄우지 않는다
        # 다른 기기에서 여는 주소(예: Tailscale serve 주소). 비우면 이 PC 안에서만 열린다
        self.dashboard_url = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")

    def slack_missing(self) -> list[str]:
        """슬랙 연결에 필요한데 비어 있는 설정 이름."""
        return [k for k, v in (("SLACK_BOT_TOKEN", self.slack_bot_token), ("SLACK_APP_TOKEN", self.slack_app_token),
                               ("OWNER_SLACK_USER_ID", self.owner_user_id)) if _placeholder(v)]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dugwali.sqlite3"

    @property
    def profile_path(self) -> Path:
        """정리 기준을 적는 개인 프로필. 기본은 데이터 폴더의 profile.md."""
        return Path(self._profile_path) if self._profile_path else self.data_dir / "profile.md"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.transcript_dir):
            d.mkdir(parents=True, exist_ok=True)


cfg = Config()
