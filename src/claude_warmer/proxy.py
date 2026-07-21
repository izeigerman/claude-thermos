import asyncio
import os
import socket

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster


def find_free_port() -> int:
    """Bind a socket to 127.0.0.1:0, read the assigned port, close, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_master(port: int, addon: object | None) -> DumpMaster:
    """Construct a DumpMaster configured in reverse mode to
    https://api.anthropic.com on 127.0.0.1:<port>, with quiet/no-terminal
    options, registering `addon` if provided. Does not start it."""
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    options = Options(
        mode=[f"reverse:{anthropic_base_url}"],
        listen_host="127.0.0.1",
        listen_port=port,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    master = DumpMaster(
        options,
        loop=loop,
        with_termlog=False,
        with_dumper=False,
    )

    if addon is not None:
        master.addons.add(addon)

    return master
