# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Sizes and alignments of types (docs/kif-stack.md section 4), for the stack estimate.

`size_align(type, plan, structs)` answers `(size, align)` in bytes under the natural C layout of the target's pointer width, or
None when kif cannot tell (a generic, an opaque or imported type, an unknown type): a caller must treat None as unknown, never as 0.
`struct_table(items)` collects the structs and enums of a module so named types can be resolved."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import ast, irtypes
from .irtypes import Type, parse_type
from .target import StackPlan

SizeAlign = Optional[Tuple[int, int]]

_ENUM_DEFAULT = 4  # a C `int`, what an enum without `repr` is


def struct_table(items) -> Dict[str, object]:
    """Name to declaration for the structs and enums written at the top level of a module."""
    return {it.name.text: it for it in items if isinstance(it, (ast.StructDecl, ast.EnumDecl))}


def _attr(attrs, name: str):
    return next((a for a in attrs if a.name.text == name), None)


def _attr_int(a) -> Optional[int]:
    for x in getattr(a, "args", None) or []:
        v = getattr(getattr(x, "value", None), "value", None)
        if isinstance(v, int):
            return v
    return None


def _scalar(name: str, plan: StackPlan) -> SizeAlign:
    ptr = plan.pointer_bits // 8
    if name in ("usize", "isize"):
        return ptr, ptr
    if name == "bool":
        return 1, 1
    if name == "char":
        return 4, 4
    bits = irtypes.INTEGERS.get(name)
    if bits is None:
        return None  # floats are not supported by kif
    n = bits // 8
    return n, min(n, 16 if bits == 128 else 8)


def size_align(t: Type, plan: StackPlan, structs: Optional[Dict[str, object]] = None, _seen: Tuple[str, ...] = ()) -> SizeAlign:
    """(size, align) of `t`, or None when unknown."""
    ptr = plan.pointer_bits // 8
    k = t.kind
    if k == irtypes.UNIT:
        return 0, 1
    if k == irtypes.SCALAR:
        return _scalar(t.name, plan)
    if k in (irtypes.REF, irtypes.FN):
        return ptr, ptr
    if k == irtypes.SLICE:
        return 2 * ptr, ptr  # pointer and length
    if k == irtypes.DYN:
        return 2 * ptr, ptr  # data pointer and vtable
    if k == irtypes.ARRAY:
        inner = size_align(t.args[0], plan, structs, _seen)
        if inner is None or t.length is None:
            return None
        return inner[0] * t.length, inner[1]
    if k == irtypes.TUPLE:
        return _aggregate([size_align(a, plan, structs, _seen) for a in t.args], 0)
    if k == irtypes.NAMED:
        return _named(t, plan, structs or {}, _seen)
    return None


def _aggregate(parts, min_align: int, packed: bool = False) -> SizeAlign:
    off = align = 0
    for p in parts:
        if p is None:
            return None
        size, al = p
        if packed:
            al = 1
        off += -off % al + size
        align = max(align, al)
    align = max(align, min_align, 1)
    return off + (-off % align), align


def _named(t: Type, plan: StackPlan, structs: Dict[str, object], seen: Tuple[str, ...]) -> SizeAlign:
    ptr = plan.pointer_bits // 8
    n = t.name
    if n in ("ptr", "c_ptr", "box"):
        return ptr, ptr
    if n in ("vector", "secure_vector"):
        return 3 * ptr, ptr  # ptr, len, cap
    if n == "str":
        return 3 * ptr, ptr
    if n == "strview":
        return 2 * ptr, ptr
    if n in ("optional", "result") and t.args:
        inner = size_align(t.args[0], plan, structs, seen)
        if inner is None:
            return None
        return _aggregate([(1, 1), inner], 0)  # a tag then the payload
    decl = structs.get(n)
    if decl is None or n in seen:
        return None  # imported, generic or recursive by value: unknown
    if isinstance(decl, ast.EnumDecl):
        if decl.repr is not None:
            return size_align(parse_type(irtypes_text(decl.repr)), plan, structs, seen) or (_ENUM_DEFAULT, _ENUM_DEFAULT)
        return _ENUM_DEFAULT, _ENUM_DEFAULT
    if decl.generics:
        return None
    packed = _repr_has(decl, "packed")
    forced = _attr(decl.attrs, "align")
    parts = [size_align(parse_type(irtypes_text(f.ty)), plan, structs, seen + (n,)) for f in decl.fields]
    return _aggregate(parts, (_attr_int(forced) or 0) if forced is not None else 0, packed)


def irtypes_text(ty) -> str:
    from .ir import type_text
    return type_text(ty)


def _repr_has(decl, word: str) -> bool:
    a = _attr(decl.attrs, "repr")
    return a is not None and any(x.name == word for x in (getattr(a, "args", None) or []))


def report(items, plan: StackPlan, file: str) -> str:
    """Every struct and enum of a module with its size and alignment, for the demo and for reading the estimate (`unknown` when kif cannot tell)."""
    table = struct_table(items)
    out = [f"Layout of {file} (pointer {plan.pointer_bits} bits):"]
    for name, decl in table.items():
        sa = size_align(parse_type(name), plan, table)
        what = "unknown" if sa is None else f"size {sa[0]}, align {sa[1]}"
        out.append(f"  {'enum' if isinstance(decl, ast.EnumDecl) else 'struct'} {name}: {what}")
    return "\n".join(out) + "\n"
