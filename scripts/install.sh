#!/bin/sh
# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause
# Installs the kif tools (kifc, kif, kif-check, kif-mimport, kif-lsp) from this checkout.
#
#   scripts/install.sh            into the active virtualenv / pip environment
#   scripts/install.sh --user     into ~/.local (commands in ~/.local/bin)
#   scripts/install.sh --editable add -e: the installed commands follow this checkout
#   scripts/install.sh --uninstall [--user]
# Other arguments are passed to pip. PYTHON overrides the interpreter.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
py=${PYTHON:-python3}
user= edit= remove=
extra=
for a in "$@"; do
    case "$a" in
        --user) user=--user ;;
        --editable) edit=-e ;;
        --uninstall) remove=1 ;;
        *) extra="$extra $a" ;;
    esac
done
if [ -n "$remove" ]; then
    # shellcheck disable=SC2086
    exec "$py" -m pip uninstall -y $extra jplcz-kif
fi
# shellcheck disable=SC2086
"$py" -m pip install $user $edit $extra "$here"
if [ -n "$user" ]; then
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add \$HOME/.local/bin to PATH" ;; esac
fi
