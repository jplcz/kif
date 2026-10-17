# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Checker steps 2 and 3: scopes and types inside function bodies.

Walks every `fn`, `method` default, `before`/`after` hook and `const` initialiser with lexical scopes (a block, a `for`
variable, a lambda) and reports:

* KIFP010 a value name that is not a local, parameter, item in scope, enumerator path or imported name;
* KIFP020 assignment to (or `&mut` of) an immutable local: `x = ..`, `x += ..`, `&mut x`. `let x;` may be assigned once
  (definite assignment is a later step). Fields and indexes need borrow information and are not examined yet;
* KIFP170 a lambda uses a local of the enclosing function (lambdas do not capture, section 4.2);
* KIF010 a duplicate lambda parameter.

Every expression also gets a type (types.py), checked gradually: only plain data (integers, `bool`, `char`, `()`, kif
structs and enums that are not generic, references to those) is strict; everything else is opaque and accepted.

* KIFP011 a call with the wrong number or type of arguments;
* KIFP012 a type mismatch: `let`, assignment, argument, return, branch, operator operands, non-`bool` conditions, an index
  that is not `usize`, a field of the wrong type;
* KIFP013 an integer literal that does not fit its type;
* KIFP014 a struct literal with a missing, unknown or repeated field;
* KIFP031 a non-unit function whose body can fall off the end.

Step 5 follows values through the control flow (a path-sensitive pass over the same walk):

* KIFP023 use of a moved (or partly moved) value; structs and `extern "C++"` types without `#[copy]` move when they are
  passed, assigned, returned or stored. References and `#[copy]` types never move. A move inside a loop body is a move on
  the next iteration too;
* KIFP024 move out of a reference, an index or a dereference, or out of a field of a struct that has a `drop`;
* KIFP022 use of a `let x;` that is not assigned on every path.

Step 6 tracks borrows. Every `&x`/`&mut x` (and every method call that auto-borrows its receiver) creates a *loan* on the
local `x`; a variable that holds a reference (or a struct holding one) carries the loans it was derived from, through
`let`, assignment, struct literals and calls (a result takes the loans of the arguments whose lifetime it names, with the
elision rules of section 4.2). A conflicting access to `x` (a write, a move, a `&mut`, or any access while a `&mut` loan
exists) is recorded as flow state; the loan is only an error when the reference is used afterwards, which is what Rust's
non-lexical lifetimes amount to for straight-line code:

* KIFP021 a reference outlives its referent: returned or stored in a parameter while borrowing a local, or used after the
  block that declares the referent ended;
* KIFP033 a reference is used after the value it borrows was modified, moved or borrowed incompatibly; also two
  conflicting borrows among the arguments of one call.

* KIFP110-113 atomic context (section 4.13): sleepy calls while atomic, `#[atomic_only]` outside it, unbalanced
  `#[enter_atomic]`/`#[leave_atomic]`, an `#[atomic_scope]` guard moved out of its block;
* KIFP131 a `#[unsafe]` function or method called outside `unsafe`; KIFP070 a C function that is not `#[safe]` (or any
  variadic one) called, or a `c_ptr`/`c_cptr` dereferenced, outside `unsafe`. A `#[unsafe]` function's own body is unsafe;
* KIFP032 recursion in a `fn` or `method` (direct or mutual; `gen fn` may recurse);
* KIFP030 a non-exhaustive `match` (enumerators, `bool`, integer ranges, otherwise a `_` arm).

Loop-carried loans, borrows through `if`/`match`/block results and calls to associated functions are not tracked (no
false errors, but they are missed).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from . import ast
from . import cells
from . import cfg
from . import places
from . import taint
from . import cmode
from . import diagnostics as dg
from . import methods as M
from . import types as T
from .types import BOOL, INTLIT, NEVER, STRLIT, UNIT, UNKNOWN, Ty

# Names that are always valid as the first segment of a path or as a value.
_KEYWORD_PATHS = frozenset({"Self", "self"})
# Generation-time built-ins of section 5; the checker leaves their types open, the interpreter implements them.
_GEN_NAMES = frozenset({"Some", "None", "Vec", "Option", "parse_int"})
# `ok(..)` and `err(..)` build the type marked `#[lang(result)]` (reloco.kif).
_RESULT_NAMES = frozenset({"ok", "err"})
CT_ARITY = {"select": 3, "eq": 2, "lt": 2, "mask": 1, "clamp": 2}   # compiler-synthesized `ct::` intrinsics (docs/kif-taint.md section 7)
# `some(x)` and `none()` build the type marked `#[lang(optional)]` (reloco.kif).
_OPTIONAL_NAMES = frozenset({"some", "none"})


def is_result(t: Ty) -> bool:
    return t.name == "result"


def lang_result(sym) -> bool:
    return any(a.name.text == "lang" and a.args and a.args[0].name == "result" for a in getattr(sym.node, "attrs", ()))

_SPEC = re.compile(r"0?\d*[x?]?")


def format_placeholders(text: str) -> int:
    """Count the `{}` / `{:x}` / `{:08x}` / `{:?}` placeholders of a format string (`{{` and `}}` are literal braces)."""
    n, i = 0, 0
    while i < len(text):
        c = text[i]
        if c == "{" and text[i + 1:i + 2] == "{":
            i += 2
        elif c == "}" and text[i + 1:i + 2] == "}":
            i += 2
        elif c == "{":
            j = text.find("}", i)
            if j < 0:
                raise ValueError("unterminated '{' in the format string")
            body = text[i + 1:j]
            if body and not (body.startswith(":") and _SPEC.fullmatch(body[1:])):
                raise ValueError(f"unsupported placeholder {{{body}}}: use {{}}, {{:x}}, {{:08x}} or {{:?}}")
            n += 1
            i = j + 1
        elif c == "}":
            raise ValueError("unmatched '}' in the format string")
        else:
            i += 1
    return n


_ARITH = frozenset({"+", "-", "*", "/", "%"})
_BITS = frozenset({"&", "|", "^"})
_SHIFT = frozenset({"<<", ">>"})
_CMP = frozenset({"==", "!=", "<", "<=", ">", ">="})
_LOGIC = frozenset({"&&", "||"})


@dataclass
class _Var:
    mut: bool
    ty: Ty = UNKNOWN
    deferred: bool = False  # `let x;`: initialised by a later assignment
    init: bool = True  # definitely assigned
    moved: bool = False  # possibly moved
    moved_fields: frozenset = frozenset()  # possibly moved field paths (`a.b`)
    name: str = ""
    depth: int = 0  # index of the declaring frame
    end: object = None  # end position of the declaring block
    param: bool = False
    loans: tuple = ()  # loans the value of this variable derives from
    lent: list = field(default_factory=list)  # loans taken on this variable
    hits: tuple = ()  # (loan, range) accesses that conflict with a loan on this variable
    guard: bool = False  # holds an `#[atomic_scope]` value


@dataclass(eq=False)
class _Loan:
    root: "_Var"
    mut: bool
    rng: object
    path: str = ""  # the part of the variable borrowed (kiflang/places.py); empty: all of it
    idx: Optional[int] = None  # the constant index of a borrowed element


@dataclass
class _Frame:
    vars: Dict[str, _Var] = field(default_factory=dict)
    lambda_boundary: bool = False
    end: object = None
    guards: int = 0  # `#[atomic_scope]` locals declared in this block


class BodyChecker:
    def __init__(self, mod) -> None:
        self.mod = mod
        self.d = mod.diagnostics
        self.frames: List[_Frame] = []
        self.generics: List[set] = []
        self.param_ty: Dict[str, Ty] = {}  # type parameters of the generic fn being checked or called (4.9)
        self.ret: Optional[Ty] = None  # the return type being checked; None when not known (hooks, untyped lambdas)
        self.loops: List[List[bool]] = []  # per enclosing loop: [saw `break`]
        self.depth = 0
        self.dead = False  # the current point is unreachable (after `return`, `break`, `panic!`)
        self.read_at: Optional[tuple] = None  # the part (path, index) of a local that the place expression being evaluated reads
        self.lvalue = False  # evaluating an assignment target: no moved/uninitialised check
        self.skip_partial = False
        self.whole_target = None  # the `x` of `x = ..`: reassigning a reference does not use it
        self.legacy = 0  # open `#[enter_atomic]` calls on the current path
        self.legacy_diverged = False
        self.base_atomic = False  # the body is checked as atomic (`#[atomic_entry]`, `#[atomic_only]`, `&token` parameter)
        self.exempt = False  # `#[atomic_exempt]` body
        self.cb_calls: list = []  # (function passed for an `#[atomic_callback]` parameter, range)
        self.calls: list = []  # (caller decl, callee decl, range, atomic at the call, leaf name)
        self.sleepy: set = set()  # ids of functions declared `#[might_sleep]`
        self.not_sleepy: set = set()  # ids of `#[atomic_exempt]` functions
        self.iface_attrs: Dict[int, list] = {}  # id(impl fn) -> attribute names of the interface method it implements
        self.unsafe = 0  # depth of enclosing `unsafe` blocks (a `#[unsafe]` fn body counts)
        self.path_decl: Dict[int, object] = {}  # id(path expr) -> the impl function it names (`Type::f`)
        self.cur = None  # the function being checked
        self.edges: Dict[int, tuple] = {}  # id(fn decl) -> (decl, [(callee decl, call-site range)])
        self.made: Dict[int, tuple] = {}  # loans carried by the value of an expression (by node id)
        self.index: Dict[str, Dict[str, M.MethodEntry]] = {}
        self.self_stack: List[Ty] = []  # `Self` of the enclosing impl
        self.cell_list: list = []  # cells (`static`) declared in this module (4.17)
        self.cell_inits: list = []  # slot cells whose init function is checked for allocation
        self.cell_uses: list = []  # (cell, function, is `with_mut`, range)
        self.lambda_hint: Optional[List[Ty]] = None  # parameter types a cell accessor gives its closure

    # -- entry points

    def run(self) -> None:
        M.check_interfaces(self)
        M.check_impls(self)
        cmode.check(self)
        self.index = M.build_index(self)
        self.collect_iface_attrs()
        for it in self.mod.tree.items:
            self.item(it, [])
        self.recursion()
        cells.finish(self)
        self.atomic_context()

    # -- atomic context (section 4.13)

    def collect_iface_attrs(self) -> None:
        for it in self.mod.tree.items:
            if isinstance(it, ast.ImplDecl) and any(a.name.text == "might_sleep" for a in it.attrs):
                for f in it.items:
                    if isinstance(f, ast.FnDecl):
                        self.iface_attrs.setdefault(id(f), []).append("might_sleep")
            if isinstance(it, ast.ImplDecl) and it.iface is not None:
                isym = self.sym_type(it.iface.text)
                if isym is None or isym.kind != "interface":
                    continue
                by_name = {m.name.text: m for m in isym.node.members if isinstance(m, ast.Method)}
                for f in it.items:
                    if isinstance(f, ast.FnDecl) and f.name.text in by_name:
                        self.iface_attrs.setdefault(id(f), []).extend(a.name.text for a in by_name[f.name.text].attrs)

    def attrs_for(self, decl, node=None) -> set:
        names = {a.name.text for a in getattr(decl, "attrs", [])}
        names.update(a.name.text for a in getattr(node, "attrs", []))
        names.update(self.iface_attrs.get(id(decl), ()))
        return names

    def in_atomic(self) -> bool:
        return self.base_atomic or self.legacy > 0 or any(fr.guards for fr in self.frames)

    def site(self, decl, node, rng) -> None:
        """A call to `decl`: `#[atomic_only]`, `#[enter_atomic]`/`#[leave_atomic]` and the call list for sleep inference."""
        if decl is None or self.dead:
            return
        attrs = self.attrs_for(decl, node)
        name = decl.name.text
        if "atomic_exempt" in attrs:
            self.not_sleepy.add(id(decl))
        elif "might_sleep" in attrs:
            self.sleepy.add(id(decl))
        if "atomic_only" in attrs and not self.in_atomic():
            self.d.append(dg.error(rng, "KIFP111", f"{name!r} is #[atomic_only] but is called outside an atomic context"))
        if "enter_atomic" in attrs:
            self.legacy += 1
        if "leave_atomic" in attrs:
            if self.legacy == 0 and not self.base_atomic and not any(fr.guards for fr in self.frames):
                self.d.append(dg.error(rng, "KIFP112", f"{name!r} leaves an atomic scope that was not entered"))
            self.legacy = max(0, self.legacy - 1)
        if self.cur is not None and not self.exempt:
            self.calls.append((self.cur, decl, rng, self.in_atomic()))

    def site_attrs(self, decl) -> None:
        attrs = self.attrs_for(decl)
        if "atomic_exempt" in attrs:
            self.not_sleepy.add(id(decl))
        elif "might_sleep" in attrs:
            self.sleepy.add(id(decl))

    def atomic_context(self) -> None:
        """KIFP110: a sleepy function (declared `#[might_sleep]`, or one that calls a sleepy one) called while atomic."""
        sleepy = set(self.sleepy) - self.not_sleepy
        known = {id(c): c for c, _, _, _ in self.calls}
        changed = True
        while changed:
            changed = False
            for caller, callee, _, _ in self.calls:
                if id(callee) in sleepy and id(caller) not in sleepy and id(caller) not in self.not_sleepy \
                        and not getattr(caller, "is_gen", False):
                    sleepy.add(id(caller))
                    changed = True
        for cb, rng in self.cb_calls:
            if id(cb) in sleepy:
                self.d.append(dg.error(rng, "KIFP110", f"{cb.name.text!r} is passed as an #[atomic_callback] but may sleep"))
        for caller, callee, rng, atomic in self.calls:
            if atomic and id(callee) in sleepy and not getattr(caller, "is_gen", False):
                why = "is #[might_sleep]" if id(callee) in self.sleepy else "may sleep (it calls a sleepy function)"
                self.d.append(dg.error(rng, "KIFP110", f"{callee.name.text!r} {why} and is called in an atomic context"))

    def call_edge(self, decl, rng) -> None:
        """Remember that the current function calls (or takes the address of) `decl`."""
        if self.cur is not None and decl is not None and not self.dead:
            self.edges[id(self.cur)][1].append((decl, rng))

    def need_unsafe(self, decl, node, rng, what: str) -> None:
        """KIFP131 for a `#[unsafe]` function; KIFP070 for a C function that is not `#[safe]` (variadics never are)."""
        if self.unsafe or self.dead or decl is None:
            return
        name = decl.name.text
        if M.has_attr(getattr(decl, "attrs", []), "unsafe"):
            self.d.append(dg.error(rng, "KIFP131", f"{name!r} is #[unsafe]: {what} needs an 'unsafe' block"))
        elif isinstance(node, ast.ExternFn) and node.abi == "C" and not M.has_attr(node.attrs + decl.attrs, "safe"):
            why = "a variadic C function" if decl.variadic else "an extern \"C\" function that is not #[safe]"
            self.d.append(dg.error(rng, "KIFP070", f"{name!r} is {why}: {what} needs an 'unsafe' block"))

    def recursion(self) -> None:
        """KIFP032: `fn` and `method` bodies must not recurse, directly or through other functions (`gen fn` may)."""
        graph = {k: [(d, r) for d, r in out if id(d) in self.edges] for k, (_, out) in self.edges.items()}

        def reaches(src: int, goal: int) -> bool:
            todo, seen = [src], set()
            while todo:
                n = todo.pop()
                if n == goal:
                    return True
                if n in seen:
                    continue
                seen.add(n)
                todo.extend(id(d) for d, _ in graph.get(n, ()))
            return False

        for key, (decl, _) in self.edges.items():
            if getattr(decl, "is_gen", False):
                continue
            for callee, rng in graph[key]:
                if getattr(callee, "is_gen", False):
                    continue
                if id(callee) == key:
                    self.d.append(dg.error(rng, "KIFP032", f"{decl.name.text!r} calls itself: recursion is not allowed in lowered code"))
                    break
                if reaches(id(callee), key):
                    self.d.append(dg.error(rng, "KIFP032", f"{decl.name.text!r} is recursive through {callee.name.text!r}: "
                                                           "recursion is not allowed in lowered code"))
                    break

    def item(self, it, outer_generics: List[ast.GenericParam]) -> None:
        if isinstance(it, ast.FnDecl):
            self.fn(it, outer_generics)
        elif isinstance(it, ast.StaticDecl):
            cells.check_static(self, it)
        elif isinstance(it, ast.ConstDecl):
            def go() -> None:
                self.coerce(it.value, self.expr(it.value), self.ty(it.ty), "the constant")

            self.scoped(outer_generics, go)
        elif isinstance(it, ast.ImplDecl):
            self.generics.append({g.name.text for g in it.generics if not g.is_lifetime})
            try:
                target = self.ty(it.target)
            finally:
                self.generics.pop()
            self.self_stack.append(target)
            try:
                for m in it.items:
                    self.item(m, it.generics)
            finally:
                self.self_stack.pop()
        elif isinstance(it, ast.Interface):
            isym = self.sym_type(it.name.text) if M.has_attr(it.attrs, "ops") else None
            # in a default body of an `#[ops]` interface `self` is the handle: `&dyn I` / `&mut dyn I`
            self.self_stack.append(Ty(it.name.text, key=f"{isym.module.key}:{it.name.text}", cat="dyn", sym=isym) if isym else UNKNOWN)
            try:
                members = list(it.members)
                for i, m in enumerate(members):
                    if isinstance(m, ast.Method) and m.body is not None:
                        self.fn(m, [])
                    elif isinstance(m, ast.Hook):
                        nxt = next((x for x in members[i + 1:] if isinstance(x, ast.Method)), None)
                        self.hook(m, nxt)
            finally:
                self.self_stack.pop()

    def is_guard(self, t: Ty) -> bool:
        """An `#[atomic_scope]` struct, or a struct that holds a reference to an `#[atomic_lock]` struct (a lock guard)."""
        sym = t.sym
        if sym is None or sym.kind != "struct":
            return False
        if M.has_attr(sym.node.attrs, "atomic_scope"):
            return True
        for f in sym.node.fields:
            ft = self.ty(f.ty, sym.module)
            if ft.name == "&" and ft.args and ft.args[0].sym is not None and ft.args[0].sym.kind == "struct" \
                    and M.has_attr(ft.args[0].sym.node.attrs, "atomic_lock"):
                return True
        return False

    def hook(self, h: ast.Hook, method) -> None:
        """A `before`/`after` hook runs in the caller's context: atomic when the method it wraps is `#[atomic_entry]`/`#[atomic_only]`."""
        saved = (self.cur, self.base_atomic, self.exempt, self.legacy, self.legacy_diverged)
        names = {a.name.text for a in method.attrs} if method is not None else set()
        self.cur = SimpleNamespace(name=ast.Name(h.range, f"{h.kind} hook"), is_gen=False)
        self.base_atomic = bool(names & {"atomic_entry", "atomic_only"})
        self.exempt, self.legacy, self.legacy_diverged = False, 0, False
        self.edges[id(self.cur)] = (self.cur, [])
        try:
            self.scoped([], lambda: self.block(h.body))
        finally:
            self.cur, self.base_atomic, self.exempt, self.legacy, self.legacy_diverged = saved

    def scoped(self, generics: List[ast.GenericParam], fn) -> None:
        saved = (self.frames, self.ret, self.loops, self.dead)
        self.frames, self.ret, self.loops, self.dead = [_Frame()], None, [], False
        self.generics.append({g.name.text for g in generics if not g.is_lifetime})
        try:
            fn()
        finally:
            self.frames, self.ret, self.loops, self.dead = saved
            self.generics.pop()

    def fn(self, f, outer_generics: List[ast.GenericParam]) -> None:
        if f.body is None:
            if self.mod.header is None and isinstance(f, ast.FnDecl):
                self.d.append(dg.error(f.name.range, "KIFP017", f"function {f.name.text!r} has no body "
                                       "(only precompiled .kifm modules declare functions without one)"))
            return
        gens = list(outer_generics) + list(getattr(f, "generics", []))
        self.edges[id(f)] = (f, [])
        saved_cur, self.cur = self.cur, f
        saved_unsafe, self.unsafe = self.unsafe, int(M.has_attr(getattr(f, "attrs", []), "unsafe"))
        fattrs = self.attrs_for(f)
        saved_atomic = (self.base_atomic, self.exempt, self.legacy, self.legacy_diverged)
        self.base_atomic = bool(fattrs & {"atomic_entry", "atomic_only"}) and not getattr(f, "is_gen", False)
        self.exempt = "atomic_exempt" in fattrs or getattr(f, "is_gen", False)
        self.legacy, self.legacy_diverged = 0, False
        if "atomic_exempt" in fattrs:
            self.not_sleepy.add(id(f))
        elif "might_sleep" in fattrs:
            self.sleepy.add(id(f))

        def go() -> None:
            self.cfg_ctx = cfg.of_attrs(f.attrs)
            me = self.self_stack[-1] if self.self_stack else UNKNOWN
            for p in f.params:
                if p.receiver:
                    pt = {"value": me, "ref": T.ref(me), "mut": T.ref(me, True)}[p.receiver]
                else:
                    pt = self.ty(p.ty) if p.ty is not None else UNKNOWN
                self.declare(p.name, _Var(mut=False, ty=pt, param=True))
                tgt = pt.args[0] if pt.name == "&" and pt.args else pt
                if tgt.sym is not None and tgt.sym.kind == "struct" and M.has_attr(tgt.sym.node.attrs, "atomic_token"):
                    self.base_atomic = self.base_atomic or not getattr(f, "is_gen", False)
            self.ret = self.ty(f.ret) if f.ret is not None else UNIT
            body = self.block(f.body, new_frame=False)
            self.leaves(f.body.range)
            if f.body.tail is None and body != NEVER and self.ret != UNIT and self.ret != UNKNOWN:
                self.d.append(dg.error(f.body.range, "KIFP031", f"function {f.name.text!r} must return {self.ret} on every path"))
            elif f.body.tail is not None:
                self.coerce(f.body.tail, body, self.ret, "the result of the function")
                self.escapes(f.body.tail, "returns")

        saved_params, self.param_ty = self.param_ty, self.param_map(getattr(f, "generics", []))
        try:
            self.scoped(gens, go)
            taint.check_body(self, f)
        finally:
            self.param_ty = saved_params
            self.cur, self.unsafe = saved_cur, saved_unsafe
            self.base_atomic, self.exempt, self.legacy, self.legacy_diverged = saved_atomic

    # -- scopes

    def declare(self, name: ast.Name, var: _Var) -> None:
        var.name, var.depth, var.end = name.text, len(self.frames) - 1, self.frames[-1].end
        self.frames[-1].vars[name.text] = var

    def find(self, name: str):
        """(var, captured): `captured` is true when a lambda boundary lies between the use and the declaration."""
        captured = False
        for fr in reversed(self.frames):
            if name in fr.vars:
                return fr.vars[name], captured
            if fr.lambda_boundary:
                captured = True
        return None, False

    @staticmethod
    def param_map(gens) -> Dict[str, Ty]:
        return {g.name.text: Ty(g.name.text, cat="param", sym=T.ParamSym(g.bounds)) for g in gens if not g.is_lifetime and g.const_ty is None}

    def is_generic(self, name: str) -> bool:
        return any(name in g for g in self.generics)

    def known_value(self, name: str) -> bool:
        mod = self.mod
        if name in mod.scope_values or name in mod.scope_types:
            return True
        pre = mod.prelude
        return pre is not None and (name in pre.values or name in pre.types)

    def known_path_head(self, name: str) -> bool:
        return name in _KEYWORD_PATHS or name in _GEN_NAMES or name in self.mod.modules or self.is_generic(name) or self.known_value(name)

    # -- symbols and types

    def sym_type(self, name: str, mod=None):
        mod = mod or self.mod
        parts = name.split("::")
        if len(parts) == 1:
            s = mod.scope_types.get(name)
            if s is None and mod.prelude is not None:
                s = mod.prelude.types.get(name)
            return s
        src = mod.modules.get("::".join(parts[:-1]))
        return src.types.get(parts[-1]) if src is not None else None

    def sym_value(self, parts: List[str]):
        mod = self.mod
        if len(parts) == 1:
            s = mod.scope_values.get(parts[0])
            if s is None and mod.prelude is not None:
                s = mod.prelude.values.get(parts[0])
            return s
        src = mod.modules.get("::".join(parts[:-1]))
        return src.values.get(parts[-1]) if src is not None else None

    def ty(self, t, mod=None) -> Ty:
        """The checker's view of a written type."""
        if t is None:
            return UNIT
        if self.depth > 8:
            return UNKNOWN
        if isinstance(t, ast.RefType):
            return T.ref(self.ty(t.inner, mod), t.mut)
        if isinstance(t, ast.SliceType):
            return Ty("[]", (self.ty(t.elem, mod),))
        if isinstance(t, ast.ArrayType):
            return Ty("[;]", (self.ty(t.elem, mod),))
        if isinstance(t, ast.FnType):
            return Ty("fn", tuple(self.ty(p, mod) for p in t.params) + (self.ty(t.ret, mod),))
        if isinstance(t, ast.TupleType):
            return UNIT if not t.elems else T.tuple_of(self.ty(x, mod) for x in t.elems)
        if isinstance(t, ast.PathType):
            name = t.text
            if name == "Self":
                return self.self_stack[-1] if self.self_stack else UNKNOWN
            if self.is_generic(name):
                return self.param_ty.get(name, UNKNOWN)
            if name in T.INT_RANGES or name in ("bool", "char", "void"):
                return T.prim(name)
            sym = self.sym_type(name, mod)
            if sym is None:
                return UNKNOWN
            if t.dyn:
                if sym.kind != "interface":
                    return UNKNOWN
                return Ty(sym.node.name.text, key=f"{sym.module.key}:{sym.node.name.text}", cat="dyn", sym=sym)
            if sym.kind == "alias" and not sym.generics and not t.segments[-1].args:
                self.depth += 1
                try:
                    return self.ty(sym.node.ty, sym.module)
                finally:
                    self.depth -= 1
            real = [g for g in sym.generics if not g.is_lifetime]
            if sym.kind == "alias" and lang_result(sym):
                gargs = [a for a in t.segments[-1].args if not isinstance(a, (ast.Lifetime, ast.ConstArg))]
                return Ty("result", (self.ty(gargs[0], mod),) if gargs else (UNKNOWN,), key=f"{sym.module.key}:result", sym=sym)
            if sym.kind in ("struct", "enum") and not real:
                return Ty(sym.node.name.text, key=f"{sym.module.key}:{sym.node.name.text}", cat="adt", sym=sym)
            if name in ("c_ptr", "c_cptr", "vector", "ptr", "optional"):
                pa = [x for x in t.segments[-1].args if not isinstance(x, (ast.Lifetime, ast.ConstArg))]
                if pa:
                    return Ty(name, (self.ty(pa[0], mod),), key=f"{sym.module.key}:{name}", sym=sym)
            return Ty(name, key=f"{sym.module.key}:{name}", sym=sym)
        return UNKNOWN

    def struct_of(self, t: Ty):
        while t.name == "&" or (t.name == "ptr" and t.args):
            t = t.args[0]
        sym = t.sym
        return sym if sym is not None and t.cat == "adt" and sym.kind == "struct" else None

    # Types that cannot be copied: kif structs and `extern "C++"` types without `#[copy]` (optional-like wrappers are
    # treated as copyable until conditional copy is modelled).
    _CONDITIONAL_COPY = frozenset({"optional", "expected", "checked_value"})

    def is_move(self, t: Ty) -> bool:
        sym = t.sym
        if t.cat == "tuple":
            return any(self.is_move(a) for a in t.args)
        if t.cat == "param":
            return not (T.implied(T.bounds_of(t)) & {"copy"})
        if sym is None or t.name == "&":
            return False
        if sym.kind == "struct":
            return not M.has_attr(sym.node.attrs, "copy")
        if sym.kind == "extern_type":
            return not M.has_attr(sym.node.attrs, "copy") and sym.node.path.text.split("::")[-1] not in self._CONDITIONAL_COPY
        return False

    # -- flow state: moved / initialised variables

    def snap(self) -> dict:
        out = {}
        for fr in self.frames:
            for v in fr.vars.values():
                out[id(v)] = (v, v.moved, v.moved_fields, v.init, v.loans, v.hits)
        out["legacy"] = (self.legacy, self.legacy_diverged)
        return out

    def restore(self, state: dict) -> None:
        for k, (v, moved, fields, init, loans, hits) in ((k, x) for k, x in state.items() if k != "legacy"):
            v.moved, v.moved_fields, v.init, v.loans, v.hits = moved, fields, init, loans, hits
        self.legacy, self.legacy_diverged = state["legacy"]

    def merge(self, states: list) -> None:
        """Join the states at the end of branches: moved on any live path, initialised on all of them."""
        live = [st for st, dead in states if not dead]
        if not live:
            self.restore(states[0][0])
            self.dead = True
            return
        depths = {st["legacy"][0] for st in live}
        self.legacy = max(depths)
        self.legacy_diverged = len(depths) > 1 or any(st["legacy"][1] for st in live)
        # a `break`/`continue` snapshot also holds the locals of the loop body, which no longer exist where the paths join
        common = set.intersection(*(set(st) for st in live))
        for key, (v, _, _, _, _, _) in ((k, x) for k, x in live[0].items() if k != "legacy" and k in common):
            v.loans = tuple(dict.fromkeys(l for st in live for l in st[key][4]))
            v.hits = tuple(dict.fromkeys(h for st in live for h in st[key][5]))
            v.moved = any(st[key][1] for st in live)
            v.moved_fields = frozenset().union(*(st[key][2] for st in live))
            v.init = all(st[key][3] for st in live)
        self.dead = False

    def place(self, e):
        """(var, field path, through) for an lvalue rooted at a local; `through` is "index"/"deref" when one is crossed."""
        fields, through = [], ""
        while True:
            if isinstance(e, ast.FieldAccess):
                fields.append(e.name.text)
                e = e.expr
            elif isinstance(e, ast.Index):
                through, fields = "index", []
                e = e.expr
            elif isinstance(e, ast.Unary) and e.op == "*":
                through, fields = through or "deref", []
                e = e.operand
            else:
                break
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1:
            var, captured = self.find(e.segments[0].name)
            if var is not None and not captured:
                return var, ".".join(reversed(fields)), through
        return None, "", through

    def value(self, e) -> Ty:
        """Evaluate `e` where its value is consumed (moved when the type is not copyable)."""
        t = self.expr(e)
        self.consume(e, t)
        return t

    def consume(self, e, t: Ty) -> None:
        if not self.is_move(t) or self.dead:
            return
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1:
            var, captured = self.find(e.segments[0].name)
            if var is not None and not captured:
                if var.guard:
                    self.d.append(dg.error(e.range, "KIFP113",
                                           f"{var.name!r} holds an atomic scope and cannot be moved out of its block"))
                var.moved = True
                e.moved = True  # read by the C backend: this use hands the value on, so the local is no longer dropped at scope end
                self.access(var, "move", e.range)
            return
        if isinstance(e, (ast.Index, ast.Unary)) and not (isinstance(e, ast.Unary) and e.op != "*"):
            what = "an index" if isinstance(e, ast.Index) else "a reference"
            self.d.append(dg.error(e.range, "KIFP024", f"cannot move out of {what}"))
            return
        if isinstance(e, ast.FieldAccess):
            var, key, through = self.place(e)
            if through:
                self.d.append(dg.error(e.range, "KIFP024", f"cannot move a field out of {through}ed data"))
            elif var is not None:
                root = self.struct_of(var.ty)
                if var.ty.name == "&":
                    self.d.append(dg.error(e.range, "KIFP024", f"cannot move out of {e.name.text!r}: it is behind a reference"))
                elif root is not None and "drop" in self.index.get(var.ty.key, {}):
                    self.d.append(dg.error(e.range, "KIFP024", f"cannot move a field out of {var.ty}, which has a 'drop'"))
                else:
                    var.moved_fields = var.moved_fields | {key}

    def check_read(self, var: _Var, name: str, rng, key: str = "") -> None:
        if self.lvalue or self.dead:
            return
        if var.moved:
            self.d.append(dg.error(rng, "KIFP023", f"use of the moved value {name!r}"))
        elif var.deferred and not var.init:
            self.d.append(dg.error(rng, "KIFP022", f"{name!r} may be used before it is assigned"))
        elif var.moved_fields and not self.skip_partial:
            self.d.append(dg.error(rng, "KIFP023", f"use of {name!r}, which is partly moved"))
        elif key and any(m == key or key.startswith(m + ".") or m.startswith(key + ".") for m in var.moved_fields):
            self.d.append(dg.error(rng, "KIFP023", f"use of the moved field '{name}.{key}'"))

    # -- borrows

    def access(self, var: _Var, kind: str, rng, at: tuple = ("", None)) -> None:
        """Record an access to the part `at` (path, constant index) of `var` ("read", "shared", "write", "mut", "move") that conflicts
        with some live loan on an overlapping part of it."""
        if self.dead:
            return
        for loan in var.lent:
            if (loan.mut or kind in ("write", "mut", "move")) and places.overlap(loan.path, loan.idx, at[0], at[1]):
                var.hits += ((loan, rng),)

    def ppath(self, e) -> Optional[tuple]:
        """(path, constant index) of the place `e` inside its root local (kiflang/places.py), or None when it is not rooted at a local."""
        segs: list = []
        idx = None
        while True:
            if isinstance(e, ast.FieldAccess):
                segs.append("." + e.name.text)
                e = e.expr
            elif isinstance(e, ast.Index):
                segs.append("[]")
                idx = self.int_literal(e.index) if not segs[:-1] else idx
                e = e.expr
            elif isinstance(e, ast.Unary) and e.op == "*":
                segs.append("*")
                e = e.operand
            else:
                break
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1:
            return "".join(reversed(segs)), idx
        return None

    def touch(self, e, kind: str) -> None:
        var, _, _ = self.place(e)
        if var is not None:
            self.access(var, kind, e.range, self.ppath(e) or ("", None))

    def borrow(self, operand, mut: bool, rng, auto: bool = False) -> tuple:
        """The loans of `&operand`/`&mut operand`. Going through a reference reborrows its referent instead of the local."""
        var, key, through = self.place(operand)
        if var is None or self.dead:
            return ()
        if (var.ty.name == "&" and (key or through or auto)) or through == "deref":
            return var.loans
        at = self.ppath(operand) or ("", None)
        self.access(var, "mut" if mut else "shared", rng, at)
        loan = _Loan(var, mut, rng, *at)
        var.lent.append(loan)
        return (loan,)

    def loans_of(self, e, t: Ty = UNKNOWN) -> tuple:
        if self.dead or not (t.name == "&" or not T.simple(t)):
            return ()
        if isinstance(e, (ast.PathExpr, ast.FieldAccess, ast.Index)) or (isinstance(e, ast.Unary) and e.op == "*"):
            var, _, _ = self.place(e)
            return var.loans if var is not None else ()
        return self.made.get(id(e), ())

    def flow_loans(self, decl, recv: tuple, args: list) -> tuple:
        """Loans of a call result: those of the receiver and arguments whose lifetime the result names."""
        out: list = []
        for i in _flow_sources(decl):
            out.extend(recv if i < 0 else (self.loans_of(args[i]) if i < len(args) else ()))
        return tuple(dict.fromkeys(out))

    def conflicts_among(self, groups: list, rng) -> None:
        """Two loans of one call on the same local, at least one of them mutable (`f(&mut x, &x)`)."""
        seen: list = []
        for loans in groups:
            for a in loans:
                for b in seen:
                    if a.root is b.root and (a.mut or b.mut) and a is not b:
                        how = "mutably" if a.mut else "while it is also borrowed mutably"
                        self.d.append(dg.error(rng, "KIFP033", f"{a.root.name!r} is borrowed {how} by two arguments of one call"))
                        return
            seen.extend(loans)

    def check_loans(self, var: _Var, rng) -> None:
        """`var` is used: every loan it carries must still be valid."""
        if self.dead:
            return
        for loan in var.loans:
            root = loan.root
            for l, hit in root.hits:
                if l is loan:
                    kind = "mutable " if loan.mut else ""
                    self.d.append(dg.error(
                        rng, "KIFP033",
                        f"{var.name!r} is used here, but the {kind}borrow of {root.name!r} (line {loan.rng.start.line + 1}) "
                        f"was invalidated by an access on line {hit.start.line + 1}"))
                    break
            if root.end is not None and root.depth > var.depth and rng.start > root.end:
                self.d.append(dg.error(
                    rng, "KIFP021",
                    f"{var.name!r} outlives {root.name!r}: it borrows {root.name!r}, which went out of scope on line {root.end.line + 1}"))

    def escapes(self, e, what: str) -> None:
        """A returned value must not borrow a local of this function."""
        if self.dead:
            return
        for loan in self.loans_of(e):
            self.d.append(dg.error(e.range, "KIFP021",
                                   f"{what} a reference to the local {loan.root.name!r}, which does not live long enough"))
            return

    # -- reporting

    def mismatch(self, e, got: Ty, want: Ty, what: str, code: str = "KIFP012") -> None:
        self.d.append(dg.error(e.range, code, f"mismatched types in {what}: expected {want}, found {got}"))

    def coerce(self, e, got: Ty, want: Optional[Ty], what: str, code: str = "KIFP012") -> None:
        if want is None:
            return
        base = want
        while base.name == "&":
            base = base.args[0]
        if got == INTLIT and base.name in T.INT_RANGES:
            self.retype(e, base)
        if want.name == "&" and want.args[0].cat == "dyn":
            self.coerce_dyn(e, got, want, what, code)
            return
        if isinstance(e, ast.TupleExpr) and want.cat == "tuple" and len(e.elems) == len(want.args):
            for x, w in zip(e.elems, want.args):
                self.coerce(x, getattr(x, "ty", UNKNOWN), w, what, code)
            e.ty = want
            return
        if is_result(want) and is_result(got) and isinstance(e, ast.Call) and len(e.args) == 1 \
                and isinstance(e.callee, ast.PathExpr) and e.callee.segments[-1].name == "ok" and want.args[0] != UNKNOWN:
            self.coerce(e.args[0], got.args[0], want.args[0], "the value inside 'ok'")
            e.ty = want
            return
        if want.name == "optional" and got.name == "optional" and isinstance(e, ast.Call) and isinstance(e.callee, ast.PathExpr) \
                and e.callee.segments[-1].name in _OPTIONAL_NAMES and want.args and want.args[0] != UNKNOWN:
            if e.args:
                self.coerce(e.args[0], got.args[0], want.args[0], "the value inside 'some'")
            e.ty = want
            return
        lit = self.int_literal(e)
        if lit is not None:
            base = want
            while base.name == "&":
                base = base.args[0]
            if base.name in T.INT_RANGES and not T.fits(lit, base.name):
                self.d.append(dg.error(e.range, "KIFP013", f"the literal {lit} does not fit {base.name}"))
            elif not T.compat(got, want):
                self.mismatch(e, got, want, what, code)
            return
        if not T.compat(got, want):
            self.mismatch(e, got, want, what, code)

    def implements(self, t: Ty, isym) -> bool:
        """Does the struct type `t` have `impl <isym> for t`?"""
        for m in M.modules_of(self.mod):
            for it in m.tree.items:
                if isinstance(it, ast.ImplDecl) and it.iface is not None and self.ty(it.target, m).key == t.key:
                    s = self.sym_type(it.iface.text, m)
                    if s is not None and s.node is isym.node:
                        return True
        return False

    def coerce_dyn(self, e, got: Ty, want: Ty, what: str, code: str) -> None:
        """`&S` / `&mut S` where `&dyn I` / `&mut dyn I` is wanted: allowed when `impl I for S` exists (the lowerer builds the handle)."""
        w = want.args[0]
        if got == UNKNOWN or got == NEVER:
            return
        if got.name != "&":
            self.mismatch(e, got, want, what, code)
            return
        g = got.args[0]
        if want.mut and not got.mut:
            self.d.append(dg.error(e.range, code, f"{what}: {want} needs a mutable borrow, found {got}"))
        elif g.cat == "dyn":
            if g.key != w.key:
                self.mismatch(e, got, want, what, code)
        elif self.struct_of(got) is not None and g.cat == "adt":
            if not self.implements(g, w.sym):
                self.d.append(dg.error(e.range, code, f"{what}: {g} does not implement {w.name} (add `impl {w.name} for {g}`)"))
            else:
                e.dyn_to = want
        elif g != UNKNOWN:
            self.mismatch(e, got, want, what, code)

    def int_literal(self, e) -> Optional[int]:
        if isinstance(e, ast.Lit) and e.kind == "int" and not e.suffix:
            return int(e.value)
        if isinstance(e, ast.Unary) and e.op == "-" and isinstance(e.operand, ast.Lit) and e.operand.kind == "int" \
                and not e.operand.suffix:
            return -int(e.operand.value)
        return None

    # -- statements and blocks

    def block(self, b: ast.Block, new_frame: bool = True) -> Ty:
        if new_frame:
            self.frames.append(_Frame(end=b.range.end))
        try:
            diverges = False
            for s in b.stmts:
                if self.stmt(s) == NEVER:
                    diverges = True
            if b.tail is not None:
                t = self.value(b.tail)
                return NEVER if diverges else t
            return NEVER if diverges else UNIT
        finally:
            if new_frame:
                self.frames.pop()

    def stmt(self, s) -> Ty:
        if isinstance(s, ast.Let):
            want = self.ty(s.ty) if s.ty is not None else None
            got = UNKNOWN
            if s.init is not None:
                got = self.value(s.init)
                self.coerce(s.init, got, want, f"the initialiser of {s.name.text!r}")
            var_ty = want if want is not None else (UNKNOWN if got == NEVER else got)
            loans = self.loans_of(s.init, got) if s.init is not None else ()
            v = _Var(mut=s.mut, ty=var_ty, deferred=s.init is None, init=s.init is not None, loans=loans)
            self.declare(s.name, v)
            if self.is_guard(var_ty) and s.name.text != "_" and not self.dead:
                v.guard = True
                self.frames[-1].guards += 1
        elif isinstance(s, ast.LetPat):
            want = self.ty(s.ty) if s.ty is not None else None
            got = UNKNOWN
            if s.init is not None:
                got = self.value(s.init)
                self.coerce(s.init, got, want, "the initialiser")
            self.destructure(s.pattern, want if want is not None else got)
        elif isinstance(s, ast.ExprStmt):
            return self.expr(s.expr)
        elif isinstance(s, ast.ItemStmt):
            saved = self.frames
            try:
                self.item(s.item, [])
            finally:
                self.frames = saved
        return UNIT

    # -- expressions

    def expr(self, e) -> Ty:
        if e is None:
            return UNKNOWN
        m = getattr(self, "e_" + type(e).__name__, None)
        t = m(e) if m is not None else UNKNOWN
        e.ty = t  # read by the interpreter and the lowering
        return t

    @staticmethod
    def retype(e, want: Ty) -> None:
        """Give an untyped integer expression (`1`, `-1`, `1 + 2`) the integer type it ended up in."""
        if getattr(e, "ty", None) != INTLIT:
            return
        e.ty = want
        if isinstance(e, ast.Binary) and e.op not in _CMP and e.op not in _LOGIC:
            BodyChecker.retype(e.left, want)
            if e.op not in _SHIFT:
                BodyChecker.retype(e.right, want)
        elif isinstance(e, ast.Unary) and e.op in ("-", "!"):
            BodyChecker.retype(e.operand, want)
        elif isinstance(e, ast.Call) and hasattr(e, "targs"):
            e.targs = {k: (want if v == INTLIT else v) for k, v in e.targs.items()}
            for a in e.args:
                BodyChecker.retype(a, want)

    def e_Lit(self, e: ast.Lit) -> Ty:
        if e.kind == "bool":
            return BOOL
        if e.kind == "str":
            return STRLIT
        if e.suffix in T.INT_RANGES:
            if not T.fits(int(e.value), e.suffix):
                self.d.append(dg.error(e.range, "KIFP013", f"the literal {e.value} does not fit {e.suffix}"))
            return T.prim(e.suffix)
        return INTLIT

    def check_variant(self, sym, name: str, rng) -> None:
        """KIFP201: an item that exists only under a `#[cfg]` predicate used where it may not exist."""
        if all(v is None for v in sym.variants) or any(v is None for v in sym.variants):
            return
        ctx = getattr(self, "cfg_ctx", None)
        if not cfg.covers(ctx, sym.variants):
            where = f"under cfg({ctx})" if ctx is not None else "unconditionally"
            have = " | ".join(str(v) for v in sym.variants)
            self.d.append(dg.error(rng, "KIFP201", f"{name!r} exists only for cfg({have}) but is used {where}: "
                                   "guard the use with the same cfg or add a variant for the other targets"))

    def e_PathExpr(self, e: ast.PathExpr) -> Ty:
        names = [s.name for s in e.segments]
        head = names[0]
        if len(names) == 1:
            var, captured = self.find(head)
            if var is not None:
                if captured:
                    self.d.append(dg.error(e.range, "KIFP170",
                                           f"a lambda cannot use the local {head!r}: lambdas do not capture"))
                else:
                    self.check_read(var, head, e.range)
                    if e is not self.whole_target:
                        self.check_loans(var, e.range)
                        if not self.lvalue:
                            self.access(var, "read", e.range, self.read_at or ("", None))
                return var.ty
            if head == "self":
                return UNKNOWN
            sym = self.sym_value(names)
            if sym is not None:
                if sym.kind == "static":
                    self.d.append(dg.error(e.range, "KIFP191", f"{head!r} is a thread-local or per-CPU cell, not a value: "
                                                               "use NAME.with(|v| ..) (4.17)"))
                    return UNKNOWN
                self.check_variant(sym, head, e.range)
                if sym.kind == "fn":
                    self.call_edge(getattr(sym.node, "decl", sym.node), e.range)
                return self.value_type(sym)
            if head in _RESULT_NAMES:
                return Ty("fn", (UNKNOWN, Ty("result", (UNKNOWN,))))
            if head in _OPTIONAL_NAMES:
                return Ty("fn", (UNKNOWN, Ty("optional", (UNKNOWN,))))
            if self.known_value(head) or head in _GEN_NAMES:
                return UNKNOWN
            self.d.append(dg.error(e.range, "KIFP010", f"unknown name {head!r}"))
            return UNKNOWN
        if self.find(head)[0] is None and not self.known_path_head(head):
            self.d.append(dg.error(e.range, "KIFP010", f"unknown name {head!r}"))
            return UNKNOWN
        tsym = self.sym_type("::".join(names[:-1]))
        if tsym is not None and tsym.kind == "enum":
            if names[-1] not in {m.name.text for m in tsym.node.members}:
                self.d.append(dg.error(e.range, "KIFP010", f"{tsym.node.name.text!r} has no enumerator {names[-1]!r}"))
                return UNKNOWN
            return self.ty(ast.PathType(e.range, [ast.PathSeg(e.range, "::".join(names[:-1]))]))
        base = self.self_stack[-1] if names[:-1] == ["Self"] and self.self_stack else None
        if base is None and tsym is not None:
            base = self.ty(ast.PathType(e.range, [ast.PathSeg(e.range, "::".join(names[:-1]))]))
        if base is not None and base.key:
            entry = self.index.get(base.key, {}).get(names[-1])
            if entry is not None:
                self.call_edge(entry.decl, e.range)
                self.path_decl[id(e)] = entry.decl
                return self.entry_type(entry, with_receiver=True)
            if tsym is not None and tsym.kind == "struct":
                self.d.append(dg.error(e.range, "KIFP010", f"{tsym.node.name.text!r} has no function {names[-1]!r}"))
                return UNKNOWN
        sym = self.sym_value(names)
        if sym is not None and sym.kind == "fn":
            self.call_edge(getattr(sym.node, "decl", sym.node), e.range)
        return self.value_type(sym) if sym is not None else UNKNOWN

    def entry_type(self, entry: "M.MethodEntry", with_receiver: bool = False, recv: Optional[Ty] = None) -> Ty:
        """`fn(params) -> ret` of an impl function; the receiver is an unchecked first parameter when `with_receiver`."""
        decl = entry.decl
        self.generics.append({g.name.text for g in list(entry.generics) + list(getattr(decl, "generics", [])) if not g.is_lifetime})
        # a call on `vector<u32>` binds the impl's `T` to `u32`, so the signature (and `Self`) carry the element type
        bind = [g for g in entry.generics if not g.is_lifetime]
        by_recv = recv is not None and recv.name == entry.target.name and len(recv.args) == len(bind) > 0
        self.self_stack.append(recv if by_recv else entry.target)
        saved, self.param_ty = self.param_ty, self.param_map(getattr(decl, "generics", []))
        if by_recv:
            self.param_ty = {**self.param_ty, **{g.name.text: a for g, a in zip(bind, recv.args)}}
        try:
            params = []
            for p in decl.params:
                if p.receiver:
                    if with_receiver:
                        params.append(UNKNOWN)
                else:
                    params.append(self.ty(p.ty, entry.module) if p.ty is not None else UNKNOWN)
            return Ty("fn", tuple(params) + (self.ty(decl.ret, entry.module),))
        finally:
            self.param_ty = saved
            self.self_stack.pop()
            self.generics.pop()

    def value_type(self, sym) -> Ty:
        node = sym.node
        if sym.kind == "const" and hasattr(node, "ty"):
            return self.ty(node.ty, sym.module)
        decl = getattr(node, "decl", node)
        if sym.kind == "fn" and isinstance(decl, ast.FnDecl):
            return self.fn_type(decl, sym.module)
        return UNKNOWN

    def fn_type(self, decl: ast.FnDecl, mod) -> Ty:
        self.generics.append({g.name.text for g in decl.generics if not g.is_lifetime})
        saved, self.param_ty = self.param_ty, self.param_map(decl.generics)
        try:
            params = tuple(self.ty(p.ty, mod) if p.ty is not None else UNKNOWN for p in decl.params)
            return Ty("fn", params + (self.ty(decl.ret, mod),))
        finally:
            self.param_ty = saved
            self.generics.pop()

    def e_Unary(self, e: ast.Unary) -> Ty:
        t = self.expr(e.operand)
        if e.op == "&mut":
            self.mutation(e.operand, "borrow as mutable")
            self.made[id(e)] = self.borrow(e.operand, True, e.range)
            return T.ref(t, True)
        if e.op == "&":
            self.made[id(e)] = self.borrow(e.operand, False, e.range)
            return T.ref(t)
        if e.op == "*":
            if t.name in ("c_ptr", "c_cptr") and not self.unsafe and not self.dead:
                self.d.append(dg.error(e.range, "KIFP070", f"dereferencing a {t.name} needs an 'unsafe' block"))
            return t.args[0] if t.name in ("&", "c_ptr", "c_cptr", "ptr") and t.args else UNKNOWN
        if t.cat == "param" and e.op in ("-", "!"):
            self.need_bound(e, t, {"signed"} if e.op == "-" else {"integer"}, f"'{e.op}'")
            return t
        if e.op == "!":
            if t == BOOL or T.is_int(t) or not T.simple(t):
                return t
            self.d.append(dg.error(e.range, "KIFP012", f"'!' needs an integer or bool, found {t}"))
            return UNKNOWN
        if e.op == "-":
            if T.is_int(t) or not T.simple(t):
                return t
            self.d.append(dg.error(e.range, "KIFP012", f"'-' needs an integer, found {t}"))
            return UNKNOWN
        return UNKNOWN

    def e_Binary(self, e: ast.Binary) -> Ty:
        lt, rt = self.expr(e.left), self.expr(e.right)
        op = e.op
        if op in _LOGIC:
            self.coerce(e.left, lt, BOOL, f"the operand of '{op}'")
            self.coerce(e.right, rt, BOOL, f"the operand of '{op}'")
            return BOOL
        if op in _SHIFT and lt.cat == "param":
            self.need_bound(e, lt, {"integer"}, f"'{op}'")
            return lt
        if op in _SHIFT:
            for x, t in ((e.left, lt), (e.right, rt)):
                if T.simple(t) and not T.is_int(t):
                    self.mismatch(x, t, INTLIT, f"the operand of '{op}'")
            return lt
        j = T.join(lt, rt)
        if j is None:
            self.d.append(dg.error(e.range, "KIFP012", f"operands of '{op}' differ: {lt} and {rt}"))
            return BOOL if op in _CMP else UNKNOWN
        if j.cat == "param":
            need = {"ord"} if op in ("<", "<=", ">", ">=") else {"eq"} if op in ("==", "!=") else {"integer"}
            self.need_bound(e, j, need, f"'{op}'")
            return BOOL if op in _CMP else j
        if j.name in T.INT_RANGES:
            self.retype(e.left, j)
            self.retype(e.right, j)
        if op in _CMP:
            return BOOL
        if T.simple(j) and not (T.is_int(j) or (j == BOOL and op in _BITS)):
            self.d.append(dg.error(e.range, "KIFP012", f"'{op}' is not defined for {j}"))
            return UNKNOWN
        return j

    def need_bound(self, e, t: Ty, need: set, what: str) -> None:
        """KIFP064: a generic body may only do what the bounds of its type parameter provide (4.9)."""
        if not (T.implied(T.bounds_of(t)) & need):
            more = {"integer": {"signed", "unsigned"}, "eq": {"ord"}, "ord": {"integer"}}
            want = "', '".join(sorted(need | set().union(*(more.get(n, set()) for n in need))))
            self.d.append(dg.error(e.range, "KIFP064", f"{what} is not available on {t}: add one of the bounds '{want}'"))

    def e_Assign(self, e: ast.Assign) -> Ty:
        vt = self.value(e.value)
        self.lvalue = e.op == "="
        self.whole_target = e.target if isinstance(e.target, ast.PathExpr) else None
        try:
            tt = self.expr(e.target)
        finally:
            self.lvalue = False
            self.whole_target = None
        self.mutation(e.target, "assign to", init_ok=e.op == "=")
        self.touch(e.target, "write")
        self.assign_loans(e, vt)
        if e.op == "=":
            self.coerce(e.value, vt, tt, "the assignment")
            var, key, through = self.place(e.target)
            if var is not None and not through:
                if not key:
                    var.moved, var.moved_fields, var.init = False, frozenset(), True
                else:
                    var.moved_fields = frozenset(m for m in var.moved_fields if m != key and not m.startswith(key + "."))
        elif e.op in ("<<=", ">>="):
            pass
        else:
            j = T.join(tt, vt)
            if j is None:
                self.d.append(dg.error(e.range, "KIFP012", f"operands of '{e.op}' differ: {tt} and {vt}"))
        return UNIT

    def assign_loans(self, e: ast.Assign, vt: Ty) -> None:
        loans = self.loans_of(e.value, vt)
        var, key, through = self.place(e.target)
        if var is None or self.dead:
            return
        if var.param and var.ty.name == "&" and (key or through):
            for loan in loans:
                self.d.append(dg.error(e.range, "KIFP021",
                                       f"the local {loan.root.name!r} does not live long enough to be stored through {var.name!r}"))
                break
        if e.op == "=" and not key and not through:
            var.loans = loans
        elif loans:
            var.loans = tuple(dict.fromkeys(var.loans + loans))

    def mutation(self, target, what: str, init_ok: bool = False) -> None:
        """KIFP020 when `target` (an lvalue chain rooted at a local) cannot be modified."""
        node, chain, deref = target, False, False
        while True:
            if isinstance(node, (ast.FieldAccess, ast.Index)):
                node, chain = node.expr, True
            elif isinstance(node, ast.Unary) and node.op == "*":
                node, chain, deref = node.operand, True, True
            else:
                break
        if not (isinstance(node, ast.PathExpr) and len(node.segments) == 1):
            return
        name = node.segments[0].name
        var, captured = self.find(name)
        if var is None or captured:
            return
        if not chain:
            if what.startswith("call") and var.ty.name == "&":
                if not var.ty.mut:
                    self.d.append(dg.error(target.range, "KIFP020", f"cannot {what} {name!r}: it is a shared reference"))
                return
            if var.mut:
                return
            if var.deferred and init_ok and not var.init:
                return
            self.d.append(dg.error(target.range, "KIFP020",
                                   f"cannot {what} the immutable variable {name!r}; declare it with 'let mut'"))
            return
        t = var.ty
        if t.name == "&":
            if not t.mut:
                self.d.append(dg.error(target.range, "KIFP020", f"cannot {what} {name!r} through a shared reference"))
            return
        if deref or var.mut or not (T.simple(t) or t.name == "[;]"):
            return
        self.d.append(dg.error(target.range, "KIFP020",
                               f"cannot {what} a part of the immutable variable {name!r}; declare it with 'let mut'"))

    def e_Cast(self, e: ast.Cast) -> Ty:
        src = self.expr(e.expr)
        dst = self.ty(e.ty)
        if dst == BOOL and T.is_int(src):
            self.d.append(dg.error(e.range, "KIFP012", "an integer cannot be cast to bool; write 'x != 0'"))
        return dst

    def ct_call(self, e: ast.Call) -> Optional[Ty]:
        """A call of a `ct::` intrinsic. They have no declaration anywhere: the compiler synthesizes their signatures here."""
        c = e.callee
        if not (isinstance(c, ast.PathExpr) and len(c.segments) == 2 and c.segments[0].name == "ct" and c.segments[1].name in CT_ARITY
                and self.find("ct")[0] is None and self.sym_value(["ct", c.segments[1].name]) is None):
            return None
        name = c.segments[1].name
        got = [self.expr(a) for a in e.args]
        if len(got) != CT_ARITY[name]:
            self.d.append(dg.error(e.range, "KIFP011", f"'ct::{name}' takes {CT_ARITY[name]} argument(s), found {len(got)}"))
            return UNKNOWN
        if name in ("select", "mask"):
            self.coerce(e.args[0], got[0], BOOL, f"the condition of 'ct::{name}'")
        vals = {"select": got[1:], "mask": []}.get(name, got)
        j = None
        for t in vals:
            j = t if j is None else T.join(j, t)
            if j is None or (T.simple(j) and not T.is_int(j) and j != INTLIT):
                self.d.append(dg.error(e.range, "KIFP012", f"'ct::{name}' works on integers of one type, found {', '.join(map(str, vals))}"))
                return UNKNOWN
        if j is not None and j.name in T.INT_RANGES:
            for a in e.args[len(e.args) - len(vals):]:
                self.retype(a, j)
        e.ct = name   # read by taint.py, ir.py and interp.py
        return BOOL if name in ("eq", "lt") else Ty("usize") if name == "mask" else (j or UNKNOWN)

    def e_Call(self, e: ast.Call) -> Ty:
        ct = self.ct_call(e)
        if ct is not None:
            return ct
        ft = self.expr(e.callee)
        got = [self.value(a) for a in e.args]
        if isinstance(e.callee, ast.PathExpr) and len(e.callee.segments) == 1 and e.callee.segments[0].name in _RESULT_NAMES \
                and self.find(e.callee.segments[0].name)[0] is None and self.sym_value([e.callee.segments[0].name]) is None:
            if len(got) != 1:
                self.d.append(dg.error(e.range, "KIFP011", f"'{e.callee.segments[0].name}' takes 1 argument, found {len(got)}"))
            return Ty("result", (got[0] if got and e.callee.segments[0].name == "ok" else UNKNOWN,))
        if isinstance(e.callee, ast.PathExpr) and len(e.callee.segments) == 1 and e.callee.segments[0].name in _OPTIONAL_NAMES \
                and self.find(e.callee.segments[0].name)[0] is None and self.sym_value([e.callee.segments[0].name]) is None:
            some = e.callee.segments[0].name == "some"
            if len(got) != (1 if some else 0):
                self.d.append(dg.error(e.range, "KIFP011", f"'{e.callee.segments[0].name}' takes {1 if some else 0} argument(s), found {len(got)}"))
            osym = self.sym_type("optional")
            return Ty("optional", (got[0] if some and got else UNKNOWN,), key=f"{osym.module.key}:optional" if osym else "", sym=osym)
        self.conflicts_among([self.loans_of(a) for a in e.args], e.range)
        if ft.name != "fn":
            return UNKNOWN
        params, ret = ft.args[:-1], ft.args[-1]
        name = e.callee.text if isinstance(e.callee, ast.PathExpr) else "function"
        decl = self.callee_decl(e.callee)
        if isinstance(e.callee, ast.PathExpr):
            if decl is not None:
                sym = self.sym_value([s.name for s in e.callee.segments])
                self.need_unsafe(decl, sym.node, e.range, "calling it")
                self.site(decl, sym.node, e.range)
            else:
                pd = self.path_decl.get(id(e.callee))
                self.need_unsafe(pd, None, e.range, "calling it")
                self.site(pd, None, e.range)
        if decl is not None:
            for prm, a in zip([q for q in decl.params if not q.receiver], e.args):
                if M.has_attr(prm.attrs, "atomic_callback") and isinstance(a, ast.PathExpr):
                    cb = self.callee_decl(a)
                    if cb is not None and not self.dead:
                        self.cb_calls.append((cb, a.range))
                        self.site_attrs(cb)
            self.made[id(e)] = self.flow_loans(decl, (), e.args)
        gp = [g for g in getattr(decl, "generics", []) if not g.is_lifetime and g.const_ty is None] if decl is not None else []
        if gp:
            params, ret = self.instantiate(e, gp, params, ret, got)
        vd = decl if decl is not None else (self.path_decl.get(id(e.callee)) if isinstance(e.callee, ast.PathExpr) else None)
        e.decl = vd   # read by taint.py
        if vd is not None and getattr(vd, "variadic", False):
            if len(got) < len(params):
                self.d.append(dg.error(e.range, "KIFP011", f"{name!r} takes at least {len(params)} argument(s), found {len(got)}"))
        elif len(got) != len(params):
            self.d.append(dg.error(e.range, "KIFP011", f"{name!r} takes {len(params)} argument(s), found {len(got)}"))
            return ret
        for i, (a, g, p) in enumerate(zip(e.args, got, params)):
            if isinstance(a, ast.Lambda) and p.name == "fn" and len(a.params) != len(p.args) - 1:
                self.d.append(dg.error(a.range, "KIFP011", f"argument {i + 1} of {name!r} must take {len(p.args) - 1} parameter(s), "
                                                           f"the lambda takes {len(a.params)}"))
                continue
            if isinstance(a, ast.Lambda) and p.name == "fn":
                for lp, want in zip(g.args[:-1], p.args[:-1]):
                    if lp != UNKNOWN and not T.compat(lp, want):
                        self.mismatch(a, g, p, f"argument {i + 1} of {name!r}", "KIFP011")
                        break
                continue
            self.coerce(a, g, p, f"argument {i + 1} of {name!r}", "KIFP011")
        return ret

    def instantiate(self, e: ast.Call, gp, params, ret: Ty, got) -> tuple:
        """Infer the type arguments of a generic call (4.9), check their bounds (KIFP062, KIFP063) and substitute them."""
        bind: Dict[str, Ty] = {}
        seg = e.callee.segments[-1] if isinstance(e.callee, ast.PathExpr) else None
        explicit = [a for a in (seg.args if seg else []) if not isinstance(a, (ast.Lifetime, ast.ConstArg))]
        for g, a in zip(gp, explicit):
            bind[g.name.text] = self.ty(a)
        for p, g in zip(params, got):
            self.unify(p, g, bind)
        name = e.callee.text if isinstance(e.callee, ast.PathExpr) else "function"
        for g in gp:
            n = g.name.text
            if n not in bind:
                if not any(self.mentions(p, n) for p in params):
                    self.d.append(dg.error(e.range, "KIFP063", f"cannot infer the type parameter {n!r} of {name!r}; "
                                                              f"write {name}::<type>(..)"))
                bind[n] = UNKNOWN
            self.check_bounds(e, name, g, bind[n])
        e.targs = bind  # read by the interpreter
        return [self.subst(p, bind) for p in params], self.subst(ret, bind)

    def unify(self, p: Ty, a: Ty, bind: Dict[str, Ty]) -> None:
        if p.cat == "param":
            if a not in (UNKNOWN, NEVER, STRLIT) and bind.get(p.name) in (None, INTLIT, UNKNOWN):
                bind[p.name] = a
        elif p.name == a.name and p.args and len(p.args) == len(a.args):
            for x, y in zip(p.args, a.args):
                self.unify(x, y, bind)

    def mentions(self, t: Ty, name: str) -> bool:
        return (t.cat == "param" and t.name == name) or any(self.mentions(a, name) for a in t.args)

    def subst(self, t: Ty, bind: Dict[str, Ty]) -> Ty:
        if t.cat == "param":
            return bind.get(t.name, t)
        if t.args:
            return replace(t, args=tuple(self.subst(a, bind) for a in t.args))
        return t

    def check_bounds(self, e, fname: str, g: ast.GenericParam, u: Ty) -> None:
        if T.is_flexible(u):
            return
        if u.cat == "param":
            have = T.implied(T.bounds_of(u))
            for b in g.bounds:
                if not (T.implied([b.text]) <= have):
                    self.d.append(dg.error(e.range, "KIFP062", f"{u} does not satisfy the bound '{b.text}' of "
                                                              f"{g.name.text!r} in {fname!r}: add it to {u}"))
            return
        base = u
        while base.name == "&":
            base = base.args[0]
        for b in g.bounds:
            if not self.has_bound(u, base, b.text):
                self.d.append(dg.error(e.range, "KIFP062", f"{u} does not satisfy the bound '{b.text}' of "
                                                          f"{g.name.text!r} in {fname!r}"))

    def has_bound(self, u: Ty, base: Ty, b: str) -> bool:
        is_ref = u.name == "&"
        intl = base.name in T.INT_RANGES
        if b == "integer":
            return intl
        if b == "signed":
            return intl and T.INT_RANGES[base.name][0] < 0
        if b == "unsigned":
            return intl and T.INT_RANGES[base.name][0] == 0
        if b in ("eq", "ord", "hash"):
            if is_ref and b != "eq":
                return False
            return intl or base.name == "char" or (base.sym is not None and base.sym.kind == "enum") \
                or (b == "eq" and base == BOOL) or (b == "hash" and base == BOOL) or not T.simple(base)
        if b == "copy":
            if is_ref:
                return not u.mut
            return base.cat == "prim" or not self.is_move(base)
        if b == "cloneable":
            return base.cat == "prim" or not self.is_move(base) or "try_clone" in self.index.get(base.key, {})
        isym = self.sym_type(b)
        if isym is not None and isinstance(isym.node, ast.Interface):
            if not base.key:
                return not T.simple(base)
            for en in self.index.get(base.key, {}).values():
                es = self.sym_type(en.iface) if en.iface else None
                if en.iface == isym.node.name.text or (es is not None and any(s.node is isym.node for s in M.chain(self, es))):
                    return True
            return False
        return True

    def callee_decl(self, c):
        if isinstance(c, ast.PathExpr) and self.find(c.segments[0].name)[0] is None:
            sym = self.sym_value([s.name for s in c.segments])
            if sym is not None and sym.kind == "fn":
                return getattr(sym.node, "decl", sym.node)
        return None

    def e_MethodCall(self, e: ast.MethodCall) -> Ty:
        if isinstance(e.receiver, ast.PathExpr) and self.find(e.receiver.segments[0].name)[0] is None:
            csym = self.sym_value([s.name for s in e.receiver.segments])
            if csym is not None and csym.kind == "static":
                return cells.call(self, e, csym)
        rt = self.expr(e.receiver)
        got = [self.value(a) for a in e.args]
        self.conflicts_among([self.loans_of(a) for a in e.args], e.range)
        base = rt
        while base.name == "&":
            base = base.args[0]
        name = e.name.text
        if base.cat == "dyn":
            entry = self.dyn_method(base, name)
            if entry is None:
                self.d.append(dg.error(e.name.range, "KIFP010", f"{base} has no method {name!r}"))
                return UNKNOWN
            if entry.receiver == "mut" and not (rt.name == "&" and rt.mut):
                self.d.append(dg.error(e.name.range, "KIFP020", f"{name!r} takes '&mut self': the handle must be '&mut {base}'"))
            e.recv_mode = entry.receiver  # read by the IR builder: how the call borrows its receiver
            e.dyn = True   # a call whose target is chosen at run time (taint.py: KIFT008)
            ft = self.entry_type(entry, recv=base)
            params, ret = ft.args[:-1], ft.args[-1]
            if len(got) != len(params):
                self.d.append(dg.error(e.name.range, "KIFP011", f"{name!r} takes {len(params)} argument(s), found {len(got)}"))
                return ret
            for i, (a, g, p) in enumerate(zip(e.args, got, params)):
                self.coerce(a, g, p, f"argument {i + 1} of {name!r}", "KIFP011")
            return ret
        if name in ("as_slice", "as_mut_slice") and not e.args and base.name in ("[]", "[;]", "vector") and base.args:
            mut = name == "as_mut_slice"
            e.recv_mode = "mut" if mut else "ref"
            if mut:
                self.mutation(e.receiver, "call 'as_mut_slice' on", init_ok=False)
            self.made[id(e)] = self.borrow(e.receiver, mut, e.receiver.range, auto=True)
            return Ty("&", (Ty("[]", (base.args[0],)),), mut=mut)
        if base.name in ("[]", "[;]") and not e.args and name in ("len", "is_empty"):
            e.recv_mode = "ref"  # an array or a slice knows its own length; the C backend lowers it
            return Ty("usize", cat="prim") if name == "len" else BOOL
        if base.cat == "param":
            entry = self.bound_method(base, name)
            if entry is None:
                if "cloneable" in T.implied(T.bounds_of(base)) and name == "try_clone":
                    return Ty("result", (base,))
                self.d.append(dg.error(e.name.range, "KIFP064", f"{base} has no method {name!r} in its bounds"))
                return UNKNOWN
        elif not base.key:
            return UNKNOWN
        else:
            entry = self.index.get(base.key, {}).get(name)
            if entry is None and base.name == "ptr" and base.args and base.args[0].key:
                base = base.args[0]  # a method of the pointee through the owning pointer
                entry = self.index.get(base.key, {}).get(name)
        if entry is None:
            if self.struct_of(base) is not None:
                self.d.append(dg.error(e.name.range, "KIFP010", f"{base} has no method {name!r}"))
            return UNKNOWN
        if not entry.receiver:
            self.d.append(dg.error(e.name.range, "KIFP011", f"{name!r} is an associated function: call it as {base}::{name}(..)"))
            return UNKNOWN
        if entry.receiver == "value":
            self.consume(e.receiver, rt)
        if entry.receiver == "mut":
            self.mutation(e.receiver, "call the '&mut self' method " + repr(name) + " on", init_ok=False)
        self.call_edge(entry.decl, e.name.range)
        self.need_unsafe(entry.decl, None, e.name.range, "calling it")
        self.site(entry.decl, None, e.name.range)
        e.recv_mode = entry.receiver
        e.decl = entry.decl   # read by taint.py
        recv = ()
        if entry.receiver == "value":
            recv = self.loans_of(e.receiver)
        elif entry.receiver in ("ref", "mut", "implicit"):
            recv = self.borrow(e.receiver, entry.receiver == "mut", e.receiver.range, auto=True)
        self.made[id(e)] = self.flow_loans(entry.decl, recv, e.args)
        ft = self.entry_type(entry, recv=base)
        params, ret = ft.args[:-1], ft.args[-1]
        if len(got) != len(params):
            self.d.append(dg.error(e.name.range, "KIFP011", f"{name!r} takes {len(params)} argument(s), found {len(got)}"))
            return ret
        for i, (a, g, p) in enumerate(zip(e.args, got, params)):
            self.coerce(a, g, p, f"argument {i + 1} of {name!r}", "KIFP011")
        return ret

    def dyn_method(self, t: Ty, name: str):
        found = M.inherited(self, t.sym).get(name)
        if found is None or found[0].static:
            return None
        return M.MethodEntry(found[0], found[1], M.receiver_of(found[0]) or "implicit", [], found[2], t)

    def bound_method(self, t: Ty, name: str):
        """The method `name` of an interface that bounds the type parameter `t`."""
        for b in t.sym.bounds:
            isym = self.sym_type(b.text)
            if isym is not None and isinstance(isym.node, ast.Interface):
                found = M.inherited(self, isym).get(name)
                if found is not None and not found[0].static:
                    return M.MethodEntry(found[0], found[1], M.receiver_of(found[0]) or "implicit", [], found[2], t)
        return None

    def e_FieldAccess(self, e: ast.FieldAccess) -> Ty:
        saved_skip, saved_at = self.skip_partial, self.read_at
        self.skip_partial = isinstance(e.expr, ast.PathExpr)
        if saved_at is None:
            self.read_at = self.ppath(e)
        try:
            t = self.expr(e.expr)
        finally:
            self.skip_partial, self.read_at = saved_skip, saved_at
        var, key, through = self.place(e)
        if var is not None and not through and var.moved_fields:
            self.skip_partial = True
            try:
                self.check_read(var, e.expr.text if isinstance(e.expr, ast.PathExpr) else "", e.range, key)
            finally:
                self.skip_partial = saved_skip
        base = t
        while base.name == "&":
            base = base.args[0]
        if base.cat == "tuple":
            if e.name.text.isdigit() and int(e.name.text) < len(base.args):
                return base.args[int(e.name.text)]
            self.d.append(dg.error(e.name.range, "KIFP010", f"{base} has no field {e.name.text!r}"))
            return UNKNOWN
        sym = self.struct_of(t)
        if sym is None:
            return UNKNOWN
        for f in sym.node.fields:
            if f.name.text == e.name.text:
                return self.ty(f.ty, sym.module)
        self.d.append(dg.error(e.name.range, "KIFP010", f"{sym.node.name.text!r} has no field {e.name.text!r}"))
        return UNKNOWN

    def e_Index(self, e: ast.Index) -> Ty:
        saved_at = self.read_at
        if saved_at is None:
            self.read_at = self.ppath(e)
        try:
            t = self.expr(e.expr)
        finally:
            self.read_at = None
        try:
            it = self.expr(e.index)
        finally:
            self.read_at = saved_at
        while t.name == "&":
            t = t.args[0]
        if isinstance(e.index, ast.RangeExpr):
            return UNKNOWN
        self.coerce(e.index, it, T.prim("usize"), "the index")
        return t.args[0] if t.name in ("[]", "[;]", "vector") and t.args else UNKNOWN

    def e_Try(self, e: ast.Try) -> Ty:
        t = self.expr(e.expr)
        if t not in (UNKNOWN, NEVER) and not is_result(t) and not t.name == "&":
            self.d.append(dg.error(e.range, "KIFP041", f"'?' needs a result, found {t}"))
        if self.ret not in (None, UNKNOWN) and not is_result(self.ret) and self.ret != NEVER:
            self.d.append(dg.error(e.range, "KIFP041", f"'?' returns the error, but the function returns {self.ret}, not a result"))
        return t.args[0] if is_result(t) else UNKNOWN

    def e_RangeExpr(self, e: ast.RangeExpr) -> Ty:
        lo, hi = self.expr(e.lo), self.expr(e.hi)
        if e.lo is not None and e.hi is not None and T.join(lo, hi) is None:
            self.d.append(dg.error(e.range, "KIFP012", f"the bounds of a range differ: {lo} and {hi}"))
        return UNKNOWN

    def e_StructLit(self, e: ast.StructLit) -> Ty:
        names = [s.name for s in e.path.segments]
        sym = self.sym_type("::".join(names)) if names != ["Self"] else None
        if names == ["Self"] and self.self_stack and self.self_stack[-1].sym is not None:
            sym = self.self_stack[-1].sym
        given = set()
        for f in e.fields:
            if f.name.text in given:
                self.d.append(dg.error(f.name.range, "KIFP014", f"field {f.name.text!r} is given twice"))
            given.add(f.name.text)
            if f.value is not None:
                vt = self.value(f.value)
                vnode = f.value
            else:
                vnode = ast.PathExpr(f.name.range, [ast.PathSeg(f.name.range, f.name.text)])
                vt = self.value(vnode)
            if sym is not None and sym.kind == "struct":
                fld = next((x for x in sym.node.fields if x.name.text == f.name.text), None)
                if fld is None:
                    self.d.append(dg.error(f.name.range, "KIFP014", f"{sym.node.name.text!r} has no field {f.name.text!r}"))
                else:
                    self.coerce(vnode, vt, self.ty(fld.ty, sym.module), f"field {f.name.text!r}")
        self.expr(e.base)
        self.made[id(e)] = tuple(dict.fromkeys(
            l for f in e.fields for l in self.loans_of(f.value if f.value is not None else
                                                      ast.PathExpr(f.name.range, [ast.PathSeg(f.name.range, f.name.text)]))))
        if sym is None or sym.kind != "struct":
            if sym is None and names != ["Self"] and not self.is_generic(names[0]):
                self.d.append(dg.error(e.path.range, "KIFP010", f"unknown struct {'::'.join(names)!r}"))
            return UNKNOWN
        if e.base is None:
            missing = [x.name.text for x in sym.node.fields if x.name.text not in given and x.default is None]
            if missing:
                self.d.append(dg.error(e.range, "KIFP014", f"missing field(s) in {sym.node.name.text!r}: {', '.join(missing)}"))
        if names == ["Self"]:
            return self.self_stack[-1]
        return self.ty(ast.PathType(e.path.range, [ast.PathSeg(e.path.range, "::".join(names))]))

    def e_ArrayLit(self, e: ast.ArrayLit) -> Ty:
        elem: Ty = NEVER
        for x in e.elems:
            t = self.value(x)
            j = T.join(elem, t)
            if j is None:
                self.mismatch(x, t, elem, "the array elements")
            else:
                elem = j
        self.expr(e.repeat)
        self.made[id(e)] = tuple(dict.fromkeys(l for x in e.elems for l in self.loans_of(x)))
        return Ty("[;]", (UNKNOWN if elem == NEVER else elem,))

    def e_TupleExpr(self, e: ast.TupleExpr) -> Ty:
        if not e.elems:
            return UNIT
        return T.tuple_of(self.value(x) for x in e.elems)

    def e_MacroCall(self, e: ast.MacroCall) -> Ty:
        if e.name.text == "untaint" and len(e.args) == 2:
            return self.expr(e.args[1])      # `untaint!("reason", f(..))`: the shape is checked by taint.py (KIFT010)
        for a in e.args:
            self.expr(a)
        if e.name.text in ("format", "fail", "warn", "panic") and e.args:
            self.check_format(e)
        if e.name.text in ("panic", "unreachable"):
            self.dead = True
            return NEVER
        return UNKNOWN

    def check_format(self, e: ast.MacroCall) -> None:
        """KIFP040: the placeholders of a literal format string must match the arguments."""
        head = e.args[0]
        if not (isinstance(head, ast.Lit) and head.kind == "str"):
            return
        try:
            n = format_placeholders(head.value)
        except ValueError as err:
            self.d.append(dg.error(head.range, "KIFP040", str(err)))
            return
        if n != len(e.args) - 1:
            self.d.append(dg.error(e.range, "KIFP040",
                                   f"{e.name.text}! has {n} placeholder(s) but {len(e.args) - 1} argument(s)"))

    def e_Block(self, e: ast.Block) -> Ty:
        return self.block(e)

    def e_CfgBlock(self, e: ast.CfgBlock) -> Ty:
        """`cfg(pred) { .. }`: checked on every target (as if its predicate held), type unit."""
        try:
            pred = cfg.parse_attribute(e.attr)
        except cfg.CfgError:
            pred = None  # reported as KIFP200 by symbols.py
        saved = getattr(self, "cfg_ctx", None)
        self.cfg_ctx = cfg.conj(saved, pred)
        try:
            self.block(e.body)
        finally:
            self.cfg_ctx = saved
        return UNIT

    def e_UnsafeBlock(self, e: ast.UnsafeBlock) -> Ty:
        self.unsafe += 1
        try:
            return self.block(e.body)
        finally:
            self.unsafe -= 1

    def e_If(self, e: ast.If) -> Ty:
        self.coerce(e.cond, self.expr(e.cond), BOOL, "the condition")
        s0, dead0 = self.snap(), self.dead
        then = self.block(e.then)
        branches = [(self.snap(), self.dead)]
        self.restore(s0)
        self.dead = dead0
        if e.otherwise is None:
            branches.append((s0, dead0))
            self.merge(branches)
            return UNIT
        other = self.expr(e.otherwise)
        branches.append((self.snap(), self.dead))
        self.merge(branches)
        j = T.join(then, other)
        if j is None:
            self.d.append(dg.error(e.range, "KIFP012", f"the branches of 'if' differ: {then} and {other}"))
            return UNKNOWN
        return j

    def run_loop(self, body, can_exit_by_condition: bool) -> bool:
        """Run a loop body (twice when it moves an outer value, so the second iteration sees the move); returns whether
        a `break` exists. The state afterwards joins the exits: the condition failing and every `break`."""
        s0, dead0 = self.snap(), self.dead
        entry = [False, []]
        self.loops.append(entry)
        try:
            body()
            end, dead_end = self.snap(), self.dead
            if not dead_end and any(not s0[k][1] and end[k][1] or end[k][2] - s0[k][2] for k in s0 if k != "legacy"):
                self.merge([(s0, dead0), (end, False)])
                before = list(self.d)
                body()
                seen = set(before)
                self.d[:] = before + [x for x in self.d[len(before):] if x not in seen]
                end, dead_end = self.snap(), self.dead
        finally:
            self.loops.pop()
        exits = list(entry[1])
        if can_exit_by_condition:
            exits += [(s0, dead0), (end, dead_end)]
        if not exits:
            self.restore(end)
            self.dead = True
        else:
            self.merge(exits)
        return entry[0]

    def e_While(self, e: ast.While) -> Ty:
        def body() -> None:
            self.coerce(e.cond, self.expr(e.cond), BOOL, "the condition")
            self.block(e.body)

        self.run_loop(body, True)
        return UNIT

    def e_Loop(self, e: ast.Loop) -> Ty:
        broke = self.run_loop(lambda: self.block(e.body), False)
        return UNIT if broke else NEVER

    def e_Break(self, e: ast.Break) -> Ty:
        if self.loops:
            self.loops[-1][0] = True
            if not self.dead:
                self.loops[-1][1].append((self.snap(), False))
        self.dead = True
        return NEVER

    def e_Continue(self, e: ast.Continue) -> Ty:
        self.dead = True
        return NEVER

    def e_For(self, e: ast.For) -> Ty:
        self.value(e.iter)
        var_ty = UNKNOWN
        if isinstance(e.iter, ast.RangeExpr):
            var_ty = T.join(self.expr(e.iter.lo), self.expr(e.iter.hi)) or UNKNOWN
        else:
            it = getattr(e.iter, "ty", None)
            inner = it.args[0] if it is not None and it.name == "&" else it
            if inner is not None and inner.name in ("[]", "[;]", "vector") and inner.args:
                # a borrowed sequence yields references to its elements; an array by value yields its elements
                var_ty = Ty("&", (inner.args[0],), mut=it.mut) if it.name == "&" else inner.args[0]

        def body() -> None:
            self.frames.append(_Frame())
            try:
                self.declare(e.var, _Var(mut=False, ty=var_ty))
                self.block(e.body)
            finally:
                self.frames.pop()

        self.run_loop(body, True)
        return UNIT

    def e_Match(self, e: ast.Match) -> Ty:
        st = self.expr(e.scrutinee)
        for arm in e.arms:
            arm.pattern = self.bare_names(arm.pattern)
        self.exhaustive(e, st)
        out: Ty = NEVER
        s0, dead0 = self.snap(), self.dead
        states = []
        for arm in e.arms:
            self.restore(s0)
            self.dead = dead0
            self.pattern(arm.pattern)
            self.frames.append(_Frame())
            try:
                self.bind_pattern(arm.pattern, st)
                if arm.guard is not None:
                    self.coerce(arm.guard, self.value(arm.guard), BOOL, "the match guard")
                t = self.value(arm.body)
            finally:
                self.frames.pop()
            states.append((self.snap(), self.dead))
            j = T.join(out, t)
            if j is None:
                self.mismatch(arm.body, t, out, "the match arms")
            else:
                out = j
        if states:
            self.merge(states)
        return out

    def bare_names(self, p):
        """A lone lowercase name that is no known value is a binding: `match x { n if n > 3 => .., _ => .. }`."""
        if isinstance(p, ast.PathPat) and len(p.path.segments) == 1:
            name = p.path.segments[0].name
            if name != "None" and name not in _GEN_NAMES and self.sym_value([name]) is None and self.find(name)[0] is None \
                    and name[:1].islower():
                return ast.BindPat(p.range, ast.Name(p.range, name))
        return p

    @staticmethod
    def unbind(p):
        """The refutable part of a pattern: `n @ 1..=5` is `1..=5`, a plain `n` is `_`."""
        while isinstance(p, ast.BindPat):
            if p.sub is None:
                return ast.WildPat(p.range)
            p = p.sub
        return p

    def exhaustive(self, e: ast.Match, st: Ty) -> None:
        """KIFP030: enums need every enumerator, `bool` both values, integers the whole range (or a `_` arm)."""
        pats: list = []

        def flat(p) -> None:
            if isinstance(p, ast.OrPat):
                for x in p.alts:
                    flat(self.unbind(x))
            else:
                pats.append(p)

        e = ast.Match(e.range, e.scrutinee, [a for a in e.arms if a.guard is None])
        for arm in e.arms:
            flat(self.unbind(arm.pattern))
        if any(isinstance(p, ast.WildPat) for p in pats):
            return
        if any(isinstance(p, ast.TuplePat) for p in pats):
            if not self.covers([[a.pattern] for a in e.arms], 1):
                self.d.append(dg.error(e.range, "KIFP030", "'match' on a tuple does not cover every combination; add the arm(s) or '_'"))
            return
        t = st
        while t.name == "&":
            t = t.args[0]
        res = {p.path.segments[-1].name for p in pats if isinstance(p, ast.VariantPat)} & {"ok", "err"}
        if res:
            for want in ("ok", "err"):
                full = any(isinstance(p, ast.VariantPat) and p.path.segments[-1].name == want
                           and all(isinstance(a, (ast.BindPat, ast.WildPat)) for a in p.args) for p in pats)
                if not full:
                    self.d.append(dg.error(e.range, "KIFP030", f"'match' on a result must cover 'ok(..)' and 'err(..)' (or have a '_' arm)"))
                    break
            return
        variants = {p.path.segments[-1].name for p in pats if isinstance(p, ast.VariantPat)}
        variants |= {p.path.segments[-1].name for p in pats
                     if isinstance(p, ast.PathPat) and p.path.segments[-1].name == "None" and len(p.path.segments) == 1}
        if variants & {"Some", "None"}:
            # an irrefutable payload (`Some(x)`, `Some(_)`) covers `Some`
            some = any(isinstance(p, ast.VariantPat) and p.path.segments[-1].name == "Some"
                       and all(isinstance(a, (ast.BindPat, ast.WildPat)) for a in p.args) for p in pats)
            if not (some and "None" in variants):
                self.d.append(dg.error(e.range, "KIFP030", "'match' on an Option must cover 'Some(..)' and 'None' (or have a '_' arm)"))
            return
        enum = t.sym if t.sym is not None and t.sym.kind == "enum" else None
        paths = [p.path for p in pats if isinstance(p, ast.PathPat) and len(p.path.segments) > 1]
        if enum is None and t == UNKNOWN and paths:
            cand = self.sym_type("::".join(s.name for s in paths[0].segments[:-1]))
            enum = cand if cand is not None and cand.kind == "enum" else None
        if enum is not None:
            names = {m.name.text for m in enum.node.members}
            covered = set()
            for path in paths:
                tsym = self.sym_type("::".join(s.name for s in path.segments[:-1]))
                if tsym is enum or (tsym is not None and tsym.node is enum.node):
                    covered.add(path.segments[-1].name)
            missing = [m.name.text for m in enum.node.members if m.name.text in names - covered]
            if missing:
                self.d.append(dg.error(e.range, "KIFP030",
                                       f"'match' does not cover {enum.node.name.text}::{', '.join(missing)}; add the arm(s) or '_'"))
            return
        if t == BOOL:
            seen = {p.lit.value for p in pats if isinstance(p, ast.LitPat) and p.lit.kind == "bool"}
            if seen != {True, False}:
                self.d.append(dg.error(e.range, "KIFP030", "'match' on a bool must cover 'true' and 'false' (or have a '_' arm)"))
            return
        if t.name in T.INT_RANGES:
            spans = sorted(
                [(int(p.lit.value),) * 2 for p in pats if isinstance(p, ast.LitPat) and p.lit.kind == "int"]
                + [(int(p.lo.value), int(p.hi.value)) for p in pats if isinstance(p, ast.RangePat)])
            lo, hi = T.INT_RANGES[t.name]
            nxt = lo
            for a, b in spans:
                if a > nxt:
                    break
                nxt = max(nxt, b + 1)
            if nxt <= hi:
                self.d.append(dg.error(e.range, "KIFP030",
                                       f"'match' on {t} does not cover every value (first gap: {nxt}); add a '_' arm"))
            return
        if t == INTLIT or t == STRLIT or any(isinstance(p, ast.LitPat) for p in pats):
            if t != UNKNOWN or any(isinstance(p, ast.LitPat) for p in pats):
                self.d.append(dg.error(e.range, "KIFP030", "'match' on literals needs a '_' arm"))

    def ctor(self, p):
        """(constructor, sub-patterns, all constructors of its type) of a pattern; None for a wildcard, no universe if open."""
        if isinstance(p, ast.LitPat) and p.lit.kind == "bool":
            return ("b", p.lit.value), [], [(("b", True), 0), (("b", False), 0)]
        if isinstance(p, ast.VariantPat):
            tag = p.path.segments[-1].name
            if tag in ("Some", "ok", "err"):
                uni = [(("Some",), 1), (("None",), 0)] if tag == "Some" else [(("ok",), 1), (("err",), 1)]
                return (tag,), p.args, uni
        if isinstance(p, ast.PathPat):
            names = [s.name for s in p.path.segments]
            if names == ["None"]:
                return ("None",), [], [(("Some",), 1), (("None",), 0)]
            if len(names) > 1:
                sym = self.sym_type("::".join(names[:-1]))
                if sym is not None and sym.kind == "enum":
                    return ("e", sym.node.name.text, names[-1]), [], \
                        [(("e", sym.node.name.text, m.name.text), 0) for m in sym.node.members]
        return ("?", id(p)), [], None

    def covers(self, rows: list, n: int) -> bool:
        """Do the pattern rows (n columns each) match every value? Constructor-by-constructor, like a match-compiler."""
        if not rows:
            return False
        if n == 0:
            return True
        flat = []
        for r in rows:
            h = self.unbind(r[0])
            if isinstance(h, ast.OrPat):
                flat += [[self.unbind(a)] + r[1:] for a in h.alts]
            else:
                flat.append([h] + r[1:])
        rows = flat
        wild = (ast.WildPat, ast.BindPat)
        tup = next((r[0] for r in rows if isinstance(r[0], ast.TuplePat)), None)
        if tup is not None:
            k = len(tup.elems)
            return self.covers([(r[0].elems if isinstance(r[0], ast.TuplePat) else [ast.WildPat(r[0].range)] * k) + r[1:]
                                for r in rows], n - 1 + k)
        heads = [None if isinstance(r[0], wild) else self.ctor(r[0]) for r in rows]
        universe = next((h[2] for h in heads if h is not None and h[2] is not None), None)
        if universe is None:
            return self.covers([r[1:] for r, h in zip(rows, heads) if h is None], n - 1)
        for key, arity in universe:
            spec = []
            for r, h in zip(rows, heads):
                if h is None:
                    spec.append([ast.WildPat(r[0].range)] * arity + r[1:])
                elif h[0] == key:
                    spec.append((list(h[1]) + [ast.WildPat(r[0].range)] * arity)[:arity] + r[1:])
            if not self.covers(spec, n - 1 + arity):
                return False
        return True

    def pattern(self, p) -> None:
        if isinstance(p, ast.BindPat):
            if p.sub is not None:
                self.pattern(p.sub)
        elif isinstance(p, ast.PathPat):
            self.e_PathExpr(p.path)
        elif isinstance(p, ast.OrPat):
            for a in p.alts:
                self.pattern(a)
        elif isinstance(p, ast.VariantPat):
            self.e_PathExpr(p.path)
            for a in p.args:
                self.pattern(a)
        elif isinstance(p, ast.TuplePat):
            for a in p.elems:
                self.pattern(a)

    def destructure(self, p, t: Ty) -> None:
        """Declare the names of an irrefutable pattern (`let (a, mut b) = t;`) with the matching parts of `t`."""
        if isinstance(p, ast.BindPat) and p.sub is None:
            self.declare(p.name, _Var(mut=p.mut, ty=t))
        elif isinstance(p, ast.TuplePat):
            if t.cat == "tuple" and len(t.args) != len(p.elems):
                self.d.append(dg.error(p.range, "KIFP012", f"the pattern has {len(p.elems)} element(s), {t} has {len(t.args)}"))
                t = UNKNOWN
            elif t not in (UNKNOWN, NEVER) and t.cat != "tuple":
                self.d.append(dg.error(p.range, "KIFP012", f"a tuple pattern cannot match {t}"))
                t = UNKNOWN
            for i, x in enumerate(p.elems):
                self.destructure(x, t.args[i] if t.cat == "tuple" else UNKNOWN)
        elif not isinstance(p, ast.WildPat):
            self.d.append(dg.error(p.range, "KIFP012", "only names, '_' and tuples can destructure in a 'let'"))

    def bind_pattern(self, p, t: Ty = UNKNOWN) -> None:
        if isinstance(p, ast.BindPat):
            self.declare(p.name, _Var(mut=p.mut, ty=t))
            if p.sub is not None:
                self.bind_pattern(p.sub, t)
        elif isinstance(p, ast.TuplePat):
            for i, x in enumerate(p.elems):
                self.bind_pattern(x, t.args[i] if t.cat == "tuple" and i < len(t.args) else UNKNOWN)
        elif isinstance(p, ast.VariantPat):
            want = p.path.segments[-1].name
            if t.name == "result" and want in ("ok", "err") and len(p.args) == 1:
                inner = t.args[0] if want == "ok" and t.args else (
                    self.ty(ast.PathType(p.range, [ast.PathSeg(p.range, "error")])) if want == "err" else UNKNOWN)
                self.bind_pattern(p.args[0], inner)
                return
            if t.name == "optional" and want == "Some" and len(p.args) == 1:
                self.bind_pattern(p.args[0], t.args[0] if t.args else UNKNOWN)
                return
            for a in p.args:
                self.bind_pattern(a)

    def leaves(self, rng) -> None:
        """KIFP112: the function exits with an `#[enter_atomic]` call still open on this path."""
        if self.dead:
            return
        if self.legacy > 0 or self.legacy_diverged:
            self.d.append(dg.error(rng, "KIFP112", "an atomic scope entered here is not left on every path"))
            self.legacy, self.legacy_diverged = 0, False

    def e_Return(self, e: ast.Return) -> Ty:
        if e.value is not None:
            self.coerce(e.value, self.value(e.value), self.ret, "the returned value")
            self.escapes(e.value, "returns")
        elif self.ret not in (None, UNIT, UNKNOWN):
            self.d.append(dg.error(e.range, "KIFP012", f"'return' without a value in a function returning {self.ret}"))
        self.leaves(e.range)
        self.dead = True
        return NEVER

    def e_Lambda(self, e: ast.Lambda) -> Ty:
        saved_ret, saved_loops = self.ret, self.loops
        s0, dead0 = self.snap(), self.dead
        self.dead = False
        self.ret, self.loops = (self.ty(e.ret) if e.ret is not None else None), []
        self.frames.append(_Frame(lambda_boundary=True))
        params: List[Ty] = []
        try:
            seen: set = set()
            hint, self.lambda_hint = self.lambda_hint, None
            for i, p in enumerate(e.params):
                if p.name.text in seen:
                    self.d.append(dg.error(p.name.range, "KIF010", f"duplicate lambda parameter {p.name.text!r}"))
                seen.add(p.name.text)
                pt = self.ty(p.ty) if p.ty is not None else (hint[i] if hint and i < len(hint) else UNKNOWN)
                params.append(pt)
                self.declare(p.name, _Var(mut=False, ty=pt))
            body = self.expr(e.body)
            if e.ret is not None:
                body = self.ty(e.ret)
        finally:
            self.frames.pop()
            self.ret, self.loops = saved_ret, saved_loops
            self.restore(s0)
            self.dead = dead0
        return Ty("fn", tuple(params) + (UNKNOWN if body == NEVER else body,))


def _lifetimes(t, out: set) -> None:
    """Lifetime names mentioned by a type ("" for an elided reference)."""
    if isinstance(t, ast.RefType):
        out.add(t.lifetime.text if t.lifetime is not None and t.lifetime.text != "'_" else "")
        _lifetimes(t.inner, out)
    elif isinstance(t, ast.Lifetime):
        out.add("" if t.text == "'_" else t.text)
    elif isinstance(t, ast.PathType):
        for seg in t.segments:
            for a in seg.args:
                _lifetimes(a, out)
    elif isinstance(t, (ast.SliceType, ast.ArrayType)):
        _lifetimes(t.elem, out)
    elif isinstance(t, ast.TupleType):
        for x in t.elems:
            _lifetimes(x, out)


def _flow_sources(decl) -> List[int]:
    """Which inputs a call result may borrow from: -1 for the receiver, else the index among the non-receiver parameters."""
    if getattr(decl, "ret", None) is None:
        return []
    names: set = set()
    _lifetimes(decl.ret, names)
    names.discard("'static")
    if not names:
        return []
    out: List[int] = []
    recv = next((p for p in decl.params if p.receiver), None)
    if "" in names:
        if recv is not None and recv.receiver in ("ref", "mut"):
            return [-1]
        if recv is not None and recv.receiver == "value":
            out.append(-1)
        names = {""}
    elif recv is not None and (recv.lifetime is not None and recv.lifetime.text in names or recv.receiver == "value"):
        out.append(-1)
    plain = [p for p in decl.params if not p.receiver]
    for i, p in enumerate(plain):
        mine: set = set()
        _lifetimes(p.ty, mine)
        mine.discard("'static")
        if mine & names:
            out.append(i)
    return out


def check_bodies(mod) -> None:
    BodyChecker(mod).run()
