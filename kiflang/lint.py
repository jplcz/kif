# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The .kif linter behind `kif lint`: naming, ABI-shape and hygiene rules (docs/kif-tools.md).

Lint runs on a checked `symbols.Module` and reports `KIFLnnn` diagnostics; the compiler's own `KIFnnn`/`KIFPnnn`
diagnostics are not repeated here. Every rule has a default level (`off`, `warning`, `error`) that `.kiflint` can
change, and every finding can be silenced in the source with a `// kif-lint: disable=KIFLnnn` comment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from . import ast
from . import diagnostics as dg
from . import lexer as lx
from .astutil import attr_name_arg, doc_above, find_attr, has_attr, walk
from .diagnostics import Diagnostic
from .manifest import manifest_of
from .source import Pos, Range
from .symbols import Module

OFF = "off"

# rule -> (default level, what it finds). docs/kif-tools.md has the rationale, the fix and an example for each.
RULES: Dict[str, Tuple[str, str]] = {
    "KIFL001": ("warning", "`#[kobj]` interface name ends in `_if` (the generated C names would repeat it)"),
    "KIFL002": ("warning", "`#[kobj]` method name starts with the interface name (the C symbol would stutter)"),
    "KIFL003": ("warning", "one-letter parameter name in an interface method"),
    "KIFL004": ("warning", "C symbol `<interface>_<method>` longer than `max_symbol_length`"),
    "KIFL005": ("off", "interface, method, struct or enum without a doc comment"),
    "KIFL010": ("warning", "`#[kobj]` interface without `version(..)`"),
    "KIFL011": ("warning", "enum without an explicit underlying type (`enum mode: u8`)"),
    "KIFL012": ("warning", "platform-width type (usize, isize, long, size_t, ...) in a `#[kobj]` method signature"),
    "KIFL013": ("warning", "struct passed or returned by value in a `#[kobj]` method signature"),
    "KIFL014": ("warning", "`override` adds a default body that no ancestor interface had"),
    "KIFL015": ("warning", "`#[flags]` enumerator that is not 0, a single bit, or a union of the enum's single bits"),
    "KIFL016": ("warning", "`#[flags]` enum without a zero enumerator"),
    "KIFL017": ("warning", "`#[flags]` enumerators with the same value"),
    "KIFL020": ("error", "baseline: a `#[kobj]` interface of the released manifest is gone"),
    "KIFL021": ("error", "baseline: a method was removed without raising `version`"),
    "KIFL022": ("error", "baseline: a method's signature hash changed (types, static-ness, errno, ABI attributes) without raising `version`"),
    "KIFL023": ("error", "baseline: a method was added without a default and without raising `version`"),
    "KIFL024": ("error", "baseline: `version` went down"),
    "KIFL025": ("off", "baseline: methods were added compatibly but `version` is unchanged"),
    "KIFL026": ("error", "baseline: `extends(..)` changed"),
    "KIFL027": ("error", "baseline: an enum or struct of the released manifest is gone"),
    "KIFL028": ("error", "baseline: enum or struct layout changed incompatibly"),
    "KIFL029": ("error", "baseline: a method lost its default without raising `version`"),
    "KIFL033": ("error", "baseline: the interface's `self(..)` handle type changed without raising `version`"),
    "KIFL030": ("warning", "`private` function that nothing in the file refers to"),
    "KIFL031": ("off", "`let` that shadows a binding already visible in the function"),
    "KIFL032": ("off", "`unsafe` block with more than `max_unsafe_statements` statements"),
}

WIDE_C = re.compile(r"^(long|size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|off_t|kif_unsigned_long|kif_long)$")
DISABLE = re.compile(r"//.*?kif-lint:\s*(disable|disable-file)\s*=\s*([A-Za-z0-9_, ]+)")


@dataclass
class Config:
    levels: Dict[str, str] = field(default_factory=dict)  # rule -> off|warning|error
    max_symbol_length: int = 40
    max_unsafe_statements: int = 5

    def level(self, rule: str) -> str:
        return self.levels.get(rule, RULES[rule][0])


def parse_config(text: str, source: str = ".kiflint") -> Config:
    """`KIFL001 = off|warning|error`, `max_symbol_length = N`, `max_unsafe_statements = N`; `#` starts a comment."""
    cfg = Config()
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, eq, value = (x.strip() for x in line.partition("="))
        if not eq:
            raise ValueError(f"{source}:{n}: expected 'name = value'")
        if key in ("max_symbol_length", "max_unsafe_statements"):
            if not value.isdigit() or int(value) < 1:
                raise ValueError(f"{source}:{n}: {key} must be a positive integer")
            setattr(cfg, key, int(value))
        elif key in RULES:
            if value not in (OFF, "warning", "error"):
                raise ValueError(f"{source}:{n}: level of {key} must be off, warning or error")
            cfg.levels[key] = value
        else:
            raise ValueError(f"{source}:{n}: unknown rule or option {key!r}")
    return cfg


def find_config(path: str) -> Optional[str]:
    """The nearest `.kiflint` in the directory of `path` or above it."""
    d = os.path.dirname(os.path.realpath(path))
    while True:
        c = os.path.join(d, ".kiflint")
        if os.path.isfile(c):
            return c
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


class _Sink:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.out: List[Diagnostic] = []

    def add(self, rule: str, rng: Range, message: str) -> None:
        level = self.cfg.level(rule)
        if level != OFF:
            make: Callable = dg.error if level == "error" else dg.warning
            self.out.append(make(rng, rule, message))


def _wide(t: ast.Type) -> Optional[str]:
    for p in walk(t):
        if isinstance(p, ast.PathType):
            name = p.segments[-1].name
            if name in ("usize", "isize") or WIDE_C.match(name):
                return name
    return None


def _by_value_struct(mod: Module, t: Optional[ast.Type]) -> Optional[str]:
    """The struct named by `t` when `t` is that struct itself (not a reference, pointer or other wrapper)."""
    if not isinstance(t, ast.PathType) or len(t.segments) != 1 or t.segments[0].args:
        return None
    sym = mod.scope_types.get(t.segments[0].name)
    return t.segments[0].name if sym is not None and sym.kind == "struct" else None


def _is_default(m: ast.Method) -> bool:
    return m.body is not None or m.has_default or m.default_sym is not None or has_attr(m.attrs, "provided")


def _ancestors(mod: Module, i: ast.Interface) -> List[ast.Interface]:
    out: List[ast.Interface] = []
    seen = {id(i)}
    cur = i
    while (base := attr_name_arg(cur.attrs, "extends")) is not None:
        sym = mod.scope_types.get(base)
        if sym is None or not isinstance(sym.node, ast.Interface) or id(sym.node) in seen:
            break
        seen.add(id(sym.node))
        out.append(sym.node)
        cur = sym.node
    return out


def enum_values(e: ast.EnumDecl) -> Optional[List[int]]:
    """The values of the enumerators when all are literals or implicit (previous + 1), else None."""
    vals: List[int] = []
    nxt = 0
    for m in e.members:
        if m.value is None:
            v = nxt
        elif isinstance(m.value, ast.Lit) and m.value.kind == "int":
            v = int(m.value.value)
        else:
            return None
        vals.append(v)
        nxt = v + 1
    return vals


def _interfaces(mod: Module, sink: _Sink, lines: List[str]) -> None:
    for i in mod.tree.of(ast.Interface):
        name = i.name.text
        kobj = has_attr(i.attrs, "kobj")
        c_visible = kobj and not has_attr(i.attrs, "cxx_only")
        if kobj and name.endswith("_if"):
            sink.add("KIFL001", i.name.range, f"interface {name!r} ends in '_if'; the generated names append it themselves")
        if kobj and find_attr(i.attrs, "version") is None:
            sink.add("KIFL010", i.name.range, f"interface {name!r} declares no version(..); state it so a later bump is visible")
        if not doc_above(lines, i):
            sink.add("KIFL005", i.name.range, f"interface {name!r} has no doc comment")
        ancestors = _ancestors(mod, i)
        for m in i.methods:
            mname = m.name.text
            if not m.override and not doc_above(lines, m):
                sink.add("KIFL005", m.name.range, f"method {name}.{mname} has no doc comment")
            if kobj and mname.startswith(name.removesuffix("_if") + "_"):
                sink.add("KIFL002", m.name.range, f"method {mname!r} repeats the interface name; the C symbol would be {name}_{mname}")
            if c_visible and not m.override and len(f"{name}_{mname}") > sink.cfg.max_symbol_length:
                sink.add("KIFL004", m.name.range, f"C symbol {name}_{mname} is longer than {sink.cfg.max_symbol_length} characters")
            if m.override and _is_default(m) and not any(_is_default(x) for a in ancestors for x in a.methods if x.name.text == mname):
                sink.add("KIFL014", m.name.range, f"'override' gives {mname!r} a default its bases lack; a handle of {name!r} cannot be converted to theirs")
            for p in m.params:
                if not p.receiver and len(p.name.text) == 1:
                    sink.add("KIFL003", p.name.range, f"parameter {p.name.text!r} of {name}.{mname} is a single letter")
            if not kobj:
                continue
            sig = [p.ty for p in m.params if p.ty is not None] + ([m.ret] if m.ret is not None else [])
            for t in sig:
                if (w := _wide(t)) is not None:
                    sink.add("KIFL012", t.range, f"{w} in {name}.{mname} has a different size on other targets; use u32/u64 or i32/i64")
                if (s := _by_value_struct(mod, t)) is not None:
                    sink.add("KIFL013", t.range, f"struct {s!r} is passed by value in {name}.{mname}; use c_ptr<{s}> or &{s} so its size is not part of the ABI")


def _enums(mod: Module, sink: _Sink, lines: List[str]) -> None:
    for e in mod.tree.of(ast.EnumDecl):
        if e.repr is None:
            sink.add("KIFL011", e.name.range, f"enum {e.name.text!r} has no underlying type (i32 is assumed); write 'enum {e.name.text}: u8'")
        if not doc_above(lines, e):
            sink.add("KIFL005", e.name.range, f"enum {e.name.text!r} has no doc comment")
        if has_attr(e.attrs, "flags"):
            _flags(e, sink)
    for s in mod.tree.of(ast.StructDecl):
        if not doc_above(lines, s):
            sink.add("KIFL005", s.name.range, f"struct {s.name.text!r} has no doc comment")


def _flags(e: ast.EnumDecl, sink: _Sink) -> None:
    vals = enum_values(e)
    if vals is None:
        return
    single = 0
    for v in vals:
        if v > 0 and v & (v - 1) == 0:
            single |= v
    seen: Dict[int, str] = {}
    for m, v in zip(e.members, vals):
        if v & ~single:
            sink.add("KIFL015", m.name.range, f"flag {m.name.text!r} = {v} has bits that no single-bit enumerator of {e.name.text!r} names")
        if v in seen:
            sink.add("KIFL017", m.name.range, f"flag {m.name.text!r} has the same value as {seen[v]!r}")
        seen.setdefault(v, m.name.text)
    if 0 not in seen:
        sink.add("KIFL016", e.name.range, f"flags enum {e.name.text!r} has no zero enumerator; add 'none = 0'")


def _fns(mod: Module) -> Iterator[ast.FnDecl]:
    """Every function with a body in the file: free functions and the ones in `impl` blocks."""
    for it in mod.tree.items:
        if isinstance(it, ast.FnDecl) and it.body is not None:
            yield it
        elif isinstance(it, ast.ImplDecl):
            yield from (f for f in it.items if isinstance(f, ast.FnDecl) and f.body is not None)


def _unused_private(mod: Module, sink: _Sink) -> None:
    refs: Set[str] = set()
    for n in walk(mod.tree.items):
        if isinstance(n, ast.PathExpr):
            refs.update(s.name for s in n.segments)
        elif isinstance(n, ast.MethodCall):
            refs.add(n.name.text)
    for f in _fns(mod):
        if f.private and f.name.text not in refs and not has_attr(f.attrs, "export") and f.name.text != "main":
            sink.add("KIFL030", f.name.range, f"private function {f.name.text!r} is never used in this file")


def _shadowing(mod: Module, sink: _Sink) -> None:
    def visit(n, scopes: List[Set[str]]) -> None:
        if isinstance(n, list):
            for x in n:
                visit(x, scopes)
        elif isinstance(n, ast.Block):
            scopes = scopes + [set()]
            for s in n.stmts:
                if isinstance(s, ast.Let):
                    visit(s.init, scopes)  # the initialiser still sees the old binding
                    if s.name.text != "_" and any(s.name.text in sc for sc in scopes):
                        sink.add("KIFL031", s.name.range, f"'let {s.name.text}' shadows an earlier binding")
                    scopes[-1].add(s.name.text)
                else:
                    visit(s, scopes)
            visit(n.tail, scopes)
        elif isinstance(n, ast.Node) and not isinstance(n, ast.ItemStmt):  # a nested item is its own scope
            for f in n.__dataclass_fields__:
                visit(getattr(n, f), scopes)

    for f in _fns(mod):
        visit(f.body, [{p.name.text for p in f.params}])


def _big_unsafe(mod: Module, sink: _Sink) -> None:
    for n in walk(mod.tree.items):
        if isinstance(n, ast.UnsafeBlock):
            count = len(n.body.stmts) + (n.body.tail is not None)
            if count > sink.cfg.max_unsafe_statements:
                sink.add("KIFL032", n.range, f"unsafe block of {count} statements; keep it to "
                         f"{sink.cfg.max_unsafe_statements} or fewer so the unsafe part is easy to review")


def _type_name(t) -> str:
    segs = getattr(t, "segments", None)
    return segs[-1].name if segs else ""


def _baseline(mod: Module, base: dict, sink: _Sink) -> None:
    """KIFL020-029 and KIFL033: the working copy against the released manifest (docs/kif-abi.md)."""
    now = manifest_of(mod)
    zero = Range(Pos(0, 0), Pos(0, 0))
    here = {i.name.text: i for i in mod.tree.of(ast.Interface)}
    for name, old in base["interfaces"].items():
        cur = now["interfaces"].get(name)
        if cur is None:
            sink.add("KIFL020", zero, f"interface {name!r} is gone or no longer #[kobj] (it is in the baseline {base['file']})")
            continue
        node = here[name]
        rng = node.name.range
        old_version, new_version = old.get("version", 1), cur["version"]
        if new_version < old_version:
            sink.add("KIFL024", rng, f"version of {name!r} went from {old_version} down to {new_version}")
        if cur["extends"] != old.get("extends"):
            sink.add("KIFL026", rng, f"{name!r} extended {old.get('extends')!r} in the baseline, now {cur['extends']!r}")
        bumped = new_version != old_version  # a lowered version is KIFL024 alone; its hashes differ by construction
        if cur["self"] != old.get("self") and not bumped:
            sink.add("KIFL033", rng, f"handle type of {name!r} was self({old.get('self')}), now self({cur['self']}); raise version")
        methods = {m.name.text: m for m in node.methods}
        old_sigs, old_defaults = old.get("signatures", {}), set(old.get("defaults", []))
        for mname, sig in old_sigs.items():
            if mname not in cur["signatures"]:
                if not bumped:
                    sink.add("KIFL021", rng, f"method {name}.{mname} was removed without raising 'version'")
            elif bumped:
                continue
            elif cur["signatures"][mname] != sig:
                sink.add("KIFL022", methods[mname].name.range, f"signature of {name}.{mname} changed without raising 'version'")
            elif mname in old_defaults and mname not in cur["defaults"]:
                sink.add("KIFL029", methods[mname].name.range, f"{name}.{mname} lost its default without raising 'version'")
        added = [m for m in cur["signatures"] if m not in old_sigs]
        for mname in added:
            if mname not in cur["defaults"] and not bumped:
                sink.add("KIFL023", methods[mname].name.range, f"new method {name}.{mname} has no default: classes built against the baseline no longer satisfy {name!r}")
        if added and new_version == old_version:
            sink.add("KIFL025", rng, f"{name!r} gained {', '.join(added)} but 'version' is still {new_version}")
    nodes = {e.name.text: e for e in mod.tree.of(ast.EnumDecl)}
    nodes.update({s.name.text: s for s in mod.tree.of(ast.StructDecl)})
    for name, old_layout in base["layouts"].items():
        if name not in now["layouts"]:
            sink.add("KIFL027", zero, f"type {name!r} is gone (it is in the baseline {base['file']})")
            continue
        new_layout = now["layouts"][name]
        # Appending enumerators is the only compatible change; everything else moves or reinterprets existing values.
        appended = base["types"].get(name) == "enum" and now["types"][name] == "enum" and new_layout.startswith(old_layout + ",")
        if new_layout != old_layout and not appended:
            sink.add("KIFL028", nodes[name].name.range, f"layout of {name!r} changed: was {old_layout!r}, now {new_layout!r}")


def _suppressed(lines: List[str], d: Diagnostic) -> bool:
    def codes(m: "re.Match[str]") -> List[str]:
        return [c.strip() for c in m.group(2).split(",")]

    for ln in lines:
        m = DISABLE.search(ln)
        if m and m.group(1) == "disable-file" and d.code in codes(m):
            return True
    candidates = [d.range.start.line]
    j = d.range.start.line - 1
    while j >= 0 and (lx.comment_text(lines[j]) is not None or lines[j].lstrip().startswith("#[")):
        candidates.append(j)
        j -= 1
    for k in candidates:
        m = DISABLE.search(lines[k])
        if m and m.group(1) == "disable" and d.code in codes(m):
            return True
    return False


def lint(mod: Module, cfg: Optional[Config] = None, baseline: Optional[dict] = None) -> List[Diagnostic]:
    """`KIFLnnn` diagnostics for the declarations of `mod`, minus the ones a `// kif-lint:` comment silences.

    `baseline` is a loaded ABI manifest (`manifest.loads`); with it the baseline rules KIFL020-029 and KIFL033 run too."""
    sink = _Sink(cfg or Config())
    lines = mod.source.split("\n")
    _interfaces(mod, sink, lines)
    _enums(mod, sink, lines)
    _unused_private(mod, sink)
    _shadowing(mod, sink)
    _big_unsafe(mod, sink)
    if baseline is not None:
        _baseline(mod, baseline, sink)
    return sorted((d for d in sink.out if not _suppressed(lines, d)), key=lambda d: (d.range.start, d.code))
