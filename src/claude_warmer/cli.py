import os
import sys

import click

from claude_warmer.config import build_config
from claude_warmer.launcher import run_launcher

__all__ = ["main"]


@click.command(
    context_settings={"ignore_unknown_options": True, "help_option_names": ["-h", "--help"]}
)
@click.option(
    "--idle",
    "idle_threshold_sec",
    type=int,
    default=270,
    show_default=True,
    envvar="CLAUDE_WARMER_IDLE_THRESHOLD_SEC",
    help="Idle threshold, in seconds, before a warming cycle runs.",
)
@click.option(
    "--interval",
    "warm_interval_sec",
    type=int,
    default=270,
    show_default=True,
    envvar="CLAUDE_WARMER_WARM_INTERVAL_SEC",
    help="Interval, in seconds, between warming cycles.",
)
@click.option(
    "-n",
    "--max-cycles",
    "max_cycles_raw",
    default="2",
    show_default=True,
    envvar="CLAUDE_WARMER_WARM_MAX_CYCLES",
    help='Maximum number of warming cycles, or "auto" for unlimited.',
)
@click.option(
    "--subagent-window",
    "subagent_active_window_sec",
    type=int,
    default=540,
    show_default=True,
    envvar="CLAUDE_WARMER_SUBAGENT_ACTIVE_WINDOW_SEC",
    help="Subagent active window, in seconds.",
)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def main(
    idle_threshold_sec: int,
    warm_interval_sec: int,
    max_cycles_raw: str,
    subagent_active_window_sec: int,
    claude_args: tuple[str, ...],
) -> None:
    """Launch Claude Code behind a local cache-warming reverse proxy."""
    try:
        config = build_config(
            idle_threshold_sec,
            warm_interval_sec,
            max_cycles_raw,
            subagent_active_window_sec,
            os.environ,
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="-n / --max-cycles") from exc

    sys.exit(run_launcher(config, list(claude_args)))
