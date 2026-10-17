# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Checker support for thread-local and per-CPU cells (`#[tls]` / `#[per_cpu]` statics, design in docs section 4.17).

`bodies.py` calls in here for three things: `check_static` for every `static` item of the module, `call` for
`CELL.with(..)`-style accessors, and `finish` once all bodies are checked (the call-graph rules). The interpreter has no
cells at all: a `gen fn` that names one is KIFP190.

* KIFP180 cell type is not zeroable;           * KIFP186 per-CPU init allocates (directly or through calls);
* KIFP181 bad initialiser (not constant, or a bad `slot::new(init)`);   * KIFP187 `slot<T>` outside a cell;
* KIFP182 per-CPU cell whose type has `drop`;  * KIFP188 a `static` needs exactly one of `#[tls]` / `#[per_cpu]`;
* KIFP183 TLS access from a TLS `drop`;       * KIFP189 TLS access in an `#[atomic_entry]` body;
* KIFP184 per-CPU `with_mut` of a cell an `#[atomic_entry]` function also reaches;   * KIFP190 cell used by a `gen fn`;
* KIFP185 an accessor closure returns a borrow; * KIFP191 a cell used as a value.
KIFP111 (per-CPU access outside atomic context) and KIFP110 (TLS `try_with` may sleep) are the existing atomic codes.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Dict, List, Optional

from . import ast
from . import diagnostics as dg
from . import types as T
from .types import Ty, UNKNOWN

_SCALARS = frozenset(T.INT_RANGES) | {"bool", "char", "f32", "f64"}
_RAW = {"tls": {"with", "with_mut"}, "per_cpu": {"with", "with_mut"}}
_SLOT = {"tls": {"try_with", "try_with_mut", "with_ready", "with_ready_mut"}, "per_cpu": {"with", "with_mut"}}


class Cell:
    """What the checker knows about one `static` cell."""

    def __init__(self, decl: ast.StaticDecl, mod, kind: str, atomic_ok: bool) -> None:
        self.decl, self.mod, self.kind, self.atomic_ok = decl, mod, kind, atomic_ok
        self.slot = False
        self.value_ty = None  # the written `T` (of `slot<T>`, or the cell type itself)
        self.init = None  # the init function of a slot (FnDecl)
        self.sleepy = SimpleNamespace(name=SimpleNamespace(text=decl.name.text + ".try_with"), attrs=[])


def _attr(attrs, name: str):
    return next((a for a in attrs if a.name.text == name), None)


def kind_of(decl: ast.StaticDecl) -> Optional[str]:
    tls, cpu = _attr(decl.attrs, "tls"), _attr(decl.attrs, "per_cpu")
    if (tls is None) == (cpu is None):
        return None
    return "tls" if tls is not None else "per_cpu"


def info(bc, sym) -> Cell:
    """The (cached) `Cell` of a `static` symbol, resolved in the module that declares it."""
    cache = bc.__dict__.setdefault("cells", {})
    c = cache.get(id(sym.node))
    if c is None:
        decl = sym.node
        kind = kind_of(decl) or "tls"
        a = _attr(decl.attrs, kind)
        c = Cell(decl, sym.module, kind, bool(a and a.args and any(x.name == "atomic_ok" for x in a.args)))
        sl = _slot_arg(bc, decl.ty, sym.module)
        if sl is not None:
            c.slot, c.value_ty = True, sl
            c.init = _init_decl(bc, decl, sym.module)
        else:
            c.value_ty = decl.ty
        cache[id(sym.node)] = c
    return c


def _is_slot(bc, t, mod) -> bool:
    if not isinstance(t, ast.PathType) or t.segments[-1].name != "slot":
        return False
    sym = bc.sym_type(t.text, mod)
    return sym is not None and any(a.name.text == "lang" and a.args and a.args[0].name == "slot"
                                   for a in getattr(sym.node, "attrs", ()))


def _slot_arg(bc, t, mod):
    if not _is_slot(bc, t, mod):
        return None
    args = [a for a in t.segments[-1].args if not isinstance(a, (ast.Lifetime, ast.ConstArg))]
    return args[0] if args else None


def _init_decl(bc, decl, mod):
    v = decl.value
    if isinstance(v, ast.Call) and isinstance(v.callee, ast.PathExpr) and v.callee.text == "slot::new" and len(v.args) == 1 \
            and isinstance(v.args[0], ast.PathExpr):
        sym = bc.sym_value([s.name for s in v.args[0].segments])
        if sym is not None and sym.kind == "fn":
            return getattr(sym.node, "decl", sym.node)
    return None


# -- the static item

def check_static(bc, it: ast.StaticDecl) -> None:
    d = bc.d
    kind = kind_of(it)
    if kind is None:
        d.append(dg.error(it.name.range, "KIFP188",
                          f"{it.name.text!r}: a static must have exactly one of #[tls] or #[per_cpu] (there are no other statics)"))
        return
    sym = bc.mod.scope_values.get(it.name.text)
    if sym is None or sym.node is not it:  # a duplicate (KIF010) was already reported
        return
    cell = info(bc, sym)
    bc.cell_list.append(cell)
    if cell.slot:
        _check_slot(bc, cell)
    else:
        _check_raw(bc, cell)
    why = _drop_of(bc, cell.value_ty, bc.mod, set())
    if kind == "per_cpu" and why:
        d.append(dg.error(it.name.range, "KIFP182",
                          f"per-CPU cell {it.name.text!r}: {why} has a 'drop', which never runs for per-CPU data"))


def _misplaced_slots(bc) -> None:
    """KIFP187: `slot<T>` is only valid as the whole type of a cell, anywhere in the module."""
    allowed = {id(c.decl.ty) for c in bc.cell_list if c.slot}

    def walk(n) -> None:
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, ast.Node):
            if isinstance(n, ast.PathType) and _is_slot(bc, n, bc.mod) and id(n) not in allowed:
                bc.d.append(dg.error(n.range, "KIFP187", "slot<T> is only valid as the whole type of a #[tls] or #[per_cpu] static"))
            for f in dataclasses.fields(n):
                walk(getattr(n, f.name))

    walk([i for i in bc.mod.tree.items if not isinstance(i, (ast.ExternType, ast.ExternImpl))])


def _check_raw(bc, cell: Cell) -> None:
    it = cell.decl
    why = _zeroable(bc, it.ty, bc.mod, set())
    if why:
        bc.d.append(dg.error(it.ty.range, "KIFP180", f"the type of {it.name.text!r} is not zeroable: {why} "
                                                    "(use slot<T> for a complex object)"))
    bad = _const(bc, it.value)
    if bad is not None:
        bc.d.append(dg.error(bad.range, "KIFP181", "a cell initialiser must be a constant expression "
                                                   "(literals, constants, operators, enumerators, tuples, arrays, struct literals)"))
        return
    if not why:
        bc.coerce(it.value, bc.expr(it.value), bc.ty(it.ty), "the cell initialiser")


def _check_slot(bc, cell: Cell) -> None:
    it = cell.decl
    if cell.init is None:
        bc.d.append(dg.error(it.value.range, "KIFP181", "a slot is initialised with slot::new(init), where init is a function"))
        return
    f = cell.init
    if f.params or f.generics:
        bc.d.append(dg.error(f.name.range, "KIFP181", f"the init function {f.name.text!r} of a slot takes no parameters"))
        return
    want = bc.ty(cell.value_ty)
    got = bc.ty(f.ret) if f.ret is not None else T.UNIT
    if cell.kind == "tls":
        ok = got.name == "result" and (not got.args or want == UNKNOWN or got.args[0] == UNKNOWN or got.args[0] == want)
        shape = f"result<{want}>"
    else:
        ok = got.name != "result" and (got == UNKNOWN or want == UNKNOWN or got == want)
        shape = str(want)
    if not ok:
        bc.d.append(dg.error(f.name.range, "KIFP181",
                             f"the init function of a {cell.kind} slot must return {shape}, not {got}"
                             + (" (per-CPU init cannot fail)" if cell.kind == "per_cpu" else "")))
    attrs = bc.attrs_for(f)
    if cell.kind == "per_cpu" and "might_sleep" in attrs:
        bc.d.append(dg.error(f.name.range, "KIFP110", f"{f.name.text!r} is #[might_sleep] but per-CPU init runs in an atomic context"))
    if "atomic_exempt" not in attrs:
        bc.cell_inits.append(cell)


# -- types

def _zeroable(bc, t, mod, seen: set) -> str:
    """Why an all-zero byte pattern is not a valid value of `t` ('' when it is)."""
    if isinstance(t, ast.RefType):
        return "a reference is never null"
    if isinstance(t, ast.FnType):
        return "a function pointer is never null"
    if isinstance(t, ast.SliceType):
        return "a slice is not a fixed-size type"
    if isinstance(t, ast.ArrayType):
        return _zeroable(bc, t.elem, mod, seen)
    if isinstance(t, ast.TupleType):
        return next((r for r in (_zeroable(bc, x, mod, seen) for x in t.elems) if r), "")
    if not isinstance(t, ast.PathType):
        return "unsupported type"
    name = t.text
    if name in _SCALARS:
        return ""
    if _is_slot(bc, t, mod):
        return ""
    sym = bc.sym_type(name, mod)
    if sym is None:
        return f"unknown type {name!r}"
    attrs = getattr(sym.node, "attrs", ())
    if _attr(attrs, "zeroable") is not None or any(a.name.text == "lang" and a.args and a.args[0].name in ("c_ptr", "c_cptr")
                                                    for a in attrs):
        return ""
    if sym.kind == "enum":
        e: ast.EnumDecl = sym.node
        nxt = 0
        for m in e.members:
            val = nxt
            if m.value is not None:
                v = bc.int_literal(m.value)
                if v is None:
                    return f"enum {name!r} has a value that is not a literal, so a zero member cannot be verified"
                val = v
            if val == 0:
                return ""
            nxt = val + 1
        return f"enum {name!r} has no member with value 0"
    if sym.kind == "struct":
        if sym.generics:
            return f"{name!r} is generic"
        if id(sym.node) in seen:
            return ""
        seen.add(id(sym.node))
        for f in sym.node.fields:
            r = _zeroable(bc, f.ty, sym.module, seen)
            if r:
                return f"field {f.name.text!r} of {name!r}: {r}"
        return ""
    if sym.kind == "alias" and not sym.generics:
        return _zeroable(bc, sym.node.ty, sym.module, seen)
    return f"{name!r} is not known to be valid when zero (mark an extern type #[zeroable] if it is)"


def _drop_entries(bc, t, mod, seen: set) -> list:
    """(type name, drop entry) for every type with a `drop` that `t` is or contains: a value's own `drop` and, afterwards,
    the `drop` of each field, element and generic argument all run when it is destroyed."""
    if isinstance(t, (ast.SliceType, ast.ArrayType)):
        return _drop_entries(bc, t.elem, mod, seen)
    if isinstance(t, ast.TupleType):
        return [e for x in t.elems for e in _drop_entries(bc, x, mod, seen)]
    if not isinstance(t, ast.PathType):
        return []
    sym = bc.sym_type(t.text, mod)
    if sym is None:
        return []
    out: list = []
    ty = bc.ty(t, mod)
    entry = bc.index.get(ty.key, {}).get("drop") if ty.key else None
    if entry is not None:
        out.append((t.text, entry))
    if sym.kind == "struct" and id(sym.node) not in seen:
        seen.add(id(sym.node))
        for f in sym.node.fields:
            out.extend(_drop_entries(bc, f.ty, sym.module, seen))
    for seg in t.segments:
        for a in seg.args:
            if not isinstance(a, (ast.Lifetime, ast.ConstArg)):
                out.extend(_drop_entries(bc, a, mod, seen))
    return out


def _drop_of(bc, t, mod, seen: set) -> str:
    """The name of a type with a `drop` that `t` is or contains ('' when there is none)."""
    found = _drop_entries(bc, t, mod, seen)
    return found[0][0] if found else ""


def _const(bc, e):
    """The first sub-expression of `e` that is not a constant expression (None when `e` is one)."""
    if isinstance(e, ast.Lit):
        return None
    if isinstance(e, ast.Unary):
        return None if e.op in ("-", "!", "~") and _const(bc, e.operand) is None else e
    if isinstance(e, ast.Binary):
        return _const(bc, e.left) or _const(bc, e.right)
    if isinstance(e, ast.Cast):
        return _const(bc, e.expr)
    if isinstance(e, ast.TupleExpr):
        return next((b for b in (_const(bc, x) for x in e.elems) if b is not None), None)
    if isinstance(e, ast.ArrayLit):
        parts = list(e.elems) + ([e.repeat] if e.repeat is not None else [])
        return next((b for b in (_const(bc, x) for x in parts) if b is not None), None)
    if isinstance(e, ast.StructLit):
        if e.base is not None:
            return e.base
        return next((b for b in (_const(bc, f.value) if f.value is not None else e for f in e.fields) if b is not None), None)
    if isinstance(e, ast.PathExpr):
        names = [s.name for s in e.segments]
        sym = bc.sym_value(names)
        if sym is not None and sym.kind == "const":
            return None
        tsym = bc.sym_type("::".join(names[:-1])) if len(names) > 1 else None
        return None if tsym is not None and tsym.kind == "enum" else e
    return e


# -- accessors

def call(bc, e: ast.MethodCall, sym) -> Ty:
    """`CELL.method(closure)`: typed here because a cell is not a value."""
    cell = info(bc, sym)
    name = e.name.text
    methods = _SLOT[cell.kind] if cell.slot else _RAW[cell.kind]
    cur = bc.cur
    if cur is not None and getattr(cur, "is_gen", False):
        bc.d.append(dg.error(e.range, "KIFP190", f"{cell.decl.name.text!r} is a {cell.kind} cell, which does not exist at generation time"))
        return UNKNOWN
    if name not in methods:
        bc.d.append(dg.error(e.name.range, "KIFP010",
                             f"{cell.kind} cell {cell.decl.name.text!r} has no method {name!r} (it has: {', '.join(sorted(methods))})"))
        return UNKNOWN
    mut = name.endswith("_mut")
    vt = bc.ty(cell.value_ty, cell.mod)
    ret = UNKNOWN
    if len(e.args) != 1 or not isinstance(e.args[0], ast.Lambda) or len(e.args[0].params) != 1:
        bc.d.append(dg.error(e.name.range, "KIFP011", f"{name!r} takes one closure with one parameter: |v| .."))
    else:
        lam = e.args[0]
        bc.lambda_hint = [T.ref(vt, mut)]
        try:
            ft = bc.expr(lam)
        finally:
            bc.lambda_hint = None
        ret = ft.args[-1] if ft.name == "fn" else UNKNOWN
        if _borrows(ret):
            bc.d.append(dg.error(lam.range, "KIFP185", f"the closure result {ret} borrows from the cell: "
                                                       "a reference to a cell must not outlive the access"))
            ret = UNKNOWN
    if bc.dead:
        return UNKNOWN
    rng = e.name.range
    if cell.kind == "per_cpu" and not bc.in_atomic():
        bc.d.append(dg.error(rng, "KIFP111", f"{cell.decl.name.text!r} is per-CPU data: {name!r} needs an atomic context"))
    if cell.kind == "tls" and cur is not None and "atomic_entry" in bc.attrs_for(cur) and not cell.atomic_ok:
        bc.d.append(dg.error(rng, "KIFP189", f"{cell.decl.name.text!r} is thread-local but {cur.name.text!r} is an #[atomic_entry]: "
                                             "the interrupted thread is not this code's thread (declare #[tls(atomic_ok)] to allow it)"))
    if name.startswith("try_with"):
        bc.sleepy.add(id(cell.sleepy))
        if cur is not None and not bc.exempt:
            bc.calls.append((cur, cell.sleepy, rng, bc.in_atomic()))
    if cur is not None:
        bc.cell_uses.append((cell, cur, mut, rng))
    return _result(bc, name, ret)


def _borrows(t: Ty) -> bool:
    return t.name == "&" or any(_borrows(a) for a in t.args)


def _result(bc, name: str, ret: Ty) -> Ty:
    if name.startswith("try_with"):
        sym = bc.sym_type("result")
        return Ty("result", (ret,), key=f"{sym.module.key}:result", sym=sym) if sym is not None else Ty("result", (ret,))
    if name.startswith("with_ready"):
        sym = bc.sym_type("optional")
        return Ty("optional", (ret,), key=f"{sym.module.key}:optional", sym=sym) if sym is not None else UNKNOWN
    return ret


# -- call-graph rules

def _reach(bc, start_ids) -> Dict[int, object]:
    """Every function (by id) reachable from `start_ids` through the recorded call edges, with its decl."""
    out: Dict[int, object] = {}
    todo = list(start_ids)
    while todo:
        n = todo.pop()
        if n in out:
            continue
        ent = bc.edges.get(n)
        if ent is None:
            continue
        out[n] = ent[0]
        todo.extend(id(c) for c, _ in ent[1])
    return out


def finish(bc) -> None:
    d = bc.d
    _misplaced_slots(bc)
    # KIFP186: per-CPU init must not allocate
    for cell in bc.cell_inits:
        if cell.kind != "per_cpu" or id(cell.init) not in bc.edges:
            continue
        for n, decl in _reach(bc, [id(cell.init)]).items():
            hit = next((c for c, _ in bc.edges[n][1] if "allocates" in bc.attrs_for(c)), None)
            if hit is not None:
                d.append(dg.error(cell.decl.name.range, "KIFP186",
                                  f"per-CPU init {cell.init.name.text!r} allocates ({decl.name.text!r} calls {hit.name.text!r}): "
                                  "per-CPU data has no allocator"))
                break
    by_fn: Dict[int, list] = {}
    for cell, fn, mut, rng in bc.cell_uses:
        by_fn.setdefault(id(fn), []).append((cell, mut, rng))
    # KIFP184: `with_mut` of a per-CPU cell that an `#[atomic_entry]` function can also reach
    entries = [k for k, (decl, _) in bc.edges.items() if "atomic_entry" in bc.attrs_for(decl) and not getattr(decl, "is_gen", False)]
    reached = {k: _reach(bc, [k]) for k in entries}
    for cell, fn, mut, rng in bc.cell_uses:
        if not (mut and cell.kind == "per_cpu"):
            continue
        for k, fns in reached.items():
            if any(c is cell for n in fns for c, _, _ in by_fn.get(n, ())):
                d.append(dg.error(rng, "KIFP184", f"with_mut of {cell.decl.name.text!r}: the #[atomic_entry] function "
                                                  f"{bc.edges[k][0].name.text!r} can also reach it and could interrupt the closure"))
                break
    # KIFP183: a TLS `drop` (the type's own, or the `drop` of anything it contains) must not reach any TLS cell
    tls_by_fn = {}
    for c, f, _, _ in bc.cell_uses:
        if c.kind == "tls":
            tls_by_fn.setdefault(id(f), c)
    for cell in bc.cell_list:
        if cell.kind != "tls":
            continue
        done = False
        for tname, entry in _drop_entries(bc, cell.value_ty, cell.mod, set()):
            if done or id(entry.decl) not in bc.edges:
                continue
            for n, decl in _reach(bc, [id(entry.decl)]).items():
                use = tls_by_fn.get(n)
                if use is not None:
                    d.append(dg.error(entry.decl.name.range, "KIFP183",
                                      f"'drop' of {tname} (part of TLS cell {cell.decl.name.text!r}) reaches TLS cell "
                                      f"{use.decl.name.text!r}, which may already be destroyed"))
                    done = True
                    break
