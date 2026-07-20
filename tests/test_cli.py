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


def test_main_surfaces_passthrough_without_error(capsys):
    exit_code = main(["--idle", "300", "--", "chat", "--model", "x"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "chat" in captured.out
    assert "--model" in captured.out
