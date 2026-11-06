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


def _install_rules(bouncer, body, *, rewrite=True, routing=False):
    rules = bouncer.config_dir / "rr_regression_rules.py"
    rules.write_text(body)
    if rewrite:
        bouncer.admin(f"set rewrite_query_py_module_file = '{rules}'")
    if routing:
        bouncer.admin(f"set routing_rules_py_module_file = '{rules}'")


def _connect(stream_socket):
    stream_socket.sendall(_startup_packet(b"puser1", b"user_passthrough"))
    stream = stream_socket.makefile("rb")
    while True:
        kind, message = _read_message(stream)
        assert kind != b"E", message
        if kind == b"Z":
            return stream


def _query(sock, stream, query, *, parse=False):
    sql = query.encode() + b"\0"
    if parse:
        payload = b"\0" + sql + struct.pack("!H", 0)
        sock.sendall(b"P" + struct.pack("!I", len(payload) + 4) + payload)
        # Failure must send FATAL immediately; do not queue Sync after a packet
        # expected to close the connection (unread TCP data can cause a reset).
    else:
        sock.sendall(b"Q" + struct.pack("!I", len(sql) + 4) + sql)
    messages = []
    while True:
        kind, message = _read_message(stream)
        messages.append((kind, message))
        if kind in (b"E", b"Z"):
            return messages


@pytest.mark.parametrize("function", ["rewrite_query", "routing_rules"])
@pytest.mark.parametrize("body", ["raise ValueError('rr-test')", "return 123"])
@pytest.mark.parametrize("parse", [False, True], ids=["query", "parse"])
def test_rr_python_failure_disconnects(bouncer, function, body, parse):
    _install_rules(
        bouncer,
        f"def {function}(username, query, in_transaction):\n    {body}\n",
        rewrite=function == "rewrite_query",
        routing=function == "routing_rules",
    )
    # Python errors must fail closed even when buffer-failure fallback is enabled.
    bouncer.admin("set rewrite_query_disconnect_on_failure = false")
    expected = b"Python query rewrite failed" if function == "rewrite_query" else b"Python routing failed"
    with socket.create_connection((bouncer.host, bouncer.port), timeout=5) as sock:
        with _connect(sock) as stream:
            messages = _query(sock, stream, "select 42", parse=parse)
            assert messages[-1][0] == b"E"
            assert expected in messages[-1][1]
            assert stream.read(1) == b""


def test_rr_none_preserves_query_and_pool(bouncer):
    _install_rules(
        bouncer,
        "def rewrite_query(username, query, in_transaction):\n    return None\n"
        "def routing_rules(username, query, in_transaction):\n    return None\n",
        routing=True,
    )
    with socket.create_connection((bouncer.host, bouncer.port), timeout=5) as sock:
        with _connect(sock) as stream:
            messages = _query(sock, stream, "select current_database()")
            assert messages[-1][0] == b"Z"
            assert [value for kind, value in messages if kind == b"D"] == [
                struct.pack("!HI", 1, 2) + b"p0"
            ]


@pytest.mark.parametrize("in_transaction", [False, True])
def test_rr_invalid_backend_change_disconnects(bouncer, in_transaction):
    bouncer.admin("set pool_mode = transaction")
    target = "user_passthrough2" if in_transaction else "missing_rr_database"
    _install_rules(
        bouncer,
        "def routing_rules(username, query, in_transaction):\n"
        "    if query.lower() == 'begin':\n        return None\n"
        f"    return '{target}'\n",
        rewrite=False, routing=True,
    )
    with socket.create_connection((bouncer.host, bouncer.port), timeout=5) as sock:
        with _connect(sock) as stream:
            if in_transaction:
                assert _query(sock, stream, "begin")[-1] == (b"Z", b"T")
            messages = _query(sock, stream, "select 42")
            expected = (
                b"Cannot change backend during a transaction or pending query"
                if in_transaction else b"Invalid routing database key"
            )
            assert messages[-1][0] == b"E"
            assert expected in messages[-1][1]
            assert stream.read(1) == b""


@pytest.mark.parametrize("rewrite,audit,verbose,level", [
    (True, True, 0, "LOG"),
    (True, False, 0, None),
    (False, True, 0, None),
    (True, True, 1, "DEBUG"),
])
def test_rr_audit_original_sql(bouncer, rewrite, audit, verbose, level):
    _install_rules(
        bouncer,
        "def rewrite_query(username, query, in_transaction):\n    return 'select 43'\n",
        rewrite=rewrite,
    )
    bouncer.admin(f"set log_audit = {'true' if audit else 'false'}")
    bouncer.admin(f"set verbose = {verbose}")
    with socket.create_connection((bouncer.host, bouncer.port), timeout=5) as sock:
        with _connect(sock) as stream:
            messages = _query(sock, stream, "select 42 /* rr_audit_original */")
            assert messages[-1][0] == b"Z"
            expected = b"43" if rewrite else b"42"
            assert [value for kind, value in messages if kind == b"D"] == [
                struct.pack("!HI", 1, len(expected)) + expected
            ]
    lines = [line for line in bouncer.log_path.read_text().splitlines()
             if "Orig Query=> select 42 /* rr_audit_original */" in line]
    if level is None:
        assert lines == []
    else:
        assert len(lines) == 1
        assert f" {level} " in lines[0]
        assert "rewrite_query: Orig Query=> select 42 /* rr_audit_original */" in lines[0]
        assert "Username =>" not in lines[0]
