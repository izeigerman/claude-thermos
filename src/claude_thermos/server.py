import signal
import threading
from collections.abc import Callable

from claude_thermos.config import Config, is_loopback_url
from claude_thermos.launcher import ProxyHandle, RealProxyHandle
from claude_thermos.proxy import WarmerAddon


def _default_proxy_factory(port: int, addon: object | None, upstream: str) -> ProxyHandle:
    return RealProxyHandle(port, addon, upstream)


def _wait_for_signal() -> None:
    """Block until SIGINT or SIGTERM, then return.

    Installs handlers that trip a threading.Event and restores the previous
    handlers on the way out. Must run on the main thread (Python only allows
    installing signal handlers there).
    """
    stop = threading.Event()

    def _handle(signum: int, frame: object) -> None:
        stop.set()

    previous = [(sig, signal.signal(sig, _handle)) for sig in (signal.SIGINT, signal.SIGTERM)]
    try:
        stop.wait()
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)


def run_server(
    config: Config,
    port: int,
    upstream: str,
    proxy_factory: Callable[[int, object | None, str], ProxyHandle] = _default_proxy_factory,
    wait: Callable[[], None] = _wait_for_signal,
) -> int:
    """Run the cache-warming proxy as a standalone daemon.

    Starts the proxy on a fixed loopback port with a WarmerAddon pointed at
    `upstream`, then blocks until a stop signal, then tears the proxy down.
    Any client can warm through it by setting
    ANTHROPIC_BASE_URL=http://127.0.0.1:<port>. Unlike the launcher, this
    process is not the parent of `claude`, so it can serve the IDE extension
    and multiple terminals at once.

    Args:
        config: Resolved warmer configuration.
        port: Fixed loopback port the proxy listens on.
        upstream: Real upstream API URL; must not be a loopback address, or
            the proxy would reverse-proxy to itself.
        proxy_factory: Factory building a ProxyHandle; injectable for testing.
        wait: Blocks until a stop is requested; injectable for testing.

    Returns:
        Process exit code (0 on clean shutdown).
    """
    if is_loopback_url(upstream):
        raise ValueError(f"upstream must be the real Anthropic API, not a loopback URL: {upstream}")

    addon = WarmerAddon(config=config, upstream=upstream, reap_sessions=True)
    proxy = proxy_factory(port, addon, upstream)
    try:
        proxy.start()
        wait()
    finally:
        proxy.stop()
    return 0
