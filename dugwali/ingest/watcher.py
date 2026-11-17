"""통화 녹음 폴더 감시. 네트워크 드라이브(NAS)에서는 파일 이벤트가 불안정해 주기적으로 훑는다.

- 앱을 켠 뒤에 생긴 파일만 자동으로 처리한다(기존 녹음 수백 개를 한꺼번에 돌리지 않도록).
- 처음 설치한 시점(baseline) 이후에 생겼는데 앱이 꺼져 있었거나 늦게 동기화돼 놓친 파일은
  자동으로 처리하지 않고 `on_missed`로 알려 사용자가 정하게 한다. 건너뛰지 않는 한 켤 때마다 다시 묻는다.
- 동기화 중인 파일을 집지 않도록 크기가 두 번 연속 같을 때만 처리한다.
"""
import logging
import time
from pathlib import Path
from typing import Callable

from ..store import Store

log = logging.getLogger(__name__)
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".amr", ".3gp", ".ogg", ".aac", ".mp4", ".webm", ".flac"}


class CallWatcher:
    def __init__(self, folder: Path, store: Store, on_file: Callable[[Path], None],
                 interval: float = 30.0, since: float | None = None, baseline: float | None = None,
                 on_missed: Callable[[list[Path]], None] | None = None) -> None:
        self.folder, self.store, self.on_file, self.interval = folder, store, on_file, interval
        self.since = time.time() if since is None else since
        self.baseline = self.since if baseline is None else min(baseline, self.since)
        self.on_missed = on_missed
        self._sizes: dict[str, int] = {}
        self._asked: set[str] = set()

    def _stable(self, key: str, size: int) -> bool:
        """크기가 직전 관측과 같으면 다 쓰인 파일로 본다."""
        if self._sizes.get(key) != size or size == 0:
            self._sizes[key] = size  # 아직 쓰는 중일 수 있음 → 다음 주기에 재확인
            return False
        return True

    def missed(self) -> list[Path]:
        """놓친 녹음(설치 이후 ~ 이번 실행 전에 생겼고 아직 처리하지 않은 파일)."""
        out = []
        for p in sorted(self.folder.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if self.baseline <= mtime < self.since and not self.store.is_seen(str(p)):
                out.append(p)
        return out

    def skip_missed(self) -> int:
        paths = self.missed()
        for p in paths:
            self.store.mark_seen(str(p))
        return len(paths)

    def scan_once(self) -> list[Path]:
        """처리한 파일 목록을 돌려준다."""
        handled, newly_missed = [], []
        for p in sorted(self.folder.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
                continue
            key = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < self.baseline or self.store.is_seen(key):
                continue
            if st.st_mtime < self.since:  # 놓친 파일: 자동 처리하지 않고 한 번만 알린다
                if key not in self._asked and self._stable(key, st.st_size):
                    self._asked.add(key)
                    self._sizes.pop(key, None)
                    newly_missed.append(p)
                continue
            if not self._stable(key, st.st_size):
                continue
            self.store.mark_seen(key)
            self._sizes.pop(key, None)
            try:
                self.on_file(p)
            except Exception:
                log.exception("통화 녹음 처리 실패: %s", p)
            handled.append(p)
        if newly_missed and self.on_missed:
            try:
                self.on_missed(newly_missed)
            except Exception:
                log.exception("놓친 통화 녹음 알림 실패")
                self._asked -= {str(p) for p in newly_missed}  # 다음 주기에 다시 알린다
        return handled

    def run_forever(self) -> None:
        log.info("통화 녹음 감시 시작: %s", self.folder)
        while True:
            try:
                self.scan_once()
            except Exception:
                log.exception("폴더 스캔 실패(NAS 연결 확인)")
            time.sleep(self.interval)
