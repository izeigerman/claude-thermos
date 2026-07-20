import argparse


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv on the first literal `--` into (known_args, passthrough)."""
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return argv, []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-warmer",
        description="Wraps Claude Code with a local reverse proxy that keeps its prompt cache warm.",
    )
    parser.add_argument(
        "--idle",
        type=int,
        default=None,
        help="Idle timeout in seconds before the cache warmer stops pinging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `claude-warmer` console script.
    Parses config + passthrough args, runs the launcher, returns the child's exit code."""
    if argv is None:
        import sys

        argv = sys.argv[1:]

    known_argv, passthrough = split_passthrough(argv)
    parser = build_parser()
    args = parser.parse_args(known_argv)

    print(f"settings={vars(args)}")
    print(f"passthrough={passthrough}")

    return 0
