# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Quick-fix engine: turns a diagnostic into text edits (docs/kif-tools.md).

The engine is independent of any editor: `fixes_for` lists the fixes of one diagnostic, `apply_edits` applies a set of
non-overlapping edits to a text, and `fix_all` repeatedly applies the *auto* fixes (the ones that cannot change what a
file means) until nothing is left. `kif fix` is the command line, an editor integration would map `Fix` to a code
action. One function per diagnostic code is registered with `@fixer("KIFxxx")`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import ast
from . import diagnostics as dg
from . import lexer as lx
from .astutil import decl_line, find_attr, has_attr, offset_of, slice_text, walk
from .diagnostics import Diagnostic
from .lint import Config, lint
from .source import Pos, Range
from .symbols import Module, SCALARS, analyze

KEYWORD_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Edit:
    """Replace `range` with `text` (an insertion has an empty range)."""

    range: Range
    text: str


@dataclass
class Fix:
    title: str
    edits: List[Edit]
    code: str = ""
    # The best fix for its diagnostic: `kif fix --unsafe` applies it.
    preferred: bool = False
    # Cannot change what the file means (adding a stated default, fixing a typo-free spelling): `kif fix` applies it unattended.
    auto: bool = False


@dataclass
class Context:
    mod: Module
    lines: List[str]
    diags: List[Diagnostic]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def at(self, r: Range) -> str:
        return slice_text(self.lines, r)

    def indent_of(self, line: int) -> str:
        s = self.lines[line]
        return s[: len(s) - len(s.lstrip())]

    def taken_names(self) -> set:
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", self.text))


Fixer = Callable[[Context, Diagnostic], List[Fix]]
FIXERS: Dict[str, List[Fixer]] = {}


def fixer(*codes: str) -> Callable[[Fixer], Fixer]:
    def reg(fn: Fixer) -> Fixer:
        for c in codes:
            FIXERS.setdefault(c, []).append(fn)
        return fn

    return reg


# ---------------------------------------------------------------- applying edits


class ConflictError(ValueError):
    """Two edits touch the same text."""


def apply_edits(text: str, edits: Iterable[Edit]) -> str:
    """Apply non-overlapping edits (positions refer to the original text). Insertions at one point keep their order."""
    lines = text.split("\n")
    spans = sorted(((offset_of(lines, e.range.start), offset_of(lines, e.range.end), n, e.text) for n, e in enumerate(edits)))
    out, at = [], 0
    for start, end, _n, new in spans:
        if start < at:
            raise ConflictError(f"overlapping edits at offset {start}")
        out += [text[at:start], new]
        at = end
    out.append(text[at:])
    return "".join(out)


def _overlap(a: List[Edit], b: List[Edit]) -> bool:
    for x in a:
        for y in b:
            if x.range.start < y.range.end and y.range.start < x.range.end:
                return True
            if x.range.start == y.range.start and (x.range.start != x.range.end or y.range.start != y.range.end):
                return True
    return False


# ---------------------------------------------------------------- helpers


def _end_of(lines: List[str]) -> Pos:
    return Pos(len(lines) - 1, len(lines[-1]))


def _header_end(mod: Module) -> Pos:
    """Where a new `use`/`import` goes: the line after the last of them (or `namespace`/`include`), else the top."""
    last = [i.range.end for i in mod.tree.items if isinstance(i, (ast.Import, ast.Use, ast.Include, ast.Namespace))]
    return Pos(max(last).line + 1, 0) if last else Pos(0, 0)


def _insert(pos: Pos, text: str) -> Edit:
    return Edit(Range(pos, pos), text)


def _delete_lines(ctx: Context, r: Range) -> Edit:
    """Delete `r`; when it is all that is on its lines, delete the lines (and one blank line after)."""
    if not ctx.lines[r.start.line][: r.start.col].strip() and not ctx.lines[r.end.line][r.end.col :].strip():
        end = r.end.line + 1
        if end < len(ctx.lines) and not ctx.lines[end].strip():
            end += 1
        return Edit(Range(Pos(r.start.line, 0), Pos(min(end, len(ctx.lines) - 1), 0 if end < len(ctx.lines) else len(ctx.lines[-1]))), "")
    return Edit(r, "")


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _declared(ctx: Context) -> Dict[str, Tuple[str, str]]:
    """name -> (kind, module path) of every public type and value reachable by `use` from another module."""
    out: Dict[str, Tuple[str, str]] = {}
    for m in ctx.mod.modules.values():
        if m is ctx.mod:
            continue
        for table in (m.types, m.values):
            for name, sym in table.items():
                if not sym.private:
                    out.setdefault(name, (sym.kind, m.name))
    return out


# C spellings people reach for, and the kif type that is meant.
C_TYPES = {"int": "i32", "unsigned": "u32", "uint": "u32", "short": "i16", "ushort": "u16", "long": "i64", "ulong": "u64",
           "int8_t": "i8", "int16_t": "i16", "int32_t": "i32", "int64_t": "i64", "uint8_t": "u8", "uint16_t": "u16",
           "uint32_t": "u32", "uint64_t": "u64", "size_t": "usize", "ssize_t": "isize", "uintptr_t": "usize",
           "intptr_t": "isize", "byte": "u8", "uchar": "u8", "boolean": "bool", "string": "str"}


# ---------------------------------------------------------------- compiler diagnostics


@fixer("KIFP010")
def _unknown_name(ctx: Context, d: Diagnostic) -> List[Fix]:
    name = ctx.at(d.range)
    if not KEYWORD_OK.match(name):
        return []
    fixes: List[Fix] = []
    if name in C_TYPES and "type" in d.message:
        fixes.append(Fix(f"Use the kif type '{C_TYPES[name]}'", [Edit(d.range, C_TYPES[name])], "KIFP010", preferred=True))
    if name in (found := _declared(ctx)):
        kind, owner = found[name]
        fixes.append(Fix(f"Add 'use {owner}::{name};' ({kind} declared in {owner})", [_insert(_header_end(ctx.mod), f"use {owner}::{name};\n")],
                         "KIFP010", preferred=not fixes))
    scope = set(ctx.mod.scope_types) | set(ctx.mod.scope_values) | (set(ctx.mod.prelude.types) if ctx.mod.prelude else set())
    if "type" in d.message:
        scope |= SCALARS
    close = sorted((c for c in scope if c != name and _distance(name, c) <= max(1, len(name) // 4)), key=lambda c: (_distance(name, c), c))
    fixes += [Fix(f"Did you mean '{c}'?", [Edit(d.range, c)], "KIFP010", preferred=not fixes and n == 0) for n, c in enumerate(close[:3])]
    return fixes


@fixer("KIF010")
def _duplicate(ctx: Context, d: Diagnostic) -> List[Fix]:
    name = ctx.at(d.range)
    if not KEYWORD_OK.match(name):
        return []
    taken, n = ctx.taken_names(), 2
    while f"{name}_{n}" in taken:
        n += 1
    return [Fix(f"Rename to '{name}_{n}'", [Edit(d.range, f"{name}_{n}")], "KIF010")]


@fixer("KIF012")
def _import_missing(ctx: Context, d: Diagnostic) -> List[Fix]:
    return [Fix("Remove this import", [_delete_lines(ctx, d.range)], "KIF012")]


@fixer("KIF001")
def _missing_semicolon(ctx: Context, d: Diagnostic) -> List[Fix]:
    if not d.message.startswith("expected ';'"):
        return []
    tokens, _ = lx.lex(ctx.text)
    before = [t for t in tokens if t.range.end <= d.range.start and t.kind != lx.EOF]
    if not before:
        return []
    return [Fix("Insert ';'", [_insert(before[-1].range.end, ";")], "KIF001", preferred=True)]


def _raw_hooks(ctx: Context, d: Diagnostic) -> Tuple[Optional[ast.Verbatim], List[ast.Verbatim]]:
    """The `m_prolog`/`m_epilog` block `d` is about, and the earlier blocks of the same kind just before the same method."""
    for it in ctx.mod.tree.of(ast.Interface):
        run: List[ast.Verbatim] = []
        for m in it.members:
            if isinstance(m, ast.Method):
                run = []
            elif isinstance(m, ast.Verbatim) and m.kind in ("m_prolog", "m_epilog"):
                if m.keyword_range == d.range:
                    return m, [x for x in run if x.kind == m.kind]
                run.append(m)
    return None, []


@fixer("KIF088")
def _dangling_hook(ctx: Context, d: Diagnostic) -> List[Fix]:
    hook, _ = _raw_hooks(ctx, d)
    if hook is None:
        return []
    return [Fix(f"Remove the {hook.kind} block (no method follows it)", [_delete_lines(ctx, hook.range)], "KIF088", preferred=True)]


@fixer("KIF089")
def _repeated_hook(ctx: Context, d: Diagnostic) -> List[Fix]:
    hook, earlier = _raw_hooks(ctx, d)
    if hook is None or not earlier:
        return []
    return [
        Fix(f"Remove the earlier {hook.kind} block (it is replaced by this one)", [_delete_lines(ctx, earlier[-1].range)], "KIF089", preferred=True),
        Fix(f"Remove this {hook.kind} block (keep the earlier one)", [_delete_lines(ctx, hook.range)], "KIF089"),
    ]


@fixer("KIFP020")
def _make_mut(ctx: Context, d: Diagnostic) -> List[Fix]:
    m = re.search(r"variable '([^']+)'", d.message) or re.search(r"cannot \w+ '([^']+)'", d.message)
    if m is None:
        return []
    name = m.group(1)
    lets = [n for n in walk(ctx.mod.tree.items) if isinstance(n, ast.Let) and n.name.text == name and n.range.end <= d.range.start]
    if not lets:
        return []
    let = max(lets, key=lambda n: n.range.start)
    if let.mut:
        return []
    return [Fix(f"Declare '{name}' with 'let mut'", [_insert(let.name.range.start, "mut ")], "KIFP020", preferred=True)]


# ---------------------------------------------------------------- lint diagnostics


def _node_at(ctx: Context, cls, r: Range):
    return next((n for n in walk(ctx.mod.tree.items) if isinstance(n, cls) and n.name.range == r), None)


def _rename_all(ctx: Context, old: str, new: str) -> List[Edit]:
    tokens, _ = lx.lex(ctx.text)
    return [Edit(t.range, new) for t in tokens if t.kind == lx.IDENT and t.text == old]


@fixer("KIFL001")
def _drop_if(ctx: Context, d: Diagnostic) -> List[Fix]:
    old = ctx.at(d.range)
    new = old.removesuffix("_if")
    if not new or new in ctx.taken_names():
        return []
    return [Fix(f"Rename '{old}' to '{new}' in this file", _rename_all(ctx, old, new), "KIFL001")]


@fixer("KIFL002")
def _strip_prefix(ctx: Context, d: Diagnostic) -> List[Fix]:
    m = _node_at(ctx, ast.Method, d.range)
    owner = next((i for i in ctx.mod.tree.of(ast.Interface) if m is not None and m in i.members), None)
    if m is None or owner is None:
        return []
    new = m.name.text[len(owner.name.text.removesuffix("_if")) + 1 :]
    if not KEYWORD_OK.match(new) or new in {x.name.text for x in owner.methods}:
        return []
    return [Fix(f"Rename method to '{new}' (callers and implementers elsewhere must follow)", [Edit(d.range, new)], "KIFL002")]


@fixer("KIFL010")
def _add_version(ctx: Context, d: Diagnostic) -> List[Fix]:
    i = _node_at(ctx, ast.Interface, d.range)
    a = find_attr(i.attrs, "kobj") if i is not None else None
    if a is None:
        return []
    # An absent version means 1, so stating it changes nothing.
    return [Fix("Add 'version(1)'", [_insert(a.range.end, ", version(1)")], "KIFL010", preferred=True, auto=True)]


@fixer("KIFL011")
def _enum_repr(ctx: Context, d: Diagnostic) -> List[Fix]:
    # i32 is what an enum without a type already is, so spelling it out changes nothing.
    fixes = [Fix("State the underlying type 'i32' (the current default)", [_insert(d.range.end, ": i32")], "KIFL011", preferred=True, auto=True)]
    e = _node_at(ctx, ast.EnumDecl, d.range)
    if e is not None:
        vals = [v for v in ((m.value.value if isinstance(m.value, ast.Lit) else 0) for m in e.members) if isinstance(v, int)]
        if vals and max(vals) < 256 and min(vals) >= 0:
            fixes.append(Fix("Use 'u8' (every value fits; changes the size)", [_insert(d.range.end, ": u8")], "KIFL011"))
    return fixes


@fixer("KIFL016")
def _add_none(ctx: Context, d: Diagnostic) -> List[Fix]:
    e = _node_at(ctx, ast.EnumDecl, d.range)
    if e is None or not e.members:
        return []
    names = ctx.taken_names()
    name = "none" if "none" not in names else "none_"
    first = e.members[0]
    same_line = first.range.start.line == e.range.start.line
    text = f"{name} = 0, " if same_line else f"{name} = 0,\n{ctx.indent_of(first.range.start.line)}"
    return [Fix(f"Add '{name} = 0'", [_insert(first.range.start, text)], "KIFL016", preferred=True)]


@fixer("KIFL005")
def _add_doc(ctx: Context, d: Diagnostic) -> List[Fix]:
    node = next((n for n in walk(ctx.mod.tree.items) if isinstance(n, (ast.Interface, ast.Method, ast.StructDecl, ast.EnumDecl))
                 and n.name.range == d.range), None)
    if node is None:
        return []
    ln = decl_line(ctx.lines, node)
    return [Fix(f"Add a doc comment for '{node.name.text}'", [_insert(Pos(ln, 0), f"{ctx.indent_of(ln)}// TODO: document {node.name.text}.\n")], "KIFL005")]


@fixer("KIFL030")
def _remove_unused(ctx: Context, d: Diagnostic) -> List[Fix]:
    f = _node_at(ctx, ast.FnDecl, d.range)
    if f is None:
        return []
    start = Pos(decl_line(ctx.lines, f), 0) if ctx.lines[f.range.start.line][: f.range.start.col].strip() in ("", "private") else f.range.start
    return [Fix(f"Remove '{f.name.text}'", [_delete_lines(ctx, Range(start, f.range.end))], "KIFL030")]


@fixer(*[f"KIFL{n:03d}" for n in (1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15, 16, 17, 30, 31, 32)])
def _suppress(ctx: Context, d: Diagnostic) -> List[Fix]:
    line = d.range.start.line
    here = Edit(Range(Pos(line, 0), Pos(line, 0)), f"{ctx.indent_of(line)}// kif-lint: disable={d.code}\n")
    top = Edit(Range(Pos(0, 0), Pos(0, 0)), f"// kif-lint: disable-file={d.code}\n")
    return [Fix(f"Suppress {d.code} for this declaration", [here], d.code), Fix(f"Suppress {d.code} in this file", [top], d.code)]


# ---------------------------------------------------------------- the engine


def check(key: str, text: str, cfg: Optional[Config] = None, resolver=None, sysroot=None,
          baseline: Optional[dict] = None) -> Tuple[Module, List[Diagnostic]]:
    """The module and every diagnostic: compiler ones, plus lint ones when the file has no errors."""
    mod = analyze(key, text, resolver, sysroot)
    diags = sorted(mod.diagnostics, key=lambda d: (d.range.start, d.code))
    if not any(d.severity == dg.ERROR for d in diags):
        diags += lint(mod, cfg, baseline)
    return mod, diags


def fixes_for(mod: Module, diag: Diagnostic, diags: Optional[List[Diagnostic]] = None) -> List[Fix]:
    """Every fix offered for `diag`, best first (preferred ones before the rest, each group in registration order)."""
    ctx = Context(mod, mod.source.split("\n"), mod.diagnostics if diags is None else diags)
    out = [f for fn in FIXERS.get(diag.code, []) for f in fn(ctx, diag)]
    return [f for f in out if f.preferred] + [f for f in out if not f.preferred]


def pending(mod: Module, diags: List[Diagnostic]) -> List[Tuple[Diagnostic, Fix]]:
    return [(d, f) for d in diags for f in fixes_for(mod, d, diags)]


def fix_all(key: str, text: str, cfg: Optional[Config] = None, resolver=None, sysroot=None, unsafe: bool = False,
            max_rounds: int = 10) -> Tuple[str, List[Tuple[Diagnostic, Fix]]]:
    """Apply the auto fixes (plus the preferred ones with `unsafe`) until none is left; returns the new text and what was applied.

    Each round applies a set of non-overlapping fixes to the text, then checks it again. A round that would add a
    compiler error is thrown away and its fixes are not tried again.
    """
    applied: List[Tuple[Diagnostic, Fix]] = []
    banned: set = set()
    for _ in range(max_rounds):
        mod, diags = check(key, text, cfg, resolver, sysroot)
        errors = sum(d.severity == dg.ERROR for d in diags)
        chosen: List[Tuple[Diagnostic, Fix]] = []
        taken: List[Edit] = []
        for d, f in pending(mod, diags):
            if not (f.auto or (unsafe and f.preferred)) or (d.code, f.title, d.range) in banned or _overlap(taken, f.edits):
                continue
            chosen.append((d, f))
            taken += f.edits
        if not chosen:
            break
        new = apply_edits(text, taken)
        if sum(x.severity == dg.ERROR for x in check(key, new, cfg, resolver, sysroot)[1]) > errors:
            banned.update((d.code, f.title, d.range) for d, f in chosen)
            continue
        text, applied = new, applied + chosen
    return text, applied


def diff(old: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path + " (fixed)"))
