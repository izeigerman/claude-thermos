from typing import Never

import pytest

from claude_thermos.config import Config
from claude_thermos.server import run_server


class _FakeProxyHandle:
    def __init__(self, port: int, addon: object | None, upstream: str) -> None:
        self.port = port
        self.addon = addon
        self.upstream = upstream
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def test_run_server_starts_waits_and_stops() -> None:
    handles: list[_FakeProxyHandle] = []
    waited: list[bool] = []

    def factory(port: int, addon: object | None, upstream: str) -> _FakeProxyHandle:
        handle = _FakeProxyHandle(port, addon, upstream)
        handles.append(handle)
        return handle

    exit_code = run_server(
        Config(),
        8787,
        "https://api.anthropic.com",
        proxy_factory=factory,
        wait=lambda: waited.append(True),
    )

    assert exit_code == 0
    assert waited == [True]
    handle = handles[0]
    assert handle.port == 8787
    assert handle.upstream == "https://api.anthropic.com"
    assert handle.addon is not None  # a WarmerAddon is wired in
    assert handle.start_calls == 1
    assert handle.stop_calls == 1


def test_run_server_stops_proxy_on_wait_error() -> None:
    handle = _FakeProxyHandle(8787, None, "https://api.anthropic.com")

    def boom() -> Never:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_server(
            Config(),
            8787,
            "https://api.anthropic.com",
            proxy_factory=lambda port, addon, upstream: handle,
            wait=boom,
        )

    assert handle.stop_calls == 1


def test_run_server_stops_proxy_when_start_fails() -> None:
    class _FailingStartHandle(_FakeProxyHandle):
        def start(self) -> Never:
            self.start_calls += 1
            raise RuntimeError("bind failed")

    handle = _FailingStartHandle(8787, None, "https://api.anthropic.com")

    with pytest.raises(RuntimeError, match="bind failed"):
        run_server(
            Config(),
            8787,
            "https://api.anthropic.com",
            proxy_factory=lambda port, addon, upstream: handle,
            wait=lambda: None,
        )

    assert handle.start_calls == 1
    assert handle.stop_calls == 1


def test_run_server_rejects_loopback_upstream() -> None:
    started = []

    def factory(port: int, addon: object | None, upstream: str) -> _FakeProxyHandle:
        handle = _FakeProxyHandle(port, addon, upstream)
        started.append(handle)
        return handle

    with pytest.raises(ValueError, match="loopback"):
        run_server(
            Config(),
            8787,
            "http://127.0.0.1:8787",
            proxy_factory=factory,
            wait=lambda: None,
        )

    assert started == []  # rejected before any proxy is built
