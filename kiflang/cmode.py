# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""C-mode restrictions (KIFP053), the construction-protocol signatures (KIFP086) and kobj-less targets (KIFP051), layout/ops attributes (KIFP087).

Both are syntactic checks over the module's tree. KIFP053 runs only in C mode (`#![target(c)]`, or a cfg environment whose
language is C) and lists what the C backend cannot represent (docs/kif-proc-language.md 9.1, docs/kif-c-backends.md). Items under
a `#[cfg]` that excludes C are not checked. KIFP086 runs in every mode.
"""

from __future__ import annotations

from typing import Optional

from . import ast
from . import cfg
from . import diagnostics as dg
from .astutil import attr_name_arg, has_attr, walk

# prelude types with no C lowering; `ptr<T>`, `vector<T>`, `str`, `span<T>` and `result<T>` do have one (3.1, 3.2)
NOT_IN_C = ("allocator_ref", "arc", "weak", "rc", "unique")
# `write!` and friends need microfmt, which is C++
NOT_IN_C_MACROS = ("write", "try_write", "info", "warn", "error", "debug", "trace", "here")
_C = cfg.Pred("flag", "c")


def is_c_mode(mod) -> bool:
    if attr_name_arg(mod.tree.inner_attrs, "target") == "c":
        return True
    return mod.cfg is not None and "c" in mod.cfg.flags


def _excludes_c(attrs) -> bool:
    p = cfg.of_attrs(attrs)
    return p is not None and cfg.exclusive(p, _C)


def _generic(params) -> bool:
    return any(not g.is_lifetime for g in params)


def check_c_mode(chk) -> None:
    mod = chk.mod
    if not is_c_mode(mod):
        return
    d = mod.diagnostics

    def refuse(rng, what: str) -> None:
        d.append(dg.error(rng, "KIFP053", f"{what} is not supported by the C backend"))

    def scan(node) -> None:
        for n in walk(node):
            if isinstance(n, ast.Try):
                continue  # `?` lowers to `if ((rc = e) != 0) return rc;`
            if isinstance(n, ast.PathType) and n.segments[-1].name in NOT_IN_C:
                refuse(n.range, f"type {n.segments[-1].name!r}")
            elif isinstance(n, ast.MacroCall) and n.name.text in NOT_IN_C_MACROS:
                refuse(n.range, f"macro {n.name.text}!")

    for it in mod.tree.items:
        attrs = getattr(it, "attrs", None) or []
        if _excludes_c(attrs):
            continue
        if isinstance(it, (ast.ExternFn, ast.ExternType, ast.ExternImpl)):
            if it.abi == "C++":
                refuse(it.range, 'extern "C++"')
            continue
        if isinstance(it, ast.FnDecl):
            if _generic(it.generics):
                refuse(it.name.range, f"generic fn {it.name.text!r}")
            scan(it)
        elif isinstance(it, ast.ImplDecl):
            if _generic(it.generics):
                refuse(it.range, "a generic impl")
            if it.iface is not None:
                isym = chk.sym_type(it.iface.text)
                if isym is not None and isinstance(isym.node, ast.Interface) and not (has_attr(isym.node.attrs, "kobj") or has_attr(isym.node.attrs, "ops")):
                    refuse(it.range, f"`impl {it.iface.text} for ..` of a plain interface (use #[ops])")
            for f in it.items:
                if isinstance(f, ast.FnDecl):
                    if _generic(f.generics):
                        refuse(f.name.range, f"generic fn {f.name.text!r}")
                    scan(f)
        elif isinstance(it, (ast.StructDecl, ast.StaticDecl, ast.ConstDecl)):
            scan(it)


def check_kobj_target(chk) -> None:
    """A `#[kobj]` interface needs a kobj emitter; only checked when a C target is selected (`--target`)."""
    env = chk.mod.cfg
    if env is None or "c" not in env.flags or "kobj" in env.flags:
        return
    target = next(iter(env.values.get("target", ())), "the selected target")
    for it in chk.mod.tree.items:
        if isinstance(it, ast.Interface) and has_attr(it.attrs, "kobj") and not _excludes_kobj(it.attrs):
            chk.mod.diagnostics.append(dg.error(it.name.range, "KIFP051", f"#[kobj] interface {it.name.text!r}: target {target!r} has no kobj emitter"))


def _excludes_kobj(attrs) -> bool:
    p = cfg.of_attrs(attrs)
    return p is not None and cfg.exclusive(p, cfg.Pred("flag", "kobj"))


def check_layout_attrs(chk) -> None:
    """`#[typedef]`, `#[repr(c|packed)]`, `#[align(n)]` and `#[ops]` (docs/kif-c-backends.md 2 and 5.1)."""
    d = chk.mod.diagnostics
    for it in chk.mod.tree.items:
        attrs = getattr(it, "attrs", None) or []
        kind = "struct" if isinstance(it, ast.StructDecl) else "enum" if isinstance(it, ast.EnumDecl) else ""

        def bad(a, why: str) -> None:
            d.append(dg.error(a.name.range, "KIFP087", f"#[{a.name.text}]: {why}"))

        for a in attrs:
            n = a.name.text
            if n == "typedef" and (not kind or a.args):
                bad(a, "goes on a struct or enum and takes no arguments")
            elif n == "align":
                v = a.args[0].value.value if a.args and len(a.args) == 1 and a.args[0].value is not None else None
                if kind != "struct":
                    bad(a, "goes on a struct")
                elif not isinstance(v, int) or isinstance(v, bool) or v <= 0 or v & (v - 1):
                    bad(a, "takes one power-of-two integer, e.g. #[align(16)]")
            elif n == "repr" and kind == "struct":
                names = [x.name for x in a.args or []]
                if not names or any(x not in ("c", "packed") for x in names):
                    bad(a, "on a struct is `c`, `packed` or both")
            elif n == "repr" and kind == "enum":
                bad(a, "goes on a struct (an enum names its type: `enum mode: u8`)")
            elif n == "ops":
                if not isinstance(it, ast.Interface) or has_attr(it.attrs, "kobj") or a.args:
                    bad(a, "goes on a plain (non-#[kobj]) interface and takes no arguments")


def _is(t, name: str, nargs: Optional[int] = None) -> bool:
    return isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == name and (
        nargs is None or len(t.segments[0].args) == nargs)


def _self_ty(t, target) -> bool:
    return _is(t, "Self") or (isinstance(t, ast.PathType) and isinstance(target, ast.PathType) and t.text == target.text)


def _result_of(t) -> Optional[object]:
    return t.segments[0].args[0] if _is(t, "result", 1) else None


def _unit(t) -> bool:
    return isinstance(t, ast.TupleType) and not t.elems


def _receiver(f: ast.FnDecl) -> str:
    return f.params[0].receiver if f.params and f.params[0].receiver else ""


def check_protocol(chk) -> None:
    """The construction protocol names (docs/kif-proc-language.md 4.7) keep their shapes, so lowering can rely on them."""
    d = chk.mod.diagnostics
    for it in chk.mod.tree.items:
        if not isinstance(it, ast.ImplDecl) or it.iface is not None:
            continue
        for f in it.items:
            if not isinstance(f, ast.FnDecl):
                continue
            name, rcv = f.name.text, _receiver(f)

            def bad(why: str) -> None:
                d.append(dg.error(f.name.range, "KIFP086", f"{name}: {why}"))

            inner = _result_of(f.ret) if f.ret is not None else None
            if name in ("try_create", "try_allocate"):
                if rcv:
                    bad("takes no receiver")
                elif inner is None or not _self_ty(inner, it.target):
                    bad("must return result<Self>")
            elif name == "try_clone":
                if rcv != "ref":
                    bad("takes `&self`")
                elif inner is None or not _self_ty(inner, it.target):
                    bad("must return result<Self>")
            elif name == "try_construct":
                if rcv != "mut":
                    bad("takes `&mut self`")
                elif inner is None or not _unit(inner):
                    bad("must return result<()>")
            elif name == "try_construct_at":
                params = [p for p in f.params if not p.receiver]
                if rcv:
                    bad("is an associated function (no receiver)")
                elif not params or params[0].name.text != "mem" or not (
                        _is(params[0].ty, "c_ptr", 1) and _self_ty(params[0].ty.segments[0].args[0], it.target)):
                    bad("first parameter must be `mem: c_ptr<Self>`")
                elif inner is None or not _unit(inner):
                    bad("must return result<()>")


def check(chk) -> None:
    check_protocol(chk)
    check_layout_attrs(chk)
    check_c_mode(chk)
    check_kobj_target(chk)
