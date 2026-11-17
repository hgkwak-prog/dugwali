"""SQLite 저장소: 건(cases), 기록(records), 할 일(todos), 처리한 녹음 파일(seen_files),
처리 실패(failures), 앱 상태(app_state).

기록은 메모·통화 1개이고, 건은 같은 일에 속한 기록들의 묶음이다. 모든 기록은 건 하나에 속한다.
"""
import json
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
"""
UNTITLED = "미정리 기록"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if "data" in d:
        d["data"] = json.loads(d["data"]) if d["data"] else None
    return d


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
        """건 도입 이전 DB: case_id 열을 추가하고 기존 기록마다 건을 하나씩 만들어 연결한다."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(records)")}
        if "case_id" not in cols:
            self._db.execute("ALTER TABLE records ADD COLUMN case_id INTEGER")
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

    def _drop_if_empty(self, cid: int) -> bool:
        if self._db.execute("SELECT 1 FROM records WHERE case_id=? LIMIT 1", (cid,)).fetchone():
            return False
        self._db.execute("DELETE FROM cases WHERE id=?", (cid,))
        return True

    def get_case(self, cid: int) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone())

    def open_cases(self, days: int | None = None) -> list[dict]:
        """진행 중인 건(최근 갱신순). 기록 수와 미완료 할 일 수를 함께 준다."""
        sql = """SELECT c.*,
                   (SELECT COUNT(*) FROM records r WHERE r.case_id=c.id) AS n_records,
                   (SELECT COUNT(*) FROM todos t JOIN records r ON r.id=t.record_id
                     WHERE r.case_id=c.id AND t.done=0) AS n_todos
                 FROM cases c WHERE c.status='open'"""
        args: tuple = ()
        if days is not None:
            sql += " AND c.updated_at >= ?"
            args = ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"),)
        with self._lock:
            return [_row(r) for r in self._db.execute(sql + " ORDER BY c.updated_at DESC, c.id DESC", args)]

    def case_records(self, cid: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE case_id=? ORDER BY created_at, id", (cid,)).fetchall()
        return [_row(r) for r in rows]

    def set_case_summary(self, cid: int, data: dict) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET data=?, dirty=0, title=? WHERE id=?",
                             (_dump(data), data["title"], cid))

    def close_case(self, cid: int) -> None:
        """건을 종결하고 남은 할 일을 완료 처리한다."""
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET status='closed', updated_at=? WHERE id=?", (_now(), cid))
            self._db.execute(
                "UPDATE todos SET done=1 WHERE record_id IN (SELECT id FROM records WHERE case_id=?)", (cid,))

    def reopen_case(self, cid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET status='open', updated_at=? WHERE id=?", (_now(), cid))

    def delete_case(self, cid: int) -> list[dict]:
        """건과 그 기록·할 일을 모두 지운다. 지운 기록을 돌려준다(로그 갱신용)."""
        recs = self.case_records(cid)
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM todos WHERE record_id IN (SELECT id FROM records WHERE case_id=?)", (cid,))
            self._db.execute(
                "UPDATE failures SET resolved_at=? WHERE resolved_at IS NULL"
                " AND record_id IN (SELECT id FROM records WHERE case_id=?)", (_now(), cid))
            self._db.execute("DELETE FROM records WHERE case_id=?", (cid,))
            self._db.execute("DELETE FROM cases WHERE id=?", (cid,))
        return recs

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
            self._sync_todos(rid, data["todos"])
        return rid

    def update_record(self, rid: int, *, raw_text: str | None = None, data: dict | None = None) -> None:
        with self._lock, self._db:
            if raw_text is not None:
                self._db.execute("UPDATE records SET raw_text=? WHERE id=?", (raw_text, rid))
            if data is not None:
                self._db.execute("UPDATE records SET data=? WHERE id=?", (_dump(data), rid))
            cid = self._db.execute("SELECT case_id FROM records WHERE id=?", (rid,)).fetchone()["case_id"]
            self._touch(cid)
            if data is not None:
                # 기록이 하나뿐인 건은 그 기록의 제목을 따라간다
                n = self._db.execute("SELECT COUNT(*) FROM records WHERE case_id=?", (cid,)).fetchone()[0]
                if n == 1:
                    self._db.execute("UPDATE cases SET title=? WHERE id=?", (data["title"], cid))
        if data is not None:
            self._sync_todos(rid, data["todos"])

    def move_record(self, rid: int, cid: int) -> int | None:
        """기록을 다른 건으로 옮긴다. 비게 된 원래 건은 지우고 그 번호를 돌려준다."""
        with self._lock, self._db:
            old = self._db.execute("SELECT case_id FROM records WHERE id=?", (rid,)).fetchone()["case_id"]
            if old == cid:
                return None
            self._db.execute("UPDATE records SET case_id=? WHERE id=?", (cid, rid))
            self._touch(cid)
            if self._drop_if_empty(old):
                return old
            self._touch(old)
            return None

    def delete_record(self, rid: int) -> dict | None:
        """기록과 그 할 일을 지운다. 건이 비면 건도 지운다. 지운 기록을 돌려준다."""
        rec = self.get_record(rid)
        if not rec:
            return None
        with self._lock, self._db:
            self._db.execute("DELETE FROM todos WHERE record_id=?", (rid,))
            self._db.execute(
                "UPDATE failures SET resolved_at=? WHERE resolved_at IS NULL AND record_id=?", (_now(), rid))
            self._db.execute("DELETE FROM records WHERE id=?", (rid,))
            if not self._drop_if_empty(rec["case_id"]):
                self._touch(rec["case_id"])
        return rec

    def set_slack_ref(self, rid: int, channel: str, thread_ts: str, msg_ts: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE records SET channel=?, thread_ts=?, msg_ts=? WHERE id=?",
                (channel, thread_ts, msg_ts, rid),
            )

    def get_record(self, rid: int) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())

    def find_by_thread(self, channel: str, thread_ts: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM records WHERE channel=? AND thread_ts=? ORDER BY id DESC LIMIT 1",
                (channel, thread_ts),
            ).fetchone())

    def find_by_audio(self, audio_path: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM records WHERE audio_path=? ORDER BY id DESC LIMIT 1", (audio_path,)).fetchone())

    def unprocessed_records(self) -> list[dict]:
        """아직 정리(구조화)하지 않은 기록: 요약 승인 전 통화, 정리에 실패한 메모."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM records WHERE data IS NULL ORDER BY created_at, id").fetchall()
        return [_row(r) for r in rows]

    def records_on(self, day: str) -> list[dict]:
        """day: YYYY-MM-DD"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE substr(created_at,1,10)=? ORDER BY created_at, id", (day,)
            ).fetchall()
        return [_row(r) for r in rows]

    # --- todos ---
    def _sync_todos(self, rid: int, todos: list[dict]) -> None:
        """재추출 시 완료 처리한 항목은 보존하고 미완료 항목만 갈아 끼운다."""
        with self._lock, self._db:
            done = {r["task"] for r in self._db.execute(
                "SELECT task FROM todos WHERE record_id=? AND done=1", (rid,))}
            self._db.execute("DELETE FROM todos WHERE record_id=? AND done=0", (rid,))
            for t in todos:
                if t["task"] in done:
                    continue
                self._db.execute(
                    "INSERT INTO todos (record_id, task, owner, due, created_at) VALUES (?,?,?,?,?)",
                    (rid, t["task"], t.get("owner"), t.get("due"), _now()),
                )

    def replace_case_todos(self, cid: int, todos: list[dict]) -> None:
        """건의 미완료 할 일을 통째로 갈아 끼운다(여러 기록에서 겹친 할 일 정리용).

        완료 처리한 항목은 보존하고, 새 항목은 건의 가장 최근 기록에 붙인다.
        """
        with self._lock, self._db:
            in_case = "record_id IN (SELECT id FROM records WHERE case_id=?)"
            done = {r["task"] for r in self._db.execute(
                f"SELECT task FROM todos WHERE done=1 AND {in_case}", (cid,))}
            last = self._db.execute(
                "SELECT id FROM records WHERE case_id=? AND data IS NOT NULL ORDER BY created_at DESC, id DESC"
                " LIMIT 1", (cid,)).fetchone()
            if not last:
                return
            self._db.execute(f"DELETE FROM todos WHERE done=0 AND {in_case}", (cid,))
            for t in todos:
                if t["task"] not in done:
                    self._db.execute(
                        "INSERT INTO todos (record_id, task, owner, due, created_at) VALUES (?,?,?,?,?)",
                        (last["id"], t["task"], t.get("owner"), t.get("due"), _now()))

    def done_tasks(self, cid: int) -> list[str]:
        with self._lock:
            return [r["task"] for r in self._db.execute(
                "SELECT t.task FROM todos t JOIN records r ON r.id=t.record_id"
                " WHERE r.case_id=? AND t.done=1 ORDER BY t.id", (cid,))]

    def open_todos(self) -> list[dict]:
        """미완료 할 일. 각 항목에 속한 건 번호(case_id)가 붙는다."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.*, r.case_id FROM todos t JOIN records r ON r.id=t.record_id"
                " WHERE t.done=0 ORDER BY (t.due IS NULL), t.due, t.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_todo(self, tid: int) -> dict | None:
        with self._lock, self._db:
            self._db.execute("UPDATE todos SET done=1 WHERE id=?", (tid,))
            r = self._db.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

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
