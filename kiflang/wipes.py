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


def copies(fn: ir.Function) -> List[Tuple[ir.Store, ir.Local]]:
    """Direct copies of a secure variable into a named variable that is not secure (`let y = x`, `y = x`): the value now also lives in
    storage nobody wipes (KIFW102). Only what the IR shows directly is found; a copy through a call or an unsafe write is not."""
    out = []
    for b in fn.blocks:
        src: Dict[ir.Temp, ir.Local] = {}
        for i in b.instrs:
            if isinstance(i, ir.Load) and i.local.wipe and i.dst.type.kind != "ref":
                src[i.dst] = i.local
            elif isinstance(i, ir.Store) and isinstance(i.src, ir.Temp) and i.src in src and not i.local.wipe and i.local is not src[i.src]:
                out.append((i, src[i.src]))
    return out


def analyze(fn: ir.Function, cfg: ir.CFG) -> int:
    """Insert the wipes of `fn`; returns how many."""
    secure = [l for l in fn.locals if l.wipe]
    if not secure and not any(l.secure_struct for l in fn.locals):
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

    moved = {i.local for _, i in fn.instrs() if isinstance(i, ir.Move) and i.local.secure_struct}
    for b in cfg.rpo:
        for n, i in enumerate(b.instrs):
            if isinstance(i, ir.Dead) and i.local in moved and i.drop != "drop":
                at(b, n, _wipe(i.local, "moved-from slot", i.site))

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


def report(mod: ir.Module, tree, file: str) -> str:
    """The secure-wipe audit of a module as text, for a security review (`kif ir --wipe-report`): every secure struct with its wiped and
    exempt fields, the `#[secure_wipe]` functions, every secure variable with its wipe sites, and the exempt variables. Run it on IR the
    `wipes` pass has been over."""
    from . import ast, astutil

    def line(n) -> int:
        return n.range.start.line + 1

    def has(attrs, name: str) -> bool:
        return any(a.name.text == name for a in attrs)

    out = [f"Secure wipe report: {file}", ""]
    structs = [n for n in astutil.walk(tree) if isinstance(n, ast.StructDecl) and has(n.attrs, "secure_wipe")]
    out.append(f"Secure structs (#[secure_wipe]): {len(structs)}")
    for st in structs:
        wiped = [f.name.text for f in st.fields if not has(f.attrs, "wipe_exempt")]
        exempt = [f.name.text for f in st.fields if has(f.attrs, "wipe_exempt")]
        out.append(f"  {file}:{line(st)}: {st.name.text}: wiped {', '.join(wiped) or 'none'}; exempt {', '.join(exempt) or 'none'}")
    fns = [f for f in mod.functions if f.node is not None and "secure_wipe" in f.attrs]
    out += ["", f"Functions marked #[secure_wipe]: {len(fns)} (every plain local is a secure variable; by-value parameters are not)"]
    out += [f"  {file}:{line(f.node)}: {f.name}" for f in fns]
    out += ["", "Secure variables and their wipe sites:"]
    total = 0
    for f in mod.functions:
        sites = [i for _, i in f.instrs() if isinstance(i, ir.Wipe)]
        for loc in (l for l in f.locals if l.wipe):
            mine = [w for w in sites if w.local is loc]
            total += len(mine)
            kind = "parameter" if loc.param else "variable"
            vol = ", volatile" if loc.wipe_volatile else ""
            out.append(f"  {f.name}: {kind} {loc.name} ({loc.ty}{vol}): {len(mine)} wipe(s)")
            out += [f"    {file}:{line(w.site) if w.site is not None else '?'}: {w.why}" for w in mine]
    out.append(f"  total wipe sites: {total}")
    out += ["", "Secure struct variables (wiped field by field by the struct's drop; a moved-from slot is wiped by kif_wipe_<S>):"]
    for f in mod.functions:
        sites = [i for _, i in f.instrs() if isinstance(i, ir.Wipe)]
        for loc in (l for l in f.locals if l.secure_struct):
            mine = [w for w in sites if w.local is loc]
            kind = "parameter" if loc.param else "variable"
            out.append(f"  {f.name}: {kind} {loc.name} ({loc.ty}): {len(mine)} moved-from wipe(s)")
            out += [f"    {file}:{line(w.site) if w.site is not None else '?'}: {w.why}" for w in mine]
    exempt_lets = [(f.name, s) for f in mod.functions if f.node is not None and "secure_wipe" in f.attrs
                   for s in astutil.walk(f.node.body) if isinstance(s, ast.Let) and has(s.attrs, "wipe_exempt")]
    out += ["", f"Exempt variables (#[wipe_exempt]): {len(exempt_lets)}"]
    out += [f"  {file}:{line(s)}: {fn}: {s.name.text}" for fn, s in exempt_lets]
    return "\n".join(out) + "\n"


def describe_local(mod, pos, name: str) -> Optional[str]:
    """Hover text for the secure variable or parameter `name` at `pos`: why it is wiped and where (lines of its wipe sites), or None for an
    ordinary variable. It builds the IR of the function around `pos` and runs the `wipes` pass on it."""
    from . import passes

    if mod.tree is None:
        return None
    try:
        built = ir.build_module(mod.tree)
    except Exception:  # noqa: BLE001 - a half-typed file must not break hover
        return None
    for f in built.functions:
        if f.node is None or not f.node.range.contains(pos):
            continue
        loc = next((l for l in f.locals if l.name == name and (l.wipe or l.secure_struct)), None)
        if loc is None:
            return None
        passes.run_function(f, ["wipes"])
        lines = sorted({w.site.range.start.line + 1 for _, w in f.instrs() if isinstance(w, ir.Wipe) and w.local is loc and getattr(w.site, "range", None) is not None})
        where = f" (line {', '.join(map(str, lines))})" if lines else ""
        if loc.secure_struct and not loc.wipe:
            return (f"**Secure wipe:** `{name}` holds a `#[secure_wipe]` struct: its drop wipes it field by field; if it is moved from, the "
                    f"vacated slot is wiped{where}.")
        how = "volatile stores" if loc.wipe_volatile else "the target's wipe primitive"
        what = "after its last use" if lines else "at scope exit"
        return f"**Secure wipe:** `{name}` is wiped {what}{where} with {how}."
    return None
