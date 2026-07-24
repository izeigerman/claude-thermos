import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

_MAX_CYCLES_ERROR = 'max-cycles must be a non-negative integer or "auto"'

# The real Anthropic API endpoint, used as the default upstream for the
# detached proxy so it never depends on (and can never inherit) a client's
# ANTHROPIC_BASE_URL that points back at the proxy itself.
DEFAULT_UPSTREAM = "https://api.anthropic.com"

# Upstream Anthropic API the launcher's proxy reverse-proxies to and warm
# requests are sent to. Resolved once from the environment so the reverse
# proxy and the warmer always agree on the same endpoint.
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_UPSTREAM)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_url(url: str) -> bool:
    """Report whether `url`'s host is a loopback address.

    Used to reject a detached-proxy upstream that points back at the local
    proxy, which would make the proxy reverse-proxy to itself.

    Args:
        url: The URL to inspect.

    Returns:
        True iff the URL's host is a loopback address.
    """
    return (urlparse(url).hostname or "") in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class Config:
    idle_threshold_sec: int = 270
    warm_interval_sec: int = 270
    warm_max_cycles: int | None = 2
    subagent_active_window_sec: int = 540
    disabled: bool = False
    # How long a session may sit idle before a detached proxy evicts it.
    # Unused by the launcher (its process dies with claude); only the daemon
    # runs long enough to accumulate stale sessions.
    session_ttl_sec: int = 3600


def _parse_max_cycles(value: int | str) -> int | None:
    if value == "auto":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(_MAX_CYCLES_ERROR) from None
    if parsed < 0:
        raise ValueError(_MAX_CYCLES_ERROR)
    return parsed


def _is_disabled(environ: Mapping[str, str]) -> bool:
    raw = environ.get("CLAUDE_WARMER_DISABLE", "")
    if not raw:
        return False
    return raw.strip().lower() not in ("0", "false")


def build_config(
    idle_threshold_sec: int,
    warm_interval_sec: int,
    max_cycles_raw: str,
    subagent_active_window_sec: int,
    environ: Mapping[str, str],
) -> Config:
    """Build a Config from already-resolved option values.

    Flag/env/default precedence is handled by the CLI layer; this parses
    max_cycles and reads the disable env var.

    Args:
        idle_threshold_sec: Idle threshold, in seconds, before warming.
        warm_interval_sec: Interval, in seconds, between warming cycles.
        max_cycles_raw: Maximum warming cycles as a string, or "auto".
        subagent_active_window_sec: Subagent active window, in seconds.
        environ: Environment mapping read for the disable flag.

    Returns:
        A Config with the resolved and parsed values.
    """
    return Config(
        idle_threshold_sec=idle_threshold_sec,
        warm_interval_sec=warm_interval_sec,
        warm_max_cycles=_parse_max_cycles(max_cycles_raw),
        subagent_active_window_sec=subagent_active_window_sec,
        disabled=_is_disabled(environ),
    )
