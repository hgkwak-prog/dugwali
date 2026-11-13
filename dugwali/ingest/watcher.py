"""통화 녹음 폴더 감시. 네트워크 드라이브(NAS)에서는 파일 이벤트가 불안정해 주기적으로 훑는다.

- 앱을 켠 뒤에 생긴 파일만 처리한다(기존 녹음 수백 개를 한꺼번에 돌리지 않도록).
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
                 interval: float = 30.0, since: float | None = None) -> None:
        self.folder, self.store, self.on_file, self.interval = folder, store, on_file, interval
        self.since = time.time() if since is None else since
        self._sizes: dict[str, int] = {}

    def scan_once(self) -> list[Path]:
        """처리한 파일 목록을 돌려준다."""
        handled = []
        for p in sorted(self.folder.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
                continue
            key = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < self.since or self.store.is_seen(key):
                continue
            if self._sizes.get(key) != st.st_size or st.st_size == 0:
                self._sizes[key] = st.st_size  # 아직 쓰는 중일 수 있음 → 다음 주기에 재확인
                continue
            self.store.mark_seen(key)
            self._sizes.pop(key, None)
            try:
                self.on_file(p)
            except Exception:
                log.exception("통화 녹음 처리 실패: %s", p)
            handled.append(p)
        return handled

    def run_forever(self) -> None:
        log.info("통화 녹음 감시 시작: %s", self.folder)
        while True:
            try:
                self.scan_once()
            except Exception:
                log.exception("폴더 스캔 실패(NAS 연결 확인)")
            time.sleep(self.interval)
