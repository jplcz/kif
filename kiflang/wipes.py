# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The `wipes` pass: where the secure variables are wiped (docs/kif-secure-wipe.md rule W1, stage 2).

For every `Local` with `wipe` set the pass finds the end of each live range with a backward liveness analysis and puts a `Wipe` there:

* after the last use on a path (a use is a `Load`, `Move`, `Borrow` or `Clobber` of the slot, or of anything derived from a `Borrow`);
* right after a store nobody reads (a dead store);
* at the entry of a parameter that is never used;
* on the edge to a successor where the slot is no longer live, when it stays live on another edge (the edge is split if needed).

A store does not end a range (the slot is one piece of storage, wiped once after its last use, not after every overwritten value).
A slot whose address escapes (a derived reference is stored, or returned) cannot be followed, so it is wiped where it goes out of
scope: before its `Dead`, or before every `return` for a parameter. Wipes sit at the end of a live range, never earlier, so a variable
read in a loop is wiped once after the loop."""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from . import ir, irtypes


def _is_use(i: ir.Instr, loc: ir.Local, derived: Set[ir.Temp]) -> bool:
    if isinstance(i, (ir.Load, ir.Borrow, ir.Clobber)) and i.local is loc:
        return True
    return any(isinstance(v, ir.Temp) and v in derived for v in i.uses())


def _derived(fn: ir.Function, loc: ir.Local) -> Tuple[Set[ir.Temp], bool]:
    """The temps that may point into `loc` (a `Borrow` of it, a `Load` of an array slot, which is its address in C, and whatever is computed from those), and whether one is kept beyond its
    live range (stored into a slot, written through a place, or returned)."""
    d: Set[ir.Temp] = set()
    changed = True
    while changed:
        changed = False
        for _, i in fn.instrs():
            t = i.defs()
            if t is None or t in d or t.type.kind in (irtypes.SCALAR, irtypes.UNIT):
                continue  # a number computed from the slot is a copy, not a way to reach it
            if isinstance(i, (ir.Borrow, ir.Load)) and i.local is loc and (isinstance(i, ir.Borrow) or loc.type.kind == "array") or any(isinstance(v, ir.Temp) and v in d for v in i.uses()):
                d.add(t)
                changed = True
    escapes = False
    for b in fn.blocks:
        for i in b.instrs:
            if isinstance(i, (ir.Store, ir.Clobber)) and isinstance(i.src, ir.Temp) and i.src in d:
                escapes = True
        if isinstance(b.term, ir.Return) and isinstance(b.term.value, ir.Temp) and b.term.value in d:
            escapes = True
    return d, escapes


def _term_uses(b: ir.Block, d: Set[ir.Temp]) -> bool:
    return b.term is not None and any(isinstance(v, ir.Temp) and v in d for v in b.term.uses())


def _wipe(loc: ir.Local, why: str, site=None) -> ir.Wipe:
    return ir.Wipe(loc, "", loc.ty, loc.wipe_volatile, f"{why} of {loc.name!r}", site=site)


def analyze(fn: ir.Function, cfg: ir.CFG) -> int:
    """Insert the wipes of `fn`; returns how many."""
    secure = [l for l in fn.locals if l.wipe]
    if not secure:
        return 0
    declared = {i.local: i.site for _, i in fn.instrs() if isinstance(i, ir.Live)}
    inserts: Dict[ir.Block, List[Tuple[int, ir.Wipe]]] = {}
    edges: Dict[Tuple[ir.Block, ir.Block], List[ir.Wipe]] = {}
    count = 0

    def at(b: ir.Block, pos: int, w: ir.Wipe) -> None:
        nonlocal count
        w.site = w.site or declared.get(w.local)
        inserts.setdefault(b, []).append((pos, w))
        count += 1

    for loc in secure:
        d, escapes = _derived(fn, loc)
        if escapes:
            for b in cfg.rpo:
                for n, i in enumerate(b.instrs):
                    if isinstance(i, ir.Dead) and i.local is loc:
                        at(b, n, _wipe(loc, "scope exit (its address escapes)", i.site))
                if loc.param and isinstance(b.term, ir.Return):
                    at(b, len(b.instrs), _wipe(loc, "return (its address escapes)", b.term.site))
            continue
        live_in: Dict[ir.Block, bool] = {b: False for b in cfg.rpo}
        live_end: Dict[ir.Block, bool] = {b: False for b in cfg.rpo}
        changed = True
        while changed:
            changed = False
            for b in reversed(cfg.rpo):
                end = _term_uses(b, d) or any(live_in[s] for s in cfg.succ[b] if s in live_in)
                live = end
                for i in reversed(b.instrs):
                    if isinstance(i, (ir.Live, ir.Dead)) and i.local is loc:
                        live = False
                    if _is_use(i, loc, d):
                        live = True
                if end != live_end[b] or live != live_in[b]:
                    live_end[b], live_in[b] = end, live
                    changed = True
        for b in cfg.rpo:
            live = live_end[b]
            for n in range(len(b.instrs) - 1, -1, -1):
                i = b.instrs[n]
                if isinstance(i, ir.Store) and i.local is loc:
                    if not live:
                        at(b, n + 1, _wipe(loc, "dead store", i.site))
                    live = True  # a store does not end the range: the slot is wiped once, after the last use of any value in it
                elif isinstance(i, (ir.Live, ir.Dead)) and i.local is loc:
                    live = False
                elif _is_use(i, loc, d):
                    if not live:
                        at(b, n + 1, _wipe(loc, "last use", i.site))
                    live = True
            for s in cfg.succ[b]:
                if live_end[b] and not live_in.get(s, True):
                    edges.setdefault((b, s), []).append(_wipe(loc, "end of live range (branch not using it)", b.term.site))
        if loc.param and not live_in[fn.entry]:
            at(fn.entry, 0, _wipe(loc, "entry: unused parameter"))

    nid = max(b.id for b in fn.blocks) + 1
    for (b, s), ws in edges.items():
        count += len(ws)
        if len(cfg.succ[b]) == 1:
            inserts.setdefault(b, []).extend((len(b.instrs), w) for w in ws)
        elif len(cfg.pred[s]) == 1:
            inserts.setdefault(s, []).extend((0, w) for w in ws)
        else:
            nb = ir.Block(nid, "wipe", list(ws), ir.Jump(s))
            nid += 1
            fn.blocks.append(nb)
            t = b.term
            if isinstance(t, ir.Branch):
                if t.then is s:
                    t.then = nb
                else:
                    t.otherwise = nb
    for b, items in inserts.items():
        for pos, w in sorted(items, key=lambda p: -p[0]):
            b.instrs.insert(pos, w)
    return count
