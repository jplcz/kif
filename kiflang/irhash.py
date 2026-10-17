# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""A meaning-based identity for the IR of a function (docs/kif-module-contracts.md, section 13).

`ir_hash(fn)` is the sha256 of a canonical CBOR form of the function's **raw** IR (what `ir.build_function` makes, before any pass).
Positions, comments, reason texts, proof texts and every name are left out; locals, temps and blocks are renumbered in order of first
appearance, so `fn f(x: u32) -> u32 { 2 * x }` has the same hash under any name, in any file, with any comments around it.

What is in the hash and what is out is written down in `HASHED` and `EXCLUDED`, field by field. The encoder walks `HASHED` only, so a
field added to the IR is *not* hashed until someone classifies it, and `tests/test_irhash.py` fails until they do.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from . import cbor
from . import ir

SCHEMA = "kif-ir-hash/1"

# class -> the fields that are part of the identity (operands, locals, temps and blocks are encoded by their renumbered id)
HASHED: Dict[str, Tuple[str, ...]] = {
    "Local": ("ty", "mut", "param", "length", "owns", "taint", "wipe"),
    "Temp": ("ty", "owns"),
    "Const": ("value", "ty"),
    "Load": ("dst", "local"),
    "Move": ("dst", "local"),
    "Store": ("local", "src", "init", "drop_old"),
    "Live": ("local",),
    "Dead": ("local", "drop"),
    "Release": ("value", "drop"),
    "Cell": ("dst", "name", "mut"),
    "Unary": ("dst", "op", "a"),
    "Binary": ("dst", "op", "a", "b"),
    "Cast": ("dst", "a", "ty"),
    "Call": ("dst", "callee", "args", "recv", "kind", "decl", "id", "flows", "ensures", "preserves", "taint"),
    "Len": ("dst", "base"),
    "Test": ("dst", "kind", "base"),
    "Check": ("kind", "args", "ty", "tag", "call"),
    "Access": ("dst", "kind", "base", "args", "name"),
    "Borrow": ("dst", "local", "mut", "path", "rng", "idx"),
    "Clobber": ("local", "src", "elem", "path", "idx"),
    "Str": ("dst", "text"),
    "Global": ("dst", "name"),
    "Make": ("dst", "kind", "args", "names"),
    "PatTest": ("dst", "base", "pattern"),
    "Project": ("dst", "base", "path"),
    "Opaque": ("dst", "what", "args"),
    "Wipe": ("local", "path", "ty", "volatile"),
    "Barrier": ("kind", "dst", "a"),
    "Jump": ("target",),
    "Branch": ("cond", "then", "otherwise"),
    "Return": ("value",),
    "Unreachable": (),
    "Block": ("instrs", "term"),
    "Function": ("params", "ret", "locals", "blocks", "attrs", "require_proved", "taint_sinks"),
}

# class -> the fields that are deliberately not part of it, and why
EXCLUDED: Dict[str, Dict[str, str]] = {
    "Local": {"id": "renumbered", "name": "names are not meaning"},
    "Temp": {"id": "renumbered"},
    "Instr": {"site": "a source position: moving code is not changing it"},
    "Term": {"site": "a source position"},
    "Call": {"reason": "the text of an `untaint!` reason is prose", "unguarded": "set by the `elide` pass, not in raw IR"},
    "Check": {"proof": "set by the provers, and prose", "refuted": "set by the provers, and prose"},
    "Barrier": {"why": "prose"},
    "Wipe": {"why": "the audit comment is prose"},
    "Block": {"id": "renumbered", "label": "names are not meaning"},
    "Function": {"name": "the hash identifies the body", "node": "the syntax tree: positions and comments", "elided_at": "set by the `elide` pass", "elided": "set by the `elide` pass"},
}

def classified(cls_name: str) -> Tuple[str, ...]:
    """Every field of `cls_name` the tables mention, hashed or excluded (used by the classification test)."""
    cls = getattr(ir, cls_name)
    out: List[str] = list(HASHED.get(cls_name, ()))
    for key, base in ((cls_name, object), ("Instr", ir.Instr), ("Term", ir.Term)):
        if issubclass(cls, base):
            out += list(EXCLUDED.get(key, ()))
    return tuple(out)


class _Ids:
    """Renumbers locals, temps and blocks in order of first appearance."""

    def __init__(self) -> None:
        self.maps: Dict[str, Dict[int, int]] = {"l": {}, "t": {}, "b": {}}

    def __call__(self, kind: str, ident: int) -> int:
        m = self.maps[kind]
        return m.setdefault(ident, len(m))


def _value(v: Any, ids: _Ids) -> Any:
    if isinstance(v, ir.Temp):
        return {"t": ids("t", v.id)}
    if isinstance(v, ir.Const):
        return {"c": v.value, "ty": v.ty}
    if isinstance(v, ir.Local):
        return {"l": ids("l", v.id)}
    if isinstance(v, ir.Block):
        return {"b": ids("b", v.id)}
    if isinstance(v, (list, tuple)):
        return [_value(x, ids) for x in v]
    if isinstance(v, frozenset):
        return sorted(v)
    if v is None or isinstance(v, (bool, int, str)):
        return v
    raise TypeError(f"cannot hash {type(v).__name__}")


def _item(i: Any, ids: _Ids) -> Dict[str, Any]:
    name = type(i).__name__
    return {"i": name, **{f: _value(getattr(i, f), ids) for f in HASHED[name]}}


def _attrs(fn: ir.Function) -> List[Any]:
    """The function's attributes with their arguments, spelled without blanks, so `#[ensures( some )]` and `#[ensures(some)]` agree."""
    node = fn.node
    if node is None:
        return list(fn.attrs)
    return [[a.name.text, "".join((a.raw or "").split())] for a in node.attrs]


def canonical(fn: ir.Function) -> Dict[str, Any]:
    """The function as plain data, for hashing: renumbered, with positions, comments, prose and names left out."""
    ids = _Ids()
    # the parameters first, in order, so they are numbered 0..n-1 whatever the builder's ids are
    for p in fn.params:
        ids("l", p.id)
    blocks = []
    for b in fn.blocks:
        ids("b", b.id)
    for b in fn.blocks:
        blocks.append({"instrs": [_item(i, ids) for i in b.instrs], "term": _item(b.term, ids) if b.term is not None else None})
    locs = [{"id": ids("l", x.id), **{f: _value(getattr(x, f), ids) for f in HASHED["Local"]}} for x in fn.locals]
    temps: Dict[int, Optional[str]] = {}
    owns: Dict[int, bool] = {}
    for b in fn.blocks:
        for i in b.instrs:
            t = i.defs()
            if t is not None:
                temps[ids("t", t.id)] = t.ty
                owns[ids("t", t.id)] = t.owns
    return {
        "params": [ids("l", p.id) for p in fn.params], "ret": fn.ret,
        "attrs": _attrs(fn), "require_proved": _value(fn.require_proved, ids), "taint_sinks": sorted(fn.taint_sinks),
        "locals": locs, "temps": [[k, temps[k], owns[k]] for k in sorted(temps)], "blocks": blocks,
    }


def ir_hash(fn: ir.Function) -> str:
    """`sha256:<hex>` of the canonical form of `fn` (raw IR)."""
    return "sha256:" + hashlib.sha256(SCHEMA.encode("ascii") + b"\0" + cbor.dumps(canonical(fn))).hexdigest()


def module_hashes(m: ir.Module) -> List[Tuple[str, str]]:
    """`(name, hash)` for every function of the module, in order."""
    return [(f.name, ir_hash(f)) for f in m.functions]
