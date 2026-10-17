# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The IR check of `#[preserves]` (the `contracts` pass, docs/kif-ir-passes.md, "Contracts").

`#[preserves]` promises that the function keeps every fact its caller knew about the referents of the named `&mut` parameters (a
`some` stays `some`, a length stays as long): the provers rely on it at each call instead of forgetting the facts. A function with a
body must keep the promise, so the pass looks for what could break it:

    KIFI030  the body changes a preserved parameter's referent in a way that can break a fact: it writes a field or the whole
             referent, or hands the parameter (or a part of it) to a call whose callee does not preserve that operand

What stays allowed: reads, writes of one element of an array, slice or vector (they change no length, option or field), calls that
`#[preserves]` the operand, and anything that is not a call or a write. A reference kept in another variable, inside a struct or tuple, or taken out of one by a pattern is followed;
one returned and used by the caller is the caller's business (its own writes through the result are checked there). A body-less declaration is trusted: it is the contract."""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from . import ir


def _preserved_locals(fn: ir.Function) -> List[ir.Local]:
    from .ir import _Builder

    if fn.node is None or not fn.attrs and not any(a.name.text == "preserves" for a in getattr(fn.node, "attrs", None) or ()):
        return []
    ops = _Builder.preserves_of(fn.node)
    if not ops:
        return []
    plain = [loc for loc, p in zip(fn.params, fn.node.params) if not p.receiver]
    recv = [loc for loc, p in zip(fn.params, fn.node.params) if p.receiver]
    return [recv[0] if k == -1 else plain[k] for k in ops if (k == -1 and recv) or 0 <= k < len(plain)]


def check(ctx) -> int:
    fn = ctx.fn
    mine = _preserved_locals(fn)
    if not mine:
        return 0
    # temps that carry a mutable reference into a preserved parameter's referent
    carrier: Dict[ir.Temp, ir.Local] = {}
    for _, i in fn.instrs():
        if isinstance(i, ir.Borrow) and i.local in mine and i.mut and not i.path.endswith("[]"):
            carrier[i.dst] = i.local
        elif isinstance(i, ir.Load) and i.local in mine and (i.dst.ty or "").startswith("&mut"):
            carrier[i.dst] = i.local
    # a reference kept in another variable (`let r = &mut *p; g(r)`) is followed: the variable carries the same parameter
    held: Dict[ir.Local, ir.Local] = {}
    grew = True
    while grew:
        grew = False
        for _, i in fn.instrs():
            if isinstance(i, ir.Store) and isinstance(i.src, ir.Temp) and i.src in carrier and i.local not in held and i.local not in mine:
                held[i.local] = carrier[i.src]
                grew = True
            elif isinstance(i, ir.Load) and i.local in held and i.dst not in carrier:
                carrier[i.dst] = held[i.local]
                grew = True
            elif isinstance(i, ir.Make) and i.dst not in carrier:
                src = next((carrier[x] for x in i.args if isinstance(x, ir.Temp) and x in carrier), None)
                if src is not None:
                    carrier[i.dst] = src
                    grew = True
            elif isinstance(i, ir.Project) and isinstance(i.base, ir.Temp) and i.base in carrier and i.dst not in carrier:
                carrier[i.dst] = carrier[i.base]
                grew = True
    found = 0
    seen: Set[tuple] = set()

    def bad(i: ir.Instr, loc: ir.Local, why: str) -> None:
        nonlocal found
        if (id(i), loc.name) in seen:
            return
        seen.add((id(i), loc.name))
        found += 1
        ctx.report("KIFI030", f"`{fn.name}` is `#[preserves]` but {why} `{loc.name}`, which can break a fact the caller relied on", i.site)

    for _, i in fn.instrs():
        if isinstance(i, ir.Clobber) and i.local in mine and not i.elem:
            bad(i, i.local, "writes through")
        elif isinstance(i, ir.Call):
            ops: List[tuple] = ([(-1, i.recv)] if i.recv is not None else []) + list(enumerate(i.args))
            for k, v in ops:
                if isinstance(v, ir.Temp) and v in carrier and not (i.preserves is not None and k in i.preserves):
                    bad(i, carrier[v], f"passes it to `{i.callee}`, which does not preserve it:")
    return found
