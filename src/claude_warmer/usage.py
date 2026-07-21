import json
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    uncached_input: int
    cache_read: int
    cache_creation: int
    output: int


def _iter_sse_data(chunks: Iterable[bytes]) -> Iterable[dict]:
    for chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data:"):
                yield json.loads(line[len("data:") :].strip())


def parse_usage_sse(chunks: Iterable[bytes]) -> Usage:
    """Accumulate usage from an Anthropic SSE stream: input/cache fields from
    the `message_start` event's message.usage, output_tokens from the final
    `message_delta` event's usage. Missing fields default to 0."""
    uncached_input = 0
    cache_read = 0
    cache_creation = 0
    output = 0
    for event in _iter_sse_data(chunks):
        message = event.get("message")
        if isinstance(message, dict):
            start_usage = message.get("usage", {})
            uncached_input = start_usage.get("input_tokens", 0)
            cache_read = start_usage.get("cache_read_input_tokens", 0)
            cache_creation = start_usage.get("cache_creation_input_tokens", 0)
        delta_usage = event.get("usage")
        if isinstance(delta_usage, dict) and "output_tokens" in delta_usage:
            output = delta_usage.get("output_tokens", 0)
    return Usage(uncached_input, cache_read, cache_creation, output)


def parse_usage_json(body: dict) -> Usage:
    """Extract the same four fields from a non-streaming /v1/messages JSON
    response's `usage` object (used for warm-request responses)."""
    usage = body.get("usage", {})
    return Usage(
        usage.get("input_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("output_tokens", 0),
    )
