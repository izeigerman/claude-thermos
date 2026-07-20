import socket

from claude_warmer.proxy import build_master, find_free_port


def test_find_free_port_bindable():
    port = find_free_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_build_master_reverse_options():
    port = find_free_port()

    master = build_master(port, None)

    assert master.options.mode == ["reverse:https://api.anthropic.com"]
    assert master.options.listen_host == "127.0.0.1"
    assert master.options.listen_port == port


def test_build_master_registers_addon():
    port = find_free_port()
    sentinel = object()

    master = build_master(port, sentinel)

    assert any(a is sentinel for a in master.addons.chain)
