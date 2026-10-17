# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Which parts of one variable two places name (shared by the checker's borrow tracking, `bodies.py`, and the IR borrow check,
`borrowck.py`).

A place's *path* is the chain of projections from the variable: `.f` a field, `[]` an element, `*` a pointer target (`.items[]` is
the elements of the field `items`). Two places overlap when one path is a prefix of the other, at a projection boundary; the
variable itself (path "") overlaps everything. Two elements with different constant indexes (`a[0]`, `a[1]`) do not overlap; a
variable index is assumed to hit any element. Whether the two borrows are compatible is the caller's business."""

from __future__ import annotations

from typing import Optional


def overlap(p: str, pi: Optional[object], q: str, qi: Optional[object]) -> bool:
    """Do the places `p` (element index `pi`, a constant or None) and `q` (`qi`) share memory?"""
    if p == q:
        return not (p.endswith("[]") and pi is not None and qi is not None and pi != qi)
    a, b = (p, q) if len(p) <= len(q) else (q, p)
    return a == "" or (b.startswith(a) and b[len(a)] in ".[")
