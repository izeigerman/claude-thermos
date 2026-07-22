import asyncio
import copy
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

from claude_thermos.config import ANTHROPIC_BASE_URL, Config
from claude_thermos.eventlog import EventLog, EventType
from claude_thermos.state import LineageState, SessionState
from claude_thermos.usage import Usage, parse_usage_json

_DEFAULT_POLL_INTERVAL_SEC = 5.0
_WARM_MAX_TOKENS = 1
_WARM_TIMEOUT_SEC = 60.0

# Captured request headers that must not be replayed on the warm request: the
# host header points at the local proxy and content-length describes the
# original body, so both are wrong for a direct call with a rewritten body.
# httpx recomputes them for us once they are removed.
_STRIPPED_HEADERS = frozenset({"host", "content-length"})


def build_warm_request(body: dict) -> dict:
    """Turn a stored real request body into a minimal-output warm request.

    The entire cacheable prefix is preserved exactly — model, system,
    tools, messages, thinking, tool_choice, and context_management,
    including every cache_control breakpoint — so the warm request reads
    and refreshes every cache tier the real request uses, message history
    included. The prompt cache keys the message tier on thinking state and
    tool_choice, so stripping either would leave the real lineage's message
    cache to expire; only tools and system survive that. Keeping them
    intact is what makes the warm preserve the whole prefix.

    Only generation is neutralized: max_tokens is capped at 1, streaming is
    disabled, and output_config is removed. max_tokens is 1 rather than 0
    because a zero-token request is rejected when thinking is enabled, and
    thinking must stay enabled to keep the message tier warm. The single
    generated token is discarded — the cache is written during prefill,
    before generation. output_config is not part of the cache key, and
    dropping it keeps a structured-output format from constraining the
    one-token reply.

    Args:
        body: The stored last real main request body. Not mutated.

    Returns:
        A new request body suitable for cache warming.
    """
    warm = copy.deepcopy(body)
    warm["max_tokens"] = _WARM_MAX_TOKENS
    warm["stream"] = False
    warm.pop("output_config", None)
    return warm


@dataclass
class _Episode:
    """Per-session warming state for a single idle-with-active-subagent
    episode. Reset when the main lineage resumes or the subagent goes
    inactive."""

    warm_count: int = 0
    last_warm_sent: float = float("-inf")
    in_progress: bool = False
    cap_emitted: bool = False
    main_request_count: int | None = None


class Warmer:
    """Periodic driver that warms the main lineage's cache prefix while a
    subagent runs.

    `tick` makes the warm decision for one session at an injected `now`;
    `run` polls all active sessions on an interval. Per-session episode
    state enforces the interval, the cycle cap, one-warm-at-a-time, and
    reset on resume or subagent inactivity.

    Warm requests are sent directly to the Anthropic API through the owned
    httpx client (injectable for testing), never through the proxy, so they
    can never touch Claude Code's traffic.
    """

    def __init__(
        self,
        config: Config,
        base_url: str = ANTHROPIC_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._base_url = base_url.rstrip("/")
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(timeout=httpx.Timeout(_WARM_TIMEOUT_SEC))
        )
        self._episodes: dict[str, _Episode] = {}

    async def tick(self, state: SessionState, eventlog: EventLog, now: float) -> None:
        """Evaluate one session and warm the main lineage if warranted.

        Warms only when the main lineage is idle, a subagent is active, at
        least warm_interval_sec has passed since the last warm-or-real main
        request, and the episode's cycle count is under warm_max_cycles (or
        it is None). Emits warm_fired / warm_result / warm_error on a warm,
        cap_reached when the cap is hit, and resume_detected when the main
        lineage sends a new real request.

        Args:
            state: The session's state.
            eventlog: The session's event log.
            now: Current time, in seconds.
        """
        main = state.main_lineage()
        if main is None:
            return
        episode = self._episodes.setdefault(state.session_id, _Episode())

        if (
            episode.main_request_count is not None
            and main.request_count > episode.main_request_count
        ):
            eventlog.emit(EventType.RESUME_DETECTED, lineage_id=main.lineage_id)
            self._reset(episode)
            return

        idle = state.is_main_idle(now, self._config.idle_threshold_sec)
        active = state.subagent_active(now, self._config.subagent_active_window_sec)
        if not (idle and active):
            if not active:
                self._reset(episode)
            return

        if episode.main_request_count is None:
            episode.main_request_count = main.request_count

        max_cycles = self._config.warm_max_cycles
        if max_cycles is not None and episode.warm_count >= max_cycles:
            if not episode.cap_emitted:
                eventlog.emit(
                    EventType.CAP_REACHED, lineage_id=main.lineage_id, cycle=episode.warm_count
                )
                episode.cap_emitted = True
            return

        last_activity = max(main.last_request_sent, episode.last_warm_sent)
        if now - last_activity < self._config.warm_interval_sec:
            return

        if episode.in_progress or main.last_request_body is None:
            return

        await self._fire(episode, main, main.last_request_body, eventlog, now)

    async def _fire(
        self, episode: _Episode, main: LineageState, body: dict, eventlog: EventLog, now: float
    ) -> None:
        episode.in_progress = True
        try:
            cycle = episode.warm_count + 1
            eventlog.emit(
                EventType.WARM_FIRED,
                lineage_id=main.lineage_id,
                cycle=cycle,
                idle_for_sec=round(now - main.last_response_end, 3),
                subagent_active=True,
            )
            result = await self._send(body, main.auth_headers)
            if isinstance(result, Usage):
                eventlog.emit(
                    EventType.WARM_RESULT,
                    lineage_id=main.lineage_id,
                    cycle=cycle,
                    usage=_usage_dict(result),
                    ttl_refreshed=True,
                )
            else:
                eventlog.emit(
                    EventType.WARM_ERROR, lineage_id=main.lineage_id, cycle=cycle, error=result
                )
            episode.warm_count += 1
            episode.last_warm_sent = now
        finally:
            episode.in_progress = False

    async def _send(self, body: dict, headers: dict) -> Usage | str:
        """POST a warm request built from `body`, failing quiet.

        Any failure — a non-2xx status, a network error, a timeout, or an
        unexpected error while building or parsing — is swallowed and
        surfaced as an error string, never raised, so a failed warm can
        neither disturb real traffic nor kill the warming loop. Cancellation
        propagates so the task can still be stopped cleanly.

        Args:
            body: The stored real request body to warm from.
            headers: The captured auth headers to replay.

        Returns:
            The response Usage on a 2xx response, or a short description of
            the error on any failure.
        """
        warm_body = build_warm_request(body)
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/messages",
                json=warm_body,
                headers=_replay_headers(headers),
            )
            response.raise_for_status()
            return parse_usage_json(response.json())
        except httpx.HTTPStatusError as exc:
            return f"HTTP {exc.response.status_code}: {_api_error_message(exc.response)}"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    def _reset(self, episode: _Episode) -> None:
        episode.warm_count = 0
        episode.last_warm_sent = float("-inf")
        episode.cap_emitted = False
        episode.main_request_count = None

    async def run(
        self,
        sessions_provider: Callable[[], Iterable[tuple[SessionState, EventLog]]],
        clock: Callable[[], float] = time.time,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        """Poll all active sessions on an interval, warming as warranted.

        Stops cleanly when cancelled.

        Args:
            sessions_provider: Returns the currently active
                (SessionState, EventLog) pairs.
            clock: Returns the current time, in seconds; injectable.
            poll_interval_sec: Seconds to sleep between polls.
        """
        while True:
            now = clock()
            for state, eventlog in sessions_provider():
                await self.tick(state, eventlog, now)
            await asyncio.sleep(poll_interval_sec)


def _replay_headers(headers: dict) -> dict:
    """Drop headers that must not be replayed on the direct warm request.

    Args:
        headers: The captured request headers.

    Returns:
        A new dict without the stripped headers, matched case-insensitively.
    """
    return {k: v for k, v in headers.items() if k.lower() not in _STRIPPED_HEADERS}


def _api_error_message(response: httpx.Response) -> str:
    """Extract the Anthropic API error message from a failed response.

    The API returns errors as {"error": {"message": ...}}. Falls back to
    the raw response text when the body is not the expected shape.

    Args:
        response: The failed HTTP response.

    Returns:
        The API's error message, or the raw response text.
    """
    try:
        error = response.json().get("error", {})
        message = error.get("message")
        if isinstance(message, str):
            return message
    except (ValueError, AttributeError):
        pass
    return response.text


def _usage_dict(usage: Usage) -> dict:
    return {
        "uncached_input": usage.uncached_input,
        "cache_read": usage.cache_read,
        "cache_creation": usage.cache_creation,
        "output": usage.output,
    }
