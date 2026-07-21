from dataclasses import dataclass, field

from claude_warmer.lineage import LineageId


@dataclass
class LineageState:
    lineage_id: LineageId
    first_seen: float
    last_request_sent: float
    last_response_end: float = float("-inf")
    in_flight: int = 0
    request_count: int = 0
    auth_headers: dict[str, str] = field(default_factory=dict)
    last_request_body: dict | None = None
    has_tools: bool = False


class SessionState:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._lineages: dict[LineageId, LineageState] = {}

    def on_request(self, lineage: LineageId, body: dict, headers: dict, now: float) -> None:
        """Create or update the lineage's state for a new request.

        Bumps in_flight and request_count, sets last_request_sent, stores
        the latest auth headers and last_request_body, and records
        has_tools = len(tools) > 0.

        Args:
            lineage: The request's lineage id.
            body: The decoded request body.
            headers: The request's headers.
            now: Current time, in seconds.
        """
        state = self._lineages.get(lineage)
        if state is None:
            state = LineageState(lineage_id=lineage, first_seen=now, last_request_sent=now)
            self._lineages[lineage] = state
        state.in_flight += 1
        state.request_count += 1
        state.last_request_sent = now
        state.auth_headers = dict(headers)
        state.last_request_body = body
        state.has_tools = len(body.get("tools", [])) > 0

    def on_response(self, lineage: LineageId, now: float) -> None:
        """Record a completed response for the lineage.

        Decrements in_flight (floored at 0) and sets last_response_end.

        Args:
            lineage: The response's lineage id.
            now: Current time, in seconds.
        """
        state = self._lineages.get(lineage)
        if state is None:
            return
        state.in_flight = max(0, state.in_flight - 1)
        state.last_response_end = now

    def main_lineage_id(self) -> LineageId | None:
        """Return the id of the first substantive lineage.

        The main lineage has the earliest first_seen among lineages with
        has_tools True.

        Returns:
            The main lineage id, or None if none exists yet.
        """
        candidates = [state for state in self._lineages.values() if state.has_tools]
        if not candidates:
            return None
        best = min(candidates, key=lambda state: (state.first_seen, state.lineage_id))
        return best.lineage_id

    def main_lineage(self) -> LineageState | None:
        """Return the main lineage's state, or None if none exists yet.

        Returns:
            The LineageState of the main lineage, or None.
        """
        main_id = self.main_lineage_id()
        if main_id is None:
            return None
        return self._lineages[main_id]

    def is_main_idle(self, now: float, idle_threshold_sec: int) -> bool:
        """Report whether the main lineage is idle.

        Args:
            now: Current time, in seconds.
            idle_threshold_sec: Seconds of inactivity that count as idle.

        Returns:
            True iff a main lineage exists, its in_flight is 0, and
            now - last_response_end >= idle_threshold_sec.
        """
        main_id = self.main_lineage_id()
        if main_id is None:
            return False
        main = self._lineages[main_id]
        return main.in_flight == 0 and now - main.last_response_end >= idle_threshold_sec

    def subagent_active(self, now: float, window_sec: int) -> bool:
        """Report whether any subagent (non-main) lineage is active.

        Args:
            now: Current time, in seconds.
            window_sec: Recency window, in seconds, for recent activity.

        Returns:
            True iff any non-main lineage has in_flight > 0 or
            now - max(last_request_sent, last_response_end) < window_sec.
        """
        main_id = self.main_lineage_id()
        for lineage, state in self._lineages.items():
            if lineage == main_id:
                continue
            if state.in_flight > 0:
                return True
            if now - max(state.last_request_sent, state.last_response_end) < window_sec:
                return True
        return False
