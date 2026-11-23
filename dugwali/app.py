"""진입점: 슬랙 봇 + 통화 녹음 감시 + 일일 다이제스트.  실행: python -m dugwali.app"""
import json
import logging
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import digest, pipeline
from .config import cfg
from .ingest.watcher import CallWatcher
from .store import Store
from .web import Dashboard

log = logging.getLogger("dugwali")


DIGEST_RETRIES = 3       # 마감 정리 전송을 하루에 이만큼까지 시도한다
DIGEST_RETRY_MIN = 10    # 실패하면 이만큼 기다렸다가 다시 시도한다
_call_lock = threading.Lock()  # 전사는 무겁다: 감시·다시 시도·놓친 녹음 처리가 겹치지 않게 한다


def _process_call(bot, store: Store, path: Path, provider: str | None = None) -> None:
    """통화 녹음 1건: 전사 → 전사문 저장 → (설정에 따라) 요약 또는 보관 후 버튼 제시."""
    rec = store.find_by_audio(str(path))
    if rec:  # 기록까지는 만들어졌던 파일(슬랙 전송만 실패): 다시 전사하지 않고 올리기만 한다
        if rec["data"]:
            bot.post_record(rec, prefix=f"📞 통화 다시 올림: `{path.name}`")
        elif not store.record_has_failure(rec["id"]):
            bot.post_held_call(rec, f"📞 통화 전사 완료: `{path.name}`\n전사문은 로컬에만 저장했습니다.")
        return

    from .stt.transcribe import transcribe

    result = transcribe(str(path), speakers=2, provider=provider)
    text = f"파일명: {path.name}\n{result['text']}"
    out = cfg.transcript_dir / f"{path.stem}.txt"
    out.write_text(text, encoding="utf-8")
    length = f" · {result['duration'] / 60:.1f}분" if result["duration"] else ""  # API는 길이를 안 줄 수 있다
    how = "외부 API" if result.get("provider") == "api" else "로컬"
    info = (f"📞 통화 전사 완료: `{path.name}`{length} "
            f"({how}, 처리 {result['elapsed']:.0f}초{', 화자 구분' if result['diarized'] else ''})")
    if not result["text"].strip():
        bot.post_dm(f"{info}\n받아쓴 내용이 없습니다.")
    elif cfg.send_calls_to_llm:
        bot.post_record(pipeline.process(store, text, "call", str(path)), prefix=info)
    else:
        bot.post_held_call(pipeline.hold(store, text, "call", str(path)),
                           f"{info}\n전사문은 로컬에만 저장했습니다: `{out}`")


def handle_call(bot, store: Store, path: Path, notify: bool = True, provider: str | None = None) -> dict | None:
    """통화 녹음 1건을 처리한다. 실패하면 실패 항목을 남기고 돌려준다(성공하면 None).

    어디서 실패하든 조용히 사라지지 않게 DB에 남기고, notify면 슬랙으로도 알린다.
    """
    with _call_lock:
        try:
            _process_call(bot, store, path, provider)
        except pipeline.ProcessError as e:  # 전사와 원문 저장은 끝났고 정리만 실패
            store.resolve_failures(stage="call", target=str(path))
            failure = e.failure
        except Exception as e:
            log.exception("통화 녹음 처리 실패: %s", path)
            failure = store.record_failure("call", str(path), str(e) or type(e).__name__)
        else:
            store.resolve_failures(stage="call", target=str(path))
            return None
    if notify:
        try:
            bot.post_failure(failure)
        except Exception:
            log.exception("실패 알림 전송 실패(조치 목록에는 남아 있습니다)")
    return failure


def digest_tick(bot, store: Store, now: datetime) -> None:
    """마감 정리를 평일 하루 한 번 보낸다. 보낸 날짜를 DB에 남겨 재시작해도 다시 보내지 않는다.

    전송에 실패하면 DIGEST_RETRY_MIN분 뒤 다시 시도하고, DIGEST_RETRIES번 실패하면 실패 항목으로 남긴다.
    앱을 늦게 켠 날에도 한 번은 보낸다.
    """
    today = now.date().isoformat()
    if (now.weekday() >= 5 or now.strftime("%H:%M") < cfg.digest_time
            or store.get_state("digest_sent_on") == today):
        return
    tries = json.loads(store.get_state("digest_try") or "{}")
    n = tries.get("n", 0) if tries.get("day") == today else 0
    if n >= DIGEST_RETRIES:
        return
    if n and now < datetime.fromisoformat(tries["at"]) + timedelta(minutes=DIGEST_RETRY_MIN):
        return
    try:
        bot.send_digest(now.date())
    except Exception as e:
        log.exception("다이제스트 전송 실패 (%d/%d)", n + 1, DIGEST_RETRIES)
        store.set_state("digest_try", json.dumps({"day": today, "n": n + 1, "at": now.isoformat()}))
        if n + 1 >= DIGEST_RETRIES:
            store.record_failure("digest", today, str(e) or type(e).__name__)
        return
    store.set_state("digest_sent_on", today)
    store.resolve_failures(stage="digest", target=today)


def purge_tick(store: Store, now: datetime) -> int:
    """휴지통에 30일 넘게 있던 것을 하루 한 번 실제로 지운다. 지운 기록 수를 돌려준다."""
    today = now.date().isoformat()
    if store.get_state("trash_purged_on") == today:
        return 0
    n = store.purge_trash()
    store.delete_sessions(expired_only=True)
    store.set_state("trash_purged_on", today)
    if n:
        log.info("휴지통에서 오래된 기록 %d건을 완전히 지웠습니다", n)
    return n


def digest_loop(bot, store: Store) -> None:
    while True:
        try:
            digest_tick(bot, store, datetime.now())
            purge_tick(store, datetime.now())
        except Exception:
            log.exception("다이제스트 점검 실패")
        time.sleep(30)


AUTH_ERRORS = ("invalid_auth", "not_authed", "token_revoked", "account_inactive", "not_allowed_token_type")


def connect_slack(store: Store, dash: Dashboard | None, url: str | None, opener=None):
    """슬랙 봇을 띄운다. 토큰이 없거나 틀리면 대시보드의 첫 설정 화면을 열고, 거기서 확인을 통과할 때까지 기다린다.

    토큰 문제가 아닌 실패(네트워크 등)는 그대로 올려 보내 run.bat이 잠시 뒤 다시 켜게 한다.
    """
    from .ingest.slack import Bot

    opened = False
    while True:
        missing = cfg.slack_missing()
        if missing:
            problem = f".env에 슬랙 설정이 비어 있습니다({', '.join(missing)})."
        else:
            try:
                bot = Bot(store)
            except Exception as err:
                if not any(code in str(err) for code in AUTH_ERRORS):
                    raise
                problem = f"슬랙 토큰이 맞지 않습니다({err})."
            else:
                if dash:
                    dash.setup_mode = False
                return bot
        if not url:
            log.error("%s 대시보드가 꺼져 있어(DASHBOARD_PORT=0) 첫 설정 화면을 열 수 없습니다. .env를 직접 고치세요.", problem)
            sys.exit(2)  # 설정 오류: run.bat이 재시작하지 않고 멈춘다
        log.warning("%s 브라우저에서 %ssetup 을 열어 슬랙을 연결하세요.", problem, url)
        dash.setup_mode, dash.slack_result = True, [f"❌ {problem}"] if not missing else []
        dash.slack_ready.clear()
        if not opened:
            opened = True
            try:
                (opener or webbrowser.open)(url + "setup")
            except Exception:
                log.exception("브라우저를 열지 못했습니다")
        dash.slack_ready.wait()


def main() -> None:
    cfg.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),  # 창이 최소화돼 있어도 나중에 볼 수 있게 파일에도 남긴다
                  RotatingFileHandler(cfg.data_dir / "dugwali.log", maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8")])
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    store = Store(cfg.db_path)
    dash = Dashboard(store) if cfg.dashboard_port else None
    url = dash.start() if dash else None
    bot = connect_slack(store, dash, url)
    if dash:
        dash.bot, bot.dashboard, bot.dashboard_url = bot, dash, url

    if cfg.call_dir:
        # 처음 켠 시점을 기억해 두고, 그 뒤에 생겼는데 놓친 녹음은 켤 때마다 처리할지 묻는다
        baseline = store.get_state("calls_baseline")
        if baseline is None:
            baseline = str(time.time())
            store.set_state("calls_baseline", baseline)
        watcher = CallWatcher(cfg.call_dir, store, lambda p: handle_call(bot, store, p),
                              baseline=float(baseline), on_missed=bot.post_missed)
        bot.watcher = watcher
        bot.process_call = lambda p, notify=True, provider=None: handle_call(bot, store, p, notify, provider)
        threading.Thread(target=watcher.run_forever, daemon=True, name="calls").start()
    else:
        log.info("CALL_DIR 미설정: 통화 녹음 감시를 끕니다")
    threading.Thread(target=digest_loop, args=(bot, store), daemon=True, name="digest").start()

    log.info("두괄이 시작 (LLM: %s, 전사: %s, 통화 전사문 자동 전송: %s). 슬랙에서 봇에게 DM을 보내 보세요.",
             cfg.llm_provider, cfg.stt_provider, "켜짐" if cfg.send_calls_to_llm else "꺼짐")
    SocketModeHandler(bot.app, cfg.slack_app_token).start()


if __name__ == "__main__":
    main()
