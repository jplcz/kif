# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The wipe guard (docs/kif-secure-wipe.md rule W11): no pass may remove, duplicate or move a `Wipe`.

The pass manager takes a `snapshot` of a function before every `transform` pass and calls `verify` after it; a function with no wipe costs
nothing. A broken rule is `KIFW012`, a compiler error naming the pass, the function and the wipe: there is no opt-out.

"Moved" means the wipe is no longer in its block, or the instructions that touch the wiped slot (a load, store, borrow, write, `live`,
`dead`, another wipe) are no longer on the same sides of it. An instruction a pass removed is not a move.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from . import ir


def _touches(i: ir.Instr, loc: ir.Local) -> bool:
    return isinstance(i, (ir.Load, ir.Store, ir.Borrow, ir.Clobber, ir.Live, ir.Dead, ir.Wipe)) and i.local is loc


def has_wipes(fn: ir.Function) -> bool:
    return any(isinstance(i, ir.Wipe) for _, i in fn.instrs())


def snapshot(fn: ir.Function) -> Dict[int, Tuple[ir.Block, ir.Wipe, Tuple[int, ...], Tuple[int, ...]]]:
    """For each wipe (by `id`): its block, and the ids of the instructions of that block that touch its slot, before and after it."""
    out = {}
    for b in fn.blocks:
        for n, i in enumerate(b.instrs):
            if isinstance(i, ir.Wipe):
                before = tuple(id(x) for x in b.instrs[:n] if _touches(x, i.local))
                after = tuple(id(x) for x in b.instrs[n + 1:] if _touches(x, i.local))
                out[id(i)] = (b, i, before, after)
    return out


def _name(w: ir.Wipe) -> str:
    return f"wipe of {w.local.name!r}{w.path}"


def verify(fn: ir.Function, before: Dict) -> List[str]:
    """What a pass did to the wipes of `fn` since `before`: empty when it kept every one where it was."""
    now: Dict[int, List[Tuple[ir.Block, int]]] = {}
    for b in fn.blocks:
        for n, i in enumerate(b.instrs):
            if isinstance(i, ir.Wipe):
                now.setdefault(id(i), []).append((b, n))
    problems: List[str] = []
    for key, places in now.items():
        if len(places) > 1 or key not in before:
            w = next(i for b, _ in places for i in b.instrs if id(i) == key)
            problems.append(f"{_name(w)} was {'duplicated' if key in before else 'created'}")
    for key, (blk, w, pre, post) in before.items():
        if key not in now:
            problems.append(f"{_name(w)} was removed")
            continue
        if len(now[key]) > 1:
            continue  # already reported as a duplicate
        b, n = now[key][0]
        if b is not blk:
            problems.append(f"{_name(w)} was moved to another block")
            continue
        cur_pre = [id(x) for x in b.instrs[:n] if _touches(x, w.local)]
        cur_post = [id(x) for x in b.instrs[n + 1:] if _touches(x, w.local)]
        gone_pre = set(pre) - {id(x) for x in b.instrs}
        if [k for k in pre if k not in gone_pre] != cur_pre or [k for k in post if k not in gone_pre] != cur_post:
            problems.append(f"{_name(w)} was moved relative to the uses of {w.local.name!r}")
    return problems
