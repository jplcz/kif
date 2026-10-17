# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Safety elision (the `elide` pass, docs/kif-ir-passes.md): remove the run-time checks the provers showed cannot fail.

A backend emits every `Check` that is in the IR and decides nothing, so what it should not emit has to be removed from the IR. This pass
does it, after `prove`, and records what it removed in `Function.elided` (the `proof` of each says why, so a tool can still explain
the elision; the dump lists them as `// elided: ...`).

Rules, per check:
- **Operation checks** (`index`, `range`, `div`, `shift`, `some`, `non_null` of an operation): removed when proved, unless the target's
  `access.<class>` tier is `checked` (`access.optional` for `some` / `non_null`), which keeps every check of the class.
- **Assumptions** (a parameter fact at the entry of a function that has a guarded entry): always removed. The caller or the guard in `F`
  established them; the body `F__nofacts` assumes them.
- **Requirements at a call** (`Check.call` is the `Call.id`): the callee's guarded entry `F` checks them itself, so the checks here exist
  only to be proved. They are always removed. If every one of a call's requirements is proved (and the tiers allow eliding them) the
  call is marked `unguarded`: it may call the unguarded twin `F__nofacts`. Otherwise it calls `F`, which traps. A requirement that
  provably fails (`Check.refuted`) stays in the IR, for the caller to report."""

from __future__ import annotations

from typing import Dict, List

from . import ir

_CLASS = {"index": "index", "range": "range", "div": "div", "shift": "shift", "some": "optional", "non_null": "optional"}


def _may(kind: str, tiers: Dict[str, str]) -> bool:
    return tiers.get(_CLASS.get(kind, kind), "prove") != "checked"


def elide(fn: ir.Function, tiers: Dict[str, str]) -> int:
    """Remove the provable checks of `fn`; returns how many were removed. `tiers` is the target's `access` map (class -> tier)."""
    kept: Dict[int, bool] = {}  # Call.id -> every requirement of it is proved and may be elided
    for _, c in fn.checks():
        if c.call:
            ok = c.proof is not None and c.refuted is None and _may(c.kind, tiers)
            kept[c.call] = kept.get(c.call, True) and ok
    removed = 0
    fn.elided_at = []
    for b in fn.blocks:
        keep = []
        gone: List[ir.Check] = []
        for i in b.instrs:
            if isinstance(i, ir.Check) and i.refuted is None and (
                    i.call or (i.proof is not None and (i.tag and not i.call or _may(i.kind, tiers)))):
                fn.elided.append(i)
                gone.append(i)
                removed += 1
                continue
            fn.elided_at += [(i, c) for c in gone]  # the emitter documents a removed check just before the instruction that followed it
            gone = []
            if isinstance(i, ir.Call) and kept.get(i.id):
                i.unguarded = True
            keep.append(i)
        fn.elided_at += [(b.term, c) for c in gone]
        b.instrs = keep
    return removed
