# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The IR backstop of the taint check (docs/kif-taint.md 6.1): a second opinion on the block graph.

The syntax-tree walker (taint.py) is the authority and gives the messages people read. This pass recomputes taint on the IR, where
every implicit decision is explicit: a `Branch` (an `if`, a `for` desugaring, a `match`), a `Check` (an index, a division, a shift)
and an `Access`. It therefore catches what the IR builder or a later rewrite introduces, and it is the check that can run after
optimisation passes. It reports the same codes with the same message format:

* `KIFT001` a tainted `Branch` condition;
* `KIFT002` a tainted index, range bound, divisor, shift amount, array length or element-write index;
* `KIFT008` a tainted function value, or a tainted argument of a call through a function value.

The declaration checks (`KIFT003`-`KIFT005`) depend on declared types, which the IR does not carry, so they stay with the walker.

Sources are the `#[tainted]` parameters (`Local.taint`) and the results of `#[tainted_result]` calls (`Call.taint`). A call of an
`#[untaint]` function gives a clean result. Taint is kept per slot (a whole-slot `Store` replaces it, a `Clobber` adds to it,
a join takes the union) and per temp, and spreads through every instruction that reads a tainted value. `?` (`Test failed`) is
the one decision on tainted data that is allowed: its result is clean, because the code generator puts a speculation barrier on
both successors."""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from . import ir
from .taint import MITIGATIONS


class Origin(NamedTuple):
    what: str
    line: int


Taint = Optional[Origin]
State = Dict[int, Origin]   # local id -> origin; an absent slot is clean


def _join(a: Taint, b: Taint) -> Taint:
    return a if a is not None else b


class _Flow:
    def __init__(self, fn: ir.Function, cfg) -> None:
        self.fn, self.cfg = fn, cfg
        self.temps: Dict[int, Origin] = {}
        self.found: Dict[Tuple[str, int, int], Tuple[str, object, Origin, str]] = {}

    # -- values

    def of(self, v) -> Taint:
        return self.temps.get(v.id) if isinstance(v, ir.Temp) else None

    def any(self, vs) -> Taint:
        t: Taint = None
        for v in vs:
            t = _join(t, self.of(v))
        return t

    def sink(self, v, site, code: str, use: str, report: bool) -> None:
        o = self.of(v)
        rng = getattr(site, "range", None)
        if o is None or not report or rng is None:
            return
        self.found.setdefault((code, rng.start.line, rng.start.col), (code, site, o, use))

    # -- one block

    def block(self, b: ir.Block, st: State, report: bool) -> None:
        for i in b.instrs:
            self.instr(i, st, report)
        t = b.term
        if isinstance(t, ir.Branch):
            self.sink(t.cond, t.site, "KIFT001", "decides a branch condition", report)

    def define(self, i, o: Taint) -> None:
        d = i.defs()
        if d is None:
            return
        if o is None:
            self.temps.pop(d.id, None)
        else:
            self.temps[d.id] = self.temps.get(d.id) or o

    def instr(self, i: ir.Instr, st: State, report: bool) -> None:
        if isinstance(i, ir.Load):
            self.define(i, st.get(i.local.id))
        elif isinstance(i, ir.Store):
            o = self.of(i.src)
            if o is None:
                st.pop(i.local.id, None)
            else:
                st[i.local.id] = o
        elif isinstance(i, ir.Clobber):
            if i.idx is not None:
                self.sink(i.idx, i.site, "KIFT002", "is used as an index or range", report)
            o = _join(self.of(i.src), self.of(i.idx) if i.idx is not None else None)
            if i.local is not None and o is not None:
                st.setdefault(i.local.id, o)
        elif isinstance(i, ir.Borrow):
            for v in list(i.rng) + ([i.idx] if i.idx is not None else []):
                self.sink(v, i.site, "KIFT002", "is used as an index or range", report)
            self.define(i, st.get(i.local.id))
        elif isinstance(i, ir.Check):
            if not i.tag:
                self.check(i, report)
        elif isinstance(i, ir.Access):
            if i.kind in ("index", "range"):
                for a in i.args:
                    self.sink(a, i.site, "KIFT002", "is used as an index or range", report)
            self.define(i, self.any([i.base]))
        elif isinstance(i, ir.Call):
            self.call(i, report)
        elif isinstance(i, ir.Test):
            self.define(i, None if i.kind == "failed" else self.of(i.base))
        elif isinstance(i, ir.Make):
            if i.kind == "repeat" and len(i.args) == 2:
                self.sink(i.args[1], i.site, "KIFT002", "is used as an array length", report)
            self.define(i, self.any(i.args))
        elif isinstance(i, (ir.Global, ir.Str, ir.Cell)):
            self.define(i, None)
        elif isinstance(i, (ir.Unary, ir.Binary, ir.Cast, ir.Len, ir.PatTest, ir.Project, ir.Opaque)):
            self.define(i, self.any(i.uses()))

    def check(self, c: ir.Check, report: bool) -> None:
        use = {"index": "is used as an index or range", "range": "is used as an index or range",
               "div": "is a divisor, whose run time depends on the value", "shift": "is a shift count, whose run time depends on the value"}.get(c.kind)
        if use is None:
            return
        picked = {"index": c.args[:1], "range": c.args[:2], "div": c.args[1:2], "shift": c.args[:1]}[c.kind]
        for a in picked:
            self.sink(a, c.site, "KIFT002", use, report)

    def call(self, c: ir.Call, report: bool) -> None:
        if c.kind == "value" and c.args:
            self.sink(c.args[0], c.site, "KIFT008", "selects the call target", report)
            for a in c.args[1:]:
                self.sink(a, c.site, "KIFT008", "is passed through an indirect call (a function value), whose target body cannot be checked", report)
            self.define(c, self.any(c.uses()))
        elif c.taint == "untaint" or c.callee == "ct::clamp":
            self.define(c, None)
        elif c.taint == "tainted_result":
            self.define(c, Origin(f"result of {c.callee}()", c.site.range.start.line + 1 if c.site is not None and getattr(c.site, "range", None) else 0))
        else:
            self.define(c, self.any(c.uses()))

    # -- the fixpoint

    def run(self) -> None:
        fn = self.fn
        entry: State = {}
        for p in fn.params:
            if p.taint:
                line = fn.node.params[0].name.range.start.line + 1 if fn.node is not None and fn.node.params else 0
                for q in (fn.node.params if fn.node is not None else []):
                    if not q.receiver and q.name.text == p.name:
                        line = q.name.range.start.line + 1
                entry[p.id] = Origin(f"parameter {p.name!r}", line)
        ins: Dict[int, State] = {fn.blocks[0].id: entry}
        order = list(fn.blocks)
        for _ in range(len(order) + 4):
            changed = False
            for b in order:
                if b.id not in ins:
                    continue
                st = dict(ins[b.id])
                self.block(b, st, False)
                for s in (b.term.targets() if b.term is not None else []):
                    cur = ins.get(s.id)
                    if cur is None:
                        ins[s.id] = dict(st)
                        changed = True
                        continue
                    for k, o in st.items():
                        if k not in cur:
                            cur[k] = o
                            changed = True
            if not changed:
                break
        for b in order:
            if b.id in ins:
                self.block(b, dict(ins[b.id]), True)


def check(ctx) -> int:
    """Report every tainted decision or address of the function; returns how many."""
    fn = ctx.fn
    if not fn.blocks or any(a in ("trusted",) for a in fn.attrs):
        return 0
    flow = _Flow(fn, ctx.get("cfg"))
    flow.run()
    for code, site, o, use in sorted(flow.found.values(), key=lambda x: (x[1].range.start.line, x[1].range.start.col, x[0])):
        ctx.report(code, f"tainted data ({o.what}, line {o.line}) {use}; {MITIGATIONS[code]}", site)
    return len(flow.found)
