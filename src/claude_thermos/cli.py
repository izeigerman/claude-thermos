import os
import sys
from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import version

import click

from claude_thermos.config import DEFAULT_UPSTREAM, Config, build_config, is_loopback_url
from claude_thermos.launcher import run_launcher
from claude_thermos.server import run_server

__all__ = ["main"]


def _warmer_options(func: Callable) -> Callable:
    """Apply the shared warmer-tuning options used by both entry points."""
    func = click.option(
        "--idle",
        "idle_threshold_sec",
        type=int,
        default=270,
        show_default=True,
        envvar="CLAUDE_THERMOS_IDLE_THRESHOLD_SEC",
        help="Idle threshold, in seconds, before a warming cycle runs.",
    )(func)
    func = click.option(
        "--interval",
        "warm_interval_sec",
        type=int,
        default=270,
        show_default=True,
        envvar="CLAUDE_THERMOS_WARM_INTERVAL_SEC",
        help="Interval, in seconds, between warming cycles.",
    )(func)
    func = click.option(
        "-n",
        "--max-cycles",
        "max_cycles_raw",
        default="4",
        show_default=True,
        envvar="CLAUDE_THERMOS_WARM_MAX_CYCLES",
        help='Maximum number of warming cycles, or "auto" for unlimited.',
    )(func)
    func = click.option(
        "--subagent-window",
        "subagent_active_window_sec",
        type=int,
        default=540,
        show_default=True,
        envvar="CLAUDE_THERMOS_SUBAGENT_ACTIVE_WINDOW_SEC",
        help="Subagent active window, in seconds.",
    )(func)
    return func


def _build(
    idle_threshold_sec: int,
    warm_interval_sec: int,
    max_cycles_raw: str,
    subagent_active_window_sec: int,
) -> Config:
    try:
        return build_config(
            idle_threshold_sec,
            warm_interval_sec,
            max_cycles_raw,
            subagent_active_window_sec,
            os.environ,
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="-n / --max-cycles") from exc


class _ThermosCLI(click.Group):
    """Group that forwards to the `launch` command for anything that is not
    the `serve` subcommand (or a top-level --version/--help), so
    `claude-thermos <claude args>` keeps forwarding straight through to claude
    while `claude-thermos serve` runs the daemon.

    It injects the `launch` command name rather than dispatching by hand, so
    option and environment-variable parsing still happen the normal way.
    """

    _PASSTHROUGH_HEAD = ("--version", "-V", "--help", "-h")

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and (args[0] in self.commands or args[0] in self._PASSTHROUGH_HEAD):
            return super().parse_args(ctx, args)
        return super().parse_args(ctx, ["launch", *args])


@click.group(
    cls=_ThermosCLI,
    context_settings={"ignore_unknown_options": True, "help_option_names": ["-h", "--help"]},
)
@click.version_option(version("claude-thermos"), "-V", "--version")
def main() -> None:
    """Launch Claude Code behind a local cache-warming reverse proxy.

    Runs claude as usual by default; `claude-thermos serve` instead runs the
    warmer as a standalone daemon that any client can share.
    """


@main.command(
    context_settings={"ignore_unknown_options": True, "help_option_names": ["-h", "--help"]}
)
@_warmer_options
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def launch(
    idle_threshold_sec: int,
    warm_interval_sec: int,
    max_cycles_raw: str,
    subagent_active_window_sec: int,
    claude_args: tuple[str, ...],
) -> None:
    """Launch Claude Code behind the warming proxy (the default action)."""
    config = _build(
        idle_threshold_sec, warm_interval_sec, max_cycles_raw, subagent_active_window_sec
    )
    sys.exit(run_launcher(config, list(claude_args)))


@main.command(context_settings={"help_option_names": ["-h", "--help"]})
@_warmer_options
@click.option(
    "--port",
    type=int,
    default=8787,
    show_default=True,
    envvar="CLAUDE_THERMOS_PORT",
    help="Loopback port the daemon listens on.",
)
@click.option(
    "--upstream",
    default=DEFAULT_UPSTREAM,
    show_default=True,
    envvar="CLAUDE_THERMOS_UPSTREAM",
    help="Real upstream Anthropic API URL to reverse-proxy to.",
)
@click.option(
    "--session-ttl",
    "session_ttl_sec",
    type=click.IntRange(min=1),
    default=3600,
    show_default=True,
    envvar="CLAUDE_THERMOS_SESSION_TTL_SEC",
    help="Seconds a session may sit idle before the daemon evicts it.",
)
def serve(
    idle_threshold_sec: int,
    warm_interval_sec: int,
    max_cycles_raw: str,
    subagent_active_window_sec: int,
    port: int,
    upstream: str,
    session_ttl_sec: int,
) -> None:
    """Run the cache-warming proxy as a standalone daemon.

    Point any client at it with ANTHROPIC_BASE_URL=http://127.0.0.1:<port>.
    Unlike the default launcher, the daemon is not the parent of claude, so
    it can warm the IDE extension and several terminals from one process.
    """
    if is_loopback_url(upstream):
        raise click.BadParameter(
            f"must be the real Anthropic API, not a loopback URL: {upstream}",
            param_hint="--upstream",
        )
    config = replace(
        _build(idle_threshold_sec, warm_interval_sec, max_cycles_raw, subagent_active_window_sec),
        session_ttl_sec=session_ttl_sec,
    )
    sys.exit(run_server(config, port, upstream))
