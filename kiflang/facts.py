# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Fact attributes: what a function requires of its arguments and guarantees about its result (docs section 4.19a).

Parameter facts (those `requirements()` lists) are preconditions: the callee body assumes them, and any call
whose arguments it cannot prove them for keeps a run-time guard (`F` guards and calls `F__nofacts`); one that provably fails is a compile
error. The rest are hints for the optimiser. Every attribute is checked against the signature it sits on, so a wrong one is an error rather than a silent
no-op, and a `requires` is never trusted blindly (the call site proves it, or the callee's guarded entry checks it).

Parameter attributes (a precondition on that argument):

* `#[some]`        an `optional<T>` that holds a value;     * `#[non_null]`  a `ptr<T>` / `c_ptr<T>` / `c_cptr<T>` that is not null;
* `#[non_empty]`   a vector, slice or string that is not empty;
* `#[len_ge(N)]`   a vector, slice or string of at least N elements (N an integer literal);
* `#[lt_len(v)]`   an integer that is below `v.len()`, where `v` is another parameter (a vector, slice, string or array).

Function attributes:

* `#[ensures(some | non_null | non_empty | len_ge(N))]`   the result has that property (needs a matching return type);
* `#[preserves]` / `#[preserves(a, b)]`   a `&mut` argument (and `&mut self`) keeps every fact the caller knew about it;
  with no arguments it applies to every `&mut` parameter.

API attributes, for the standard library and any wrapper of a checked operation (they describe a method, and say how a
caller that has proved a fact may skip its check):

* `#[when(true | false, some | non_null | non_empty)]`   on a `bool` method: when it returns that value, the receiver has the fact
  (`has_value` is `when(true, some)`, `is_null` is `when(false, non_null)`, `is_empty` is `when(false, non_empty)`);
* `#[len_of]`   on a `usize` method that returns the receiver's element count, so `i < v.len()` is the fact `lt_len(v)`;
* `#[unchecked_form(name, fact)]`   the call may be replaced by the `#[unsafe]` method `name` of the same `impl` (same
  parameters and result) when the caller has proved `fact`: `some`, `non_null`, `non_empty` of the receiver, or `lt_len(p)` of
  the parameter `p` against the receiver's length.

Codes: KIFP210 unknown fact, KIFP211 fact does not fit the type it is on, KIFP212 bad arguments, KIFP213 `lt_len` / `preserves`
names something that is not a suitable parameter, KIFP214 `ensures` / `when` / `len_of` without a matching return type, KIFP215 an API attribute on a function without a
receiver, KIFP216 `unchecked_form` names a method that is not an `#[unsafe]` sibling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import ast
from . import diagnostics as dg

PARAM_FACTS = ("some", "non_null", "non_empty", "len_ge", "lt_len")
RESULT_FACTS = ("some", "non_null", "non_empty", "len_ge")
FN_ATTRS = ("ensures", "preserves")
API_ATTRS = ("when", "len_of", "unchecked_form")
_INTS = frozenset("u8 u16 u32 u64 usize i8 i16 i32 i64 isize".split())
_SEQ = ("vector", "str", "str_view")
_PTR = ("ptr", "c_ptr", "c_cptr")


def _strip(t):
    """The type behind any `&` / `&mut`."""
    while isinstance(t, ast.RefType):
        t = t.inner
    return t


def _kind(t) -> Optional[str]:
    """`some`, `ptr`, `seq`, `array` or `int` for the source type `t`, else None."""
    t = _strip(t)
    if isinstance(t, ast.SliceType):
        return "seq"
    if isinstance(t, ast.ArrayType):
        return "array"
    if isinstance(t, ast.PathType):
        n = t.segments[-1].name
        if n == "optional":
            return "some"
        if n in _PTR:
            return "ptr"
        if n in _SEQ:
            return "seq"
        if n in _INTS:
            return "int"
    return None


_FITS = {"some": "some", "non_null": "ptr", "non_empty": "seq", "len_ge": "seq", "lt_len": "int"}
_WANTS = {"some": "an optional<T>", "non_null": "a ptr<T>, c_ptr<T> or c_cptr<T>", "non_empty": "a vector, slice or string",
          "len_ge": "a vector, slice or string", "lt_len": "an integer"}


def _fact(d: List, a, name: str, args: List[ast.AttrArg], allowed) -> bool:
    """Whether `name(args)` is a known fact with well-formed arguments."""
    if name not in allowed:
        d.append(dg.error(a.range, "KIFP210", f"unknown fact {name!r}; the facts here are {', '.join(allowed)}"))
        return False
    if name == "len_ge":
        if not (len(args) == 1 and args[0].value is not None and args[0].value.kind == "int" and args[0].value.value >= 0):
            d.append(dg.error(a.range, "KIFP212", "len_ge takes one integer literal: `len_ge(2)`"))
            return False
    elif name == "lt_len":
        if not (len(args) == 1 and args[0].name and not args[0].args):
            d.append(dg.error(a.range, "KIFP212", "lt_len takes the name of another parameter: `lt_len(v)`"))
            return False
    elif args:
        d.append(dg.error(a.range, "KIFP212", f"{name} takes no arguments"))
        return False
    return True


_RECV_FACTS = ("some", "non_null", "non_empty")


def check_api(d: List, f: ast.FnDecl) -> None:
    """`when`, `len_of` and `unchecked_form` on `f` (method shape only; the named sibling is checked by `check_impl`)."""
    recv = next((p for p in f.params if p.receiver in ("ref", "mut")), None)
    pnames = {p.name.text for p in f.params if not p.receiver}
    for a in getattr(f, "attrs", []):
        n = a.name.text
        if n not in API_ATTRS:
            continue
        if recv is None:
            d.append(dg.error(a.range, "KIFP215", f"#[{n}] describes a method: it needs a `&self` or `&mut self` receiver"))
            continue
        args = a.args or []
        if n == "when":
            ok = len(args) == 2 and args[0].value is not None and args[0].value.kind == "bool" and args[1].name in _RECV_FACTS \
                and not args[1].args
            if not ok:
                d.append(dg.error(a.range, "KIFP212", "when takes a bool and a fact: `when(true, some)`"))
            elif not (isinstance(f.ret, ast.PathType) and f.ret.text == "bool"):
                d.append(dg.error(a.range, "KIFP214", "when needs a method that returns bool"))
        elif n == "len_of":
            if args:
                d.append(dg.error(a.range, "KIFP212", "len_of takes no arguments"))
            elif not (isinstance(f.ret, ast.PathType) and f.ret.text == "usize") or pnames:
                d.append(dg.error(a.range, "KIFP214", "len_of needs a method with no parameters that returns usize"))
        else:
            fact = args[1] if len(args) == 2 else None
            ok = fact is not None and args[0].name and not args[0].args and (
                fact.name in _RECV_FACTS and not fact.args
                or fact.name == "lt_len" and len(fact.args) == 1 and fact.args[0].name and not fact.args[0].args)
            if not ok:
                d.append(dg.error(a.range, "KIFP212", "unchecked_form takes a method name and a fact: `unchecked_form(unsafe_at, lt_len(i))`"))
            elif fact.name == "lt_len" and fact.args[0].name not in pnames:
                d.append(dg.error(a.range, "KIFP213", f"lt_len({fact.args[0].name}): not a parameter of {f.name.text!r}"))


def check_impl(d: List, it: ast.ExternImpl) -> None:
    """Every `unchecked_form(name, ..)` of an extern `impl` names an `#[unsafe]` method of the same block."""
    unsafe = {f.name.text for f in it.fns if any(a.name.text == "unsafe" for a in f.attrs)}
    for f in it.fns:
        for a in f.attrs:
            if a.name.text == "unchecked_form" and a.args and a.args[0].name and a.args[0].name not in unsafe:
                d.append(dg.error(a.range, "KIFP216", f"unchecked_form({a.args[0].name}): the impl has no `#[unsafe]` method of that name"))


_CHECK_KINDS = ("index", "range", "some", "non_null", "div", "shift")  # the run-time check kinds of ir.CHECK_KINDS


def check_require_proved(d: List, attrs) -> None:
    """`#[require_proved]` / `#[require_proved(index, div)]` (on a function, an `impl` or, as `#![..]`, a file) names only check kinds."""
    for a in attrs or ():
        if a.name.text != "require_proved":
            continue
        for x in a.args or []:
            if not x.name or x.args or x.name not in _CHECK_KINDS:
                d.append(dg.error(a.range, "KIFP217", f"require_proved takes check kinds ({', '.join(_CHECK_KINDS)}), not {x.name or 'a value'!r}"))
        if a.args is None and a.raw:
            d.append(dg.error(a.range, "KIFP217", "require_proved takes check kinds: `require_proved(index, div)`"))


def check_fn(bc, f: ast.FnDecl) -> None:
    """Validate the fact attributes of `f` and of its parameters; reports into `bc.d`."""
    d = bc.d
    check_api(d, f)
    check_require_proved(d, f.attrs)
    params = {p.name.text: p for p in f.params if not p.receiver}
    for p in f.params:
        for a in getattr(p, "attrs", []):
            n = a.name.text
            if n not in PARAM_FACTS:
                continue
            if p.receiver:
                d.append(dg.error(a.range, "KIFP211", f"#[{n}] does not apply to {p.receiver} self"))
                continue
            if not _fact(d, a, n, a.args or [], PARAM_FACTS):
                continue
            k = _kind(p.ty)
            if k != _FITS[n]:
                d.append(dg.error(a.range, "KIFP211", f"#[{n}] on {p.name.text!r} needs {_WANTS[n]}"))
            elif n == "lt_len":
                tgt = a.args[0].name
                q = params.get(tgt)
                if q is None or q is p or _kind(q.ty) not in ("seq", "array"):
                    d.append(dg.error(a.range, "KIFP213", f"lt_len({tgt}): {tgt!r} must be another parameter that is a vector, slice, string or array"))
    for a in getattr(f, "attrs", []):
        n = a.name.text
        if n == "ensures":
            if not a.args or len(a.args) != 1:
                d.append(dg.error(a.range, "KIFP212", "ensures takes one fact: `ensures(some)`"))
                continue
            x = a.args[0]
            if not _fact(d, a, x.name or "", x.args, RESULT_FACTS):
                continue
            if f.ret is None or _kind(f.ret) != _FITS[x.name]:
                d.append(dg.error(a.range, "KIFP214", f"ensures({x.name}) needs a return type that is {_WANTS[x.name]}"))
        elif n == "preserves":
            muts = {p.name.text for p in f.params if not p.receiver and isinstance(p.ty, ast.RefType) and p.ty.mut}
            has_self = any(p.receiver == "mut" for p in f.params)
            for x in a.args or []:
                if not x.name or x.args:
                    d.append(dg.error(a.range, "KIFP212", "preserves takes parameter names: `preserves(v, o)`"))
                elif x.name != "self" and x.name not in muts or x.name == "self" and not has_self:
                    d.append(dg.error(a.range, "KIFP213", f"preserves({x.name}): {x.name!r} is not a `&mut` parameter"))
            if not a.args and not muts and not has_self:
                d.append(dg.error(a.range, "KIFP213", "preserves: the function has no `&mut` parameter"))


# ---------------------------------------------------------------- what the code generators enforce

@dataclass(frozen=True, eq=False)
class Req:
    """A parameter fact that is a precondition: the body may rely on it, and every call the C backend lowers must prove it
    (call-site analysis, docs 4.19a).

    `kind` is the IR check that expresses it: `some` or `non_null` on the parameter, or `index` for a length fact. `n` is the
    constant index of a length fact (`len_ge(N)` needs `N - 1 < len`); `other` is the parameter whose length `lt_len` names."""

    param: ast.Param
    fact: str
    kind: str
    n: Optional[int]
    other: Optional[ast.Param]

    @property
    def tag(self) -> str:
        return f"{self.fact}:{self.param.name.text}"


def _depth(t) -> int:
    n = 0
    while isinstance(t, ast.RefType):
        t, n = t.inner, n + 1
    return n


def _is(t, name: str) -> bool:
    t = _strip(t)
    return isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == name


def length_param(p: ast.Param) -> bool:
    """A parameter whose length the generated code can read: a slice behind one `&`, or a vector (by value or behind one `&`)."""
    if _depth(p.ty) > 1 or p.receiver:
        return False
    return isinstance(_strip(p.ty), ast.SliceType) and _depth(p.ty) == 1 or _is(p.ty, "vector")


def requirements(f: ast.FnDecl) -> List[Req]:
    """The parameter facts of `f` that are enforced: `some` on an optional, `non_null` on a plain `ptr<T>`, `non_empty` / `len_ge(N)`
    on a slice or vector, and `lt_len(v)` on a `usize` against such a `v`. Any other fact (or a fact on a type not listed) is
    only a hint: nothing assumes it, nothing guards it."""
    params = {p.name.text: p for p in f.params if not p.receiver}
    out: List[Req] = []
    for p in f.params:
        if p.receiver:
            continue
        for a in getattr(p, "attrs", []):
            n, args = a.name.text, a.args or []
            if n == "some" and _is(p.ty, "optional") and _depth(p.ty) <= 1:
                out.append(Req(p, n, "some", None, None))
            elif n == "non_null" and _depth(p.ty) == 0 and _is(p.ty, "ptr"):
                out.append(Req(p, n, "non_null", None, None))
            elif n in ("non_empty", "len_ge") and length_param(p):
                k = 1 if n == "non_empty" else (args[0].value.value if len(args) == 1 and args[0].value is not None else 0)
                if k > 0:
                    out.append(Req(p, n, "index", k - 1, None))
            elif n == "lt_len" and len(args) == 1 and args[0].name and _depth(p.ty) == 0 and _is(p.ty, "usize"):
                q = params.get(args[0].name)
                if q is not None and q is not p and length_param(q):
                    out.append(Req(p, n, "index", None, q))
    return out
