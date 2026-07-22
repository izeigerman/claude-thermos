from claude_thermos.lineage import LineageId
from claude_thermos.state import SessionState


def _body(model: str, tool_count: int, system: str = "system prompt") -> dict:
    return {
        "model": model,
        "system": system,
        "tools": [{"name": f"tool-{i}"} for i in range(tool_count)],
        "messages": [{"role": "user", "content": "hi"}],
    }


_HEADERS = {"authorization": "Bearer abc"}

_PING = _body("claude-opus-4-8", 0)
_MAIN = _body("claude-opus-4-8", 31)
_SUBAGENT = _body("claude-sonnet-5", 25, system="subagent system prompt")

_PING_ID = LineageId.from_request_body(_PING)
_MAIN_ID = LineageId.from_request_body(_MAIN)
_SUBAGENT_ID = LineageId.from_request_body(_SUBAGENT)


def test_main_is_first_substantive_lineage() -> None:
    state = SessionState("sess-1")
    state.on_request(_PING_ID, _PING, _HEADERS, now=0)
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=1)
    assert state.main_lineage_id() == _MAIN_ID


def test_main_idle_after_threshold() -> None:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    state.on_response(_MAIN_ID, now=0)
    state.on_request(_SUBAGENT_ID, _SUBAGENT, _HEADERS, now=100)
    state.on_response(_SUBAGENT_ID, now=110)
    assert state.is_main_idle(now=200, idle_threshold_sec=270) is False
    assert state.is_main_idle(now=300, idle_threshold_sec=270) is True


def test_main_not_idle_while_in_flight() -> None:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    assert state.is_main_idle(now=10_000, idle_threshold_sec=1) is False


def test_subagent_active_during_gap() -> None:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    state.on_response(_MAIN_ID, now=0)
    state.on_request(_SUBAGENT_ID, _SUBAGENT, _HEADERS, now=100)
    state.on_response(_SUBAGENT_ID, now=110)
    assert state.subagent_active(now=110, window_sec=540) is True
    assert state.subagent_active(now=110 + 540, window_sec=540) is False


def test_subagent_active_ignores_main() -> None:
    state = SessionState("sess-1")
    state.on_request(_MAIN_ID, _MAIN, _HEADERS, now=0)
    assert state.subagent_active(now=0, window_sec=540) is False
