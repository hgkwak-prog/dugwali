"""SQLite 저장소: 건(cases), 기록(records), 할 일(todos), 처리한 녹음 파일(seen_files),
처리 실패(failures), 앱 상태(app_state), 수정 이력(corrections).

- 삭제는 지우지 않고 `deleted_at`을 찍는다(휴지통). `TRASH_DAYS`가 지나면 `purge_trash`가 실제로 지운다.
- 사람이 고친 값은 `manual`로 표시한다. LLM이 다시 정리해도 그 값은 덮어쓰지 않는다.
- 할 일은 이 테이블이 기준이다. LLM이 뽑은 목록은 `sync_todos`로 맞추되, 사람이 고친 것은 건드리지 않고
  LLM이 더는 언급하지 않는 것은 지우지 않고 "완료 추정"(`suspect`)으로 표시한다.

기록은 메모·통화 1개이고, 건은 같은 일에 속한 기록들의 묶음이다. 모든 기록은 건 하나에 속한다.
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    data TEXT,
    dirty INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    data TEXT,
    channel TEXT,
    thread_ts TEXT,
    msg_ts TEXT,
    audio_path TEXT
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    owner TEXT,
    due TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_files (path TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    target TEXT NOT NULL,
    record_id INTEGER,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    before TEXT,
    after TEXT
);
"""
# 이전 버전 DB에 없으면 더하는 열
_COLUMNS = [
    ("records", "case_id", "INTEGER"),
    ("records", "deleted_at", "TEXT"),
    ("records", "manual", "TEXT"),          # 사람이 고친 필드 이름들(JSON 배열)
    ("cases", "deleted_at", "TEXT"),
    ("cases", "manual", "TEXT"),
    ("todos", "manual", "INTEGER NOT NULL DEFAULT 0"),          # 사람이 만들었거나 고친 할 일
    ("todos", "suspect", "INTEGER NOT NULL DEFAULT 0"),         # 완료 추정: LLM이 더는 언급하지 않음
    ("todos", "closed_by_case", "INTEGER NOT NULL DEFAULT 0"),  # 건을 종결하면서 완료 처리된 것
    ("todos", "deleted_at", "TEXT"),
]
TRASH_DAYS = 30
_SPACE = re.compile(r"[\s\W_]+")
UNTITLED = "미정리 기록"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    """삭제 시각. 같은 초에 따로 지운 것과 함께 지운 것을 구분하려고 마이크로초까지 남긴다."""
    return datetime.now().isoformat(timespec="microseconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if "data" in d:
        d["data"] = json.loads(d["data"]) if d["data"] else None
    if "manual" in d and "task" not in d:  # 기록·건: 사람이 고친 필드 목록
        d["manual"] = json.loads(d["manual"]) if d["manual"] else []
    return d


def _key(task: str) -> str:
    """할 일 문구 비교용: 띄어쓰기와 문장부호 차이는 같은 일로 본다."""
    return _SPACE.sub("", task).lower()


def _dump(data: dict | None) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data else None


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """이전 버전 DB: 없는 열을 더하고, 건 도입 이전 기록마다 건을 하나씩 만들어 연결한다."""
        for table, col, decl in _COLUMNS:
            if col not in {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for r in self._db.execute("SELECT id, created_at, data FROM records WHERE case_id IS NULL").fetchall():
            title = (json.loads(r["data"]) if r["data"] else {}).get("title") or UNTITLED
            cid = self._db.execute(
                "INSERT INTO cases (title, created_at, updated_at) VALUES (?,?,?)",
                (title, r["created_at"], r["created_at"]),
            ).lastrowid
            self._db.execute("UPDATE records SET case_id=? WHERE id=?", (cid, r["id"]))

    # --- cases ---
    def _touch(self, cid: int) -> None:
        self._db.execute("UPDATE cases SET dirty=1, updated_at=? WHERE id=?", (_now(), cid))

    def _settle_emptied(self, cid: int, when: str) -> None:
        """살아 있는 기록이 없어진 건을 정리한다: 기록이 하나도 없으면 지우고, 휴지통 기록만 남았으면 함께 휴지통으로."""
        if self._db.execute("SELECT 1 FROM records WHERE case_id=? AND deleted_at IS NULL LIMIT 1", (cid,)).fetchone():
            self._touch(cid)
        elif self._db.execute("SELECT 1 FROM records WHERE case_id=? LIMIT 1", (cid,)).fetchone():
            self._db.execute("UPDATE cases SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (when, cid))
        else:
            self._db.execute("DELETE FROM cases WHERE id=?", (cid,))

    def get_case(self, cid: int) -> dict | None:
        """휴지통에 있는 건도 돌려준다(`deleted_at`으로 구분)."""
        with self._lock:
            return _row(self._db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone())

    def open_cases(self, days: int | None = None) -> list[dict]:
        """진행 중인 건(최근 갱신순). 기록 수와 미완료 할 일 수를 함께 준다."""
        return self.list_cases("open", days)

    def list_cases(self, status: str = "open", days: int | None = None) -> list[dict]:
        sql = """SELECT c.*,
                   (SELECT COUNT(*) FROM records r WHERE r.case_id=c.id AND r.deleted_at IS NULL) AS n_records,
                   (SELECT COUNT(*) FROM todos t JOIN records r ON r.id=t.record_id
                     WHERE r.case_id=c.id AND r.deleted_at IS NULL AND t.deleted_at IS NULL AND t.done=0) AS n_todos
                 FROM cases c WHERE c.status=? AND c.deleted_at IS NULL"""
        args: tuple = (status,)
        if days is not None:
            sql += " AND c.updated_at >= ?"
            args += ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"),)
        with self._lock:
            return [_row(r) for r in self._db.execute(sql + " ORDER BY c.updated_at DESC, c.id DESC", args)]

    def case_records(self, cid: int, deleted: bool = False) -> list[dict]:
        """건의 기록(시간순). deleted=True면 휴지통에 있는 기록만."""
        cond = "IS NOT NULL" if deleted else "IS NULL"
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM records WHERE case_id=? AND deleted_at {cond} ORDER BY created_at, id", (cid,)
            ).fetchall()
        return [_row(r) for r in rows]

    def new_case(self, title: str) -> int:
        with self._lock, self._db:
            return self._db.execute("INSERT INTO cases (title, created_at, updated_at) VALUES (?,?,?)",
                                    (title or UNTITLED, _now(), _now())).lastrowid

    def set_case_summary(self, cid: int, data: dict, manual: list[str] | None = None) -> None:
        """건의 통합 정리를 저장한다. manual을 주면 사람이 고친 필드 목록도 바꾼다."""
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET data=?, dirty=0, title=? WHERE id=?",
                             (_dump(data), data["title"], cid))
            if manual is not None:
                self._db.execute("UPDATE cases SET manual=? WHERE id=?", (json.dumps(manual, ensure_ascii=False), cid))

    def close_case(self, cid: int) -> None:
        """건을 종결하고 남은 할 일을 완료 처리한다. 재개하면 되돌릴 수 있게 표시해 둔다."""
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET status='closed', updated_at=? WHERE id=?", (_now(), cid))
            self._db.execute(
                "UPDATE todos SET done=1, closed_by_case=1 WHERE done=0 AND deleted_at IS NULL"
                " AND record_id IN (SELECT id FROM records WHERE case_id=?)", (cid,))

    def reopen_case(self, cid: int) -> None:
        """건을 다시 연다. 종결하면서 완료 처리됐던 할 일도 다시 연다."""
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET status='open', updated_at=? WHERE id=?", (_now(), cid))
            self._db.execute(
                "UPDATE todos SET done=0, closed_by_case=0 WHERE closed_by_case=1"
                " AND record_id IN (SELECT id FROM records WHERE case_id=?)", (cid,))

    def delete_case(self, cid: int) -> list[dict]:
        """건과 그 기록을 휴지통으로 옮긴다. 옮긴 기록을 돌려준다(로그 갱신용)."""
        recs, when = self.case_records(cid), _stamp()
        with self._lock, self._db:
            self._db.execute("UPDATE records SET deleted_at=? WHERE case_id=? AND deleted_at IS NULL", (when, cid))
            self._db.execute("UPDATE cases SET deleted_at=? WHERE id=?", (when, cid))
            self._db.execute(
                "UPDATE failures SET resolved_at=? WHERE resolved_at IS NULL"
                " AND record_id IN (SELECT id FROM records WHERE case_id=?)", (when, cid))
        return recs

    def restore_case(self, cid: int) -> list[dict]:
        """휴지통의 건을 되살린다. 건과 함께 지워졌던 기록만 같이 돌아온다."""
        case = self.get_case(cid)
        if not case or not case["deleted_at"]:
            return []
        with self._lock, self._db:
            self._db.execute("UPDATE records SET deleted_at=NULL WHERE case_id=? AND deleted_at=?",
                             (cid, case["deleted_at"]))
            self._db.execute("UPDATE cases SET deleted_at=NULL, dirty=1, updated_at=? WHERE id=?", (_now(), cid))
        return self.case_records(cid)

    def deleted_cases(self) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM records r WHERE r.case_id=c.id AND r.deleted_at=c.deleted_at)"
                " AS n_records FROM cases c WHERE c.deleted_at IS NOT NULL ORDER BY c.deleted_at DESC")]

    # --- records ---
    def add_record(self, source: str, raw_text: str, data: dict | None = None,
                   audio_path: str | None = None, created_at: datetime | None = None,
                   case_id: int | None = None) -> int:
        """기록을 만든다. case_id가 없으면 새 건을 만들어 넣는다."""
        ts = (created_at or datetime.now()).isoformat(timespec="seconds")
        with self._lock, self._db:
            if case_id is None:
                case_id = self._db.execute(
                    "INSERT INTO cases (title, created_at, updated_at) VALUES (?,?,?)",
                    ((data or {}).get("title") or UNTITLED, ts, ts),
                ).lastrowid
            else:
                self._touch(case_id)
            rid = self._db.execute(
                "INSERT INTO records (created_at, source, raw_text, data, audio_path, case_id)"
                " VALUES (?,?,?,?,?,?)",
                (ts, source, raw_text, _dump(data), audio_path, case_id),
            ).lastrowid
        if data:
            self.sync_todos(case_id, data["todos"])
        return rid

    def update_record(self, rid: int, *, raw_text: str | None = None, data: dict | None = None,
                      manual: list[str] | None = None) -> None:
        """기록을 고친다. 할 일은 여기서 맞추지 않는다(`sync_todos`를 따로 부른다)."""
        with self._lock, self._db:
            if raw_text is not None:
                self._db.execute("UPDATE records SET raw_text=? WHERE id=?", (raw_text, rid))
            if data is not None:
                self._db.execute("UPDATE records SET data=? WHERE id=?", (_dump(data), rid))
            if manual is not None:
                self._db.execute("UPDATE records SET manual=? WHERE id=?",
                                 (json.dumps(manual, ensure_ascii=False), rid))
            cid = self._db.execute("SELECT case_id FROM records WHERE id=?", (rid,)).fetchone()["case_id"]
            self._touch(cid)
            if data is not None:
                # 살아 있는 기록이 하나뿐인 건은 그 기록의 제목을 따라간다
                n = self._db.execute("SELECT COUNT(*) FROM records WHERE case_id=? AND deleted_at IS NULL",
                                     (cid,)).fetchone()[0]
                if n == 1:
                    self._db.execute("UPDATE cases SET title=? WHERE id=?", (data["title"], cid))

    def move_record(self, rid: int, cid: int) -> int | None:
        """기록을 다른 건으로 옮긴다. 원래 건 번호를 돌려준다(같은 건이면 None)."""
        with self._lock, self._db:
            old = self._db.execute("SELECT case_id FROM records WHERE id=?", (rid,)).fetchone()["case_id"]
            if old == cid:
                return None
            self._db.execute("UPDATE records SET case_id=? WHERE id=?", (cid, rid))
            self._touch(cid)
            self._settle_emptied(old, _stamp())
            return old

    def delete_record(self, rid: int) -> dict | None:
        """기록을 휴지통으로 옮긴다. 건의 할 일은 남은 기록으로 옮겨 두고, 건이 비면 건도 함께 옮긴다."""
        rec = self.get_record(rid)
        if not rec or rec["deleted_at"]:
            return None
        when = _stamp()
        with self._lock, self._db:
            other = self._db.execute(
                "SELECT id FROM records WHERE case_id=? AND id<>? AND deleted_at IS NULL"
                " ORDER BY (data IS NULL), created_at DESC, id DESC LIMIT 1", (rec["case_id"], rid)).fetchone()
            if other:  # 건의 할 일이 지우는 기록에 붙어 있어도 건에는 남는다
                self._db.execute("UPDATE todos SET record_id=? WHERE record_id=?", (other["id"], rid))
            self._db.execute("UPDATE records SET deleted_at=? WHERE id=?", (when, rid))
            self._db.execute("UPDATE failures SET resolved_at=? WHERE resolved_at IS NULL AND record_id=?", (when, rid))
            self._settle_emptied(rec["case_id"], when)
        return rec

    def restore_record(self, rid: int) -> dict | None:
        """휴지통의 기록을 되살린다. 건이 함께 휴지통에 있었으면 건도 되살린다."""
        rec = self.get_record(rid)
        if not rec or not rec["deleted_at"]:
            return None
        with self._lock, self._db:
            self._db.execute("UPDATE records SET deleted_at=NULL WHERE id=?", (rid,))
            self._db.execute("UPDATE cases SET deleted_at=NULL WHERE id=?", (rec["case_id"],))
            self._touch(rec["case_id"])
        return self.get_record(rid)

    def deleted_records(self) -> list[dict]:
        """휴지통의 기록 중 건은 살아 있는 것(건째로 지운 것은 `deleted_cases`에 나온다)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.* FROM records r JOIN cases c ON c.id=r.case_id"
                " WHERE r.deleted_at IS NOT NULL AND (c.deleted_at IS NULL OR c.deleted_at<>r.deleted_at)"
                " ORDER BY r.deleted_at DESC").fetchall()
        return [_row(r) for r in rows]

    def purge_trash(self, days: int = TRASH_DAYS) -> int:
        """휴지통에 들어간 지 days일이 지난 것을 실제로 지운다. 지운 기록 수를 돌려준다."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            n = self._db.execute("SELECT COUNT(*) FROM records WHERE deleted_at < ?", (cutoff,)).fetchone()[0]
            self._db.execute("DELETE FROM todos WHERE deleted_at < ?"
                             " OR record_id IN (SELECT id FROM records WHERE deleted_at < ?)", (cutoff, cutoff))
            self._db.execute("DELETE FROM failures WHERE record_id IN (SELECT id FROM records WHERE deleted_at < ?)",
                             (cutoff,))
            self._db.execute("DELETE FROM records WHERE deleted_at < ?", (cutoff,))
            self._db.execute("DELETE FROM cases WHERE deleted_at < ?"
                             " AND id NOT IN (SELECT case_id FROM records)", (cutoff,))
        return n

    def set_slack_ref(self, rid: int, channel: str, thread_ts: str, msg_ts: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE records SET channel=?, thread_ts=?, msg_ts=? WHERE id=?",
                (channel, thread_ts, msg_ts, rid),
            )

    def get_record(self, rid: int) -> dict | None:
        """휴지통에 있는 기록도 돌려준다(`deleted_at`으로 구분)."""
        with self._lock:
            return _row(self._db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())

    def find_by_thread(self, channel: str, thread_ts: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM records WHERE channel=? AND thread_ts=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (channel, thread_ts),
            ).fetchone())

    def find_by_audio(self, audio_path: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM records WHERE audio_path=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (audio_path,)).fetchone())

    def unprocessed_records(self) -> list[dict]:
        """아직 정리(구조화)하지 않은 기록: 요약 승인 전 통화, 정리에 실패한 메모."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE data IS NULL AND deleted_at IS NULL ORDER BY created_at, id").fetchall()
        return [_row(r) for r in rows]

    def records_on(self, day: str) -> list[dict]:
        """day: YYYY-MM-DD"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE substr(created_at,1,10)=? AND deleted_at IS NULL ORDER BY created_at, id",
                (day,)).fetchall()
        return [_row(r) for r in rows]

    def records_since(self, days: int) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE created_at >= ? AND deleted_at IS NULL"
                " ORDER BY created_at DESC, id DESC", (since,)).fetchall()
        return [_row(r) for r in rows]

    # --- todos ---
    _IN_CASE = "record_id IN (SELECT id FROM records WHERE case_id=? AND deleted_at IS NULL)"
    _LIVE = ("FROM todos t JOIN records r ON r.id=t.record_id"
             " WHERE t.deleted_at IS NULL AND r.deleted_at IS NULL")

    def _home(self, cid: int) -> int | None:
        """건의 새 할 일을 붙일 기록: 정리된 것 중 가장 최근 기록."""
        r = self._db.execute(
            "SELECT id FROM records WHERE case_id=? AND deleted_at IS NULL"
            " ORDER BY (data IS NULL), created_at DESC, id DESC LIMIT 1", (cid,)).fetchone()
        return r["id"] if r else None

    def sync_todos(self, cid: int, todos: list[dict]) -> None:
        """LLM이 뽑은 할 일 목록에 건의 할 일을 맞춘다. 문구가 같으면 같은 일로 본다.

        - 사람이 고쳤거나 만든 할 일(manual)은 건드리지 않는다.
        - 이미 끝냈거나 지운 일은 LLM이 다시 뽑아도 되살리지 않는다.
        - 같은 문구가 여러 개 열려 있으면 하나로 줄인다(기록마다 겹쳐 나온 할 일).
        - LLM이 더는 언급하지 않는 열린 할 일은 지우지 않고 완료 추정(suspect)으로 표시한다.
        """
        with self._lock, self._db:
            home = self._home(cid)
            if home is None:
                return
            rows = [dict(r) for r in self._db.execute(f"SELECT * FROM todos WHERE {self._IN_CASE} ORDER BY id", (cid,))]
            mentioned: set[int] = set()
            for t in todos:
                same = [r for r in rows if _key(r["task"]) == _key(t["task"])]
                live = [r for r in same if not r["done"] and not r["deleted_at"]]
                if not same:
                    self._db.execute(
                        "INSERT INTO todos (record_id, task, owner, due, created_at) VALUES (?,?,?,?,?)",
                        (home, t["task"], t.get("owner"), t.get("due"), _now()))
                    continue
                if not live:
                    continue  # 끝냈거나 지운 일
                keep = next((r for r in live if r["manual"]), live[0])
                mentioned.add(keep["id"])
                for dup in live:
                    if dup["id"] != keep["id"] and not dup["manual"]:
                        self._db.execute("DELETE FROM todos WHERE id=?", (dup["id"],))
                        mentioned.add(dup["id"])
                if keep["manual"]:
                    self._db.execute("UPDATE todos SET suspect=0 WHERE id=?", (keep["id"],))
                else:
                    self._db.execute("UPDATE todos SET task=?, owner=?, due=?, suspect=0 WHERE id=?",
                                     (t["task"], t.get("owner"), t.get("due"), keep["id"]))
            for r in rows:
                if r["id"] not in mentioned and not r["done"] and not r["deleted_at"] and not r["manual"]:
                    self._db.execute("UPDATE todos SET suspect=1 WHERE id=?", (r["id"],))

    def add_todo(self, cid: int, task: str, owner: str | None, due: str | None) -> int | None:
        """사람이 직접 더한 할 일."""
        with self._lock, self._db:
            home = self._home(cid)
            if home is None:
                return None
            return self._db.execute(
                "INSERT INTO todos (record_id, task, owner, due, created_at, manual) VALUES (?,?,?,?,?,1)",
                (home, task, owner, due, _now())).lastrowid

    def get_todo(self, tid: int) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT t.*, r.case_id FROM todos t JOIN records r ON r.id=t.record_id WHERE t.id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def edit_todo(self, tid: int, task: str, owner: str | None, due: str | None) -> None:
        """사람이 고친 할 일. 이후 LLM이 다시 정리해도 바뀌지 않는다."""
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET task=?, owner=?, due=?, manual=1, suspect=0 WHERE id=?",
                             (task, owner, due, tid))

    def open_todos(self) -> list[dict]:
        """미완료 할 일. 각 항목에 속한 건 번호(case_id)가 붙는다."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT t.*, r.case_id {self._LIVE} AND t.done=0 ORDER BY (t.due IS NULL), t.due, t.id").fetchall()
        return [dict(r) for r in rows]

    def case_todos(self, cid: int) -> list[dict]:
        """건의 할 일 전부(완료 포함). 미완료가 먼저, 그 안에서는 기한순."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                f"SELECT t.*, r.case_id {self._LIVE} AND r.case_id=?"
                " ORDER BY t.done, (t.due IS NULL), t.due, t.id", (cid,))]

    def done_tasks(self, cid: int) -> list[str]:
        with self._lock:
            return [r["task"] for r in self._db.execute(
                f"SELECT t.task {self._LIVE} AND r.case_id=? AND t.done=1 ORDER BY t.id", (cid,))]

    def complete_todo(self, tid: int) -> dict | None:
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET done=1, suspect=0, closed_by_case=0 WHERE id=?", (tid,))
        return self.get_todo(tid)

    def reopen_todo(self, tid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET done=0, closed_by_case=0 WHERE id=?", (tid,))

    def keep_todo(self, tid: int) -> None:
        """완료 추정을 사람이 "아직 남음"으로 확인했다. 다시 추정하지 않게 사람이 확인한 일로 둔다."""
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET suspect=0, manual=1 WHERE id=?", (tid,))

    def delete_todo(self, tid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET deleted_at=? WHERE id=?", (_stamp(), tid))

    def restore_todo(self, tid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET deleted_at=NULL WHERE id=?", (tid,))

    def deleted_todos(self) -> list[dict]:
        """휴지통의 할 일 중 그 기록은 살아 있는 것."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT t.*, r.case_id FROM todos t JOIN records r ON r.id=t.record_id"
                " WHERE t.deleted_at IS NOT NULL AND r.deleted_at IS NULL ORDER BY t.deleted_at DESC")]

    # --- corrections ---
    def add_correction(self, target: str, target_id: int, field: str, before, after) -> None:
        """사람이 고친 내용을 남긴다(나중에 정리 기준으로 올릴 근거)."""
        def text(v):
            return v if isinstance(v, str) or v is None else json.dumps(v, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO corrections (created_at, target, target_id, field, before, after) VALUES (?,?,?,?,?,?)",
                (_now(), target, target_id, field, text(before), text(after)))

    def corrections(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,))]

    # --- seen files ---
    def is_seen(self, path: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM seen_files WHERE path=?", (path,)).fetchone() is not None

    def mark_seen(self, path: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO seen_files (path, seen_at) VALUES (?,?)", (path, _now()))

    # --- failures ---
    def record_failure(self, stage: str, target: str, error: str, record_id: int | None = None) -> dict:
        """실패를 남긴다. 같은 대상의 열린 실패가 있으면 새로 만들지 않고 시도 횟수만 올린다."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM failures WHERE stage=? AND target=? AND resolved_at IS NULL", (stage, target)
            ).fetchone()
            if row:
                fid = row["id"]
                self._db.execute("UPDATE failures SET attempts=attempts+1, error=? WHERE id=?", (error[:500], fid))
            else:
                fid = self._db.execute(
                    "INSERT INTO failures (created_at, stage, target, record_id, error) VALUES (?,?,?,?,?)",
                    (_now(), stage, target, record_id, error[:500])).lastrowid
        return self.get_failure(fid)

    def record_has_failure(self, rid: int) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM failures WHERE record_id=? AND resolved_at IS NULL", (rid,)).fetchone() is not None

    def get_failure(self, fid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM failures WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None

    def open_failures(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM failures WHERE resolved_at IS NULL ORDER BY id")]

    def resolve_failures(self, *, fid: int | None = None, stage: str | None = None,
                         target: str | None = None) -> None:
        """실패 항목을 닫는다. 번호로, 또는 단계+대상으로 지정한다."""
        with self._lock, self._db:
            if fid is not None:
                self._db.execute("UPDATE failures SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
                                 (_now(), fid))
            else:
                self._db.execute(
                    "UPDATE failures SET resolved_at=? WHERE stage=? AND target=? AND resolved_at IS NULL",
                    (_now(), stage, target))

    # --- app state ---
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO app_state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
