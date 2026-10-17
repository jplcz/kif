# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The IR borrow check (the `borrows` pass, docs/kif-ir-passes.md, "Borrow check").

A `Borrow` creates a *loan* on a part of a slot. A loan travels with the references derived from it: through temps, `Store`s into
slots, struct literals and calls (a result takes the loans of the operands its lifetimes name, `Call.flows`). A loan is *live* while
a temp or slot that holds it is still read later. A conflicting access while a loan is live is an error, which is what non-lexical
lifetimes amount to; the same access with no live holder is fine.

    KIFI020  a value is used after the slot it borrows was written, moved or borrowed incompatibly
    KIFI021  a reference outlives its referent (the slot dies, or a returned value borrows a by-value parameter)
    KIFI022  two conflicting borrows among the operands of one call

Unlike the checker's tracking (kiflang/bodies.py), this works on the block graph, so loans carried round a loop and through the
results of `if` / `match` are followed. Two loans on different constant elements (`a[0]`, `a[1]`) do not conflict; a variable
index is assumed to hit any element."""

from __future__ import annotations

from typing import Callable, Dict, FrozenSet, List, Optional, Set

from . import ir, places

EMPTY: FrozenSet[ir.Borrow] = frozenset()
State = Dict[ir.Local, FrozenSet[ir.Borrow]]


def carries(ty: Optional[str]) -> bool:
    """Can a value of this type hold a reference? (`&`, or a type with a lifetime argument)"""
    return bool(ty) and ("&" in ty or "'" in ty)


def _through(b: "ir.Borrow | ir.Clobber") -> bool:
    """A place reached through a reference held in the slot: it reborrows or writes the referent, not the slot."""
    return b.path.startswith("*") or (b.local is not None and (b.local.ty or "").startswith("&"))


def _overlap(p: str, pi: Optional[ir.Value], q: str, qi: Optional[ir.Value]) -> bool:
    return places.overlap(p, pi.value if isinstance(pi, ir.Const) else None, q, qi.value if isinstance(qi, ir.Const) else None)


def _temps(vals) -> List[ir.Temp]:
    return [v for v in vals if isinstance(v, ir.Temp)]


class Checker:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.fn: ir.Function = ctx.fn
        self.cfg: ir.CFG = ctx.get("cfg")
        self.tl: Dict[ir.Temp, FrozenSet[ir.Borrow]] = {}
        self.reported: Set[tuple] = set()
        self.reserved = self.two_phase()
        self.shape_only = self.shape_only_loads()

    def two_phase(self) -> Set[ir.Borrow]:
        """The `mut` borrows of a call's receiver: the reference is only reserved while the arguments are evaluated and becomes active at the
        call, so `v.push(v.len())` may read `v` in between (the same two-phase borrow rule as Rust's)."""
        uses: Dict[ir.Temp, List] = {}
        for b in self.fn.blocks:
            for i in [*b.instrs, *([b.term] if b.term else [])]:
                for t in _temps(i.uses()):
                    uses.setdefault(t, []).append(i)
        return {i for _, i in self.fn.instrs() if isinstance(i, ir.Borrow) and i.mut and len(uses.get(i.dst, [])) == 1 and isinstance(uses[i.dst][0], ir.Call) and uses[i.dst][0].recv is i.dst}

    def shape_only_loads(self) -> Set[ir.Temp]:
        """Loads of a slot whose value nobody reads except for its length (`Len`), or at all: the load that precedes an element borrow or an
        index check. Neither reads an element, so an element held by a `mut` loan does not conflict with it."""
        uses: Dict[ir.Temp, List] = {}
        for b in self.fn.blocks:
            for i in [*b.instrs, *([b.term] if b.term else [])]:
                for t in _temps(i.uses()):
                    uses.setdefault(t, []).append(i)
        return {i.dst for _, i in self.fn.instrs() if isinstance(i, ir.Load) and not isinstance(i, ir.Move) and all(isinstance(u, ir.Len) for u in uses.get(i.dst, ()))}

    # -- liveness: which slots and temps are read later --------------------------------------------------------------

    def reads(self, i: "ir.Instr | ir.Term") -> set:
        out = set(_temps(i.uses()))
        if isinstance(i, ir.Load) and carries(i.local.ty):
            out.add(i.local)
        elif isinstance(i, (ir.Borrow, ir.Clobber)) and i.local is not None and _through(i):
            out.add(i.local)
        return out

    def live(self) -> Dict[ir.Block, set]:
        live_in: Dict[ir.Block, set] = {b: set() for b in self.cfg.rpo}
        changed = True
        while changed:
            changed = False
            for b in reversed(self.cfg.rpo):
                cur = set()
                for s in self.cfg.succ[b]:
                    cur |= live_in.get(s, set())
                for i in [b.term, *reversed(b.instrs)] if b.term else reversed(b.instrs):
                    self.backward(cur, i)
                if cur != live_in[b]:
                    live_in[b] = cur
                    changed = True
        return live_in

    def backward(self, cur: set, i: "ir.Instr | ir.Term") -> None:
        d = i.defs() if isinstance(i, ir.Instr) else None
        if d is not None:
            cur.discard(d)
        if isinstance(i, (ir.Store, ir.Live, ir.Dead)):
            cur.discard(i.local)
        cur |= self.reads(i)

    # -- loans -------------------------------------------------------------------------------------------------------

    def of(self, v) -> FrozenSet[ir.Borrow]:
        return self.tl.get(v, EMPTY) if isinstance(v, ir.Temp) else EMPTY

    def held(self, live: set, st: State) -> List[ir.Borrow]:
        """The loans some live temp or slot still holds."""
        out: Dict[ir.Borrow, None] = {}
        for h in live:
            for ln in (self.tl.get(h, EMPTY) if isinstance(h, ir.Temp) else st.get(h, EMPTY)):
                out[ln] = None
        return list(out)

    def step(self, st: State, i: ir.Instr, after: Optional[set]) -> None:
        """Apply `i`; with `after` (what is read after it) also report conflicts."""
        rep = after is not None
        if rep and i.defs() is not None:
            after = after - {i.defs()}  # what the instruction defines is new: the old value of that temp (a loop's last trip) is not held
        if isinstance(i, ir.Load):
            if rep and i.dst not in self.shape_only:
                self.access(i, i.local, "", None, "move" if isinstance(i, ir.Move) else "read", self.held(after, st))
            self.tl[i.dst] = st.get(i.local, EMPTY) if carries(i.dst.ty or i.local.ty) else EMPTY
        elif isinstance(i, ir.Store):
            if rep:
                self.access(i, i.local, "", None, "write", self.held(after, st))
            src = self.of(i.src)
            if carries(i.local.ty) and src:
                st[i.local] = src
            else:
                st.pop(i.local, None)
        elif isinstance(i, ir.Clobber):
            src = self.of(i.src)
            if i.local is not None and not _through(i):
                if rep:
                    self.access(i, i.local, i.path, i.idx, "write", self.held(after, st))
                if src:
                    st[i.local] = st.get(i.local, EMPTY) | src
        elif isinstance(i, ir.Borrow):
            if _through(i):
                self.tl[i.dst] = st.get(i.local, EMPTY)
            else:
                if rep:
                    self.access(i, i.local, i.path, i.idx, "mut" if i.mut else "shared", self.held(after, st))
                self.tl[i.dst] = frozenset([i])
        elif isinstance(i, ir.Live):
            st.pop(i.local, None)
        elif isinstance(i, ir.Dead):
            if rep:
                self.dangling(i, i.local, self.held(after, st), "its scope ended here")
            st.pop(i.local, None)
        elif isinstance(i, ir.Call):
            ops = ([i.recv] if i.recv is not None else []) + list(i.args)
            if rep:
                self.call_conflicts(i, ops)
            if i.dst is not None and carries(i.dst.ty):
                if i.flows is None:
                    pick = ops
                else:
                    pick = [(i.recv if k < 0 else i.args[k]) for k in i.flows if (k < 0 and i.recv is not None) or 0 <= k < len(i.args)]
                self.tl[i.dst] = frozenset().union(*[self.of(v) for v in pick])
        else:
            d = i.defs()
            if d is not None:
                self.tl[d] = frozenset().union(*[self.of(v) for v in i.uses()]) if carries(d.ty) else EMPTY

    # -- conflicts ---------------------------------------------------------------------------------------------------

    def say(self, code: str, msg: str, site, ln: ir.Borrow) -> None:
        key = (code, id(site), id(ln))
        if key not in self.reported:
            self.reported.add(key)
            self.ctx.report(code, msg, site or ln.site)

    def access(self, i, local: ir.Local, path: str, idx, kind: str, loans: List[ir.Borrow]) -> None:
        """`kind`: read | write | move | shared | mut. A shared loan only conflicts with writes, moves and `mut` borrows."""
        for ln in loans:
            if ln.local is not local or not _overlap(ln.path, ln.idx, path, idx):
                continue
            if kind in ("read", "shared") and (not ln.mut or ln in self.reserved):
                continue
            how = {"read": "read", "write": "written", "move": "moved", "shared": "borrowed", "mut": "borrowed mutably"}[kind]
            what = "mutably borrowed" if ln.mut else "borrowed"
            line = ln.site.range.start.line + 1 if ln.site is not None and getattr(ln.site, "range", None) else 0
            self.say("KIFI020", f"{local.name!r} is {how} here while it is {what} (line {line}) by a reference that is used later", i.site, ln)

    def dangling(self, i, local: ir.Local, loans: List[ir.Borrow], why: str) -> None:
        for ln in loans:
            if ln.local is local:
                self.say("KIFI021", f"a reference to {local.name!r} is used after {why}", i.site, ln)

    def call_conflicts(self, i: ir.Call, ops: list) -> None:
        seen: List[ir.Borrow] = []
        for v in ops:
            mine = [ln for ln in self.of(v) if ln not in seen]
            for ln in mine:
                for o in seen:
                    if o.local is ln.local and (o.mut or ln.mut) and _overlap(o.path, o.idx, ln.path, ln.idx):
                        self.say("KIFI022", f"{ln.local.name!r} is borrowed {'mutably ' if ln.mut else ''}by two operands of one call", i.site, ln)
            seen.extend(mine)

    # -- driver ------------------------------------------------------------------------------------------------------

    def run(self) -> int:
        live_in = self.live()
        entry: Dict[ir.Block, State] = {self.fn.entry: {}}
        work = list(self.cfg.rpo)
        while work:
            b = work.pop(0)
            st = dict(entry.get(b, {}))
            for i in b.instrs:
                self.step(st, i, None)
            for s in self.cfg.succ[b]:
                old = entry.get(s)
                new = dict(st) if old is None else {k: old.get(k, EMPTY) | st.get(k, EMPTY) for k in set(old) | set(st)}
                if old != new:
                    entry[s] = new
                    work.append(s)
        for b in self.cfg.rpo:
            st = dict(entry.get(b, {}))
            after = set()
            for s in self.cfg.succ[b]:
                after |= live_in.get(s, set())
            if b.term:
                self.backward(after, b.term)
            tail: List[set] = []
            for i in reversed(b.instrs):
                tail.append(set(after))
                self.backward(after, i)
            tail.reverse()
            for i, live_after in zip(b.instrs, tail):
                self.step(st, i, live_after)
            if isinstance(b.term, ir.Return) and b.term.value is not None:
                for ln in self.of(b.term.value):
                    if ln.local.param and not _through(ln) and not (ln.local.ty or "").startswith("&"):
                        self.say("KIFI021", f"the returned reference borrows the parameter {ln.local.name!r}, which is gone when the function returns", b.term.site, ln)
        return len(self.reported)


def check(ctx) -> int:
    """Report the borrow errors of `ctx.fn`; returns how many."""
    return Checker(ctx).run()
