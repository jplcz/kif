# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`#[require_proved]` across a `.kifm` boundary (docs/kif-module-contracts.md, section 5).

A function under `#[require_proved]` promises that no run-time check of the demanded kinds remains in code it covers. A call into a
precompiled module reaches a body the importer cannot analyze, so the promise rests on what the vendor recorded:

* KIFI040 (error): the callee's `kept_checks` holds a demanded kind, or the vendor recorded no analysis (`"unknown"`);
* KIFI041: the vendor's prover is not this compiler's (`compiler.prover`), so its `proved` / `kept_checks` claims rest on another
  prover. A warning, an error when `prover_must_match` is set (the project's `[signing] prover = "same"`, or `--prover-must-match`).
"""

from __future__ import annotations

from . import ast, astutil
from . import diagnostics as dg
from . import prover_id
from .ir import required_kinds


def _scope_kinds(bc, f):
    impl_attrs = []
    for it in bc.mod.tree.items:
        if isinstance(it, ast.ImplDecl) and any(m is f for m in it.items):
            impl_attrs = it.attrs
    return required_kinds(getattr(bc.mod.tree, "inner_attrs", []), impl_attrs, f.attrs)


def check_body(bc, f) -> None:
    """Called by the body checker next to the taint check, after `Call.decl` is known."""
    if getattr(f, "body", None) is None:
        return
    kinds = _scope_kinds(bc, f)
    if not kinds:
        return
    values = getattr(getattr(bc.mod, "cfg", None), "values", None) or {}
    must_match = bool(values.get("prover_must_match"))
    mine = prover_id.prover_id()
    for node in astutil.walk(f.body):
        decl = getattr(node, "decl", None) if isinstance(node, (ast.Call, ast.MethodCall)) else None
        vendor = getattr(decl, "vendor", None)
        if vendor is None:
            continue
        name, kept = decl.name.text, vendor.entry.get("kept_checks")
        where = f"{name}() of {vendor.module}.kifm"
        if kept == "unknown":
            bc.d.append(dg.error(node.range, "KIFI040", f"#[require_proved] demands proofs, but the vendor recorded no analysis of {where}"))
        else:
            bad = sorted(set(kept) & kinds)
            if bad:
                bc.d.append(dg.error(node.range, "KIFI040", f"#[require_proved] demands a proof of {', '.join(bad)} checks, but {where} keeps "
                                     f"{'it' if len(bad) == 1 else 'them'} in the vendor's compiled body"))
        if vendor.compiler["prover"] != mine:
            make = dg.error if must_match else dg.warning
            bc.d.append(make(node.range, "KIFI041", f"{where} was proved by another compiler ({vendor.compiler['name']} {vendor.compiler['version']}, "
                             "prover " + vendor.compiler["prover"][:19] + "): its `proved` and `kept_checks` claims rest on that prover"))
