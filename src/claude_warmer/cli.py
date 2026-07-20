import os

from claude_warmer.config import load_config, split_passthrough

__all__ = ["main", "split_passthrough"]


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `claude-warmer` console script.
    Parses config + passthrough args, runs the launcher, returns the child's exit code."""
    if argv is None:
        import sys

        argv = sys.argv[1:]

    config, passthrough = load_config(argv, os.environ)

    print(f"config={config}")
    print(f"passthrough={passthrough}")

    return 0
