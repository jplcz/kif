#!/usr/bin/env bash
# Shows what kif does with a secure-wipe program right now: diagnostics, IR, generated C and the wipe plan of a target.
# Usage: ./show.sh [FILE.kif] [TARGET]      (defaults: demo.kif, c; try freebsd-kernel or linux-kernel)
set -u
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
file="${1:-$here/demo.kif}"
[ -f "$here/$file" ] && file="$here/$file"
file="$(realpath "$file")"
target="${2:-c}"
kif() { (cd "$root" && python3 -m kiflang.kif "$@"); }
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

echo "=== diagnostics: kif check $(basename "$file") --target $target"
kif check "$file" --target "$target" && echo "(none)"

echo
echo "=== IR: kif ir (every pass); look for 'wipe' lines"
kif ir "$file" --passes default 2>&1 | grep -v '^\s*$'

echo
echo "=== generated C: kifc --c (look for KIF_SECURE_WIPE)"
(cd "$root" && python3 -m kiflang.kifc --c "$out" --target "$target" "$file") 2>&1 | head -20
for f in "$out"/*.h "$out"/*.c; do [ -f "$f" ] && { echo "--- $(basename "$f")"; cat "$f"; }; done

echo
echo "=== wipe plan of target $target (docs/kif-targets.md, wipe.*)"
(cd "$root" && python3 - "$target" <<'PY'
import sys
from kiflang import target
p = target.load(sys.argv[1]).wipe()
print("macro  :", p.macro)
print("body   :", p.body)
print("volatile:", p.volatile_body)
print("header :", p.header)
print("error  :", p.error or "none (KIFW006 not raised)")
PY
)
