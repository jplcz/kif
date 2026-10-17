# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The identity of the check machinery (docs/kif-module-contracts.md, section 3, key `compiler`).

`proved` and `kept_checks` in a `.kifm` are claims of the exporting compiler's prover. `prover_id()` is a sha256 over the source of
everything that decides them (the IR builder, the passes, the provers and the eliding pass, the fact rules), so *any* change of what
those can prove changes the identity. An importer that relies on the claims compares the two.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache

_HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("elide.py", "facts.py", "ir.py", "irtypes.py", "passes.py", "prove.py")


@lru_cache(maxsize=1)
def prover_id() -> str:
    h = hashlib.sha256()
    for name in FILES:
        with open(os.path.join(_HERE, name), "rb") as f:
            h.update(name.encode("ascii") + b"\0" + f.read().replace(b"\r\n", b"\n") + b"\0")
    return "sha256:" + h.hexdigest()
