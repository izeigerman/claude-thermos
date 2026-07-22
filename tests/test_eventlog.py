import json
from pathlib import Path

from claude_thermos.eventlog import EventLog, EventType, EventWriter
from claude_thermos.lineage import LineageId


def test_emit_writes_one_json_line_per_event(tmp_path: Path) -> None:
    log = EventLog("sess-1", root=tmp_path)
    log.emit(EventType.SESSION_START, LineageId("lineage-a"))
    log.emit(EventType.LINEAGE_SEEN, LineageId("lineage-a"), has_tools=True)
    log.close()

    lines = (tmp_path / "sess-1" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert isinstance(first["ts"], float)
    # The enum serializes to its plain string value on disk.
    assert first["event"] == "session_start"
    assert first["lineage_id"] == "lineage-a"

    second = json.loads(lines[1])
    assert isinstance(second["ts"], float)
    assert second["event"] == "lineage_seen"
    assert second["lineage_id"] == "lineage-a"
    assert second["has_tools"] is True


def test_path_layout(tmp_path: Path) -> None:
    log = EventLog("sess-2", root=tmp_path)
    log.emit(EventType.SESSION_START, LineageId("lineage-a"))
    log.close()

    assert (tmp_path / "sess-2" / "events.jsonl").is_file()


def test_usage_event_shape(tmp_path: Path) -> None:
    log = EventLog("sess-4", root=tmp_path)
    usage = {
        "uncached_input": 2,
        "cache_read": 157547,
        "cache_creation": 4520,
        "output": 1286,
    }
    log.emit(EventType.USAGE, LineageId("lineage-a"), usage=usage)
    log.close()

    line = (tmp_path / "sess-4" / "events.jsonl").read_text().splitlines()[0]
    event = json.loads(line)
    assert event["event"] == "usage"
    assert event["lineage_id"] == "lineage-a"
    assert event["usage"] == usage


def test_close_writes_summary_with_rollup_totals(tmp_path: Path) -> None:
    log = EventLog("sess-5", root=tmp_path)
    lineage = LineageId("lineage-a")
    log.emit(EventType.WARM_FIRED, lineage, cycle=1)
    log.emit(EventType.WARM_RESULT, lineage, cycle=1, usage={"cache_read": 100})
    log.emit(EventType.WARM_FIRED, lineage, cycle=2)
    log.emit(EventType.WARM_RESULT, lineage, cycle=2, usage={"cache_read": 250})
    log.close()

    summary = json.loads((tmp_path / "sess-5" / "summary.json").read_text())
    assert summary == {"warms_fired": 2, "cache_read_total": 350}


def test_shared_writer_serves_multiple_sessions(tmp_path: Path) -> None:
    writer = EventWriter()
    log_a = EventLog("sess-a", root=tmp_path, writer=writer)
    log_b = EventLog("sess-b", root=tmp_path, writer=writer)

    log_a.emit(EventType.SESSION_START, LineageId("lineage-a"))
    log_b.emit(EventType.SESSION_START, LineageId("lineage-b"))
    log_a.emit(EventType.WARM_FIRED, LineageId("lineage-a"), cycle=1)

    log_a.close()
    log_b.close()
    writer.close()

    events_a = (tmp_path / "sess-a" / "events.jsonl").read_text().splitlines()
    events_b = (tmp_path / "sess-b" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in events_a] == ["session_start", "warm_fired"]
    assert [json.loads(line)["event"] for line in events_b] == ["session_start"]
