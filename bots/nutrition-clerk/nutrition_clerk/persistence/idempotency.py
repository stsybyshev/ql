from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

MAX_SEEN = 500


class Idempotency:
    """Crash-safe store of {Telegram offset, ring of recently-seen msg_ids}.

    Writes are atomic (tmp + fsync + os.replace). Commit only AFTER the handler
    has finished and the reply has been sent — that gives at-least-once
    delivery with dedup, effectively exactly-once for logging.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._offset = 0
        self._seen: deque[int] = deque(maxlen=MAX_SEEN)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self._offset = int(data.get("offset", 0))
        for msg_id in data.get("seen", []):
            self._seen.append(int(msg_id))

    @property
    def offset(self) -> int:
        return self._offset

    def seen(self, msg_id: int) -> bool:
        return msg_id in self._seen

    def commit(self, offset: int, msg_id: int) -> None:
        if msg_id in self._seen:
            return
        self._seen.append(msg_id)
        self._offset = max(self._offset, offset)
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"offset": self._offset, "seen": list(self._seen)}
        with tmp.open("w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
