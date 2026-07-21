import asyncio
import json
import os
import socket
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Protocol

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from claude_warmer.eventlog import EventLog, EventType
from claude_warmer.lineage import LineageId, extract_session_id
from claude_warmer.state import SessionState
from claude_warmer.usage import Usage, parse_usage_json, parse_usage_sse

MESSAGES_PATH = "/v1/messages"


def find_free_port() -> int:
    """Find a free TCP port on the loopback interface.

    Binds a socket to 127.0.0.1:0, reads the assigned port, and closes it.

    Returns:
        The assigned port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_master(port: int, addon: object | None) -> DumpMaster:
    """Construct a DumpMaster in reverse-proxy mode. Does not start it.

    Configured in reverse mode to the Anthropic API (or ANTHROPIC_BASE_URL)
    on 127.0.0.1:<port>, with quiet/no-terminal options.

    Args:
        port: Loopback port the proxy listens on.
        addon: Addon to register, or None to register nothing.

    Returns:
        The configured, unstarted DumpMaster.
    """
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    options = Options(
        mode=[f"reverse:{anthropic_base_url}"],
        listen_host="127.0.0.1",
        listen_port=port,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    master = DumpMaster(
        options,
        loop=loop,
        with_termlog=False,
        with_dumper=False,
    )

    if addon is not None:
        master.addons.add(addon)

    return master


class _RequestLike(Protocol):
    """The subset of mitmproxy's Request that WarmerAddon's hooks read (and,
    for `headers`, mutate in place via `__setitem__`). Kept as a structural
    protocol (rather than importing mitmproxy's concrete type) so a
    lightweight duck-typed fake satisfies it in tests. Members are declared
    as read-only properties so a plain attribute on a fake class still
    satisfies the protocol."""

    @property
    def path(self) -> str: ...
    @property
    def headers(self) -> MutableMapping[str, str]: ...
    def get_text(self) -> str | None: ...


class _ResponseLike(Protocol):
    @property
    def content(self) -> bytes | None: ...


class FlowLike(Protocol):
    """The subset of mitmproxy's HTTPFlow that WarmerAddon's hooks use."""

    @property
    def request(self) -> _RequestLike: ...
    @property
    def response(self) -> _ResponseLike | None: ...


def _handle_request(
    state: SessionState,
    eventlog: EventLog,
    body: dict,
    headers: dict,
    now: float,
    seen_lineages: set[LineageId],
) -> LineageId:
    """Record a request on state and emit session/lineage events.

    Emits session_start on the first request ever seen for the session and
    lineage_seen on the first sight of a lineage.

    Args:
        state: The session's state.
        eventlog: The session's event log.
        body: The decoded request body.
        headers: The request's headers.
        now: Current time, in seconds.
        seen_lineages: Lineage ids already seen this session; mutated in place.

    Returns:
        The resolved lineage id.
    """
    lineage = LineageId.from_request_body(body)
    is_new_session = not seen_lineages
    is_new_lineage = lineage not in seen_lineages

    state.on_request(lineage, body, headers, now)

    if is_new_session:
        eventlog.emit(EventType.SESSION_START, session_id=state.session_id, lineage=lineage)
    if is_new_lineage:
        eventlog.emit(
            EventType.LINEAGE_SEEN, lineage=lineage, has_tools=len(body.get("tools", [])) > 0
        )
        seen_lineages.add(lineage)

    return lineage


def _parse_usage(raw_response: bytes) -> Usage:
    stripped = raw_response.lstrip()
    if stripped.startswith(b"event:") or stripped.startswith(b"data:"):
        return parse_usage_sse([raw_response])
    return parse_usage_json(json.loads(raw_response))


def _handle_response(
    state: SessionState,
    eventlog: EventLog,
    lineage: LineageId,
    raw_response: bytes,
    now: float,
) -> None:
    """Record a response on state and emit a usage event.

    Parses usage from `raw_response`, records the response on `state`, and
    emits a usage event.

    Args:
        state: The session's state.
        eventlog: The session's event log.
        lineage: The response's lineage id.
        raw_response: The raw response bytes.
        now: Current time, in seconds.
    """
    usage = _parse_usage(raw_response)
    state.on_response(lineage, now)
    eventlog.emit(
        EventType.USAGE,
        lineage=lineage,
        usage={
            "uncached_input": usage.uncached_input,
            "cache_read": usage.cache_read,
            "cache_creation": usage.cache_creation,
            "output": usage.output,
        },
    )


@dataclass
class _Session:
    state: SessionState
    eventlog: EventLog
    seen_lineages: set[LineageId]


class WarmerAddon:
    """mitmproxy addon that observes /v1/messages traffic into per-session
    SessionState + EventLog and emits structured events. Fires no warm
    requests of its own."""

    def __init__(
        self,
        eventlog_factory: Callable[[str], EventLog] = EventLog,
    ) -> None:
        self._eventlog_factory = eventlog_factory
        self._sessions: dict[str, _Session] = {}

    def request(self, flow: FlowLike) -> None:
        """Handle a mitmproxy request hook.

        On a /v1/messages request, decodes the JSON body, resolves
        session/lineage, updates state, and forces accept-encoding:
        identity so the response usage parses.

        Args:
            flow: The mitmproxy flow for the request.
        """
        if not flow.request.path.startswith(MESSAGES_PATH):
            return
        flow.request.headers["accept-encoding"] = "identity"

        resolved = self._resolve_session_and_body(flow)
        if resolved is None:
            return
        session, body = resolved
        _handle_request(
            session.state,
            session.eventlog,
            body,
            dict(flow.request.headers),
            time.time(),
            session.seen_lineages,
        )

    def response(self, flow: FlowLike) -> None:
        """Handle a mitmproxy response hook.

        Parses usage from the response, updates state, and emits a usage
        event. Never modifies the response body.

        Args:
            flow: The mitmproxy flow for the response.
        """
        if flow.response is None:
            return
        resolved = self._resolve_session_and_body(flow)
        if resolved is None:
            return
        session, body = resolved
        lineage = LineageId.from_request_body(body)
        _handle_response(
            session.state,
            session.eventlog,
            lineage,
            flow.response.content or b"",
            time.time(),
        )

    def _get_or_create_session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session(
                state=SessionState(session_id),
                eventlog=self._eventlog_factory(session_id),
                seen_lineages=set(),
            )
            self._sessions[session_id] = session
        return session

    def _resolve_session_and_body(self, flow: FlowLike) -> tuple[_Session, dict] | None:
        if not flow.request.path.startswith(MESSAGES_PATH):
            return None
        try:
            body = json.loads(flow.request.get_text() or "")
        except (json.JSONDecodeError, ValueError):
            return None
        session_id = extract_session_id(body)
        if session_id is None:
            return None
        return self._get_or_create_session(session_id), body
