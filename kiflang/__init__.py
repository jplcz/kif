# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""kif language front end (docs/kif-proc-language.md). Currently: lexer, parser and syntax tree."""

__version__ = "0.1.0"  # keep in step with pyproject.toml (tests/test_modsec.py checks)

from .diagnostics import ERROR, WARNING, Diagnostic
from .parser import parse

__all__ = ["parse", "Diagnostic", "ERROR", "WARNING"]
