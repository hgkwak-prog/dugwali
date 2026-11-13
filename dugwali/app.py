"""진입점: 슬랙 봇 + 통화 녹음 감시 + 일일 다이제스트.  실행: python -m dugwali.app"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from . import digest, pipeline
from .config import cfg
from .ingest.watcher import CallWatcher
from .store import Store

log = logging.getLogger("dugwali")


def handle_call(bot, store: Store, path: Path) -> None:
    """통화 녹음 1건: 전사 → 전사문 저장 → (설정에 따라) 요약 또는 보관 후 버튼 제시."""
    from .stt.transcribe import transcribe

    result = transcribe(str(path), speakers=2)
    text = f"파일명: {path.name}\n{result['text']}"
    out = cfg.transcript_dir / f"{path.stem}.txt"
    out.write_text(text, encoding="utf-8")
    info = (f"📞 통화 전사 완료: `{path.name}` · {result['duration'] / 60:.1f}분 "
            f"(처리 {result['elapsed']:.0f}초{', 화자 구분' if result['diarized'] else ''})")
    if not result["text"].strip():
        bot.post_dm(f"{info}\n받아쓴 내용이 없습니다.")
    elif cfg.send_calls_to_llm:
        bot.post_record(pipeline.process(store, text, "call", str(path)), prefix=info)
    else:
        bot.post_held_call(pipeline.hold(store, text, "call", str(path)),
                           f"{info}\n전사문은 로컬에만 저장했습니다: `{out}`")


def digest_loop(bot, store: Store) -> None:
    sent_on = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") >= cfg.digest_time and sent_on != now.date() and now.weekday() < 5:
            # 앱을 늦게 켠 날에도 한 번은 보낸다
            try:
                bot.post_dm(digest.build(store))
            except Exception:
                log.exception("다이제스트 전송 실패")
            sent_on = now.date()
        time.sleep(30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k, v in (("SLACK_BOT_TOKEN", cfg.slack_bot_token), ("SLACK_APP_TOKEN", cfg.slack_app_token),
                              ("OWNER_SLACK_USER_ID", cfg.owner_user_id)) if not v]
    if missing:
        raise SystemExit(f".env에 다음 값이 필요합니다: {', '.join(missing)}")

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from .ingest.slack import Bot

    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    bot = Bot(store)

    if cfg.call_dir:
        watcher = CallWatcher(cfg.call_dir, store, lambda p: handle_call(bot, store, p))
        threading.Thread(target=watcher.run_forever, daemon=True, name="calls").start()
    else:
        log.info("CALL_DIR 미설정: 통화 녹음 감시를 끕니다")
    threading.Thread(target=digest_loop, args=(bot, store), daemon=True, name="digest").start()

    log.info("두괄이 시작. 슬랙에서 봇에게 DM을 보내 보세요.")
    SocketModeHandler(bot.app, cfg.slack_app_token).start()


if __name__ == "__main__":
    main()
