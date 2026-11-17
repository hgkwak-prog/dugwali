"""외부 전사 API 호출. OpenAI 호환 형식(`POST {base}/audio/transcriptions`)을 쓰는 제공자면 연결된다.

음성 파일이 외부 서버로 나간다. 어느 제공자로 보낼지는 설정(STT_API_BASE_URL)으로만 정한다.
"""
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..config import cfg


def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    # 파일 이름은 확장자만 남긴다(통화 녹음 파일명에는 상대 이름·번호가 들어 있다)
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="audio{file_path.suffix.lower()}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
                 + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe_api(path: str) -> dict:
    if not cfg.stt_api_key:
        raise RuntimeError("STT_PROVIDER=api에는 STT_API_KEY가 필요합니다.")
    fields = {"model": cfg.stt_api_model, "response_format": "json"}
    if cfg.stt_language:
        fields["language"] = cfg.stt_language
    body, ctype = _multipart(fields, Path(path))
    req = urllib.request.Request(
        f"{cfg.stt_api_base_url}/audio/transcriptions", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.stt_api_key}", "Content-Type": ctype})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.stt_api_timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"전사 API 오류 {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"전사 API에 연결하지 못했습니다: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError("전사 API 응답을 읽지 못했습니다.") from e
    return {
        "text": str(data.get("text") or "").strip(),
        "duration": float(data.get("duration") or 0),
        "elapsed": time.time() - started,
        "diarized": False,
        "provider": "api",
    }
