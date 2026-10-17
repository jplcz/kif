# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Checker step 1: modules, symbol tables, `import`/`use`, duplicate names and type-name resolution.

A file is a module named by its `namespace a::b;` (or its file stem). `import "x.kif";` makes the module reachable
by path (`x::name`); `use x::name;`, `use x::{a, b as c};` and `use x::*;` bring names in unqualified. The shipped
`prelude/reloco.kif` is the one global module: its names are visible everywhere and a file's own names shadow them.
Other shipped modules (`prelude/microfmt.kif`) are found by `import` when the resolver does not know the path.

Codes: KIF010 duplicate name, KIF012 import not found or cyclic, KIFP010 unknown name, KIFP016 private item of another
module, KIFP130 wrong number of generic arguments. Only declarations are examined (signatures, fields, aliases).
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import ast, astutil, cfg, facts, taint
from . import diagnostics as dg
from .parser import parse
from .source import Pos, Range

_ZERO = Range(Pos(0, 0), Pos(0, 0))
PRELUDE_DIR = os.path.join(os.path.dirname(__file__), "prelude")

# Built into the compiler; everything else (str, ptr, vector, ...) is declared by prelude/reloco.kif.
SCALARS = frozenset("bool char void i8 i16 i32 i64 u8 u16 u32 u64 usize isize".split())

# Called as resolver(importing_key, path); returns (key, text) or None when the file does not exist.
FileResolver = Callable[[str, str], Optional[Tuple[str, str]]]


@dataclass
class Symbol:
    kind: str  # struct | enum | interface | foreign | alias | extern_type | block | fn | const
    node: ast.Node
    module: "Module"
    generics: List[ast.GenericParam] = field(default_factory=list)
    cxx: str = ""  # C++ spelling of an extern type or function
    variants: list = field(default_factory=list)  # cfg predicates of every variant of this name (None = unconditional)

    def __post_init__(self) -> None:
        from . import cfg

        self.variants = [cfg.of_attrs(getattr(self.node, "attrs", None) or [])]

    @property
    def private(self) -> bool:
        return bool(getattr(self.node, "private", False))


@dataclass
class Module:
    key: str
    name: str
    types: Dict[str, Symbol] = field(default_factory=dict)  # declared here
    values: Dict[str, Symbol] = field(default_factory=dict)
    scope_types: Dict[str, Symbol] = field(default_factory=dict)  # declared here + `use`d
    scope_values: Dict[str, Symbol] = field(default_factory=dict)
    modules: Dict[str, "Module"] = field(default_factory=dict)  # reachable by path: itself and its imports
    tree: Optional[ast.KifFile] = None
    diagnostics: List[dg.Diagnostic] = field(default_factory=list)
    prelude: Optional["Module"] = None
    source: str = ""  # the text, for `.doc()` in reflection
    cfg: Optional[cfg.Env] = None  # the configuration `#[cfg]`/`cfg(..) { }` are tested against (the interpreter, backends)
    header: Optional[dict] = None  # the `.kifm` header when loaded from a precompiled module (bodies are absent)
    imported: Dict[str, "Module"] = field(default_factory=dict)  # `import` path -> module
    signature_checked: bool = False
    signature: Optional[object] = None  # the `modsign.Verdict` on a `.kifm`'s signature when a signing policy checked it

    @property
    def namespaced(self) -> bool:
        """True when the file declares `namespace a::b;` (its items then default to the C++ spelling `a::b::name`)."""
        return self.tree is not None and any(isinstance(it, ast.Namespace) for it in self.tree.items)


REFLECTION_TYPES = frozenset({"Module", "Interface", "Method", "Param", "Type", "Attr", "Enum", "EnumMember", "Struct", "Cell",
                              "Function", "Field"})


def _module_name(key: str, tree: ast.KifFile) -> str:
    for it in tree.items:
        if isinstance(it, ast.Namespace):
            return "::".join(n.text for n in it.path)
    return os.path.splitext(os.path.basename(key))[0]


@functools.lru_cache(maxsize=1)
def prelude() -> Module:
    return _load("reloco.kif", open(os.path.join(PRELUDE_DIR, "reloco.kif"), encoding="utf-8").read(), None, None, {}, ())


def analyze(key: str, text: str, resolver: Optional[FileResolver] = None, sysroot=None, cfg_env=None) -> Module:
    """`cfg_env` (cfg.Env) is kept on the module for the interpreter and backends, and the checker reads it only for
    the target-dependent rules (C mode, a kobj-less target: cmode.py)."""
    return _analyze(key, text, resolver, sysroot, cfg_env)


def _analyze(key: str, text: str, resolver: Optional[FileResolver], sysroot, cfg_env=None) -> Module:
    """Check one file (and, transitively, what it imports). `key` identifies it (a path); see FileResolver.

    With a `Sysroot` (see sysroot.py) imports come from it alone, the prelude included; `resolver` is then ignored.
    """
    if sysroot is None:
        return _load(key, text, prelude(), resolver, {}, (), cfg_env)
    res = sysroot.resolver()
    try:
        pkey, ptext = sysroot.prelude()
    except Exception as exc:  # SysrootError, ModuleFileError
        mod = _load(key, text, None, res, {}, (), cfg_env)
        mod.diagnostics.append(dg.error(_ZERO, getattr(exc, "code", "KIF014"), str(exc)))
        return mod
    return _load(key, text, _load(pkey, ptext, None, None, {}, ()), res, {}, (), cfg_env)


def _load(key: str, text: str, pre: Optional[Module], resolver: Optional[FileResolver], cache: Dict[str, Module],
          stack: Tuple[str, ...], cfg_env=None) -> Module:
    tree, diags = parse(text)
    mod = Module(key, _module_name(key, tree), tree=tree, diagnostics=list(diags), prelude=pre, source=text)
    mod.cfg = cfg_env
    if tree is not None:
        facts.check_require_proved(mod.diagnostics, tree.inner_attrs)
    header = getattr(resolver, "headers", {}).get(key)
    if header is not None:
        mod.header = header
        from . import modsec

        for problem in modsec.check(tree, header["security"]):
            mod.diagnostics.append(dg.error(_ZERO, "KIF014", problem))
    cache[key] = mod
    mod.modules[mod.name] = mod
    _collect(mod)
    _check_cfg(mod)
    from . import modsec

    modsec.annotate(mod)
    for it in tree.items:
        if isinstance(it, ast.Import):
            _import(mod, it, resolver, cache, stack + (key,))
    for it in tree.items:
        if isinstance(it, ast.Use):
            for e in it.entries:
                _use(mod, e)
    _Resolver(mod).run()
    from .bodies import check_bodies

    check_bodies(mod)
    return mod


def _shipped(path: str) -> Optional[Tuple[str, str]]:
    full = os.path.join(PRELUDE_DIR, os.path.basename(path))
    if os.path.basename(path) == path and os.path.isfile(full):
        return full, open(full, encoding="utf-8").read()
    return None


def _import(mod: Module, imp: ast.Import, resolver: Optional[FileResolver], cache: Dict[str, Module],
            stack: Tuple[str, ...]) -> None:
    if imp.path == "reloco.kif":
        return  # the prelude is always there
    try:
        found = resolver(mod.key, imp.path) if resolver else None
    except Exception as exc:  # SysrootError, ModuleFileError
        mod.diagnostics.append(dg.error(imp.range, getattr(exc, "code", "KIF014"), f"import {imp.path!r}: {exc}"))
        return
    if found is None and not getattr(resolver, "exclusive", False):
        found = _shipped(imp.path)
    if found is None:
        mod.diagnostics.append(dg.error(imp.range, "KIF012", f"import {imp.path!r} not found"))
        return
    key, text = found
    if key in stack:
        mod.diagnostics.append(dg.error(imp.range, "KIF012", f"import cycle through {imp.path!r}"))
        return
    other = cache.get(key) or _load(key, text, mod.prelude, resolver, cache, stack, mod.cfg)
    mod.modules.setdefault(other.name, other)
    mod.imported[imp.path] = other
    policy = getattr(resolver, "signing", None)
    if policy is not None and other.header is not None and not other.signature_checked:
        other.signature_checked = True
        for severity, code, message in policy.evaluate(other.key, other.header):
            other.diagnostics.append(getattr(dg, severity)(imp.range, code, message))
        other.signature = policy.verdicts.get(other.key)
    for d in other.diagnostics:
        if d.code in ("KIF014", "KIF015"):  # a bad or stale precompiled module is the importer's problem too
            mod.diagnostics.append(dg.error(imp.range, d.code, f"{imp.path!r}: {d.message}"))
        elif d.code in ("KIF018", "KIF019", "KIF020", "KIF021"):  # the signing policy covers the whole import tree: say which import led there
            mod.diagnostics.append(getattr(dg, "error" if d.severity == dg.ERROR else "warning")(imp.range, d.code, f"{imp.path!r}: {d.message}"))
    if other.header is not None:
        from . import modsec

        for severity, code, message in modsec.compat(mod.cfg, other.header["security"]):
            mod.diagnostics.append(getattr(dg, severity)(imp.range, code, f"{imp.path!r}: {message}"))
    want = (mod.header or {}).get("deps", {}).get(imp.path)
    if want is not None:
        from .modfile import digest_of

        if digest_of(other) != want:
            mod.diagnostics.append(dg.error(imp.range, "KIF015", f"{imp.path!r} is not the version {mod.name!r} was built against "
                                            "(its interface changed); rebuild or replace the precompiled module"))


def _check_cfg(mod: Module) -> None:
    """KIFP200: every `#[cfg(..)]` and `cfg(..) { }` predicate must be well formed."""
    for n in astutil.walk(mod.tree):
        if isinstance(n, ast.Attribute) and n.name.text == "cfg":
            try:
                cfg.parse_attribute(n)
            except cfg.CfgError as exc:
                mod.diagnostics.append(dg.error(exc.range or n.range, "KIFP200", f"bad cfg predicate: {exc}"))


def _collect(mod: Module) -> None:
    d = mod.diagnostics

    def add(table: Dict[str, Symbol], scope: Dict[str, Symbol], name: str, rng, sym: Symbol, what: str) -> None:
        if name in table:
            old = table[name].variants
            new = sym.variants[0]
            if new is not None and all(p is not None and cfg.exclusive(new, p) for p in old):
                old.append(new)  # a variant for another target: not a duplicate
            else:
                d.append(dg.error(rng, "KIF010", f"duplicate {what} {name!r}"))
        else:
            table[name] = scope[name] = sym

    def cxx_of(attrs, name: str, path: str = "") -> str:
        for a in attrs:
            if a.name.text == "cxx_name" and a.args and a.args[0].value is not None:
                return str(a.args[0].value.value)
        if path and "::" in path:
            return path
        return f"{mod.name}::{name}" if mod.namespaced else name

    T, V = mod.types, mod.values
    ST, SV = mod.scope_types, mod.scope_values
    for it in mod.tree.items:
        if isinstance(it, ast.StructDecl):
            add(T, ST, it.name.text, it.name.range, Symbol("struct", it, mod, it.generics, cxx_of([], it.name.text)), "type")
        elif isinstance(it, ast.EnumDecl):
            add(T, ST, it.name.text, it.name.range, Symbol("enum", it, mod, [], cxx_of([], it.name.text)), "type")
        elif isinstance(it, ast.Interface):
            add(T, ST, it.name.text, it.name.range, Symbol("interface", it, mod), "interface")
        elif isinstance(it, ast.ForeignClass):
            add(T, ST, it.name.text, it.name.range, Symbol("foreign", it, mod), "foreign class")
        elif isinstance(it, ast.BlockDecl):
            add(T, ST, it.name.text, it.name.range, Symbol("block", it, mod), "block")
        elif isinstance(it, ast.TypeAlias):
            add(T, ST, it.name.text, it.name.range, Symbol("alias", it, mod, it.generics), "type")
        elif isinstance(it, ast.ExternType):
            last = it.path.segments[-1]
            add(T, ST, last.name, last.range, Symbol("extern_type", it, mod, it.generics,
                                                     cxx_of(it.attrs, last.name, it.path.text)), "type")
        elif isinstance(it, ast.FnDecl):
            add(V, SV, it.name.text, it.name.range, Symbol("fn", it, mod, it.generics), "function")
        elif isinstance(it, ast.ExternFn):
            n = it.decl.name.text.rsplit("::", 1)[-1]
            add(V, SV, n, it.decl.name.range, Symbol("fn", it, mod, it.decl.generics,
                                                     cxx_of(it.attrs, n, it.decl.name.text)), "function")
        elif isinstance(it, ast.ConstDecl):
            add(V, SV, it.name.text, it.name.range, Symbol("const", it, mod), "constant")
        elif isinstance(it, ast.StaticDecl):
            add(V, SV, it.name.text, it.name.range, Symbol("static", it, mod), "cell")


def _find_module(mod: Module, parts: List[str]) -> Optional[Module]:
    return mod.modules.get("::".join(parts))


def _use(mod: Module, e: ast.UseEntry) -> None:
    d = mod.diagnostics
    parts = [n.text for n in e.path]
    if e.glob:
        src = _find_module(mod, parts)
        if src is None:
            d.append(dg.error(e.range, "KIFP010", f"unknown module {'::'.join(parts)!r} (is it imported?)"))
            return
        for table, scope in ((src.types, mod.scope_types), (src.values, mod.scope_values)):
            for name, sym in table.items():
                if not sym.private:
                    scope.setdefault(name, sym)
        return
    bind = (e.alias or e.path[-1]).text
    whole = _find_module(mod, parts)
    if whole is not None and len(parts) > 0 and "::".join(parts) in mod.modules:
        mod.modules.setdefault(bind, whole)  # `use a::b;` / `use a::b as c;` names a module
        return
    src = _find_module(mod, parts[:-1]) if len(parts) > 1 else None
    if src is None:
        d.append(dg.error(e.range, "KIFP010", f"unknown module in 'use {'::'.join(parts)}' (is it imported?)"))
        return
    name = parts[-1]
    hit = False
    for table, scope in ((src.types, mod.scope_types), (src.values, mod.scope_values)):
        sym = table.get(name)
        if sym is None:
            continue
        hit = True
        if sym.private:
            d.append(dg.error(e.range, "KIFP016", f"{name!r} is private to module {src.name!r}"))
            continue
        if bind in scope and scope[bind] is not sym:
            d.append(dg.error((e.alias or e.path[-1]).range, "KIF010", f"duplicate name {bind!r}"))
        else:
            scope[bind] = sym
    if not hit:
        d.append(dg.error(e.range, "KIFP010", f"module {src.name!r} has no item {name!r}"))


class _Resolver:
    def __init__(self, mod: Module) -> None:
        self.mod = mod
        self.d = mod.diagnostics
        self.scope: List[set] = []
        self.has_self = False

    def run(self) -> None:
        for it in self.mod.tree.items:
            self.item(it)

    # -- scopes

    def names(self, generics: List[ast.GenericParam]) -> set:
        seen: set = set()
        for g in generics:
            for o in g.outlives:
                if o.text != "'static" and o.text not in {x.name.text for x in generics}:
                    self.d.append(dg.error(o.range, "KIFP160", f"undeclared lifetime {o.text}"))
            if g.name.text in seen:
                self.d.append(dg.error(g.name.range, "KIF010", f"duplicate generic parameter {g.name.text!r}"))
            seen.add(g.name.text)
        return seen

    def unique(self, items, what: str, owners=None) -> None:
        """Duplicate names; `owners` (the declarations the names belong to) lets `#[cfg]` variants of one name coexist."""
        seen: Dict[str, list] = {}
        for i, name in enumerate(items):
            pred = cfg.of_attrs(getattr(owners[i], "attrs", None) or []) if owners else None
            old = seen.get(name.text)
            if old is not None:
                if pred is not None and all(p is not None and cfg.exclusive(pred, p) for p in old):
                    old.append(pred)
                else:
                    self.d.append(dg.error(name.range, "KIF010", f"duplicate {what} {name.text!r}"))
            else:
                seen[name.text] = [pred]

    # -- items

    def item(self, it) -> None:
        if isinstance(it, ast.FnDecl):
            self.fn(it)
        elif isinstance(it, ast.ExternFn):
            self.fn(it.decl)
        elif isinstance(it, ast.ConstDecl):
            self.type_(it.ty)
        elif isinstance(it, ast.StaticDecl):
            self.type_(it.ty)
        elif isinstance(it, ast.TypeAlias):
            self.in_generics(it.generics, lambda: self.type_(it.ty))
        elif isinstance(it, ast.StructDecl):
            taint.check_type_attrs(self.d, it.attrs, "a struct")
            for f in it.fields:
                taint.check_type_attrs(self.d, f.attrs, "a field")
            self.unique([f.name for f in it.fields], "field", it.fields)
            self.in_generics(it.generics, lambda: [self.field_type(f.ty) for f in it.fields], True)
        elif isinstance(it, ast.EnumDecl):
            taint.check_type_attrs(self.d, it.attrs, "an enum")
            self.unique([m.name for m in it.members], "enumerator")
            if it.repr:
                self.type_(it.repr)
        elif isinstance(it, ast.Interface):
            self.interface(it)
        elif isinstance(it, ast.ImplDecl):
            self.impl(it)
        elif isinstance(it, ast.ExternImpl):
            facts.check_impl(self.d, it)
            self.in_generics(it.generics, lambda: [self.type_(it.target)] + [self.fn(f) for f in it.fns], True)
        elif isinstance(it, ast.ExternType):
            self.unique([g.name for g in it.generics], "generic parameter")
        elif isinstance(it, ast.BlockDecl):
            self.unique([r.name for r in it.registers], "register")
            for r in it.registers:
                self.type_(r.ty)
                self.unique([f.name for f in r.fields], "field")

    def in_generics(self, generics, fn, self_ok: bool = False) -> None:
        self.scope.append(self.names(generics))
        saved = self.has_self
        self.has_self = self.has_self or self_ok
        try:
            fn()
        finally:
            self.scope.pop()
            self.has_self = saved

    def fn(self, f: ast.FnDecl) -> None:
        facts.check_fn(self, f)
        taint.check_fn(self.d, f)

        def go() -> None:
            self.unique([p.name for p in f.params], "parameter")
            for p in f.params:
                if p.ty is not None:
                    self.type_(p.ty)
            if f.ret:
                self.type_(f.ret)
            for p in f.params:
                if p.lifetime is not None:
                    self.declared(p.lifetime)
            self.elision(f)
            for g in getattr(f, "generics", []):
                for b in g.bounds:
                    self.bound(b)

        self.in_generics(getattr(f, "generics", []), go)

    def interface(self, i: ast.Interface) -> None:
        self.unique([m.name for m in i.methods], "method", i.methods)
        self.in_generics([], lambda: [self.fn(m) for m in i.methods], True)

    def impl(self, i: ast.ImplDecl) -> None:
        facts.check_require_proved(self.d, i.attrs)
        def go() -> None:
            self.type_(i.target)
            if i.iface is not None and self.lookup(i.iface.text)[0] is None:
                self.d.append(dg.error(i.iface.range, "KIFP010", f"unknown interface {i.iface.text!r}"))
            fns = [f for f in i.items if isinstance(f, ast.FnDecl)]
            self.unique([f.name for f in fns], "function", fns)
            for m in i.items:
                if isinstance(m, ast.FnDecl):
                    self.fn(m)
                else:
                    self.type_(m.ty)

        self.in_generics(i.generics, go, True)

    # -- types

    BOUNDS = frozenset("integer signed unsigned copy cloneable eq ord hash".split())

    def bound(self, b: ast.PathType) -> None:
        if b.text not in self.BOUNDS and self.lookup(b.text)[0] is None:
            self.d.append(dg.error(b.range, "KIFP010", f"unknown bound {b.text!r}"))

    def declared(self, lt: ast.Lifetime) -> None:
        if lt.text not in ("'static", "'_") and not any(lt.text in s for s in self.scope):
            self.d.append(dg.error(lt.range, "KIFP160", f"undeclared lifetime {lt.text}; declare it as a generic parameter"))

    def lifetimes(self, t) -> list:
        """Every lifetime position in a type, in order: the name, or None where it is elided (`&T`, `'_`, `view<T>`)."""
        out: list = []
        if isinstance(t, ast.RefType):
            lt = t.lifetime.text if t.lifetime else None
            out.append(None if lt == "'_" else lt)
            out += self.lifetimes(t.inner)
        elif isinstance(t, (ast.ArrayType, ast.SliceType)):
            out += self.lifetimes(t.elem)
        elif isinstance(t, ast.TupleType):
            for e in t.elems:
                out += self.lifetimes(e)
        elif isinstance(t, ast.PathType):
            seg = t.segments[-1]
            given = [a for a in seg.args if isinstance(a, ast.Lifetime)]
            for a in seg.args:
                if isinstance(a, ast.Lifetime):
                    out.append(None if a.text == "'_" else a.text)
                elif not isinstance(a, ast.ConstArg):
                    out += self.lifetimes(a)
            if not given and not any(t.text in s for s in self.scope):
                sym = self.lookup(t.text, t)[0]
                if sym is not None:
                    out += [None] * sum(1 for g in sym.generics if g.is_lifetime)
        return out

    def elision(self, f) -> None:
        """Result lifetimes: an elided one needs `&self`, or exactly one distinct lifetime among the parameters."""
        if f.ret is None or None not in self.lifetimes(f.ret):
            return
        if any(p.receiver in ("ref", "mut") for p in f.params):
            return
        sources: set = set()
        elided = 0
        for p in f.params:
            for lt in self.lifetimes(p.ty) if p.ty is not None else []:
                if lt is None:
                    elided += 1
                else:
                    sources.add(lt)
        if len(sources) + elided != 1:
            what = "no lifetime to borrow from" if not sources and not elided else "several lifetimes to borrow from"
            self.d.append(dg.error(f.ret.range, "KIFP162",
                                   f"cannot elide the result lifetime: {what}; name it, e.g. `&'a T`"))

    def field_type(self, t) -> None:
        if None in self.lifetimes(t):
            self.d.append(dg.error(t.range, "KIFP161", "a reference in a struct field needs a named lifetime, e.g. `&'a T`"))
        self.type_(t)

    def type_(self, t) -> None:
        if isinstance(t, ast.RefType):
            if t.lifetime is not None:
                self.declared(t.lifetime)
            self.type_(t.inner)
        elif isinstance(t, (ast.ArrayType, ast.SliceType)):
            self.type_(t.elem)
        elif isinstance(t, ast.FnType):
            for p in t.params:
                self.type_(p)
            if t.ret:
                self.type_(t.ret)
        elif isinstance(t, ast.TupleType):
            for e in t.elems:
                self.type_(e)
        elif isinstance(t, ast.PathType):
            self.path(t)

    def path(self, t: ast.PathType) -> None:
        for seg in t.segments:
            for a in seg.args:
                if isinstance(a, ast.Lifetime):
                    self.declared(a)
                elif not isinstance(a, ast.ConstArg):
                    self.type_(a)
        name = t.text
        nargs = sum(1 for a in t.segments[-1].args if not isinstance(a, ast.Lifetime))
        if name == "Self":
            if not self.has_self:
                self.d.append(dg.error(t.range, "KIFP010", "'Self' is only available inside a struct, impl or interface"))
            return
        if any(name in s for s in self.scope):
            return
        sym, err = self.lookup(name, t)
        if err:
            self.d.append(dg.error(t.range, err[0], err[1]))
            return
        if sym is None:
            if name in ("Vec", "Option"):  # generation-time built-ins (section 5)
                if nargs != 1:
                    self.arity(t, name, 1, 1, nargs)
                return
            if name in REFLECTION_TYPES:  # opaque reflection handles (section 6.1)
                if nargs:
                    self.arity(t, name, 0, 0, nargs)
                return
            if name in SCALARS:
                if nargs:
                    self.arity(t, name, 0, 0, nargs)
                return
            self.d.append(dg.error(t.range, "KIFP010", f"unknown type {name!r}"))
            return
        params = [g for g in sym.generics if not g.is_lifetime]
        lo = hi = len(params)
        if sym.kind in ("alias", "extern_type"):
            lo = sum(1 for g in params if g.default is None)
        if sym.kind in ("interface", "foreign", "enum", "block") and nargs:
            lo = hi = 0
        if not lo <= nargs <= hi:
            self.arity(t, name, lo, hi, nargs)

    def lookup(self, name: str, t=None):
        """Resolve a type name: own scope, then `use`d names, then the global prelude; `m::T` via imported modules."""
        parts = name.split("::")
        if len(parts) == 1:
            sym = self.mod.scope_types.get(name)
            if sym is None and self.mod.prelude is not None:
                sym = self.mod.prelude.types.get(name)
            return sym, None
        src = self.mod.modules.get("::".join(parts[:-1]))
        if src is None:
            # `extern "C++" type reloco::flat_map<..>` can also be named by its full C++ path
            for scope in (self.mod.scope_types, self.mod.prelude.types if self.mod.prelude else {}):
                for sym in scope.values():
                    if sym.kind == "extern_type" and sym.cxx == name:
                        return sym, None
            return None, None
        sym = src.types.get(parts[-1])
        if sym is not None and sym.private and src is not self.mod:
            return None, ("KIFP016", f"{parts[-1]!r} is private to module {src.name!r}")
        return sym, None

    def arity(self, t: ast.PathType, name: str, lo: int, hi: int, got: int) -> None:
        want = str(lo) if lo == hi else f"{lo} to {hi}"
        self.d.append(dg.error(t.range, "KIFP130", f"{name!r} takes {want} generic argument(s), found {got}"))
