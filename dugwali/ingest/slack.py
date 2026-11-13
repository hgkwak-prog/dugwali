"""슬랙 봇(Socket Mode). 봇과의 DM이 유일한 입구다.

- DM에 메모(또는 음성 파일)를 던지면 → 스레드에 두괄식 초안 + 양식 버튼
- 그 스레드에 답글을 달면 → 보충 정보로 합쳐 초안 갱신
- "할일" / "오늘" / "도움말" 명령
"""
import logging
import urllib.request
from pathlib import Path

from slack_bolt import App

from .. import digest, pipeline
from ..agent import render
from ..config import cfg
from ..store import Store
from .watcher import AUDIO_EXT

log = logging.getLogger(__name__)

HELP = (
    "*두괄이 사용법*\n"
    "• 여기에 아무렇게나 적어 보내면 두괄식·개조식으로 정리해 스레드에 답합니다.\n"
    "• 정리된 스레드에 답글을 달면 그 내용을 반영해 다시 정리합니다.\n"
    "• 음성 메모 파일을 올리면 받아써서 정리합니다.\n"
    "• `할일` 미완료 목록 · `오늘` 오늘 정리 · `도움말`"
)


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


def record_blocks(rec: dict) -> list[dict]:
    d = rec["data"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": render.slack(d)[:2900]}}]
    if d["ask_user"]:
        q = "\n".join(f"❓ {x}" for x in d["ask_user"])
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"{q}\n_이 스레드에 답글로 알려주시면 반영합니다. 몰라도 그대로 쓸 수 있어요._"}]})
    rid = str(rec["id"])
    blocks.append({"type": "actions", "elements": [
        _button("슬랙 공유용", "fmt_slack", rid),
        _button("메일 양식", "fmt_mail", rid),
        _button("구두 보고 대본", "fmt_verbal", rid),
    ]})
    return blocks


def todo_blocks(store: Store) -> list[dict]:
    from datetime import date

    todos = store.open_todos()
    if not todos:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "미완료 할 일이 없습니다."}}]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*미완료 {len(todos)}건*"}}]
    for t in todos[:20]:
        who = "" if t["owner"] in (None, "나") else f"{t['owner']}: "
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{who}{t['task']} · {digest.due_tag(t['due'], date.today())}"},
            "accessory": _button("완료", "todo_done", str(t["id"]), "primary"),
        })
    return blocks


def held_call_blocks(rec: dict, info: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": info}},
        {"type": "actions", "elements": [_button("이 통화 요약하기 (전사문을 LLM으로 전송)", "summarize_call", str(rec["id"]))]},
    ]


class Bot:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.app = App(token=cfg.slack_bot_token)
        self._dm_channel: str | None = None
        self._register()

    # --- 봇이 먼저 말 걸기(통화 초안, 다이제스트) ---
    def dm_channel(self) -> str:
        if not self._dm_channel:
            self._dm_channel = self.app.client.conversations_open(users=cfg.owner_user_id)["channel"]["id"]
        return self._dm_channel

    def post_dm(self, text: str, blocks: list[dict] | None = None) -> dict:
        return self.app.client.chat_postMessage(channel=self.dm_channel(), text=text, blocks=blocks)

    def post_record(self, rec: dict, prefix: str = "") -> None:
        """봇이 시작하는 기록(통화)을 DM 최상위 메시지로 올린다."""
        blocks = record_blocks(rec)
        if prefix:
            blocks.insert(0, {"type": "context", "elements": [{"type": "mrkdwn", "text": prefix}]})
        res = self.post_dm(rec["data"]["conclusion"], blocks)
        self.store.set_slack_ref(rec["id"], res["channel"], res["ts"], res["ts"])

    def post_held_call(self, rec: dict, info: str) -> None:
        res = self.post_dm(info, held_call_blocks(rec, info))
        self.store.set_slack_ref(rec["id"], res["channel"], res["ts"], res["ts"])

    # --- 내부 ---
    def _download(self, f: dict) -> Path:
        cfg.ensure_dirs()
        dest = cfg.transcript_dir / f"slack_{f['id']}_{Path(f.get('name') or 'audio').name}"
        req = urllib.request.Request(
            f.get("url_private_download") or f["url_private"],
            headers={"Authorization": f"Bearer {cfg.slack_bot_token}"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            dest.write_bytes(r.read())
        return dest

    def _register(self) -> None:
        app, store = self.app, self.store

        @app.event("message")
        def on_message(event, client):
            if event.get("channel_type") != "im" or event.get("bot_id"):
                return
            if event.get("subtype") not in (None, "file_share"):
                return
            if cfg.owner_user_id and event.get("user") != cfg.owner_user_id:
                return
            channel, ts = event["channel"], event["ts"]
            text = (event.get("text") or "").strip()
            thread_ts = event.get("thread_ts")

            # 1) 기존 기록 스레드의 답글 → 보충
            if thread_ts and thread_ts != ts:
                rec = store.find_by_thread(channel, thread_ts)
                if rec and rec["data"] and text:
                    try:
                        rec = pipeline.amend(store, rec["id"], text)
                        client.chat_update(channel=channel, ts=rec["msg_ts"],
                                           text=rec["data"]["conclusion"], blocks=record_blocks(rec))
                        client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
                    except Exception as e:
                        log.exception("보충 반영 실패")
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"반영 실패: {e}")
                return

            # 2) 명령
            if text in ("할일", "할 일", "todo"):
                client.chat_postMessage(channel=channel, text="미완료 할 일", blocks=todo_blocks(store))
                return
            if text in ("오늘", "정리", "digest"):
                client.chat_postMessage(channel=channel, text=digest.build(store))
                return
            if text in ("도움말", "help", "?"):
                client.chat_postMessage(channel=channel, text=HELP)
                return

            # 3) 새 메모(텍스트 또는 음성 파일)
            audio = [f for f in event.get("files") or []
                     if Path(f.get("name") or "").suffix.lower() in AUDIO_EXT
                     or str(f.get("mimetype", "")).startswith("audio/")]
            if not text and not audio:
                return
            holder = client.chat_postMessage(channel=channel, thread_ts=ts,
                                             text="받아쓰는 중…" if audio else "정리 중…")
            try:
                source, audio_path = "slack", None
                if audio:
                    from ..stt.transcribe import transcribe

                    path = self._download(audio[0])
                    result = transcribe(str(path), speakers=1, diarize=False)
                    text = f"{text}\n{result['text']}".strip()
                    source, audio_path = "voice", str(path)
                    if not text:
                        raise RuntimeError("음성에서 내용을 받아쓰지 못했습니다.")
                rec = pipeline.process(store, text, source, audio_path)
                store.set_slack_ref(rec["id"], channel, ts, holder["ts"])
                client.chat_update(channel=channel, ts=holder["ts"],
                                   text=rec["data"]["conclusion"], blocks=record_blocks(rec))
            except Exception as e:
                log.exception("메모 처리 실패")
                client.chat_update(channel=channel, ts=holder["ts"],
                                   text=f"정리 실패: {e}\n원문은 위 메시지에 그대로 남아 있습니다.")

        def _format_action(action_id: str, label: str, fn):
            @app.action(action_id)
            def handler(ack, body, client):
                ack()
                rec = store.get_record(int(body["actions"][0]["value"]))
                if not rec or not rec["data"]:
                    return
                client.chat_postMessage(
                    channel=body["channel"]["id"], thread_ts=rec["thread_ts"],
                    text=f"*{label}* (복사해서 쓰세요)\n```{fn(rec['data'])}```",
                )

        _format_action("fmt_slack", "슬랙 공유용", render.slack)
        _format_action("fmt_mail", "메일 양식", render.mail)
        _format_action("fmt_verbal", "구두 보고 대본", render.verbal)

        @app.action("todo_done")
        def on_todo_done(ack, body, client):
            ack()
            store.complete_todo(int(body["actions"][0]["value"]))
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                               text="미완료 할 일", blocks=todo_blocks(store))

        @app.action("summarize_call")
        def on_summarize_call(ack, body, client):
            ack()
            channel, ts = body["channel"]["id"], body["message"]["ts"]
            rid = int(body["actions"][0]["value"])
            client.chat_update(channel=channel, ts=ts, text="정리 중…", blocks=[])
            try:
                rec = pipeline.summarize(store, rid)
                client.chat_update(channel=channel, ts=ts,
                                   text=rec["data"]["conclusion"], blocks=record_blocks(rec))
            except Exception as e:
                log.exception("통화 요약 실패")
                client.chat_update(channel=channel, ts=ts, text=f"요약 실패: {e}")
