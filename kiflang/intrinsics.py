# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The compiler-synthesized intrinsics (docs/kif-taint.md section 7): one table for the checker, the editor and the docs.

They have no declaration in any `.kif` file. `bodies.py` types a call from `CT`, `complete.py` offers them after `ct::` and in
signature help, and `taint.py` points at them when an `#[untaint]` function computes by hand."""

from typing import Dict, NamedTuple, Tuple


class Intrinsic(NamedTuple):
    arity: int
    signature: str   # the synthesized forward declaration, shown in hovers and signature help
    doc: str
    snippet: str     # LSP snippet text


CT: Dict[str, Intrinsic] = {
    "select": Intrinsic(3, "fn select(c: bool, a: T, b: T) -> T", "`a` when `c`, else `b`, as a mask: no branch on the (tainted) condition.",
                        "select(${1:cond}, ${2:a}, ${3:b})"),
    "eq": Intrinsic(2, "fn eq(a: T, b: T) -> bool", "`a == b` as a value that is never branched on by the generated code.", "eq(${1:a}, ${2:b})"),
    "lt": Intrinsic(2, "fn lt(a: T, b: T) -> bool", "`a < b` as a value that is never branched on by the generated code.", "lt(${1:a}, ${2:b})"),
    "mask": Intrinsic(1, "fn mask(c: bool) -> usize", "All ones when `c`, else zero.", "mask(${1:cond})"),
    "clamp": Intrinsic(2, "fn clamp(x: T, n: T) -> T",
                       "`x` when `x < n`, else 0 (the `array_index_nospec` idiom). `#[untaint(trusted)]`: call it as "
                       "`untaint!(\"reason\", ct::clamp(x, n))`.", "clamp(${1:x}, ${2:n})"),
}

CT_ARITY: Dict[str, int] = {k: v.arity for k, v in CT.items()}

# namespaces the compiler owns (`spec` joins them with the speculation barriers of stage 6); a user module of the same name wins
NAMESPACES: Tuple[str, ...] = ("ct",)
