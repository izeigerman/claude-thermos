import pytest

from claude_warmer.cli import main, split_passthrough


def test_main_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_double_dash_splits_passthrough():
    known, passthrough = split_passthrough(
        ["--idle", "300", "--", "chat", "--model", "x"]
    )
    assert known == ["--idle", "300"]
    assert passthrough == ["chat", "--model", "x"]


def test_main_delegates_to_run_launcher(monkeypatch):
    captured = {}

    def fake_run_launcher(config, passthrough):
        captured["config"] = config
        captured["passthrough"] = passthrough
        return 5

    monkeypatch.setattr("claude_warmer.cli.run_launcher", fake_run_launcher)

    exit_code = main(["--idle", "300", "--", "chat", "--model", "x"])

    assert exit_code == 5
    assert captured["config"].idle_threshold_sec == 300
    assert captured["passthrough"] == ["chat", "--model", "x"]
