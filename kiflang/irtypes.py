# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Structured types of the IR (docs/kif-ir.md, "Types").

`Local.ty` and `Temp.ty` stay text, because text is what the data encoding carries; `Local.type` / `Temp.type` parse it into a `Type`
(cached), so a pass or a code generator asks `t.is_signed` instead of comparing strings. `str(parse_type(s)) == s` for every text
the builder produces (`ty_text` writes the canonical form)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from . import types as T

SIGNED = {"i8": 8, "i16": 16, "i32": 32, "i64": 64, "i128": 128, "isize": 64}
UNSIGNED = {"u8": 8, "u16": 16, "u32": 32, "u64": 64, "u128": 128, "usize": 64}
INTEGERS = {**SIGNED, **UNSIGNED}
FLOATS = {"f32": 32, "f64": 64}
SCALARS = frozenset(INTEGERS) | frozenset(FLOATS) | {"bool", "char"}

# kind of a Type
SCALAR, UNIT, REF, SLICE, ARRAY, TUPLE, FN, DYN, NAMED, UNKNOWN = "scalar", "unit", "ref", "slice", "array", "tuple", "fn", "dyn", "named", "unknown"


@dataclass(frozen=True)
class Type:
    """`kind` is one of the constants above. `name`: the scalar's or named type's name (`vector`, `optional`, a struct's path as written),
    `args` its type arguments (the target of a reference, the element of a slice or array, the members of a tuple, the parameters of
    a `fn` followed by its result). `mut` marks `&mut`. `length` is the element count of an array when it is a constant."""
    kind: str
    name: str = ""
    args: Tuple["Type", ...] = ()
    mut: bool = False
    length: Optional[int] = None

    def __str__(self) -> str:
        k = self.kind
        if k == REF:
            return ("&mut " if self.mut else "&") + str(self.args[0])
        if k == SLICE:
            return f"[{self.args[0]}]"
        if k == ARRAY:
            return f"[{self.args[0]}; {'_' if self.length is None else self.length}]"
        if k == TUPLE:
            return "(" + ", ".join(str(a) for a in self.args) + ")"
        if k == FN:
            ret = self.args[-1]
            return "fn(" + ", ".join(str(a) for a in self.args[:-1]) + ")" + ("" if ret.kind == UNIT else f" -> {ret}")
        if k == DYN:
            return "dyn " + self.name
        if k == UNIT:
            return "()"
        if k == UNKNOWN:
            return "?"
        return self.name + (f"<{', '.join(str(a) for a in self.args)}>" if self.args else "")

    # -- questions a pass or a generator asks
    @property
    def is_scalar(self) -> bool:
        return self.kind == SCALAR

    @property
    def is_integer(self) -> bool:
        return self.kind == SCALAR and self.name in INTEGERS

    @property
    def is_signed(self) -> bool:
        return self.kind == SCALAR and self.name in SIGNED

    @property
    def is_unsigned(self) -> bool:
        return self.kind == SCALAR and self.name in UNSIGNED

    @property
    def bits(self) -> Optional[int]:
        """The width of an integer or float (`usize` and `isize` count as 64), None for anything else."""
        return INTEGERS.get(self.name) or FLOATS.get(self.name) if self.kind == SCALAR else None

    @property
    def target(self) -> Optional["Type"]:
        """What a reference points at, what a slice, array or vector holds, what an optional or pointer wraps; None for other types."""
        if self.kind in (REF, SLICE, ARRAY) or self.kind == NAMED and self.name in ("vector", "optional", "ptr", "slot") and self.args:
            return self.args[0]
        return None

    @property
    def is_sequence(self) -> bool:
        return self.kind in (SLICE, ARRAY) or self.kind == NAMED and self.name in ("vector", "str", "str_view")

    @property
    def is_unknown(self) -> bool:
        return self.kind == UNKNOWN


UNKNOWN_TYPE = Type(UNKNOWN)
UNIT_TYPE = Type(UNIT)


def ty_text(t: Optional[T.Ty]) -> Optional[str]:
    """The canonical text of a checker type (None when it is unknown): generic arguments and tuple members included, array length not
    (the builder keeps a constant length on the `Local`)."""
    if t is None or t == T.UNKNOWN:
        return None
    if t.name == "&":
        return ("&mut " if t.mut else "&") + (ty_text(t.args[0]) or "?")
    if t.name == "fn":
        return str(Type(FN, args=tuple(parse_type(ty_text(a) or "?") for a in t.args)))
    if t.cat == "dyn":
        return "dyn " + t.name
    if t.name == "(,)":
        return "(" + ", ".join(ty_text(a) or "?" for a in t.args) + ")"
    if t.name == "[]":
        return f"[{ty_text(t.args[0]) or '?'}]"
    if t.name == "[;]":
        return f"[{ty_text(t.args[0]) or '?'}; _]"
    return t.name + (f"<{', '.join(ty_text(a) or '?' for a in t.args)}>" if t.args else "")


class _Parser:
    def __init__(self, text: str) -> None:
        self.s, self.i = text, 0

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def eat(self, lit: str) -> bool:
        self.ws()
        if self.s.startswith(lit, self.i):
            self.i += len(lit)
            return True
        return False

    def word(self) -> str:
        self.ws()
        j = self.i
        while j < len(self.s) and (self.s[j].isalnum() or self.s[j] in "_:"):
            j += 1
        w, self.i = self.s[self.i:j], j
        return w

    def list_until(self, close: str) -> Tuple[Type, ...]:
        out = []
        if self.eat(close):
            return ()
        while True:
            out.append(self.type())
            if self.eat(close):
                return tuple(out)
            if not self.eat(","):
                raise ValueError(f"bad type text {self.s!r}")

    def type(self) -> Type:
        if self.eat("&"):
            self.ws()
            mut = self.s.startswith("mut ", self.i)
            if mut:
                self.i += 4
            return Type(REF, args=(self.type(),), mut=mut)
        if self.eat("["):
            elem = self.type()
            if self.eat("]"):
                return Type(SLICE, args=(elem,))
            if not self.eat(";"):
                raise ValueError(f"bad type text {self.s!r}")
            n = self.word()
            if not self.eat("]"):
                raise ValueError(f"bad type text {self.s!r}")
            return Type(ARRAY, args=(elem,), length=int(n) if n.isdigit() else None)
        if self.eat("("):
            members = self.list_until(")")
            return Type(TUPLE, args=members) if members else UNIT_TYPE
        if self.eat("?"):
            return UNKNOWN_TYPE
        w = self.word()
        if w == "dyn":
            return Type(DYN, self.word())
        if w == "fn":
            if not self.eat("("):
                raise ValueError(f"bad type text {self.s!r}")
            params = self.list_until(")")
            ret = self.type() if self.eat("->") else UNIT_TYPE
            return Type(FN, args=params + (ret,))
        if not w:
            raise ValueError(f"bad type text {self.s!r}")
        if self.eat("<"):
            return Type(NAMED, w, self.list_until(">"))
        return Type(SCALAR, w) if w in SCALARS else Type(NAMED, w)


def parse_type(text: Optional[str]) -> Type:
    """The structured form of a type text; `?` or None is `UNKNOWN_TYPE`, and text that does not parse is too (the IR never fails on a
    type it does not understand: it just does not know it)."""
    if text is None:
        return UNKNOWN_TYPE
    try:
        p = _Parser(text)
        t = p.type()
        p.ws()
        return t if p.i == len(text) else UNKNOWN_TYPE
    except ValueError:
        return UNKNOWN_TYPE
