# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Syntax tree and IR as plain data: JSON text or canonical CBOR (docs/kif-ir.md, "Serialisation").

Both go through the same two steps: `*_to_data` turns the object into dicts, lists, strings, integers, booleans and None
(exactly what JSON and `cbor.py` carry), and `*_from_data` rebuilds it. `dumps_*` / `loads_*` do the last step. Decoding
validates: an unknown node kind, a missing or extra field, a wrong type or a dangling reference is a `SerialError`, never a
half-built object.

* **Syntax tree** (`ast_to_data`): `{"k": "<node class>", "<field>": ..., ...}`, ranges as `{"range": [line, col, line, col]}`.
  Only the syntax is stored, not what the checker computed (types, moves, resolved symbols), so a consumer re-checks the tree.
* **IR** (`ir_to_data`): `{"format": "kif-ir", "version": 1, "functions": [...]}`. Locals, temps and blocks are numbered and
  referenced by id; an instruction is `{"instr": "<class>", ...}` with its source range under `"at"`. The AST node an instruction
  came from (`site`) is stored as that range only, and comes back as a bare node carrying it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, List, Optional, Type

from . import ast
from . import cbor
from . import ir
from .source import Pos, Range

AST_FORMAT, AST_VERSION = "kif-ast", 1
IR_FORMAT, IR_VERSION = "kif-ir", 2
AST_DEPTH = 4096  # CBOR nesting allowed when decoding a tree (the `.kifm` default of 64 is for flat manifests)


class SerialError(ValueError):
    pass


def _rng(r: Range) -> List[int]:
    return [r.start.line, r.start.col, r.end.line, r.end.col]


def _unrng(v: Any) -> Range:
    if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, int) and not isinstance(x, bool) for x in v)):
        raise SerialError(f"bad range {v!r}")
    return Range(Pos(v[0], v[1]), Pos(v[2], v[3]))


# ------------------------------------------------------------------ syntax tree

_NODES: Dict[str, Type] = {n: c for n, c in vars(ast).items() if isinstance(c, type) and dataclasses.is_dataclass(c)}


def ast_to_data(node: Any) -> Any:
    """A node (or a list, string, number, boolean, None or range found in one) as data."""
    if isinstance(node, Range):
        return {"range": _rng(node)}
    if dataclasses.is_dataclass(node) and type(node).__name__ in _NODES:
        out: Dict[str, Any] = {"k": type(node).__name__}
        for f in dataclasses.fields(node):
            out[f.name] = ast_to_data(getattr(node, f.name))
        return out
    if isinstance(node, (list, tuple)):
        return [ast_to_data(x) for x in node]
    if node is None or isinstance(node, (bool, int, str)):
        return node
    raise SerialError(f"cannot encode {type(node).__name__}")


def ast_from_data(d: Any) -> Any:
    if isinstance(d, dict):
        if set(d) == {"range"}:
            return _unrng(d["range"])
        kind = d.get("k")
        cls = _NODES.get(kind) if isinstance(kind, str) else None
        if cls is None:
            raise SerialError(f"unknown node kind {kind!r}")
        names = {f.name for f in dataclasses.fields(cls)}
        have = set(d) - {"k"}
        if have != names:
            raise SerialError(f"{kind}: fields {sorted(have ^ names)} do not match")
        return cls(**{n: ast_from_data(d[n]) for n in names})
    if isinstance(d, list):
        return [ast_from_data(x) for x in d]
    if d is None or isinstance(d, (bool, int, str)):
        return d
    raise SerialError(f"bad value {d!r}")


def _wrap(fmt: str, version: int, key: str, body: Any) -> Dict[str, Any]:
    return {"format": fmt, "version": version, key: body}


def _unwrap(d: Any, fmt: str, version: int, key: str) -> Any:
    if not isinstance(d, dict) or d.get("format") != fmt:
        raise SerialError(f"not a {fmt} document")
    if d.get("version") != version:
        raise SerialError(f"unsupported {fmt} version {d.get('version')!r} (this is {version})")
    if key not in d:
        raise SerialError(f"{fmt}: missing {key!r}")
    return d[key]


def dumps_ast(tree: Any, as_cbor: bool = False) -> Any:
    """The tree as JSON text, or as CBOR bytes with `as_cbor`."""
    try:
        data = _wrap(AST_FORMAT, AST_VERSION, "tree", ast_to_data(tree))
        return cbor.dumps(data) if as_cbor else json.dumps(data, separators=(",", ":"))
    except RecursionError as e:
        raise SerialError("syntax tree nested too deeply") from e


def loads_ast(data: Any) -> Any:
    try:
        d = cbor.loads(data, AST_DEPTH) if isinstance(data, (bytes, bytearray)) else json.loads(data)
        return ast_from_data(_unwrap(d, AST_FORMAT, AST_VERSION, "tree"))
    except (cbor.CborError, json.JSONDecodeError) as e:
        raise SerialError(str(e)) from e
    except RecursionError as e:
        raise SerialError("syntax tree nested too deeply") from e


# ------------------------------------------------------------------ IR

_INSTRS: Dict[str, Type] = {c.__name__: c for c in (ir.Load, ir.Move, ir.Store, ir.Live, ir.Dead, ir.Release, ir.Cell, ir.Unary, ir.Binary, ir.Cast, ir.Call, ir.Len, ir.Test, ir.Check, ir.Access, ir.Borrow, ir.Clobber, ir.Str, ir.Global, ir.Make, ir.PatTest, ir.Project, ir.Opaque, ir.Barrier)}
_TERMS: Dict[str, Type] = {c.__name__: c for c in (ir.Jump, ir.Branch, ir.Return, ir.Unreachable)}


def _val(v: Any) -> Any:
    """An operand or reference as data: `{"t": id}` temp, `{"c": value, "ty": ..}` constant, `{"l": id}` local, `{"b": id}` block."""
    if isinstance(v, ir.Temp):
        return {"t": v.id}
    if isinstance(v, ir.Const):
        return {"c": v.value, "ty": v.ty}
    if isinstance(v, ir.Local):
        return {"l": v.id}
    if isinstance(v, ir.Block):
        return {"b": v.id}
    if isinstance(v, (list, tuple)):
        return [_val(x) for x in v]
    if v is None or isinstance(v, (bool, int, str)):
        return v
    raise SerialError(f"cannot encode {type(v).__name__}")


def _item(i: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {("instr" if isinstance(i, ir.Instr) else "term"): type(i).__name__}
    for f in dataclasses.fields(i):
        if f.name != "site":
            out[f.name] = _val(getattr(i, f.name))
    if i.site is not None:
        out["at"] = _rng(i.site.range)
    return out


def _function(fn: ir.Function) -> Dict[str, Any]:
    temps: Dict[int, ir.Temp] = {}
    for b in fn.blocks:
        for i in b.instrs:
            t = i.defs()
            if t is not None:
                temps[t.id] = t
    return {
        "name": fn.name, "ret": fn.ret, "attrs": list(fn.attrs), "params": [p.id for p in fn.params],
        "locals": [{"id": x.id, "name": x.name, "ty": x.ty, "mut": x.mut, "param": x.param, "length": x.length, "owns": x.owns, "taint": x.taint} for x in fn.locals],
        "temps": [{"id": k, "ty": v.ty, **({"owns": True} if v.owns else {})} for k, v in temps.items()],
        "blocks": [{"id": b.id, "label": b.label, "instrs": [_item(i) for i in b.instrs], "term": _item(b.term) if b.term else None}
                   for b in fn.blocks],
        "elided": [_item(c) for c in fn.elided],
        "taint_sinks": sorted(fn.taint_sinks),
    }


def ir_to_data(m: ir.Module) -> Dict[str, Any]:
    d = _wrap(IR_FORMAT, IR_VERSION, "functions", [_function(f) for f in m.functions])
    d["skipped"] = [list(x) for x in m.skipped]
    return d


def _has_default(cls: Type, name: str) -> bool:
    f = next(f for f in dataclasses.fields(cls) if f.name == name)
    return f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING


class _Reader:
    def __init__(self) -> None:
        self.locals: Dict[int, ir.Local] = {}
        self.temps: Dict[int, ir.Temp] = {}
        self.blocks: Dict[int, ir.Block] = {}

    @staticmethod
    def need(d: Any, key: str, kind: type, what: str) -> Any:
        v = d.get(key) if isinstance(d, dict) else None
        if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
            raise SerialError(f"{what}: {key!r} is missing or not a {kind.__name__}")
        return v

    @staticmethod
    def opt(d: dict, key: str, kind: type, what: str) -> Any:
        v = d.get(key)
        if v is not None and (not isinstance(v, kind) or (kind is int and isinstance(v, bool))):
            raise SerialError(f"{what}: {key!r} is not a {kind.__name__}")
        return v

    @staticmethod
    def ref(table: Dict[int, Any], id_: Any, what: str) -> Any:
        if not isinstance(id_, int) or isinstance(id_, bool) or id_ not in table:
            raise SerialError(f"{what}: dangling reference {id_!r}")
        return table[id_]

    def value(self, v: Any, what: str) -> Any:
        if isinstance(v, list):
            return [self.value(x, what) for x in v]
        if isinstance(v, dict):
            if set(v) == {"t"}:
                return self.ref(self.temps, v["t"], what)
            if set(v) == {"l"}:
                return self.ref(self.locals, v["l"], what)
            if set(v) == {"b"}:
                return self.ref(self.blocks, v["b"], what)
            if set(v) == {"c", "ty"} and isinstance(v["c"], (bool, int, str)):
                return ir.Const(v["c"], v["ty"])
            raise SerialError(f"{what}: bad operand {v!r}")
        if v is None or isinstance(v, (bool, int, str)):
            return v
        raise SerialError(f"{what}: bad operand {v!r}")

    def item(self, d: Any, table: Dict[str, Type], tag: str, what: str) -> Any:
        cls = table.get(d.get(tag)) if isinstance(d, dict) else None
        if cls is None:
            raise SerialError(f"{what}: unknown {tag} {d.get(tag) if isinstance(d, dict) else d!r}")
        names = [f.name for f in dataclasses.fields(cls) if f.name != "site"]
        extra = set(d) - set(names) - {tag, "at"}
        missing = [n for n in names if n not in d and not _has_default(cls, n)]
        if extra or missing:
            raise SerialError(f"{what}: {cls.__name__} fields {sorted(extra | set(missing))} do not match")
        obj = cls(**{n: self.value(d[n], what) for n in names if n in d})
        if isinstance(obj, ir.Call) and isinstance(obj.ensures, list):
            obj.ensures = (obj.ensures[0], obj.ensures[1]) if len(obj.ensures) == 2 else None
        if "at" in d:
            obj.site = ast.Node(_unrng(d["at"]))
        return obj

    def function(self, d: Any) -> ir.Function:
        what = f"function {d.get('name')!r}" if isinstance(d, dict) else "function"
        name = self.need(d, "name", str, what)
        for x in self.need(d, "locals", list, what):
            loc = ir.Local(self.need(x, "id", int, what), self.need(x, "name", str, what), self.need(x, "ty", str, what),
                           bool(x.get("mut", True)), bool(x.get("param", False)), self.opt(x, "length", int, what), bool(x.get("owns", False)), bool(x.get("taint", False)))
            self.locals[loc.id] = loc
        for t in self.need(d, "temps", list, what):
            tid = self.need(t, "id", int, what)
            self.temps[tid] = ir.Temp(tid, self.opt(t, "ty", str, what), bool(t.get("owns", False)))
        raw = self.need(d, "blocks", list, what)
        for b in raw:
            blk = ir.Block(self.need(b, "id", int, what), self.need(b, "label", str, what))
            self.blocks[blk.id] = blk
        fn = ir.Function(name, [self.ref(self.locals, p, what) for p in self.need(d, "params", list, what)], self.opt(d, "ret", str, what),
                         list(self.locals.values()), [self.blocks[b["id"]] for b in raw], list(self.need(d, "attrs", list, what)))
        for b in raw:
            blk = self.blocks[b["id"]]
            blk.instrs = [self.item(i, _INSTRS, "instr", what) for i in self.need(b, "instrs", list, what)]
            t = b.get("term")
            blk.term = self.item(t, _TERMS, "term", what) if t is not None else None
        fn.elided = [self.item(c, _INSTRS, "instr", what) for c in d.get("elided", [])]
        sinks = d.get("taint_sinks")
        if sinks is not None:
            if not isinstance(sinks, list) or not all(isinstance(x, str) for x in sinks):
                raise SerialError(f"{what}: 'taint_sinks' is not a list of strings")
            fn.taint_sinks = frozenset(sinks)
        return fn


def ir_from_data(d: Any) -> ir.Module:
    """Rebuild a module and check every function with `ir.verify`; a malformed or inconsistent document is a `SerialError`."""
    fns = _unwrap(d, IR_FORMAT, IR_VERSION, "functions")
    if not isinstance(fns, list):
        raise SerialError("functions is not a list")
    m = ir.Module()
    for f in fns:
        fn = _Reader().function(f)
        problems = ir.verify(fn)
        if problems:
            raise SerialError(f"function {fn.name!r}: {problems[0]}")
        m.functions.append(fn)
    m.skipped = [(str(a), str(b)) for a, b in d.get("skipped", [])]
    return m


def dumps_ir(m: ir.Module, as_cbor: bool = False) -> Any:
    data = ir_to_data(m)
    return cbor.dumps(data) if as_cbor else json.dumps(data, separators=(",", ":"))


def loads_ir(data: Any) -> ir.Module:
    try:
        d = cbor.loads(data) if isinstance(data, (bytes, bytearray)) else json.loads(data)
    except (cbor.CborError, json.JSONDecodeError) as e:
        raise SerialError(str(e)) from e
    return ir_from_data(d)
