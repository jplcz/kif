# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Basic check provers on the mid-level IR (docs/kif-ir.md, section "Proving checks").

`prove_function(fn)` finds the `Check`s that cannot fail and sets `Check.proof` to the reason. Nothing is deleted and no code
generator reads the proofs yet. It is a forward "must" analysis: a fact is known at a point only if it holds on every path that
reaches the point.

Facts are about **locals** (slots), never about temps, because only a slot can change:

* `("some", L)`, `("non_null", L)`: the optional / pointer in `L` holds a value / is not null.
* `("lt", K, bound, c)`: `K + c < bound`, where `K` is an unsigned local and `bound` is `("len", V)` (the element count of local
  `V`) or `("const", n)`. The offset `c` gives `i + 1 < len` and `len - 1` bounds.
* `("len_gt", V, c)`: `len(V) > c`.
* `("len_eq", V, W)`: `len(V) == len(W)` (stored in both orders).
* `("max", L, h)`: `0 <= L <= h` (the slot got a masked or reduced value, or passed a shift check).
* `("eq", L, n)`: `L` holds the constant `n` (constant propagation through slots: `let n = 4; a[n]`).

A fact is learned from a `Test` followed by a `Branch` (on the edge that taken), from a comparison followed by a `Branch`, and from
a `Check` that has passed (a later identical check is then redundant). It is forgotten when the slot is written: a `Store`, a
`Clobber`, or a `Borrow` with `mut` (the borrow rules make that the only way a slot changes, see docs/kif-ir.md).

Not used on purpose: the fact attributes on parameters (`#[some]`, `#[lt_len(v)]`, ...). Their call sites are not verified yet, so
relying on them would be unsound.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from . import ir

_UNSIGNED = ir.UNSIGNED_TYPES

# What a value stands for: ("local", L) the current value of slot L | ("len", L) the element count of slot L | ("const", n) |
# ("range", h) a value known to be in 0..=h (`x & m`, an unsigned `x % k`) | ("add", base, c) `base + c` where base is a "local" or "len" (c may be negative: the arithmetic is checked, so it cannot wrap)
Desc = Tuple
Fact = Tuple
_BITS = {"i8": 8, "i16": 16, "i32": 32, "i64": 64, "isize": 64, "u8": 8, "u16": 16, "u32": 32, "u64": 64, "usize": 64}


class _Path:
    """A field of a slot (`x.n`, `x.a.b`) treated as a slot of its own: it has the facts a slot has, and it is forgotten whenever the
    slot it hangs off is. Reading it twice gives the same value as long as nothing wrote to the root: a write to any part of a
    place is a `Clobber` of its root, and a call that can change it has borrowed the root `mut`."""

    __slots__ = ("parent", "name", "ty")

    def __init__(self, parent, name: str, ty: Optional[str]) -> None:
        self.parent, self.name, self.ty = parent, name, ty or "?"

    def __str__(self) -> str:
        return f"{self.parent}.{self.name}"


# a field of one of these types can change under a shared reference, so a fact about it would be unsound
_SHARED_MUTABLE = ("slot", "cell", "atomic", "volatile")


def _root(x):
    while isinstance(x, _Path):
        x = x.parent
    return x


def _under(x, loc) -> bool:
    """Does `x` mention a field path hanging off `loc`?"""
    if isinstance(x, _Path):
        return _root(x) is loc
    return isinstance(x, tuple) and any(_under(y, loc) for y in x)


# an argument of one of these kinds cannot give the callee a way to write through a reference
_SCALARS = frozenset({"bool", "char", "i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32", "u64", "usize"})


def _inert(ty: Optional[str]) -> bool:
    return ty in _SCALARS or (ty is not None and ty.startswith("&") and not ty.startswith("&mut"))


def _has(x, loc: ir.Local) -> bool:
    if isinstance(x, _Path):
        return _has(x.parent, loc)
    return x is loc or (isinstance(x, tuple) and any(_has(y, loc) for y in x))


class _State:
    """What is known at one program point."""

    def __init__(self) -> None:
        self.facts: Set[Fact] = set()
        self.vals: Dict[ir.Temp, Desc] = {}  # what a temp stands for, while the slot it was read from is unchanged
        self.preds: Dict[ir.Temp, Tuple[frozenset, frozenset]] = {}  # facts when the bool temp is true / false
        self.ens: Dict[ir.Temp, Tuple[str, int]] = {}  # a temp that a call with `#[ensures(..)]` (or `some(x)`) produced: the fact it has (kind, n)

    def copy(self) -> "_State":
        s = _State()
        s.facts, s.vals, s.preds, s.ens = set(self.facts), dict(self.vals), dict(self.preds), dict(self.ens)
        return s

    def kill(self, loc: ir.Local) -> None:
        self.facts = {f for f in self.facts if not _has(f, loc)}
        self.vals = {t: d for t, d in self.vals.items() if not _has(d, loc)}
        self.preds = {t: p for t, p in self.preds.items() if not any(_has(f, loc) for f in p[0] | p[1])}

    def kill_paths(self, loc: ir.Local) -> None:
        """Forget what is known about the fields of `loc`, but not about `loc` itself: a callee that was handed the reference
        may have written through it."""
        self.facts = {f for f in self.facts if not _under(f, loc)}
        self.vals = {t: d for t, d in self.vals.items() if not _under(d, loc)}
        self.preds = {t: p for t, p in self.preds.items() if not any(_under(f, loc) for f in p[0] | p[1])}

    def __eq__(self, other) -> bool:
        return isinstance(other, _State) and (self.facts, self.vals, self.preds, self.ens) == (other.facts, other.vals, other.preds, other.ens)


def _meet(states: List[Optional[_State]]) -> Optional[_State]:
    live = [s for s in states if s is not None]
    if not live:
        return None
    r = live[0].copy()
    for s in live[1:]:
        r.facts &= s.facts
        r.vals = {k: v for k, v in r.vals.items() if s.vals.get(k) == v}
        r.preds = {k: v for k, v in r.preds.items() if s.preds.get(k) == v}
        r.ens = {k: v for k, v in r.ens.items() if s.ens.get(k) == v}
    return r


def split(d: Optional[Desc]):
    """`(base, offset)` of a description: `("add", b, c)` is `(b, c)`, anything else is `(d, 0)`."""
    return (d[1], d[2]) if d is not None and d[0] == "add" else (d, 0)


def add(d: Optional[Desc], c: int) -> Optional[Desc]:
    if d is None:
        return None
    if d[0] == "const":
        return ("const", d[1] + c)
    base, off = split(d)
    return base if off + c == 0 else ("add", base, off + c)


def show(loc, c: int) -> str:
    return f"{loc}" if c == 0 else f"{loc} + {c}" if c > 0 else f"{loc} - {-c}"


def _const(v) -> Optional[int]:
    return v.value if isinstance(v, ir.Const) and isinstance(v.value, int) and not isinstance(v.value, bool) else None


class _Prover:
    def __init__(self, fn: ir.Function, cfg: Optional[ir.CFG] = None) -> None:
        self.fn = fn
        self.cfg = cfg
        self.proved = 0
        self.paths: Dict[Tuple[int, str], _Path] = {}
        self.keep = self.preserved()

    def preserved(self) -> Set[ir.Borrow]:
        """The `mut` borrows that are only handed to a call whose callee `#[preserves]` that operand: the callee keeps the facts about the slot."""
        users: Dict[ir.Temp, List[ir.Instr]] = {}
        for b in self.fn.blocks:
            for i in [*b.instrs, *([b.term] if b.term else [])]:
                for t in i.uses():
                    if isinstance(t, ir.Temp):
                        users.setdefault(t, []).append(i)
        out: Set[ir.Borrow] = set()
        for _, i in self.fn.instrs():
            if isinstance(i, ir.Borrow) and i.mut and len(users.get(i.dst, ())) == 1:
                c = users[i.dst][0]
                if isinstance(c, ir.Call) and c.preserves is not None and self.operand(c, i.dst) in c.preserves:
                    out.add(i)
        return out

    @staticmethod
    def operand(c: ir.Call, t: ir.Temp) -> Optional[int]:
        """-1 when `t` is the receiver of `c`, else its index among `c.args`."""
        return -1 if c.recv is t else next((k for k, a in enumerate(c.args) if a is t), None)

    def path(self, parent, name: str, ty: Optional[str]) -> _Path:
        """One `_Path` object per (parent, field), so facts can compare them by identity like slots."""
        key = (id(parent), name)
        if key not in self.paths:
            self.paths[key] = _Path(parent, name, ty)
        return self.paths[key]

    def desc(self, st: _State, v) -> Optional[Desc]:
        c = _const(v)
        if c is not None:
            return ("const", c)
        return self.resolve(st, st.vals.get(v)) if isinstance(v, ir.Temp) else None

    def resolve(self, st: _State, d: Optional[Desc]) -> Optional[Desc]:
        """A slot known to hold a constant is that constant."""
        base, off = split(d)
        if base is not None and base[0] == "local":
            for f in st.facts:
                if f[0] == "eq" and f[1] is base[1]:
                    return ("const", f[2] + off)
        return d

    def arith(self, op: str, a: Optional[Desc], b: Optional[Desc]) -> Optional[Desc]:
        if a is None or b is None:
            return None
        if a[0] == "const" and b[0] == "const":
            return ("const", a[1] + b[1] if op == "+" else a[1] - b[1])
        if b[0] == "const":
            return add(a, b[1] if op == "+" else -b[1])
        if a[0] == "const" and op == "+":
            return add(b, a[1])
        return None

    # -- facts a comparison gives: lt(x, y) is "x < y", le(x, y) is "x <= y"
    def lt(self, x: Optional[Desc], y: Optional[Desc]) -> frozenset:
        if x is None or y is None:
            return frozenset()
        xb, xo = split(x)
        yb, yo = split(y)
        if xb[0] == "local" and xb[1].ty in _UNSIGNED and yb[0] in ("len", "const"):
            return frozenset({("lt", xb[1], yb, xo - yo)})  # K + xo < base + yo  <=>  K + (xo - yo) < base
        if xb[0] == "const" and yb[0] == "len":
            return frozenset({("len_gt", yb[1], xb[1] - yo)})
        return frozenset()

    def le(self, x: Optional[Desc], y: Optional[Desc]) -> frozenset:
        return self.lt(x, add(y, 1))  # x <= y  <=>  x < y + 1

    @staticmethod
    def positive(x: Optional[Desc], y: Optional[Desc], strict: bool) -> frozenset:
        """`x < y` (`x <= y` when not `strict`) with a constant `x` and a slot `y` makes the slot positive when the constant is at
        least 0 (at least 1 for `<=`)."""
        if x is not None and y is not None and x[0] == "const" and y[0] == "local" and x[1] >= (0 if strict else 1):
            return frozenset({("pos", y[1])})
        return frozenset()

    def compare(self, st: _State, op: str, a, b) -> Optional[Tuple[frozenset, frozenset]]:
        x, y = self.desc(st, a), self.desc(st, b)
        if op == "<":
            return self.lt(x, y) | self.positive(x, y, True), self.le(y, x)
        if op == "<=":
            return self.le(x, y) | self.positive(x, y, False), self.lt(y, x)
        if op == ">":
            return self.lt(y, x) | self.positive(y, x, True), self.le(x, y)
        if op == ">=":
            return self.le(y, x) | self.positive(y, x, False), self.lt(x, y)
        if op in ("==", "!="):
            pair = (x, y) if y == ("const", 0) else (y, x) if x == ("const", 0) else None
            if pair and pair[0] is not None and pair[0][0] == "len":
                nonzero = frozenset({("len_gt", pair[0][1], 0)})
                return (frozenset(), nonzero) if op == "==" else (nonzero, frozenset())
            if x is not None and y is not None and x[0] == "len" and y[0] == "len" and x[1] is not y[1]:
                same = frozenset({("len_eq", x[1], y[1]), ("len_eq", y[1], x[1])})
                return (same, frozenset()) if op == "==" else (frozenset(), same)
            for k, c in ((x, y), (y, x)):
                if k is not None and c is not None and k[0] == "local" and c[0] == "const":
                    same = frozenset({("eq", k[1], c[1])})
                    other = frozenset({("nz", k[1])}) if c[1] == 0 else frozenset()
                    return (same, other) if op == "==" else (other, same)
        return None

    def reduced(self, st: _State, i: ir.Binary) -> Optional[int]:
        """The largest value `x & m` (a constant `m >= 0`, either side, whatever `x` is) or an unsigned `x % k` (a constant
        `k > 0`) can have; the result is never negative."""
        a, b = self.desc(st, i.a), self.desc(st, i.b)
        if i.op == "&":
            for m in (a, b):
                if m is not None and m[0] == "const" and m[1] >= 0:
                    return m[1]
            return None
        if i.dst.ty in _UNSIGNED and b is not None and b[0] == "const" and b[1] > 0:
            return b[1] - 1
        return None

    def ranged(self, st: _State, i: ir.Binary) -> Optional[int]:
        """The largest value of `a + b`, `a * b`, `a >> k` or an unsigned `c - b` when the operands lie in known ranges `0..=h` (see
        `bounds`). The arithmetic is checked, so a result outside the type traps rather than wraps, and an upper bound stays valid."""
        ba, bb = self.bounds(st, self.desc(st, i.a)), self.bounds(st, self.desc(st, i.b))
        if i.op == ">>":
            k = self.desc(st, i.b)
            return ba[0] >> k[1] if ba is not None and k is not None and k[0] == "const" and 0 <= k[1] < 64 else None
        if i.op == "-":
            a = self.desc(st, i.a)
            # only when the subtrahend cannot exceed the constant: no underflow, so no wrap on any target
            return a[1] if i.dst.ty in _UNSIGNED and a is not None and a[0] == "const" and bb is not None and 0 <= bb[0] <= a[1] else None
        if ba is None or bb is None:
            return None
        hi = ba[0] + bb[0] if i.op == "+" else ba[0] * bb[0]
        bits = _BITS.get(i.dst.ty or "")
        return hi if bits is not None and hi < (1 << bits) else None  # a bound beyond the type could wrap

    def bounds(self, st: _State, d: Optional[Desc]) -> Optional[Tuple[int, str]]:
        """`(h, why)` when `d` is known to lie in `0..=h`: a constant, a masked / reduced value, a slot with a `max` fact or an
        upper bound `K + c < m`, or one of those plus a non-negative offset."""
        if d is None:
            return None
        base, off = split(d)
        if off < 0:
            return None  # a subtraction could wrap below zero
        if base[0] == "const":
            return (base[1] + off, f"constant {base[1] + off}") if base[1] + off >= 0 else None
        if base[0] == "range":
            return base[1] + off, f"a value reduced to at most {base[1]}" + (f" plus {off}" if off else "")
        if base[0] == "local":
            for f in st.facts:
                if f[0] == "max" and f[1] is base[1]:
                    return f[2] + off, f"{base[1]} <= {f[2]} is known" + (f", plus {off}" if off else "")
                if f[0] == "lt" and f[1] is base[1] and f[2][0] == "const":
                    return f[2][1] - 1 - f[3] + off, f"{show(base[1], f[3])} < {f[2][1]} is known" + (f", plus {off}" if off else "")
        return None

    # -- one instruction; `mark` is true in the final pass, which records proofs
    def step(self, st: _State, i: ir.Instr, mark: bool) -> None:
        if isinstance(i, ir.Load):
            st.vals[i.dst] = ("local", i.local)
        elif isinstance(i, ir.Access) and i.kind == "field":
            d = st.vals.get(i.base) if isinstance(i.base, ir.Temp) else None
            ty = i.dst.ty or ""
            if d is not None and d[0] == "local" and not ty.startswith(_SHARED_MUTABLE):
                st.vals[i.dst] = ("local", self.path(d[1], i.name, i.dst.ty))
        elif isinstance(i, ir.Call):
            for v in i.uses():  # a reference handed over (a `&mut`, or a struct that may hold one) lets the callee write fields
                d = st.vals.get(v) if isinstance(v, ir.Temp) else None
                if d is not None and d[0] == "local" and not _inert(v.ty) and not (i.preserves is not None and self.operand(i, v) in i.preserves):
                    (st.kill if (v.ty or "").startswith("&mut") else st.kill_paths)(_root(d[1]))  # a `&mut` reborrow may change the referent itself (its length, an option)
            if i.dst is not None and i.ensures is not None:
                st.ens[i.dst] = i.ensures
        elif isinstance(i, ir.Store):
            self.store(st, i.local, i.src)
        elif isinstance(i, ir.Live):
            st.kill(i.local)  # a new binding: nothing known of an earlier trip round a loop applies
        elif isinstance(i, ir.Clobber):
            if i.local is not None and not i.elem:
                st.kill(i.local)
        elif isinstance(i, ir.Borrow):
            if i.mut and i not in self.keep:
                st.kill(i.local)
        elif isinstance(i, ir.Make):
            if i.kind == "some":
                st.ens[i.dst] = ("some", 0)
        elif isinstance(i, ir.Len):
            d = st.vals.get(i.base) if isinstance(i.base, ir.Temp) else None
            if d is not None and d[0] == "local":
                st.vals[i.dst] = ("len", d[1])
        elif isinstance(i, ir.Test):
            d = st.vals.get(i.base) if isinstance(i.base, ir.Temp) else None
            if d is not None and d[0] == "local":
                fact = ("len_gt", d[1], 0) if i.kind == "non_empty" else (i.kind, d[1])
                if i.kind in ("some", "non_null", "non_empty"):
                    st.preds[i.dst] = (frozenset({fact}), frozenset())
        elif isinstance(i, ir.Unary):
            if i.op == "!" and i.a in st.preds:
                pos, neg = st.preds[i.a]
                st.preds[i.dst] = (neg, pos)
        elif isinstance(i, ir.Binary):
            p = self.compare(st, i.op, i.a, i.b)
            if p is not None:
                st.preds[i.dst] = p
            elif i.op in ("+", "-", "*", ">>"):
                d = self.arith(i.op, self.desc(st, i.a), self.desc(st, i.b)) if i.op in ("+", "-") else None
                if d is None:
                    hi = self.ranged(st, i)
                    d = ("range", hi) if hi is not None else None
                if d is not None:
                    st.vals[i.dst] = d
            elif i.op in ("&", "%"):
                hi = self.reduced(st, i)
                if hi is not None:
                    st.vals[i.dst] = ("range", hi)
        elif isinstance(i, ir.Check):
            if mark and i.proof is None:
                i.proof = self.prove(st, i)
                self.proved += i.proof is not None
                if i.proof is None and i.kind == "index":
                    i.refuted = self.refute_index(self.desc(st, i.args[0]), self.desc(st, i.args[1]))
            self.learn(st, i)

    def store(self, st: _State, loc: ir.Local, src) -> None:
        """`loc = src` forgets what was known of `loc`, then learns that it now holds a constant, or is a copy of another slot (the
        copy has every bound the other slot has: `for i in 0..n` copies the hidden counter)."""
        d = self.desc(st, src)
        raw = st.vals.get(src) if isinstance(src, ir.Temp) else None
        copies = []
        if raw is not None and raw[0] == "local" and raw[1] is not loc:
            copies = [f for f in st.facts if f[0] == "lt" and f[1] is raw[1]]
        st.kill(loc)
        e = st.ens.get(src) if isinstance(src, ir.Temp) else None
        if e is not None:  # the value came from a call that guarantees something about it (checked at that function's returns)
            st.facts.add(("len_gt", loc, e[1] - 1) if e[0] == "len_ge" else (e[0], loc))
        if d is not None and d[0] == "const":
            st.facts.add(("eq", loc, d[1]))
        elif d is not None and d[0] == "range":
            st.facts.add(("max", loc, d[1]))
        for f in copies:
            if not _has(f[2], loc):
                st.facts.add(("lt", loc, f[2], f[3]))

    def learn(self, st: _State, c: ir.Check) -> None:
        """A check that did not panic leaves its condition true for the code after it."""
        if c.kind in ("some", "non_null"):
            d = self.desc(st, c.args[0])
            if d is not None and d[0] == "local":
                st.facts.add((c.kind, d[1]))
        elif c.kind == "index":
            st.facts |= self.lt(self.desc(st, c.args[0]), self.desc(st, c.args[1]))
        elif c.kind == "shift" and c.ty in _BITS:
            d = self.desc(st, c.args[0])
            if d is not None and d[0] == "local":
                st.facts.add(("max", d[1], _BITS[c.ty] - 1))
        elif c.kind == "div" and c.ty in _UNSIGNED:  # a signed divisor could still meet MIN / -1 with another dividend
            d = self.desc(st, c.args[1])
            if d is not None and d[0] == "local":
                st.facts.add(("nz", d[1]))

    @staticmethod
    def refute_index(i, n) -> Optional[str]:
        """Why `i < n` can never hold: both are known numbers and `i` is not below `n` (a constant index into a fixed-size array)."""
        if i is not None and n is not None and i[0] == "const" and n[0] == "const" and i[1] >= n[1]:
            return f"{i[1]} is not below the length {n[1]}"
        return None

    def prove(self, st: _State, c: ir.Check) -> Optional[str]:
        if c.kind in ("some", "non_null"):
            e = st.ens.get(c.args[0]) if isinstance(c.args[0], ir.Temp) else None
            if e is not None and e[0] == c.kind:
                return f"{c.kind} is guaranteed by what produced the value (`some(..)` or a call with #[ensures])"
            d = self.desc(st, c.args[0])
            if d is not None and d[0] == "local" and (c.kind, d[1]) in st.facts:
                return f"{c.kind}({d[1]}) is known on every path here"
            return None
        if c.kind == "index":
            return self.prove_index(st, self.desc(st, c.args[0]), self.desc(st, c.args[1]))
        if c.kind == "range":
            return self.prove_range(st, c)
        if c.kind == "div":
            return self.prove_div(st, c)
        if c.kind == "shift":
            return self.prove_shift(st, c)
        return None

    def prove_shift(self, st: _State, c: ir.Check) -> Optional[str]:
        """The amount is known to be in `0..bits`: a constant, a masked or reduced value (`n & 31`, an unsigned `n % 32`), or a
        slot bounded by one of those or by a comparison. A signed amount is covered too: the bound includes `0 <=`."""
        bits = _BITS.get(c.ty or "")
        b = self.bounds(st, self.desc(st, c.args[0])) if bits else None
        if b is not None and b[0] < bits:
            return f"{b[1]}, below {bits} bits"
        return None

    def prove_div(self, st: _State, c: ir.Check) -> Optional[str]:
        """The divisor is a non-zero constant, or a slot known positive or (for an unsigned type) non-zero. A signed type also
        needs the divisor not to be -1, which a positive divisor and a constant other than -1 both give."""
        b = self.desc(st, c.args[1])
        if b is None:
            return None
        signed = c.ty not in _UNSIGNED
        if b[0] == "const":
            if b[1] != 0 and not (signed and b[1] == -1):
                return f"the divisor is the constant {b[1]}"
            return None
        if b[0] == "local":
            if ("pos", b[1]) in st.facts:
                return f"{b[1]} > 0 is known"
            if not signed and ("nz", b[1]) in st.facts:
                return f"{b[1]} != 0 is known"
        if b[0] == "len" and any(f[0] == "len_gt" and f[1] is b[1] and f[2] >= 0 for f in st.facts):
            return f"len({b[1]}) > 0 is known"
        return None

    @staticmethod
    def same_lengths(st: _State, n: Desc) -> List[Desc]:
        """`n` (a `len` description) and every length known to be equal to it."""
        return [n] + [("len", f[2]) for f in st.facts if f[0] == "len_eq" and f[1] is n[1]]

    def prove_index(self, st: _State, i: Optional[Desc], n: Optional[Desc]) -> Optional[str]:
        if i is None or n is None:
            return None
        r = self.index_proof(st, i, n)
        if r is None and n[0] == "len":
            for m in self.same_lengths(st, n)[1:]:
                r = self.index_proof(st, i, m)
                if r is not None:
                    return f"{r}, and len({m[1]}) == len({n[1]})"
        return r

    def index_proof(self, st: _State, i: Desc, n: Desc) -> Optional[str]:
        ib, io = split(i)
        if ib[0] == "const" and n[0] == "const":
            return f"constant index {ib[1]} < {n[1]}" if 0 <= ib[1] < n[1] else None
        if ib[0] == "const" and n[0] == "len":
            if ib[1] >= 0 and any(f[0] == "len_gt" and f[1] is n[1] and f[2] >= ib[1] for f in st.facts):
                return f"len({n[1]}) > {ib[1]} is known"
            return None
        if ib[0] == "len" and n[0] == "len" and ib[1] is n[1] and io < 0:
            # `-` wraps, so `len - k` is only below `len` when the length is at least `k` (`len > k - 1`)
            if any(f[0] == "len_gt" and f[1] is n[1] and f[2] >= -io - 1 for f in st.facts):
                return f"len({n[1]}) > {-io - 1} is known, so len({n[1]}) - {-io} < len({n[1]})"
            return None
        if ib[0] == "local":
            for f in st.facts:
                if f[0] == "lt" and f[1] is ib[1] and f[3] >= io:  # K + io <= K + f[3] < bound
                    if n[0] == "len" and f[2] == n:
                        return f"{show(ib[1], f[3])} < len({n[1]}) is known"
                    if n[0] == "const" and f[2][0] == "const" and f[2][1] <= n[1]:
                        return f"{show(ib[1], f[3])} < {f[2][1]} <= {n[1]} is known"
        if n[0] == "const" and ib[0] in ("range", "local"):
            b = self.bounds(st, i)
            if b is not None and b[0] < n[1]:
                return f"{b[1]}, below length {n[1]}"
        return None

    def prove_range(self, st: _State, c: ir.Check) -> Optional[str]:
        lo, hi, n = c.args
        dl, dh, dn = self.desc(st, lo), self.desc(st, hi), self.desc(st, n)
        if dh is None or dn is None:
            return None
        if not self.lo_ok(st, dl, dh, dn, lo is hi):
            return None
        if hi is n or dh == dn:
            return "the range ends at the length"
        hb, ho = split(dh)
        if hb[0] == "const" and dn[0] == "const":
            return f"constant range ends at {hb[1]} <= {dn[1]}" if 0 <= hb[1] <= dn[1] else None
        if hb[0] == "const" and dn[0] == "len":
            if hb[1] <= 0 or any(f[0] == "len_gt" and f[1] is dn[1] and f[2] >= hb[1] - 1 for f in st.facts):
                return f"len({dn[1]}) >= {hb[1]} is known"
        if hb[0] == "len" and dn[0] == "len" and hb[1] is dn[1] and ho <= 0:
            if ho == 0:
                return "the range ends at or before the length"
            if any(f[0] == "len_gt" and f[1] is dn[1] and f[2] >= -ho - 1 for f in st.facts):  # `-` wraps below zero
                return f"len({dn[1]}) >= {-ho} is known, so the range ends before the length"
            return None
        if hb[0] == "local":
            for f in st.facts:  # hi <= len  <=  K + ho < len
                if f[0] == "lt" and f[1] is hb[1] and f[2] == dn and ho <= f[3] + 1:
                    return f"{show(hb[1], f[3])} < len is known, so the range ends inside it"
        return None

    @staticmethod
    def lo_ok(st: _State, dl: Optional[Desc], dh: Desc, dn: Desc, same: bool) -> bool:
        """`lo <= hi` and `lo >= 0`."""
        if same:
            return True
        if dl is None:
            return False
        if dl[0] == "const":
            return dl[1] == 0 or (dh[0] == "const" and 0 <= dl[1] <= dh[1])
        lb, lo_off = split(dl)
        hb, hi_off = split(dh)
        if lb == hb and lo_off <= hi_off:  # same base, smaller offset (an unsigned slot never goes below zero without a trap)
            return True
        # K + lo_off < bound <= hi (the bound is hi's own base), so lo <= hi
        return lb[0] == "local" and any(f[0] == "lt" and f[1] is lb[1] and f[2] == hb and lo_off <= f[3] + 1 + hi_off for f in st.facts)

    # -- the dataflow
    def run(self) -> int:
        cfg = self.cfg or ir.CFG(self.fn)
        edge: Dict[Tuple[ir.Block, ir.Block], _State] = {}
        entry_in = _State()

        def block_in(b: ir.Block) -> Optional[_State]:
            if b is self.fn.entry:
                return entry_in
            return _meet([edge.get((p, b)) for p in cfg.pred[b]])

        def transfer(b: ir.Block, mark: bool) -> Optional[Dict[ir.Block, _State]]:
            st = block_in(b)
            if st is None:
                return None
            st = st.copy()
            for i in b.instrs:
                self.step(st, i, mark)
            t = b.term
            out: Dict[ir.Block, _State] = {}
            if isinstance(t, ir.Branch):
                pos, neg = st.preds.get(t.cond, (frozenset(), frozenset()))
                for target, extra in ((t.then, pos), (t.otherwise, neg)):
                    s = st.copy()
                    s.facts |= extra
                    out[target] = _meet([out[target], s]) if target in out else s  # both edges to one block: only what both give
            else:
                for target in t.targets() if t is not None else []:
                    out[target] = st
            return out

        changed = True
        while changed:
            changed = False
            for b in cfg.rpo:
                out = transfer(b, False)
                if out is None:
                    continue
                for target, s in out.items():
                    if edge.get((b, target)) != s:
                        edge[(b, target)] = s
                        changed = True
        for b in cfg.rpo:
            transfer(b, True)
        return self.proved


def prove_function(fn: ir.Function, cfg: Optional[ir.CFG] = None) -> int:
    """Set `Check.proof` on every check of `fn` that provably cannot fail; returns how many were newly proved.

    A check that already has a proof keeps it. Checks in blocks that cannot be reached are left alone. `cfg` is the function's
    `ir.CFG` when the caller (the pass manager) already has one."""
    return _Prover(fn, cfg).run()


def prove_module(m: ir.Module) -> int:
    return sum(prove_function(f) for f in m.functions)
