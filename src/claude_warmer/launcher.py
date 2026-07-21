import asyncio
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import Protocol

from mitmproxy.tools.dump import DumpMaster

from claude_warmer.config import Config
from claude_warmer.proxy import WarmerAddon, build_master, find_free_port


class ProxyHandle(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class RealProxyHandle:
    """ProxyHandle backed by mitmproxy's DumpMaster.

    `build_master` binds `master.event_loop` to whatever asyncio loop is
    current at construction time, creating a throwaway loop via
    `asyncio.new_event_loop()` when none is running. `master.shutdown()`
    calls `master.event_loop.call_soon_threadsafe(...)` internally, so the
    master MUST be driven on that same loop or `shutdown()` will target a
    dead loop and hang forever. `start()` therefore runs `master.run()` on
    a background thread via `master.event_loop.run_until_complete(...)`
    after making that loop current on the thread, and closes the loop once
    the run completes to avoid a file descriptor leak.

    `master.run()` returning does not by itself close the listening
    sockets (mitmproxy leaves that to process exit in its own CLI), so the
    background thread also explicitly tears down the proxyserver addon's
    server instances before closing the loop.
    """

    def __init__(self, port: int, addon: object | None = None) -> None:
        self._port = port
        self._addon = addon
        self._master: DumpMaster | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        master = build_master(self._port, self._addon)
        self._master = master
        loop = master.event_loop

        def _run() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(master.run())
                proxyserver = master.addons.get("proxyserver")
                if proxyserver is not None:
                    loop.run_until_complete(proxyserver.servers.update([]))
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._master is None:
            return
        self._master.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=10)


def child_env(base_env: Mapping[str, str], port: int) -> dict[str, str]:
    """Return a copy of base_env pointing Claude at the local proxy.

    Args:
        base_env: The environment to copy.
        port: Loopback port the proxy listens on.

    Returns:
        A copy of base_env with ANTHROPIC_BASE_URL set to
        http://127.0.0.1:<port>.
    """
    env = dict(base_env)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    return env


def _default_proxy_factory(port: int, addon: object | None) -> ProxyHandle:
    return RealProxyHandle(port, addon)


def _default_spawn(args: list[str], env: dict[str, str]) -> int:
    completed = subprocess.run(["claude", *args], env=env)
    return completed.returncode


def run_launcher(
    config: Config,
    claude_args: list[str],
    proxy_factory: Callable[[int, object | None], ProxyHandle] = _default_proxy_factory,
    spawn: Callable[[list[str], dict[str, str]], int] = _default_spawn,
) -> int:
    """Run Claude Code behind the warming proxy.

    Starts the proxy (unless config.disabled), spawns `claude` with
    child_env + claude_args and inherited stdio, waits for it, then stops
    the proxy.

    Args:
        config: Resolved launcher configuration.
        claude_args: Extra arguments passed through to `claude`.
        proxy_factory: Factory building a ProxyHandle from port and addon;
            injectable for testing.
        spawn: Callable that runs `claude` and returns its exit code;
            injectable for testing.

    Returns:
        The child process's exit code.
    """
    port = find_free_port()

    addon = None if config.disabled else WarmerAddon()

    proxy = proxy_factory(port, addon)
    proxy.start()
    try:
        env = child_env(os.environ, port)
        return spawn(claude_args, env)
    finally:
        proxy.stop()
