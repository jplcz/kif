# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Temporary drops (the `tempdrops` pass, docs/kif-ir-passes.md): where an owned value that no slot holds must be dropped.

A call result, struct literal or `?` value that owns something (`Temp.owns`) is held by nobody unless it is stored, returned, passed
on by value or put in a literal. Two cases leave it to be dropped here:

- **Never handed on** (`mk(n)?.len()`, `mk(n)?;`): used only as a receiver, so it is dropped right after its last use. This is done
  only when every use is a call or `len` whose result borrows nothing from it, and all uses are in the block that defines it (so a
  loop drops it once per iteration, as C++ does at the end of the full expression).
- **Handed on later** (`pair { a: mk(n)?, b: mks(m)? }`): when a later `?` fails, the value built so far is abandoned. A forward
  dataflow over the CFG tracks which such temps are still held; at each `return` the ones still held get a `Release`.

`Release.drop` is "drop" when the value is certainly held at that return and "flag" when it is held on some paths only; a code
generator that cannot keep a flag must refuse the function. A `result<T>` temp is not tracked: its `?` test and `access ok` take it
apart, and nothing is owned on the failing path."""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from . import ir

_CONSUMES = (ir.Store, ir.Make, ir.Clobber, ir.Call)


def _handed_on(i: ir.Instr, t: ir.Temp) -> bool:
    """Whether `i` takes ownership of `t`: stores it, returns it, passes it by value or builds it into a literal."""
    if isinstance(i, ir.Store):
        return i.src is t
    if isinstance(i, ir.Clobber):
        return i.src is t
    if isinstance(i, (ir.Make, ir.Call)):
        return any(a is t for a in i.args)
    if isinstance(i, ir.Return):
        return i.value is t
    return False


def _uses(i: ir.Instr, t: ir.Temp) -> bool:
    return any(u is t for u in i.uses()) or (isinstance(i, ir.Call) and i.recv is t)


def analyze(fn: ir.Function, cfg: ir.CFG) -> int:
    """Insert the `Release`s of `fn`; returns how many it added."""
    done = {i.value.id for _, i in fn.instrs() if isinstance(i, ir.Release)}
    tracked: Dict[int, ir.Temp] = {}
    home: Dict[int, ir.Block] = {}
    for b, i in fn.instrs():
        t = i.defs()
        if t is None or not t.owns or t.id in done or t.type.name == "result" or t.type.kind == "ref":
            continue
        if isinstance(i, (ir.Call, ir.Make, ir.Move)) or (isinstance(i, ir.Access) and i.kind == "ok"):
            tracked[t.id] = t
            home[t.id] = b
    if not tracked:
        return 0
    users: Dict[int, List[Tuple[ir.Block, ir.Instr]]] = {k: [] for k in tracked}
    for b in fn.blocks:
        for i in [*b.instrs, *([b.term] if b.term else [])]:
            for k, t in tracked.items():
                if _uses(i, t):
                    users[k].append((b, i))
    added = 0
    later: Set[int] = set()
    for k, t in tracked.items():
        if any(_handed_on(i, t) for _, i in users[k]):
            later.add(k)
            continue
        if any(b is not home[k] or isinstance(i, ir.Term) or not _borrows_nothing(i, t) for b, i in users[k]):
            continue  # not safe to drop early: left to the code generator to refuse
        blk = home[k]
        last = max((blk.instrs.index(i) for _, i in users[k]), default=-1)
        if last < 0:
            last = next(n for n, i in enumerate(blk.instrs) if i.defs() is t)
        blk.instrs.insert(last + 1, ir.Release(t, "drop", site=blk.instrs[last].site))
        added += 1
    if later:
        added += _on_return(fn, cfg, {k: tracked[k] for k in later})
    return added


def _borrows_nothing(i: ir.Instr, t: ir.Temp) -> bool:
    """Whether `i` uses `t` without keeping a reference into it: a method or `len` whose result is a plain value."""
    if isinstance(i, ir.Len):
        return True
    if isinstance(i, ir.Call) and i.recv is t and i.dst is not None:
        d = i.dst
        return bool(d.ty) and "?" not in d.ty and d.type.kind != "ref" and not d.owns and not any(a is t for a in i.args)
    return isinstance(i, ir.Call) and i.recv is t and i.dst is None


def _on_return(fn: ir.Function, cfg: ir.CFG, tracked: Dict[int, ir.Temp]) -> int:
    """Release, at every `return`, the temps that are defined and not yet handed on."""
    entry: Dict[ir.Block, Tuple[Set[int], Set[int]]] = {}

    def run(b: ir.Block, st: Tuple[Set[int], Set[int]]) -> Tuple[Set[int], Set[int]]:
        maybe, must = set(st[0]), set(st[1])
        for i in [*b.instrs, *([b.term] if b.term else [])]:
            for k, t in tracked.items():
                if _handed_on(i, t):
                    maybe.discard(k)
                    must.discard(k)
            t = i.defs() if isinstance(i, ir.Instr) else None
            if t is not None and t.id in tracked:
                maybe.add(t.id)
                must.add(t.id)
        return maybe, must

    entry[fn.entry] = (set(), set())
    changed = True
    while changed:
        changed = False
        for b in cfg.rpo:
            if b not in entry:
                continue
            out = run(b, entry[b])
            for s in cfg.succ[b]:
                old = entry.get(s)
                new = (set(out[0]), set(out[1])) if old is None else (old[0] | out[0], old[1] & out[1])
                if old is None or new != old:
                    entry[s] = new
                    changed = True
    added = 0
    for b in cfg.rpo:
        if not isinstance(b.term, ir.Return) or b not in entry:
            continue
        maybe, must = run(b, entry[b])
        for k in sorted(maybe):
            b.instrs.append(ir.Release(tracked[k], "drop" if k in must else "flag", site=b.term.site))
            added += 1
    return added
