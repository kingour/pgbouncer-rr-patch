import socket
import struct

import pytest

from .test_misc import _read_message, _startup_packet


@pytest.mark.parametrize("proxied", [False, True])
@pytest.mark.parametrize("routed", [False, True])
def test_proxy_with_routing_and_rewrite(bouncer, proxied, routed):
    rules = bouncer.config_dir / "proxy_rr_rules.py"
    rules.write_text(
        "def rewrite_query(username, query, in_transaction):\n"
        "    return 'select current_database()'\n\n"
        "def routing_rules(username, query, in_transaction):\n"
        "    return 'user_passthrough2'\n"
    )
    bouncer.admin("set proxy_protocol = 1")
    bouncer.admin(f"set rewrite_query_py_module_file = '{rules}'")
    if routed:
        bouncer.admin(f"set routing_rules_py_module_file = '{rules}'")

    with socket.create_connection((bouncer.host, bouncer.port), timeout=5) as sock:
        header = b"PROXY TCP4 192.0.2.20 127.0.0.1 12345 6432\r\n" if proxied else b""
        # Use pools with the same backend identity when routing between them.
        sock.sendall(header + _startup_packet(b"puser1", b"user_passthrough"))
        with sock.makefile("rb") as stream:
            while True:
                kind, message = _read_message(stream)
                assert kind != b"E", message
                if kind == b"Z":
                    break
            query = b"select 42\0"
            sock.sendall(b"Q" + struct.pack("!I", len(query) + 4) + query)
            rows = []
            while True:
                kind, message = _read_message(stream)
                assert kind != b"E", message
                if kind == b"D":
                    rows.append(message)
                if kind == b"Z":
                    break
            expected = b"p2" if routed else b"p0"
            assert rows == [struct.pack("!HI", 1, len(expected)) + expected]
        if proxied:
            assert "@192.0.2.20:127.0.0.1:" in bouncer.log_path.read_text()
