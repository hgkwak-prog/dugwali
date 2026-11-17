"""로컬 전사 + 화자 분리 단독 스크립트 (두괄이와 무관하게 동작).

faster-whisper로 단어 단위 타임스탬프까지 받아쓰고, pyannote로 찾은 화자 구간을
단어마다 붙인 뒤 같은 화자의 연속 발화를 한 줄로 묶는다. 음성은 밖으로 나가지 않는다.

필요한 것
  pip install faster-whisper "av<19" pyannote.audio torch
  Hugging Face 토큰 (pyannote/speaker-diarization-3.1, pyannote/segmentation-3.0 약관 동의)
  토큰은 --hf-token 또는 환경변수 HF_TOKEN 으로 넘긴다.

사용 예
  python transcribe_speakers.py 녹음.m4a                 # 화자 수 자동
  python transcribe_speakers.py 회의.m4a --speakers 3     # 화자 수를 알면 지정 (정확도↑)
  python transcribe_speakers.py 녹음.m4a --no-diarize     # 전사만
결과는 오디오 옆에 <파일명>.txt 로 저장되고 화면에도 출력된다.
"""
import argparse
import os
import sys
import time
from pathlib import Path

SAMPLE_RATE = 16000


def transcribe_words(audio, model_name: str, compute: str, language: str | None) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type=compute)
    segments, info = model.transcribe(
        audio, language=language, vad_filter=True, beam_size=5, word_timestamps=True
    )
    words = []
    for seg in segments:
        print(f"  전사 중... {seg.end:6.0f}초", end="\r", file=sys.stderr, flush=True)
        if seg.words:
            words += [{"start": w.start, "end": w.end, "text": w.word} for w in seg.words]
        else:  # 단어 타임스탬프가 없으면 구간 통째로
            words.append({"start": seg.start, "end": seg.end, "text": seg.text})
    print(file=sys.stderr)
    return words


def diarize(audio, token: str, model_name: str, speakers: int | None,
            min_speakers: int | None, max_speakers: int | None) -> list[tuple[float, float, str]]:
    import torch
    from pyannote.audio import Pipeline

    try:
        pipeline = Pipeline.from_pretrained(model_name, token=token)
    except TypeError:  # pyannote 3.x
        pipeline = Pipeline.from_pretrained(model_name, use_auth_token=token)
    if pipeline is None:
        raise RuntimeError(f"{model_name} 을(를) 불러오지 못했습니다. 토큰과 약관 동의를 확인하세요.")

    kwargs = {}
    if speakers:
        kwargs["num_speakers"] = speakers
    else:
        if min_speakers:
            kwargs["min_speakers"] = min_speakers
        if max_speakers:
            kwargs["max_speakers"] = max_speakers

    # 파일 대신 파형을 넘겨 pyannote 쪽 오디오 디코더(ffmpeg/torchcodec) 의존을 피한다.
    waveform = torch.from_numpy(audio).unsqueeze(0)
    result = pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, **kwargs)
    # pyannote 4.x는 겹침 없는 버전을 따로 준다. 단어 정렬에는 그쪽이 낫다.
    annotation = (getattr(result, "exclusive_speaker_diarization", None)
                  or getattr(result, "speaker_diarization", None)
                  or result)
    return [(t.start, t.end, spk) for t, _, spk in annotation.itertracks(yield_label=True)]


def assign_speakers(words: list[dict], turns: list[tuple[float, float, str]]) -> list[dict]:
    """단어마다 가장 많이 겹치는 화자, 겹치는 게 없으면 가장 가까운 화자를 붙인다.
    화자 이름은 등장 순서대로 화자1, 화자2, ..."""
    names: dict[str, str] = {}
    out = []
    for w in words:
        best, best_overlap = None, 0.0
        for start, end, spk in turns:
            o = min(w["end"], end) - max(w["start"], start)
            if o > best_overlap:
                best, best_overlap = spk, o
        if best is None and turns:
            mid = (w["start"] + w["end"]) / 2
            best = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))[2]
        if best is not None and best not in names:
            names[best] = f"화자{len(names) + 1}"
        out.append({**w, "speaker": names.get(best)})
    return out


def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_lines(words: list[dict]) -> str:
    """같은 화자의 연속 단어를 한 줄로. 화자가 없으면 2초 이상 쉬면 줄을 바꾼다."""
    lines, cur, cur_spk, cur_start, last_end = [], [], object(), 0.0, 0.0
    for w in words:
        spk = w.get("speaker")
        new_line = spk != cur_spk or (spk is None and w["start"] - last_end > 2.0)
        if new_line and cur:
            lines.append((cur_start, cur_spk, "".join(cur).strip()))
            cur = []
        if not cur:
            cur_start, cur_spk = w["start"], spk
        cur.append(w["text"])
        last_end = w["end"]
    if cur:
        lines.append((cur_start, cur_spk, "".join(cur).strip()))
    return "\n".join(
        f"[{fmt_time(t)}] {spk}: {text}" if spk else f"[{fmt_time(t)}] {text}"
        for t, spk, text in lines if text
    )


def main() -> int:
    p = argparse.ArgumentParser(description="로컬 전사 + 화자 분리")
    p.add_argument("audio", help="오디오 파일 (m4a, mp3, wav 등)")
    p.add_argument("-o", "--out", help="결과 파일 (기본: 오디오 옆 <이름>.txt)")
    p.add_argument("--speakers", type=int, help="화자 수를 알면 지정")
    p.add_argument("--min-speakers", type=int)
    p.add_argument("--max-speakers", type=int)
    p.add_argument("--no-diarize", action="store_true", help="화자 분리 없이 전사만")
    p.add_argument("--model", default=os.getenv("WHISPER_MODEL", "large-v3-turbo"))
    p.add_argument("--compute", default=os.getenv("WHISPER_COMPUTE", "int8"))
    p.add_argument("--language", default="ko", help="언어 코드. auto면 자동 감지")
    p.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    p.add_argument("--diar-model", default="pyannote/speaker-diarization-3.1")
    args = p.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 한글 깨짐 방지
    if not args.no_diarize and not args.hf_token:
        p.error("화자 분리에는 Hugging Face 토큰이 필요합니다 (--hf-token 또는 HF_TOKEN). "
                "전사만 하려면 --no-diarize")

    from faster_whisper import decode_audio

    started = time.time()
    audio = decode_audio(args.audio, sampling_rate=SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    print(f"길이 {fmt_time(duration)} — 전사 시작 ({args.model})", file=sys.stderr)

    language = None if args.language == "auto" else args.language
    words = transcribe_words(audio, args.model, args.compute, language)
    t_stt = time.time() - started

    if not args.no_diarize and words:
        print("화자 분리 중...", file=sys.stderr)
        turns = diarize(audio, args.hf_token, args.diar_model,
                        args.speakers, args.min_speakers, args.max_speakers)
        words = assign_speakers(words, turns)

    text = format_lines(words)
    out = Path(args.out) if args.out else Path(args.audio).with_suffix(".txt")
    out.write_text(text + "\n", encoding="utf-8")

    elapsed = time.time() - started
    print(text)
    print(f"\n--- 길이 {duration:.0f}초, 전사 {t_stt:.0f}초, 전체 {elapsed:.0f}초 "
          f"(실시간 대비 {elapsed / max(duration, 1):.2f}배) → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
