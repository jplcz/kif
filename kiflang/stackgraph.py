# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The call graph and the stack depth of every function (docs/kif-stack.md section 5, stage 3).

`analyze(built, items, plan)` returns a `Depth` per function: its frame plus the deepest callee chain, or *unbounded* when it can reach
a cycle (recursion has no static bound). A call whose frame kif cannot know does not make the answer a lie: the depth is a lower bound
and `incomplete` says which calls are missing, so the author can add `#[stack(N)]` to them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import ast, ir
from . import diagnostics as dg
from . import layout, stack, stackhelpers
from .irtypes import parse_type
from .modsec import _decls
from .target import StackPlan


@dataclass
class Depth:
    name: str  # the qualified name: `f`, `Type::method`
    frame: Optional[int]
    depth: Optional[int]  # frame plus the deepest callee, None when unbounded
    chain: List[str] = field(default_factory=list)  # the deepest path, this function first
    cycle: List[str] = field(default_factory=list)  # for an unbounded function: a cycle it reaches
    incomplete: List[str] = field(default_factory=list)  # calls on any path that have no known frame (the depth is a lower bound)
    callees: List[str] = field(default_factory=list)
    node: object = None
    line: int = 0
    parts: List[Tuple[str, int]] = field(default_factory=list)  # the variables of the frame, biggest first

    @property
    def unbounded(self) -> bool:
        return self.depth is None


HELPER = "<generated helper "  # `<generated helper try_push>`: a function the backend writes (`kif_vec_u32_try_push`); its cost is the backend's (stackhelpers.py)
DROP = "<generated drop glue "  # `<generated drop glue vector>`: `kif_vec_u32_drop`, `kif_str_drop`, ...
LEAF_TRY = {"try_at", "try_slice", "try_value", "try_get", "try_front", "try_back"}  # accessors: inline, no frame worth counting
GLUE_TYPES = {"vector", "secure_vector", "str", "optional"}
PTR = "<generated ptr "  # `<generated ptr S new>` / `<generated ptr S delete>`: the `S_try_new` / `S_delete` of one heap type: their cost depends on S


def hidden_cost(plan: StackPlan, name: str) -> Optional[int]:
    """What the generated function named by a hidden node costs below its caller on this target, None when the backend cannot say."""
    from . import stackhelpers

    if name.startswith(HELPER):
        return stackhelpers.helper_cost(plan, name[len(HELPER):-1])
    return stackhelpers.drop_cost(plan, name[len(DROP):-1])


def _heap_type(t) -> str:
    """S of a `result<ptr<S>>` (what `ptr::try_allocate` returns) or `ptr<S>`, else an empty string."""
    while t.kind == "named" and t.name in ("result",) and t.args:
        t = t.args[0]
    return t.args[0].name if t.kind == "named" and t.name == "ptr" and t.args and t.args[0].kind == "named" else ""


def _receiver(c: ir.Call) -> str:
    """The type a helper call works on: the (dereferenced) receiver, or the `vector` of `vector::try_create`."""
    v = c.recv
    if v is not None and getattr(v, "ty", None):
        t = v.type
        while t.kind == "ref" and t.args:
            t = t.args[0]
        return t.name or ""
    return c.callee.split("::")[0] if "::" in c.callee else ""


def _callees(fn: ir.Function, key: str, table: Dict[str, object], notes: List[str]) -> List[str]:
    out: List[str] = []
    for _, i in fn.instrs():
        target = None
        if isinstance(i, ir.Call):
            if i.kind == "value":
                notes.append("a call through a function value")
            elif i.kind == "unresolved" and i.callee in table:
                target = i.callee  # the IR builder does not resolve `extern` declarations: found by name
            elif i.kind == "unresolved":
                notes.append(f"unresolved call {i.callee}()")
            elif i.decl:
                if i.decl in table:
                    target = i.decl
                else:
                    notes.append(f"{i.decl}() is not declared in this module")
            # an empty `decl` on a free/method/assoc call is a prelude or intrinsic helper: inline code, except the ones the C backend writes as functions
            if not i.decl and i.callee in ("ptr::try_allocate", "ptr::try_create") and i.dst is not None:
                # the call's own type often has no `S` yet (`result<ptr<?>>`): then every `ptr<S>` the function holds may be the one built here
                made = [_heap_type(i.dst.type)] if _heap_type(i.dst.type) else sorted({_heap_type(l.type) for l in fn.locals if _heap_type(l.type)})
                for s in made[1:]:
                    if f"{PTR}{s} new>" not in out:
                        out.append(f"{PTR}{s} new>")
                if made:
                    target = f"{PTR}{made[0]} new>"
                else:
                    notes.append("a ptr allocation of a type kif cannot determine")
            elif not i.decl and i.kind != "value" and i.callee.rsplit("::", 1)[-1].startswith("try_") and i.callee.rsplit("::", 1)[-1] not in LEAF_TRY:
                op = i.callee.rsplit("::", 1)[-1]
                rt = _receiver(i)
                family = "str" if rt == "str" else "vector" if rt in ("vector", "secure_vector") else ""
                target = f"{HELPER}{family + '.' if family else ''}{op}>"
        elif isinstance(i, (ir.Dead, ir.Release)) and i.drop:
            ty = (i.local if isinstance(i, ir.Dead) else i.value).type
            if ty.kind == "named" and f"{ty.name}::drop" in table:
                target = f"{ty.name}::drop"
            elif ty.kind == "named" and ty.name == "ptr" and ty.args and ty.args[0].kind == "named":
                target = f"{PTR}{ty.args[0].name} delete>"
            elif ty.kind == "named" and ty.name in GLUE_TYPES:
                target = f"{DROP}{'vector' if ty.name in ('vector', 'secure_vector') else ty.name}>"
        if target is not None and target not in out:
            out.append(target)
    return out


def _sccs(nodes: List[str], edges: Dict[str, List[str]]) -> List[List[str]]:
    """Tarjan, iterative (a deep call chain must not hit Python's recursion limit)."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on: Set[str] = set()
    stack_: List[str] = []
    out: List[List[str]] = []
    n = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(edges.get(root, [])))]
        index[root] = low[root] = n
        n += 1
        stack_.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack_.append(w)
                    on.add(w)
                    work.append((w, iter(edges.get(w, []))))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack_.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    out.append(comp[::-1])
    return out


def analyze(built: ir.Module, items, plan: StackPlan, tree=None) -> List[Depth]:
    """The depth of every function that has a body, in source order."""
    keys: Dict[int, str] = {}
    table: Dict[str, object] = {}
    for key, node, _emb in _decls(tree if tree is not None else _Tree(items)):
        keys[id(node)] = key
        table[key] = node
    wrapper = {id(it.decl): it.attrs for it in items if isinstance(it, ast.ExternFn)}  # `#[stack(N)]` sits on the `extern "C"` item
    frames = {}
    notes: Dict[str, List[str]] = {}
    edges: Dict[str, List[str]] = {}
    order: List[str] = []
    info: Dict[str, Depth] = {}
    for f, fr in zip(built.functions, stack.frames(built, items, plan)):
        key = keys.get(id(f.node), f.name)
        order.append(key)
        frames[key] = fr
        notes[key] = []
        if fr.size is None and not fr.declared:
            notes[key].append(f"its own frame is unknown ({fr.why})")
        edges[key] = _callees(f, key, table, notes[key])
        info[key] = Depth(key, fr.size, None, node=f.node, line=fr.line, callees=edges[key], parts=fr.parts)
    # a declaration without a body costs what `#[stack(N)]` says, or is an incomplete spot
    own: Dict[str, Optional[int]] = {k: fr.size or 0 for k, fr in frames.items()}
    for key, node in table.items():
        if key not in frames:
            n = stack.declared_stack(node, wrapper.get(id(node), ()))
            own[key] = n or 0
            notes[key] = [] if n is not None else [f"{key}() has no #[stack(N)]"]
            edges[key] = []
    sizes = layout.struct_table(items)
    for name in sorted({c for cs in edges.values() for c in cs if c.startswith(PTR)}):
        s, what = name[len(PTR):-1].rsplit(" ", 1)
        if what == "new":
            ctor = table.get(f"{s}::try_construct_at")
            slots = [layout.size_align(parse_type(ir.type_text(q.ty)), plan, sizes) for q in (getattr(ctor, "params", None) or [])[1:]]
            if None in slots:
                own[name], notes[name] = 0, [f"{s}::try_construct_at has a parameter kif cannot size, so ptr<{s}> creation is a lower bound"]
            else:
                own[name], notes[name] = stackhelpers.heap_new_frame(plan, slots), []
            edges[name] = [f"{s}::try_construct_at"] if ctor is not None else []
        else:
            own[name], notes[name] = stackhelpers.heap_delete_frame(plan), []
            edges[name] = [f"{s}::drop"] if f"{s}::drop" in table else []
    for name in sorted({c for cs in edges.values() for c in cs if c.startswith((HELPER, DROP))}):
        cost = hidden_cost(plan, name)
        own[name], edges[name] = cost or 0, []
        notes[name] = [] if cost is not None else [f"{name[1:-1]}: the backend of this target has no stack estimator (set stack.helper_frame / stack.drop_frame)"]
    comps = _sccs(list(own), edges)
    cyclic: Dict[str, List[str]] = {}
    for c in comps:
        if len(c) > 1 or c[0] in edges.get(c[0], []):
            for v in c:
                cyclic[v] = c
    memo: Dict[str, Tuple[Optional[int], List[str], List[str], List[str]]] = {}

    def solve(v: str):
        # callees are finished before callers in Tarjan's order, so this is a plain bottom-up walk
        if v in memo:
            return memo[v]
        if v in cyclic:
            memo[v] = (None, [v], cyclic[v], [])
            return memo[v]
        best, chain, inc = 0, [], list(notes.get(v, []))
        for w in edges.get(v, []):
            d, ch, cyc, i2 = solve(w)
            inc += [x for x in i2 if x not in inc]
            if d is None:
                memo[v] = (None, [v] + ch, cyc, inc)
                return memo[v]
            if d > best:
                best, chain = d, ch
        memo[v] = (own[v] + best, [v] + chain, [], inc)
        return memo[v]

    for c in comps:  # Tarjan emits a component after everything it reaches
        for v in c:
            solve(v)
    out = []
    for key in order:
        d, ch, cyc, inc = memo[key]
        x = info[key]
        x.depth, x.chain, x.cycle, x.incomplete = d, ch, cyc, inc
        out.append(x)
    return out


class _Tree:
    def __init__(self, items):
        self.items = items


def diagnostics(ds: List[Depth]) -> List[dg.Diagnostic]:
    """KIFW013 (recursion: no static bound) and KIFW014 (the bound is a lower bound: some calls have no known frame)."""
    out: List[dg.Diagnostic] = []
    for d in ds:
        rng = d.node.name.range if d.node is not None else None
        if rng is None:
            continue
        if d.unbounded:
            out.append(dg.warning(rng, "KIFW013", f"{d.name}() has no static stack bound: it reaches the recursion {' -> '.join(d.cycle + d.cycle[:1])}"))
        elif d.incomplete:
            out.append(dg.info(rng, "KIFW014", f"the stack depth of {d.name}() is a lower bound: " + "; ".join(d.incomplete[:4]) + (" ..." if len(d.incomplete) > 4 else "")
                               + " (declare #[stack(N)] on the extern functions, or on the function itself)"))
    return out


def report(ds: List[Depth], plan: StackPlan, file: str) -> str:
    """The depths as text, deepest first; unbounded ones apart (`kif ir --stack-report`)."""
    bounded = sorted((d for d in ds if not d.unbounded), key=lambda d: (-d.depth, d.name))
    out = [f"Stack depth of {file} (frame + deepest callee chain; align {plan.align}, overhead {plan.frame_overhead}):", ""]
    for d in bounded:
        lower = " (lower bound)" if d.incomplete else ""
        out.append(f"  {file}:{d.line}: {d.name}: {d.depth} bytes{lower}; frame {d.frame if d.frame is not None else 'unknown'}; chain " + " -> ".join(d.chain))
    unb = [d for d in ds if d.unbounded]
    if unb:
        out += ["", "Unbounded (recursion):"]
        out += [f"  {file}:{d.line}: {d.name}: reaches {' -> '.join(d.cycle + d.cycle[:1])}" for d in unb]
    inc = [d for d in bounded if d.incomplete]
    if inc:
        out += ["", "Incomplete (calls with no known frame):"]
        out += [f"  {file}:{d.line}: {d.name}: " + "; ".join(d.incomplete) for d in inc]
    return "\n".join(out) + "\n"
