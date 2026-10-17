# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Diagnostics (docs/kif-proc-language.md section 10)."""

from __future__ import annotations

from dataclasses import dataclass

from .source import Range

ERROR = 1
WARNING = 2
INFO = 3   # a suggestion: never fails a build, not even with -Werror


@dataclass(frozen=True)
class Diagnostic:
    range: Range
    severity: int
    code: str
    message: str

    def format(self, path: str) -> str:
        kind = {ERROR: "error", INFO: "note"}.get(self.severity, "warning")
        return f"{path}:{self.range.start.line + 1}:{self.range.start.col + 1}: {kind} {self.code}: {self.message}"


def error(rng: Range, code: str, message: str) -> Diagnostic:
    return Diagnostic(rng, ERROR, code, message)


def info(rng: Range, code: str, message: str) -> Diagnostic:
    return Diagnostic(rng, INFO, code, message)


def warning(rng: Range, code: str, message: str) -> Diagnostic:
    return Diagnostic(rng, WARNING, code, message)
