# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Tree-walking interpreter for the procedural language (docs/kif-proc-language.md sections 4.3, 5 and 8).

It runs the *checked* AST (`symbols.analyze` annotates every expression with its type in `.ty`) and has exactly the
semantics the C and C++ lowering must have, so the differential tests can compare all three:

* integers are Python ints wrapped to the static type after every operation (`+ - *` wrap, `/ %` truncate towards zero,
  shifts and divisions trap as section 4.3 says); an untyped literal that was never given a type counts as `i64`;
* structs and arrays are values: `#[copy]` structs and arrays are copied when they are read into a `let`, assignment,
  argument, return or field (moves share the object, the checker guarantees the source is dead);
* `&x`, `&mut x`, `&a.b` and `&v[i]` are `Ref`s to the place; field access, indexing and method calls dereference them;
* a trap, a limit or a call that cannot run at generation time raises `Trap` carrying a KIFP code and the source range.

Strings, `Vec`, `Option`, `format!` and reflection (section 5) are the next step; the plain-data subset is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import ast
from . import methods as M
from . import reflect
from . import types as T
from .bodies import BodyChecker
from .reflect import Handle

UNIT: tuple = ()
DEFAULT_STEPS = 10_000_000
DEFAULT_DEPTH = 64
MAX_VEC = 1_000_000
MAX_STR = 16 * 1024 * 1024


class Trap(Exception):
    """A runtime failure: KIFP100 (trap, `panic!`), KIFP101 (not available at generation time), KIFP103 (limits)."""

    def __init__(self, code: str, message: str, rng=None) -> None:
        super().__init__(message)
        self.code, self.message, self.range = code, message, rng


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class Tup(list):
    """A tuple value (a list subclass, so `t.0 = x` and equality work; arrays stay plain lists)."""

    def __repr__(self) -> str:
        return "(" + ", ".join(repr(x) for x in self) + ("," if len(self) == 1 else "") + ")"


class Struct:
    __slots__ = ("key", "name", "fields", "copy")

    def __init__(self, key: str, name: str, fields: Dict[str, Any], copy: bool) -> None:
        self.key, self.name, self.fields, self.copy = key, name, fields, copy

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Struct) and self.key == other.key and self.fields == other.fields

    def __repr__(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in self.fields.items())})"


class EnumVal:
    __slots__ = ("decl", "member", "value")

    def __init__(self, decl: ast.EnumDecl, member: str, value: int) -> None:
        self.decl, self.member, self.value = decl, member, value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnumVal) and self.decl is other.decl and self.member == other.member

    def __hash__(self) -> int:
        return hash((id(self.decl), self.member))

    def __repr__(self) -> str:
        return f"{self.decl.name.text}::{self.member}"


class Ref:
    """A reference to a place: `box[key]` where `box` is a scope, a struct's field dict or a list."""

    __slots__ = ("box", "key")

    def __init__(self, box: Any, key: Any) -> None:
        self.box, self.key = box, key

    def get(self) -> Any:
        return self.box[self.key]

    def set(self, v: Any) -> None:
        self.box[self.key] = v


class VecVal(list):
    """`Vec<T>` (generation time): a growable list that moves instead of copying."""


class Opt:
    """`Option<T>`: `Opt(True, x)` is `Some(x)`, `Opt(False)` is `None`."""

    __slots__ = ("some", "value")

    def __init__(self, some: bool, value: Any = None) -> None:
        self.some, self.value = some, value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Opt) and self.some == other.some and self.value == other.value

    def __repr__(self) -> str:
        return f"Some({self.value!r})" if self.some else "None"


class Res:
    """`result<T>`: `Res(True, x)` is `ok(x)`, `Res(False, e)` is `err(e)`."""

    __slots__ = ("ok", "value")

    def __init__(self, ok: bool, value: Any = None) -> None:
        self.ok, self.value = ok, value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Res) and self.ok == other.ok and self.value == other.value

    def __repr__(self) -> str:
        return f"ok({self.value!r})" if self.ok else f"err({self.value!r})"


class ErrVal:
    """An `error::name` value (reloco's one error enum)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ErrVal) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"error::{self.name}"


@dataclass
class Builtin:
    name: str
    fn: Any


@dataclass
class FnVal:
    decl: ast.FnDecl
    mod: Any
    self_ty: Optional[T.Ty] = None


@dataclass
class Closure:
    lam: ast.Lambda
    mod: Any
    self_ty: Optional[T.Ty]
    env: Any = None
    tenv: Any = None


class Scope:
    __slots__ = ("vars", "parent")

    def __init__(self, parent: Optional["Scope"] = None) -> None:
        self.vars: Dict[str, Any] = {}
        self.parent = parent

    def box_of(self, name: str) -> Optional[dict]:
        s: Optional[Scope] = self
        while s is not None:
            if name in s.vars:
                return s.vars
            s = s.parent
        return None


def wrap(v: int, name: str) -> int:
    lo, hi = T.INT_RANGES[name]
    if lo <= v <= hi:
        return v
    bits = hi - lo + 1
    v = (v - lo) % bits + lo
    return v


def _ity(t: Optional[T.Ty]) -> str:
    return t.name if t is not None and t.name in T.INT_RANGES else "i64"


def _bits(name: str) -> int:
    lo, hi = T.INT_RANGES[name]
    return (hi - lo + 1).bit_length() - 1


def _deref(v: Any) -> Any:
    while isinstance(v, Ref):
        v = v.get()
    return v


def _copy(v: Any) -> Any:
    if isinstance(v, Struct) and v.copy:
        return Struct(v.key, v.name, {k: _copy(x) for k, x in v.fields.items()}, True)
    if type(v) is list:
        return [_copy(x) for x in v]
    if type(v) is Tup:
        return Tup(_copy(x) for x in v)
    return v


class Interp:
    def __init__(self, mod, steps: int = DEFAULT_STEPS, depth: int = DEFAULT_DEPTH) -> None:
        self.mod = mod
        self.max_steps, self.max_depth = steps, depth
        self.steps = 0
        self.depth = 0
        self._chk: Dict[int, BodyChecker] = {}
        self.cur_mod = mod
        self.self_ty: Optional[T.Ty] = None
        self.scope = Scope()
        self._consts: Dict[int, Any] = {}
        self._enum_vals: Dict[int, Dict[str, int]] = {}
        self.tenv: Dict[str, T.Ty] = {}
        self.warnings: List[Tuple[str, Any]] = []

    # -- entry points

    def checker(self, mod=None) -> BodyChecker:
        mod = mod or self.cur_mod
        c = self._chk.get(id(mod))
        if c is None:
            c = BodyChecker(mod)
            c.index = M.build_index(c)
            self._chk[id(mod)] = c
        return c

    def call(self, name: str, *args: Any) -> Any:
        """Call the function `name` of the module with Python values (ints, bools, `Struct`s, lists)."""
        sym = self.mod.values.get(name)
        if sym is None or sym.kind != "fn":
            raise KeyError(f"no function {name!r}")
        return self.invoke(FnVal(getattr(sym.node, "decl", sym.node), sym.module), list(args), None)

    # -- helpers

    def trap(self, msg: str, e=None, code: str = "KIFP100") -> Trap:
        return Trap(code, msg, getattr(e, "range", None))

    def ev(self, e) -> Any:
        self.steps += 1
        if self.steps > self.max_steps:
            raise self.trap(f"step limit of {self.max_steps} exceeded", e, "KIFP103")
        m = getattr(self, "x_" + type(e).__name__, None)
        if m is None:
            raise NotImplementedError(f"the interpreter does not evaluate {type(e).__name__} yet")
        return m(e)

    def val(self, e) -> Any:
        return _copy(self.ev(e))

    def with_ctx(self, mod, self_ty, scope, fn, tenv=None):
        saved = (self.cur_mod, self.self_ty, self.scope, self.tenv)
        self.cur_mod, self.self_ty, self.scope, self.tenv = mod, self_ty, scope, tenv or {}
        try:
            return fn()
        finally:
            self.cur_mod, self.self_ty, self.scope, self.tenv = saved

    def rt(self, t: Optional[T.Ty]) -> Optional[T.Ty]:
        """A type with the type parameters of the running generic call replaced by their arguments."""
        if t is not None and t.cat == "param":
            return self.tenv.get(t.name, t)
        return t

    def ity(self, t: Optional[T.Ty]) -> str:
        return _ity(self.rt(t))

    # -- calls

    def invoke(self, f: Any, args: List[Any], e=None, tenv=None) -> Any:
        f = _deref(f)
        if isinstance(f, Closure):
            scope = Scope(f.env)
            if len(args) != len(f.lam.params):
                raise self.trap("wrong number of arguments", e)
            for p, a in zip(f.lam.params, args):
                scope.vars[p.name.text] = a
            return self.run_body(f.mod, f.self_ty, scope, lambda: self.ev(f.lam.body), f.tenv)
        if isinstance(f, Builtin):
            return f.fn(args, e)
        if not isinstance(f, FnVal):
            raise self.trap("not a function", e)
        decl = f.decl
        if decl.body is None:
            raise self.trap(f"{decl.name.text!r} has no body: extern functions cannot run at generation time", e, "KIFP101")
        if M.has_attr(decl.attrs, "unsafe"):
            raise self.trap(f"{decl.name.text!r} is #[unsafe] and cannot run at generation time", e, "KIFP101")
        scope = Scope()
        if len(args) == len(decl.params) + 1 and not any(p.receiver for p in decl.params):
            scope.vars["self"] = args[0]  # a default method of an interface: `self` is implicit
            args = args[1:]
        if len(args) != len(decl.params):
            raise self.trap(f"{decl.name.text!r} takes {len(decl.params)} argument(s)", e)
        for p, a in zip(decl.params, args):
            scope.vars[p.name.text] = a
        return self.run_body(f.mod, f.self_ty, scope, lambda: self.block_value(decl.body, scope), tenv)

    def run_body(self, mod, self_ty, scope, fn, tenv=None) -> Any:
        self.depth += 1
        if self.depth > self.max_depth:
            self.depth -= 1
            raise self.trap(f"call depth of {self.max_depth} exceeded", None, "KIFP103")
        try:
            return _copy(self.with_ctx(mod, self_ty, scope, fn, tenv))
        except _Return as r:
            return r.value
        finally:
            self.depth -= 1

    def block_value(self, b: ast.Block, scope: Scope) -> Any:
        saved, self.scope = self.scope, scope
        try:
            for s in b.stmts:
                self.stmt(s)
            return self.val(b.tail) if b.tail is not None else UNIT
        finally:
            self.scope = saved

    def x_Block(self, b: ast.Block) -> Any:
        self.steps += 1
        if self.steps > self.max_steps:
            raise self.trap(f"step limit of {self.max_steps} exceeded", b, "KIFP103")
        return self.block_value(b, Scope(self.scope))

    def x_UnsafeBlock(self, e: ast.UnsafeBlock) -> Any:
        return self.x_Block(e.body)

    def x_CfgBlock(self, e: ast.CfgBlock) -> Any:
        from . import cfg

        env = getattr(self.mod, "cfg", None)
        try:
            if env is not None and env.test(cfg.parse_attribute(e.attr)):
                self.x_Block(e.body)
        except cfg.CfgError:
            pass
        return ()

    def stmt(self, s) -> None:
        if isinstance(s, ast.Let):
            if s.init is not None:
                self.scope.vars[s.name.text] = self.val(s.init)
            else:
                self.scope.vars.pop(s.name.text, None)
        elif isinstance(s, ast.LetPat):
            if s.init is not None:
                self.matches(s.pattern, self.val(s.init), self.scope)
        elif isinstance(s, ast.ExprStmt):
            self.ev(s.expr)

    # -- names

    def x_Lit(self, e: ast.Lit) -> Any:
        return e.value

    def x_TupleExpr(self, e: ast.TupleExpr) -> Any:
        return Tup(self.val(x) for x in e.elems) if e.elems else UNIT

    def enum_values(self, decl: ast.EnumDecl) -> Dict[str, int]:
        out = self._enum_vals.get(id(decl))
        if out is None:
            out, nxt = {}, 0
            for m in decl.members:
                if m.value is not None:
                    nxt = self.with_ctx(self.cur_mod, None, Scope(), lambda m=m: self.ev(m.value))
                out[m.name.text] = nxt
                nxt += 1
            self._enum_vals[id(decl)] = out
        return out

    def enum_val(self, decl: ast.EnumDecl, member: str) -> EnumVal:
        return EnumVal(decl, member, self.enum_values(decl)[member])

    def x_PathExpr(self, e: ast.PathExpr) -> Any:
        names = [s.name for s in e.segments]
        chk = self.checker()
        if len(names) == 1:
            box = self.scope.box_of(names[0])
            if box is not None:
                if names[0] not in box:
                    raise self.trap(f"{names[0]!r} is not assigned", e)
                return box[names[0]]
        b = self.builtin_path(names)
        if b is not None:
            return b
        else:
            base = self.self_ty if names[:-1] == ["Self"] else None
            tsym = chk.sym_type("::".join(names[:-1])) if base is None else None
            if tsym is not None and tsym.kind == "enum":
                return self.enum_val(tsym.node, names[-1])
            if base is None and tsym is not None:
                base = chk.ty(ast.PathType(e.range, [ast.PathSeg(e.range, "::".join(names[:-1]))]))
            if base is not None and base.key:
                entry = chk.index.get(base.key, {}).get(names[-1])
                if entry is not None:
                    return FnVal(entry.decl, entry.module, entry.target)
        sym = chk.sym_value(names)
        if sym is None:
            raise self.trap(f"cannot resolve {'::'.join(names)!r}", e, "KIFP101")
        if sym.kind == "fn":
            return FnVal(getattr(sym.node, "decl", sym.node), sym.module)
        if sym.kind == "const":
            if id(sym.node) not in self._consts:
                self._consts[id(sym.node)] = self.with_ctx(sym.module, None, Scope(), lambda: self.val(sym.node.value))
            return _copy(self._consts[id(sym.node)])
        raise self.trap(f"{'::'.join(names)!r} is not a value the interpreter can use", e, "KIFP101")

    def builtin_path(self, names: List[str]) -> Any:
        key = "::".join(names)
        if key == "None":
            return Opt(False)
        if key == "ok":
            return Builtin(key, lambda a, e: Res(True, a[0]))
        if key == "err":
            return Builtin(key, lambda a, e: Res(False, a[0]))
        if len(names) == 2 and names[0] == "error":
            return ErrVal(names[1])
        if key == "Some":
            return Builtin(key, lambda a, e: Opt(True, a[0]))
        if key == "Vec::new":
            return Builtin(key, lambda a, e: VecVal())
        if key == "parse_int":
            return Builtin(key, lambda a, e: self.parse_int(a[0], e))
        return None

    def parse_int(self, s: Any, e) -> int:
        try:
            v = int(s, 0) if isinstance(s, str) and s.strip() == s else None
        except ValueError:
            v = None
        if v is None or not -2**63 <= v < 2**64:
            raise self.trap(f"parse_int: {s!r} is not an integer in range", e)
        return v

    # -- operators

    def x_Unary(self, e: ast.Unary) -> Any:
        if e.op in ("&", "&mut"):
            pl = self.place(e.operand)
            if pl is not None:
                return Ref(*pl)
            return Ref({0: self.ev(e.operand)}, 0)
        v = self.ev(e.operand)
        if e.op == "*":
            if not isinstance(v, Ref):
                raise self.trap("dereference of a raw pointer", e, "KIFP101")
            return v.get()
        v = _deref(v)
        if e.op == "!":
            return (not v) if isinstance(v, bool) else wrap(~v, self.ity(e.ty))
        if e.op == "-":
            return wrap(-v, self.ity(e.ty))
        raise NotImplementedError(f"unary {e.op}")

    def arith(self, op: str, a: Any, b: Any, ty: Optional[T.Ty], e) -> Any:
        if isinstance(a, bool) and isinstance(b, bool):
            return {"&": a and b, "|": a or b, "^": a != b}[op]
        if isinstance(a, str) and op == "+":
            if len(a) + len(b) > MAX_STR:
                raise self.trap("string length limit exceeded", e, "KIFP103")
            return a + b
        n = self.ity(ty)
        if op == "+":
            return wrap(a + b, n)
        if op == "-":
            return wrap(a - b, n)
        if op == "*":
            return wrap(a * b, n)
        if op in ("/", "%"):
            if b == 0:
                raise self.trap("integer division by zero" if op == "/" else "integer remainder by zero", e)
            lo, _ = T.INT_RANGES[n]
            if lo < 0 and a == lo and b == -1:
                raise self.trap("integer overflow in division", e)
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q if op == "/" else a - q * b
        if op == "&":
            return wrap(a & b, n)
        if op == "|":
            return wrap(a | b, n)
        if op == "^":
            return wrap(a ^ b, n)
        if op in ("<<", ">>"):
            if b < 0 or b >= _bits(n):
                raise self.trap(f"shift count {b} is not below {_bits(n)}", e)
            return wrap(a << b, n) if op == "<<" else a >> b
        raise NotImplementedError(op)

    def x_Binary(self, e: ast.Binary) -> Any:
        op = e.op
        if op == "&&":
            return bool(_deref(self.ev(e.left))) and bool(_deref(self.ev(e.right)))
        if op == "||":
            return bool(_deref(self.ev(e.left))) or bool(_deref(self.ev(e.right)))
        a, b = _deref(self.ev(e.left)), _deref(self.ev(e.right))
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op in ("<", "<=", ">", ">="):
            a, b = (a.value, b.value) if isinstance(a, EnumVal) else (a, b)
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return self.arith(op, a, b, e.ty, e)

    def x_Cast(self, e: ast.Cast) -> Any:
        v = _deref(self.ev(e.expr))
        dst = self.rt(e.ty)
        if dst.name in T.INT_RANGES:
            if isinstance(v, EnumVal):
                v = v.value
            return wrap(int(v), dst.name)
        if dst.sym is not None and dst.sym.kind == "enum":
            decl = dst.sym.node
            for name, value in self.enum_values(decl).items():
                if value == v:
                    return EnumVal(decl, name, value)
            raise self.trap(f"{v} is not a value of enum {decl.name.text}", e)
        return v

    # -- places and assignment

    def place(self, e) -> Optional[Tuple[Any, Any]]:
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1:
            box = self.scope.box_of(e.segments[0].name)
            return (box, e.segments[0].name) if box is not None else None
        if isinstance(e, ast.FieldAccess):
            base = _deref(self.ev(e.expr))
            if isinstance(base, Struct):
                return base.fields, e.name.text
            if type(base) is Tup and e.name.text.isdigit() and int(e.name.text) < len(base):
                return base, int(e.name.text)
            raise self.trap(f"no field {e.name.text!r}", e)
        if isinstance(e, ast.Index):
            base = _deref(self.ev(e.expr))
            idx = _deref(self.ev(e.index))
            if not isinstance(base, list) or not isinstance(idx, int):
                raise NotImplementedError("indexing anything but arrays")
            if not 0 <= idx < len(base):
                raise self.trap(f"index out of bounds: the length is {len(base)} but the index is {idx}", e)
            return base, idx
        if isinstance(e, ast.Unary) and e.op == "*":
            r = self.ev(e.operand)
            if isinstance(r, Ref):
                return r.box, r.key
            raise self.trap("dereference of a raw pointer", e, "KIFP101")
        return None

    def x_Assign(self, e: ast.Assign) -> Any:
        v = self.val(e.value)
        pl = self.place(e.target)
        if pl is None:
            raise self.trap("cannot assign here", e)
        box, key = pl
        if e.op == "=":
            box[key] = v
        else:
            box[key] = self.arith(e.op[:-1], _deref(box[key]), _deref(v), e.target.ty, e)
        return UNIT

    def x_FieldAccess(self, e: ast.FieldAccess) -> Any:
        base = _deref(self.ev(e.expr))
        if isinstance(base, Struct) and e.name.text in base.fields:
            return base.fields[e.name.text]
        if type(base) is Tup and e.name.text.isdigit() and int(e.name.text) < len(base):
            return base[int(e.name.text)]
        raise self.trap(f"no field {e.name.text!r}", e)

    def x_Index(self, e: ast.Index) -> Any:
        box, key = self.place(e)
        return box[key]

    def x_StructLit(self, e: ast.StructLit) -> Any:
        t = e.ty
        sym = t.sym
        if sym is None or sym.kind != "struct":
            raise self.trap("unknown struct", e, "KIFP101")
        decl = sym.node
        fields: Dict[str, Any] = {}
        if e.base is not None:
            base = _deref(self.ev(e.base))
            fields.update({k: _copy(v) for k, v in base.fields.items()})
        for f in e.fields:
            fields[f.name.text] = self.val(f.value) if f.value is not None else \
                _copy(self.x_PathExpr(ast.PathExpr(f.name.range, [ast.PathSeg(f.name.range, f.name.text)])))
        for f in decl.fields:
            if f.name.text not in fields and f.default is not None:
                fields[f.name.text] = self.with_ctx(sym.module, None, Scope(), lambda f=f: self.val(f.default))
        ordered = {f.name.text: fields[f.name.text] for f in decl.fields if f.name.text in fields}
        return Struct(t.key, decl.name.text, ordered, M.has_attr(decl.attrs, "copy"))

    def x_ArrayLit(self, e: ast.ArrayLit) -> Any:
        if e.repeat is not None:
            n = _deref(self.ev(e.repeat))
            first = self.val(e.elems[0])
            return [_copy(first) for _ in range(n)]
        return [self.val(x) for x in e.elems]

    # -- calls

    def x_Call(self, e: ast.Call) -> Any:
        if getattr(e, "ct", None):
            return self.ct(e)
        if getattr(e, "spec", None):   # a speculation barrier changes timing, not values
            return self.val(e.args[0]) if e.args else UNIT
        f = self.ev(e.callee)
        targs = {k: self.rt(v) for k, v in getattr(e, "targs", {}).items()}
        return self.invoke(f, [self.val(a) for a in e.args], e, targs)

    def ct(self, e: ast.Call) -> Any:
        """The `ct::` intrinsics (docs/kif-taint.md section 7); the interpreter has no timing, only the values."""
        a = [_deref(self.val(x)) for x in e.args]
        name = e.ct
        if name == "select":
            return a[1] if a[0] else a[2]
        if name == "eq":
            return a[0] == a[1]
        if name == "lt":
            return a[0] < a[1]
        if name == "mask":
            return wrap(-1 if a[0] else 0, "usize")
        return a[0] if a[0] < a[1] else 0   # clamp: out of range gives 0 (the array_index_nospec idiom)

    def x_MethodCall(self, e: ast.MethodCall) -> Any:
        recv = self.ev(e.receiver)
        obj = _deref(recv)
        if isinstance(obj, Handle):
            try:
                return reflect.call(obj, e.name.text, [_deref(self.val(a)) for a in e.args])
            except KeyError:
                raise self.trap(f"{obj.kind} has no accessor {e.name.text!r}", e, "KIFP101") from None
        if isinstance(obj, (str, list, Opt, Res)):
            return self.builtin_method(obj, e.name.text, [self.val(a) for a in e.args], e)
        key = obj.key if isinstance(obj, Struct) else ""
        if not key:
            t = getattr(e.receiver, "ty", None)
            while t is not None and t.name == "&":
                t = t.args[0]
            key = t.key if t is not None else ""
        entry = self.checker().index.get(key, {}).get(e.name.text) if key else None
        if entry is None:
            raise self.trap(f"no method {e.name.text!r} the interpreter can call", e, "KIFP101")
        if entry.receiver == "value":
            self_arg = _copy(obj)
        elif isinstance(recv, Ref):
            self_arg = recv
        else:
            pl = self.place(e.receiver)
            self_arg = Ref(*pl) if pl is not None else Ref({0: recv}, 0)
        args = [self_arg] + [self.val(a) for a in e.args]
        return self.invoke(FnVal(entry.decl, entry.module, entry.target), args, e)

    def builtin_method(self, o: Any, name: str, a: List[Any], e) -> Any:
        a = [_deref(x) for x in a]
        if isinstance(o, str):
            table = {
                "len": lambda: len(o.encode()), "is_empty": lambda: not o, "contains": lambda: a[0] in o,
                "starts_with": lambda: o.startswith(a[0]), "ends_with": lambda: o.endswith(a[0]),
                "replace": lambda: o.replace(a[0], a[1]), "upper": lambda: o.upper(), "lower": lambda: o.lower(),
                "to_string": lambda: o, "view": lambda: o, "trim": lambda: o.strip(), "try_clone": lambda: Res(True, o),
                "split": lambda: VecVal(o.split(a[0])),
                "find": lambda: Opt(True, o.find(a[0])) if a[0] in o else Opt(False),
            }
        elif isinstance(o, Res):
            def rvalue() -> Any:
                if not o.ok:
                    raise self.trap(f"unwrap of {o.value!r}", e)
                return o.value
            table = {"is_ok": lambda: o.ok, "is_err": lambda: not o.ok, "unwrap": rvalue,
                     "unwrap_or": lambda: o.value if o.ok else a[0]}
        elif isinstance(o, Opt):
            def unwrap() -> Any:
                if not o.some:
                    raise self.trap("unwrap of None", e)
                return o.value
            table = {"is_some": lambda: o.some, "is_none": lambda: not o.some, "unwrap": unwrap,
                     "unwrap_or": lambda: o.value if o.some else a[0]}
        else:
            def push() -> Any:
                if len(o) >= MAX_VEC:
                    raise self.trap(f"Vec length limit of {MAX_VEC} exceeded", e, "KIFP103")
                o.append(a[0])
                return UNIT

            def pop() -> Any:
                return Opt(True, o.pop()) if o else Opt(False)

            table = {"len": lambda: len(o), "is_empty": lambda: not o, "push": push if isinstance(o, VecVal) else None,
                     "pop": pop if isinstance(o, VecVal) else None,
                     "join": lambda: a[0].join(self.text(x) for x in o), "contains": lambda: a[0] in o}
        fn = table.get(name)
        if fn is None:
            raise self.trap(f"no method {name!r} on this value", e, "KIFP101")
        return fn()

    def text(self, v: Any, debug: bool = False) -> str:
        v = _deref(v)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' if debug else v
        if isinstance(v, EnumVal):
            return f"{v.decl.name.text}::{v.member}" if debug else v.member
        if isinstance(v, Struct):
            return f"{v.name} {{ " + ", ".join(f"{k}: {self.text(x, True)}" for k, x in v.fields.items()) + " }"
        if isinstance(v, Res):
            return f"{'ok' if v.ok else 'err'}({self.text(v.value, True)})"
        if isinstance(v, ErrVal):
            return repr(v)
        if isinstance(v, Opt):
            return f"Some({self.text(v.value, True)})" if v.some else "None"
        if isinstance(v, Tup):
            return "(" + ", ".join(self.text(x, True) for x in v) + ("," if len(v) == 1 else "") + ")"
        if isinstance(v, Handle):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(self.text(x, True) for x in v) + "]"
        if v == UNIT:
            return "()"
        return str(v)

    def format(self, args: List[Any], e) -> str:
        fmt, vals, out, i, k = args[0], args[1:], [], 0, 0
        while i < len(fmt):
            c = fmt[i]
            if c in "{}" and fmt[i + 1:i + 2] == c:
                out.append(c)
                i += 2
            elif c == "{":
                j = fmt.index("}", i)
                spec = fmt[i + 2:j] if fmt[i + 1:i + 2] == ":" else ""
                v = _deref(vals[k])
                k += 1
                if spec.endswith("?"):
                    out.append(self.text(v, True))
                elif spec.endswith("x"):
                    width = int(spec[:-1] or 0)
                    if isinstance(v, EnumVal):
                        v = v.value
                    out.append(format(v & ((1 << 64) - 1) if v < 0 else v, f"0{width}x" if spec.startswith("0") else f"{width}x"))
                else:
                    out.append(self.text(v))
                i = j + 1
            else:
                out.append(c)
                i += 1
        s = "".join(out)
        if len(s) > MAX_STR:
            raise self.trap("string length limit exceeded", e, "KIFP103")
        return s

    def x_Try(self, e: ast.Try) -> Any:
        v = _deref(self.ev(e.expr))
        if not isinstance(v, Res):
            raise self.trap("'?' needs a result value", e, "KIFP101")
        if v.ok:
            return v.value
        raise _Return(Res(False, v.value))

    def x_Lambda(self, e: ast.Lambda) -> Any:
        return Closure(e, self.cur_mod, self.self_ty, self.scope, self.tenv)

    def x_MacroCall(self, e: ast.MacroCall) -> Any:
        name = e.name.text
        if name in ("panic", "fail", "warn", "format"):
            msg = self.format([self.val(a) for a in e.args], e) if e.args else "explicit panic"
            if name == "format":
                return msg
            if name == "panic":
                raise self.trap(f"panic: {msg}", e)
            if name == "fail":
                raise self.trap(msg, e, "KIFP102")
            self.warnings.append((msg, getattr(e, "range", None)))
            return UNIT
        if name == "unreachable":
            raise self.trap("panic: unreachable", e)
        if name == "untaint" and len(e.args) == 2:
            return self.val(e.args[1])
        raise NotImplementedError(f"{name}!")

    # -- control flow

    def x_If(self, e: ast.If) -> Any:
        if _deref(self.ev(e.cond)):
            return self.x_Block(e.then)
        if e.otherwise is None:
            return UNIT
        return self.ev(e.otherwise)

    def x_While(self, e: ast.While) -> Any:
        while _deref(self.ev(e.cond)):
            try:
                self.x_Block(e.body)
            except _Break:
                break
            except _Continue:
                continue
        return UNIT

    def x_Loop(self, e: ast.Loop) -> Any:
        while True:
            try:
                self.x_Block(e.body)
            except _Break:
                return UNIT
            except _Continue:
                continue

    def x_For(self, e: ast.For) -> Any:
        it = e.iter
        if isinstance(it, ast.RangeExpr):
            lo, hi = _deref(self.ev(it.lo)), _deref(self.ev(it.hi))
            items: Any = range(lo, hi + (1 if it.inclusive else 0))
        else:
            seq = _deref(self.ev(it))
            if not isinstance(seq, list):
                raise NotImplementedError("for over anything but ranges and arrays")
            items = [_copy(x) for x in seq]
        for x in items:
            scope = Scope(self.scope)
            scope.vars[e.var.text] = x
            saved, self.scope = self.scope, scope
            try:
                self.x_Block(e.body)
            except _Break:
                break
            except _Continue:
                continue
            finally:
                self.scope = saved
        return UNIT

    def x_Break(self, e: ast.Break) -> Any:
        raise _Break()

    def x_Continue(self, e: ast.Continue) -> Any:
        raise _Continue()

    def x_Return(self, e: ast.Return) -> Any:
        raise _Return(self.val(e.value) if e.value is not None else UNIT)

    def x_Match(self, e: ast.Match) -> Any:
        v = _deref(self.ev(e.scrutinee))
        for arm in e.arms:
            scope = Scope(self.scope)
            if self.matches(arm.pattern, v, scope):
                saved, self.scope = self.scope, scope
                try:
                    if arm.guard is not None and not self.val(arm.guard):
                        continue
                    return self.val(arm.body)
                finally:
                    self.scope = saved
        raise self.trap("no match arm applies", e)

    def matches(self, p, v: Any, scope: Optional[Scope] = None) -> bool:
        if isinstance(p, ast.WildPat):
            return True
        if isinstance(p, ast.BindPat):
            if p.sub is not None and not self.matches(p.sub, v, scope):
                return False
            if scope is not None:
                scope.vars[p.name.text] = _copy(v)
            return True
        if isinstance(p, ast.TuplePat):
            v = _deref(v)
            return len(v) == len(p.elems) and all(self.matches(a, x, scope) for a, x in zip(p.elems, v))
        if isinstance(p, ast.VariantPat):
            tag = p.path.segments[-1].name
            if isinstance(v, Res):
                if tag != ("ok" if v.ok else "err"):
                    return False
            elif tag != "Some" or not isinstance(v, Opt) or not v.some:
                return False
            return all(self.matches(a, v.value, scope) for a in p.args)
        if isinstance(p, ast.LitPat):
            return p.lit.value == v and type(p.lit.value) is type(v)
        if isinstance(p, ast.RangePat):
            return p.lo.value <= v <= p.hi.value
        if isinstance(p, ast.OrPat):
            return any(self.matches(a, v, scope) for a in p.alts)
        if isinstance(p, ast.PathPat):
            return _deref(self.x_PathExpr(p.path)) == v
        return False


def run(mod, name: str, *args: Any, steps: int = DEFAULT_STEPS, depth: int = DEFAULT_DEPTH) -> Any:
    """Call `name` of an analysed module (see `Interp.call`)."""
    return Interp(mod, steps, depth).call(name, *args)
