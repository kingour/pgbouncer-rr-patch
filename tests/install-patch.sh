#!/bin/bash
# Apply the installer to a disposable checkout of an upstream Git revision.
# Usage: bash tests/install-patch.sh /path/to/pgbouncer [revision]
set -euo pipefail

upstream=$(cd "${1:?Usage: $0 <upstream repository> [revision]}" && pwd)
revision=${2:-v1.26.1}
patchdir=$(cd "$(dirname "$0")/.." && pwd)
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

git -C "$upstream" archive "$revision" | tar -x -C "$workdir"
cd "$patchdir"
./install-pgbouncer-rr-patch.sh "$workdir" > "$workdir/install.log" 2>&1 || {
    cat "$workdir/install.log"
    exit 1
}
cat "$workdir/install.log"
if find "$workdir" -name '*.rej' | grep -q .; then
    echo 'FAIL: patch rejects found' >&2
    exit 1
fi
for name in pycall route_connection rewrite_query; do
    test -f "$workdir/src/$name.c"
    test -f "$workdir/include/$name.h"
    grep -q "src/$name.c" "$workdir/Makefile"
    grep -q "include/$name.h" "$workdir/Makefile"
done
grep -q 'PYTHON_CONFIG ?= python3-config' "$workdir/Makefile"
grep -q -- '--embed --ldflags' "$workdir/Makefile"
echo "PASS: installer applied to $revision with all extension build inputs"
