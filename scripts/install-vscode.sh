#!/bin/sh
# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause
# Builds the kif VS Code extension in a separate staging directory (the checkout is not touched: no server/ copy, no
# node_modules, no .vsix left behind), bundles the Python language server (kiflang/) into it, and installs it into VS Code.
#
#   scripts/install-vscode.sh                    build and install into the default profile
#   scripts/install-vscode.sh --profile NAME     install into a VS Code profile (extensions are per profile)
#   scripts/install-vscode.sh --out DIR          keep the .vsix in DIR (default: the staging dir, removed afterwards)
#   scripts/install-vscode.sh --no-install       only build (needs --out to keep the result)
#   scripts/install-vscode.sh --code CMD         the VS Code CLI (default: code; e.g. code-insiders, codium)
# Needs node, npm (and network for @vscode/vsce on first use).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
profile= out= install=1 code=${CODE:-code}
while [ $# -gt 0 ]; do
    case "$1" in
        --profile) profile=$2; shift ;;
        --out) out=$2; shift ;;
        --no-install) install= ;;
        --code) code=$2; shift ;;
        -h|--help) sed -n 5,14p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install-vscode.sh: unknown argument $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$install" ] && [ -z "$out" ]; then
    echo "install-vscode.sh: --no-install needs --out DIR, or the .vsix is deleted with the staging directory" >&2
    exit 2
fi
stage=$(mktemp -d "${TMPDIR:-/tmp}/kif-vscode.XXXXXX")
trap 'rm -rf "$stage"' EXIT INT TERM

# the extension sources only: no node_modules, server/ or earlier .vsix
(cd "$here/kif-editor/vscode" && tar -c --exclude=node_modules --exclude=server --exclude='*.vsix' .) | tar -x -C "$stage"
cp "$here/LICENSE"* "$stage/" 2>/dev/null || true
mkdir "$stage/server"
cp -r "$here/kiflang" "$stage/server/"
find "$stage/server" -name __pycache__ -type d -prune -exec rm -rf {} +

cd "$stage"
npm ci --omit=dev --no-audit --no-fund
node --check extension.js
vsix=$stage/kif-language.vsix
npx --yes @vscode/vsce package --out "$vsix"

if [ -n "$out" ]; then
    mkdir -p "$out"
    cp "$vsix" "$out/"
    vsix=$(cd "$out" && pwd)/kif-language.vsix
    echo "built $vsix"
fi
if [ -n "$install" ]; then
    if [ -n "$profile" ]; then
        "$code" --install-extension "$vsix" --force --profile "$profile"
    else
        "$code" --install-extension "$vsix" --force
    fi
    echo "installed; run 'Developer: Reload Window' in VS Code"
fi
