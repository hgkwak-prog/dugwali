"""슬랙 봇(Socket Mode). 봇과의 DM이 유일한 입구다.

- DM에 메모(또는 음성 파일)를 던지면 → 스레드에 두괄식 초안 + 양식 버튼
- 그 스레드에 답글을 달면 → 보충 정보로 합쳐 초안 갱신
- 초안의 버튼으로 다른 건에 합치기 / 삭제
- "건" / "할일" / "오늘" / "도움말" 명령
"""
import logging
import urllib.request
from datetime import date
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
    "• 같은 일에 대한 기록은 초안의 *합치기* 버튼으로 한 건으로 묶습니다. 일이 끝난 뒤의 후속 메모도 같은 방법으로 붙입니다.\n"
    "• 음성 메모 파일을 올리면 받아써서 정리합니다.\n"
    "• `건` 진행 중인 건 목록(통합 정리·종결·삭제) · `할일` 미완료 목록 · `오늘` 오늘 정리 · `도움말`"
)
MAX_CASES_LISTED = 15


def _plain(text: str, limit: int = 75) -> dict:
    return {"type": "plain_text", "text": text if len(text) <= limit else text[: limit - 1] + "…"}


def _button(text: str, action_id: str, value: str, style: str | None = None,
            confirm: str | None = None) -> dict:
    b = {"type": "button", "text": _plain(text), "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    if confirm:
        b["confirm"] = {"title": _plain("확인"), "text": _plain(confirm, 300),
                        "confirm": _plain("삭제"), "deny": _plain("취소"), "style": "danger"}
    return b


def _section(text: str, accessory: dict | None = None) -> dict:
    b = {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    if accessory:
        b["accessory"] = accessory
    return b


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def record_blocks(store: Store, rec: dict) -> list[dict]:
    d, rid = rec["data"], str(rec["id"])
    case = store.get_case(rec["case_id"])
    others = [c for c in store.open_cases() if c["id"] != rec["case_id"]]
    blocks = [_section(render.slack(d))]

    n = len(store.case_records(rec["case_id"]))
    if n > 1:
        blocks.append(_context(f"건 *#{case['id']} {case['title']}* · 기록 {n}건 · `건`에서 통합 정리 보기"))
    if d["ask_user"]:
        q = "\n".join(f"❓ {x}" for x in d["ask_user"])
        blocks.append(_context(f"{q}\n_이 스레드에 답글로 알려주시면 반영합니다. 몰라도 그대로 쓸 수 있어요._"))

    blocks.append({"type": "actions", "elements": [
        _button("슬랙 공유용", "fmt_slack", rid),
        _button("메일 양식", "fmt_mail", rid),
        _button("구두 보고 대본", "fmt_verbal", rid),
    ]})
    manage = []
    suggested = next((c for c in others if c["id"] == d.get("related_case")), None)
    if suggested:
        manage.append(_button(f"↪ #{suggested['id']} {suggested['title']}에 합치기", "merge_suggest",
                              f"{rid}:{suggested['id']}", "primary"))
    if others:
        manage.append({
            "type": "static_select", "action_id": "merge_select",
            "placeholder": _plain("다른 건에 합치기"),
            "options": [{"text": _plain(f"#{c['id']} {c['title']}"), "value": f"{rid}:{c['id']}"}
                        for c in others[:100]],
        })
    manage.append(_button("삭제", "delete_record", rid, "danger", "이 기록과 여기서 나온 할 일을 지웁니다."))
    blocks.append({"type": "actions", "elements": manage})
    return blocks


def todo_blocks(store: Store) -> list[dict]:
    todos = store.open_todos()
    if not todos:
        return [_section("미완료 할 일이 없습니다.")]
    blocks = [_section(f"*미완료 {len(todos)}건*")]
    for t in todos[:20]:
        blocks.append(_section(digest.todo_text(t, date.today()),
                               _button("완료", "todo_done", str(t["id"]), "primary")))
    return blocks


def case_list_blocks(store: Store) -> list[dict]:
    cases = store.open_cases()
    if not cases:
        return [_section("진행 중인 건이 없습니다.")]
    blocks = [_section(f"*진행 중인 건 {len(cases)}개*")]
    for c in cases[:MAX_CASES_LISTED]:
        cid = str(c["id"])
        blocks.append(_section(f"*#{cid} {c['title']}* · 기록 {c['n_records']}건 · 할 일 {c['n_todos']}건"))
        blocks.append({"type": "actions", "elements": [
            _button("정리 보기", "case_view", cid),
            _button("종결", "case_close", cid),
            _button("삭제", "case_delete", cid, "danger",
                    f"#{cid} 건의 기록 {c['n_records']}건과 할 일을 모두 지웁니다."),
        ]})
    if len(cases) > MAX_CASES_LISTED:
        blocks.append(_context(f"최근 {MAX_CASES_LISTED}개만 표시했습니다. 끝난 건은 종결하면 목록에서 빠집니다."))
    return blocks


def case_view_blocks(store: Store, cid: int, data: dict) -> list[dict]:
    recs = store.case_records(cid)
    lines = []
    for r in recs:
        title = r["data"]["title"] if r["data"] else "(요약 전 통화 전사문, 정리에서 제외)"
        lines.append(f"{r['created_at'][5:16].replace('T', ' ')} {title}")
    key = f"c{cid}"
    return [
        _section(render.slack(data)),
        _context(f"건 *#{cid}* · 기록 {len(recs)}건\n" + "\n".join(f"• {x}" for x in lines)),
        {"type": "actions", "elements": [
            _button("슬랙 공유용", "fmt_slack", key),
            _button("메일 양식", "fmt_mail", key),
            _button("구두 보고 대본", "fmt_verbal", key),
        ]},
    ]


def held_call_blocks(rec: dict, info: str) -> list[dict]:
    rid = str(rec["id"])
    return [
        _section(info),
        {"type": "actions", "elements": [
            _button("이 통화 요약하기 (전사문을 LLM으로 전송)", "summarize_call", rid),
            _button("삭제", "delete_record", rid, "danger", "이 통화 기록을 지웁니다. 전사문 파일은 남습니다."),
        ]},
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
        blocks = record_blocks(self.store, rec)
        if prefix:
            blocks.insert(0, _context(prefix))
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

    def _redraw(self, client, rec: dict) -> None:
        """기록의 초안 메시지를 현재 상태로 다시 그린다."""
        if rec.get("channel") and rec.get("msg_ts") and rec["data"]:
            client.chat_update(channel=rec["channel"], ts=rec["msg_ts"],
                               text=rec["data"]["conclusion"], blocks=record_blocks(self.store, rec))

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
                        self._redraw(client, pipeline.amend(store, rec["id"], text))
                        client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
                    except Exception as e:
                        log.exception("보충 반영 실패")
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"반영 실패: {e}")
                return

            # 2) 명령
            if text in ("건", "목록", "cases"):
                client.chat_postMessage(channel=channel, text="진행 중인 건", blocks=case_list_blocks(store))
                return
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
                self._redraw(client, store.get_record(rec["id"]))
            except Exception as e:
                log.exception("메모 처리 실패")
                client.chat_update(channel=channel, ts=holder["ts"],
                                   text=f"정리 실패: {e}\n원문은 위 메시지에 그대로 남아 있습니다.")

        def _format_action(action_id: str, label: str, fn):
            @app.action(action_id)
            def handler(ack, body, client):
                ack()
                value = body["actions"][0]["value"]
                if value.startswith("c"):  # 건 통합 정리(이미 계산돼 캐시된 결과)
                    data = pipeline.case_summary(store, int(value[1:]))
                else:
                    rec = store.get_record(int(value))
                    data = rec["data"] if rec else None
                if not data:
                    return
                msg = body["message"]
                client.chat_postMessage(
                    channel=body["channel"]["id"], thread_ts=msg.get("thread_ts") or msg["ts"],
                    text=f"*{label}* (복사해서 쓰세요)\n```{fn(data)}```",
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
                self._redraw(client, pipeline.summarize(store, rid))
            except Exception as e:
                log.exception("통화 요약 실패")
                client.chat_update(channel=channel, ts=ts, text=f"요약 실패: {e}")

        def _merge(body, client, value: str) -> None:
            rid, cid = (int(x) for x in value.split(":"))
            msg = body["message"]
            thread = msg.get("thread_ts") or msg["ts"]
            try:
                rec = pipeline.merge(store, rid, cid)
                self._redraw(client, rec)
                case = store.get_case(cid)
                client.chat_postMessage(
                    channel=body["channel"]["id"], thread_ts=thread,
                    text=f"↪ *#{cid} {case['title']}* 건에 합쳤습니다. `건`에서 통합 정리를 볼 수 있어요.")
            except Exception as e:
                log.exception("병합 실패")
                client.chat_postMessage(channel=body["channel"]["id"], thread_ts=thread, text=f"합치기 실패: {e}")

        @app.action("merge_suggest")
        def on_merge_suggest(ack, body, client):
            ack()
            _merge(body, client, body["actions"][0]["value"])

        @app.action("merge_select")
        def on_merge_select(ack, body, client):
            ack()
            _merge(body, client, body["actions"][0]["selected_option"]["value"])

        @app.action("delete_record")
        def on_delete_record(ack, body, client):
            ack()
            pipeline.delete(store, int(body["actions"][0]["value"]))
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                               text="🗑 삭제했습니다.", blocks=[])

        @app.action("case_view")
        def on_case_view(ack, body, client):
            ack()
            channel, cid = body["channel"]["id"], int(body["actions"][0]["value"])
            holder = client.chat_postMessage(channel=channel, text=f"#{cid} 건 정리 중…")
            try:
                data = pipeline.case_summary(store, cid)
                if not data:
                    client.chat_update(channel=channel, ts=holder["ts"], text=f"#{cid} 건에 정리할 기록이 없습니다.")
                    return
                client.chat_update(channel=channel, ts=holder["ts"], text=data["conclusion"],
                                   blocks=case_view_blocks(store, cid, data))
            except Exception as e:
                log.exception("건 정리 실패")
                client.chat_update(channel=channel, ts=holder["ts"], text=f"정리 실패: {e}")

        def _case_list_action(action_id: str, fn):
            @app.action(action_id)
            def handler(ack, body, client):
                ack()
                fn(store, int(body["actions"][0]["value"]))
                client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                                   text="진행 중인 건", blocks=case_list_blocks(store))

        _case_list_action("case_close", pipeline.close)
        _case_list_action("case_delete", pipeline.delete_case)
