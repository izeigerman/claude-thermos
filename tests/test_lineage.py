import json

from claude_thermos.lineage import LineageId, extract_session_id


def _body(
    model: str,
    tool_names: list[str],
    system: str | list[dict[str, object]],
    session_id: str | None = "sess-main",
) -> dict:
    body: dict = {
        "model": model,
        "system": system,
        "tools": [{"name": name} for name in tool_names],
        "messages": [{"role": "user", "content": "hi"}],
    }
    if session_id is not None:
        body["metadata"] = {"user_id": json.dumps({"session_id": session_id, "org_id": "org-1"})}
    return body


def test_session_id_extracted() -> None:
    body = _body("claude-opus-4-8", ["Read"], "you are helpful", session_id="sess-main")
    assert extract_session_id(body) == "sess-main"


def test_session_id_missing_returns_none() -> None:
    body_no_metadata = _body("claude-opus-4-8", ["Read"], "you are helpful", session_id=None)
    assert extract_session_id(body_no_metadata) is None

    body_no_session_id = _body("claude-opus-4-8", ["Read"], "you are helpful", session_id=None)
    body_no_session_id["metadata"] = {"user_id": json.dumps({"org_id": "org-1"})}
    assert extract_session_id(body_no_session_id) is None


def test_from_request_body_returns_lineage_id() -> None:
    body = _body("claude-opus-4-8", ["Read"], "you are helpful")
    assert isinstance(LineageId.from_request_body(body), LineageId)


def test_main_vs_subagent_differ() -> None:
    main_body = _body("claude-opus-4-8", [f"tool-{i}" for i in range(31)], "main system prompt")
    subagent_body = _body(
        "claude-sonnet-5", [f"stool-{i}" for i in range(25)], "subagent system prompt"
    )
    assert LineageId.from_request_body(main_body) != LineageId.from_request_body(subagent_body)


def test_main_lineage_stable_across_turns() -> None:
    tool_names = ["Read", "Write", "Bash"]
    turn_one = _body("claude-opus-4-8", tool_names, "you are helpful")
    turn_one["messages"] = [{"role": "user", "content": "first turn"}]
    turn_two = _body("claude-opus-4-8", tool_names, "you are helpful")
    turn_two["messages"] = [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "second turn"},
    ]
    assert LineageId.from_request_body(turn_one) == LineageId.from_request_body(turn_two)


def test_tool_order_independent() -> None:
    tool_names = ["Read", "Write", "Bash"]
    body_a = _body("claude-opus-4-8", tool_names, "you are helpful")
    body_b = _body("claude-opus-4-8", list(reversed(tool_names)), "you are helpful")
    assert LineageId.from_request_body(body_a) == LineageId.from_request_body(body_b)


def test_system_as_str_or_blocks() -> None:
    tool_names = ["Read", "Write"]
    body_str = _body("claude-opus-4-8", tool_names, "you are helpful")
    body_blocks = _body(
        "claude-opus-4-8",
        tool_names,
        [
            {"type": "text", "text": "you are helpful", "cache_control": {"type": "ephemeral"}},
        ],
    )
    assert LineageId.from_request_body(body_str) == LineageId.from_request_body(body_blocks)
