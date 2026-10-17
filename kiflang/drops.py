# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Drop analysis (the `drops` pass, docs/kif-ir-passes.md): where an owned value must be dropped.

A slot that `owns` a value (`Local.owns`: a vector, a string, a struct with drop glue, ...) is dropped when it goes out of scope or is
overwritten, unless the value was moved out first. Whether the value is still there depends on the path taken, so this is a forward
dataflow over the CFG on a tiny lattice per owning slot: HOLDS (a value is in it), EMPTY (none: not yet stored, or moved out), or both.

It fills in `Dead.drop` and `Store.drop_old`:
- `""`: nothing to drop (the slot is empty on every path),
- `"drop"`: the value is there on every path, drop it unconditionally,
- `"flag"`: it is there on some paths only, a code generator keeps a flag for the slot."""

from __future__ import annotations

from typing import Dict

from . import ir

HOLDS, EMPTY = 1, 2
_WORD = {HOLDS: "drop", HOLDS | EMPTY: "flag", EMPTY: ""}


def _step(st: Dict[ir.Local, int], i: ir.Instr, record: bool) -> None:
    if isinstance(i, ir.Live) and i.local.owns:
        st[i.local] = EMPTY
    elif isinstance(i, ir.Store) and i.local.owns:
        if record and not i.init:
            i.drop_old = _WORD[st.get(i.local, EMPTY)]
        st[i.local] = HOLDS
    elif isinstance(i, ir.Move) and i.local.owns:
        st[i.local] = EMPTY
    elif isinstance(i, ir.Dead) and i.local.owns:
        if record:
            i.drop = _WORD[st.get(i.local, EMPTY)]
        st[i.local] = EMPTY


def analyze(fn: ir.Function, cfg: ir.CFG) -> int:
    """Fill in the drop decisions of `fn`; returns how many drops it found (`Dead`s and overwrites that drop, conditionally or not)."""
    entry: Dict[ir.Block, Dict[ir.Local, int]] = {fn.entry: {p: HOLDS for p in fn.params if p.owns}}
    changed = True
    while changed:
        changed = False
        for b in cfg.rpo:
            st = dict(entry.get(b, {}))
            for i in b.instrs:
                _step(st, i, False)
            for s in cfg.succ[b]:
                old = entry.get(s)
                if old is None:
                    entry[s] = dict(st)
                    changed = True
                    continue
                for loc in set(old) | set(st):
                    new = old.get(loc, EMPTY) | st.get(loc, EMPTY)
                    if old.get(loc) != new:
                        old[loc] = new
                        changed = True
    found = 0
    for b in cfg.rpo:
        st = dict(entry.get(b, {}))
        for i in b.instrs:
            _step(st, i, True)
            found += bool(getattr(i, "drop", "") or getattr(i, "drop_old", ""))
    return found
