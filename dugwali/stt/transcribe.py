"""전사. 방식은 `STT_PROVIDER`로 고른다.

- `local`(기본): faster-whisper(CPU, int8)로 이 PC에서 받아쓰고, HF_TOKEN이 있으면 pyannote로 화자를
  구분한다. 음성이 밖으로 나가지 않는다. 무거운 라이브러리는 실제로 전사할 때만 불러온다.
- `api`: 음성 파일을 외부 전사 API로 보낸다(`stt/api.py`). 화자 구분은 하지 않는다.
"""
import importlib.util
import logging
import time

from ..config import cfg

log = logging.getLogger(__name__)
SAMPLE_RATE = 16000
_whisper = None
_diarizer = None


def _load_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(cfg.whisper_model, device="cpu", compute_type=cfg.whisper_compute)
    return _whisper


def _load_diarizer():
    global _diarizer
    if _diarizer is None:
        from pyannote.audio import Pipeline

        name = "pyannote/speaker-diarization-3.1"
        try:
            _diarizer = Pipeline.from_pretrained(name, token=cfg.hf_token)
        except TypeError:  # 구버전 pyannote
            _diarizer = Pipeline.from_pretrained(name, use_auth_token=cfg.hf_token)
    return _diarizer


def _diarize(audio, speakers: int) -> list[tuple[float, float, str]]:
    import torch

    waveform = torch.from_numpy(audio).unsqueeze(0)
    result = _load_diarizer()({"waveform": waveform, "sample_rate": SAMPLE_RATE}, num_speakers=speakers)
    annotation = getattr(result, "speaker_diarization", result)
    return [(turn.start, turn.end, spk) for turn, _, spk in annotation.itertracks(yield_label=True)]


def assign_speakers(segments: list[dict], turns: list[tuple[float, float, str]]) -> list[dict]:
    """각 전사 구간에 가장 많이 겹치는 화자를 붙인다. 화자 이름은 등장 순서대로 화자1, 화자2."""
    names: dict[str, str] = {}
    out = []
    for seg in segments:
        overlap: dict[str, float] = {}
        for start, end, spk in turns:
            o = min(seg["end"], end) - max(seg["start"], start)
            if o > 0:
                overlap[spk] = overlap.get(spk, 0.0) + o
        spk = max(overlap, key=overlap.get) if overlap else None
        if spk is not None and spk not in names:
            names[spk] = f"화자{len(names) + 1}"
        out.append({**seg, "speaker": names.get(spk)})
    return out


def format_transcript(segments: list[dict]) -> str:
    """같은 화자의 연속 발화는 한 줄로 합친다."""
    lines: list[str] = []
    last = object()
    for seg in segments:
        spk = seg.get("speaker")
        text = seg["text"].strip()
        if not text:
            continue
        if spk == last and lines:
            lines[-1] += " " + text
        else:
            lines.append(f"{spk}: {text}" if spk else text)
        last = spk
    return "\n".join(lines)


def local_available() -> bool:
    """로컬 전사 패키지가 설치돼 있는지."""
    return importlib.util.find_spec("faster_whisper") is not None


def transcribe(path: str, speakers: int = 2, diarize: bool | None = None, provider: str | None = None) -> dict:
    """오디오 파일 → {"text", "duration", "elapsed", "diarized", "provider"}

    provider를 주면 설정(STT_PROVIDER) 대신 그 방식으로 전사한다. duration을 모르면 0이다.
    """
    provider = provider or cfg.stt_provider
    if provider == "api":
        from .api import transcribe_api

        return transcribe_api(path)
    if provider != "local":
        raise ValueError(f"STT_PROVIDER는 local 또는 api여야 합니다: {provider!r}")
    if not local_available():
        raise RuntimeError("로컬 전사 패키지가 없습니다: pip install -r requirements-stt.txt")
    return _transcribe_local(path, speakers, diarize)


def _transcribe_local(path: str, speakers: int, diarize: bool | None) -> dict:
    from faster_whisper import decode_audio

    started = time.time()
    audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    segs, _ = _load_whisper().transcribe(audio, language=cfg.stt_language or None, vad_filter=True, beam_size=5)
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segs]

    use_diar = bool(cfg.hf_token) if diarize is None else diarize
    diarized = False
    if use_diar and segments:
        try:
            segments = assign_speakers(segments, _diarize(audio, speakers))
            diarized = True
        except Exception:
            log.exception("화자 구분 실패, 화자 표기 없이 진행합니다")
    return {
        "text": format_transcript(segments),
        "duration": duration,
        "elapsed": time.time() - started,
        "diarized": diarized,
        "provider": "local",
    }
