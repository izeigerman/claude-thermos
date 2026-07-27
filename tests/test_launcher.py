import socket
import time
from typing import Never

import pytest

from claude_thermos.config import Config
from claude_thermos.launcher import ProxyHandle, RealProxyHandle, child_env, run_launcher
from claude_thermos.proxy import find_free_port


def test_child_env_sets_base_url() -> None:
    base_env = {"PATH": "/usr/bin"}

    env = child_env(base_env, 8123)

    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8123"
    assert base_env == {"PATH": "/usr/bin"}


class _FakeProxyHandle(ProxyHandle):
    def __init__(self, port: int) -> None:
        self.port = port
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def test_run_launcher_injects_env_and_tears_down() -> None:
    captured = {}
    handles = []

    def fake_proxy_factory(port: int, addon: object | None) -> _FakeProxyHandle:
        handle = _FakeProxyHandle(port)
        handles.append(handle)
        return handle

    def fake_spawn(args: list[str], env: dict[str, str]) -> int:
        captured["args"] = args
        captured["env"] = env
        return 7

    config = Config()
    exit_code = run_launcher(
        config,
        ["chat", "-p", "hi"],
        proxy_factory=fake_proxy_factory,
        spawn=fake_spawn,
    )

    assert exit_code == 7
    assert captured["args"] == ["chat", "-p", "hi"]
    handle = handles[0]
    assert captured["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{handle.port}"
    assert handle.start_calls == 1
    assert handle.stop_calls == 1


def test_run_launcher_stops_proxy_on_child_error() -> None:
    handle = _FakeProxyHandle(8123)

    def fake_proxy_factory(port: int, addon: object | None) -> _FakeProxyHandle:
        return handle

    def fake_spawn(args: list[str], env: dict[str, str]) -> Never:
        raise RuntimeError("boom")

    config = Config()

    with pytest.raises(RuntimeError, match="boom"):
        run_launcher(
            config,
            [],
            proxy_factory=fake_proxy_factory,
            spawn=fake_spawn,
        )

    assert handle.stop_calls == 1


def _can_connect(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def test_real_proxy_handle_starts_and_stops() -> None:
    port = find_free_port()
    handle = RealProxyHandle(port)

    handle.start()

    deadline = time.monotonic() + 5
    connected = False
    while time.monotonic() < deadline:
        if _can_connect(port):
            connected = True
            break
        time.sleep(0.05)
    assert connected, "proxy never started accepting connections"

    handle.stop()

    deadline = time.monotonic() + 5
    stopped = False
    while time.monotonic() < deadline:
        if not _can_connect(port):
            stopped = True
            break
        time.sleep(0.05)
    assert stopped, "proxy still accepting connections after stop()"
