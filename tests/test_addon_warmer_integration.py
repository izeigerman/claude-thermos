import json
from pathlib import Path

import httpx

from claude_thermos.config import Config
from claude_thermos.eventlog import EventLog
from claude_thermos.lineage import LineageId
from claude_thermos.proxy import WarmerAddon
from claude_thermos.warmer import _Episode


def _body(model: str, tool_count: int, session_id: str, system: str = "system prompt") -> dict:
    return {
        "model": model,
        "system": system,
        "tools": [{"name": f"tool-{i}"} for i in range(tool_count)],
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": json.dumps({"session_id": session_id})},
    }


_HEADERS = {"authorization": "Bearer abc"}
_MAIN = _body("claude-opus-4-8", 31, "sess-1")
_SUBAGENT = _body("claude-sonnet-5", 25, "sess-1", system="subagent system prompt")
_MAIN_ID = LineageId.from_request_body(_MAIN)
_SUBAGENT_ID = LineageId.from_request_body(_SUBAGENT)

_CONFIG = Config(idle_threshold_sec=270, warm_interval_sec=270, warm_max_cycles=2)

_USAGE_RESPONSE = {
    "input_tokens": 0,
    "cache_read_input_tokens": 272360,
    "cache_creation_input_tokens": 12,
    "output_tokens": 0,
}


def _recording_client(requests: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"usage": _USAGE_RESPONSE})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _read_events(tmp_path: Path, session_id: str = "sess-1") -> list[dict]:
    lines = (tmp_path / session_id / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _seed_idle_with_subagent(addon: WarmerAddon) -> None:
    for state, _ in addon.active_sessions():
        state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
        state.on_response(_MAIN_ID, now=0)
        state.on_request(_SUBAGENT_ID, _SUBAGENT, _HEADERS, now=100)


async def test_warmer_task_starts_and_fires(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    addon = WarmerAddon(
        config=_CONFIG,
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
        client=_recording_client(requests),
    )
    # Create the session, then drive it into the idle-with-active-subagent state.
    addon._get_or_create_session("sess-1")
    _seed_idle_with_subagent(addon)

    assert addon._warmer is not None
    for state, eventlog in addon.active_sessions():
        await addon._warmer.tick(state, eventlog, now=400)

    addon.done()

    assert len(requests) == 1
    names = [e["event"] for e in _read_events(tmp_path)]
    assert "warm_fired" in names


async def test_summary_written_on_shutdown(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    addon = WarmerAddon(
        config=_CONFIG,
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
        client=_recording_client(requests),
    )
    addon._get_or_create_session("sess-1")
    _seed_idle_with_subagent(addon)
    assert addon._warmer is not None
    for state, eventlog in addon.active_sessions():
        await addon._warmer.tick(state, eventlog, now=400)

    addon.done()

    names = [e["event"] for e in _read_events(tmp_path)]
    assert "session_end" in names

    summary = json.loads((tmp_path / "sess-1" / "summary.json").read_text())
    assert summary["warms_fired"] == 1
    assert summary["cache_read_total"] == 272360


def test_disabled_skips_warmer(tmp_path: Path) -> None:
    addon = WarmerAddon(
        config=Config(disabled=True),
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
    )
    assert addon._warmer is None


async def test_launcher_mode_does_not_start_reaper(tmp_path: Path) -> None:
    addon = WarmerAddon(
        config=Config(disabled=True),
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
    )
    addon.running()
    assert addon._reaper_task is None


async def test_daemon_mode_starts_reaper(tmp_path: Path) -> None:
    addon = WarmerAddon(
        config=Config(disabled=True),
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
        reap_sessions=True,
    )
    addon.running()
    assert addon._reaper_task is not None
    addon._reaper_task.cancel()


def test_reaper_evicts_idle_session(tmp_path: Path) -> None:
    config = Config(
        idle_threshold_sec=270,
        warm_interval_sec=270,
        warm_max_cycles=2,
        session_ttl_sec=1000,
    )
    addon = WarmerAddon(
        config=config,
        eventlog_factory=lambda sid: EventLog(sid, root=tmp_path),
        reap_sessions=True,
    )
    addon._get_or_create_session("sess-1")
    _seed_idle_with_subagent(addon)  # last activity at t=100
    assert addon._warmer is not None
    addon._warmer._episodes["sess-1"] = _Episode()  # would leak without forget()

    # Not stale yet: 999s < ttl.
    addon._evict_stale(now=100 + 999)
    assert "sess-1" in addon._sessions

    # Stale: 1000s >= ttl.
    addon._evict_stale(now=100 + 1000)
    assert "sess-1" not in addon._sessions
    assert "sess-1" not in addon._warmer._episodes

    names = [e["event"] for e in _read_events(tmp_path)]
    assert "session_end" in names
    assert (tmp_path / "sess-1" / "summary.json").exists()

    addon.done()  # idempotent: the evicted session is not torn down twice
