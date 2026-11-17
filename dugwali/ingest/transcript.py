"""다른 도구에서 이미 전사한 결과(.txt / .vtt / .srt)를 읽어 본문만 남긴다.

자막 형식(VTT·SRT)이면 번호와 시간 줄을 빼고, 화자 표기가 있으면 "화자: 말" 형태로 바꾼다.
같은 화자의 연속 발화는 한 줄로 합친다. 어느 전사 도구의 결과든 받는다.
"""
import re
from pathlib import Path

TRANSCRIPT_EXT = {".txt", ".vtt", ".srt"}
_VOICE = re.compile(r"^<v(?:\.[^ >]*)?\s+([^>]+)>(.*)$")
_TAG = re.compile(r"<[^>]+>")
_SPEAKER = re.compile(r"^([^:：]{1,30})[:：]\s*(.*)$")


def clean(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if "-->" not in text:
        return text.strip()  # 일반 텍스트
    lines: list[tuple[str | None, str]] = []
    skip_block = False
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            skip_block = False
            continue
        if skip_block or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            skip_block = True  # 빈 줄이 나올 때까지 건너뛴다
            continue
        m = _VOICE.match(line)
        speaker, body = (m.group(1).strip(), m.group(2)) if m else (None, line)
        body = _TAG.sub("", body).strip()
        if speaker is None:
            m = _SPEAKER.match(body)
            if m and m.group(2):
                speaker, body = m.group(1).strip(), m.group(2).strip()
        if not body:
            continue
        if lines and speaker is not None and lines[-1][0] == speaker:
            lines[-1] = (speaker, f"{lines[-1][1]} {body}")
        else:
            lines.append((speaker, body))
    return "\n".join(f"{s}: {b}" if s else b for s, b in lines)


def read(path: Path) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8", "cp949", "utf-16"):
        try:
            return clean(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return clean(data.decode("utf-8", "replace"))
