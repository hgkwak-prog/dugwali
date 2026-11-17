"""로컬 대시보드. 봇과 같은 프로세스가 127.0.0.1에만 띄운다. 로그인은 없다.

- 표준 라이브러리만 쓴다(설치할 것이 늘지 않게). 화면은 서버가 그린 HTML이고 스크립트 없이 동작한다.
- 이 PC 밖에서는 열리지 않는다: 127.0.0.1에만 바인딩하고, Host 헤더가 이 주소가 아니면 거절한다.
- 모든 쓰기(POST)는 실행할 때마다 새로 만드는 토큰을 확인한다. 다른 웹사이트가 몰래 요청을 보내지 못한다.
- 쓰기는 슬랙 버튼과 같은 내부 함수(`pipeline.py`)를 거친다.

슬랙 봇 없이도 뜬다(`python -m dugwali.cli dashboard`). 그때는 슬랙이 필요한 동작만 빠진다.
"""
import hmac
import logging
import secrets
import threading
from datetime import date
from html import escape as e
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import digest, pipeline, profile
from .config import cfg
from .ingest import transcript
from .labels import SOURCE_LABEL
from .store import Store
from .stt.transcribe import local_available

log = logging.getLogger(__name__)
QUESTION_DAYS = 14       # 이 기간 안의 기록에서 아직 답하지 않은 질문을 보여준다
MAX_BODY = 5_000_000     # 붙여 넣는 전사문 크기 상한(바이트)
STAGE_LABEL = {"extract": "정리", "call": "통화 녹음 처리", "voice": "음성 메모 전사", "digest": "마감 정리 전송"}
STT_STAGES = ("call", "voice")

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2024;--muted:#667085;--line:#e3e6ea;--accent:#2f6fed;
--on-accent:#fff;--warn:#b54708;--warn-bg:#fff4e5;--ok:#027a48;--ok-bg:#e8f7ef;--danger:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--text:#e6e8eb;--muted:#98a2b3;
--line:#2c3038;--accent:#7aa5ff;--on-accent:#0d1830;--warn:#fdb022;--warn-bg:#3a2a10;--ok:#47cd89;--ok-bg:#12301f;--danger:#f97066}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,"Malgun Gothic","Apple SD Gothic Neo",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0}
nav{max-width:980px;margin:0 auto;padding:0 16px;display:flex;gap:4px;align-items:center;flex-wrap:wrap}
nav .brand{font-weight:700;margin-right:12px;color:var(--text)}
nav a{padding:12px 10px;color:var(--muted);border-bottom:2px solid transparent}
nav a.on{color:var(--text);border-bottom-color:var(--accent)}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:4px 0 14px}h2{font-size:16px;margin:26px 0 10px}h3{font-size:15px;margin:0 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.row{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
.grow{flex:1;min-width:240px}
.muted{color:var(--muted)}.small{font-size:13px}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;background:var(--line);color:var(--text);margin-left:6px}
.badge.warn{background:var(--warn-bg);color:var(--warn)}.badge.ok{background:var(--ok-bg);color:var(--ok)}
.flash{background:var(--ok-bg);color:var(--ok);border-radius:8px;padding:10px 14px;margin:0 0 14px}
.notice{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:10px 14px;margin:10px 0}
ul{margin:4px 0;padding-left:20px}li{margin:2px 0}
.label{font-weight:600;margin-top:10px}
.concl{font-size:16px;font-weight:600;margin:2px 0 4px}
form.inline{display:inline}
button{font:inherit;padding:5px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
button:hover{filter:brightness(.96)}
textarea,input[type=text]{width:100%;font:inherit;padding:8px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--text)}
textarea{min-height:260px;font-family:ui-monospace,Consolas,monospace;font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:13px;margin:8px 0}
details summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:8px}
.todo{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:8px 0;border-top:1px solid var(--line)}
.todo:first-child{border-top:0}.done{color:var(--muted);text-decoration:line-through}
.over{color:var(--danger);font-weight:600}
"""


def _when(ts: str) -> str:
    return ts[:16].replace("T", " ")


def _source(rec: dict) -> str:
    return SOURCE_LABEL.get(rec["source"], rec["source"])


def _list(label: str, items: list[str]) -> str:
    if not items:
        return ""
    return f'<div class="label">{e(label)}</div><ul>' + "".join(f"<li>{e(x)}</li>" for x in items) + "</ul>"


def data_html(d: dict) -> str:
    """정리 결과(공통 스키마) 한 덩어리. 결론이 맨 위다."""
    who = f' <span class="muted">· {e(d["counterpart"])}</span>' if d.get("counterpart") else ""
    todos = [f"{t['task']} ({t.get('owner') or '담당 미정'}, {t.get('due') or '기한 미정'})" for t in d["todos"]]
    out = [f'<div class="muted small">[{e(d["kind"])}]{who}</div>', f'<div class="concl">{e(d["conclusion"])}</div>',
           _list("결정", d["decisions"]), _list("할 일", todos), _list("확인 필요", d["open_questions"]),
           _list("배경", d["background"])]
    if d.get("resume_point"):
        out.append(f'<div class="label">복귀 지점</div><div>{e(d["resume_point"])}</div>')
    if d.get("share_to"):
        out.append(f'<div class="muted small" style="margin-top:8px">공유 대상: {e(d["share_to"])}</div>')
    return "".join(out)


class Dashboard:
    def __init__(self, store: Store, bot=None, port: int | None = None) -> None:
        self.store, self.bot = store, bot
        self.port = cfg.dashboard_port if port is None else port
        self.token = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> str | None:
        """백그라운드로 띄우고 주소를 돌려준다. 포트를 못 잡으면 None(봇은 계속 돈다)."""
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        except OSError as err:
            log.error("대시보드를 띄우지 못했습니다(포트 %s): %s. .env의 DASHBOARD_PORT를 바꿔 보세요.", self.port, err)
            return None
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True, name="dashboard").start()
        log.info("대시보드: %s (이 PC에서만 열립니다)", self.url)
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # ---------- 공통 틀 ----------
    def _form(self, action: str, label: str, back: str, primary: bool = False, fields: dict | None = None) -> str:
        hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(str(v))}">'
                         for k, v in {"token": self.token, "next": back, **(fields or {})}.items())
        cls = ' class="primary"' if primary else ""
        return f'<form class="inline" method="post" action="{e(action)}">{hidden}<button{cls}>{e(label)}</button></form>'

    def _missed(self) -> list[Path] | None:
        watcher = getattr(self.bot, "watcher", None)
        return watcher.missed() if watcher else None

    def _page(self, title: str, active: str, body: str, msg: str = "") -> str:
        failures, pending = digest.attention(self.store)
        n = len(failures) + len(pending) + len(self._missed() or [])
        tabs = [("/", "건", "cases"), ("/todos", "할 일", "todos"),
                ("/attention", "조치 필요" + (f' <span class="badge warn">{n}</span>' if n else ""), "attention"),
                ("/import", "전사문 가져오기", "import"), ("/profile", "프로필", "profile")]
        nav = "".join(f'<a href="{h}" class="{"on" if k == active else ""}">{label}</a>' for h, label, k in tabs)
        flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · 두괄이</title>'
                f"<style>{CSS}</style></head><body><header><nav><span class=\"brand\">두괄이</span>{nav}</nav></header>"
                f"<main>{flash}{body}</main></body></html>")

    # ---------- 화면 ----------
    def page_cases(self, q: dict, msg: str) -> str:
        status = "closed" if q.get("status") == "closed" else "open"
        cases = self.store.list_cases(status)
        switch = ('<a href="/?status=closed">종결한 건 보기</a>' if status == "open" else '<a href="/">진행 중인 건 보기</a>')
        body = [f'<div class="row"><h1>{"진행 중인 건" if status == "open" else "종결한 건"} {len(cases)}개</h1>'
                f'<div class="small">{switch}</div></div>']
        if not cases:
            body.append('<div class="card muted">건이 없습니다. 슬랙에서 두괄이에게 메모를 보내면 여기에 쌓입니다.</div>')
        for c in cases:
            recs = [r for r in self.store.case_records(c["id"]) if r["data"]]
            concl = (c["data"] if c["data"] and not c["dirty"] and len(recs) > 1 else recs[-1]["data"] if recs else None)
            line = e(concl["conclusion"]) if concl else '<span class="muted">아직 정리 안 됨</span>'
            todo = f'<span class="badge">할 일 {c["n_todos"]}</span>' if c["n_todos"] else ""
            body.append(f'<div class="card"><div class="row"><div class="grow"><h3><a href="/cases/{c["id"]}">'
                        f'#{c["id"]} {e(c["title"])}</a>{todo}</h3><div>{line}</div></div>'
                        f'<div class="muted small">기록 {c["n_records"]}건 · {_when(c["updated_at"])}</div></div></div>')
        return self._page("건", "cases", "".join(body), msg)

    def _todo_row(self, t: dict, back: str, today: date, show_case: bool = True) -> str:
        tag = digest.due_tag(t["due"], today)
        tag = f'<span class="over">{e(tag)}</span>' if tag.startswith("⚠️") else f'<span class="muted">{e(tag)}</span>'
        who = "" if digest.is_mine(t) else f'<span class="muted">{e(t["owner"])}: </span>'
        case = f' <a class="small" href="/cases/{t["case_id"]}">#{t["case_id"]}</a>' if show_case else ""
        if t["done"]:
            return (f'<div class="todo"><div class="done">{who}{e(t["task"])}</div>'
                    f'{self._form(f"/todos/{t["id"]}/reopen", "되돌리기", back)}</div>')
        return (f'<div class="todo"><div>{who}{e(t["task"])} · {tag}{case}</div>'
                f'{self._form(f"/todos/{t["id"]}/done", "완료", back)}</div>')

    def page_todos(self, q: dict, msg: str) -> str:
        today = date.today()
        todos = self.store.open_todos()
        urgent = [t for t in todos if digest.is_urgent(t, today)]
        mine = [t for t in todos if digest.is_mine(t) and t not in urgent]
        others = [t for t in todos if not digest.is_mine(t)]
        body = [f"<h1>미완료 할 일 {len(todos)}건</h1>"]
        if not todos:
            body.append('<div class="card muted">미완료 할 일이 없습니다.</div>')
        for title, group in (("먼저 할 일 (기한이 지났거나 오늘·내일)", urgent), ("내 할 일", mine), ("받을 일 (다른 사람 담당)", others)):
            if group:
                body.append(f"<h2>{e(title)} · {len(group)}건</h2><div class=\"card\">"
                            + "".join(self._todo_row(t, "/todos", today) for t in group) + "</div>")
        body.append('<p class="muted small">완료한 항목은 그 건의 화면에서 되돌릴 수 있습니다.</p>')
        return self._page("할 일", "todos", "".join(body), msg)

    def page_case(self, cid: int, msg: str) -> str | None:
        case = self.store.get_case(cid)
        if not case:
            return None
        back, today = f"/cases/{cid}", date.today()
        recs = self.store.case_records(cid)
        done = [r for r in recs if r["data"]]
        state = '<span class="badge ok">진행 중</span>' if case["status"] == "open" else '<span class="badge">종결</span>'
        body = [f'<p class="small"><a href="/">← 건 목록</a></p><h1>#{cid} {e(case["title"])}{state}</h1>'
                f'<div class="muted small">기록 {len(recs)}건 · 최근 갱신 {_when(case["updated_at"])}</div>']
        if len(done) > 1:
            body.append("<h2>통합 정리</h2>")
            if case["data"] and not case["dirty"]:
                body.append(f'<div class="card">{data_html(case["data"])}</div>')
            else:
                body.append('<div class="card"><div class="muted">기록이 바뀐 뒤로 통합 정리를 다시 만들지 않았습니다.</div><p>'
                            + self._form(f"/cases/{cid}/summary", "통합 정리 만들기 (LLM 1회 호출)", back, True) + "</p></div>")
        todos = self.store.case_todos(cid)
        if todos:
            body.append(f'<h2>할 일</h2><div class="card">'
                        + "".join(self._todo_row(t, back, today, show_case=False) for t in todos) + "</div>")
        body.append("<h2>기록</h2>")
        for r in recs:
            head = f'<div class="muted small">{_when(r["created_at"])} · {e(_source(r))} · 기록 {r["id"]}</div>'
            if r["data"]:
                asks = "".join(f"<li>{e(x)}</li>" for x in r["data"]["ask_user"])
                asks = (f'<div class="notice small">❓ 아직 답하지 않은 질문<ul>{asks}</ul>'
                        "슬랙의 그 스레드에 답글로 알려주면 반영됩니다.</div>") if asks else ""
                inner = f'<h3>{e(r["data"]["title"])}</h3>{data_html(r["data"])}{asks}'
            else:
                inner = ('<div class="notice">아직 정리하지 않은 기록입니다. 원문은 이 PC에만 있습니다.</div>'
                         + self._form(f"/records/{r['id']}/summarize", "정리하기 (원문을 LLM으로 전송)", back, True))
            raw = f'<details><summary>원문 보기</summary><pre>{e(r["raw_text"])}</pre></details>'
            body.append(f'<div class="card">{head}{inner}{raw}</div>')
        return self._page(f"#{cid} {case['title']}", "cases", "".join(body), msg)

    def page_attention(self, q: dict, msg: str) -> str:
        store, back = self.store, "/attention"
        failures, pending = digest.attention(store)
        missed = self._missed()
        watch = "확인 안 함(감시 꺼짐)" if missed is None else f"{len(missed)}개"
        body = [f"<h1>조치 필요</h1><div class=\"muted small\">처리 실패 {len(failures)}건 · 정리 안 된 기록 {len(pending)}건 · "
                f"놓친 통화 녹음 {watch}. AI의 판단이 아니라 DB와 녹음 폴더를 조회한 결과입니다.</div>"]
        if not failures and not pending and not missed:
            body.append('<div class="card">막힌 건이 없습니다. 해야 할 업무는 <a href="/todos">할 일</a>에서 봅니다.</div>')
        if failures:
            body.append(f"<h2>처리 실패 {len(failures)}건</h2>")
        for f in failures:
            rec = store.get_record(f["record_id"]) if f.get("record_id") else None
            if f["stage"] == "call":
                name = e(Path(f["target"]).name)
            elif rec:
                name = f'<a href="/cases/{rec["case_id"]}">기록 {rec["id"]}</a>'
            else:
                name = e(f["target"])
            buttons = self._form(f"/failures/{f['id']}/retry", "다시 시도", back, True)
            if f["stage"] in STT_STAGES and cfg.stt_provider == "api" and local_available():
                buttons += " " + self._form(f"/failures/{f['id']}/retry", "로컬 전사로 다시 시도", back, fields={"provider": "local"})
            buttons += " " + self._form(f"/failures/{f['id']}/dismiss", "넘기기", back)
            kept = " · 원문은 저장돼 있습니다" if f["stage"] == "extract" else ""
            body.append(f'<div class="card"><h3>{e(STAGE_LABEL.get(f["stage"], f["stage"]))} 실패 · {name}</h3>'
                        f'<div class="muted small">{_when(f["created_at"])} · 시도 {f["attempts"]}회{kept}</div>'
                        f'<pre>{e(f["error"])}</pre>{buttons}</div>')
        if pending:
            body.append(f"<h2>정리 안 된 기록 {len(pending)}건</h2>")
        for r in pending:
            name = f' · {e(Path(r["audio_path"]).name)}' if r.get("audio_path") else ""
            body.append(f'<div class="card"><div class="row"><div class="grow"><h3><a href="/cases/{r["case_id"]}">'
                        f'기록 {r["id"]}</a></h3><div class="muted small">{_when(r["created_at"])} · {e(_source(r))}{name}'
                        f'</div></div><div>{self._form(f"/records/{r["id"]}/summarize", "정리하기 (원문을 LLM으로 전송)", back, True)}'
                        "</div></div></div>")
        if missed:
            names = "".join(f"<li>{e(p.name)}</li>" for p in missed[:20])
            more = f'<div class="muted small">… 외 {len(missed) - 20}개</div>' if len(missed) > 20 else ""
            body.append(f"<h2>놓친 통화 녹음 {len(missed)}개</h2><div class=\"card\"><div class=\"muted small\">"
                        f"꺼져 있던 동안 생겼거나 늦게 동기화된 파일입니다.</div><ul>{names}</ul>{more}"
                        + self._form("/missed/process", "모두 처리", back, True) + " "
                        + self._form("/missed/skip", "건너뛰기", back) + "</div>")
        open_ids = {c["id"] for c in store.open_cases()}
        asks = [(r, x) for r in store.records_since(QUESTION_DAYS)
                if r["data"] and r["case_id"] in open_ids for x in r["data"]["ask_user"]]
        if asks:
            body.append(f"<h2>아직 답하지 않은 질문 {len(asks)}개</h2><div class=\"card\">"
                        + "".join(f'<div class="todo"><div>❓ {e(x)} <a class="small" href="/cases/{r["case_id"]}">'
                                  f'#{r["case_id"]} {e(r["data"]["title"])}</a></div></div>' for r, x in asks)
                        + f'</div><p class="muted small">최근 {QUESTION_DAYS}일 기록 기준입니다. 슬랙의 그 스레드에 답글로 '
                        "알려주면 반영되고, 답하지 않아도 정리는 그대로 쓸 수 있습니다.</p>")
        return self._page("조치 필요", "attention", "".join(body), msg)

    def page_import(self, q: dict, msg: str) -> str:
        gate = ("저장한 뒤 바로 LLM으로 정리합니다 (SEND_CALLS_TO_LLM=true)." if cfg.send_calls_to_llm else
                "이 PC에 저장만 합니다. 정리하려면 그 뒤에 '정리하기'를 눌러야 합니다 (SEND_CALLS_TO_LLM=false).")
        hidden = f'<input type="hidden" name="token" value="{e(self.token)}">'
        body = (f"<h1>전사문 가져오기</h1><p class=\"muted\">다른 도구에서 이미 전사한 통화·회의 내용을 붙여 넣습니다. "
                f"자막 형식(VTT·SRT)이면 번호와 시간은 알아서 뺍니다.</p><div class=\"notice small\">{e(gate)}</div>"
                f'<form method="post" action="/import" class="card">{hidden}'
                '<div class="label">이름 (선택)</div><input type="text" name="name" placeholder="예: A사 이대리 통화 11/17">'
                '<div class="label">덧붙일 메모 (선택)</div><input type="text" name="note" placeholder="예: 가견적 요청 건 후속">'
                '<div class="label">전사문</div><textarea name="text" required></textarea>'
                '<p><button class="primary">가져오기</button></p></form>'
                '<p class="muted small">파일로 가지고 있다면 슬랙 DM에 .txt / .vtt / .srt 파일을 올려도 됩니다.</p>')
        return self._page("전사문 가져오기", "import", body, msg)

    def page_profile(self, q: dict, msg: str) -> str:
        p = profile.load()
        hint = "" if p.exists else ('<div class="notice">프로필 파일이 없어 기본값으로 정리합니다. 만들려면 '
                                    "<code>python -m dugwali.cli profile --init</code>을 실행하세요.</div>")
        body = (f"<h1>프로필</h1><p class=\"muted\">내 메모를 어떤 기준과 양식으로 정리할지 적는 파일입니다. "
                f"메모장으로 고치면 다음 정리부터 반영됩니다.</p>{hint}"
                f"<h2>두괄이가 읽은 결과</h2><pre>{e(profile.describe(p))}</pre>"
                f"<h2>LLM에 함께 보내는 내용</h2><pre>{e(p.prompt_text)}</pre>")
        return self._page("프로필", "profile", body, msg)

    # ---------- 쓰기 ----------
    def _background(self, fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("대시보드에서 시작한 작업 실패")
        threading.Thread(target=run, daemon=True, name="dashboard-job").start()

    def _show(self, rec: dict) -> None:
        if self.bot:
            try:
                self.bot.show_record(rec)
            except Exception:
                log.exception("슬랙 반영 실패(대시보드에는 반영됨)")

    def act(self, path: str, form: dict) -> tuple[str, str]:
        """POST 한 건을 처리하고 (돌아갈 주소, 알림 문구)를 돌려준다."""
        store, parts = self.store, [x for x in path.split("/") if x]
        back = form.get("next", "/")
        back = back if back.startswith("/") and not back.startswith("//") else "/"

        if parts == ["import"]:
            text = transcript.clean(form.get("text", ""))
            if not text:
                return "/import", "전사문이 비어 있습니다."
            try:
                rec = pipeline.import_transcript(store, text, form.get("name", "").strip() or None, form.get("note", ""))
            except pipeline.ProcessError as err:
                return f"/cases/{err.record['case_id']}", f"원문은 저장했지만 정리에 실패했습니다: {err}"
            if rec["data"]:
                self._show(rec)
                return f"/cases/{rec['case_id']}", "전사문을 가져와 정리했습니다."
            return f"/cases/{rec['case_id']}", "전사문을 이 PC에 저장했습니다. 정리하려면 '정리하기'를 누르세요."

        if len(parts) == 3 and parts[0] == "todos" and parts[1].isdigit():
            if parts[2] == "done":
                store.complete_todo(int(parts[1]))
                return back, "완료 처리했습니다."
            if parts[2] == "reopen":
                store.reopen_todo(int(parts[1]))
                return back, "다시 미완료로 돌렸습니다."

        if len(parts) == 3 and parts[0] == "records" and parts[1].isdigit() and parts[2] == "summarize":
            if not store.get_record(int(parts[1])):
                return back, "그 기록이 없습니다."
            try:
                rec = pipeline.summarize(store, int(parts[1]))
            except pipeline.ProcessError as err:
                return back, f"정리에 실패했습니다: {err}"
            self._show(rec)
            return f"/cases/{rec['case_id']}", "정리했습니다."

        if len(parts) == 3 and parts[0] == "cases" and parts[1].isdigit() and parts[2] == "summary":
            try:
                ok = pipeline.case_summary(store, int(parts[1]))
            except pipeline.LLMError as err:
                return back, f"통합 정리에 실패했습니다: {err}"
            return back, "통합 정리를 만들었습니다." if ok else "정리할 기록이 없습니다."

        if len(parts) == 3 and parts[0] == "failures" and parts[1].isdigit():
            f = store.get_failure(int(parts[1]))
            if not f or f["resolved_at"]:
                return back, "이미 처리된 항목입니다."
            if parts[2] == "dismiss":
                return back, f"넘겼습니다. {pipeline.dismiss_failure(store, f['id'])}".strip()
            if parts[2] == "retry":
                provider = "local" if form.get("provider") == "local" else None
                if self.bot:
                    if f["stage"] in STT_STAGES:  # 전사는 오래 걸린다
                        self._background(self.bot.retry_failure, f, provider)
                        return back, "다시 시도를 시작했습니다. 끝나면 슬랙에 올라오고 이 목록에서 빠집니다."
                    left = self.bot.retry_failure(f, provider)
                    return back, f"다시 실패했습니다: {left['error']}" if left else "다시 처리했습니다."
                if f["stage"] != "extract":
                    return back, "이 항목은 슬랙 봇과 함께 실행 중일 때만 다시 시도할 수 있습니다."
                if not store.get_record(f["record_id"]):
                    store.resolve_failures(fid=f["id"])
                    return back, "그 기록은 이미 지워졌습니다."
                try:
                    pipeline.summarize(store, f["record_id"])
                except pipeline.ProcessError as err:
                    return back, f"다시 실패했습니다: {err}"
                return back, "다시 처리했습니다."

        if parts in (["missed", "process"], ["missed", "skip"]):
            watcher = getattr(self.bot, "watcher", None)
            if not watcher:
                return back, "통화 녹음 감시가 꺼져 있습니다."
            if parts[1] == "skip":
                return back, f"놓친 통화 녹음 {watcher.skip_missed()}개를 건너뛰었습니다. 파일은 그대로 있습니다."
            paths = watcher.missed()
            for p in paths:
                store.mark_seen(str(p))
            self._background(lambda: [self.bot.process_call(p, True, None) for p in paths])
            return back, f"놓친 통화 녹음 {len(paths)}개를 처리합니다. 끝나는 대로 슬랙에 하나씩 올라옵니다."

        return back, "알 수 없는 요청입니다."

    # ---------- HTTP ----------
    def _handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "dugwali"

            def log_message(self, fmt, *args):  # 접근 기록은 남기지 않는다(주소에 건 번호가 들어간다)
                pass

            def _send(self, status: int, body: str = "", location: str | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)

            def _local(self) -> bool:
                """이 PC의 브라우저가 이 주소로 직접 연 요청만 받는다(DNS 리바인딩 차단)."""
                return self.headers.get("Host", "") in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}")

            def do_GET(self):
                if not self._local():
                    return self._send(403, "이 PC에서만 열 수 있습니다.")
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                msg, parts = q.get("msg", ""), [x for x in url.path.split("/") if x]
                try:
                    if not parts:
                        page = dash.page_cases(q, msg)
                    elif parts[0] == "cases" and len(parts) == 2 and parts[1].isdigit():
                        page = dash.page_case(int(parts[1]), msg)
                    elif len(parts) == 1 and parts[0] in ("todos", "attention", "import", "profile"):
                        page = getattr(dash, f"page_{parts[0]}")(q, msg)
                    else:
                        page = None
                except Exception:
                    log.exception("대시보드 화면 오류: %s", url.path)
                    return self._send(500, dash._page("오류", "", '<div class="notice">화면을 그리다 오류가 났습니다. '
                                                      "로그(data/dugwali.log)를 확인하세요.</div>"))
                if page is None:
                    return self._send(404, dash._page("없음", "", '<div class="card">그런 화면이 없습니다. '
                                                      '<a href="/">건 목록으로</a></div>'))
                self._send(200, page)

            def do_POST(self):
                if not self._local():
                    return self._send(403, "이 PC에서만 열 수 있습니다.")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, "내용이 너무 큽니다.")
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                                                     keep_blank_values=True).items()}
                if not hmac.compare_digest(form.get("token", ""), dash.token):
                    return self._send(403, dash._page("거절", "", '<div class="notice">이 화면이 오래됐습니다(두괄이가 '
                                                      '다시 켜졌을 수 있습니다). <a href="/">새로 열어</a> 다시 해 주세요.</div>'))
                try:
                    back, msg = dash.act(urlsplit(self.path).path, form)
                except Exception as err:
                    log.exception("대시보드 요청 처리 오류: %s", self.path)
                    back, msg = "/", f"처리하다 오류가 났습니다: {err}"
                sep = "&" if "?" in back else "?"
                self._send(303, location=f"{back}{sep}msg={quote(msg)}" if msg else back)

        return Handler
