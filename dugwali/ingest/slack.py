"""슬랙 봇(Socket Mode). 봇과의 DM이 유일한 입구다.

- DM에 메모(또는 음성 파일)를 던지면 → 스레드에 두괄식 초안 + 양식 버튼
- 그 스레드에 답글을 달면 → 보충 정보로 합쳐 초안 갱신
- 초안의 버튼으로 다른 건에 합치기 / 삭제
- "건" / "할일" / "오늘" / "조치" / "도움말" 명령
- 처리에 실패한 건은 다시 시도 버튼과 함께 알리고, "조치" 목록에 남긴다
"""
import logging
import urllib.request
from datetime import date
from pathlib import Path

from slack_bolt import App

from .. import digest, pipeline, profile, views
from ..agent import render
from ..config import cfg
from ..labels import SOURCE_LABEL
from ..store import Store
from ..stt.transcribe import local_available
from . import transcript
from .watcher import AUDIO_EXT

log = logging.getLogger(__name__)

HELP = (
    "*두괄이 사용법*\n"
    "• 여기에 아무렇게나 적어 보내면 두괄식·개조식으로 정리해 스레드에 답합니다.\n"
    "• 정리된 스레드에 답글을 달면 그 내용을 반영해 다시 정리합니다.\n"
    "• 같은 일에 대한 기록은 초안의 *합치기* 버튼으로 한 건으로 묶습니다. 일이 끝난 뒤의 후속 메모도 같은 방법으로 붙입니다.\n"
    "• 음성 메모 파일을 올리면 받아써서 정리합니다. 다른 도구로 전사한 파일(.txt/.vtt/.srt)도 받습니다.\n"
    "• `건` 진행 중인 건 목록(통합 정리·종결·삭제) · `할일` 미완료 목록 · `오늘` 오늘 정리\n"
    "• `조치` 처리에 실패했거나 아직 정리 안 된 기록, 놓친 통화 녹음\n"
    "• `프로필` 내 정리 기준(profile.md)이 어떻게 읽혔는지 · `대시보드` 건·할 일을 보는 화면 주소 · `도움말`"
)
MAX_CASES_LISTED = 15
MAX_ATTENTION_LISTED = 10
STAGE_LABEL = {"extract": "정리", "call": "통화 녹음 처리", "voice": "음성 메모 전사", "digest": "마감 정리 전송"}
STT_STAGES = ("call", "voice")  # 전사가 들어가는 단계: 전사 방식을 바꿔 다시 시도할 수 있다


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
    d, rid = views.record_data(store, rec), str(rec["id"])
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
    manage.append(_button("삭제", "delete_record", rid, "danger",
                          "이 기록을 휴지통으로 옮깁니다. 30일 안에 대시보드에서 되살릴 수 있습니다."))
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
                    f"#{cid} 건의 기록 {c['n_records']}건을 휴지통으로 옮깁니다. 30일 안에 되살릴 수 있습니다."),
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
        _section(render.slack(views.case_data(store, cid, data))),
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
            _button("요약하기 (전사문을 LLM으로 전송)", "summarize_call", rid),
            _button("삭제", "delete_record", rid, "danger", "이 기록을 지웁니다. 전사문 파일은 남습니다."),
        ]},
    ]


def _failure_name(f: dict) -> str:
    if f["stage"] == "call":
        return f"`{Path(f['target']).name}`"
    if f.get("record_id"):
        return f"기록 {f['record_id']}"
    return f["target"]


def failure_blocks(f: dict, in_list: bool = False) -> list[dict]:
    """실패 한 건: 무엇이 왜 실패했는지와 다시 시도·넘기기 버튼."""
    kept = "\n원문은 저장돼 있습니다." if f["stage"] == "extract" else ""
    value = f"{'L' if in_list else 'S'}:{f['id']}"
    buttons = [_button("다시 시도", "retry_failure", value, "primary")]
    if f["stage"] in STT_STAGES and cfg.stt_provider == "api" and local_available():
        buttons.append(_button("로컬 전사로 다시 시도", "retry_failure_local", value))
    buttons.append(_button("넘기기", "dismiss_failure", value))
    return [
        _section(f"⚠️ *{STAGE_LABEL.get(f['stage'], f['stage'])} 실패* · {_failure_name(f)} · "
                 f"시도 {f['attempts']}회{kept}\n```{f['error'][:300]}```"),
        {"type": "actions", "elements": buttons},
    ]


def missed_blocks(paths: list[Path], in_list: bool = False) -> list[dict]:
    names = "\n".join(f"• `{p.name}`" for p in paths[:MAX_ATTENTION_LISTED])
    more = f"\n… 외 {len(paths) - MAX_ATTENTION_LISTED}개" if len(paths) > MAX_ATTENTION_LISTED else ""
    value = "L" if in_list else "S"
    return [
        _section(f"📼 *놓친 통화 녹음 {len(paths)}개* (꺼져 있던 동안 생겼거나 늦게 동기화됨)\n{names}{more}"),
        {"type": "actions", "elements": [
            _button("모두 처리", "missed_process", value, "primary"),
            _button("건너뛰기", "missed_skip", value),
        ]},
    ]


def attention_blocks(store: Store, missed: list[Path] | None = ()) -> list[dict]:
    """사람이 조치해야 하는 것 전부: 처리 실패, 아직 정리 안 된 기록, 놓친 통화 녹음.

    LLM의 판단이 아니라 DB와 녹음 폴더를 조회한 결과다. missed가 None이면 통화 녹음 감시가 꺼진 상태다.
    """
    failures, pending = digest.attention(store)
    counts = (f"처리 실패 {len(failures)}건 · 정리 안 된 기록 {len(pending)}건 · 놓친 통화 녹음 "
              + ("확인 안 함(감시 꺼짐)" if missed is None else f"{len(missed)}개"))
    missed = missed or []
    if not failures and not pending and not missed:
        return [_section(f"막힌 건이 없습니다. ({counts})\n해야 할 업무는 `할일`에서 봅니다.")]
    blocks = [_section(f"*조치 필요* · {counts}")]
    for f in failures[:MAX_ATTENTION_LISTED]:
        blocks += failure_blocks(f, in_list=True)
    for r in pending[:MAX_ATTENTION_LISTED]:
        rid = str(r["id"])
        name = f" · `{Path(r['audio_path']).name}`" if r.get("audio_path") else ""
        blocks.append(_section(f"📄 *기록 {rid}* · {r['created_at'][:16].replace('T', ' ')} · "
                               f"{SOURCE_LABEL.get(r['source'], r['source'])}{name} · 아직 정리 안 됨"))
        blocks.append({"type": "actions", "elements": [
            _button("정리하기 (LLM으로 전송)", "pending_summarize", rid, "primary"),
            _button("삭제", "pending_delete", rid, "danger", "이 기록을 지웁니다. 전사문 파일은 남습니다."),
        ]})
    hidden = max(len(failures) - MAX_ATTENTION_LISTED, 0) + max(len(pending) - MAX_ATTENTION_LISTED, 0)
    if hidden:
        blocks.append(_context(f"{hidden}건은 목록에 다 싣지 못했습니다. 위 항목을 처리한 뒤 `조치`를 다시 보내세요."))
    if missed:
        blocks += missed_blocks(list(missed), in_list=True)
    return blocks


class Bot:
    def __init__(self, store: Store) -> None:
        self.store = store
        # 통화 녹음 감시를 켰을 때 app.py가 채운다
        self.watcher = None        # CallWatcher
        self.process_call = None   # (Path, notify, provider) -> 남은 실패 항목 또는 None
        self.dashboard = None      # 대시보드를 띄웠을 때 app.py가 채운다
        self.dashboard_url = None
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

    def post_failure(self, f: dict) -> None:
        """실패를 DM으로 알린다. 원문이 저장된 기록이면 이 메시지를 그 기록의 자리로 삼는다."""
        res = self.post_dm(f"{STAGE_LABEL.get(f['stage'], f['stage'])} 실패", failure_blocks(f))
        self._link(f, res["channel"], res["ts"])

    def post_missed(self, paths: list[Path]) -> None:
        self.post_dm(f"놓친 통화 녹음 {len(paths)}개", missed_blocks(paths))

    def send_digest(self, day: date | None = None) -> None:
        self.post_dm(digest.build(self.store, day))

    # --- 내부 ---
    def _link(self, f: dict, channel: str, ts: str) -> None:
        rec = self.store.get_record(f["record_id"]) if f.get("record_id") else None
        if rec and not rec.get("msg_ts"):
            self.store.set_slack_ref(rec["id"], channel, ts, ts)

    def _missed(self) -> list[Path] | None:
        return self.watcher.missed() if self.watcher else None

    def _import(self, client, channel: str, ts: str, note: str, f: dict) -> None:
        """슬랙에 올린 전사문 파일(.txt/.vtt/.srt)을 통화 기록으로 받는다."""
        store = self.store
        holder = client.chat_postMessage(channel=channel, thread_ts=ts, text="전사문 받는 중…")
        rec = None
        try:
            body = transcript.read(self._download(f))
            if not body:
                raise RuntimeError("전사문에서 내용을 찾지 못했습니다.")
            rec = pipeline.hold(store, "\n".join(x for x in (note, f"파일명: {f.get('name')}", body) if x), "import")
            store.set_slack_ref(rec["id"], channel, ts, holder["ts"])
            if cfg.send_calls_to_llm:
                self._redraw(client, pipeline.summarize(store, rec["id"]))
            else:
                info = "📄 전사문을 받았습니다. 이 PC에만 저장했습니다."
                client.chat_update(channel=channel, ts=holder["ts"], text=info,
                                   blocks=held_call_blocks(store.get_record(rec["id"]), info))
        except pipeline.ProcessError as e:
            client.chat_update(channel=channel, ts=holder["ts"], text="정리 실패", blocks=failure_blocks(e.failure))
        except Exception as e:
            log.exception("전사문 가져오기 실패")
            client.chat_update(channel=channel, ts=holder["ts"], text=f"전사문 가져오기 실패: {e}")

    def retry_failure(self, f: dict, provider: str | None = None) -> dict | None:
        """슬랙 밖(대시보드)에서 실패를 다시 시도할 때 쓴다."""
        return self._retry(self.app.client, f, provider)

    def show_record(self, rec: dict) -> None:
        """슬랙 밖(대시보드)에서 정리한 기록을 슬랙에도 반영한다."""
        self._show(self.app.client, rec)

    def refresh_record(self, rec: dict) -> None:
        """슬랙 밖(대시보드)에서 바뀐 기록의 초안을 다시 그린다. 초안이 올라간 적 없으면 아무것도 하지 않는다."""
        if not (rec.get("channel") and rec.get("msg_ts")):
            return
        if rec.get("deleted_at"):
            self.app.client.chat_update(channel=rec["channel"], ts=rec["msg_ts"], blocks=[],
                                        text="🗑 휴지통으로 옮겼습니다(대시보드). 30일 안에 되살릴 수 있습니다.")
        else:
            self._redraw(self.app.client, rec)

    def _voice(self, client, rid: int, provider: str | None = None) -> dict | None:
        """음성 메모 기록을 받아써서 정리한다. 막히면 실패 항목을 남기고 돌려준다(성공하면 None).

        통화 녹음과 같은 게이트를 거친다: SEND_CALLS_TO_LLM이 꺼져 있으면 전사문을 보관만 하고 버튼을 낸다.
        """
        from ..stt.transcribe import transcribe

        store, target = self.store, f"record:{rid}"
        rec = store.get_record(rid)
        try:
            result = transcribe(rec["audio_path"], speakers=1, diarize=False, provider=provider)
            if not result["text"].strip():
                raise RuntimeError("음성에서 내용을 받아쓰지 못했습니다.")
        except Exception as e:
            log.exception("음성 메모 전사 실패: 기록 %s", rid)
            return store.record_failure("voice", target, str(e) or type(e).__name__, rid)
        store.resolve_failures(stage="voice", target=target)
        store.update_record(rid, raw_text=f"{rec['raw_text']}\n{result['text']}".strip())
        if cfg.send_calls_to_llm:
            try:
                self._show(client, pipeline.summarize(store, rid))
            except pipeline.ProcessError as e:
                return e.failure
        else:
            info = "🎙 음성 메모를 받아썼습니다. 전사문은 이 PC에만 저장했습니다."
            client.chat_update(channel=rec["channel"], ts=rec["msg_ts"], text=info,
                               blocks=held_call_blocks(rec, info))
        return None

    def _show(self, client, rec: dict) -> None:
        """정리된 기록을 제자리에 그린다. 자리가 없으면 DM에 새로 올린다."""
        if rec.get("channel") and rec.get("msg_ts"):
            self._redraw(client, rec)
        else:
            self.post_record(rec)

    def _retry(self, client, f: dict, provider: str | None = None) -> dict | None:
        """실패한 작업을 다시 한다. 또 실패하면 그 실패 항목을, 성공하면 None을 돌려준다.

        provider: 전사가 들어가는 단계에서 설정과 다른 전사 방식으로 다시 할 때.
        """
        store = self.store
        if f.get("record_id") and not store.get_record(f["record_id"]):
            store.resolve_failures(fid=f["id"])
            return None
        if f["stage"] == "voice":
            return self._voice(client, f["record_id"], provider)
        if f["stage"] == "extract":
            try:
                self._show(client, pipeline.summarize(store, f["record_id"]))
            except pipeline.ProcessError as e:
                return e.failure
            return None
        if f["stage"] == "call":
            if not self.process_call:
                return store.record_failure("call", f["target"], "통화 녹음 감시가 꺼져 있습니다(CALL_DIR).")
            return self.process_call(Path(f["target"]), False, provider)
        if f["stage"] == "digest":
            try:
                self.send_digest(date.fromisoformat(f["target"]))
            except Exception as e:
                return store.record_failure("digest", f["target"], str(e) or type(e).__name__)
            store.set_state("digest_sent_on", f["target"])
        store.resolve_failures(fid=f["id"])
        return None

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
            if text in ("조치", "조치 필요", "실패"):
                client.chat_postMessage(channel=channel, text="조치 필요",
                                        blocks=attention_blocks(store, self._missed()))
                return
            if text in ("대시보드 로그아웃", "dashboard logout"):
                n = store.delete_sessions()
                client.chat_postMessage(channel=channel, text=f"대시보드에 로그인한 브라우저 {n}개를 모두 로그아웃했습니다.")
                return
            if text in ("대시보드", "dashboard"):
                link = self.dashboard.login_link() if self.dashboard and self.dashboard_url else None
                if not self.dashboard_url:
                    reply = "대시보드가 꺼져 있습니다(.env의 DASHBOARD_PORT 확인)."
                elif link:  # 이 DM은 소유자만 받으므로 링크가 곧 본인 확인이다
                    reply = (f"*다른 기기에서 열기* (10분 안에 한 번만 쓸 수 있는 링크)\n{link}\n"
                             f"*두괄이가 켜져 있는 PC에서 열기*\n{self.dashboard_url}\n"
                             "로그인한 브라우저는 대시보드의 `접속 관리`에서 보고 끊을 수 있습니다. "
                             "한꺼번에 끊으려면 `대시보드 로그아웃`을 보내세요.")
                else:
                    reply = f"대시보드: {self.dashboard_url}\n두괄이가 켜져 있는 그 PC의 브라우저에서만 열립니다."
                client.chat_postMessage(channel=channel, text=reply)
                return
            if text in ("프로필", "profile"):
                p = profile.load()
                hint = "" if p.exists else ("\n프로필 파일이 없어 기본값으로 정리합니다. 만들려면 PC에서 "
                                            "`python -m dugwali.cli profile --init`을 실행하세요.")
                client.chat_postMessage(channel=channel, text=f"```{profile.describe(p)}```{hint}")
                return
            if text in ("도움말", "help", "?"):
                client.chat_postMessage(channel=channel, text=HELP)
                return

            # 3) 새 메모(텍스트 또는 음성 파일)
            audio = [f for f in event.get("files") or []
                     if Path(f.get("name") or "").suffix.lower() in AUDIO_EXT
                     or str(f.get("mimetype", "")).startswith("audio/")]
            docs = [f for f in event.get("files") or []
                    if Path(f.get("name") or "").suffix.lower() in transcript.TRANSCRIPT_EXT]
            if docs and not audio:  # 다른 도구에서 전사한 결과: 통화 녹음과 같은 게이트를 거친다
                self._import(client, channel, ts, text, docs[0])
                return
            if not text and not audio:
                return
            holder = client.chat_postMessage(channel=channel, thread_ts=ts,
                                             text="받아쓰는 중…" if audio else "정리 중…")
            try:
                if audio:  # 기록을 먼저 만들어 두고 받아쓴다: 전사가 실패해도 다시 시도할 수 있다
                    path = self._download(audio[0])
                    rid = store.add_record("voice", text, None, str(path))
                    store.set_slack_ref(rid, channel, ts, holder["ts"])
                    left = self._voice(client, rid)
                    if left:
                        client.chat_update(channel=channel, ts=holder["ts"], text="처리 실패",
                                           blocks=failure_blocks(left))
                    return
                rec = pipeline.process(store, text, "slack")
                store.set_slack_ref(rec["id"], channel, ts, holder["ts"])
                self._redraw(client, store.get_record(rec["id"]))
            except pipeline.ProcessError as e:  # 원문은 저장됨: 이 자리에서 다시 시도할 수 있게 한다
                store.set_slack_ref(e.record["id"], channel, ts, holder["ts"])
                client.chat_update(channel=channel, ts=holder["ts"], text="정리 실패",
                                   blocks=failure_blocks(e.failure))
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
                    data = views.case_data(store, int(value[1:]), data) if data else None
                else:
                    rec = store.get_record(int(value))
                    data = views.record_data(store, rec) if rec else None
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
            except pipeline.ProcessError as e:
                client.chat_update(channel=channel, ts=ts, text="요약 실패", blocks=failure_blocks(e.failure))
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
                               text="🗑 휴지통으로 옮겼습니다. 30일 안에 대시보드 휴지통에서 되살릴 수 있습니다.", blocks=[])

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

        # --- 조치 필요: 실패 다시 시도, 정리 안 된 기록, 놓친 통화 녹음 ---
        def _relist(client, body) -> None:
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text="조치 필요",
                               blocks=attention_blocks(store, self._missed()))

        def _failure_action(body):
            where, _, fid = body["actions"][0]["value"].partition(":")
            return where == "L", store.get_failure(int(fid))

        def _do_retry(body, client, provider: str | None = None) -> None:
            channel, ts = body["channel"]["id"], body["message"]["ts"]
            in_list, f = _failure_action(body)
            left = None
            if f and not f["resolved_at"]:
                if not in_list:
                    client.chat_update(channel=channel, ts=ts, text="다시 시도 중…", blocks=[])
                try:
                    left = self._retry(client, f, provider)
                except Exception as e:
                    log.exception("다시 시도 실패")
                    left = store.record_failure(f["stage"], f["target"], str(e) or type(e).__name__,
                                                f["record_id"])
            if in_list:
                _relist(client, body)
            elif left:
                self._link(left, channel, ts)
                client.chat_update(channel=channel, ts=ts, text="다시 실패", blocks=failure_blocks(left))
            else:
                rec = store.get_record(f["record_id"]) if f and f.get("record_id") else None
                if not (rec and rec.get("msg_ts") == ts and rec["data"]):  # 초안으로 바뀐 자리는 그대로 둔다
                    client.chat_update(channel=channel, ts=ts, text="✅ 다시 처리했습니다.", blocks=[])

        @app.action("retry_failure")
        def on_retry_failure(ack, body, client):
            ack()
            _do_retry(body, client)

        @app.action("retry_failure_local")
        def on_retry_failure_local(ack, body, client):
            ack()
            _do_retry(body, client, "local")

        @app.action("dismiss_failure")
        def on_dismiss_failure(ack, body, client):
            ack()
            in_list, f = _failure_action(body)
            note = pipeline.dismiss_failure(store, f["id"]) if f else ""
            note = f" {note} (`조치`에서 볼 수 있습니다)" if "남아" in note else f" {note}" if note else ""
            if in_list:
                _relist(client, body)
            else:
                client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                                   text=f"넘겼습니다.{note}", blocks=[])

        @app.action("pending_summarize")
        def on_pending_summarize(ack, body, client):
            ack()
            try:
                self._show(client, pipeline.summarize(store, int(body["actions"][0]["value"])))
            except Exception:
                log.exception("정리 실패")  # 실패 항목으로 목록에 다시 나온다
            _relist(client, body)

        @app.action("pending_delete")
        def on_pending_delete(ack, body, client):
            ack()
            pipeline.delete(store, int(body["actions"][0]["value"]))
            _relist(client, body)

        @app.action("missed_process")
        def on_missed_process(ack, body, client):
            ack()
            paths = self._missed()
            if body["actions"][0]["value"] != "L":
                client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=[],
                                   text=f"놓친 통화 녹음 {len(paths)}개를 처리합니다. 끝나는 대로 하나씩 올라옵니다.")
            for p in paths:
                store.mark_seen(str(p))
                if self.process_call:
                    self.process_call(p, True, None)
            if body["actions"][0]["value"] == "L":
                _relist(client, body)

        @app.action("missed_skip")
        def on_missed_skip(ack, body, client):
            ack()
            n = self.watcher.skip_missed() if self.watcher else 0
            if body["actions"][0]["value"] == "L":
                _relist(client, body)
            else:
                client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=[],
                                   text=f"놓친 통화 녹음 {n}개를 건너뛰었습니다. 파일은 그대로 있습니다.")

        _case_list_action("case_close", pipeline.close)
        _case_list_action("case_delete", pipeline.delete_case)
