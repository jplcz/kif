# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Source positions and ranges (0-based line/column, like LSP)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Pos:
    line: int
    col: int


@dataclass(frozen=True)
class Range:
    start: Pos
    end: Pos

    def contains(self, pos: Pos) -> bool:
        """True if pos is inside the range (end inclusive, so a cursor right after a name still hits it)."""
        return self.start <= pos <= self.end

    def covers(self, other: "Range") -> bool:
        return self.start <= other.start and other.end <= self.end


def span(first: Range, last: Range) -> Range:
    return Range(first.start, last.end)
