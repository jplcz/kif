# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`.kifa`, the standalone IR attestation (docs/kif-module-contracts.md, section 13.2 item 4).

A canonical CBOR map `{magic "KIFA", version, target, profile, compiler, functions: {name: ir_hash}, signature?}`. It is signed and
verified with `modsign` exactly like a `.kifm` header, so the trust, revocation and `[signing]` rules are the same. `compare` re-analyzes a
source and reports each function as match, changed, added or removed."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import cbor

MAGIC = "KIFA"
VERSION = 1
MATCH, CHANGED, ADDED, REMOVED = "match", "changed", "added", "removed"


class AttestError(ValueError):
    """A `.kifa` that is malformed or of an unsupported version."""


def hashes(mod) -> Dict[str, str]:
    """`{function name: ir hash}` of an analysed module (raw IR, the same value as `kif ir --hash` and the C comment)."""
    from . import irhash, passes

    return dict(irhash.module_hashes(passes.run_module(mod.tree, []).module))


def build(mod, target_name: str, profile: Optional[str] = None, sign: Optional[Tuple[bytes, Optional[bytes]]] = None) -> bytes:
    """The `.kifa` bytes of an analysed module; `sign` is `(private key PEM, certificate or None)`."""
    from . import __version__, modsec, prover_id, target

    tgt = target.load(target_name)
    header: Dict[str, Any] = {
        "magic": MAGIC, "version": VERSION, "target": tgt.name, "profile": tgt.profile(profile)["name"],
        "compiler": {"name": "kif", "version": __version__, "prover": prover_id.prover_id(), "security_schema": modsec.SCHEMA},
        "functions": hashes(mod),
    }
    if sign is not None:
        from . import modsign

        header = modsign.sign(header, sign[0], sign[1])
    return cbor.dumps(header)


def decode(data: bytes) -> Dict[str, Any]:
    """Validate and return the header map; `AttestError` when anything is off."""
    try:
        h = cbor.loads(data)
    except cbor.CborError as exc:
        raise AttestError(f"not a valid .kifa: {exc}") from exc
    if not isinstance(h, dict) or h.get("magic") != MAGIC:
        raise AttestError("not a .kifa file (bad magic)")
    if not isinstance(h.get("version"), int) or not 1 <= h["version"] <= VERSION:
        raise AttestError(f"unsupported .kifa version {h.get('version')!r} (this compiler reads up to {VERSION})")
    for key in ("target", "profile"):
        if not isinstance(h.get(key), str):
            raise AttestError(f".kifa field {key!r} is missing or has the wrong type")
    comp = h.get("compiler")
    if not isinstance(comp, dict) or not all(isinstance(comp.get(k), str) for k in ("name", "version", "prover")):
        raise AttestError(".kifa field 'compiler' is missing or malformed")
    fns = h.get("functions")
    if not isinstance(fns, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.startswith("sha256:") for k, v in fns.items()):
        raise AttestError(".kifa field 'functions' must map names to sha256 hashes")
    if "signature" in h:
        from . import modsign

        try:
            modsign.validate_shape(h["signature"])
        except ValueError as exc:
            raise AttestError(f".kifa {exc}") from exc
    return h


def compare(header: Dict[str, Any], mod) -> List[Tuple[str, str]]:
    """`(name, status)` per function, sorted by name: `match`, `changed` (another hash), `added` (in the source only) or `removed`."""
    attested, now = header["functions"], hashes(mod)
    out = []
    for name in sorted(set(attested) | set(now)):
        if name not in now:
            out.append((name, REMOVED))
        elif name not in attested:
            out.append((name, ADDED))
        else:
            out.append((name, MATCH if attested[name] == now[name] else CHANGED))
    return out
