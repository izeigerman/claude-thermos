import json
import socket
from collections.abc import MutableMapping
from pathlib import Path

from claude_warmer.eventlog import EventLog
from claude_warmer.lineage import LineageId
from claude_warmer.proxy import (
    WarmerAddon,
    build_master,
    find_free_port,
    _handle_request,
    _handle_response,
)
from claude_warmer.state import SessionState


def test_find_free_port_bindable() -> None:
    port = find_free_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_build_master_reverse_options() -> None:
    port = find_free_port()

    master = build_master(port, None)

    assert master.options.mode == ["reverse:https://api.anthropic.com"]
    assert master.options.listen_host == "127.0.0.1"
    assert master.options.listen_port == port


def test_build_master_registers_addon() -> None:
    port = find_free_port()
    sentinel = object()

    master = build_master(port, sentinel)

    assert any(a is sentinel for a in master.addons.chain)


_HEADERS = {"authorization": "Bearer abc"}


def _body(model: str, tool_count: int, system: str = "system prompt") -> dict:
    return {
        "model": model,
        "system": system,
        "tools": [{"name": f"tool-{i}"} for i in range(tool_count)],
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": json.dumps({"session_id": "sess-1"})},
    }


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _events(tmp_path: Path, session_id: str = "sess-1") -> list[dict]:
    lines = (tmp_path / session_id / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_handle_request_emits_session_and_lineage(tmp_path: Path) -> None:
    state = SessionState("sess-1")
    eventlog = EventLog("sess-1", root=tmp_path)
    seen_lineages: set[LineageId] = set()
    main = _body("claude-opus-4-8", 12)

    lineage = _handle_request(state, eventlog, main, _HEADERS, now=0, seen_lineages=seen_lineages)

    assert state.main_lineage_id() == lineage

    # A second request on the same lineage must not re-emit either event.
    _handle_request(state, eventlog, main, _HEADERS, now=1, seen_lineages=seen_lineages)
    eventlog.close()

    events = _events(tmp_path)
    session_start = [e for e in events if e["event"] == "session_start"]
    lineage_seen = [e for e in events if e["event"] == "lineage_seen"]
    assert len(session_start) == 1
    assert session_start[0]["lineage_id"] == lineage
    assert len(lineage_seen) == 1
    assert lineage_seen[0]["lineage_id"] == lineage


def test_handle_response_emits_usage(tmp_path: Path) -> None:
    state = SessionState("sess-1")
    eventlog = EventLog("sess-1", root=tmp_path)
    seen_lineages: set[LineageId] = set()
    main = _body("claude-opus-4-8", 12)

    lineage = _handle_request(state, eventlog, main, _HEADERS, now=0, seen_lineages=seen_lineages)
    assert state.is_main_idle(now=0, idle_threshold_sec=0) is False  # request still in flight

    raw_response = _sse_event(
        "message_start",
        {
            "message": {
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 157547,
                    "cache_creation_input_tokens": 4520,
                }
            }
        },
    ) + _sse_event("message_delta", {"usage": {"output_tokens": 1286}})

    _handle_response(state, eventlog, lineage, raw_response, now=0)

    assert state.is_main_idle(now=0, idle_threshold_sec=0) is True  # in_flight decremented to 0
    eventlog.close()

    events = _events(tmp_path)
    usage_events = [e for e in events if e["event"] == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["lineage_id"] == lineage
    assert usage_events[0]["usage"] == {
        "uncached_input": 2,
        "cache_read": 157547,
        "cache_creation": 4520,
        "output": 1286,
    }


def test_no_session_id_excluded_from_main(tmp_path: Path) -> None:
    state = SessionState("sess-1")
    eventlog = EventLog("sess-1", root=tmp_path)
    seen_lineages: set[LineageId] = set()

    # A title/quota ping: no tools, and no session metadata at all.
    ping = {
        "model": "claude-opus-4-8",
        "tools": [],
        "messages": [{"role": "user", "content": "hi"}],
    }
    main = _body("claude-opus-4-8", 12)

    _handle_request(state, eventlog, ping, _HEADERS, now=0, seen_lineages=seen_lineages)
    assert state.main_lineage_id() is None

    _handle_request(state, eventlog, main, _HEADERS, now=1, seen_lineages=seen_lineages)
    assert state.main_lineage_id() == LineageId.from_request_body(main)

    eventlog.close()


class _FakeRequest:
    """Duck-typed stand-in for mitmproxy's Request: exposes only the
    attributes WarmerAddon's hooks read/write (`path`, `headers`,
    `get_text()`)."""

    def __init__(self, path: str, text: str, headers: dict[str, str] | None = None) -> None:
        self.path = path
        self.headers: MutableMapping[str, str] = dict(headers) if headers else {}
        self._text = text

    def get_text(self) -> str:
        return self._text


class _FakeResponse:
    """Duck-typed stand-in for mitmproxy's Response: exposes only `content`."""

    def __init__(self, content: bytes) -> None:
        self.content = content


class _FakeFlow:
    """Duck-typed stand-in for mitmproxy's HTTPFlow: exposes only `request`
    and `response`."""

    def __init__(self, request: _FakeRequest, response: _FakeResponse | None = None) -> None:
        self.request = request
        self.response = response


def _addon(tmp_path: Path) -> WarmerAddon:
    return WarmerAddon(eventlog_factory=lambda session_id: EventLog(session_id, root=tmp_path))


def test_request_forces_accept_encoding_identity(tmp_path: Path) -> None:
    addon = _addon(tmp_path)
    flow = _FakeFlow(
        request=_FakeRequest(
            path="/v1/messages",
            text=json.dumps(_body("claude-opus-4-8", 12)),
            headers={"accept-encoding": "gzip"},
        )
    )

    addon.request(flow)

    assert flow.request.headers["accept-encoding"] == "identity"


def test_request_with_no_session_id_passes_through(tmp_path: Path) -> None:
    addon = _addon(tmp_path)
    ping = {
        "model": "claude-opus-4-8",
        "tools": [],
        "messages": [{"role": "user", "content": "hi"}],
    }
    flow = _FakeFlow(request=_FakeRequest(path="/v1/messages", text=json.dumps(ping)))

    addon.request(flow)  # must not raise, and must not create any session state

    assert list(tmp_path.iterdir()) == []


def test_request_with_invalid_json_body_does_not_raise(tmp_path: Path) -> None:
    addon = _addon(tmp_path)
    flow = _FakeFlow(request=_FakeRequest(path="/v1/messages", text="not json"))

    addon.request(flow)  # must not raise

    assert list(tmp_path.iterdir()) == []


def test_request_response_round_trip_emits_events(tmp_path: Path) -> None:
    addon = _addon(tmp_path)
    main = _body("claude-opus-4-8", 12)
    flow = _FakeFlow(request=_FakeRequest(path="/v1/messages", text=json.dumps(main)))

    addon.request(flow)
    flow.response = _FakeResponse(
        content=_sse_event(
            "message_start",
            {
                "message": {
                    "usage": {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    }
                }
            },
        )
        + _sse_event("message_delta", {"usage": {"output_tokens": 5}})
    )
    addon.response(flow)

    events = _events(tmp_path)
    event_types = [e["event"] for e in events]
    assert "session_start" in event_types
    assert "lineage_seen" in event_types
    assert "usage" in event_types
