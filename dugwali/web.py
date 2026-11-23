"""로컬 대시보드. 봇과 같은 프로세스가 127.0.0.1에만 띄운다. 로그인은 없다.

- 표준 라이브러리만 쓴다(설치할 것이 늘지 않게). 화면은 서버가 그린 HTML이고 스크립트 없이 동작한다.
- 127.0.0.1에만 바인딩한다. 이 PC의 브라우저가 직접 연 요청은 로그인 없이 받는다(Host 헤더로 확인).
- 다른 기기에서는 `DASHBOARD_URL`을 설정했을 때만 열린다. 그 PC까지 닿는 길은 Tailscale serve 같은
  VPN 프록시가 맡고, 누구인지는 슬랙으로 확인한다: 봇이 소유자의 DM에만 보내는 일회용 링크로 로그인하면
  그 브라우저에 세션 쿠키를 준다. 프록시를 거친 요청은 세션이 없으면 아무 화면도 보여주지 않는다.
- 모든 쓰기(POST)는 화면 토큰을 확인한다. 다른 웹사이트가 몰래 요청을 보내지 못한다. 토큰은 DB에 두어
  두괄이를 다시 켜도 열려 있던 화면이 그대로 동작한다.
- 쓰기는 슬랙 버튼과 같은 내부 함수(`pipeline.py`)를 거친다. 고친 내용은 슬랙 초안에도 반영한다.
- 삭제는 휴지통으로 옮기는 것이고, 화면을 여는 것만으로는 LLM을 부르지 않는다.

슬랙 봇 없이도 뜬다(`python -m dugwali.cli dashboard`). 그때는 슬랙이 필요한 동작만 빠진다.
"""
import hashlib
import hmac
import logging
import secrets
import threading
import re
import time
from datetime import date, datetime
from html import escape as e
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import digest, pipeline, profile, views
from .config import cfg
from .ingest import transcript
from .labels import SOURCE_LABEL
from .store import TRASH_DAYS, UNTITLED, Store
from .stt.transcribe import local_available

log = logging.getLogger(__name__)
QUESTION_DAYS = 14       # 이 기간 안의 기록에서 아직 답하지 않은 질문을 보여준다
MAX_BODY = 5_000_000     # 붙여 넣는 전사문 크기 상한(바이트)
STAGE_LABEL = {"extract": "정리", "call": "통화 녹음 처리", "voice": "음성 메모 전사", "digest": "마감 정리 전송"}
STT_STAGES = ("call", "voice")
SESSION_DAYS = 30        # 로그인한 브라우저를 기억하는 기간
LOGIN_CODE_MINUTES = 10  # 슬랙으로 보내는 일회용 접속 링크의 유효 시간
LOGIN_REQUEST_GAP = 30   # 로그인 화면에서 링크를 다시 보내 달라고 할 수 있는 최소 간격(초)
LOGIN_REQUEST_MAX = 5    # 그 화면에서 LOGIN_CODE_MINUTES분 동안 보낼 수 있는 링크 수
COOKIE = "dugwali_session"
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Tailscale-User-Login")
_UNDO = re.compile(r"^(case|record|todo):\d+$")


def _safe_next(path: str) -> str:
    """로그인 뒤 돌아갈 곳. 이 대시보드 안의 경로만 허용한다(다른 사이트로 보내는 링크를 막는다)."""
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path or path.startswith("/login"):
        return "/"
    return path


class Flash(str):
    """알림 문구. undo("record:5" 등)가 있으면 되돌리기 버튼을 함께 보여준다."""
    undo = ""

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
.flash{background:var(--ok-bg);color:var(--ok);border-radius:8px;padding:10px 14px;margin:0 0 14px;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
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
textarea.short{min-height:74px;font-family:inherit;font-size:15px}
select{font:inherit;padding:5px 8px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:10px}
.fields{display:flex;gap:8px;flex-wrap:wrap}.fields input{flex:1;min-width:120px}
button.danger{color:var(--danger)}a.btn{display:inline-block;padding:5px 12px;border-radius:7px;border:1px solid var(--line);color:var(--text)}
a.btn.danger{color:var(--danger)}
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
        self.token = store.get_state("dashboard_token") or secrets.token_urlsafe(24)
        store.set_state("dashboard_token", self.token)
        self._server: ThreadingHTTPServer | None = None
        self._codes: dict[str, float] = {}   # 일회용 접속 코드 → 만료 시각
        self._requests: list[float] = []     # 로그인 화면에서 링크를 보낸 시각
        self._codes_lock = threading.Lock()
        self._ctx = threading.local()        # 요청마다: 로그인한 세션(이 PC에서 직접 열었으면 None)

    # ---------- 다른 기기에서 접속 ----------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def login_link(self, next_path: str = "/") -> str | None:
        """슬랙으로 보낼 일회용 접속 링크. LOGIN_CODE_MINUTES분 안에 한 번만 쓸 수 있다.

        next_path가 있으면 로그인한 뒤 그 화면으로 간다(슬랙 알림의 건 링크를 열었다가 로그인한 경우)."""
        if not cfg.dashboard_url:
            return None
        code, now = secrets.token_urlsafe(32), time.time()
        with self._codes_lock:
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            self._codes[code] = now + LOGIN_CODE_MINUTES * 60
        nxt = _safe_next(next_path)
        return f"{cfg.dashboard_url}/login?code={code}" + (f"&next={quote(nxt)}" if nxt != "/" else "")

    def login_message(self, next_path: str = "/") -> str | None:
        """슬랙 DM으로 보내는 로그인 안내. 슬랙 `대시보드` 명령과 로그인 화면의 버튼이 함께 쓴다."""
        link = self.login_link(next_path)
        if not link:
            return None
        return (f"*<{link}|이 링크를 눌러 대시보드에 로그인>* ({LOGIN_CODE_MINUTES}분 안에 한 번만 쓸 수 있습니다)\n"
                "Tailscale이 켜진 기기에서 여세요. 슬랙 앱 안에서 열렸다면 `브라우저에서 열기`로 연 뒤 로그인해야 "
                f"로그인이 {SESSION_DAYS}일 동안 유지됩니다.\n"
                "로그인한 브라우저는 대시보드의 `접속 관리`에서 보고 끊을 수 있고, 한꺼번에 끊으려면 "
                "`대시보드 로그아웃`을 보내세요.")

    def request_login(self, next_path: str) -> str:
        """로그인 화면의 '슬랙으로 링크 받기'. 링크는 소유자 DM으로만 간다. 결과 상태를 돌려준다."""
        if not (self.bot and hasattr(self.bot, "post_dm")):
            return "noslack"
        now = time.time()
        with self._codes_lock:
            self._requests = [t for t in self._requests if t > now - LOGIN_CODE_MINUTES * 60]
            if (self._requests and self._requests[-1] > now - LOGIN_REQUEST_GAP) or len(self._requests) >= LOGIN_REQUEST_MAX:
                return "busy"
            self._requests.append(now)
        try:
            self.bot.post_dm(self.login_message(next_path))
        except Exception:
            log.exception("슬랙으로 로그인 링크를 보내지 못했습니다")
            return "error"
        return "sent"

    def _redeem(self, code: str, agent: str) -> str | None:
        """접속 코드를 쓰고 세션 토큰을 돌려준다. 틀렸거나 이미 썼거나 시간이 지났으면 None."""
        with self._codes_lock:
            expires = self._codes.pop(code, 0)
        if expires < time.time():
            return None
        token = secrets.token_urlsafe(32)
        self.store.add_session(self._hash(token), agent, SESSION_DAYS)
        return token

    def page_login(self, state: str, code: str = "", next_path: str = "/") -> str:
        """다른 기기의 로그인 화면.

        state: new(로그인 안 한 브라우저), code(슬랙 링크로 옴), failed(링크를 이미 썼거나 시간 지남),
        sent(슬랙으로 링크를 보냄), busy(방금 보냄), noslack(슬랙 없이 띄움), error(보내기 실패)."""
        nxt = e(_safe_next(next_path))
        ask = ('<form method="post" action="/login/request">'
               f'<input type="hidden" name="next" value="{nxt}">'
               '<button class="primary">슬랙으로 로그인 링크 받기</button></form>')
        ask_again = ask.replace('class="primary"', "")
        if state == "code":
            inner = ("<p>이 브라우저에서 두괄이 대시보드를 엽니다. 한 번 로그인하면 "
                     f"{SESSION_DAYS}일 동안 다시 묻지 않습니다. 공용 PC에서는 쓰지 마세요.</p>"
                     f'<form method="post" action="/login"><input type="hidden" name="code" value="{e(code)}">'
                     f'<input type="hidden" name="next" value="{nxt}">'
                     '<button class="primary">로그인</button></form>'
                     '<p class="muted small">슬랙 앱 안에서 열렸다면 메뉴의 <b>브라우저에서 열기</b>로 연 뒤 누르세요. '
                     "앱 안 브라우저는 로그인을 기억하지 못할 수 있습니다.</p>")
        elif state == "failed":
            inner = ('<div class="notice">이 링크는 이미 썼거나 '
                     f"{LOGIN_CODE_MINUTES}분이 지났습니다.</div><p>새 링크를 받아 다시 로그인하세요.</p>{ask}")
        elif state == "sent":
            inner = ("<p><b>슬랙 DM을 확인하세요.</b> 두괄이가 로그인 링크를 보냈습니다. "
                     f"{LOGIN_CODE_MINUTES}분 안에 이 기기에서 여세요.</p>"
                     f'<p class="muted small">안 왔으면 {LOGIN_REQUEST_GAP}초 뒤에 다시 받을 수 있습니다.</p>{ask_again}')
        elif state == "busy":
            inner = ('<div class="notice">방금 링크를 보냈습니다. 슬랙 DM을 먼저 확인하고, '
                     "안 왔으면 조금 뒤에 다시 받으세요.</div>" + ask_again)
        elif state == "noslack":
            inner = ('<div class="notice">슬랙 봇이 연결되지 않아 링크를 보낼 수 없습니다.</div>'
                     "<p>두괄이를 슬랙과 함께 켠 뒤(<code>run.bat</code>) 다시 시도하세요.</p>")
        elif state == "error":
            inner = ('<div class="notice">슬랙으로 링크를 보내지 못했습니다. 두괄이 PC의 로그'
                     "(data/dugwali.log)를 확인하세요.</div>" + ask_again)
        else:
            inner = ("<p>이 브라우저는 아직 로그인하지 않았습니다. 버튼을 누르면 두괄이가 <b>내 슬랙 DM</b>으로 "
                     f"로그인 링크를 보냅니다.</p>{ask}"
                     '<p class="muted small">슬랙에서 두괄이에게 <b>대시보드</b>를 보내도 같은 링크를 받습니다.</p>')
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>로그인 · 두괄이</title><style>{CSS}</style></head>'
                f'<body><main><h1>두괄이 대시보드</h1><div class="card">{inner}</div></main></body></html>')

    def page_sessions(self, q: dict, msg: str) -> str:
        mine = getattr(self._ctx, "session", None)
        rows = self.store.list_sessions()
        body = ["<h1>접속 관리</h1>"]
        if not cfg.dashboard_url:
            body.append('<div class="card">다른 기기에서 여는 기능이 꺼져 있습니다. 지금은 이 PC에서만 열립니다. '
                        "켜려면 README의 \"다른 기기에서 열기\"를 보세요.</div>")
        else:
            body.append(f'<p class="muted">다른 기기에서 여는 주소: {e(cfg.dashboard_url)} · 새 기기는 슬랙에서 '
                        f"<b>대시보드</b>를 보내 받은 링크로 로그인합니다. 로그인은 {SESSION_DAYS}일 동안 유지됩니다.</p>")
        if rows:
            items = "".join(
                f'<div class="todo"><div>{e(r["agent"] or "알 수 없는 브라우저")}'
                + ('<span class="badge ok">지금 이 브라우저</span>' if mine and mine["id"] == r["id"] else "")
                + f'<div class="muted small">로그인 {_when(r["created_at"])} · 마지막 접속 {_when(r["last_seen"])} · '
                f'{_when(r["expires_at"])[:10]}까지</div></div>'
                f'{self._form(f"/sessions/{r["id"]}/logout", "로그아웃", "/sessions")}</div>' for r in rows)
            body.append(f'<h2>로그인한 브라우저 {len(rows)}개</h2><div class="card">{items}</div>'
                        + self._form("/sessions/logout-all", "모두 로그아웃", "/sessions", cls="danger"))
        elif cfg.dashboard_url:
            body.append('<div class="card muted">로그인한 브라우저가 없습니다.</div>')
        body.append('<p class="muted small">이 PC에서 직접 연 화면은 로그인 없이 열리고 이 목록에 나오지 않습니다.</p>')
        return self._page("접속 관리", "sessions", "".join(body), msg)

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
    def _hidden(self, back: str = "", fields: dict | None = None) -> str:
        return "".join(f'<input type="hidden" name="{e(k)}" value="{e(str(v))}">'
                       for k, v in {"token": self.token, "next": back, **(fields or {})}.items())

    def _form(self, action: str, label: str, back: str, primary: bool = False, fields: dict | None = None,
              cls: str = "") -> str:
        cls = "primary" if primary else cls
        cls = f' class="{cls}"' if cls else ""
        return (f'<form class="inline" method="post" action="{e(action)}">{self._hidden(back, fields)}'
                f"<button{cls}>{e(label)}</button></form>")

    def _missed(self) -> list[Path] | None:
        watcher = getattr(self.bot, "watcher", None)
        return watcher.missed() if watcher else None

    def _page(self, title: str, active: str, body: str, msg: str = "") -> str:
        failures, pending = digest.attention(self.store)
        n = len(failures) + len(pending) + len(self._missed() or [])
        tabs = [("/", "건", "cases"), ("/todos", "할 일", "todos"),
                ("/attention", "조치 필요" + (f' <span class="badge warn">{n}</span>' if n else ""), "attention"),
                ("/import", "전사문 가져오기", "import"), ("/profile", "프로필", "profile"), ("/trash", "휴지통", "trash")]
        if cfg.dashboard_url:
            tabs.append(("/sessions", "접속 관리", "sessions"))
        nav = "".join(f'<a href="{h}" class="{"on" if k == active else ""}">{label}</a>' for h, label, k in tabs)
        undo = getattr(msg, "undo", "")
        if _UNDO.match(undo):
            kind, target = undo.split(":")
            undo = self._form("/trash/restore", "되돌리기", "/", fields={"kind": kind, "id": target})
        else:
            undo = ""
        flash = f'<div class="flash"><span>{e(msg)}</span>{undo}</div>' if msg else ""
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
        act = f"/todos/{t['id']}"
        if t["done"]:
            return (f'<div class="todo"><div class="done">{who}{e(t["task"])}</div>'
                    f'{self._form(f"{act}/reopen", "되돌리기", back)}</div>')
        mark = '<span class="badge">직접 고침</span>' if t["manual"] else ""
        if t["suspect"]:
            return (f'<div class="todo"><div>{who}{e(t["task"])} · {tag}{case}<span class="badge warn">완료 추정</span>'
                    '<div class="muted small">다시 정리했더니 이 할 일이 더는 나오지 않았습니다. 끝난 일인지 확인해 주세요.</div></div>'
                    f'<div>{self._form(f"{act}/done", "완료 맞음", back, True)} {self._form(f"{act}/keep", "아직 남음", back)}'
                    "</div></div>")
        return (f'<div class="todo"><div>{who}{e(t["task"])} · {tag}{case}{mark}</div><div>'
                f'{self._form(f"{act}/done", "완료", back)} <a class="btn" href="{act}/edit?next={quote(back)}">수정</a>'
                "</div></div>")

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
        body.append('<p class="muted small">완료한 항목은 그 건의 화면에서 되돌릴 수 있습니다. '
                    "할 일을 새로 더하려면 그 건을 여세요.</p>")
        return self._page("할 일", "todos", "".join(body), msg)

    def _manual_badge(self, fields: list[str]) -> str:
        if not fields:
            return ""
        names = ", ".join(views.FIELD_LABEL.get(f, f) for f in fields)
        return f'<span class="badge" title="다시 정리해도 이 값은 바뀌지 않습니다">직접 고침: {e(names)}</span>'

    def page_case(self, cid: int, msg: str) -> str | None:
        store, case = self.store, self.store.get_case(cid)
        if not case:
            return None
        if case["deleted_at"]:
            body = (f'<h1>#{cid} {e(case["title"])}<span class="badge">휴지통</span></h1><div class="card">이 건은 휴지통에 '
                    f'있습니다. {self._form("/trash/restore", "되살리기", "/", True, {"kind": "case", "id": cid})}</div>')
            return self._page(f"#{cid}", "trash", body, msg)
        back, today = f"/cases/{cid}", date.today()
        recs = store.case_records(cid)
        done = [r for r in recs if r["data"]]
        is_open = case["status"] == "open"
        state = '<span class="badge ok">진행 중</span>' if is_open else '<span class="badge">종결</span>'
        toggle = (self._form(f"/cases/{cid}/close", "종결", back) if is_open
                  else self._form(f"/cases/{cid}/reopen", "다시 열기", back, True))
        body = [f'<p class="small"><a href="/">← 건 목록</a></p><div class="row"><h1>#{cid} {e(case["title"])}{state}</h1>'
                f'<div class="actions">{toggle} <a class="btn danger" href="/cases/{cid}/delete">삭제</a></div></div>'
                f'<div class="muted small">기록 {len(recs)}건 · 최근 갱신 {_when(case["updated_at"])}'
                + ("" if is_open else " · 종결하면서 완료 처리된 할 일은 다시 열면 되돌아옵니다") + "</div>"]
        if len(done) > 1:
            body.append("<h2>통합 정리</h2>")
            if case["data"] and not case["dirty"]:
                body.append(f'<div class="card">{data_html(views.case_data(store, cid, case["data"]))}'
                            f'<div class="actions"><a class="btn" href="/cases/{cid}/edit">수정</a>'
                            f'{self._manual_badge(case["manual"])}</div></div>')
            else:
                body.append('<div class="card"><div class="muted">기록이 바뀐 뒤로 통합 정리를 다시 만들지 않았습니다.</div><p>'
                            + self._form(f"/cases/{cid}/summary", "통합 정리 만들기 (LLM 1회 호출)", back, True) + "</p></div>")
        todos = store.case_todos(cid)
        body.append('<h2>할 일</h2><div class="card">'
                    + ("".join(self._todo_row(t, back, today, show_case=False) for t in todos)
                       or '<div class="muted">할 일이 없습니다.</div>')
                    + (f'<form method="post" action="/cases/{cid}/todos" class="actions fields">{self._hidden(back)}'
                       '<input type="text" name="task" placeholder="할 일 더하기" required>'
                       '<input type="text" name="owner" placeholder="담당 (비우면 나)">'
                       '<input type="text" name="due" placeholder="기한 (예: 2026-11-20)">'
                       "<button>더하기</button></form>" if done else "") + "</div>")
        body.append("<h2>기록</h2>")
        others = [c for c in store.open_cases() if c["id"] != cid]
        for r in recs:
            head = f'<div class="muted small">{_when(r["created_at"])} · {e(_source(r))} · 기록 {r["id"]}</div>'
            if r["data"]:
                d = views.record_data(store, r)
                asks = "".join(f"<li>{e(x)}</li>" for x in d["ask_user"])
                asks = (f'<div class="notice small">❓ 아직 답하지 않은 질문<ul>{asks}</ul>'
                        "슬랙의 그 스레드에 답글로 알려주면 반영됩니다.</div>") if asks else ""
                inner = f'<h3>{e(d["title"])}</h3>{data_html(d)}{asks}'
                edit = f'<a class="btn" href="/records/{r["id"]}/edit">수정</a>'
            else:
                inner = ('<div class="notice">아직 정리하지 않은 기록입니다. 원문은 이 PC에만 있습니다.</div>'
                         + self._form(f"/records/{r['id']}/summarize", "정리하기 (원문을 LLM으로 전송)", back, True))
                edit = ""
            options = ('<option value="new">새 건으로 떼어내기</option>' if len(recs) > 1 else "") + "".join(
                f'<option value="{c["id"]}">#{c["id"]} {e(c["title"])}</option>' for c in others[:100])
            move = (f'<form class="inline" method="post" action="/records/{r["id"]}/move">{self._hidden(back)}'
                    f'<select name="target">{options}</select> <button>옮기기</button></form>') if options else ""
            raw = f'<details><summary>원문 보기</summary><pre>{e(r["raw_text"])}</pre></details>'
            body.append(f'<div class="card">{head}{inner}<div class="actions">{edit}{move}'
                        f'<a class="btn danger" href="/records/{r["id"]}/delete">삭제</a>'
                        f'{self._manual_badge(r["manual"])}</div>{raw}</div>')
        if done:
            body.append(f'<h2>이 건에 메모 더하기</h2><form method="post" action="/cases/{cid}/note" class="card">'
                        f'{self._hidden(back)}<textarea class="short" name="text" required '
                        'placeholder="후속 내용을 적으면 새 기록으로 정리해 이 건에 붙입니다"></textarea>'
                        '<div class="actions"><button class="primary">더하기 (LLM으로 전송)</button></div></form>')
        return self._page(f"#{cid} {case['title']}", "cases", "".join(body), msg)

    def _edit_form(self, action: str, d: dict, back: str) -> str:
        rows = []
        for f in views.EDITABLE:
            label = views.FIELD_LABEL[f]
            if f in views.LIST_FIELDS:
                rows.append(f'<div class="label">{label} <span class="muted small">(한 줄에 하나)</span></div>'
                            f'<textarea class="short" name="{f}">{e(chr(10).join(d.get(f) or []))}</textarea>')
            elif f == "conclusion":
                rows.append(f'<div class="label">{label}</div><textarea class="short" name="{f}" required>'
                            f'{e(d.get(f) or "")}</textarea>')
            else:
                req = " required" if f == "title" else ""
                rows.append(f'<div class="label">{label}</div><input type="text" name="{f}" value="{e(d.get(f) or "")}"{req}>')
        return (f'<form method="post" action="{e(action)}" class="card">{self._hidden(back)}{"".join(rows)}'
                f'<div class="actions"><button class="primary">저장</button> <a class="btn" href="{e(back)}">취소</a></div></form>'
                '<p class="muted small">고친 항목은 "직접 고침"으로 표시되고, 이후 다시 정리해도 바뀌지 않습니다. '
                "할 일은 건 화면의 할 일 목록에서 고칩니다.</p>")

    def page_edit(self, kind: str, target_id: int, msg: str) -> str | None:
        """기록(record) 또는 건의 통합 정리(case)를 고치는 화면."""
        if kind == "record":
            rec = self.store.get_record(target_id)
            if not rec or not rec["data"] or rec["deleted_at"]:
                return None
            back, d, manual, name = f"/cases/{rec['case_id']}", rec["data"], rec["manual"], f"기록 {target_id}"
        else:
            case = self.store.get_case(target_id)
            if not case or not case["data"] or case["deleted_at"]:
                return None
            back, d, manual, name = f"/cases/{target_id}", case["data"], case["manual"], f"#{target_id} 통합 정리"
        release = ""
        if manual:
            names = ", ".join(views.FIELD_LABEL.get(f, f) for f in manual)
            release = (f'<div class="card"><div>직접 고친 항목: <b>{e(names)}</b></div><div class="muted small">표시를 지우면 '
                       "다음에 다시 정리할 때 LLM이 뽑은 값으로 바뀔 수 있습니다. 지금 값은 그대로 둡니다.</div>"
                       f'<div class="actions">{self._form(f"/{kind}s/{target_id}/release", "직접 고침 표시 지우기", back)}</div></div>')
        body = (f'<p class="small"><a href="{back}">← 건으로</a></p><h1>{e(name)} 수정</h1>'
                + self._edit_form(f"/{kind}s/{target_id}/edit", d, back) + release)
        return self._page(f"{name} 수정", "cases", body, msg)

    def page_todo_edit(self, tid: int, q: dict, msg: str) -> str | None:
        t = self.store.get_todo(tid)
        if not t or t["deleted_at"]:
            return None
        back = q.get("next", "") if q.get("next", "").startswith("/") and not q.get("next", "").startswith("//") else ""
        back = back or f"/cases/{t['case_id']}"
        body = (f'<p class="small"><a href="{e(back)}">← 돌아가기</a></p><h1>할 일 수정</h1>'
                f'<form method="post" action="/todos/{tid}/edit" class="card">{self._hidden(back)}'
                f'<div class="label">할 일</div><input type="text" name="task" value="{e(t["task"])}" required>'
                f'<div class="label">담당 <span class="muted small">(비우면 나)</span></div>'
                f'<input type="text" name="owner" value="{e(t["owner"] or "")}">'
                f'<div class="label">기한 <span class="muted small">(YYYY-MM-DD로 적으면 기한이 지났는지 계산합니다)</span></div>'
                f'<input type="text" name="due" value="{e(t["due"] or "")}">'
                f'<div class="actions"><button class="primary">저장</button> <a class="btn" href="{e(back)}">취소</a></div></form>'
                f'<div class="card"><div class="muted small">잘못 뽑힌 할 일이면 지울 수 있습니다. 휴지통에서 되살릴 수 있습니다.</div>'
                f'<div class="actions">{self._form(f"/todos/{tid}/delete", "이 할 일 삭제", back, cls="danger")}</div></div>')
        return self._page("할 일 수정", "todos", body, msg)

    def page_delete(self, kind: str, target_id: int, msg: str) -> str | None:
        """삭제 확인 화면. 스크립트 없이 한 번 더 묻는다."""
        if kind == "case":
            case = self.store.get_case(target_id)
            if not case or case["deleted_at"]:
                return None
            n = len(self.store.case_records(target_id))
            what, back = f"#{target_id} {case['title']} 건과 기록 {n}건", f"/cases/{target_id}"
        else:
            rec = self.store.get_record(target_id)
            if not rec or rec["deleted_at"]:
                return None
            title = rec["data"]["title"] if rec["data"] else UNTITLED
            what, back = f"기록 {target_id} ({title})", f"/cases/{rec['case_id']}"
        body = (f'<h1>삭제할까요?</h1><div class="card"><div><b>{e(what)}</b>을(를) 휴지통으로 옮깁니다.</div>'
                f'<div class="muted small">{TRASH_DAYS}일 안에는 휴지통에서 되살릴 수 있고, 그 뒤에는 완전히 지워집니다. '
                "전사문 파일과 음성 파일은 지우지 않습니다.</div>"
                f'<div class="actions">{self._form(f"/{kind}s/{target_id}/delete", "휴지통으로 옮기기", "/", cls="danger")} '
                f'<a class="btn" href="{back}">취소</a></div></div>')
        return self._page("삭제 확인", "cases", body, msg)

    def page_trash(self, q: dict, msg: str) -> str:
        store, now = self.store, datetime.now()

        def left(ts: str) -> str:
            days = TRASH_DAYS - (now - datetime.fromisoformat(ts)).days
            return f"{max(days, 0)}일 뒤 완전히 지워짐"

        def restore(kind: str, target_id: int) -> str:
            return self._form("/trash/restore", "되살리기", "/trash", fields={"kind": kind, "id": target_id})

        cases, recs, todos = store.deleted_cases(), store.deleted_records(), store.deleted_todos()
        body = [f'<h1>휴지통</h1><p class="muted">지운 것은 {TRASH_DAYS}일 동안 여기 있다가 완전히 지워집니다.</p>']
        if not cases and not recs and not todos:
            body.append('<div class="card muted">휴지통이 비어 있습니다.</div>')
        for title, rows, render in (
            ("건", cases, lambda c: (f'#{c["id"]} {e(c["title"])} <span class="muted small">· 기록 {c["n_records"]}건</span>',
                                    restore("case", c["id"]))),
            ("기록", recs, lambda r: (f'기록 {r["id"]} · {e(r["data"]["title"] if r["data"] else UNTITLED)} '
                                     f'<span class="muted small">· {e(_source(r))} · #{r["case_id"]}</span>',
                                     restore("record", r["id"]))),
            ("할 일", todos, lambda t: (f'{e(t["task"])} <span class="muted small">· #{t["case_id"]}</span>',
                                       restore("todo", t["id"]))),
        ):
            if rows:
                body.append(f'<h2>{title} {len(rows)}개</h2><div class="card">' + "".join(
                    f'<div class="todo"><div>{render(x)[0]}<div class="muted small">{left(x["deleted_at"])}</div></div>'
                    f"{render(x)[1]}</div>" for x in rows) + "</div>")
        return self._page("휴지통", "trash", "".join(body), msg)

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

    def _sync_slack(self, *records: dict) -> None:
        """대시보드에서 바뀐 기록의 슬랙 초안을 다시 그린다(지웠으면 지웠다고 표시)."""
        if not self.bot:
            return
        for rec in records:
            try:
                self.bot.refresh_record(self.store.get_record(rec["id"]))
            except Exception:
                log.exception("슬랙 반영 실패(대시보드에는 반영됨)")

    def _sync_case(self, cid: int) -> None:
        self._sync_slack(*[r for r in self.store.case_records(cid) if r["data"]])

    def act(self, path: str, form: dict) -> tuple:
        """POST 한 건을 처리하고 (돌아갈 주소, 알림 문구[, 되돌리기 대상])를 돌려준다."""
        store, parts = self.store, [x for x in path.split("/") if x]
        back = form.get("next", "/")
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        num = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None

        if parts == ["sessions", "logout-all"]:
            return "/sessions", f"브라우저 {store.delete_sessions()}개를 모두 로그아웃했습니다."
        if num is not None and parts[0] == "sessions" and parts[2:] == ["logout"]:
            store.delete_session(num)
            return "/sessions", "로그아웃했습니다."

        if parts == ["trash", "restore"] and form.get("kind") in ("case", "record", "todo") and form.get("id", "").isdigit():
            where = pipeline.restore(store, form["kind"], int(form["id"]))
            if where.startswith("/cases/"):
                self._sync_case(int(where.split("/")[2]))
                return where, "되살렸습니다."
            return "/trash", "되살릴 것을 찾지 못했습니다."

        if num is not None and len(parts) == 3 and parts[0] in ("records", "cases") and parts[2] in ("edit", "release"):
            kind = parts[0][:-1]
            if parts[2] == "release":
                pipeline.release(store, kind, num)
                return back, "직접 고침 표시를 지웠습니다. 다음에 다시 정리하면 LLM 값으로 바뀔 수 있습니다."
            try:
                changed = (pipeline.edit_record if kind == "record" else pipeline.edit_case)(store, num, form)
            except ValueError as err:
                return back, str(err)
            if not changed:
                return back, "바뀐 내용이 없습니다."
            if kind == "record":
                self._sync_slack(store.get_record(num))
            names = ", ".join(views.FIELD_LABEL[f] for f in changed)
            return back, f"저장했습니다: {names}. 다시 정리해도 이 값은 유지됩니다."

        if num is not None and parts[0] == "records" and parts[2:] == ["move"]:
            rec, target = store.get_record(num), form.get("target", "")
            if not rec or rec["deleted_at"]:
                return back, "그 기록이 없습니다."
            old = rec["case_id"]
            try:
                moved = pipeline.split(store, num) if target == "new" else pipeline.merge(store, num, int(target))
            except (ValueError, pipeline.LLMError) as err:
                return back, f"옮기지 못했습니다: {err}"
            self._sync_case(moved["case_id"])
            if store.get_case(old) and not store.get_case(old)["deleted_at"]:
                self._sync_case(old)
            what = "새 건으로 떼어냈습니다." if target == "new" else f"#{moved['case_id']} 건으로 옮겼습니다."
            return f"/cases/{moved['case_id']}", f"기록 {num}을(를) {what}"

        if num is not None and parts[0] == "records" and parts[2:] == ["delete"]:
            rec = pipeline.delete(store, num)
            if not rec:
                return "/", "그 기록이 없습니다."
            self._sync_slack(rec)
            case = store.get_case(rec["case_id"])
            where = f"/cases/{rec['case_id']}" if case and not case["deleted_at"] else "/"
            return where, f"기록 {num}을(를) 휴지통으로 옮겼습니다.", f"record:{num}"

        if num is not None and parts[0] == "cases" and len(parts) == 3:
            case = store.get_case(num)
            if not case or case["deleted_at"]:
                return "/", "그 건이 없습니다."
            if parts[2] == "delete":
                recs = store.case_records(num)
                pipeline.delete_case(store, num)
                self._sync_slack(*recs)
                return "/", f"#{num} 건을 휴지통으로 옮겼습니다.", f"case:{num}"
            if parts[2] == "close":
                pipeline.close(store, num)
                return "/", f"#{num} 건을 종결했습니다. 종결한 건 보기에서 다시 열 수 있습니다."
            if parts[2] == "reopen":
                pipeline.reopen(store, num)
                self._sync_case(num)
                return f"/cases/{num}", "다시 열었습니다. 종결하면서 완료 처리됐던 할 일도 되돌렸습니다."
            if parts[2] == "todos":
                if not form.get("task", "").strip():
                    return back, "할 일 내용이 비어 있습니다."
                pipeline.add_todo(store, num, form["task"], form.get("owner"), form.get("due"))
                self._sync_case(num)
                return back, "할 일을 더했습니다."
            if parts[2] == "note":
                if not form.get("text", "").strip():
                    return back, "메모가 비어 있습니다."
                try:
                    pipeline.add_note(store, num, form["text"].strip())
                except pipeline.ProcessError as err:
                    return back, f"원문은 이 건에 저장했지만 정리에 실패했습니다: {err}"
                self._sync_case(num)
                return back, "메모를 정리해 이 건에 붙였습니다."

        if num is not None and parts[0] == "todos" and len(parts) == 3:
            if parts[2] == "edit":
                todo = store.get_todo(num)
                if not todo:
                    return back, "그 할 일이 없습니다."
                if not form.get("task", "").strip():
                    return back, "할 일 내용이 비어 있습니다."
                changed = pipeline.edit_todo(store, num, form["task"], form.get("owner"), form.get("due"))
                self._sync_case(todo["case_id"])
                return back, "저장했습니다. 다시 정리해도 이 할 일은 유지됩니다." if changed else "바뀐 내용이 없습니다."
            if parts[2] in ("done", "reopen", "keep", "delete"):
                todo = pipeline.todo_action(store, num, parts[2])
                if not todo:
                    return back, "그 할 일이 없습니다."
                self._sync_case(todo["case_id"])
                text = {"done": "완료 처리했습니다.", "reopen": "다시 미완료로 돌렸습니다.",
                        "keep": "남은 일로 두었습니다. 다시 완료로 추정하지 않습니다.",
                        "delete": "할 일을 휴지통으로 옮겼습니다."}[parts[2]]
                return (back, text, f"todo:{num}") if parts[2] == "delete" else (back, text)

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

            def _send(self, status: int, body: str = "", location: str | None = None,
                      cookie: str | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)

            def _cookie(self, token: str | None) -> str:
                """세션 쿠키. token이 None이면 지운다."""
                secure = "; Secure" if cfg.dashboard_url.startswith("https://") else ""
                age = SESSION_DAYS * 86400 if token else 0
                return f"{COOKIE}={token or ''}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax{secure}"

            def _allowed(self) -> bool:
                """이 요청을 받아도 되는지 정하고, 안 되면 여기서 응답한다.

                - 이 PC의 브라우저가 직접 연 요청(프록시 헤더 없음, Host가 이 PC): 로그인 없이 받는다.
                - 그 밖(프록시를 거쳤거나 다른 이름으로 온 요청): DASHBOARD_URL이 있고 세션 쿠키가 유효할 때만.
                """
                dash._ctx.session = None
                direct = (self.headers.get("Host", "") in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}")
                          and not any(self.headers.get(h) for h in PROXY_HEADERS))
                if direct:
                    return True
                if not cfg.dashboard_url:
                    self._send(403, "이 PC에서만 열 수 있습니다.")
                    return False
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                token = jar[COOKIE].value if COOKIE in jar else ""
                dash._ctx.session = dash.store.get_session(dash._hash(token)) if token else None
                if dash._ctx.session or urlsplit(self.path).path.startswith("/login"):
                    return True
                self._send(401, dash.page_login("new", next_path=self.path if self.command == "GET" else "/"))
                return False

            def do_GET(self):
                if not self._allowed():
                    return
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/login":
                    nxt = _safe_next(q.get("next", "/"))
                    if dash._ctx.session:   # 이미 로그인한 브라우저: 링크를 쓰지 않고 바로 연다
                        return self._send(303, location=nxt)
                    return self._send(200, dash.page_login("code" if q.get("code") else "new", q.get("code", ""), nxt))
                msg, parts = Flash(q.get("msg", "")), [x for x in url.path.split("/") if x]
                msg.undo = q.get("undo", "")
                num = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
                try:
                    if not parts:
                        page = dash.page_cases(q, msg)
                    elif parts[0] == "cases" and len(parts) == 2 and num is not None:
                        page = dash.page_case(num, msg)
                    elif num is not None and len(parts) == 3 and parts[0] in ("cases", "records") and parts[2] == "edit":
                        page = dash.page_edit(parts[0][:-1], num, msg)
                    elif num is not None and len(parts) == 3 and parts[0] in ("cases", "records") and parts[2] == "delete":
                        page = dash.page_delete(parts[0][:-1], num, msg)
                    elif num is not None and parts[0] == "todos" and parts[2:] == ["edit"]:
                        page = dash.page_todo_edit(num, q, msg)
                    elif len(parts) == 1 and parts[0] in ("todos", "attention", "import", "profile", "trash", "sessions"):
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
                if not self._allowed():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, "내용이 너무 큽니다.")
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                                                     keep_blank_values=True).items()}
                path, nxt = urlsplit(self.path).path, _safe_next(form.get("next", "/"))
                if path == "/login/request" and not dash._ctx.session:
                    if not cfg.dashboard_url:
                        return self._send(404, "")
                    return self._send(200, dash.page_login(dash.request_login(nxt), next_path=nxt))
                if path == "/login":  # 아직 세션이 없다: 일회용 코드가 곧 자격이다
                    token = dash._redeem(form.get("code", ""), self.headers.get("User-Agent", ""))
                    if not token:
                        return self._send(401, dash.page_login("failed", next_path=nxt))
                    return self._send(303, location=nxt, cookie=self._cookie(token))
                if not hmac.compare_digest(form.get("token", ""), dash.token):
                    return self._send(403, dash._page("거절", "", '<div class="notice">이 화면이 오래됐습니다(두괄이가 '
                                                      '다시 켜졌을 수 있습니다). <a href="/">새로 열어</a> 다시 해 주세요.</div>'))
                try:
                    back, msg, *undo = dash.act(urlsplit(self.path).path, form)
                except Exception as err:
                    log.exception("대시보드 요청 처리 오류: %s", self.path)
                    back, msg, undo = "/", f"처리하다 오류가 났습니다: {err}", []
                sep = "&" if "?" in back else "?"
                extra = f"&undo={quote(undo[0])}" if undo else ""
                self._send(303, location=f"{back}{sep}msg={quote(msg)}{extra}" if msg else back)

        return Handler
