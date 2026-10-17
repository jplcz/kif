# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Structured control flow from the IR's block graph (docs/kif-ir-codegen.md, "Structured output").

The IR is a graph of blocks joined by `Jump` / `Branch`. A backend that writes one `goto` per edge produces code nobody can read, so
this module rebuilds what the source said: `if` / `else`, loops, `break` and `continue`. It needs only the graph, no source tree.

The result is a tree of items (`Blk`, `If`, `Loop`, `Break`, `Continue`, `Goto`) that a backend walks:

* `Blk(b)`   the instructions of block `b` (its terminator is a `Return` / `Unreachable` the backend writes, or is expressed by what follows);
* `If(b, then, other)`   a conditional on the `Branch` that ends `b`;
* `Loop(body)`   an endless loop whose body leaves by `Break` (a natural loop of the graph: a header that dominates its back edges);
* `Break` / `Continue`   leave / restart the innermost loop;
* `Goto(b)`   the fallback for an edge nothing above can express; the target's label is written because `b` is in `Structured.gotos`.

How: dominators find the loops (an edge to a block that dominates its source is a back edge). Post-dominators find where a branch
rejoins: the immediate post-dominator of the branching block is the join, written after the `if`. Every other edge is either the
fall-through to the enclosing construct's next block, a `break` / `continue`, a block that has not been written yet (written right
there), or one already written (a `goto`). A graph with a loop that has two entries (irreducible) is not structured: `build` returns None
and the backend falls back to `flat`, one label and `goto` per block."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import ir


@dataclass(eq=False)
class Blk:
    block: ir.Block


@dataclass(eq=False)
class If:
    block: ir.Block  # the block whose `Branch` is the condition
    then: List[object]
    other: List[object]


@dataclass(eq=False)
class Loop:
    body: List[object]


@dataclass(eq=False)
class Break:
    pass


@dataclass(eq=False)
class Continue:
    pass


@dataclass(eq=False)
class Goto:
    target: ir.Block


@dataclass(eq=False)
class Structured:
    items: List[object]
    gotos: Set[ir.Block] = field(default_factory=set)  # blocks that need their label written


_EXIT = object()  # the virtual block every `Return` / `Unreachable` leads to, for post-dominators


def _idoms(root, succs: Dict, preds: Dict) -> Dict:
    """Immediate dominators of every node reachable from `root` (Cooper, Harvey, Kennedy)."""
    order: List = []
    seen = {root}
    stack = [(root, iter(succs.get(root, [])))]
    while stack:
        n, it = stack[-1]
        for s in it:
            if s not in seen:
                seen.add(s)
                stack.append((s, iter(succs.get(s, []))))
                break
        else:
            order.append(n)
            stack.pop()
    order.reverse()
    num = {n: i for i, n in enumerate(order)}
    idom: Dict = {root: root}
    changed = True
    while changed:
        changed = False
        for n in order[1:]:
            new = None
            for p in preds.get(n, []):
                if p not in idom:
                    continue
                if new is None:
                    new = p
                    continue
                a, b = p, new
                while a is not b:
                    while num[a] > num[b]:
                        a = idom[a]
                    while num[b] > num[a]:
                        b = idom[b]
                new = a
            if new is not None and idom.get(n) is not new:
                idom[n] = new
                changed = True
    return idom


def _dominates(idom: Dict, a, b) -> bool:
    while True:
        if a is b:
            return True
        up = idom.get(b)
        if up is None or up is b:
            return False
        b = up


class _Loop:
    def __init__(self, header: ir.Block, body: Set[ir.Block], exit: Optional[ir.Block]) -> None:
        self.header, self.body, self.exit = header, body, exit


def flat(fn: ir.Function) -> Structured:
    """One label and `goto` per block: the layout for a graph that `build` cannot structure."""
    items: List[object] = []
    gotos: Set[ir.Block] = set()
    for k, b in enumerate(fn.blocks):
        nxt = fn.blocks[k + 1] if k + 1 < len(fn.blocks) else None
        items.append(Blk(b))
        t = b.term
        if isinstance(t, ir.Jump):
            if t.target is not nxt:
                gotos.add(t.target)
                items.append(Goto(t.target))
        elif isinstance(t, ir.Branch):
            gotos.add(t.then)
            then: List[object] = [Goto(t.then)]
            other: List[object] = []
            if t.otherwise is not nxt:
                gotos.add(t.otherwise)
                other = [Goto(t.otherwise)]
            items.append(If(b, then, other))
    return Structured(items, gotos)


def build(fn: ir.Function) -> Optional[Structured]:
    """The structured form of `fn`, or None when its graph is irreducible."""
    entry = fn.blocks[0]
    succs: Dict = {}
    preds: Dict = {}
    reach: Set[ir.Block] = set()
    work = [entry]
    while work:
        b = work.pop()
        if b in reach:
            continue
        reach.add(b)
        for s in (b.term.targets() if b.term is not None else []):
            succs.setdefault(b, []).append(s)
            preds.setdefault(s, []).append(b)
            work.append(s)
    idom = _idoms(entry, succs, preds)
    rpo = {b: i for i, b in enumerate(sorted(reach, key=lambda b: _depth(idom, b)))}  # only used to pick between exits deterministically

    loops: Dict[ir.Block, Set[ir.Block]] = {}
    for u in reach:
        for v in succs.get(u, []):
            if v is u or _dominates(idom, v, u):
                body = loops.setdefault(v, {v})
                stack = [u]
                while stack:
                    n = stack.pop()
                    if n not in body:
                        body.add(n)
                        stack.extend(preds.get(n, []))
    # irreducible: a cycle is left once the back edges are removed, i.e. some loop has a second entry
    colour: Dict[ir.Block, int] = {}

    def cyclic(n: ir.Block) -> bool:
        colour[n] = 1
        for v in succs.get(n, []):
            if v is n or _dominates(idom, v, n):
                continue
            if colour.get(v) == 1 or (v not in colour and cyclic(v)):
                return True
        colour[n] = 2
        return False

    if cyclic(entry):
        return None

    rsucc: Dict = {}
    rpred: Dict = {}
    for b in reach:
        ss = succs.get(b, [])
        if not ss:
            rsucc.setdefault(_EXIT, []).append(b)  # reversed graph: EXIT -> b
            rpred.setdefault(b, []).append(_EXIT)
        for s in ss:
            rsucc.setdefault(s, []).append(b)
            rpred.setdefault(b, []).append(s)
    pdom = _idoms(_EXIT, rsucc, rpred)

    def join(n: ir.Block) -> Optional[ir.Block]:
        j = pdom.get(n)
        return None if j is None or j is _EXIT or j is n else j

    def exit_of(h: ir.Block, body: Set[ir.Block]) -> Optional[ir.Block]:
        outs = [s for b in body for s in succs.get(b, []) if s not in body]
        if not outs:
            return None
        t = h.term
        for s in (t.targets() if t is not None else []):
            if s not in body:
                return s
        return min(outs, key=lambda s: rpo.get(s, 0))

    visited: Set[ir.Block] = set()
    gotos: Set[ir.Block] = set()

    def edge(x: ir.Block, stop: Optional[ir.Block], loop: Optional[_Loop]) -> List[object]:
        if x is stop:
            return []
        if loop is not None and x is loop.header:
            return [Continue()]
        if loop is not None and x is loop.exit:
            return [Break()]
        if x in visited:
            gotos.add(x)
            return [Goto(x)]
        return enter(x, stop, loop) if x in loops else plain(x, stop, loop)

    def enter(h: ir.Block, stop: Optional[ir.Block], outer: Optional[_Loop]) -> List[object]:
        info = _Loop(h, loops[h], exit_of(h, loops[h]))
        out: List[object] = [Loop(plain(h, h, info))]
        if info.exit is not None:
            out += edge(info.exit, stop, outer)
        return out

    def plain(b: ir.Block, stop: Optional[ir.Block], loop: Optional[_Loop]) -> List[object]:
        visited.add(b)
        out: List[object] = [Blk(b)]
        t = b.term
        if isinstance(t, ir.Jump):
            out += edge(t.target, stop, loop)
        elif isinstance(t, ir.Branch):
            j = join(b)
            if j is not None and loop is not None and j not in loop.body:
                j = None
            inner = j if j is not None else stop
            then = edge(t.then, inner, loop)
            other = edge(t.otherwise, inner, loop)
            out.append(If(b, then, other))
            if j is not None:
                out += edge(j, stop, loop)
        return out

    return Structured(edge(entry, None, None), gotos)


def _depth(idom: Dict, b: ir.Block) -> int:
    n = 0
    while idom.get(b) is not None and idom[b] is not b:
        b = idom[b]
        n += 1
    return n
