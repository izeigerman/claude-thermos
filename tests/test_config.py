import pytest

from claude_warmer.config import Config, load_config


def test_defaults():
    config, passthrough = load_config([], {})
    assert config == Config(
        idle_threshold_sec=270,
        warm_interval_sec=270,
        warm_max_cycles=2,
        subagent_active_window_sec=540,
        disabled=False,
    )
    assert passthrough == []


def test_flag_overrides_env_overrides_default():
    config, _ = load_config([], {})
    assert config.idle_threshold_sec == 270

    config, _ = load_config([], {"CLAUDE_WARMER_IDLE_THRESHOLD_SEC": "300"})
    assert config.idle_threshold_sec == 300

    config, _ = load_config(
        ["--idle", "400"], {"CLAUDE_WARMER_IDLE_THRESHOLD_SEC": "300"}
    )
    assert config.idle_threshold_sec == 400


def test_auto_max_cycles():
    config, _ = load_config(["-n", "auto"], {})
    assert config.warm_max_cycles is None


def test_bad_max_cycles_raises():
    with pytest.raises(ValueError, match=r'max-cycles must be a non-negative integer or "auto"'):
        load_config(["-n", "-1"], {})


def test_disable_env():
    config, _ = load_config([], {"CLAUDE_WARMER_DISABLE": "1"})
    assert config.disabled is True

    config, _ = load_config([], {"CLAUDE_WARMER_DISABLE": "0"})
    assert config.disabled is False


def test_passthrough_split():
    config, passthrough = load_config(
        ["--idle", "300", "--", "chat", "-p", "hi"], {}
    )
    assert passthrough == ["chat", "-p", "hi"]
