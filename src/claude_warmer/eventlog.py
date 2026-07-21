import json
import queue
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from claude_warmer.lineage import LineageId


class EventType(StrEnum):
    """Names of the structured events written to the event log. As a
    StrEnum each member serializes to its plain string value, so the
    on-disk JSONL format carries the raw event name."""

    SESSION_START = "session_start"
    LINEAGE_SEEN = "lineage_seen"
    USAGE = "usage"


class EventLog:
    """Structured JSONL event log with disk I/O kept off the caller's thread.

    `emit` only serializes the record and hands it to an unbounded queue,
    so it never blocks on a write or flush. A single background writer
    thread owns the file: it drains the queue, writes whatever records are
    immediately available as one batch, and flushes once per batch. That
    keeps the proxy event loop from stalling on disk while still flushing
    promptly when traffic is sparse (a lone record is its own batch).

    A `None` on the queue is the shutdown sentinel. `close` enqueues it and
    joins the writer, which guarantees every queued record is flushed
    before the file is closed. Because the writer is a daemon thread,
    `close` MUST be called to avoid losing records still in the queue at
    process exit.
    """

    def __init__(self, session_id: str, root: Path | None = None) -> None:
        root = root if root is not None else Path.home() / ".claude-warmer" / "logs"
        self._session_id = session_id
        self._session_dir = root / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = (self._session_dir / "events.jsonl").open("a")
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._writer = threading.Thread(
            target=self._drain, name=f"eventlog-{session_id}", daemon=True
        )
        self._writer.start()

    def emit(self, event: EventType, lineage_id: LineageId, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "event": event, "lineage_id": lineage_id, **fields}
        self._queue.put_nowait(json.dumps(record) + "\n")

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            batch = [item]
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._flush(batch)
                    return
                batch.append(nxt)
            self._flush(batch)

    def _flush(self, batch: list[str]) -> None:
        for line in batch:
            self._file.write(line)
        self._file.flush()

    def close(self) -> None:
        self._queue.put_nowait(None)
        self._writer.join()
        self._file.close()
