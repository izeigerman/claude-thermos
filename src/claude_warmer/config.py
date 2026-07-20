import argparse
from collections.abc import Mapping
from dataclasses import dataclass

_MAX_CYCLES_ERROR = 'max-cycles must be a non-negative integer or "auto"'


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv on the first literal `--` into (known_args, passthrough)."""
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return argv, []


@dataclass(frozen=True)
class Config:
    idle_threshold_sec: int = 270
    warm_interval_sec: int = 270
    warm_max_cycles: int | None = 2
    subagent_active_window_sec: int = 540
    disabled: bool = False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-warmer")
    parser.add_argument("--idle", type=int, default=None)
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("-n", "--max-cycles", dest="max_cycles", default=None)
    parser.add_argument("--subagent-window", type=int, default=None)
    return parser


def _parse_max_cycles(value: int | str) -> int | None:
    if value == "auto":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(_MAX_CYCLES_ERROR) from None
    if parsed < 0:
        raise ValueError(_MAX_CYCLES_ERROR)
    return parsed


def _is_disabled(environ: Mapping[str, str]) -> bool:
    raw = environ.get("CLAUDE_WARMER_DISABLE", "")
    if not raw:
        return False
    return raw.strip().lower() not in ("0", "false")


def _resolve_int(
    flag_value: int | None, env_name: str, environ: Mapping[str, str], default: int
) -> int:
    if flag_value is not None:
        return flag_value
    if env_name in environ:
        return int(environ[env_name])
    return default


def load_config(argv: list[str], environ: Mapping[str, str]) -> tuple[Config, list[str]]:
    """Resolve config from CLI flags over env vars over defaults, and return
    (config, claude_passthrough_args). Splits argv on the first '--'."""
    known_argv, passthrough = split_passthrough(argv)
    args = _build_parser().parse_args(known_argv)

    idle_threshold_sec = _resolve_int(args.idle, "CLAUDE_WARMER_IDLE_THRESHOLD_SEC", environ, 270)
    warm_interval_sec = _resolve_int(args.interval, "CLAUDE_WARMER_WARM_INTERVAL_SEC", environ, 270)
    subagent_active_window_sec = _resolve_int(
        args.subagent_window, "CLAUDE_WARMER_SUBAGENT_ACTIVE_WINDOW_SEC", environ, 540
    )

    if args.max_cycles is not None:
        raw_max_cycles: int | str = args.max_cycles
    elif "CLAUDE_WARMER_WARM_MAX_CYCLES" in environ:
        raw_max_cycles = environ["CLAUDE_WARMER_WARM_MAX_CYCLES"]
    else:
        raw_max_cycles = 2
    warm_max_cycles = _parse_max_cycles(raw_max_cycles)

    config = Config(
        idle_threshold_sec=idle_threshold_sec,
        warm_interval_sec=warm_interval_sec,
        warm_max_cycles=warm_max_cycles,
        subagent_active_window_sec=subagent_active_window_sec,
        disabled=_is_disabled(environ),
    )
    return config, passthrough
