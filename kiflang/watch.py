# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kif check --watch` / `kif build --watch`: rerun the command whenever a source, module file or kif.toml changes.

Polling with the standard library only (no inotify dependency, same on every OS). The watched set is every `.kif`/`.kifm`/`.m`
file below the current directory and below the directories of the files named on the command line, plus `kif.toml` and the
sysroot. Hidden and build directories are skipped. For CMake projects `cmake --build` is the primary way to build; this mode is
for quick feedback while editing.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, Iterable, List

EXTENSIONS = (".kif", ".kifm", ".m", ".toml")
SKIP = {"build", "node_modules", "__pycache__"}
DEFAULT_INTERVAL = 0.5


def snapshot(roots: Iterable[str]) -> Dict[str, float]:
    """path -> mtime of every watched file below `roots`."""
    seen: Dict[str, float] = {}
    for root in roots:
        if os.path.isfile(root):
            seen[root] = _mtime(root)
            continue
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP]
            for n in names:
                if n.endswith(EXTENSIONS):
                    p = os.path.join(d, n)
                    seen[p] = _mtime(p)
    return seen


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def roots_for(args: List[str], sysroot: str = "") -> List[str]:
    """The directories to watch: the cwd, the directory of each argument that is a file, and the sysroot."""
    roots = ["."]
    roots += [os.path.dirname(os.path.abspath(a)) for a in args if os.path.isfile(a)]
    if sysroot:
        roots.append(sysroot)
    return roots


def watch(run: Callable[[], int], roots: List[str], interval: float = DEFAULT_INTERVAL, rounds: int = 0) -> int:
    """Run once, then again after every change. `rounds` > 0 stops after that many reruns (tests); 0 runs until Ctrl-C."""
    last = snapshot(roots)
    status = run()
    n = 0
    try:
        while not rounds or n < rounds:
            time.sleep(interval)
            now = snapshot(roots)
            if now == last:
                continue
            changed = sorted(p for p in set(now) | set(last) if now.get(p) != last.get(p))
            last = now
            print(f"\nkif: {os.path.relpath(changed[0])} changed" + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else "") + "; rerunning",
                  file=sys.stderr)
            status = run()
            n += 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    return status
