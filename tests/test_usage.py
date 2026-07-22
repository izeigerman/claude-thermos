import json

from claude_thermos.usage import Usage, parse_usage_json, parse_usage_sse


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def test_parse_sse_reads_start_and_delta() -> None:
    chunks = [
        _sse_event(
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
        ),
        _sse_event("content_block_delta", {"delta": {"text": "hi"}}),
        _sse_event("message_delta", {"usage": {"output_tokens": 1286}}),
    ]
    assert parse_usage_sse(chunks) == Usage(2, 157547, 4520, 1286)


def test_parse_sse_missing_fields_zero() -> None:
    chunks = [
        _sse_event("message_start", {"message": {"usage": {"input_tokens": 5}}}),
    ]
    assert parse_usage_sse(chunks) == Usage(5, 0, 0, 0)


def test_parse_json_warm_response() -> None:
    body = {
        "usage": {
            "input_tokens": 0,
            "cache_read_input_tokens": 272360,
            "cache_creation_input_tokens": 12,
            "output_tokens": 0,
        }
    }
    assert parse_usage_json(body) == Usage(0, 272360, 12, 0)
