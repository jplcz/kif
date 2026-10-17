# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Checker step 4: methods and `impl` blocks.

`build_index` collects, for every named type, the functions of its `impl` blocks (`impl S { .. }`,
`impl iface for S { .. }`, `extern "C++" impl T { .. }`) and the provided methods of the interfaces it implements, so the
body checker can resolve `x.method(..)` and `Type::function(..)`.

`check_impls` reports the declaration errors of section 4.8:

* KIFP026 `impl <interface> for S`: a required method is missing, a method is not in the interface, or its signature
  differs;
* KIFP027 a method or associated function declared twice in the `impl` blocks of one type;
* KIFP028 `impl <interface> for` a `#[copy]` struct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import ast, cfg
from . import diagnostics as dg
from . import types as T
from .types import UNKNOWN, Ty


@dataclass
class MethodEntry:
    decl: object  # ast.FnDecl or ast.Method
    module: object
    receiver: str  # "" (associated function) | value | ref | mut | implicit (interface method written without `self`)
    generics: List[ast.GenericParam]
    iface: Optional[str] = None  # the interface it came from, if any
    target: Ty = UNKNOWN  # the type the impl is for (`Self`)


def receiver_of(decl) -> str:
    for p in decl.params:
        if p.receiver:
            return p.receiver
    return ""


def has_attr(attrs, name: str) -> bool:
    return any(a.name.text == name for a in attrs)


def modules_of(mod) -> list:
    seen, out = set(), []
    for m in list(mod.modules.values()) + ([mod.prelude] if mod.prelude is not None else []):
        if id(m) not in seen:
            seen.add(id(m))
            out.append(m)
    return out


def _is_default(m: ast.Method) -> bool:
    return m.body is not None or m.has_default or m.default_sym is not None or has_attr(m.attrs, "provided")


def base_of(chk, isym):
    """The interface named by `#[extends(base)]` on `isym`, or None."""
    for a in isym.node.attrs:
        if a.name.text == "extends" and a.args and a.args[0].name:
            b = chk.sym_type(a.args[0].name, isym.module)
            if b is not None and isinstance(b.node, ast.Interface):
                return b
    return None


def chain(chk, isym) -> list:
    """`isym` followed by its bases, nearest first (stops at a cycle)."""
    out, seen = [], set()
    while isym is not None and id(isym.node) not in seen:
        seen.add(id(isym.node))
        out.append(isym)
        isym = base_of(chk, isym)
    return out


def inherited(chk, isym) -> Dict[str, tuple]:
    """Every method an interface has, own and inherited: name -> (Method, module, interface name); derived wins."""
    out: Dict[str, tuple] = {}
    for s in chain(chk, isym):
        for m in s.node.methods:
            out.setdefault(m.name.text, (m, s.module, s.node.name.text))
    return out


def build_index(chk) -> Dict[str, Dict[str, MethodEntry]]:
    index: Dict[str, Dict[str, MethodEntry]] = {}

    def add(key: str, name: str, entry: MethodEntry) -> None:
        index.setdefault(key, {}).setdefault(name, entry)

    for m in modules_of(chk.mod):
        for it in m.tree.items:
            if isinstance(it, ast.ExternImpl):
                chk.generics.append({g.name.text for g in it.generics if not g.is_lifetime})
                try:
                    target = chk.ty(it.target, m)
                finally:
                    chk.generics.pop()
                key = target.key
                for f in it.fns:
                    if key:
                        add(key, f.name.text, MethodEntry(f, m, receiver_of(f), it.generics, None, target))
            elif isinstance(it, ast.ImplDecl):
                chk.generics.append({g.name.text for g in it.generics if not g.is_lifetime})
                try:
                    target = chk.ty(it.target, m)
                finally:
                    chk.generics.pop()
                key = target.key
                if not key:
                    continue
                for f in it.items:
                    if isinstance(f, ast.FnDecl):
                        add(key, f.name.text, MethodEntry(f, m, receiver_of(f), it.generics,
                                                           it.iface.text if it.iface else None, target))
                if it.iface is not None:
                    isym = chk.sym_type(it.iface.text, m)
                    if isym is not None and isinstance(isym.node, ast.Interface):
                        for im, imod, iname in inherited(chk, isym).values():
                            if not im.static:
                                add(key, im.name.text, MethodEntry(im, imod, receiver_of(im) or "implicit", [], iname, target))
    return index


KOBJ_ONLY_ATTRS = ("self", "softc", "errno", "version")


def check_hooks(it: ast.Interface, d: list) -> None:
    """`m_prolog` / `m_epilog` apply to the NEXT method only (spec 7.2), so each needs a method after it.

    KIF088: one with no method after it; KIF089: the same one twice before a method (the first would be ignored).
    KIFP140: the attributes `self`, `softc`, `errno`, `version` and the raw C hooks need a `#[kobj]` interface.
    """
    kobj = has_attr(it.attrs, "kobj")
    pending: Dict[str, ast.Verbatim] = {}
    for m in it.members:
        if isinstance(m, ast.Method):
            pending.clear()
        elif isinstance(m, ast.Verbatim) and m.kind in ("m_prolog", "m_epilog"):
            if not kobj:
                d.append(dg.error(m.keyword_range, "KIFP140", f"{m.kind} needs a #[kobj] interface (it is C/.m output)"))
            if m.kind in pending:
                d.append(dg.error(m.keyword_range, "KIF089", f"{m.kind} appears twice before one method"))
            pending[m.kind] = m
    for m in pending.values():
        d.append(dg.error(m.keyword_range, "KIF088", f"{m.kind} is not followed by a method (it applies to the next method only)"))
    if not kobj:
        for a in it.attrs:
            if a.name.text in KOBJ_ONLY_ATTRS:
                d.append(dg.error(a.range, "KIFP140", f"#[{a.name.text}] needs a #[kobj] interface"))


def check_interfaces(chk) -> None:
    """`#[extends(base)]`: the base must exist (KIFP010), not loop (KIFP142) and be of the same kind (KIFP141)."""
    d = chk.mod.diagnostics
    for it in chk.mod.tree.items:
        if not isinstance(it, ast.Interface):
            continue
        check_hooks(it, d)
        for a in it.attrs:
            if a.name.text != "extends":
                continue
            if not (a.args and a.args[0].name):
                d.append(dg.error(a.range, "KIFP010", "extends needs the name of an interface"))
                continue
            name = a.args[0].name
            me = chk.sym_type(it.name.text)
            base = chk.sym_type(name)
            if base is None or not isinstance(base.node, ast.Interface):
                d.append(dg.error(a.range, "KIFP010", f"unknown interface {name!r}"))
            elif me is not None and any(s.node is it for s in chain(chk, base)):
                d.append(dg.error(a.range, "KIFP142", f"{it.name.text!r} extends itself (through {name!r})"))
            elif has_attr(base.node.attrs, "kobj") != has_attr(it.attrs, "kobj"):
                d.append(dg.error(a.range, "KIFP141", f"{it.name.text!r} and {name!r} differ in #[kobj]: extends works within one kind"))


def check_impls(chk) -> None:
    mod, d = chk.mod, chk.mod.diagnostics
    inherent: Dict[str, Dict[str, tuple]] = {}
    for it in mod.tree.items:
        if not isinstance(it, ast.ImplDecl):
            continue
        chk.generics.append({g.name.text for g in it.generics if not g.is_lifetime})
        try:
            target = chk.ty(it.target)
        finally:
            chk.generics.pop()
        if it.iface is None:
            seen = inherent.setdefault(target.key or str(id(it)), {})
            for f in it.items:
                if isinstance(f, ast.FnDecl):
                    old = seen.get(f.name.text)
                    pred = cfg.of_attrs(f.attrs)
                    variant = old is not None and pred is not None and all(
                        p is not None and cfg.exclusive(pred, p) for p in old[2])
                    if old is not None and old[0] is not it and not variant:
                        d.append(dg.error(f.name.range, "KIFP027", f"{f.name.text!r} is already declared in another impl of {target}"))
                    seen[f.name.text] = (it, f, (old[2] if old else []) + [pred])
            continue
        isym = chk.sym_type(it.iface.text)
        if isym is None or not isinstance(isym.node, ast.Interface):
            continue  # unknown interface: reported by symbols.py
        tsym = chk.struct_of(target)
        if tsym is not None and has_attr(tsym.node.attrs, "copy"):
            d.append(dg.error(it.range, "KIFP028", f"{target} is #[copy]: an implementer is move-only, its identity is its address"))
        inh = inherited(chk, isym)
        required = {n: v[0] for n, v in inh.items()}
        given = {f.name.text: f for f in it.items if isinstance(f, ast.FnDecl)}
        for name, f in given.items():
            im = required.get(name)
            if im is None:
                d.append(dg.error(f.name.range, "KIFP026", f"{it.iface.text!r} has no method {name!r}"))
            else:
                _signature(chk, it, f, im, inh[name][1])
        for name, im in required.items():
            if name not in given and not _is_default(im) and not im.static:
                d.append(dg.error(it.range, "KIFP026", f"missing method {name!r} of {it.iface.text!r}"))


def _signature(chk, impl: ast.ImplDecl, f: ast.FnDecl, im: ast.Method, imod) -> None:
    d = chk.mod.diagnostics
    fp = [p for p in f.params if not p.receiver]
    ip = [p for p in im.params if not p.receiver]
    if len(fp) != len(ip):
        d.append(dg.error(f.name.range, "KIFP026", f"{f.name.text!r} takes {len(fp)} parameter(s), the interface has {len(ip)}"))
        return
    chk.generics.append({g.name.text for g in impl.generics if not g.is_lifetime})
    try:
        for a, b in zip(fp, ip):
            ta, tb = chk.ty(a.ty), chk.ty(b.ty, imod)
            if not (T.compat(ta, tb) and T.compat(tb, ta)):
                d.append(dg.error(a.name.range, "KIFP026", f"parameter {a.name.text!r}: the interface has {tb}, found {ta}"))
        ra, rb = chk.ty(f.ret), chk.ty(im.ret, imod)
        if not (T.compat(ra, rb) and T.compat(rb, ra)):
            d.append(dg.error(f.name.range, "KIFP026", f"{f.name.text!r} returns {ra}, the interface has {rb}"))
    finally:
        chk.generics.pop()
    fr, ir = receiver_of(f), receiver_of(im)
    if ir and fr and fr != ir and not (ir == "mut" and fr == "ref"):
        d.append(dg.error(f.name.range, "KIFP026", f"receiver differs from the interface ({ir})"))
