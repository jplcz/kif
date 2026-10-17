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
        self.tries: List[Tuple[ir.Test, Origin]] = []       # `?` on tainted data: a barrier goes at the start of both successors
        self.loads: List[Tuple[ir.Access, Origin]] = []     # tainted element loaded through a clean index: a value barrier follows

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
            o = self.of(i.base)
            if report and i.kind == "index" and o is not None and self.any(i.args) is None:
                self.loads.append((i, o))
            self.define(i, self.any([i.base]))
        elif isinstance(i, ir.Call):
            self.call(i, report)
        elif isinstance(i, ir.Test):
            if report and i.kind == "failed" and self.of(i.base) is not None:
                self.tries.append((i, self.of(i.base)))
            self.define(i, None if i.kind == "failed" else self.of(i.base))
        elif isinstance(i, ir.Make):
            if i.kind == "repeat" and len(i.args) == 2:
                self.sink(i.args[1], i.site, "KIFT002", "is used as an array length", report)
            self.define(i, self.any(i.args))
        elif isinstance(i, ir.Barrier):
            self.define(i, self.of(i.a) if i.a is not None else None)   # a barrier does not clean the value: only an #[untaint] function does
        elif isinstance(i, (ir.Global, ir.Str, ir.Cell)):
            self.define(i, None)
        elif isinstance(i, ir.Binary) and i.op == "*" and "mul" in self.fn.taint_sinks:
            for a in (i.a, i.b):
                self.sink(a, i.site, "KIFT002", "is a multiplication operand (this target's multiplier has variable latency)", report)
            self.define(i, self.any(i.uses()))
        elif isinstance(i, (ir.Unary, ir.Binary, ir.Cast, ir.Len, ir.PatTest, ir.Project, ir.Opaque)):
            self.define(i, self.any(i.uses()))

    def check(self, c: ir.Check, report: bool) -> None:
        use = {"index": "is used as an index or range", "range": "is used as an index or range",
               "div": "is a divisor, whose run time depends on the value", "shift": "is a shift count, whose run time depends on the value"}.get(c.kind)
        if use is None:
            return
        op = getattr(c.site, "op", None)
        if c.kind == "div" and ("rem" if op == "%" else "div") not in self.fn.taint_sinks or c.kind == "shift" and "shift" not in self.fn.taint_sinks:
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


class Note(NamedTuple):
    line: int   # 1-based
    col: int    # 1-based
    kind: str   # source | barrier | untaint
    text: str
    function: str
    end_line: int = 0   # 1-based end of the construct, where the editor puts its hint
    end_col: int = 0


def explain(fn: ir.Function) -> List[Note]:
    """What the taint feature did in one function, in source order, for `kif ir --explain-taint` and the editor: the tainted sources,
    every speculation barrier (written or inserted, with the reason) and every `untaint!` reason. Run it on the IR the `barriers`
    pass has been over."""
    out: List[Note] = []

    def at(site) -> Tuple[int, int]:
        r = getattr(site, "range", None)
        return (r.start.line + 1, r.start.col + 1) if r is not None else (0, 0)

    def end(site) -> Tuple[int, int]:
        r = getattr(site, "range", None)
        return (r.end.line + 1, r.end.col + 1) if r is not None else (0, 0)

    if fn.node is not None:
        for p in fn.params:
            if p.taint:
                q = next((x for x in fn.node.params if not x.receiver and x.name.text == p.name), None)
                out.append(Note(*at(q.name if q is not None else fn.node), "source", f"parameter {p.name!r} is tainted", fn.name, *end(q.name if q is not None else fn.node)))
    for _, i in fn.instrs():
        if isinstance(i, ir.Barrier):
            kind = "value barrier" if i.kind == "value" else "fence"
            out.append(Note(*at(i.site), "barrier", f"{kind} ({'implicit: ' + i.why if i.why else 'explicit: spec::' + ('value' if i.kind == 'value' else 'barrier') + '()'})", fn.name, *end(i.site)))
        elif isinstance(i, ir.Call):
            if i.taint == "tainted_result":
                out.append(Note(*at(i.site), "source", f"result of {i.callee}() is tainted", fn.name, *end(i.site)))
            if i.reason:
                out.append(Note(*at(i.site), "untaint", f"{i.callee}(): {i.reason}", fn.name, *end(i.site)))
    return sorted(out, key=lambda n: (n.line, n.col, n.kind))


def report(mod: ir.Module, file: str, imported=None) -> str:
    """The taint audit of a module as text, for a security review (`kif ir --taint-report`): every sanitizer and how often it is called,
    every `#[trusted]` function, every `untaint!` reason, every speculation barrier, and the tainted sources. Run it on IR the
    `barriers` pass has been over, so the implicit barriers are included."""
    notes = [n for f in mod.functions for n in explain(f)]
    calls: Dict[str, int] = {}
    for f in mod.functions:
        for _, i in f.instrs():
            if isinstance(i, ir.Call) and i.taint == "untaint":
                calls[i.callee] = calls.get(i.callee, 0) + 1
    def named(attr: str) -> List[Tuple[int, str, bool]]:
        out = []
        for f in mod.functions:
            n = f.node
            if n is not None and any(a.name.text == attr for a in getattr(n, "attrs", [])):
                out.append((n.name.range.start.line + 1, f.name, any(a.name.text == "untaint" and a.raw is not None for a in n.attrs)))
        return sorted(out)

    lines = [f"Taint report: {file}", ""]
    san = named("untaint")
    lines.append(f"Sanitizers (#[untaint]): {len(san)}")
    lines += [f"  {file}:{ln}: {name}{' (trusted: body not checked)' if trusted else ''}, {calls.get(name, 0)} call site(s)" for ln, name, trusted in san]
    trusted_exports = named("trusted")
    lines += ["", f"Exported functions opted out of taint.exports (#[trusted]): {len(trusted_exports)}"]
    lines += [f"  {file}:{ln}: {name}" for ln, name, _ in trusted_exports]
    sites = [n for n in notes if n.kind == "untaint"]
    lines += ["", f"untaint! sites: {len(sites)}"]
    lines += [f"  {file}:{n.line}:{n.col}: {n.text} [{n.function}]" for n in sites]
    bars = [n for n in notes if n.kind == "barrier"]
    implicit = sum("implicit" in n.text for n in bars)
    lines += ["", f"Speculation barriers: {len(bars)} ({len(bars) - implicit} explicit, {implicit} implicit)"]
    lines += [f"  {file}:{n.line}:{n.col}: {n.text} [{n.function}]" for n in bars]
    src = [n for n in notes if n.kind == "source"]
    lines += ["", f"Tainted sources: {len(src)}"]
    lines += [f"  {file}:{n.line}:{n.col}: {n.text} [{n.function}]" for n in src]
    lines += vendor_lines(imported or {})
    return "\n".join(lines) + "\n"


def vendor_lines(imported) -> List[str]:
    """The audit section for precompiled imports: what each `.kifm` vendor recorded, labelled by where the evidence comes from.
    A checked sanitizer is `vendor-checked` (the vendor's compiler verified its body), a `#[untaint(trusted)]` one `vendor-trusted`
    (nobody verified it: audit it)."""
    out: List[str] = []
    for path, m in sorted(imported.items()):
        sec = getattr(m, "header", None) and m.header["security"]
        if not sec:
            continue
        fns = {k: e for k, e in sec["functions"].items() if not e.get("embedded")}
        san = sorted((k, e["untaint"]) for k, e in fns.items() if e["untaint"])
        trusted = sorted(k for k, e in fns.items() if e["trusted"])
        out += ["", f"Precompiled {path} ({m.header['name']}, target {sec['target']}, profile {sec['profile']}, "
                f"compiler {m.header['compiler']['name']} {m.header['compiler']['version']})"]
        v = getattr(m, "signature", None)
        out.append("  signature: not checked" if v is None else "  signature: unsigned" if v.status == "unsigned" else
                   f"  signature: {v.status}, signed by {v.signer} ({v.fingerprint})")
        out += [f"  sanitizer {k} (from {path}, vendor-{'trusted' if kind == 'trusted' else 'checked'})" for k, kind in san]
        out += [f"  #[trusted] export {k} (from {path})" for k in trusted]
        out += [f"  tainted result {k} (from {path})" for k, e in sorted(fns.items()) if e["tainted_result"]]
    return out


def _is_barrier(i, kind: str) -> bool:
    return isinstance(i, ir.Barrier) and i.kind == kind and bool(i.why)


def insert_barriers(fn: ir.Function, flow: "_Flow") -> int:
    """Rule 5a (docs/kif-taint.md): a fence at the start of both successors of a `?` on tainted data, and a value barrier after a
    tainted element load. Each carries `why`, the audit comment of the generated code. Running twice adds nothing."""
    n = 0
    top = max((t.id for b in fn.blocks for i in b.instrs for t in [i.defs()] if t is not None), default=0)
    for t, o in flow.tries:
        br = next((b.term for b in fn.blocks if isinstance(b.term, ir.Branch) and b.term.cond is t.dst), None)
        for blk in (br.then, br.otherwise) if br is not None else ():
            if not (blk.instrs and _is_barrier(blk.instrs[0], "fence")):
                blk.instrs.insert(0, ir.Barrier("fence", why=f"`?` on tainted data ({o.what}, line {o.line}) [rule 5a]", site=t.site))
                n += 1
    for acc, o in flow.loads:
        for blk in fn.blocks:
            if acc in blk.instrs:
                k = blk.instrs.index(acc)
                if k + 1 < len(blk.instrs) and _is_barrier(blk.instrs[k + 1], "value") and blk.instrs[k + 1].a is not None:
                    break
                top += 1
                raw = ir.Temp(top, acc.dst.ty)
                orig, acc.dst = acc.dst, raw
                blk.instrs.insert(k + 1, ir.Barrier("value", orig, raw, why=f"tainted element load ({o.what}, line {o.line}) [rule 5a]", site=acc.site))
                n += 1
                break
    return n


def barriers(ctx) -> int:
    """The `barriers` transform: run the flow, then insert what rule 5a asks for."""
    fn = ctx.fn
    if not fn.blocks or "trusted" in fn.attrs:
        return 0
    flow = _Flow(fn, ctx.get("cfg"))
    flow.run()
    return insert_barriers(fn, flow)


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
