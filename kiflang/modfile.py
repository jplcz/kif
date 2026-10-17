# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Precompiled modules (`.kifm`, docs/kif-proc-language.md section 15).

A `.kifm` is a CBOR map that carries everything an importer needs and nothing it must not see: the module's source with the
bodies of its ordinary (non-generic) functions removed, a digest of that text and the digests of the modules it imports. The
implementation lives in the object code the vendor ships next to it. What stays readable is exactly what a consumer needs
in source form:

- declarations: structs, enums, interfaces, aliases, `extern` items, function and method signatures, doc comments;
- `const` values (importers evaluate them);
- generic functions, methods of generic impls, interface default methods, hooks, `gen fn`s and derives (they are instantiated
  or expanded in the importer, so they cannot be compiled ahead of time). `embedded` in the header lists them, so the vendor
  can see what a release exposes and keep secrets out of those.

Keys of the map: `magic` ("KIFM"), `version` (1), `name`, `text`, `digest` (sha256 hex of `text`), `deps` (import path ->
digest of the imported module's exported text), `stripped` (number of bodies removed) and `embedded` (names). Unknown keys are
ignored, a newer `version` is rejected.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

from . import ast, cbor

MAGIC = "KIFM"
VERSION = 1


class ModuleFileError(ValueError):
    """A `.kifm` that is malformed, truncated, tampered with or of an unsupported version."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _offset(starts: List[int], pos) -> int:
    return starts[pos.line] + pos.col


def strip(mod) -> Tuple[str, int, List[str]]:
    """(exported text, number of bodies removed, names of the generic/default bodies that stay)."""
    text = mod.source
    starts, n = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(n)
        n += len(line)
    starts.append(n)
    cuts: List[Tuple[int, int]] = []
    embedded: List[str] = []

    def fn(f: ast.FnDecl, owner: str, impl_generic: bool) -> None:
        if f.body is None:
            return
        real = [g for g in f.generics if not g.is_lifetime]
        if f.is_gen or real or impl_generic:
            embedded.append(owner + f.name.text)
        else:
            cuts.append((_offset(starts, f.body.range.start), _offset(starts, f.body.range.end)))

    for it in mod.tree.items:
        if isinstance(it, ast.FnDecl):
            fn(it, "", False)
        elif isinstance(it, ast.ImplDecl):
            generic = any(not g.is_lifetime for g in it.generics)
            for m in it.items:
                if isinstance(m, ast.FnDecl):
                    fn(m, it.target.text + "::" if isinstance(it.target, ast.PathType) else "", generic)
        elif isinstance(it, ast.Interface):
            for m in it.members:
                if isinstance(m, ast.Method) and m.body is not None:
                    embedded.append(f"{it.name.text}::{m.name.text}")
                elif isinstance(m, ast.Hook):
                    embedded.append(f"{it.name.text}::{m.kind}")
    for a, b in sorted(cuts, reverse=True):
        text = text[:a] + ";" + text[b:]
    return text, len(cuts), embedded


def exported_text(mod) -> str:
    return mod.source if getattr(mod, "header", None) else strip(mod)[0]


def digest_of(mod) -> str:
    """Digest of a module as an importer sees it: identical for the source and for its `.kifm`."""
    header = getattr(mod, "header", None)
    return header["digest"] if header else sha256(strip(mod)[0])


def export(mod) -> bytes:
    """The `.kifm` bytes of an analysed module (its imports are recorded by digest, not embedded)."""
    text, stripped, embedded = strip(mod)
    deps: Dict[str, str] = {}
    for it in mod.tree.items:
        if isinstance(it, ast.Import) and it.path != "reloco.kif":
            dep = getattr(mod, "imported", {}).get(it.path)
            if dep is not None:
                deps[it.path] = digest_of(dep)
    return cbor.dumps({
        "magic": MAGIC, "version": VERSION, "name": mod.name, "text": text, "digest": sha256(text),
        "deps": deps, "stripped": stripped, "embedded": sorted(embedded),
    })


def decode(data: bytes) -> Dict[str, Any]:
    """Validate and return the header map (including `text`); `ModuleFileError` when anything is off."""
    try:
        h = cbor.loads(data)
    except cbor.CborError as exc:
        raise ModuleFileError(f"not a valid .kifm: {exc}") from exc
    if not isinstance(h, dict) or h.get("magic") != MAGIC:
        raise ModuleFileError("not a .kifm file (bad magic)")
    if not isinstance(h.get("version"), int) or h["version"] > VERSION or h["version"] < 1:
        raise ModuleFileError(f"unsupported .kifm version {h.get('version')!r} (this compiler reads up to {VERSION})")
    for key, kind in (("name", str), ("text", str), ("digest", str), ("deps", dict)):
        if not isinstance(h.get(key), kind):
            raise ModuleFileError(f".kifm field {key!r} is missing or has the wrong type")
    if sha256(h["text"]) != h["digest"]:
        raise ModuleFileError(".kifm digest does not match its text (file was modified or corrupted)")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in h["deps"].items()):
        raise ModuleFileError(".kifm field 'deps' must map paths to digests")
    return h
