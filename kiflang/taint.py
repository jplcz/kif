# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Taint (docs/kif-taint.md): untrusted data may be computed with, never decided on and never used to address memory.

Stage 1 covers what a function body can see by itself: `#[tainted]` parameters are the sources, taint spreads through every
computation into locals (strong update on a whole assignment, weak on a field or element write, union at a join), and a tainted
value in a sink is an error.

* KIFT001  a tainted value decides control flow (`if` / `while` / `for` bound, `match` scrutinee or guard, `&&`, `||`);
* KIFT002  a tainted value is an index, range bound used as an index, array length, divisor, shift count, call target or address;
* KIFT010  a call of an `#[untaint]` function without `untaint!("reason", f(..))`, or an `untaint!` that is malformed or wraps
  something else; the reason is copied into the generated code;
* KIFT007  a taint attribute that is misplaced, malformed or contradictory.

Not yet (later stages): `KIFT003`-`KIFT005` and `KIFT008` (calls, declared places, returns), type and field sources, the `#[untaint]`
purity check `KIFT009`, `?` and element-load barriers, the IR backstop. Until stage 2 a call result is tainted when any argument is
(or the callee is `#[tainted_result]`) and clean when the callee is `#[untaint]`, but a tainted argument is not yet refused.

Every message names the origin of the data and lists the mitigations that apply (rule 11). There is no switch to turn the check off.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional

from . import ast
from . import diagnostics as dg

FN_ATTRS = ("tainted_result", "untaint", "trusted")
PARAM_ATTRS = ("tainted",)
TYPE_ATTRS = ("tainted",)
ALL_ATTRS = ("tainted",) + FN_ATTRS


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


def _join(a: Taint, b: Taint) -> Taint:
    return a if a is not None else b


MITIGATIONS = {
    "KIFT001": "mitigations: map the value to a clean code with an #[untaint] function first; compute with a branch-free "
               "select instead of branching",
    "KIFT002": "mitigations: clamp the value with an #[untaint] function (the array_index_nospec idiom); select the element "
               "branch-free; mask the value to a constant size",
}


class _Walk:
    def __init__(self, bc, f) -> None:
        self.bc = bc
        self.f = f
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

    def sink(self, t: Taint, rng, code: str, use: str) -> None:
        if t is not None:
            self.err(code, rng, t, use)

    # -- scopes and flow

    def find(self, name: str) -> Optional[tuple]:
        for i in range(len(self.scopes) - 1, -1, -1):
            if name in self.scopes[i]:
                return i, self.scopes[i][name]
        return None

    def declare(self, name: str, t: Taint, ref: bool = False) -> None:
        self.scopes[-1][name] = _V(t, ref)

    def update(self, name: str, t: Taint, strong: bool) -> None:
        hit = self.find(name)
        if hit is None:
            return
        i, v = hit
        self.scopes[i][name] = _V(t if strong else _join(v.taint, t), v.ref)

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
                        out[i][n] = _V(_join(out[i][n].taint, v.taint), out[i][n].ref)
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
            if _has(getattr(p, "attrs", []), "tainted"):
                o = Origin(f"parameter {p.name.text!r}", p.name.range.start.line + 1)
                self.declare(p.name.text, o, isinstance(p.ty, ast.RefType))
            else:
                self.declare(p.name.text, None, isinstance(p.ty, ast.RefType))
        self.block(self.f.body)

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
        return fn(e) if fn is not None else None

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
        elif e.op in ("/", "%"):
            self.sink(b, e.right.range, "KIFT002", "is a divisor (variable-latency, and the zero check is a branch)")
        elif e.op in ("<<", ">>"):
            self.sink(b, e.right.range, "KIFT002", "is a shift count")
        return _join(a, b)

    def e_Assign(self, e: ast.Assign) -> Taint:
        v = self.expr(e.value)
        root = self.target(e.target)
        if root is not None:
            whole = isinstance(e.target, ast.PathExpr) and e.op == "="
            self.update(root, v, whole)
        return None

    def target(self, e) -> Optional[str]:
        """The local at the root of an assignment target; the sub-expressions that address it are checked as sinks."""
        if isinstance(e, ast.PathExpr):
            return e.segments[0].name if len(e.segments) == 1 else None
        if isinstance(e, ast.FieldAccess):
            return self.target(e.expr)
        if isinstance(e, ast.Index):
            self.sink(self.expr(e.index), e.index.range, "KIFT002", "is used as an index")
            return self.target(e.expr)
        if isinstance(e, ast.Unary) and e.op == "*":
            t = self.expr(e.operand)
            if not self.is_ref(e.operand):
                self.sink(t, e.range, "KIFT002", "is dereferenced as an address")
            return self.target(e.operand)
        self.expr(e)
        return None

    def e_Cast(self, e: ast.Cast) -> Taint:
        return self.expr(e.expr)

    def untaint_attrs(self, c) -> Optional[list]:
        """The attributes of the function `c` names when it is a direct call target, else None."""
        if isinstance(c, ast.PathExpr) and (len(c.segments) > 1 or self.find(c.segments[0].name) is None):
            sym = self.bc.sym_value([x.name for x in c.segments])
            if sym is not None and sym.kind == "fn":
                return list(getattr(sym.node, "attrs", []))
        return None

    def e_Call(self, e: ast.Call) -> Taint:
        attrs = self.untaint_attrs(e.callee)
        if attrs is not None and _has(attrs, "untaint") and id(e) not in self.wrapped:
            self.bc.d.append(dg.error(e.range, "KIFT010", f"{e.callee.text}() is an #[untaint] function: a call needs a reason, "
                                      f'written `untaint!("why this data may be treated as clean", {e.callee.text}(..))`; '
                                      "the reason is copied into the generated code for audit"))
        args = self.all(e.args)
        c = e.callee
        if isinstance(c, ast.PathExpr) and len(c.segments) == 1 and self.find(c.segments[0].name) is None or \
                isinstance(c, ast.PathExpr) and len(c.segments) > 1:
            sym = self.bc.sym_value([x.name for x in c.segments])
            attrs = getattr(sym.node, "attrs", []) if sym is not None and sym.kind == "fn" else []
            if _has(attrs, "untaint"):
                return None
            return Origin(f"result of {c.text}()", e.range.start.line + 1) if _has(attrs, "tainted_result") else args
        t = self.expr(c)
        self.sink(t, c.range, "KIFT002", "selects the call target")
        return _join(t, args)

    def e_MethodCall(self, e: ast.MethodCall) -> Taint:
        return _join(self.expr(e.receiver), self.all(e.args))

    def e_FieldAccess(self, e: ast.FieldAccess) -> Taint:
        return self.expr(e.expr)

    def e_Index(self, e: ast.Index) -> Taint:
        base, i = self.expr(e.expr), self.expr(e.index)
        self.sink(i, e.index.range, "KIFT002", "is used as an index or range")
        return base

    def e_Try(self, e: ast.Try) -> Taint:
        return self.expr(e.expr)

    def e_RangeExpr(self, e: ast.RangeExpr) -> Taint:
        return _join(self.expr(e.lo) if e.lo is not None else None, self.expr(e.hi) if e.hi is not None else None)

    def e_StructLit(self, e: ast.StructLit) -> Taint:
        t = self.all([f.value if f.value is not None else ast.PathExpr(f.name.range, [ast.PathSeg(f.name.range, f.name.text)])
                      for f in e.fields])
        return _join(t, self.expr(e.base) if e.base is not None else None)

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
        if not (isinstance(reason, ast.Lit) and reason.kind == "str" and reason.value.strip()):
            self.bc.d.append(dg.error(e.range, "KIFT010", 'untaint! takes a non-empty reason string and a call: '
                                      '`untaint!("why this data may be treated as clean", f(x))`'))
            self.wrapped.update(id(a) for a in e.args if isinstance(a, ast.Call))   # one error is enough
            t = None   # a malformed `untaint!` never launders: the result stays as tainted as the arguments
            for a in e.args:
                t = _join(t, self.all(a.args) if isinstance(a, ast.Call) else self.expr(a))
            return t
        attrs = self.untaint_attrs(call.callee) if isinstance(call, ast.Call) else None
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
            self.expr(e.value)
        return None


def _is_borrow(e) -> bool:
    return isinstance(e, ast.Unary) and e.op in ("&", "&mut")


def check_body(bc, f) -> None:
    """Run the walk over the body of `f` (called by the body checker, after the function was type-checked)."""
    if getattr(f, "body", None) is not None:
        _Walk(bc, f).run()
