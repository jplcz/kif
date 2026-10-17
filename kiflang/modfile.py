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
digest of the imported module's exported text), `stripped` (number of bodies removed), `embedded` (names), `compiler` (the
exporting compiler: name, version, prover identity, security schema), `security` (every safety contract and security-boundary fact an
importer's checks need, `kiflang/modsec.py`) and `security_digest` (sha256 hex of the canonical encoding of `security`). Unknown keys
are ignored, a newer `version` is rejected. Design: docs/kif-module-contracts.md.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from . import ast, cbor, modsec

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


def security_digest(security: Any) -> str:
    return hashlib.sha256(cbor.dumps(security)).hexdigest()


def export(mod, target_name: str = "c", profile: Optional[str] = None, sign: Optional[Tuple[bytes, Optional[bytes]]] = None) -> bytes:
    """The `.kifm` bytes of an analysed module (its imports are recorded by digest, not embedded). `target_name` and `profile` are what
    the module was analysed for; they are recorded in the `security` section. `sign` is `(private key PEM, certificate or None)`: the
    header then carries a `signature` over everything else (docs/kif-module-contracts.md, section 11); `modsign.CryptoError` when it cannot."""
    from . import modsec, prover_id, target
    from . import __version__

    tgt = target.load(target_name)
    security = modsec.build(mod, tgt.name, tgt.profile(profile)["name"], tgt.language)
    text, stripped, embedded = strip(mod)
    deps: Dict[str, str] = {}
    for it in mod.tree.items:
        if isinstance(it, ast.Import) and it.path != "reloco.kif":
            dep = getattr(mod, "imported", {}).get(it.path)
            if dep is not None:
                deps[it.path] = digest_of(dep)
    header = {
        "magic": MAGIC, "version": VERSION, "name": mod.name, "text": text, "digest": sha256(text),
        "deps": deps, "stripped": stripped, "embedded": sorted(embedded),
        "compiler": {"name": "kif", "version": __version__, "prover": prover_id.prover_id(), "security_schema": modsec.SCHEMA},
        "security": security, "security_digest": security_digest(security),
    }
    if sign is not None:
        from . import modsign

        header = modsign.sign(header, sign[0], sign[1])
    return cbor.dumps(header)


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
    comp = h.get("compiler")
    if not isinstance(comp, dict) or not all(isinstance(comp.get(k), str) for k in ("name", "version", "prover")) \
            or not isinstance(comp.get("security_schema"), int):
        raise ModuleFileError(".kifm field 'compiler' is missing or malformed")
    if comp["security_schema"] > modsec.SCHEMA or comp["security_schema"] < 1:
        raise ModuleFileError(f"unsupported .kifm security schema {comp['security_schema']!r} (this compiler reads up to {modsec.SCHEMA})")
    try:
        modsec.validate(h.get("security"))
    except ValueError as exc:
        raise ModuleFileError(f".kifm security section: {exc}") from exc
    if not isinstance(h.get("security_digest"), str) or security_digest(h["security"]) != h["security_digest"]:
        raise ModuleFileError(".kifm security section does not match its digest (file was modified or corrupted)")
    if "signature" in h:
        from . import modsign

        try:
            modsign.validate_shape(h["signature"])
        except ValueError as exc:
            raise ModuleFileError(f".kifm {exc}") from exc
    return h
