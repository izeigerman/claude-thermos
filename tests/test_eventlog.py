import json
from pathlib import Path

from claude_thermos.eventlog import EventLog, EventType
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
