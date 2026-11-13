"""SQLite 저장소: 기록(records), 할 일(todos), 처리한 녹음 파일(seen_files)."""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_SCHEMA = """
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
"""


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if "data" in d:
        d["data"] = json.loads(d["data"]) if d["data"] else None
    return d


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    # --- records ---
    def add_record(self, source: str, raw_text: str, data: dict | None = None,
                   audio_path: str | None = None, created_at: datetime | None = None) -> int:
        ts = (created_at or datetime.now()).isoformat(timespec="seconds")
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO records (created_at, source, raw_text, data, audio_path) VALUES (?,?,?,?,?)",
                (ts, source, raw_text, json.dumps(data, ensure_ascii=False) if data else None, audio_path),
            )
            rid = cur.lastrowid
        if data:
            self._sync_todos(rid, data["todos"])
        return rid

    def update_record(self, rid: int, *, raw_text: str | None = None, data: dict | None = None) -> None:
        with self._lock, self._db:
            if raw_text is not None:
                self._db.execute("UPDATE records SET raw_text=? WHERE id=?", (raw_text, rid))
            if data is not None:
                self._db.execute(
                    "UPDATE records SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), rid)
                )
        if data is not None:
            self._sync_todos(rid, data["todos"])

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

    def records_on(self, day: str) -> list[dict]:
        """day: YYYY-MM-DD"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE substr(created_at,1,10)=? ORDER BY created_at", (day,)
            ).fetchall()
        return [_row(r) for r in rows]

    # --- todos ---
    def _sync_todos(self, rid: int, todos: list[dict]) -> None:
        """재추출 시 완료 처리한 항목은 보존하고 미완료 항목만 갈아 끼운다."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._db:
            done = {r["task"] for r in self._db.execute(
                "SELECT task FROM todos WHERE record_id=? AND done=1", (rid,))}
            self._db.execute("DELETE FROM todos WHERE record_id=? AND done=0", (rid,))
            for t in todos:
                if t["task"] in done:
                    continue
                self._db.execute(
                    "INSERT INTO todos (record_id, task, owner, due, created_at) VALUES (?,?,?,?,?)",
                    (rid, t["task"], t.get("owner"), t.get("due"), now),
                )

    def open_todos(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM todos WHERE done=0 ORDER BY (due IS NULL), due, id"
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
            self._db.execute(
                "INSERT OR IGNORE INTO seen_files (path, seen_at) VALUES (?,?)",
                (path, datetime.now().isoformat(timespec="seconds")),
            )
