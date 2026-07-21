import json
from collections.abc import Callable
from pathlib import Path

import httpx

from claude_warmer.config import Config
from claude_warmer.eventlog import EventLog
from claude_warmer.lineage import LineageId
from claude_warmer.state import SessionState
from claude_warmer.warmer import Warmer, build_warm_request


def _body(model: str, tool_count: int, system: str = "system prompt") -> dict:
    return {
        "model": model,
        "system": system,
        "tools": [{"name": f"tool-{i}"} for i in range(tool_count)],
        "messages": [{"role": "user", "content": "hi"}],
    }


_HEADERS = {"authorization": "Bearer abc"}
_MAIN = _body("claude-opus-4-8", 31)
_SUBAGENT = _body("claude-sonnet-5", 25, system="subagent system prompt")
_MAIN_ID = LineageId.from_request_body(_MAIN)
_SUBAGENT_ID = LineageId.from_request_body(_SUBAGENT)

_CONFIG = Config(
    idle_threshold_sec=270,
    warm_interval_sec=270,
    warm_max_cycles=2,
    subagent_active_window_sec=540,
)

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


def _client_from(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _idle_session() -> SessionState:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    state.on_response(_MAIN_ID, now=0)
    state.on_request(_SUBAGENT_ID, _SUBAGENT, _HEADERS, now=100)
    return state


def _events(tmp_path: Path) -> EventLog:
    return EventLog("sess-1", root=tmp_path)


def _read_events(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "sess-1" / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _event_names(tmp_path: Path) -> list[str]:
    return [e["event"] for e in _read_events(tmp_path)]


async def test_warms_when_idle_and_subagent_active(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(_CONFIG, client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    log.close()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/messages"
    assert requests[0].headers["authorization"] == "Bearer abc"
    assert json.loads(requests[0].content)["max_tokens"] == 0

    events = _read_events(tmp_path)
    names = [e["event"] for e in events]
    assert "warm_fired" in names
    result = next(e for e in events if e["event"] == "warm_result")
    assert result["usage"]["cache_read"] == 272360


async def test_respects_interval(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(_CONFIG, client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    await warmer.tick(state, log, now=500)
    log.close()

    assert len(requests) == 1


async def test_cap_stops_warming(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(_CONFIG, client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    await warmer.tick(state, log, now=700)
    await warmer.tick(state, log, now=1000)
    await warmer.tick(state, log, now=1300)
    log.close()

    assert len(requests) == 2
    assert _event_names(tmp_path).count("cap_reached") == 1


async def test_auto_ignores_cap(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(Config(warm_max_cycles=None), client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    await warmer.tick(state, log, now=700)
    await warmer.tick(state, log, now=1000)
    log.close()

    assert len(requests) == 3


async def test_resume_resets_and_stops(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(_CONFIG, client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=410)
    await warmer.tick(state, log, now=500)
    await warmer.tick(state, log, now=800)
    log.close()

    assert len(requests) == 1
    assert "resume_detected" in _event_names(tmp_path)


async def test_no_warm_without_subagent(tmp_path: Path) -> None:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    state.on_response(_MAIN_ID, now=0)
    log = _events(tmp_path)
    requests: list[httpx.Request] = []
    warmer = Warmer(_CONFIG, client=_recording_client(requests))

    await warmer.tick(state, log, now=400)
    log.close()

    assert len(requests) == 0


async def test_warm_error_on_http_error(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )

    warmer = Warmer(_CONFIG, client=_client_from(handler))

    await warmer.tick(state, log, now=400)
    log.close()

    events = _read_events(tmp_path)
    names = [e["event"] for e in events]
    assert "warm_fired" in names
    assert "warm_result" not in names
    error = next(e for e in events if e["event"] == "warm_error")
    assert error["error"] == "HTTP 429: slow down"


async def test_warm_error_on_network_error(tmp_path: Path) -> None:
    state = _idle_session()
    log = _events(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    warmer = Warmer(_CONFIG, client=_client_from(handler))

    await warmer.tick(state, log, now=400)
    log.close()

    events = _read_events(tmp_path)
    names = [e["event"] for e in events]
    assert "warm_result" not in names
    error = next(e for e in events if e["event"] == "warm_error")
    assert "boom" in error["error"]


def _stored_body() -> dict:
    return {
        "model": "claude-opus-4-8",
        "system": [
            {"type": "text", "text": "you are helpful"},
            {"type": "text", "text": "long prefix", "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [{"name": "Read"}, {"name": "Edit"}],
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "abc",
                        "content": "done",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ],
        "max_tokens": 4096,
        "thinking": {"type": "adaptive"},
        "stream": True,
        "output_config": {"foo": "bar"},
        "context_management": {"edits": []},
    }


def test_build_warm_request_preserves_prefix() -> None:
    body = _stored_body()
    warm = build_warm_request(body)
    assert warm["system"] == body["system"]
    assert warm["tools"] == body["tools"]
    assert warm["messages"] == body["messages"]


def test_build_warm_request_neutralizes_generation_params() -> None:
    warm = build_warm_request(_stored_body())
    assert warm["max_tokens"] == 0
    assert warm["stream"] is False
    assert warm.get("thinking") in (None, {"type": "disabled"})
    assert "output_config" not in warm
    assert "context_management" not in warm


def test_build_warm_request_does_not_mutate_input() -> None:
    body = _stored_body()
    original = _stored_body()
    build_warm_request(body)
    assert body == original


def test_build_warm_request_removes_forcing_tool_choice() -> None:
    body = _stored_body()
    body["tool_choice"] = {"type": "any"}
    warm = build_warm_request(body)
    assert warm.get("tool_choice", {}).get("type") not in ("any", "tool")
