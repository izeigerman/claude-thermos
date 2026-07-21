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
    SESSION_END = "session_end"
    LINEAGE_SEEN = "lineage_seen"
    USAGE = "usage"
    WARM_FIRED = "warm_fired"
    WARM_RESULT = "warm_result"
    WARM_ERROR = "warm_error"
    CAP_REACHED = "cap_reached"
    RESUME_DETECTED = "resume_detected"


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
    before the file is closed, then writes summary.json with the rollup
    totals accumulated from the emitted events. Because the writer is a
    daemon thread, `close` MUST be called to avoid losing records still in
    the queue at process exit.
    """

    def __init__(self, session_id: str, root: Path | None = None) -> None:
        root = root if root is not None else Path.home() / ".claude-warmer" / "logs"
        self._session_id = session_id
        self._session_dir = root / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = (self._session_dir / "events.jsonl").open("a")
        self._warms_fired = 0
        self._cache_read_total = 0
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._writer = threading.Thread(
            target=self._drain, name=f"eventlog-{session_id}", daemon=True
        )
        self._writer.start()

    def emit(self, event: EventType, lineage_id: LineageId, **fields: Any) -> None:
        if event is EventType.WARM_FIRED:
            self._warms_fired += 1
        elif event is EventType.WARM_RESULT:
            self._cache_read_total += fields.get("usage", {}).get("cache_read", 0)
        record = {"ts": round(time.time(), 3), "event": event, "lineage_id": lineage_id, **fields}
        self._queue.put_nowait(json.dumps(record) + "\n")

    def _write_summary(self) -> None:
        summary = {
            "warms_fired": self._warms_fired,
            "cache_read_total": self._cache_read_total,
        }
        (self._session_dir / "summary.json").write_text(json.dumps(summary))

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
        self._write_summary()
