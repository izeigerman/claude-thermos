import pytest
from click.testing import CliRunner

from claude_thermos.cli import main
from claude_thermos.config import Config


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0


def test_bad_max_cycles_reports_error() -> None:
    result = CliRunner().invoke(main, ["-n", "-1"])
    assert result.exit_code != 0
    assert 'max-cycles must be a non-negative integer or "auto"' in result.output


def test_flag_env_precedence_and_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def recorder(config: Config, claude_args: list[str]) -> int:
        captured["config"] = config
        captured["claude_args"] = claude_args
        return 0

    monkeypatch.setattr("claude_thermos.cli.run_launcher", recorder)

    result = CliRunner().invoke(
        main,
        ["--idle", "400", "--interval", "999", "--", "chat", "-p", "hi"],
        env={"CLAUDE_WARMER_IDLE_THRESHOLD_SEC": "300"},
    )

    assert result.exit_code == 0
    assert captured["config"].idle_threshold_sec == 400
    assert captured["config"].warm_interval_sec == 999
    assert captured["config"].subagent_active_window_sec == 540
    assert captured["claude_args"] == ["chat", "-p", "hi"]

    result = CliRunner().invoke(main, [], env={"CLAUDE_WARMER_IDLE_THRESHOLD_SEC": "300"})

    assert result.exit_code == 0
    assert captured["config"].idle_threshold_sec == 300


def test_passthrough_without_double_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def recorder(config: Config, claude_args: list[str]) -> int:
        captured["config"] = config
        captured["claude_args"] = claude_args
        return 0

    monkeypatch.setattr("claude_thermos.cli.run_launcher", recorder)

    result = CliRunner().invoke(main, ["chat", "-p", "hi"])

    assert result.exit_code == 0
    assert captured["claude_args"] == ["chat", "-p", "hi"]
    assert captured["config"].idle_threshold_sec == 270


def test_serve_dispatches_to_run_server(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def recorder(config: Config, port: int, upstream: str) -> int:
        captured["config"] = config
        captured["port"] = port
        captured["upstream"] = upstream
        return 0

    monkeypatch.setattr("claude_thermos.cli.run_server", recorder)

    result = CliRunner().invoke(
        main, ["serve", "--port", "9000", "--idle", "120", "--session-ttl", "1800"]
    )

    assert result.exit_code == 0
    assert captured["port"] == 9000
    assert captured["upstream"] == "https://api.anthropic.com"
    assert captured["config"].idle_threshold_sec == 120
    assert captured["config"].session_ttl_sec == 1800


def test_serve_rejects_loopback_upstream() -> None:
    result = CliRunner().invoke(main, ["serve", "--upstream", "http://127.0.0.1:8787"])

    assert result.exit_code != 0
    assert "loopback" in result.output


@pytest.mark.parametrize("ttl", ["0", "-5"])
def test_serve_rejects_nonpositive_session_ttl(ttl: str) -> None:
    result = CliRunner().invoke(main, ["serve", "--session-ttl", ttl])

    assert result.exit_code != 0
    assert "session-ttl" in result.output.lower()


def test_serve_is_not_swallowed_by_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """The literal first token `serve` must select the daemon, not claude."""
    monkeypatch.setattr(
        "claude_thermos.cli.run_launcher",
        lambda config, args: pytest.fail("serve routed to launcher"),
    )
    monkeypatch.setattr("claude_thermos.cli.run_server", lambda config, port, upstream: 0)

    result = CliRunner().invoke(main, ["serve"])

    assert result.exit_code == 0
