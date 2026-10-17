# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Checker step 3: the types of expressions (shared by `bodies.py`).

Types are deliberately *gradual*: only plain data is checked strictly (integers, `bool`, `char`, `()`, kif structs and
enums that are not generic, and references to those). Everything else (slices, arrays, `extern "C++"` types, generics, `Self`,
method results, ...) is `unknown`/opaque and compatible with anything until the later steps give it real rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

INT_RANGES = {
    "i8": (-(1 << 7), (1 << 7) - 1), "i16": (-(1 << 15), (1 << 15) - 1), "i32": (-(1 << 31), (1 << 31) - 1),
    "i64": (-(1 << 63), (1 << 63) - 1), "isize": (-(1 << 63), (1 << 63) - 1),
    "u8": (0, (1 << 8) - 1), "u16": (0, (1 << 16) - 1), "u32": (0, (1 << 32) - 1),
    "u64": (0, (1 << 64) - 1), "usize": (0, (1 << 64) - 1),
}


@dataclass(frozen=True)
class Ty:
    name: str
    args: Tuple["Ty", ...] = ()
    mut: bool = False
    key: str = ""  # identity of a named type (module key + name)
    cat: str = "other"  # prim | adt | other
    sym: object = field(default=None, compare=False, hash=False)  # the declaring symbol of a named type

    def __str__(self) -> str:
        if self.name == "&":
            return ("&mut " if self.mut else "&") + str(self.args[0])
        if self.name == "fn":
            ret = self.args[-1]
            return "fn(" + ", ".join(str(a) for a in self.args[:-1]) + ")" + ("" if ret == UNIT else f" -> {ret}")
        if self.cat == "dyn":
            return "dyn " + self.name
        if self.name == "(,)":
            return "(" + ", ".join(str(a) for a in self.args) + ")"
        if self.name == "[]":
            return f"[{self.args[0]}]"
        if self.name == "[;]":
            return f"[{self.args[0]}; _]"
        return self.name


UNKNOWN = Ty("?")
NEVER = Ty("!", cat="prim")
UNIT = Ty("()", cat="prim")
BOOL = Ty("bool", cat="prim")
INTLIT = Ty("{integer}", cat="prim")
STRLIT = Ty("{string}")


# What a bound lets the generic body do (4.9): the fixed set, anything else is an interface name.
_IMPLIES = {
    "integer": {"integer", "copy", "eq", "ord", "hash"}, "signed": {"signed", "integer", "copy", "eq", "ord", "hash"},
    "unsigned": {"unsigned", "integer", "copy", "eq", "ord", "hash"}, "ord": {"ord", "eq"},
}


class ParamSym:
    """The `sym` of a type parameter's type: it looks like a symbol (`kind`) and carries the bounds."""

    kind = "param"
    node = None
    generics: list = []
    module = None

    def __init__(self, bounds) -> None:
        self.bounds = list(bounds)


def bounds_of(t: Ty) -> list:
    return [b.text for b in getattr(t.sym, "bounds", ())]


def implied(bounds) -> set:
    out: set = set()
    for b in bounds:
        out |= _IMPLIES.get(b, {b})
    return out


def tuple_of(elems) -> Ty:
    return Ty("(,)", tuple(elems), cat="tuple")


def prim(name: str) -> Ty:
    return Ty(name, cat="prim")


def ref(inner: Ty, mut: bool = False) -> Ty:
    return Ty("&", (inner,), mut)


def is_int(t: Ty) -> bool:
    if t.cat == "param":
        return bool(implied(bounds_of(t)) & {"integer"})
    return t.name in INT_RANGES or t == INTLIT


def is_flexible(t: Ty) -> bool:
    return t in (UNKNOWN, NEVER, INTLIT, STRLIT)


def simple(t: Ty) -> bool:
    """Strictly checked: plain data and references to it."""
    if t.name == "&":
        return simple(t.args[0])
    if t.cat == "tuple":
        return True
    return t.cat in ("prim", "adt", "param") and t != UNKNOWN


def compat(actual: Ty, expected: Ty) -> bool:
    """Can a value of type `actual` be used where `expected` is wanted? (no implicit numeric conversions)"""
    if actual == expected or UNKNOWN in (actual, expected) or NEVER in (actual, expected) or STRLIT in (actual, expected):
        return True
    if actual == INTLIT:
        return not simple(expected) or is_int(expected)
    if expected == INTLIT:
        return not simple(actual) or is_int(actual)
    if not (simple(actual) and simple(expected)):
        return True
    if actual.cat == "tuple" and expected.cat == "tuple":
        return len(actual.args) == len(expected.args) and all(compat(a, e) for a, e in zip(actual.args, expected.args))
    if actual.name == "&" and expected.name == "&":
        if expected.mut and not actual.mut:
            return False
        return compat(actual.args[0], expected.args[0])
    return False


def join(a: Ty, b: Ty) -> Optional[Ty]:
    """The common type of two branches, or None when they clash."""
    if a == NEVER:
        return b
    if b == NEVER:
        return a
    if a == UNKNOWN or b == UNKNOWN:
        return UNKNOWN
    if a == INTLIT and is_int(b):
        return b
    if b == INTLIT and is_int(a):
        return a
    if compat(a, b):
        return b if not is_flexible(b) else a
    if compat(b, a):
        return a
    return None


def fits(value: int, name: str) -> bool:
    lo, hi = INT_RANGES[name]
    return lo <= value <= hi
