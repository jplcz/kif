# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The `ct::` optimizer guard (docs/kif-taint.md section 7a, rules G1-G7).

A branch-free sanitizer must still be branch-free after the kif passes ran. The pass manager takes a `snapshot` of a function before
every `transform` pass and calls `verify` after it; a function with no *ct region* costs nothing. A broken rule is `KIFT012`, a
compiler error that names the pass, the function and the rule: there is no opt-out.

The region is every `ct::` intrinsic call, every instruction computed from one, and the whole body of an `#[untaint]` function that is not
`trusted`. It is marked on the instructions as the attribute `ct` (not a dataclass field, so the IR text, the serialised form and the IR
hash do not change). A pass that creates an instruction from a tagged one must set `ct` on it too (`inherit`).

G7 is the same promise for the printed C: `verify_text` checks that after textual folding every `ct` intrinsic is still followed by its
value barrier.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from . import ir


class Snapshot(NamedTuple):
    branches: frozenset      # ids of every `Branch` (G1, G6)
    keep: Tuple[int, ...]    # ids of the `ct::` calls and the barriers, in block order (G3, G4)
    consts: frozenset        # ids of the `ct::` calls whose operands are all constants: these may fold
    findings: frozenset      # (code, line, col) the taint check reported (G2)
    by_id: Dict[int, str]    # a name for each id above, for the message


def is_ct_call(i) -> bool:
    return isinstance(i, ir.Call) and i.kind == "intrinsic" and i.callee.startswith("ct::")


def inherit(new: ir.Instr, old: ir.Instr) -> ir.Instr:
    """Give an instruction a pass created from `old` the region tag of `old`."""
    if getattr(old, "ct", False):
        new.ct = True
    return new


def mark(fn: ir.Function) -> bool:
    """Tag the region of `fn` (idempotent); True when the function has one."""
    if not fn.blocks:
        return False
    whole = "untaint" in fn.attrs and "trusted" not in fn.attrs
    live: Set[int] = set()
    found = False
    for _b, i in fn.instrs():
        d = i.defs()
        hit = whole or is_ct_call(i) or any(getattr(u, "id", None) in live and type(u) is ir.Temp for u in i.uses())
        if hit:
            i.ct = True
            found = True
            if d is not None:
                live.add(d.id)
        elif getattr(i, "ct", False):
            found = True
    return found


def has_region(fn: ir.Function) -> bool:
    return any(getattr(i, "ct", False) for _b, i in fn.instrs())


def _taint_findings(fn: ir.Function, resolve=None, config=None) -> frozenset:
    from . import passes, taint_ir

    ctx = passes.Context(fn, resolve, dict(config or {}))
    ctx.results["cfg"] = ir.CFG(fn)
    taint_ir.check(ctx)
    return frozenset((d.code, d.range.start.line, d.range.start.col) for d in ctx.diagnostics)


def snapshot(fn: ir.Function, resolve=None, config=None) -> Snapshot:
    branches = frozenset(id(b.term) for b in fn.blocks if isinstance(b.term, ir.Branch))
    keep: List[int] = []
    names: Dict[int, str] = {}
    consts: Set[int] = set()
    for _b, i in fn.instrs():
        if is_ct_call(i) or isinstance(i, ir.Barrier):
            keep.append(id(i))
            names[id(i)] = i.callee if isinstance(i, ir.Call) else f"{i.kind} barrier"
            if is_ct_call(i) and all(isinstance(a, ir.Const) for a in i.args):
                consts.add(id(i))
    return Snapshot(branches, tuple(keep), frozenset(consts), _taint_findings(fn, resolve, config), names)


def verify(fn: ir.Function, before: Snapshot, resolve=None, config=None) -> List[str]:
    """The rules the function broke since `before` was taken, each `Gn: what` (empty: the guard is satisfied)."""
    out: List[str] = []
    for b in fn.blocks:   # G1, G6: a conditional terminator that was not there before
        if isinstance(b.term, ir.Branch) and id(b.term) not in before.branches:
            line = b.term.site.range.start.line + 1 if b.term.site is not None else 0
            out.append(f"G1: a conditional branch was created in the region" + (f" (line {line})" if line else ""))
    now: List[int] = []
    for _b, i in fn.instrs():
        if is_ct_call(i) or isinstance(i, ir.Barrier):
            now.append(id(i))
    present = set(now)
    for k in before.keep:   # G3, G4: nothing the region relies on is gone
        if k not in present and k not in before.consts:
            rule = "G3" if not before.by_id[k].endswith("barrier") else "G4"
            out.append(f"{rule}: {before.by_id[k]} was removed")
    order = [k for k in now if k in set(before.keep)]
    wanted = [k for k in before.keep if k in present]
    if order != wanted:   # G3, G4: and not moved across another
        out.append("G4: a ct:: operation or barrier was reordered")
    for f in sorted(_taint_findings(fn, resolve, config) - before.findings):   # G2: nothing new for the taint check
        out.append(f"G2: the taint check now reports {f[0]} (line {f[1] + 1}) that it did not before")
    return out


def verify_text(lines: List[str], barrier) -> List[str]:
    """G7 on the printed C: after each `/* kif: ct intrinsic ... */` comment come the definition `kif_tN = ...;` and then the target's
    barrier statement for `kif_tN` (`barrier(v)` renders it), still in this order and adjacent."""
    import re

    out: List[str] = []
    for n, l in enumerate(lines):
        if "kif: ct intrinsic" not in l:
            continue
        rest = [x.strip() for x in lines[n + 1:n + 3]]
        m = re.match(r"(kif_t\d+) = ", rest[0]) if rest else None
        if m is None or len(rest) < 2 or rest[1] != barrier(m.group(1)) + ";":
            out.append(f"G7: the ct:: operation at output line {n + 1} lost its value barrier or its place")
    return out
