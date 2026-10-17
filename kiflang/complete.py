# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Context-aware completion for the language server (see docs/kif-tools.md, "Completion").

The text before the cursor decides what is offered; the module's AST (and the prelude's) supplies the names:

* `#[` / `#![`       attribute names; `#[name(` offers that attribute's arguments (`ATTR_ARGS`, `attr_arg_items`)
* `x.`               fields and methods of the type of `x`, taken from the checker (the half-typed text is cut back to `x`);
                     falls back to scanning for `x: T` / a struct literal / an
                     `T::f()` initialiser in the enclosing function's text
* `a::b::`           the names of that module, the variants of an enum, the associated functions of a type
* after `:` `->` `<` types only
* `Name {`           the fields not yet written (and a snippet filling all of them)
* `let x: T = `      constructors of `T` first: its struct literal, its associated functions returning it, its enum
                     variants, `some`/`none`/`ok`/`err` for `optional`/`result`
* anywhere else      keywords as snippets, construction patterns (struct literals, `impl S { new }`, `impl iface for S`
                     with the missing methods, `match` over every variant of an enum in scope), templates (`#[kobj]`
                     and `#[ops]` interfaces, cells), types, values, modules
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import ast
from . import lexer as lx
from . import types as T
from .astutil import doc_above, slice_text, walk

CK_METHOD, CK_FUNCTION, CK_FIELD, CK_MODULE, CK_STRUCT, CK_INTERFACE, CK_ENUM, CK_KEYWORD, CK_CONSTANT = 2, 3, 5, 9, 22, 8, 13, 14, 21
CK_VARIABLE = 6
CK_ENUM_MEMBER, CK_PROPERTY, CK_SNIPPET = 20, 10, 15

# Keyword -> snippet (LSP snippet syntax); keywords without an entry are offered as plain words.
SNIPPETS: Dict[str, str] = {
    "fn": "fn ${1:name}(${2}) ${3:-> ${4:u32} }{\n\t$0\n}",
    "struct": "struct ${1:name} {\n\t${2:field}: ${3:u32},\n}",
    "enum": "enum ${1:name}: ${2:u32} {\n\t${3:first},\n}",
    "impl": "impl ${1:type} {\n\t$0\n}",
    "interface": "interface ${1:name} {\n\tmethod ${2:name}(${3}) -> ${4:u32};\n}",
    "if": "if ${1:cond} {\n\t$0\n}",
    "while": "while ${1:cond} {\n\t$0\n}",
    "for": "for ${1:i} in ${2:0}..${3:n} {\n\t$0\n}",
    "loop": "loop {\n\t$0\n}",
    "match": "match ${1:value} {\n\t${2:_} => $0,\n}",
    "let": "let ${1:name} = ${2:value};",
    "const": "const ${1:NAME}: ${2:u32} = ${3:0};",
    "import": 'import "${1:file.kif}";',
}

# Words that open a declaration are only useful at the top level or inside `impl`.
_STMT_ONLY = {"let", "if", "while", "for", "loop", "match", "break", "continue", "return"}
_DECL_ONLY = {"fn", "struct", "enum", "impl", "interface", "import", "include", "namespace", "foreign", "class", "extern", "macro",
              "use", "type", "private", "export", "method", "override", "static", "before", "after", "default"}

# Templates: (label, detail, snippet, where) with where = "top" (declarations) or "body" (statements/expressions).
TEMPLATES: List[Tuple[str, str, str, str]] = [
    ("kobj interface", "#[kobj] interface", "// ${1:What the device does.}\n#[kobj, version(1)]\ninterface ${2:name} {\n"
     "\t// ${3:What the method does.}\n\tmethod ${4:read}(${5:offset: u32}) -> ${6:u32};\n}", "top"),
    ("ops interface", "#[ops] interface", "#[ops]\ninterface ${1:name} {\n\tmethod ${2:add}(&mut self, ${3:n: u32}) -> ${4:u32};\n}", "top"),
    ("tls cell", "#[tls] static", "#[tls] static ${1:COUNT}: ${2:u32} = ${3:0};", "top"),
    ("per_cpu cell", "#[per_cpu] static", "#[per_cpu] static ${1:HITS}: ${2:u64} = ${3:0};", "top"),
    ("tls slot", "#[tls] slot<T>", "#[tls] static ${1:CONN}: slot<${2:conn}> = slot::new(${3:make});", "top"),
    ("per_cpu slot", "#[per_cpu] slot<T>", "#[per_cpu] static ${1:STATS}: slot<${2:stats}> = slot::new(${3:init});", "top"),
    ("exported function", "#[export] fn", "#[export]\nfn ${1:name}(${2}) ${3:-> ${4:u32} }{\n\t$0\n}", "top"),
    ("inline function", "#[inline] fn", "#[inline]\nfn ${1:name}(${2}) ${3:-> ${4:u32} }{\n\t$0\n}", "top"),
    ("result function", "fn returning result<T>", "fn ${1:name}(${2}) -> result<${3:u32}> {\n\t$0\n\tok(${4:value})\n}", "top"),
    ("ok(..)", "result", "ok(${1:value})", "body"),
    ("err(..)", "result", "err(error::${1:invalid})", "body"),
    ("some(..)", "optional", "some(${1:value})", "body"),
    ("none()", "optional", "none()", "body"),
    ("if cond return err", "guard", "if ${1:cond} {\n\treturn err(error::${2:invalid});\n}", "body"),
    ("cell with / with_mut", "cell access", "${1:CELL}.with_mut(|${2:v}| {\n\t$0\n});", "body"),
]

# What each attribute takes inside its parentheses (spec sections 4.7, 4.11, 4.17, 5, 7, 8). A plain word is offered as
# written, "<n>" means a number (offered as a snippet), "<type>" / "<struct>" / "<iface>" / "<derive>" / "<lang>" are
# filled from the module, and "<str>" is a string literal.
ATTR_ARGS: Dict[str, List[str]] = {
    "repr": ["c", "packed"],
    "unsafe": ["relocatable"],
    "derive": ["clone", "trace", "<derive>"],
    "tls": ["atomic_ok"],
    "per_cpu": ["irq"],
    "small_stack": ["off", "<n>"],
    "stack": ["<n>"],
    "align": ["<n>"],
    "version": ["<n>"],
    "errno": ["positive", "negative"],
    "extends": ["<iface>"],
    "self": ["<struct>"],
    "softc": ["<struct>"],
    "send": ["<type>"],
    "foreign": ["<type>"],
    "lang": ["<lang>"],
    "export": ["cxx"],
    "target": ["c", "no_i64_div"],
    "header": ["<str>"],
    "link_name": ["<str>"],
    "ensures": ["some", "non_null", "non_empty", "len_ge"],
    "len_ge": ["<n>"],
    "when": ["true", "false"],
    "visibility": ["default", "hidden", "protected", "internal"],
    "cxx_name": ["<str>"],
    "operator": ["<str>"],
    "object": ["<str>"],
}

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TYPE_CTX = re.compile(rf"(?:->|(?<!:):(?!:)|<)\s*(?:&\s*(?:mut\s+)?)?(?:{_IDENT})?$")


def _modules(mod) -> List[object]:
    seen, out = set(), []
    todo = [mod] + list(getattr(mod, "modules", {}).values()) + ([mod.prelude] if getattr(mod, "prelude", None) else [])
    while todo:
        m = todo.pop(0)
        if m is None or id(m) in seen or m.tree is None:
            continue
        seen.add(id(m))
        out.append(m)
    return out


def _last_name(t) -> Optional[str]:
    return t.segments[-1].name if isinstance(t, ast.PathType) and t.segments else None


def _decl(mod, name: str):
    for scope in (mod.scope_types, mod.prelude.types if mod.prelude else {}):
        s = scope.get(name)
        if s is not None:
            return s.node
    return None


def impl_fns(mod, type_name: str) -> List[ast.FnDecl]:
    """Every function of the `impl` blocks (kif and `extern "C++"`) for `type_name`, in every reachable module."""
    out: List[ast.FnDecl] = []
    for m in _modules(mod):
        for it in m.tree.items:
            if isinstance(it, ast.ImplDecl) and _last_name(it.target) == type_name:
                out += [f for f in it.items if isinstance(f, ast.FnDecl)]
            elif isinstance(it, ast.ExternImpl) and _last_name(it.target) == type_name:
                out += list(it.fns)
    return out


def _fn_detail(f: ast.FnDecl) -> str:
    ps = ", ".join(("self" if p.receiver == "value" else "&self" if p.receiver == "ref" else "&mut self")
                   if p.receiver else p.name.text for p in f.params)
    return f"fn {f.name.text}({ps})"


def _fn_item(f: ast.FnDecl, kind: int, tname: str = "") -> dict:
    args = [p for p in f.params if not p.receiver]
    snippet = f"{f.name.text}(" + ", ".join(f"${{{i + 1}:{p.name.text}}}" for i, p in enumerate(args)) + ")"
    return {"label": f.name.text, "kind": kind, "detail": _fn_detail(f), "insertText": snippet, "insertTextFormat": 2,
            "data": {"k": "fn", "t": tname, "n": f.name.text}}


def _receiver_type(text_before: str, name: str) -> Optional[str]:
    """The type name of the variable `name` from the enclosing function's text up to the cursor, or None."""
    starts = [m.start() for m in re.finditer(r"\bfn\s+" + _IDENT, text_before)]
    scope = text_before[starts[-1]:] if starts else text_before
    if name == "self":
        impls = list(re.finditer(rf"\bimpl\b[^{{;]*?(?:\bfor\s+)?({_IDENT})\s*(?:<[^>{{]*>)?\s*{{", text_before))
        return impls[-1].group(1) if impls else None
    pats = [rf"\b{name}\s*:\s*&?\s*(?:mut\s+)?(?:[a-z_]+::)*({_IDENT})",
            rf"\blet\s+(?:mut\s+)?{name}\s*=\s*(?:[a-z_]+::)*({_IDENT})\s*(?:\{{|::\w+\s*\()"]
    found = None
    for p in pats:
        for m in re.finditer(p, scope):
            if found is None or m.start() > found[0]:
                found = (m.start(), m.group(1))
    return found[1] if found else None


def _members(mod, tname: str) -> List[dict]:
    items: List[dict] = []
    node = _decl(mod, tname)
    if isinstance(node, ast.StructDecl):
        items += [{"label": f.name.text, "kind": CK_FIELD, "detail": "field", "data": {"k": "field", "t": tname, "n": f.name.text}}
                  for f in node.fields]
    seen: Set[str] = {i["label"] for i in items}
    for f in impl_fns(mod, tname):
        if f.name.text not in seen and any(p.receiver for p in f.params):
            seen.add(f.name.text)
            items.append(_fn_item(f, CK_METHOD, tname))
    return items


def _qualified(mod, parts: List[str]) -> List[dict]:
    if parts == ["error"] and "error" not in mod.modules:
        from .target import ERROR_ERRNO
        return [{"label": n, "kind": CK_ENUM_MEMBER, "detail": f"error::  (errno {e})"} for n, e in sorted(ERROR_ERRNO.items())]
    owner = mod.modules.get(parts[0])
    if owner is None and len(parts) == 1:
        node = _decl(mod, parts[0])
        if isinstance(node, ast.EnumDecl):
            return [{"label": m.name.text, "kind": CK_ENUM_MEMBER, "detail": f"{node.name.text}::"} for m in node.members]
        fns = [f for f in impl_fns(mod, parts[0]) if not any(p.receiver for p in f.params)]
        if fns:
            return [_fn_item(f, CK_FUNCTION, parts[0]) for f in fns]
    for q in parts[1:]:
        owner = owner.modules.get(q) if owner else None
    names = {**(owner.types if owner else {}), **(owner.values if owner else {})}
    kinds = {"struct": CK_STRUCT, "enum": CK_ENUM, "interface": CK_INTERFACE, "fn": CK_FUNCTION, "const": CK_CONSTANT}
    return [{"label": n, "kind": kinds.get(s.kind, CK_STRUCT), "detail": s.kind, "data": {"k": "sym", "m": parts, "n": n}}
            for n, s in sorted(names.items()) if not s.private]


def _depth(text_before: str) -> int:
    """Brace depth at the cursor (0 = top level); strings and comments are skipped roughly."""
    stripped = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*', "", text_before)
    return stripped.count("{") - stripped.count("}")


def _in_impl_or_top(text_before: str) -> bool:
    d = _depth(text_before)
    return d == 0 or bool(re.search(r"\b(?:impl|interface|class|foreign)\b[^{}]*{[^{}]*$", text_before))


def _src(sym, node) -> str:
    """The source text of `node` in the module that declared `sym`."""
    return " ".join(slice_text(sym.module.source.split("\n"), node.range).split())


def _default_for(ty: str, name: str, n: int) -> str:
    if re.fullmatch(r"[iu](?:8|16|32|64|size)", ty):
        return f"${{{n}:0}}"
    if ty == "bool":
        return f"${{{n}:false}}"
    return f"${{{n}:{name}}}"


def _structs(mod) -> Dict[str, object]:
    return {n: s for n, s in mod.scope_types.items() if isinstance(s.node, ast.StructDecl)}


def _struct_literal(sym, name: str, only: Optional[Set[str]] = None) -> Optional[str]:
    fields = [f for f in sym.node.fields if f.default is None and (only is None or f.name.text in only)]
    if not fields:
        return None
    parts = [f"{f.name.text}: {_default_for(_src(sym, f.ty), f.name.text, i + 1)}" for i, f in enumerate(fields)]
    return name + " { " + ", ".join(parts) + " }"


def _construct_items(mod) -> List[dict]:
    """Struct literals and `impl S { new }` / `try_create` skeletons for every struct in scope."""
    out: List[dict] = []
    for name, sym in sorted(_structs(mod).items()):
        lit = _struct_literal(sym, name)
        if lit:
            out.append({"label": f"{name} {{ .. }}", "kind": CK_SNIPPET, "detail": "construct: struct literal", "filterText": name,
                        "insertText": lit, "insertTextFormat": 2, "sortText": "1_" + name})
    return out


def _impl_ctor_items(mod) -> List[dict]:
    out: List[dict] = []
    for name, sym in sorted(_structs(mod).items()):
        fields = [f for f in sym.node.fields if f.default is None]
        if not fields:
            continue
        params = ", ".join(f"{f.name.text}: {_src(sym, f.ty)}" for f in fields)
        inits = ", ".join(f"{f.name.text}: {f.name.text}" for f in fields)
        out.append({"label": f"impl {name} {{ new }}", "kind": CK_SNIPPET, "detail": "construct: constructor", "filterText": f"impl {name}",
                    "insertText": f"impl {name} {{\n\tfn new({params}) -> {name} {{ {name} {{ {inits} }} }}\n}}", "insertTextFormat": 2})
        out.append({"label": f"impl {name} {{ try_create }}", "kind": CK_SNIPPET, "detail": "construct: fallible constructor",
                    "filterText": f"impl {name}",
                    "insertText": f"impl {name} {{\n\tfn try_create({params}) -> result<Self> {{\n\t\tok(Self {{ {inits} }})\n\t}}\n}}",
                    "insertTextFormat": 2})
    return out


def _impl_iface_items(mod) -> List[dict]:
    out: List[dict] = []
    for name, sym in sorted(mod.scope_types.items()):
        if not isinstance(sym.node, ast.Interface):
            continue
        stubs = []
        for i, m in enumerate(x for x in sym.node.methods if not x.has_default and x.body is None and not x.static):
            ps = ["&mut self" if any(p.receiver == "mut" for p in m.params) else "self" if any(p.receiver == "value" for p in m.params)
                  else "&self"]
            ps += [f"{p.name.text}: {_src(sym, p.ty)}" for p in m.params if not p.receiver and p.ty is not None]
            ret = f" -> {_src(sym, m.ret)}" if m.ret is not None else ""
            stubs.append(f"\tfn {m.name.text}({', '.join(ps)}){ret} {{\n\t\t${{{i + 2}:todo}}\n\t}}")
        out.append({"label": f"impl {name} for ..", "kind": CK_SNIPPET, "detail": "construct: implement every required method",
                    "filterText": f"impl {name} for", "insertTextFormat": 2,
                    "insertText": f"impl {name} for ${{1:type}} {{\n" + "\n".join(stubs) + "\n}"})
    return out


def _local_items(before: str) -> List[dict]:
    """Parameters and `let`/`for` names of the enclosing function that are declared before the cursor, nearest first."""
    starts = [m.start() for m in re.finditer(r"\bfn\s+" + _IDENT, before)]
    if not starts or _depth(before) == 0:
        return []
    scope = before[starts[-1]:]
    names: Dict[str, int] = {}
    sig = scope.split("{", 1)[0]
    for m in re.finditer(rf"({_IDENT})\s*:", sig):
        names[m.group(1)] = m.start()
    for m in re.finditer(rf"\b(?:let|for)\s+(?:mut\s+)?({_IDENT})", scope):
        names[m.group(1)] = m.start()
    names.pop("self", None)
    ranked = sorted(names.items(), key=lambda kv: -kv[1])
    return [{"label": n, "kind": CK_VARIABLE, "detail": "local", "sortText": f"1_{i:04d}"} for i, (n, _) in enumerate(ranked)]


def _inferred_enum_vars(analyze_fn, text: str, line: int, col: int) -> List[tuple]:
    """(name, enum) for the unannotated `let`s before the cursor in the enclosing function, typed by the checker."""
    try:
        m = analyze_fn(text)
    except Exception:
        return []
    if m is None or m.tree is None:
        return []
    pos = (line, col)
    out = []
    for fn in walk(m.tree):
        if isinstance(fn, ast.FnDecl) and (fn.range.start.line, fn.range.start.col) <= pos <= (fn.range.end.line, fn.range.end.col):
            out = []
            for n in walk(fn):
                if isinstance(n, ast.Let) and n.ty is None and (n.range.end.line, n.range.end.col) <= pos:
                    t = getattr(n.init, "ty", None) if n.init is not None else None
                    while isinstance(t, T.Ty) and t.name == "&" and t.args:
                        t = t.args[0]
                    if isinstance(t, T.Ty):
                        out.append((n.name.text, t.name))
    return out


def _match_items(mod, before: str, inferred: Iterable[tuple] = ()) -> List[dict]:
    """`match v { E::a => .., E::b => .. }` for every enum-typed parameter, annotated local or (inferred) unannotated local."""
    starts = [m.start() for m in re.finditer(r"\bfn\s+" + _IDENT, before)]
    scope = before[starts[-1]:] if starts else ""
    out, seen = [], set()
    pairs = [m.groups() for m in re.finditer(rf"\b({_IDENT})\s*:\s*&?\s*(?:mut\s+)?({_IDENT})", scope)] + list(inferred)
    for var, ty in pairs:
        sym = mod.scope_types.get(ty)
        if var in seen or sym is None or not isinstance(sym.node, ast.EnumDecl):
            continue
        seen.add(var)
        arms = "\n".join(f"\t{ty}::{e.name.text} => ${{{i + 1}:todo}}," for i, e in enumerate(sym.node.members))
        out.append({"label": f"match {var} {{ every {ty} }}", "kind": CK_SNIPPET, "detail": "construct: exhaustive match",
                    "filterText": f"match {var}", "insertText": f"match {var} {{\n{arms}\n}}", "insertTextFormat": 2})
    return out


def _literal_fields(mod, before: str) -> Optional[List[dict]]:
    """Inside `Name { .. }`: the fields not written yet, or None when the cursor is not in a struct literal."""
    m = re.search(rf"(?<![\w:])({_IDENT})\s*{{([^{{}}]*)$", before)
    if not m or m.group(1) not in _structs(mod):
        return None
    head = before[: m.start()].rstrip()
    if re.search(r"\b(?:struct|impl|for|enum|interface|fn|else|loop|unsafe|match|if|while|in)\s*$", head) or head.endswith(("->", ")")):
        return None
    name, body = m.group(1), m.group(2)
    sym = _structs(mod)[name]
    written = set(re.findall(rf"({_IDENT})\s*:", body))
    todo = [f for f in sym.node.fields if f.name.text not in written]
    out = [{"label": f.name.text, "kind": CK_FIELD, "detail": _src(sym, f.ty), "insertText": f"{f.name.text}: ${{1:{f.name.text}}}",
            "insertTextFormat": 2} for f in todo]
    rest = _struct_literal(sym, name, {f.name.text for f in todo if f.default is None})
    if len(todo) > 1 and rest:
        inner = rest[len(name) + 3:-2]
        out.append({"label": "remaining fields", "kind": CK_SNIPPET, "detail": "construct: fill every missing field", "sortText": "0",
                    "insertText": inner, "insertTextFormat": 2})
    return out


def _ctor_items(mod, tname: str) -> List[dict]:
    """What builds a `tname`, for the cursor right after `let x: tname = `."""
    out: List[dict] = []
    sym = mod.scope_types.get(tname)
    if tname == "optional":
        return [{"label": "some(..)", "kind": CK_SNIPPET, "insertText": "some(${1:value})", "insertTextFormat": 2, "sortText": "0"},
                {"label": "none()", "kind": CK_SNIPPET, "insertText": "none()", "insertTextFormat": 2, "sortText": "0"}]
    if tname == "result":
        return [{"label": "ok(..)", "kind": CK_SNIPPET, "insertText": "ok(${1:value})", "insertTextFormat": 2, "sortText": "0"},
                {"label": "err(..)", "kind": CK_SNIPPET, "insertText": "err(error::${1:invalid})", "insertTextFormat": 2, "sortText": "0"}]
    if sym is None:
        return out
    if isinstance(sym.node, ast.StructDecl):
        lit = _struct_literal(sym, tname)
        if lit:
            out.append({"label": f"{tname} {{ .. }}", "kind": CK_SNIPPET, "detail": "struct literal", "filterText": tname,
                        "insertText": lit, "insertTextFormat": 2, "sortText": "0"})
    if isinstance(sym.node, ast.EnumDecl):
        out += [{"label": f"{tname}::{e.name.text}", "kind": CK_ENUM_MEMBER, "sortText": "0"} for e in sym.node.members]
    for f in impl_fns(mod, tname):
        if any(p.receiver for p in f.params) or f.ret is None:
            continue
        ret = _src(sym, f.ret)
        if re.search(rf"\b(?:{tname}|Self)\b", ret):
            item = _fn_item(f, CK_FUNCTION, tname)
            item.update({"label": f"{tname}::{f.name.text}", "insertText": f"{tname}::" + item["insertText"], "sortText": "0",
                         "detail": f"{_fn_detail(f)} -> {ret}"})
            out.append(item)
    return out


def infer_receiver(analyze_fn, text: str, line: int, dot_col: int, cursor_col: int):
    """The checker's type of the expression just before the `.` at (line, dot_col), or None.

    The half-typed `recv.partial` is cut back to `recv` (followed by each of a few terminators until the text parses),
    analysed, and the largest typed expression ending at the dot is taken. Needs no annotations: unannotated locals,
    call chains and closure parameters all work.
    """
    lines = text.split("\n")
    head = lines[line][:dot_col].rstrip()
    end = (line, len(head))
    for term in ("", ";", ")", ");"):
        patched = "\n".join(lines[:line] + [head + term + lines[line][cursor_col:]] + lines[line + 1:])
        try:
            m = analyze_fn(patched)
        except Exception:
            continue
        if m is None or m.tree is None:
            continue
        best = None
        for n in walk(m.tree):
            t = getattr(n, "ty", None)
            if isinstance(n, (ast.Lambda, ast.Block)):
                continue  # these end where their last expression ends, but they are not the receiver
            if isinstance(t, T.Ty) and t != T.UNKNOWN and (n.range.end.line, n.range.end.col) == end:
                if best is None or (n.range.start.line, n.range.start.col) < (best.range.start.line, best.range.start.col):
                    best = n
        if best is not None:
            t = best.ty
            while t.name == "&" and t.args:
                t = t.args[0]
            return t
    return None


def complete(mod, text: str, line: int, col: int, attributes: Iterable[str], scalars: Iterable[str], analyze_fn=None,
             import_entries=None, candidates=None, keyword_snippets: bool = True) -> List[dict]:
    lines = text.split("\n")
    cur = lines[line][:col]
    before = "\n".join(lines[:line] + [cur])
    imp = re.match(r'\s*import\s+"([^"]*)$', cur)
    if imp:
        return _import_items(import_entries(imp.group(1))) if import_entries else []
    cf = cfg_items(before, cur)
    if cf is not None:
        return cf
    am = re.search(rf"#!?\[(?:[^\]\[(]*,\s*)?({_IDENT})\(([^\])]*)$", cur)
    if am:
        return attr_arg_items(mod, am.group(1), am.group(2))
    if re.search(r"#!?\[[^\]]*\([^\])]*$", cur):
        return []
    if re.search(r"#!?\[[^\]]*$", cur):
        return [{"label": a, "kind": CK_PROPERTY} for a in attributes]
    mem = re.search(rf"\.\s*(?:{_IDENT})?$", cur)
    if mem and not re.search(r"\d\.\s*(?:[A-Za-z_]\w*)?$", cur) and not cur.rstrip().endswith(".."):
        ty = infer_receiver(analyze_fn, text, line, mem.start(), col) if analyze_fn else None
        if ty is not None:
            return _members(mod, ty.name)
        ident = re.search(rf"({_IDENT})\s*$", cur[: mem.start()])
        t = _receiver_type(before, ident.group(1)) if ident else None
        return _members(mod, t) if t else []
    qual = re.search(rf"((?:{_IDENT}::)+)(?:{_IDENT})?$", cur)
    if qual:
        return _qualified(mod, [x for x in qual.group(1).split("::") if x])
    fields = _literal_fields(mod, before)
    if fields is not None:
        return fields
    items: List[dict] = []
    ctor = re.search(rf"\blet\s+(?:mut\s+)?{_IDENT}\s*:\s*&?\s*(?:mut\s+)?(?:[a-z_]+::)*({_IDENT})[^=;]*=\s*(?:{_IDENT})?$", cur)
    if ctor:
        items += _ctor_items(mod, ctor.group(1))
    types_only = bool(_TYPE_CTX.search(cur)) and not re.search(r"=\s*[^=]*$", cur)
    if not types_only:
        top = _in_impl_or_top(before)
        words = sorted(lx.KEYWORDS | set(lx.CONTEXTUAL_WORDS))
        for k in words:
            if (k in _STMT_ONLY and top) or (k in _DECL_ONLY and not top):
                continue
            item = {"label": k, "kind": CK_KEYWORD}
            if k in SNIPPETS and keyword_snippets:
                item.update({"insertText": SNIPPETS[k], "insertTextFormat": 2, "kind": CK_SNIPPET, "detail": "snippet"})
            items.append(item)
    if not types_only:
        top = _depth(before) == 0
        if top:
            items += _impl_ctor_items(mod) + _impl_iface_items(mod)
        else:
            items += _construct_items(mod) + _match_items(mod, before, _inferred_enum_vars(analyze_fn, text, line, col) if analyze_fn else ())
        items += [{"label": l, "kind": CK_SNIPPET, "detail": d, "insertText": t, "insertTextFormat": 2, "filterText": l.split(" ")[0]}
                  for l, d, t, w in TEMPLATES if (w == "top") == top]
    items += [{"label": s, "kind": CK_STRUCT, "detail": "builtin type"} for s in sorted(scalars)]
    seen: Set[str] = set()
    if not types_only:
        locals_ = _local_items(before)
        items += locals_
        seen |= {i["label"] for i in locals_}
    uses = Counter(re.findall(_IDENT, text))  # names the file already uses come before the rest of their tier
    kinds = {"struct": CK_STRUCT, "enum": CK_ENUM, "interface": CK_INTERFACE, "fn": CK_FUNCTION, "const": CK_CONSTANT}
    scopes = [mod.scope_types] + ([] if types_only else [mod.scope_values])
    if mod.prelude:
        scopes += [mod.prelude.types] + ([] if types_only else [mod.prelude.values])
    for scope in scopes:
        for n, s in sorted(scope.items()):
            if n in seen or (types_only and s.kind in ("fn", "const")):
                continue
            seen.add(n)
            tier = "4" if s.module is not mod and mod.prelude is not None and s.module is mod.prelude else "2"
            items.append({"label": n, "kind": kinds.get(s.kind, CK_STRUCT), "detail": s.kind, "data": {"k": "sym", "m": [], "n": n},
                          "sortText": f"{tier}_{999 - min(uses[n], 999):03d}_{n}"})
    items += [{"label": n, "kind": CK_MODULE, "sortText": "3_" + n} for n in sorted(mod.modules) if n not in seen]
    for i in items:
        if "sortText" not in i:  # keywords and snippets after the names, builtin types last
            i["sortText"] = ("5_" if i["kind"] == CK_STRUCT and i.get("detail") == "builtin type" else "3_") + i["label"]
    if candidates and not types_only:
        prefix = re.search(rf"({_IDENT})$", cur)
        if prefix and len(prefix.group(1)) >= 2:
            items += _unimported_items(mod, text, prefix.group(1), seen, candidates())
    return items


# -- signature help

def _open_call(before: str) -> Optional[Tuple[int, int]]:
    """(offset of the unmatched `(` before the cursor, number of top-level commas after it), or None outside a call."""
    depth, commas, i = 0, 0, len(before) - 1
    while i >= 0:
        ch = before[i]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                return (i, commas) if ch == "(" else None
            depth -= 1
        elif ch == "," and depth == 0:
            commas += 1
        elif ch == ";" and depth == 0:
            return None
        elif ch == '"':  # skip a string literal backwards
            i -= 1
            while i >= 0 and not (before[i] == '"' and before[i - 1: i] != "\\"):
                i -= 1
        i -= 1
    return None


def _find_fn(mod, tname: str, fname: str):
    """(module, FnDecl) of method/associated function `fname` of type `tname` (kif and `extern "C++"` impls)."""
    for m in _modules(mod):
        for it in m.tree.items:
            if isinstance(it, ast.ImplDecl) and _last_name(it.target) == tname:
                for f in it.items:
                    if isinstance(f, ast.FnDecl) and f.name.text == fname:
                        return m, f
            elif isinstance(it, ast.ExternImpl) and _last_name(it.target) == tname:
                for f in it.fns:
                    if f.name.text == fname:
                        return m, f
    return None


def signature_help(mod, text: str, line: int, col: int, analyze_fn=None) -> Optional[dict]:
    """The LSP SignatureHelp for the call the cursor is inside, or None."""
    lines = text.split("\n")
    before = "\n".join(lines[:line] + [lines[line][:col]])
    oc = _open_call(before)
    if oc is None:
        return None
    off, commas = oc
    head = before[:off].rstrip()
    m = re.search(rf"((?:{_IDENT}::)*{_IDENT})$", head)
    if not m:
        return None
    path = m.group(1).split("::")
    found = None  # (module whose source holds the declaration, FnDecl)
    method = False
    pre = head[: m.start()].rstrip()
    if pre.endswith(".") and not pre.endswith(".."):
        method = True
        dot = len(pre) - 1
        hl = before.count("\n", 0, dot)
        dot_col = dot - (before.rfind("\n", 0, dot) + 1)
        # the rest of the dot's line (the method name and its arguments) is dropped while the receiver is analysed
        ty = infer_receiver(analyze_fn, text, hl, dot_col, len(lines[hl])) if analyze_fn else None
        if ty is not None:
            found = _find_fn(mod, ty.name, path[-1])
    elif len(path) == 1:
        sym = mod.scope_values.get(path[0]) or (mod.prelude.values.get(path[0]) if mod.prelude else None)
        if sym is not None and isinstance(sym.node, ast.FnDecl):
            found = (sym.module, sym.node)
    else:
        tname = path[-2]
        found = _find_fn(mod, tname, path[-1])
        if found is None:
            owner = mod.modules.get(path[0])
            for q in path[1:-1]:
                owner = owner.modules.get(q) if owner else None
            sym = owner.values.get(path[-1]) if owner else None
            if sym is not None and isinstance(sym.node, ast.FnDecl):
                found = (sym.module, sym.node)
    if found is None:
        return None
    fm, f = found
    src = fm.source.split("\n") if fm.source else None
    if src is None:
        return None
    params = [p for p in f.params if not (method and p.receiver)]
    labels = [" ".join(slice_text(src, p.range).split()) for p in params]
    ret = f" -> {' '.join(slice_text(src, f.ret.range).split())}" if f.ret is not None else ""
    label = f"fn {f.name.text}(" + ", ".join(labels) + ")" + ret
    info: dict = {"label": label, "parameters": []}
    pos = len(f"fn {f.name.text}(")
    for l in labels:
        info["parameters"].append({"label": [pos, pos + len(l)]})
        pos += len(l) + 2
    doc = doc_above(src, f) if f.range.start.line < len(src) else ""
    if doc:
        info["documentation"] = {"kind": "markdown", "value": doc}
    return {"signatures": [info], "activeSignature": 0, "activeParameter": min(commas, max(len(labels) - 1, 0))}


# -- completionItem/resolve

def _fn_label(src: List[str], f: ast.FnDecl, skip_receiver: bool) -> str:
    params = [" ".join(slice_text(src, p.range).split()) for p in f.params if not (skip_receiver and p.receiver)]
    ret = f" -> {' '.join(slice_text(src, f.ret.range).split())}" if f.ret is not None else ""
    return f"fn {f.name.text}(" + ", ".join(params) + ")" + ret


def resolve_item(mod, item: dict) -> dict:
    """Fill in the `detail` (the full declaration) and `documentation` (the `//` comment above it) of a completion item.

    The cheap list is sent first and this runs only for the item the user highlights (`completionItem/resolve`).
    """
    d = item.get("data") or {}
    node, owner, detail = None, None, None
    if d.get("k") == "fn":
        found = _find_fn(mod, d["t"], d["n"]) if d.get("t") else None
        if found:
            owner, node = found
            detail = _fn_label(owner.source.split("\n"), node, False) if owner.source else None
    elif d.get("k") == "field":
        sym = mod.scope_types.get(d["t"]) or (mod.prelude.types.get(d["t"]) if mod.prelude else None)
        if sym is not None and isinstance(sym.node, ast.StructDecl):
            node = next((f for f in sym.node.fields if f.name.text == d["n"]), None)
            owner = sym.module
            if node is not None and owner.source:
                detail = f"{d['n']}: {_src(sym, node.ty)}"
    elif d.get("k") == "sym":
        scope_mod = mod
        for q in d.get("m", [])[:1]:
            scope_mod = mod.modules.get(q)
        for q in d.get("m", [])[1:]:
            scope_mod = scope_mod.modules.get(q) if scope_mod else None
        if scope_mod is not None:
            sym = (scope_mod.types.get(d["n"]) or scope_mod.values.get(d["n"])) if d.get("m") else \
                (mod.scope_types.get(d["n"]) or mod.scope_values.get(d["n"])
                 or (mod.prelude.types.get(d["n"]) or mod.prelude.values.get(d["n"]) if mod.prelude else None))
            if sym is not None and sym.module.source and sym.node.range.start.line < len(sym.module.source.split("\n")):
                owner, node = sym.module, sym.node
                head = re.split(r"\s*\{", _src(sym, sym.node), maxsplit=1)[0].strip()
                detail = head if len(head) < 200 else head[:200] + "..."
    if node is None or owner is None or not owner.source:
        return item
    out = dict(item)
    if detail:
        out["detail"] = detail
    doc = doc_above(owner.source.split("\n"), node)
    if doc:
        out["documentation"] = {"kind": "markdown", "value": doc}
    return out


# -- imports

CK_FILE, CK_FOLDER = 17, 19


def _import_items(entries: List[Tuple[str, bool]]) -> List[dict]:
    """Items for the path inside `import "`: the `.kif` files and directories next to the file and in the include directories."""
    out = []
    for name, is_dir in entries:
        out.append({"label": name + ("/" if is_dir else ""), "kind": CK_FOLDER if is_dir else CK_FILE,
                    "insertText": name + ("/" if is_dir else ""), "detail": "directory" if is_dir else "kif module",
                    "command": {"title": "retrigger", "command": "editor.action.triggerSuggest"} if is_dir else None})
    return [{k: v for k, v in i.items() if v is not None} for i in out]


def _import_edit(text: str, path: str) -> dict:
    """The edit that adds `import "<path>";`: after the last import, else after the file's leading comment block, else first."""
    lines = text.split("\n")
    last = max((i for i, l in enumerate(lines) if re.match(r'\s*import\s+"', l)), default=None)
    if last is not None:
        at = last + 1
    else:
        at = 0
        while at < len(lines) and lines[at].startswith("//"):
            at += 1
        at = at if at and at < len(lines) and not lines[at].strip() else 0
        at = at + 1 if at else 0
    new = f'import "{path}";\n' + ("\n" if last is None and at == 0 and text.strip() else "")
    return {"range": {"start": {"line": at, "character": 0}, "end": {"line": at, "character": 0}}, "newText": new}


_DECL = re.compile(r"^(fn|struct|enum|interface|const)\s+([A-Za-z_]\w*)", re.M)


def _unimported_items(mod, text: str, prefix: str, seen: Set[str], files: List[Tuple[str, str, str]]) -> List[dict]:
    """Declarations of neighbouring `.kif` files that this file has not imported: accepting one writes `stem::name` and adds the import."""
    have = set(getattr(mod, "imported", {}) or {})
    out = []
    for path, stem, src in files:
        if path in have or stem in mod.modules:
            continue
        for kind, name in _DECL.findall(src):
            if name.startswith(prefix) and name not in seen:
                out.append({"label": name, "kind": {"struct": CK_STRUCT, "enum": CK_ENUM, "interface": CK_INTERFACE,
                                                    "fn": CK_FUNCTION, "const": CK_CONSTANT}[kind],
                            "detail": f'{kind} in "{path}" (adds the import)', "filterText": name, "sortText": "9_" + name,
                            "insertText": f"{stem}::{name}", "additionalTextEdits": [_import_edit(text, path)]})
    return out


# -- attribute arguments

def attr_arg_items(mod, attr: str, typed: str) -> List[dict]:
    """The arguments of `#[attr(` given the text typed since the `(`; items already written (after a comma) are left out."""
    done = {w.strip() for w in typed.split(",")[:-1]}
    out: List[dict] = []

    def add(label: str, kind: int, detail: str = "", insert: Optional[str] = None) -> None:
        if label not in done:
            item = {"label": label, "kind": kind, "detail": detail or f"#[{attr}(..)]"}
            if insert:
                item.update({"insertText": insert, "insertTextFormat": 2})
            out.append(item)

    for a in ATTR_ARGS.get(attr, []):
        if a == "<n>":
            add("number", CK_CONSTANT, "a byte count or version", "${1:" + ("1" if attr == "version" else "256") + "}")
        elif a == "<str>":
            add('"..."', CK_CONSTANT, "a string", '"${1:}"')
        elif a == "<derive>":
            for m in _modules(mod):
                for it in m.tree.items:
                    if isinstance(it, ast.FnDecl) and it.name.text.startswith("derive_") and it.is_gen:
                        add(it.name.text[len("derive_"):], CK_FUNCTION, "generator derive_" + it.name.text[len("derive_"):])
        elif a in ("<iface>", "<struct>", "<type>", "<lang>"):
            if a == "<lang>":
                for m in _modules(mod):
                    for it in walk(m.tree.items):
                        for at in getattr(it, "attrs", None) or ():
                            if at.name.text == "lang" and at.args and at.args[0].name:
                                add(at.args[0].name, CK_PROPERTY, "lang item")
                continue
            want = {"<iface>": ast.Interface, "<struct>": ast.StructDecl}.get(a)
            for n, sym in sorted(mod.scope_types.items()):
                if want is None or isinstance(sym.node, want):
                    add(n, CK_INTERFACE if want is ast.Interface else CK_STRUCT, sym.kind)
        else:
            add(a, CK_PROPERTY)
    seen, uniq = set(), []
    for i in out:
        if i["label"] not in seen:
            seen.add(i["label"])
            uniq.append(i)
    return uniq


def cfg_environment() -> "tuple[set, dict]":
    """Every flag and `key = "value"` the built-in targets and profiles can set (the union, as the checker also looks at all of them)."""
    from . import cfg as cfgmod, target as tg
    flags: set = set()
    values: dict = {}
    for name in tg.builtin_names():
        try:
            t = tg.load(name)
        except Exception:  # an unreadable target only loses its own keys
            continue
        for prof in t.profile_names():
            env = cfgmod.env_of_target(t)
            cfgmod.apply_profile(env, t.profile(prof), [])
            flags |= env.flags
            for k, v in env.values.items():
                values.setdefault(k, set()).update(v)
    return flags, values


def cfg_items(before: str, cur: str):
    """Inside `cfg(` / `#[cfg(`: flags, `key = "value"` keys, `all`/`any`/`not`, and the values after `key = "`; None elsewhere."""
    m = re.search(r"\bcfg\s*\(([^{};]*)$", cur) or re.search(r"#!?\[[^\]]*\bcfg\s*\(([^\]{};]*)$", cur)
    if not m:
        return None
    arg = m.group(1)
    vm = re.search(rf'({_IDENT})\s*=\s*"([^"]*)$', arg)
    flags, values = cfg_environment()
    if vm:
        return [{"label": v, "kind": CK_ENUM_MEMBER, "detail": f"{vm.group(1)} value"} for v in sorted(values.get(vm.group(1), ()))]
    if re.search(r"\w\s*=\s*$", arg):
        return []
    items = [{"label": c, "kind": CK_FUNCTION, "detail": "combinator", "insertText": c + "(${1})", "insertTextFormat": 2}
             for c in ("all", "any", "not")]
    items += [{"label": f, "kind": CK_PROPERTY, "detail": "flag"} for f in sorted(flags)]
    items += [{"label": k, "kind": CK_PROPERTY, "detail": "key = \"value\"", "insertText": k + ' = "${1}"', "insertTextFormat": 2}
              for k in sorted(values)]
    return items
