import pytest

from claude_thermos.config import Config, build_config, is_loopback_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "http://[::1]:8787",
        "http://127.0.0.2:8787",
        "http://127.255.255.254",
    ],
)
def test_is_loopback_url_true(url: str) -> None:
    assert is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://api.anthropic.com", "https://example.test:8787"],
)
def test_is_loopback_url_false(url: str) -> None:
    assert is_loopback_url(url) is False


def test_build_config_defaults() -> None:
    config = build_config(270, 270, "2", 540, {})
    assert config == Config(
        idle_threshold_sec=270,
        warm_interval_sec=270,
        warm_max_cycles=2,
        subagent_active_window_sec=540,
        disabled=False,
    )


def test_build_config_auto_max_cycles() -> None:
    config = build_config(270, 270, "auto", 540, {})
    assert config.warm_max_cycles is None


@pytest.mark.parametrize("max_cycles_raw", ["-1", "x"])
def test_build_config_bad_max_cycles_raises(max_cycles_raw: str) -> None:
    with pytest.raises(ValueError, match=r'max-cycles must be a non-negative integer or "auto"'):
        build_config(270, 270, max_cycles_raw, 540, {})


def test_build_config_disabled_env() -> None:
    assert build_config(270, 270, "2", 540, {"CLAUDE_WARMER_DISABLE": "1"}).disabled is True
    assert build_config(270, 270, "2", 540, {"CLAUDE_WARMER_DISABLE": "0"}).disabled is False
    assert build_config(270, 270, "2", 540, {}).disabled is False
