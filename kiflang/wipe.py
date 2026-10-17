# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`#[secure_wipe]` and `#[wipe_exempt]`: which declarations may carry them (docs/kif-secure-wipe.md, stage 1).

* KIFW001 `#[secure_wipe]` on a variable or parameter whose type is not plain data;  * KIFW002 a secure struct that has a `drop`;
* KIFW003 a field of a secure struct that is not wipeable and not exempt;            * KIFW004 an attribute where it has no effect;
* KIFW005 a secure variable that is shared (atomic, a static).

`plain` is the trivial-type classification: a value whose bytes can be overwritten without leaking, double-freeing or
breaking an invariant.
"""

from __future__ import annotations

from . import ast
from . import diagnostics as dg
from . import methods as M
from .types import Ty

# extern value types that are plain data when their arguments are
_VALUE_TYPES = frozenset({"optional", "wrapping", "saturating", "checked_value", "duration", "instant", "non_zero", "error"})


def _attr(attrs, name: str):
    return next((a for a in attrs if a.name.text == name), None)


def is_volatile(attrs) -> bool:
    """`#[secure_wipe(volatile)]`: the wipe goes through the target's volatile primitive (rule W2)."""
    a = _attr(attrs, "secure_wipe")
    return a is not None and any(x.name == "volatile" for x in a.args or [])


def check_args(bc, a) -> None:
    """The only argument `#[secure_wipe]` takes is `volatile`."""
    if a is None or a.raw is None and not a.args:
        return
    if a.args is None or len(a.args) > 1 or any(x.name != "volatile" or x.args or x.value is not None for x in a.args):
        bc.d.append(dg.error(a.range, "KIFW004", "#[secure_wipe] takes no argument but `volatile`"))


def is_secure_struct(sym) -> bool:
    return sym is not None and sym.kind == "struct" and _attr(sym.node.attrs, "secure_wipe") is not None


def plain(bc, t: Ty, seen: frozenset = frozenset()) -> str:
    """Why `t` is not plain data ('' when it is)."""
    if t.name == "&":
        return "a reference"
    if t.name == "fn":
        return "a function value"
    if t.name == "[]":
        return "a slice"
    if t.name == "[;]":
        return plain(bc, t.args[0], seen)
    if t.name == "(,)":
        return next((r for r in (plain(bc, x, seen) for x in t.args) if r), "")
    if t.cat == "prim":
        return ""
    sym = t.sym
    if sym is None:
        return f"{t} is not known to be plain data"
    if sym.kind == "enum":
        return ""
    if sym.kind == "struct" and t.cat == "adt":
        if is_secure_struct(sym):
            return f"{t} is a secure struct with its own generated drop"
        if bc.index.get(t.key, {}).get("drop") is not None:
            return f"{t} has a drop"
        if id(sym.node) in seen:
            return ""
        for f in sym.node.fields:
            r = plain(bc, bc.ty(f.ty, sym.module), seen | {id(sym.node)})
            if r:
                return f"field {f.name.text!r} of {t}: {r}"
        return ""
    if t.name in _VALUE_TYPES:
        return next((r for r in (plain(bc, x, seen) for x in t.args) if r), "")
    return f"{t} owns or borrows something"


def wipeable(bc, t: Ty) -> bool:
    """A field type that a secure struct's generated `drop` can wipe: plain data, a secure struct, `secure_vector<T>`."""
    if t.name == "[;]":
        return wipeable(bc, t.args[0])
    return not plain(bc, t) or is_secure_struct(t.sym) or t.name == "secure_vector"


def check_secure_vectors(bc, it) -> None:
    """KIFW007: `secure_vector<T>` with an element that is not plain data (rule W12.1); each written type is checked once."""
    from .astutil import walk

    seen = bc.__dict__.setdefault("_w007", set())
    for n in walk(it):
        if not isinstance(n, ast.PathType) or id(n) in seen or not n.segments or n.segments[-1].name != "secure_vector" or len(n.segments[-1].args) != 1:
            continue
        seen.add(id(n))
        arg = n.segments[-1].args[0]
        if isinstance(arg, ast.PathType) and bc.is_generic(arg.text):
            continue
        t = bc.ty(arg)
        why = "" if t.name == "?" or t.cat == "unknown" else plain(bc, t)
        if why:
            bc.d.append(dg.error(arg.range, "KIFW007", f"secure_vector<{t}>: the element is not plain data ({why}); use trivial elements, "
                                                       "or hold owning or secure values in a secure struct that has the vector as a field"))


def _shared(t: Ty) -> bool:
    return t.name == "atomic"


def check_struct(bc, it: ast.StructDecl) -> None:
    d = bc.d
    sym = bc.mod.scope_types.get(it.name.text)
    if sym is None or sym.node is not it:
        return
    secure = _attr(it.attrs, "secure_wipe") is not None
    check_args(bc, _attr(it.attrs, "secure_wipe"))
    if secure and not sym.generics:
        padding_note(bc, it)
    if secure:
        ty = Ty(it.name.text, key=f"{sym.module.key}:{it.name.text}", cat="adt", sym=sym)
        if bc.index.get(ty.key, {}).get("drop") is not None:
            d.append(dg.error(it.name.range, "KIFW002",
                              f"{it.name.text!r} has a drop and cannot be #[secure_wipe]: the generated drop and the hand-written one "
                              "would be two destructors (wipe by hand, or move the drop work into a wrapper that owns a secure struct)"))
    for f in it.fields:
        ex = _attr(f.attrs, "wipe_exempt")
        if _attr(f.attrs, "secure_wipe") is not None:
            d.append(dg.error(f.name.range, "KIFW004", "#[secure_wipe] has no effect on a field (mark the struct instead)"))
        if ex is None:
            if secure and not sym.generics and not wipeable(bc, bc.ty(f.ty, sym.module)):
                d.append(dg.error(f.name.range, "KIFW003",
                                  f"field {f.name.text!r} of secure struct {it.name.text!r} cannot be wiped "
                                  f"({plain(bc, bc.ty(f.ty, sym.module))}): tag it #[wipe_exempt] or change its type"))
        elif not secure:
            d.append(dg.error(ex.range, "KIFW004", f"#[wipe_exempt] on field {f.name.text!r}: {it.name.text!r} is not #[secure_wipe]"))
        elif not sym.generics and wipeable(bc, bc.ty(f.ty, sym.module)):
            d.append(dg.error(ex.range, "KIFW004", f"#[wipe_exempt] on field {f.name.text!r}: its type is wiped anyway, remove the tag"))


_PRIM_BYTES = {"bool": 1, "char": 4, "i8": 1, "u8": 1, "i16": 2, "u16": 2, "i32": 4, "u32": 4, "i64": 8, "u64": 8, "isize": 8, "usize": 8, "i128": 16, "u128": 16}


def _layout(bc, t, seen=()):
    """(size, align) of a field type under the natural C layout of a 64-bit target, or None when kif cannot tell (a generic, an enum, a pointer-sized
    type of an unknown target): only the types it can size are checked for padding."""
    if isinstance(t, ast.ArrayType):
        inner = _layout(bc, t.elem, seen)
        if inner is None or not (isinstance(t.length, ast.Lit) and t.length.kind == "int"):
            return None
        return inner[0] * int(t.length.value), inner[1]
    if isinstance(t, ast.PathType):
        if t.text in _PRIM_BYTES:
            n = _PRIM_BYTES[t.text]
            return n, min(n, 8)
        sym = bc.mod.scope_types.get(t.text)
        if sym is not None and sym.kind == "struct" and not sym.generics and t.text not in seen:
            return _struct_layout(bc, sym.node, seen + (t.text,))[:2] if _struct_layout(bc, sym.node, seen + (t.text,)) else None
    return None


def _struct_layout(bc, it, seen=()):
    """(size, align, padding bytes) of a struct, or None when a field cannot be sized."""
    off = pad = align = 0
    for f in it.fields:
        lay = _layout(bc, f.ty, seen)
        if lay is None:
            return None
        size, al = lay
        gap = -off % al
        pad += gap
        off += gap + size
        align = max(align, al)
    tail = -off % (align or 1)
    return off + tail, align or 1, pad + tail


def padding_note(bc, it: ast.StructDecl) -> None:
    """KIFW101: a secure struct whose natural layout has padding bytes, which the field-by-field wipe does not touch (rule W5)."""
    lay = _struct_layout(bc, it)
    if lay is not None and lay[2]:
        bc.d.append(dg.info(it.name.range, "KIFW101",
                            f"secure struct {it.name.text!r} has {lay[2]} byte(s) of padding that the field-by-field wipe does not erase "
                            "(reorder the fields or give it an explicit layout without padding)"))


def check_item_attrs(bc, it) -> None:
    """`#[secure_wipe]` is for `let`, `fn` and `struct`; `#[wipe_exempt]` for fields and `let`."""
    attrs = getattr(it, "attrs", None) or []
    if isinstance(it, ast.StaticDecl):
        a = _attr(attrs, "secure_wipe")
        if a is not None:
            bc.d.append(dg.error(a.range, "KIFW005", f"static {it.name.text!r} is shared: a secure variable is a local owned by one thread of control"))
        return
    if not isinstance(it, (ast.StructDecl, ast.FnDecl)):
        a = _attr(attrs, "secure_wipe")
        if a is not None:
            bc.d.append(dg.error(a.range, "KIFW004", "#[secure_wipe] applies to a let, a function or a struct"))
    a = _attr(attrs, "wipe_exempt")
    if a is not None:
        bc.d.append(dg.error(a.range, "KIFW004", "#[wipe_exempt] applies to a struct field or a let in a #[secure_wipe] function"))


def check_fn(bc, f: ast.FnDecl) -> None:
    secure_fn = _attr(f.attrs, "secure_wipe") is not None
    check_args(bc, _attr(f.attrs, "secure_wipe"))
    for p in f.params:
        sw, ex = _attr(p.attrs, "secure_wipe"), _attr(p.attrs, "wipe_exempt")
        check_args(bc, sw)
        if ex is not None:
            bc.d.append(dg.error(ex.range, "KIFW004", "#[wipe_exempt] on a parameter: a function-level #[secure_wipe] never wipes parameters"))
        if sw is None:
            continue
        if p.receiver or p.ty is None:
            bc.d.append(dg.error(sw.range, "KIFW001", "#[secure_wipe] on a receiver: only plain by-value parameters can be wiped"))
            continue
        t = bc.ty(p.ty)
        if _shared(t):
            bc.d.append(dg.error(sw.range, "KIFW005", f"parameter {p.name.text!r} is atomic: a secure variable is owned by one thread of control"))
            continue
        why = plain(bc, t)
        if why:
            bc.d.append(dg.error(sw.range, "KIFW001", f"#[secure_wipe] on parameter {p.name.text!r}: {why}, so it cannot be overwritten"))


def check_let(bc, s: ast.Let, var_ty: Ty) -> None:
    sw, ex = _attr(s.attrs, "secure_wipe"), _attr(s.attrs, "wipe_exempt")
    check_args(bc, sw)
    in_secure_fn = bc.cur is not None and M.has_attr(getattr(bc.cur, "attrs", []), "secure_wipe")
    if ex is not None and not in_secure_fn:
        bc.d.append(dg.error(ex.range, "KIFW004", f"#[wipe_exempt] on {s.name.text!r}: the enclosing function is not #[secure_wipe]"))
    if ex is not None and sw is not None:
        bc.d.append(dg.error(ex.range, "KIFW004", f"{s.name.text!r} is both #[secure_wipe] and #[wipe_exempt]"))
    if sw is None:
        return
    if _shared(var_ty):
        bc.d.append(dg.error(sw.range, "KIFW005", f"{s.name.text!r} is atomic: a secure variable is owned by one thread of control"))
        return
    why = plain(bc, var_ty)
    if why:
        bc.d.append(dg.error(sw.range, "KIFW001", f"#[secure_wipe] on {s.name.text!r}: {why}, so it cannot be overwritten"))
