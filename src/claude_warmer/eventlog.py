import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO


class EventType(StrEnum):
    """Names of the structured events written to the event log. As a
    StrEnum each member serializes to its plain string value, so the
    on-disk JSONL format carries the raw event name."""

    SESSION_START = "session_start"
    LINEAGE_SEEN = "lineage_seen"
    USAGE = "usage"


class EventLog:
    def __init__(self, session_id: str, root: Path | None = None) -> None:
        root = root if root is not None else Path.home() / ".claude-warmer" / "logs"
        self._session_id = session_id
        self._session_dir = root / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = (self._session_dir / "events.jsonl").open("a")

    def emit(self, event: EventType, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def write_summary(self, **totals: Any) -> None:
        (self._session_dir / "summary.json").write_text(json.dumps(totals))

    def close(self) -> None:
        self._file.close()
