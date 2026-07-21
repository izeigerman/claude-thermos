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
    """Bind a socket to 127.0.0.1:0, read the assigned port, close, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_master(port: int, addon: object | None) -> DumpMaster:
    """Construct a DumpMaster configured in reverse mode to
    https://api.anthropic.com on 127.0.0.1:<port>, with quiet/no-terminal
    options, registering `addon` if provided. Does not start it."""
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
    """Record this request on `state` and emit session_start (first request
    ever seen for this session) / lineage_seen (first sight of this
    lineage). Returns the resolved lineage id."""
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
    """Parse usage from `raw_response`, record the response on `state`, and
    emit a usage event."""
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
        """mitmproxy hook: on a /v1/messages request, decode the JSON body,
        resolve session/lineage, update state, and force
        accept-encoding: identity so the response usage parses."""
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
        """mitmproxy hook: parse usage from the response, update state, emit
        a usage event, then emit idle_detected/subagent_active on new
        transitions. Never modifies the response body."""
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
