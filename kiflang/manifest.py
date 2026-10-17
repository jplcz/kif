# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The ABI manifest: what a released `.kif` file promises (docs/kif-abi.md).

A manifest is a JSON document describing the parts of a file that other code is built against: every `#[kobj]`
interface (method ids, signature hashes, defaults, version, `extends`, handle type) and the layout of every enum and
struct. `kif manifest` writes one per release; `kif lint --baseline` compares the working copy to it (KIFL020-033).
Bodies never appear in a manifest, so changing a default body is not an ABI change. Everything here is derived from
the syntax tree and the checked module only, so the manifest is the same for the C and the C++ backend.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from . import ast
from .astutil import attr_name_arg, find_attr, has_attr, slice_text
from .symbols import Module

FORMAT = "kif-abi-manifest"
FORMAT_VERSION = 2

# Attributes that change what implementers and callers may assume (docs/kif-proc-language.md 4.13). Advice such as
# `#[trace]` or documentation attributes stay out, so adding them is never an ABI break.
ABI_ATTRIBUTES = ("atomic_entry", "atomic_only", "might_sleep")

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3


def fnv1a64(text: str) -> int:
    h = _FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * _FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def method_id(interface: str, method: str) -> int:
    """The 64-bit id a kobj method is dispatched by: FNV-1a of `interface::method`."""
    return fnv1a64(f"{interface}::{method}")


def _expr_text(e: ast.Expr) -> str:
    if isinstance(e, ast.Lit):
        return str(e.value)
    if isinstance(e, ast.PathExpr):
        return "::".join(s.name for s in e.segments)
    return "?"


def type_text(t: Optional[ast.Node]) -> str:
    """Canonical spelling of a type: whitespace and lifetimes are not part of the ABI, everything else is."""
    if t is None:
        return "()"
    if isinstance(t, ast.PathType):
        out = []
        for s in t.segments:
            args = [type_text(a) for a in s.args if not isinstance(a, ast.Lifetime)]
            out.append(s.name + (f"<{','.join(args)}>" if args else ""))
        return "::".join(out)
    if isinstance(t, ast.RefType):
        return ("&mut " if t.mut else "&") + type_text(t.inner)
    if isinstance(t, ast.SliceType):
        return f"[{type_text(t.elem)}]"
    if isinstance(t, ast.ArrayType):
        return f"[{type_text(t.elem)};{_expr_text(t.length)}]"
    if isinstance(t, ast.FnType):
        ret = f"->{type_text(t.ret)}" if t.ret is not None else ""
        return f"fn({','.join(type_text(p) for p in t.params)}){ret}"
    if isinstance(t, ast.TupleType):
        return f"({','.join(type_text(e) for e in t.elems)})"
    if isinstance(t, ast.ConstArg):
        return _expr_text(t.expr)
    return "?"


def attr_int(attrs: List[ast.Attribute], name: str, default: int) -> int:
    a = find_attr(attrs, name)
    if a is not None and a.args and a.args[0].value is not None and a.args[0].value.kind == "int":
        return int(a.args[0].value.value)
    return default


def interface_version(i: ast.Interface) -> int:
    return attr_int(i.attrs, "version", 1)


def interface_errno(i: ast.Interface) -> str:
    return attr_name_arg(i.attrs, "errno") or "positive"


def interface_self(i: ast.Interface) -> Optional[str]:
    return attr_name_arg(i.attrs, "self")


def method_abi_attrs(m: ast.Method) -> List[str]:
    return [a for a in ABI_ATTRIBUTES if has_attr(m.attrs, a)]


def signature_text(i: ast.Interface, m: ast.Method) -> str:
    """The text a method's signature hash is taken over.

    The receiver is not part of it (it is the interface's handle type, recorded separately); `errno` matters only for a
    `cresult<>` result and `version` only when it is not 1, so a signature keeps its hash while nothing about it changes.
    """
    params = ",".join(type_text(p.ty) for p in m.params if not p.receiver)
    ret = type_text(m.ret)
    text = f"{i.name.text}::{m.name.text}({params})->{ret};{'static' if m.static else 'instance'}"
    if m.ret is not None and "cresult" in ret:
        text += f";errno={interface_errno(i)}"
    if abi := method_abi_attrs(m):
        text += ";attrs=" + ",".join(abi)
    if (v := interface_version(i)) != 1:
        text += f";v={v}"
    return text


def signature_hash(i: ast.Interface, m: ast.Method) -> int:
    return fnv1a64(signature_text(i, m))


def _has_default(m: ast.Method) -> bool:
    return m.body is not None or m.has_default or m.default_sym is not None or has_attr(m.attrs, "provided")


def enum_layout(e: ast.EnumDecl, lines: List[str]) -> str:
    """`u8:a=0,b=1,c=2`: the underlying type, then each enumerator with its value in declaration order.

    Implicit values count up from the previous one; a value that is not a literal is spelled as written (without blanks).
    """
    parts: List[str] = []
    nxt: Optional[int] = 0
    for m in e.members:
        if m.value is None:
            val = str(nxt) if nxt is not None else "?"
            nxt = nxt + 1 if nxt is not None else None
        elif isinstance(m.value, ast.Lit) and m.value.kind == "int":
            nxt = int(m.value.value)
            val = str(nxt)
            nxt += 1
        else:
            val = "".join(slice_text(lines, m.value.range).split())
            nxt = None
        parts.append(f"{m.name.text}={val}")
    return f"{type_text(e.repr) if e.repr is not None else 'i32'}:" + ",".join(parts)


def struct_layout(s: ast.StructDecl) -> str:
    """`a:u32,b:&Foo`: each field in declaration order, preceded by the `repr(..)` attribute when there is one."""
    head = ""
    if (a := find_attr(s.attrs, "repr")) is not None and a.args:
        head = "repr(" + ",".join(x.name or "" for x in a.args) + ");"
    if (al := find_attr(s.attrs, "align")) is not None and al.args and al.args[0].value is not None:
        head += f"align({al.args[0].value.value});"
    return head + ",".join(f"{f.name.text}:{type_text(f.ty)}" for f in s.fields)


def _interface_entry(i: ast.Interface) -> dict:
    own = [m for m in i.methods if not m.override]
    cxx_only = has_attr(i.attrs, "cxx_only")
    return {
        "cxx_only": cxx_only,
        "extends": attr_name_arg(i.attrs, "extends"),
        "self": interface_self(i),
        "version": interface_version(i),
        "errno": interface_errno(i),
        "c_symbols": [] if cxx_only else [f"{i.name.text}_{m.name.text}" for m in own],
        "ids": {m.name.text: f"0x{method_id(i.name.text, m.name.text):016x}" for m in own},
        "signatures": {m.name.text: f"0x{signature_hash(i, m):016x}" for m in own},
        "defaults": [m.name.text for m in own if _has_default(m)],
        "attrs": {m.name.text: method_abi_attrs(m) for m in own if method_abi_attrs(m)},
    }


def manifest_of(mod: Module, path: str = "") -> dict:
    """The manifest of one checked module: the `#[kobj]` interfaces and the enums and structs it declares."""
    lines = mod.source.split("\n")
    interfaces = {i.name.text: _interface_entry(i) for i in mod.tree.of(ast.Interface) if has_attr(i.attrs, "kobj")}
    types: Dict[str, str] = {}
    layouts: Dict[str, str] = {}
    for e in mod.tree.of(ast.EnumDecl):
        types[e.name.text] = "enum"
        layouts[e.name.text] = enum_layout(e, lines)
    for s in mod.tree.of(ast.StructDecl):
        types[s.name.text] = "struct"
        layouts[s.name.text] = struct_layout(s)
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "file": os.path.basename(path or mod.key),
        "interfaces": interfaces,
        "types": types,
        "layouts": layouts,
    }


def dumps(manifest: dict) -> str:
    return json.dumps(manifest, indent=2) + "\n"


def loads(text: str, source: str = "manifest") -> dict:
    """Parse and validate a manifest; ValueError names what is wrong."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source}: not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"{source}: not a kif ABI manifest (missing \"format\": \"{FORMAT}\"); create one with `kif manifest`")
    if doc.get("version") != FORMAT_VERSION:
        raise ValueError(f"{source}: manifest version {doc.get('version')!r} is not supported (this tool writes {FORMAT_VERSION}); recreate it with `kif manifest`")
    for section in ("interfaces", "types", "layouts"):
        if not isinstance(doc.get(section), dict):
            raise ValueError(f"{source}: manifest has no {section!r} section")
    return doc


def pick(baselines: List[dict], path: str) -> Optional[dict]:
    """The baseline for `path`: the manifest whose `file` is the base name of `path`."""
    name = os.path.basename(path)
    return next((b for b in baselines if b.get("file") == name), None)
