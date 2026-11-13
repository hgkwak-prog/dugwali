"""입구(슬랙·통화·CLI)와 무관한 핵심 흐름: 추출 → 저장 → 로그 갱신."""
from .agent import extract as ex
from .sinks import markdown as md_sink
from .labels import SOURCE_LABEL
from .store import Store


def process(store: Store, raw_text: str, source: str = "slack", audio_path: str | None = None) -> dict:
    """새 기록을 만들고 구조화한다."""
    data = ex.extract(raw_text, SOURCE_LABEL.get(source, source))
    rid = store.add_record(source, raw_text, data, audio_path)
    rec = store.get_record(rid)
    md_sink.rewrite_day(store, rec["created_at"][:10])
    return rec


def hold(store: Store, raw_text: str, source: str, audio_path: str | None = None) -> dict:
    """LLM에 보내지 않고 원문만 보관한다(승인 전 통화 전사문)."""
    return store.get_record(store.add_record(source, raw_text, None, audio_path))


def summarize(store: Store, rid: int) -> dict:
    """보관만 해 둔 기록을 사용자가 승인했을 때 구조화한다."""
    rec = store.get_record(rid)
    data = ex.extract(rec["raw_text"], SOURCE_LABEL.get(rec["source"], rec["source"]))
    store.update_record(rid, data=data)
    md_sink.rewrite_day(store, rec["created_at"][:10])
    return store.get_record(rid)


def amend(store: Store, rid: int, extra: str) -> dict:
    """스레드 답글로 들어온 보충 정보를 합쳐 다시 구조화한다."""
    rec = store.get_record(rid)
    raw = f"{rec['raw_text']}\n\n[작성자 보충] {extra}"
    data = ex.extract(raw, SOURCE_LABEL.get(rec["source"], rec["source"]))
    store.update_record(rid, raw_text=raw, data=data)
    md_sink.rewrite_day(store, rec["created_at"][:10])
    return store.get_record(rid)
