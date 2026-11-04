# PgBouncer RR — Routing, SQL Rewriting, and Auditing

This repository is a fork of [pgbouncer-fast-switchover](https://github.com/awslabs/pgbouncer-fast-switchover), specifically up until [8056484](https://github.com/awslabs/pgbouncer-fast-switchover/commit/8056484c816899fc97f2f3a3f2dff1ff11393ddf).

This repository provides source patches for PgBouncer. The `pgbouncer_v1.26.x` branch targets PgBouncer 1.26 sources and provides four features:

| Feature | Purpose | Implementation |
| --- | --- | --- |
| Query routing | Select a pool from `[databases]` through a Python callback using the username, SQL, and transaction state | [src/route_connection.c](src/route_connection.c) |
| SQL rewriting | Replace SQL through a Python callback before forwarding it to the backend | [src/rewrite_query.c](src/rewrite_query.c) |
| SQL audit logging | Record the username and original SQL within the rewrite path | [src/rewrite_query.c](src/rewrite_query.c) |
| PROXY protocol v1 | Accept a proxy-reported client address and include it in log prefixes | [proxy-protocol.diff](proxy-protocol.diff) |

The project includes source patches, an installer, configuration examples, and regression tests. It does not provide cross-database joins, distributed transactions, or automatic failover.

## Processing Order and Code Structure

```text
Connection → optional PROXY v1 header → PostgreSQL startup/authentication
Query Q or Parse P → SQL rewriting (log original SQL) → routing → backend pool
```

[src/client.c.diff](src/client.c.diff) hooks both callbacks into query processing. **The routing callback receives the rewritten SQL.** Requests to the admin database enter the upstream admin handler first. Only Q (Simple Query) and P (Parse) packets enter the RR path; Bind/Execute packets do not each trigger routing or rewriting.

[src/pycall.c](src/pycall.c) handles Python module imports, three-argument calls, return-type validation, and exception logging. [src/main.c.diff](src/main.c.diff) registers RR settings, and [Makefile.diff](Makefile.diff) adds the C sources and Python embedding linker flags.

## Installation and Build

Use the Autoconf/Makefile build; the installer does not integrate RR into Meson. Required dependencies include a C11 compiler, Git, patch, make, Autoconf/Automake/Libtool, libevent, and Python development headers and libraries. TLS requires OpenSSL development packages, and documentation generation requires the upstream documentation tools.

Prepare a clean copy of the PgBouncer 1.26 sources and this branch:

```bash
git clone --branch stable-1.26 https://github.com/pgbouncer/pgbouncer.git
git clone --branch pgbouncer_v1.26.x https://github.com/kingour/pgbouncer-rr-patch.git
cd pgbouncer
# Record the source revision: stable-1.26 is a branch, not a pinned 1.26.1 release tag.
git rev-parse HEAD
git submodule update --init
cd ../pgbouncer-rr-patch
./install-pgbouncer-rr-patch.sh ../pgbouncer
cd ../pgbouncer
./autogen.sh
./configure
make
./pgbouncer --version
sudo make install
```

The patches use 1.26.1 sources as their compatibility baseline; validate other 1.26 revisions before use. The installer modifies the target tree in place and copies this project's README and examples into it. It does not roll back on failure, and the installation as a whole is not idempotent. Use a disposable copy. If installation fails, retain the error output and retry with a fresh, clean copy.

Patches are applied in this order: `proxy-protocol.diff` → `rr-config.diff` → optional `format-cmake.diff` → the Makefile, client, main, and bouncer header patches. The installer then copies the RR sources and tests. `format-cmake.diff` is applied only when the target contains `dev/format.sh`; it provides CMake 4 compatibility for the pinned formatter version.

### Selecting a Python Version

The build defaults to `python3-config`. It first tries `--embed --ldflags`, then falls back to `--ldflags` for older Python versions. Failure to obtain headers or linker flags stops Make.

```bash
make PYTHON_CONFIG=/usr/bin/python2.7-config
# Or specify the config tool for your installed Python 3 development package.
make PYTHON_CONFIG=/usr/bin/python3.6-config
```

Install matching Python development libraries. Run `make clean` before switching Python versions; changing the system `python` symlink is unnecessary. Python 2.7/3.6 flag compatibility does not establish compatibility with every older system; CentOS 7, for example, also requires a C11-capable compiler. The test tools below use Python 3 independently of the Python version embedded in the binary.

## Configuration and Startup

Start with [pgbouncer-example.ini](pgbouncer-example.ini), replacing the backend hosts, ports, databases, and authentication file. The root-level [routing_rules.py](routing_rules.py) and [rewrite_query.py](rewrite_query.py) contain example rules that must be adapted to your schema.

| Setting | Default | Description |
| --- | --- | --- |
| `routing_rules_py_module_file` | `not_enabled` | Path to the Python module defining `routing_rules` |
| `rewrite_query_py_module_file` | `not_enabled` | Path to the Python module defining `rewrite_query` |
| `rewrite_query_disconnect_on_failure` | `false` | Disconnect when the query or rewritten result cannot fit in the buffer |
| `log_audit` | `false` | Enable original-SQL logging in the rewrite path; use the strings `true` / `false` |
| `proxy_protocol` | `0` | Accept optional PROXY v1 headers |
| `pkt_buf` | `4096` | Upstream packet buffer size; the project example uses `32768` |
| `pool_mode` | `session` | Upstream default; the project example uses `statement` |

The following configuration routes between two local databases. Create the databases, users, and tables first, and configure backend authentication for the user:

```ini
[databases]
dev = host=127.0.0.1 port=5432 dbname=app_a
dev.1 = host=127.0.0.1 port=5432 dbname=app_a
dev.2 = host=127.0.0.1 port=5432 dbname=app_b

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/users.txt
pool_mode = transaction
pkt_buf = 32768
routing_rules_py_module_file = /etc/pgbouncer/routing_rules.py
rewrite_query_py_module_file = /etc/pgbouncer/rewrite_query.py
rewrite_query_disconnect_on_failure = true
log_audit = true
proxy_protocol = 0
```

[users.txt](users.txt) demonstrates the authentication file format; replace its contents with your own user credentials. Deploy both Python files to the configured paths, then start PgBouncer:

```bash
pgbouncer /etc/pgbouncer/pgbouncer.ini
psql -h 127.0.0.1 -p 6432 -U app_user -d dev
```

Reloading configuration does not reimport Python source code. Modules use Python's import cache, so restart the process after changing rules rather than relying on hot reload.

## Query Routing

Callback interface:

```python
def routing_rules(username, query, in_transaction):
    if in_transaction:
        return None
    if "tablea" in query:
        return "dev.1"
    if "tableb" in query:
        return "dev.2"
    return None
```

Return a database key from `[databases]` or `None`. Returning `None` preserves the current pool; it does not return the connection to its initial default pool. The substring matching above is only an example. Comments, letter case, and matching field names can affect the result; write rules and assertions for your application's SQL.

Python exceptions, invalid return types, and nonexistent database keys disconnect the client. Switching pools is rejected with a disconnect when the existing backend is in a transaction or has a pending request. Rules should preserve the current pool during transactions. Each SQL statement must execute entirely on one backend.

## SQL Rewriting

Callback interface:

```python
def rewrite_query(username, query, in_transaction):
    if query.strip() == "SELECT 1":
        return "SELECT 2"
    return None
```

Return a new SQL string to replace the query, or return the original string or `None` to leave it unchanged. The root-level example rewrites fixed-format sales aggregation queries to use `store_sales` / `product_sales`; create and maintain those tables before using it.

Rewriting requires a complete query packet. If a fragmented packet is incomplete but fits in the buffer, processing waits for the remaining data. When the query or rewritten result exceeds `pkt_buf`, `rewrite_query_disconnect_on_failure` determines whether to preserve the original SQL or disconnect. **Python exceptions and invalid return types always disconnect the client, regardless of this setting.**

SQL rewriting is not a full SQL parser. Extended-query parameter values are not part of the Parse SQL text, so this callback cannot access all bound parameters.

## SQL Audit Logging

```ini
rewrite_query_py_module_file = /etc/pgbouncer/rewrite_query.py
log_audit = true
```

Auditing currently runs within the rewrite path. It records the username and original SQL before calling Python, replacing newlines with spaces. Queries do not reach this log point when rewriting is disabled, the packet is incomplete, or earlier processing skips the query. To log without changing SQL, have `rewrite_query` return `None`.

With verbose mode disabled, `log_audit = true` emits an INFO-level log. Verbose mode takes the DEBUG logging branch instead. Rewritten SQL also has DEBUG logging. Example:

```text
LOG ... rewrite_query: Username => app_user, Orig Query=> SELECT 1
```

Logs use PgBouncer's existing outputs and connection prefixes. They do not include full Bind parameter values, query results, or execution-time auditing. Original SQL can contain application data; configure log access and retention for your use case.

## PROXY Protocol v1

Enable it at an entry point restricted to trusted proxies:

```ini
proxy_protocol = 1
```

Optional `TCP4`, `TCP6`, and `UNKNOWN` headers and fragmented headers are supported. Direct connections and SSL negotiation without a PROXY header continue to work. PROXY protocol v2 is not supported.

For a proxy connected from `127.0.0.1` reporting client address `192.0.2.20`, the log prefix includes:

```text
@192.0.2.20:127.0.0.1:<peer-port>
```

The reported address affects logging only. Authentication and access control still use the actual TCP peer address. This feature does not authenticate the reported source; restrict incoming connections to trusted proxies before enabling it to prevent clients from spoofing logged addresses.

## Testing and Troubleshooting

Run installation checks from the patch repository. The second argument must identify a revision that exists in your local upstream repository:

```bash
bash tests/install-patch.sh /path/to/pgbouncer <revision>
python3 tests/python-config.py
```

The installation check uses a temporary copy to verify source/build inputs, configuration documentation consistency, and the absence of patch reject files. It does not modify the original upstream tree, compile the binary, or start a service. Python config tests cover modern embedding flags, legacy fallback, and failure paths.

After installation and compilation, run the following from the patched PgBouncer directory:

```bash
python3 -m venv /tmp/pgbouncer-test-venv
. /tmp/pgbouncer-test-venv/bin/activate
python -m pip install pytest pytest-asyncio pytest-timeout pytest-xdist psycopg filelock
python -m pytest -o asyncio_default_fixture_loop_scope=function test/test_misc.py -k proxy_protocol -q
python -m pytest -o asyncio_default_fixture_loop_scope=function test/test_proxy_rr.py -q
make -C test check
```

Integration tests require PostgreSQL tools (`initdb`, `pg_ctl`, `postgres`, `psql`, and `pg_config`) and a non-root user allowed to listen on local ports. Tests create temporary instances. PROXY tests cover address types, fragmentation, direct connections, and SSL negotiation. RR tests cover rewriting/routing combinations, Python exceptions and invalid return types (Q/P packets), `None`, invalid routing keys, transaction-time pool-switch protection, and audit settings/log levels.

The HBA check is an upstream unit test, not the full upstream test suite. If HBA compilation cannot find headers from a non-system OpenSSL installation such as Homebrew, pass the corresponding `CPPFLAGS` and `LDFLAGS` explicitly to the test Make command.

Common issues:

- **Patch application fails:** check the target revision, repeated installation, and partially applied patches. Preserve the original errors and reproduce with a clean copy.
- **Python linking fails:** check that `PYTHON_CONFIG`, development libraries, and runtime libraries match.
- **SQL is not rewritten:** check the module path, function name, rule matching, and `pkt_buf`; use verbose logs to investigate.
- **Routing disconnects:** check the returned database key, Python errors, and attempts to switch pools during a transaction.
- **Audit logs are missing:** check that rewriting is enabled, the `log_audit` string is correct, and the expected log level matches verbose mode.

## Attribution and License

This project preserves the PgBouncer RR routing and rewriting design from the original authors and maintains patches for newer PgBouncer versions. Upstream PgBouncer is governed by its own license.

See [LICENSE.txt](LICENSE.txt) and [NOTICE.txt](NOTICE.txt) for this repository's license and attribution. Some historical source files retain their original license statements; preserve those statements when using or redistributing the code.
