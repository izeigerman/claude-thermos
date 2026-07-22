import json
import queue
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from claude_thermos.lineage import LineageId


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


@dataclass
class _Write:
    """A line to append to a file, then flush as part of its batch."""

    file: TextIO
    line: str


@dataclass
class _Barrier:
    """A flush-and-signal request. The writer flushes the file and sets the
    event, so a caller waiting on it knows every earlier write to that file
    has reached disk."""

    file: TextIO
    done: threading.Event


@dataclass
class _Eof:
    """The shutdown sentinel. Enqueued by `close` to stop the drain loop
    once every earlier item has been processed."""


class EventWriter:
    """A single background thread that owns disk writes for any number of
    event logs.

    One writer serves every session's EventLog, so N concurrent sessions
    cost one writer thread rather than N. Callers hand it work through an
    unbounded queue and never block on disk: `write` enqueues an append,
    `flush` enqueues a barrier and waits for the writer to flush that one
    file.

    The writer drains the queue and processes whatever is immediately
    available as one batch: it appends every queued line, flushes each file
    the batch touched exactly once, then signals any barriers in the batch.
    That keeps flushing promptly when traffic is sparse (a lone record is
    its own batch) while coalescing flushes under load.

    An `_Eof` on the queue is the shutdown sentinel. `close` enqueues it and
    joins the thread, which guarantees every queued item is processed first.
    Because the thread is a daemon, `close` MUST be called to avoid losing
    items still in the queue at process exit.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[_Write | _Barrier | _Eof] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, name="eventlog-writer", daemon=True)
        self._thread.start()

    def write(self, file: TextIO, line: str) -> None:
        self._queue.put_nowait(_Write(file, line))

    def flush(self, file: TextIO) -> None:
        """Block until every earlier write to `file` has been flushed."""
        done = threading.Event()
        self._queue.put_nowait(_Barrier(file, done))
        done.wait()

    def close(self) -> None:
        self._queue.put_nowait(_Eof())
        self._thread.join()

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, _Eof):
                return
            batch = [item]
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(nxt, _Eof):
                    self._run_batch(batch)
                    return
                batch.append(nxt)
            self._run_batch(batch)

    def _run_batch(self, batch: list[_Write | _Barrier]) -> None:
        touched: list[TextIO] = []
        barriers: list[_Barrier] = []
        for item in batch:
            if isinstance(item, _Write):
                item.file.write(item.line)
            else:
                barriers.append(item)
            if item.file not in touched:
                touched.append(item.file)
        for file in touched:
            file.flush()
        for barrier in barriers:
            barrier.done.set()


class EventLog:
    """Structured JSONL event log with disk I/O kept off the caller's thread.

    `emit` only serializes the record and hands it to an EventWriter, so it
    never blocks on a write or flush. The actual disk I/O is owned by that
    writer's background thread. A single writer can be shared across every
    session's EventLog; when none is supplied a private one is created and
    owned by this log.

    `close` flushes this session's file through the writer — which
    guarantees every record emitted so far has reached disk — then closes
    the file and writes summary.json with the rollup totals accumulated from
    the emitted events. If this log owns its writer, `close` also shuts the
    writer down. Because the writer thread is a daemon, `close` MUST be
    called to avoid losing records still in flight at process exit.
    """

    def __init__(
        self, session_id: str, root: Path | None = None, writer: EventWriter | None = None
    ) -> None:
        root = root if root is not None else Path.home() / ".claude-thermos" / "logs"
        self._session_id = session_id
        self._session_dir = root / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = (self._session_dir / "events.jsonl").open("a")
        self._warms_fired = 0
        self._cache_read_total = 0
        self._owns_writer = writer is None
        self._writer = writer if writer is not None else EventWriter()

    def emit(self, event: EventType, lineage_id: LineageId, **fields: Any) -> None:
        if event is EventType.WARM_FIRED:
            self._warms_fired += 1
        elif event is EventType.WARM_RESULT:
            self._cache_read_total += fields.get("usage", {}).get("cache_read", 0)
        record = {"ts": round(time.time(), 3), "event": event, "lineage_id": lineage_id, **fields}
        self._writer.write(self._file, json.dumps(record) + "\n")

    def _write_summary(self) -> None:
        summary = {
            "warms_fired": self._warms_fired,
            "cache_read_total": self._cache_read_total,
        }
        (self._session_dir / "summary.json").write_text(json.dumps(summary))

    def close(self) -> None:
        self._writer.flush(self._file)
        self._file.close()
        self._write_summary()
        if self._owns_writer:
            self._writer.close()
