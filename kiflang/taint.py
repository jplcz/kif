# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Taint (docs/kif-taint.md): untrusted data may be computed with, never decided on and never used to address memory.

Sources are `#[tainted]` parameters, `#[tainted]` struct types and fields, and `#[tainted_result]` functions. Taint spreads through
every computation into locals (strong update on a whole assignment, weak on a field or element write, union at a join), and a
tainted value in a sink is an error. A declared place (a field, a `&mut` parameter's referent, a static) never becomes tainted
implicitly: writing tainted data into one that is not declared `#[tainted]` is an error.

* KIFT001  a tainted value decides control flow (`if` / `while` / `for` bound, `match` scrutinee or guard, `&&`, `||`);
* KIFT002  a tainted value is an index, range bound used as an index, array length, divisor, shift count or address;
* KIFT003  a tainted argument (or receiver) for a parameter that is not `#[tainted]`;
* KIFT004  a tainted value stored in a field, referent or static that is not `#[tainted]`;
* KIFT005  a tainted value returned from a function that is neither `#[tainted_result]` nor `#[untaint]`;
* KIFT008  a tainted call target, or a tainted argument of an indirect call (function value, interface method);
* KIFT010  a call of an `#[untaint]` function without `untaint!("reason", f(..))`, or an `untaint!` that is malformed or wraps
  something else; the reason is copied into the generated code;
* KIFT007  a taint attribute that is misplaced, malformed or contradictory.

`#[untaint]` bodies are also checked for purity (`KIFT009`, rule 9; `#[untaint(trusted)]` is skipped).

Not yet (later stages): `?` and element-load barriers, the IR backstop, per-field taint of
locals (a local carries one taint). A call result is tainted when any argument is (or the callee is `#[tainted_result]`) and clean
when the callee is `#[untaint]`.

Every message names the origin of the data and lists the mitigations that apply (rule 11). There is no switch to turn the check off.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, NamedTuple, Optional

from . import ast, astutil
from . import diagnostics as dg

FN_ATTRS = ("tainted_result", "untaint", "trusted")
PARAM_ATTRS = ("tainted",)
TYPE_ATTRS = ("tainted",)
ALL_ATTRS = ("tainted",) + FN_ATTRS


_CLAMP_ATTRS = [SimpleNamespace(name=SimpleNamespace(text="untaint"), raw=[])]


def _trusted(f) -> bool:
    return any(a.name.text == "untaint" and a.raw is not None for a in getattr(f, "attrs", []))


def _has(attrs, name: str) -> bool:
    return any(a.name.text == name for a in attrs)


def _no_args(d: List, a: ast.Attribute) -> bool:
    if a.args or a.raw:
        d.append(dg.error(a.range, "KIFT007", f"#[{a.name.text}] takes no arguments"))
        return False
    return True


# ---------------------------------------------------------------- attribute validation (KIFT007)

def check_type_attrs(d: List, attrs, where: str) -> None:
    """`#[tainted]` on a struct, enum or field takes no arguments; the function attributes do not belong there."""
    for a in attrs:
        n = a.name.text
        if n == "tainted":
            _no_args(d, a)
        elif n in FN_ATTRS:
            d.append(dg.error(a.range, "KIFT007", f"#[{n}] applies to a function, not to {where}"))


def check_fn(d: List, f: ast.FnDecl) -> None:
    """Validate the taint attributes of `f` and of its parameters."""
    attrs = list(getattr(f, "attrs", []))
    tainted_params = False
    for p in f.params:
        for a in getattr(p, "attrs", []):
            n = a.name.text
            if n == "tainted":
                tainted_params = _no_args(d, a) or tainted_params
            elif n in FN_ATTRS:
                d.append(dg.error(a.range, "KIFT007", f"#[{n}] applies to a function, not to parameter {p.name.text!r}"))
    seen = {a.name.text: a for a in attrs}
    for a in attrs:
        n = a.name.text
        if n == "tainted":
            d.append(dg.error(a.range, "KIFT007", "#[tainted] on a function does nothing: put it on a parameter, "
                              "or use #[tainted_result] for the result"))
        elif n == "tainted_result":
            if _no_args(d, a) and f.ret is None:
                d.append(dg.error(a.range, "KIFT007", "#[tainted_result] needs a function that returns a value"))
        elif n == "trusted":
            if _no_args(d, a) and "export" not in seen:
                d.append(dg.error(a.range, "KIFT007", "#[trusted] applies to an #[export] function (it opts the function out of "
                                  "the target's `taint.exports` policy)"))
        elif n == "untaint":
            args = a.args or []
            if a.raw is not None and not (len(args) == 1 and args[0].name == "trusted" and not args[0].args):
                d.append(dg.error(a.range, "KIFT007", "#[untaint] takes no arguments, or `trusted`: `#[untaint(trusted)]`"))
    if "untaint" in seen and "tainted_result" in seen:
        d.append(dg.error(seen["untaint"].range, "KIFT007", "#[untaint] and #[tainted_result] contradict each other"))
    if "untaint" in seen and not tainted_params:
        d.append(dg.error(seen["untaint"].range, "KIFT007", "#[untaint] needs at least one #[tainted] parameter: "
                          "there is nothing to untaint otherwise"))
    if "trusted" in seen and tainted_params:
        d.append(dg.error(seen["trusted"].range, "KIFT007", "#[trusted] contradicts the #[tainted] parameters of the same function"))


# ---------------------------------------------------------------- the body walk

class Origin(NamedTuple):
    what: str   # "parameter 'n'"
    line: int   # 1-based


Taint = Optional[Origin]


class _V(NamedTuple):
    taint: Taint
    ref: bool   # a reference: the taint is the referent's, so dereferencing it is not an address use
    clean_place: bool = False   # a `&mut` parameter whose referent is declared clean: tainted data may not be written through it


def _join(a: Taint, b: Taint) -> Taint:
    return a if a is not None else b


MITIGATIONS = {
    "KIFT001": "mitigations: map the value to a clean code with an #[untaint] function first; compute with a branch-free "
               "select instead of branching",
    "KIFT002": "mitigations: clamp the value with an #[untaint] function (the array_index_nospec idiom); select the element "
               "branch-free; mask the value to a constant size",
    "KIFT003": "mitigations: mark the callee's parameter #[tainted] (its body is then checked for tainted input); sanitise the "
               "value with an #[untaint] function before the call",
    "KIFT004": "mitigations: declare the destination field, static or parameter #[tainted]; keep the value in a local",
    "KIFT005": "mitigations: mark the function #[tainted_result] (or return a #[tainted] type); sanitise before returning",
    "KIFT008": "mitigations: call a concrete function instead of the indirect one; sanitise the value first",
}


def _line(n) -> int:
    return n.range.start.line + 1


def _field_tainted(f) -> bool:
    return _has(f.attrs, "tainted")


DEFAULT_SINKS = frozenset(("div", "rem", "shift"))


def policy(env) -> tuple:
    """`(exports_all, sinks)` from a cfg environment (`taint.exports` and `taint.sinks` of the target); the defaults without one."""
    values = getattr(env, "values", None) or {}
    sinks = values.get("taint_sinks")
    return "all" in values.get("taint_exports", ()), DEFAULT_SINKS if sinks is None else frozenset(sinks)


class _Walk:
    def __init__(self, bc, f) -> None:
        self.bc = bc
        self.f = f
        self.exports_all, self.sinks = policy(getattr(bc.mod, "cfg", None))
        # read by ir.py and taint_ir.py, so the IR check follows the same policy
        f.taint_all = self.all_tainted()
        f.taint_sinks = self.sinks
        self.scopes: List[Dict[str, _V]] = [{}]
        self.seen: set = set()
        self.wrapped: set = set()   # the calls that sit directly inside an `untaint!`

    # -- diagnostics

    def err(self, code: str, rng, origin: Origin, use: str) -> None:
        key = (code, rng.start.line, rng.start.col)
        if key in self.seen:
            return
        self.seen.add(key)
        self.bc.d.append(dg.error(rng, code, f"tainted data ({origin.what}, line {origin.line}) {use}; {MITIGATIONS[code]}"))

    def all_tainted(self) -> bool:
        """`taint.exports: "all"` taints every parameter of an `#[export]` function that is not `#[trusted]`."""
        a = getattr(self.f, "attrs", [])
        return self.exports_all and _has(a, "export") and not _has(a, "trusted")

    def sink(self, t: Taint, rng, code: str, use: str) -> None:
        if t is not None:
            self.err(code, rng, t, use)

    # -- types: structs are the only declared taint carriers

    @staticmethod
    def struct_ty(ty):
        """The struct declaration behind `ty` (through `&` only: a pointer's pointee is a different value), else None."""
        while ty is not None and getattr(ty, "name", "") == "&" and ty.args:
            ty = ty.args[0]
        sym = getattr(ty, "sym", None)
        return sym.node if sym is not None and getattr(ty, "cat", "") == "adt" and sym.kind == "struct" else None

    def struct_of(self, e):
        return self.struct_ty(getattr(e, "ty", None))

    def struct_ast(self, t):
        """The struct declaration a source type names (`T` / `&T` / `&mut T`), for callee parameters, which have no resolved type."""
        while isinstance(t, ast.RefType):
            t = t.inner
        if isinstance(t, ast.PathType) and len(t.segments) == 1:
            sym = self.bc.sym_type(t.segments[0].name)
            if sym is not None and sym.kind == "struct":
                return sym.node
        return None

    @staticmethod
    def type_origin(d) -> Origin:
        return Origin(f"type {d.name.text!r}", _line(d.name))

    @staticmethod
    def any_field(d) -> Taint:
        for f in d.fields:
            if _field_tainted(f):
                return Origin(f"field {d.name.text}.{f.name.text}", _line(f.name))
        return None

    def accepts(self, p) -> bool:
        """Whether a parameter may receive tainted data: declared `#[tainted]`, or of a struct type that carries taint itself."""
        if _has(getattr(p, "attrs", []), "tainted"):
            return True
        d = self.struct_ast(p.ty) if p.ty is not None else None
        return d is not None and (_has(d.attrs, "tainted") or self.any_field(d) is not None)

    # -- scopes and flow

    def find(self, name: str) -> Optional[tuple]:
        for i in range(len(self.scopes) - 1, -1, -1):
            if name in self.scopes[i]:
                return i, self.scopes[i][name]
        return None

    def declare(self, name: str, t: Taint, ref: bool = False, clean_place: bool = False) -> None:
        self.scopes[-1][name] = _V(t, ref, clean_place)

    def update(self, name: str, t: Taint, strong: bool) -> None:
        hit = self.find(name)
        if hit is None:
            return
        i, v = hit
        self.scopes[i][name] = _V(t if strong else _join(v.taint, t), v.ref, v.clean_place)

    def snap(self) -> list:
        return [dict(s) for s in self.scopes]

    def restore(self, s: list) -> None:
        self.scopes = [dict(x) for x in s]

    def merge(self, states: list) -> None:
        out = [dict(x) for x in states[0]]
        for st in states[1:]:
            for i, sc in enumerate(st[:len(out)]):
                for n, v in sc.items():
                    if n in out[i]:
                        out[i][n] = out[i][n]._replace(taint=_join(out[i][n].taint, v.taint))
        self.scopes = out

    def loop(self, body) -> None:
        for _ in range(4):
            before = self.snap()
            body()
            self.merge([before, self.snap()])
            if self.snap() == before:
                return

    # -- entry

    def run(self) -> None:
        for p in self.f.params:
            src = _has(getattr(p, "attrs", []), "tainted") or (self.f.taint_all and not p.receiver)
            o = Origin(f"parameter {p.name.text!r}", _line(p.name)) if src else None
            ref = isinstance(p.ty, ast.RefType)
            self.declare(p.name.text, o, ref, ref and p.ty.mut and not self.accepts(p))
        t = self.block(self.f.body)
        if self.f.body.tail is not None:
            self.returns(self.whole(self.f.body.tail, t), self.f.body.tail.range)

    def returns(self, t: Taint, rng) -> None:
        a = list(getattr(self.f, "attrs", []))
        if _has(a, "tainted_result") or _has(a, "untaint") or self.f.ret is None:
            return
        d = self.struct_ast(self.f.ret)
        if d is not None and (_has(d.attrs, "tainted") or self.any_field(d) is not None):
            return   # the result type carries the taint itself
        self.sink(t, rng, "KIFT005", f"is returned from {self.f.name.text}(), which is neither #[tainted_result] nor #[untaint]")

    # -- statements

    def block(self, b: ast.Block) -> Taint:
        self.scopes.append({})
        try:
            for s in b.stmts:
                self.stmt(s)
            return self.expr(b.tail) if b.tail is not None else None
        finally:
            self.scopes.pop()

    def stmt(self, s) -> None:
        if isinstance(s, ast.Let):
            t = self.expr(s.init) if s.init is not None else None
            self.declare(s.name.text, t, isinstance(s.ty, ast.RefType) or _is_borrow(s.init))
        elif isinstance(s, ast.LetPat):
            t = self.expr(s.init) if s.init is not None else None
            self.bind(s.pattern, t)
        elif isinstance(s, ast.ExprStmt):
            self.expr(s.expr)

    def bind(self, p, t: Taint) -> None:
        if isinstance(p, ast.BindPat):
            self.declare(p.name.text, t)
            if p.sub is not None:
                self.bind(p.sub, t)
        elif isinstance(p, ast.TuplePat):
            for x in p.elems:
                self.bind(x, t)
        elif isinstance(p, ast.VariantPat):
            for x in p.args:
                self.bind(x, t)
        elif isinstance(p, ast.OrPat):
            for x in p.alts:
                self.bind(x, t)

    # -- expressions

    def expr(self, e) -> Taint:
        fn = getattr(self, "e_" + type(e).__name__, None)
        t = fn(e) if fn is not None else None
        if t is None:
            d = self.struct_of(e)
            if d is not None and _has(d.attrs, "tainted"):
                t = self.type_origin(d)   # every value of a #[tainted] type is tainted
        return t

    def whole(self, e, t: Taint) -> Taint:
        """The taint of `e` used as a whole value (a call argument, a returned or stored value): any tainted field counts."""
        d = self.struct_of(e)
        return t if t is not None or d is None else self.any_field(d)

    def arg(self, e) -> Taint:
        return self.whole(e, self.expr(e))

    def all(self, es) -> Taint:
        t: Taint = None
        for e in es:
            t = _join(t, self.expr(e))
        return t

    def e_PathExpr(self, e: ast.PathExpr) -> Taint:
        if len(e.segments) == 1:
            hit = self.find(e.segments[0].name)
            if hit is not None:
                return hit[1].taint
        return None

    def e_Unary(self, e: ast.Unary) -> Taint:
        t = self.expr(e.operand)
        if e.op == "*" and not self.is_ref(e.operand):
            self.sink(t, e.range, "KIFT002", "is dereferenced as an address")
        return t

    def is_ref(self, e) -> bool:
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1:
            hit = self.find(e.segments[0].name)
            return hit is not None and hit[1].ref
        return not isinstance(e, ast.PathExpr)

    def e_Binary(self, e: ast.Binary) -> Taint:
        a, b = self.expr(e.left), self.expr(e.right)
        if e.op in ("&&", "||"):
            self.sink(a, e.left.range, "KIFT001", f"decides the `{e.op}` operand")
            self.sink(b, e.right.range, "KIFT001", f"decides the `{e.op}` operand")
        elif e.op in ("/", "%") and ("div" if e.op == "/" else "rem") in self.sinks:
            self.sink(b, e.right.range, "KIFT002", "is a divisor (variable-latency, and the zero check is a branch)")
        elif e.op in ("<<", ">>") and "shift" in self.sinks:
            self.sink(b, e.right.range, "KIFT002", "is a shift count")
        elif e.op == "*" and "mul" in self.sinks:
            self.sink(a, e.left.range, "KIFT002", "is a multiplication operand (this target's multiplier has variable latency)")
            self.sink(b, e.right.range, "KIFT002", "is a multiplication operand (this target's multiplier has variable latency)")
        return _join(a, b)

    # -- stores (rule 3)

    def e_Assign(self, e: ast.Assign) -> Taint:
        v = self.arg(e.value)
        root, declared = self.target(e.target, v, e.value.range)
        if root is not None and not declared:
            self.update(root, v, isinstance(e.target, ast.PathExpr) and e.op == "=")
        return None

    def static_of(self, name: str):
        if self.find(name) is not None:
            return None
        sym = self.bc.sym_value([name])
        return sym.node if sym is not None and sym.kind == "static" else None

    def target(self, e, v: Taint, rng) -> tuple:
        """`(root local, declared)` of an assignment target. `declared` is true when the destination's own declaration decides
        (a field, a static): then the root local is not updated. Reports KIFT004 for a tainted value stored in a clean place and
        checks the sub-expressions that address it."""
        if isinstance(e, ast.PathExpr):
            if len(e.segments) != 1:
                return None, True
            st = self.static_of(e.segments[0].name)
            if st is not None:
                if not _has(st.attrs, "tainted"):
                    self.sink(v, rng, "KIFT004", f"is stored in the static {e.segments[0].name!r}, which is not #[tainted]")
                return None, True
            return e.segments[0].name, False
        if isinstance(e, ast.FieldAccess):
            d = self.struct_of(e.expr)
            root, declared = self.target(e.expr, v if d is None else None, rng)
            if d is not None:
                f = next((x for x in d.fields if x.name.text == e.name.text), None)
                if f is not None and not _has(d.attrs, "tainted"):
                    if _field_tainted(f):
                        return root, True
                    self.sink(v, rng, "KIFT004", f"is stored in field {d.name.text}.{f.name.text}, which is not #[tainted]")
                    return root, True
                if f is not None:
                    return root, True
            return root, declared
        if isinstance(e, ast.Index):
            self.sink(self.expr(e.index), e.index.range, "KIFT002", "is used as an index")
            return self.through(e.expr, v, rng)
        if isinstance(e, ast.Unary) and e.op == "*":
            t = self.expr(e.operand)
            if not self.is_ref(e.operand):
                self.sink(t, e.range, "KIFT002", "is dereferenced as an address")
            return self.through(e.operand, v, rng)
        self.expr(e)
        return None, False

    def through(self, base, v: Taint, rng) -> tuple:
        """A write through `base` (an element, or a dereference): a `&mut` parameter with a clean referent refuses tainted data."""
        root, declared = self.target(base, v, rng)
        if isinstance(base, ast.PathExpr) and len(base.segments) == 1:
            hit = self.find(base.segments[0].name)
            if hit is not None and hit[1].clean_place:
                self.sink(v, rng, "KIFT004", f"is written through {base.segments[0].name!r}, whose referent is not #[tainted]")
                return root, True
        return root, declared

    def e_Cast(self, e: ast.Cast) -> Taint:
        return self.expr(e.expr)

    # -- calls (rule 7)

    def decl_attrs(self, node) -> list:
        return list(getattr(node, "attrs", [])) + list(getattr(getattr(node, "decl", None), "attrs", []))

    def direct(self, c) -> Optional[object]:
        """The declaration node a direct call target names (a function or an extern), else None."""
        if isinstance(c, ast.PathExpr) and (len(c.segments) > 1 or self.find(c.segments[0].name) is None):
            sym = self.bc.sym_value([x.name for x in c.segments])
            if sym is not None and sym.kind == "fn":
                return sym.node
        return None

    def untaint_attrs(self, call) -> Optional[list]:
        """The attributes of the function a call resolves to, else None."""
        if getattr(call, "ct", None) == "clamp":
            return _CLAMP_ATTRS   # `ct::clamp` is the one `#[untaint(trusted)]` intrinsic
        if isinstance(call, ast.Call):
            node = self.direct(call.callee)
            if node is not None:
                return self.decl_attrs(node)
            d = getattr(call, "decl", None)
            return self.decl_attrs(d) if d is not None else None
        d = getattr(call, "decl", None)
        return self.decl_attrs(d) if d is not None else None

    def check_args(self, name: str, params, exprs, taints) -> None:
        for p, a, t in zip(params, exprs, taints):
            if t is not None and not self.accepts(p):
                self.err("KIFT003", a.range, t, f"is passed to parameter {p.name.text!r} of {name}(), which is not declared #[tainted]")

    def written_by(self, name: str, params, exprs, rng) -> None:
        """A `&mut` argument for a #[tainted] parameter: the callee may store tainted data there, so the local becomes tainted."""
        for p, a in zip(params, exprs):
            if _has(getattr(p, "attrs", []), "tainted") and isinstance(a, ast.Unary) and a.op == "&mut":
                root = a.operand
                while isinstance(root, (ast.FieldAccess, ast.Index)):
                    root = root.expr
                if isinstance(root, ast.PathExpr) and len(root.segments) == 1:
                    self.update(root.segments[0].name, Origin(f"written by {name}()", rng.start.line + 1), False)

    def result_of(self, name: str, attrs: Optional[list], args: Taint, rng) -> Taint:
        if attrs is not None and _has(attrs, "untaint"):
            return None
        if attrs is not None and _has(attrs, "tainted_result"):
            return Origin(f"result of {name}()", rng.start.line + 1)
        return args

    def e_Call(self, e: ast.Call) -> Taint:
        attrs = self.untaint_attrs(e)
        name = e.callee.text if isinstance(e.callee, ast.PathExpr) else "a function value"
        if attrs is not None and _has(attrs, "untaint") and id(e) not in self.wrapped:
            self.bc.d.append(dg.error(e.range, "KIFT010", f"{name}() is an #[untaint] function: a call needs a reason, "
                                      f'written `untaint!("why this data may be treated as clean", {name}(..))`; '
                                      "the reason is copied into the generated code for audit"))
        taints = [self.arg(a) for a in e.args]
        args = None
        for t in taints:
            args = _join(args, t)
        if getattr(e, "ct", None) or getattr(e, "spec", None):   # a compiler-synthesized `ct::` / `spec::` intrinsic: pure, no declaration to check arguments against
            return self.result_of(name, attrs, args, e.range)
        c = e.callee
        node = self.direct(c)
        decl = getattr(node, "decl", node) if node is not None else getattr(e, "decl", None)
        if decl is not None and isinstance(c, ast.PathExpr) and (len(c.segments) > 1 or self.find(c.segments[0].name) is None):
            params = [q for q in decl.params if not q.receiver]
            self.check_args(name, params, e.args, taints)
            self.written_by(name, params, e.args, e.range)
            return self.result_of(name, attrs, args, e.range)
        if isinstance(c, ast.PathExpr) and len(c.segments) == 1 and self.find(c.segments[0].name) is None:
            return args     # `some(x)`, `ok(x)` and the like: construction, not a call
        t = self.expr(c)
        self.sink(t, c.range, "KIFT008", "selects the call target")
        for a, ta in zip(e.args, taints):
            self.sink(ta, a.range, "KIFT008", "is passed through an indirect call (a function value), whose target body cannot be checked")
        return _join(t, args)

    def e_MethodCall(self, e: ast.MethodCall) -> Taint:
        recv = self.arg(e.receiver)
        taints = [self.arg(a) for a in e.args]
        args = recv
        for t in taints:
            args = _join(args, t)
        decl = getattr(e, "decl", None)
        name = e.name.text
        if getattr(e, "dyn", False):
            self.sink(recv, e.receiver.range, "KIFT008", "is the receiver of an interface call, whose target body cannot be checked")
            for a, ta in zip(e.args, taints):
                self.sink(ta, a.range, "KIFT008", "is passed through an interface call, whose target body cannot be checked")
            return args
        attrs = self.decl_attrs(decl) if decl is not None else None
        if attrs is not None and _has(attrs, "untaint") and id(e) not in self.wrapped:
            self.bc.d.append(dg.error(e.range, "KIFT010", f"{name}() is an #[untaint] function: a call needs a reason, "
                                      f'written `untaint!("why this data may be treated as clean", x.{name}(..))`'))
        if decl is not None:
            ps = list(decl.params)
            rp = next((q for q in ps if q.receiver), None)
            if recv is not None and rp is not None and not (_has(rp.attrs, "tainted") or self.recv_carries(e.receiver)):
                self.err("KIFT003", e.receiver.range, recv, f"is the receiver of {name}(), whose `self` is not declared #[tainted]")
            params = [q for q in ps if not q.receiver]
            self.check_args(name, params, e.args, taints)
            self.written_by(name, params, e.args, e.range)
        return self.result_of(name, attrs, args, e.range)

    def recv_carries(self, e) -> bool:
        """The receiver's struct type carries taint itself, so its methods take tainted values by nature."""
        d = self.struct_of(e)
        return d is not None and (_has(d.attrs, "tainted") or self.any_field(d) is not None)

    def e_FieldAccess(self, e: ast.FieldAccess) -> Taint:
        t = self.expr(e.expr)
        d = self.struct_of(e.expr)
        if d is not None:
            f = next((x for x in d.fields if x.name.text == e.name.text), None)
            if f is not None and _field_tainted(f):
                t = _join(t, Origin(f"field {d.name.text}.{f.name.text}", _line(f.name)))
        return t

    def e_Index(self, e: ast.Index) -> Taint:
        base, i = self.expr(e.expr), self.expr(e.index)
        self.sink(i, e.index.range, "KIFT002", "is used as an index or range")
        return base

    def e_Try(self, e: ast.Try) -> Taint:
        return self.expr(e.expr)

    def e_RangeExpr(self, e: ast.RangeExpr) -> Taint:
        return _join(self.expr(e.lo) if e.lo is not None else None, self.expr(e.hi) if e.hi is not None else None)

    def e_StructLit(self, e: ast.StructLit) -> Taint:
        d = self.struct_of(e)
        t: Taint = None
        for f in e.fields:
            v = f.value if f.value is not None else ast.PathExpr(f.name.range, [ast.PathSeg(f.name.range, f.name.text)])
            vt = self.arg(v)
            fd = next((x for x in d.fields if x.name.text == f.name.text), None) if d is not None else None
            if d is None:
                t = _join(t, vt)
            elif fd is not None and not _field_tainted(fd) and not _has(d.attrs, "tainted"):
                self.sink(vt, v.range, "KIFT004", f"initialises field {d.name.text}.{fd.name.text}, which is not #[tainted]")
        if e.base is not None:
            t = _join(t, self.expr(e.base))
        return t

    def e_ArrayLit(self, e: ast.ArrayLit) -> Taint:
        if e.repeat is not None:
            self.sink(self.expr(e.repeat), e.repeat.range, "KIFT002", "is an array length")
        return self.all(e.elems)

    def e_TupleExpr(self, e: ast.TupleExpr) -> Taint:
        return self.all(e.elems)

    def e_MacroCall(self, e: ast.MacroCall) -> Taint:
        if e.name.text == "untaint":
            return self.untaint_macro(e)
        self.all(e.args)
        return None

    def untaint_macro(self, e: ast.MacroCall) -> Taint:
        reason = e.args[0] if len(e.args) == 2 else None
        call = e.args[1] if len(e.args) == 2 else None
        is_call = isinstance(call, (ast.Call, ast.MethodCall))
        if not (isinstance(reason, ast.Lit) and reason.kind == "str" and reason.value.strip()):
            self.bc.d.append(dg.error(e.range, "KIFT010", 'untaint! takes a non-empty reason string and a call: '
                                      '`untaint!("why this data may be treated as clean", f(x))`'))
            self.wrapped.update(id(a) for a in e.args if isinstance(a, (ast.Call, ast.MethodCall)))   # one error is enough
            t = None   # a malformed `untaint!` never launders: the result stays as tainted as the arguments
            for a in e.args:
                t = _join(t, self.all(a.args) if isinstance(a, (ast.Call, ast.MethodCall)) else self.expr(a))
            return t
        attrs = self.untaint_attrs(call) if is_call else None
        if attrs is None or not _has(attrs, "untaint"):
            self.bc.d.append(dg.error(e.range, "KIFT010", "untaint! wraps a direct call of an #[untaint] function"))
            return self.expr(call)
        self.wrapped.add(id(call))
        return self.expr(call)

    def e_Block(self, e: ast.Block) -> Taint:
        return self.block(e)

    def e_UnsafeBlock(self, e: ast.UnsafeBlock) -> Taint:
        return self.block(e.body)

    def e_CfgBlock(self, e: ast.CfgBlock) -> Taint:
        self.block(e.body)
        return None

    def e_If(self, e: ast.If) -> Taint:
        self.sink(self.expr(e.cond), e.cond.range, "KIFT001", "decides a branch condition")
        start = self.snap()
        t = self.block(e.then)
        outs = [self.snap()]
        self.restore(start)
        if e.otherwise is not None:
            t = _join(t, self.expr(e.otherwise))
        outs.append(self.snap())
        self.merge(outs)
        return t

    def e_While(self, e: ast.While) -> Taint:
        def body() -> None:
            self.sink(self.expr(e.cond), e.cond.range, "KIFT001", "decides a loop condition")
            self.block(e.body)

        self.loop(body)
        return None

    def e_Loop(self, e: ast.Loop) -> Taint:
        self.loop(lambda: self.block(e.body))
        return None

    def e_For(self, e: ast.For) -> Taint:
        it = self.expr(e.iter)
        self.sink(it, e.iter.range, "KIFT001", "decides how many times a loop runs")

        def body() -> None:
            self.scopes.append({e.var.text: _V(it, False)})
            try:
                self.block(e.body)
            finally:
                self.scopes.pop()

        self.loop(body)
        return None

    def e_Match(self, e: ast.Match) -> Taint:
        s = self.expr(e.scrutinee)
        self.sink(s, e.scrutinee.range, "KIFT001", "decides which `match` arm runs")
        start = self.snap()
        outs, t = [], None
        for arm in e.arms:
            self.restore(start)
            self.scopes.append({})
            try:
                self.bind(arm.pattern, s)
                if arm.guard is not None:
                    self.sink(self.expr(arm.guard), arm.guard.range, "KIFT001", "decides a `match` guard")
                t = _join(t, self.expr(arm.body))
            finally:
                self.scopes.pop()
            outs.append(self.snap())
        if outs:
            self.merge(outs)
        else:
            self.restore(start)
        return t

    def e_Return(self, e: ast.Return) -> Taint:
        if e.value is not None:
            self.returns(self.arg(e.value), e.value.range)
        return None


def _is_borrow(e) -> bool:
    return isinstance(e, ast.Unary) and e.op in ("&", "&mut")


PURE_NAMESPACES = ("ct", "spec")   # intrinsic namespaces a pure sanitizer may call (docs/kif-taint.md section 7)

PURITY_FIX = ("mitigations: return the computed value instead of storing it; move the effect to the caller; "
              "use #[untaint(trusted)] for a platform primitive")


def _purity(bc, f) -> None:
    """KIFT009: an `#[untaint]` function with a body is a verifier and a speculation barrier, nothing else (rule 9)."""
    w = _Walk(bc, f)
    refs = {p.name.text for p in f.params if isinstance(p.ty, ast.RefType)}
    seen = set()

    def bad(n, what: str) -> None:
        if id(n) not in seen:
            seen.add(id(n))
            bc.d.append(dg.error(n.range, "KIFT009", f"#[untaint] function {f.name.text!r} must be pure: {what}; {PURITY_FIX}"))

    for p in f.params:
        if isinstance(p.ty, ast.RefType) and p.ty.mut:
            bad(p.name, f"parameter {p.name.text!r} is a `&mut` reference")
    for n in astutil.walk(f.body):
        if isinstance(n, ast.MacroCall):
            if n.name.text != "untaint":
                bad(n, f"`{n.name.text}!` has a side effect or a panic")
        elif isinstance(n, ast.UnsafeBlock):
            bad(n, "it contains `unsafe`")
        elif isinstance(n, ast.Lambda):
            bad(n, "it contains a lambda")
        elif isinstance(n, ast.Assign):
            t = n.target
            while isinstance(t, (ast.FieldAccess, ast.Index)):
                t = t.expr
            name = t.segments[0].name if isinstance(t, ast.PathExpr) and len(t.segments) == 1 else None
            if name is None:
                bad(n, "it stores through a reference")
            elif w.static_of(name) is not None:
                bad(n, f"it writes the static {name!r}")
            elif name in refs:
                bad(n, f"it writes through parameter {name!r}")
    for n in astutil.walk(f.body):
        if not isinstance(n, (ast.Call, ast.MethodCall)):
            continue
        if isinstance(n, ast.Call) and isinstance(n.callee, ast.PathExpr) and len(n.callee.segments) > 1 \
                and n.callee.segments[0].name in PURE_NAMESPACES:
            continue
        attrs = w.untaint_attrs(n)
        if attrs is None or not _has(attrs, "untaint"):
            bad(n, "it calls a function that is not #[untaint]")


SUGGEST = ("this #[untaint] function computes by hand: a C compiler may turn a hand-built mask back into a branch, and the "
           "result is not behind a speculation barrier. Prefer the `ct::` intrinsics (`ct::select`, `ct::lt`, `ct::eq`, "
           "`ct::mask`, `ct::clamp`), which the code generator keeps branch-free and wraps in the value barrier, and put "
           "`spec::value(..)` on the result (docs/kif-taint.md section 7)")


def _suggest(bc, f) -> None:
    """KIFT011 (a suggestion, never an error): an `#[untaint]` body that builds its result from comparisons or masks."""
    by_hand = False
    for n in astutil.walk(f.body):
        if isinstance(n, ast.Call) and (getattr(n, "ct", None) or getattr(n, "spec", None)):
            return
        if isinstance(n, ast.Binary) and n.op in ("<", "<=", ">", ">=", "==", "!="):
            by_hand = True
        elif isinstance(n, ast.Cast) and getattr(n.expr, "ty", None) is not None and n.expr.ty.name == "bool":
            by_hand = True
        elif isinstance(n, ast.Binary) and n.op in ("&", "|", "^", "*") and any(
                isinstance(x, ast.Unary) and x.op in ("-", "!") for x in (n.left, n.right)):
            by_hand = True
    if by_hand:
        bc.d.append(dg.info(f.name.range, "KIFT011", SUGGEST))


def check_body(bc, f) -> None:
    """Run the walk over the body of `f` (called by the body checker, after the function was type-checked)."""
    if getattr(f, "body", None) is not None:
        _Walk(bc, f).run()
        if _has(getattr(f, "attrs", []), "untaint") and not _trusted(f):
            _purity(bc, f)
            _suggest(bc, f)
