import pytest
from click.testing import CliRunner

from claude_warmer.cli import main
from claude_warmer.config import Config


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

    monkeypatch.setattr("claude_warmer.cli.run_launcher", recorder)

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

    monkeypatch.setattr("claude_warmer.cli.run_launcher", recorder)

    result = CliRunner().invoke(main, ["chat", "-p", "hi"])

    assert result.exit_code == 0
    assert captured["claude_args"] == ["chat", "-p", "hi"]
    assert captured["config"].idle_threshold_sec == 270
