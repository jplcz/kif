# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Read-only reflection handles for `gen fn` (docs/kif-proc-language.md section 6.1).

A `Handle` wraps one syntax node of an analysed module. Every accessor is pure and returns interpreter values
(`str`, ints, `bool`, `Opt`, `VecVal`) or other handles; nothing here can change the module.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import ast
from .lexer import comment_text

_C_NAMES = {
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "usize": "size_t", "isize": "ptrdiff_t", "bool": "bool", "char": "char", "void": "void",
}
_BITS = {"i8": 8, "u8": 8, "i16": 16, "u16": 16, "i32": 32, "u32": 32, "i64": 64, "u64": 64, "bool": 1}


class Handle:
    """An opaque reference to a declaration: `kind` is the reflection type name (`Interface`, `Method`, ..)."""

    __slots__ = ("kind", "node", "mod", "index")

    def __init__(self, kind: str, node: Any, mod: Any, index: int = 0) -> None:
        self.kind, self.node, self.mod, self.index = kind, node, mod, index

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Handle) and self.kind == other.kind and self.node is other.node

    def __hash__(self) -> int:
        return id(self.node)

    def __repr__(self) -> str:
        n = getattr(self.node, "name", None)
        return f"<{self.kind} {n.text}>" if n is not None else f"<{self.kind}>"


def module_handle(mod) -> Handle:
    """The `Module` handle a generator is given."""
    return Handle("Module", mod, mod)


def _vec(items: List[Any]):
    from .interp import VecVal

    return VecVal(items)


def _opt(v: Any):
    from .interp import Opt

    return Opt(True, v) if v is not None else Opt(False)


def _doc(h: Handle) -> str:
    """The `//` comment lines directly above a declaration (attribute lines are skipped over)."""
    lines = h.mod.source.splitlines()
    i = h.node.range.start.line - 1
    while i >= 0 and lines[i].lstrip().startswith("#["):
        i -= 1
    out: List[str] = []
    while i >= 0:
        text = comment_text(lines[i])
        if text is None:
            break
        out.append(text)
        i -= 1
    return "\n".join(reversed(out))


def _attrs(h: Handle) -> List[Handle]:
    return [Handle("Attr", a, h.mod) for a in getattr(h.node, "attrs", []) if not a.inner]


def _attr(h: Handle, name: str):
    return _opt(next((a for a in _attrs(h) if a.node.name.text == name), None))


def _arg(h: Handle, key: str) -> Optional[ast.AttrArg]:
    return next((a for a in (h.node.args or []) if a.name == key), None)


def _text(t: Any) -> str:
    """Kif source text of a type."""
    if isinstance(t, ast.PathType):
        args = [a for a in t.segments[-1].args if not isinstance(a, ast.Lifetime)]
        return t.text + ("<" + ", ".join(_text(a) if not isinstance(a, ast.ConstArg) else "N" for a in args) + ">" if args else "")
    if isinstance(t, ast.RefType):
        return ("&mut " if t.mut else "&") + _text(t.inner)
    if isinstance(t, ast.SliceType):
        return "[" + _text(t.elem) + "]"
    if isinstance(t, ast.ArrayType):
        return "[" + _text(t.elem) + "; _]"
    if isinstance(t, ast.TupleType):
        return "(" + ", ".join(_text(x) for x in t.elems) + ")"
    if isinstance(t, ast.FnType):
        return "fn(" + ", ".join(_text(x) for x in t.params) + ") -> " + _text(t.ret)
    return "()"


def _spelling(t: Any, cxx: bool) -> str:
    if isinstance(t, ast.PathType):
        name = t.text
        if name in _C_NAMES:
            return _C_NAMES[name]
        args = [a for a in t.segments[-1].args if not isinstance(a, ast.Lifetime)]
        if name in ("ptr", "c_ptr") and args:
            return _spelling(args[0], cxx) + " *"
        return name
    if isinstance(t, ast.RefType):
        return _spelling(t.inner, cxx) + (" &" if cxx else " *")
    if isinstance(t, ast.TupleType) and not t.elems:
        return "void"
    return _text(t)


def _type_kind(t: Any) -> str:
    if isinstance(t, ast.PathType):
        name = t.text
        if name == "bool":
            return "bool"
        if name in _BITS or name in ("usize", "isize"):
            return "int"
        if name in ("ptr", "c_ptr"):
            return "ptr"
        if name == "str":
            return "str"
        return "named"
    return {"RefType": "ref", "SliceType": "slice", "ArrayType": "array", "FnType": "fn", "TupleType": "tuple"}.get(
        type(t).__name__, "unknown") if not (isinstance(t, ast.TupleType) and not t.elems) else "unit"


def _type_inner(h: Handle):
    t = h.node
    if isinstance(t, (ast.RefType,)):
        return _opt(Handle("Type", t.inner, h.mod))
    if isinstance(t, (ast.SliceType, ast.ArrayType)):
        return _opt(Handle("Type", t.elem, h.mod))
    if isinstance(t, ast.PathType):
        args = [a for a in t.segments[-1].args if not isinstance(a, (ast.Lifetime, ast.ConstArg))]
        if args:
            return _opt(Handle("Type", args[0], h.mod))
    return _opt(None)


def _size_bits(h: Handle):
    t = h.node
    if isinstance(t, ast.PathType):
        if t.text in _BITS:
            return _opt(_BITS[t.text])
    return _opt(None)


def _params(h: Handle) -> List[Handle]:
    return [Handle("Param", p, h.mod) for p in h.node.params if not p.receiver]


def _unit() -> ast.TupleType:
    return ast.TupleType(None, [])


def _ret(h: Handle) -> Handle:
    return Handle("Type", h.node.ret if h.node.ret is not None else _unit(), h.mod)


def _decl(p: ast.Param) -> str:
    return f"{p.name.text}: {_text(p.ty)}"


def _signature(h: Handle) -> str:
    ret = "" if h.node.ret is None else f" -> {_text(h.node.ret)}"
    return f"{h.node.name.text}({_decl_list(h)}){ret}"


def _decl_list(h: Handle) -> str:
    return ", ".join(_decl(p.node) for p in _params(h))


def _is_static(h: Handle) -> bool:
    return bool(h.node.static)


def _extends(h: Handle):
    for a in h.node.attrs:
        if a.name.text == "extends" and a.args:
            base = h.mod.types.get(a.args[0].name or "")
            if base is not None and isinstance(base.node, ast.Interface):
                return _opt(Handle("Interface", base.node, base.module))
    return _opt(None)


def _find_interface(h: Handle, name: str):
    sym = h.mod.types.get(name)
    if sym is not None and isinstance(sym.node, ast.Interface):
        return _opt(Handle("Interface", sym.node, sym.module))
    return _opt(None)


def _items(h: Handle, cls, kind: str) -> List[Handle]:
    return [Handle(kind, it, h.mod) for it in h.mod.tree.items if isinstance(it, cls)]


def _methods(h: Handle) -> List[Handle]:
    return [Handle("Method", m, h.mod, i) for i, m in enumerate(h.node.methods)]


def _cell_slot_arg(h: Handle):
    """The `T` of `slot<T>` when the cell is declared as a slot (syntactic: the last path segment is `slot`)."""
    t = h.node.ty
    if isinstance(t, ast.PathType) and t.segments[-1].name == "slot":
        args = [a for a in t.segments[-1].args if not isinstance(a, (ast.Lifetime, ast.ConstArg))]
        return args[0] if args else None
    return None


def _cell_kind(h: Handle) -> str:
    from .cells import kind_of

    return kind_of(h.node) or ""


def _cell_atomic_ok(h: Handle) -> bool:
    return any(a.name.text == "tls" and any(x.name == "atomic_ok" for x in a.args or []) for a in h.node.attrs)


def _members(h: Handle) -> List[Handle]:
    return [Handle("EnumMember", m, h.mod, i) for i, m in enumerate(h.node.members)]


def _underlying(h: Handle) -> Handle:
    return Handle("Type", h.node.repr if h.node.repr is not None else ast.PathType(None, [ast.PathSeg(None, "u32")]), h.mod)


def _value(h: Handle):
    v = h.node.value
    return _opt(v.value if isinstance(v, ast.Lit) and isinstance(v.value, int) else None)


Table = Dict[str, Callable[..., Any]]

METHODS: Dict[str, Table] = {
    "Module": {
        "name": lambda h: h.mod.name,
        "interfaces": lambda h: _vec(_items(h, ast.Interface, "Interface")),
        "enums": lambda h: _vec(_items(h, ast.EnumDecl, "Enum")),
        "structs": lambda h: _vec(_items(h, ast.StructDecl, "Struct")),
        "functions": lambda h: _vec([x for x in _items(h, ast.FnDecl, "Function") if not x.node.is_gen]),
        "cells": lambda h: _vec(_items(h, ast.StaticDecl, "Cell")),
        "find_interface": _find_interface,
    },
    "Interface": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "version": lambda h: next((int(a.args[0].value.value) for a in h.node.attrs
                                   if a.name.text == "version" and a.args and a.args[0].value is not None), 1),
        "extends": _extends,
        "methods": lambda h: _vec(_methods(h)),
        "attr": _attr,
        "attrs": lambda h: _vec(_attrs(h)),
        "is_cxx_only": lambda h: any(a.name.text == "cxx_only" for a in h.node.attrs),
    },
    "Method": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "params": lambda h: _vec(_params(h)),
        "ret": _ret,
        "is_static": _is_static,
        "has_default": lambda h: bool(h.node.has_default or h.node.body is not None or h.node.default_sym is not None),
        "attr": _attr,
        "attrs": lambda h: _vec(_attrs(h)),
        "index": lambda h: h.index,
        "params_decl": _decl_list,
        "params_names": lambda h: ", ".join(p.node.name.text for p in _params(h)),
        "signature": _signature,
    },
    "Function": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "params": lambda h: _vec(_params(h)),
        "ret": _ret,
        "attr": _attr,
        "attrs": lambda h: _vec(_attrs(h)),
        "params_decl": _decl_list,
        "params_names": lambda h: ", ".join(p.node.name.text for p in _params(h)),
        "signature": _signature,
    },
    "Param": {
        "name": lambda h: h.node.name.text,
        "ty": lambda h: Handle("Type", h.node.ty, h.mod),
        "attr": _attr,
    },
    "Type": {
        "kind": lambda h: _type_kind(h.node),
        "name": lambda h: _text(h.node),
        "inner": _type_inner,
        "size_bits": _size_bits,
        "c_spelling": lambda h: _spelling(h.node, False),
        "cpp_spelling": lambda h: _spelling(h.node, True),
    },
    "Attr": {
        "name": lambda h: h.node.name.text,
        "has": lambda h, k: _arg(h, k) is not None,
        "get": lambda h, k: _opt(_arg_value(h, k)),
        "get_int": lambda h, k: _opt(_arg_value(h, k) if isinstance(_arg_value(h, k), int) else None),
    },
    "Enum": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "underlying": _underlying,
        "members": lambda h: _vec(_members(h)),
        "is_flags": lambda h: any(a.name.text == "flags" for a in h.node.attrs),
        "attr": _attr,
    },
    "EnumMember": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "value": _value,
        "attr": _attr,
    },
    "Struct": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "fields": lambda h: _vec([Handle("Param", f, h.mod) for f in h.node.fields]),
        "attr": _attr,
        "attrs": lambda h: _vec(_attrs(h)),
    },
    "Cell": {
        "name": lambda h: h.node.name.text,
        "doc": _doc,
        "kind": _cell_kind,
        "ty": lambda h: Handle("Type", h.node.ty, h.mod),
        "is_slot": lambda h: _cell_slot_arg(h) is not None,
        "value_ty": lambda h: Handle("Type", _cell_slot_arg(h) or h.node.ty, h.mod),
        "is_atomic_ok": _cell_atomic_ok,
        "attr": _attr,
        "attrs": lambda h: _vec(_attrs(h)),
    },
}


def _arg_value(h: Handle, key: str):
    a = _arg(h, key)
    if a is None or a.value is None:
        return None
    return a.value.value


def call(h: Handle, name: str, args: List[Any]):
    """Call the accessor `name` on a handle; `KeyError` when the handle type has no such accessor."""
    return METHODS[h.kind][name](h, *args)
