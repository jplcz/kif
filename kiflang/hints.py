# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Editor hints for run-time checks: which ones the IR provers (prove.py) drop and which stay.

`FunctionHints` is the work for one function; `HintCache` keeps the result per function so an edit elsewhere in the file costs
nothing, and a function being edited is left out until the document has been quiet for a grace period (see lsp.py)."""

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from . import ast, ir, passes

# what the user sees for each check kind (the IR kind is `some` for an optional's `.value()`)
LABELS = {"index": "bounds", "range": "range", "some": "has-value", "div": "divisor", "shift": "shift", "non_null": "null"}
MODES = ("off", "kept", "all")
TAINT_KINDS = ("barrier", "untaint")   # hints of the taint feature (docs/kif-taint.md), shown unless `inlayHints.taint` is off


@dataclass(frozen=True)
class Hint:
    line: int  # 0-based, absolute in the document
    col: int
    kind: str
    proved: bool
    why: str  # the proof, empty for a kept check
    tip: str = ""  # taint hints (`barrier`, `untaint`): the tooltip


def function_hints(fn: ast.FnDecl) -> List[Hint]:
    """One hint per run-time check of `fn`, at the end of the expression it guards. A function the IR builder does not handle has none."""
    try:
        f = ir.build_function(fn)
    except ir.Unsupported:
        return []
    passes.run_function(f, ["prove", "barriers"])
    out: Dict[Tuple[int, int, str], Hint] = {}
    for _, c in f.checks():
        if c.tag or c.site is None or getattr(c.site, "range", None) is None:
            continue
        end = c.site.range.end
        key = (end.line, end.col, c.kind)
        old = out.get(key)
        if old is not None and not old.proved:
            continue  # two checks at one spot: the weaker (kept) one is what the reader needs to see
        out[key] = Hint(end.line, end.col, c.kind, c.proof is not None, c.proof or "")
    from . import taint_ir

    for n in taint_ir.explain(f):
        if n.kind in TAINT_KINDS and n.end_line:
            out[(n.end_line - 1, n.end_col - 1, n.kind + n.text)] = Hint(n.end_line - 1, n.end_col - 1, n.kind, False, "", n.text)
    return sorted(out.values(), key=lambda h: (h.line, h.col))


def _slice(lines: List[str], r) -> str:
    if r.start.line == r.end.line:
        return lines[r.start.line][r.start.col:r.end.col]
    mid = lines[r.start.line + 1:r.end.line]
    return "\n".join([lines[r.start.line][r.start.col:]] + mid + [lines[r.end.line][:r.end.col]])


def functions_of(tree) -> List[ast.FnDecl]:
    items = list(getattr(tree, "items", []))
    fns: List[ast.FnDecl] = []
    for it in items:
        if isinstance(it, ast.FnDecl) and it.body is not None:
            fns.append(it)
        elif isinstance(it, ast.ImplDecl):
            fns.extend(f for f in it.items if isinstance(f, ast.FnDecl) and f.body is not None)
    return fns


class HintCache:
    """Hints per function, keyed by the function's own text and by the text of every declaration that is not a function.

    The second part matters because a struct's field types, an array length or an enum decide what a check can be proved from;
    editing another function does not change the key, so it does not recompute this one."""

    LIMIT = 512  # functions kept; the oldest are dropped first

    def __init__(self) -> None:
        self.entries: Dict[Tuple[str, str], List[Hint]] = {}

    @staticmethod
    def context(tree, lines: List[str]) -> str:
        parts = []
        for it in getattr(tree, "items", []):
            if isinstance(it, ast.FnDecl):
                continue
            if isinstance(it, ast.ImplDecl):
                parts.append("impl " + _slice(lines, it.range).split("{", 1)[0])  # the header only; its methods are keyed one by one
            else:
                parts.append(_slice(lines, it.range))
        return "\n".join(parts)

    def lookup(self, key: Tuple[str, str]) -> Optional[List[Hint]]:
        return self.entries.get(key)

    def store(self, key: Tuple[str, str], hints: List[Hint]) -> None:
        self.entries.pop(key, None)
        self.entries[key] = hints
        while len(self.entries) > self.LIMIT:
            self.entries.pop(next(iter(self.entries)))

    def key(self, fn: ast.FnDecl, lines: List[str], context: str) -> Tuple[str, str]:
        return (_slice(lines, fn.range), context)

    def for_function(self, fn: ast.FnDecl, lines: List[str], context: str, allow_compute: bool) -> Optional[List[Hint]]:
        """The hints of `fn` at its current position, or None when they are not at hand and `allow_compute` is false (the caller
        is inside the grace period). Cached hints are relative to the function's start, so moving a function keeps them valid."""
        key = self.key(fn, lines, context)
        rel = self.lookup(key)
        s = fn.range.start
        if rel is None:
            if not allow_compute:
                return None
            rel = [replace(h, line=h.line - s.line, col=h.col - (s.col if h.line == s.line else 0)) for h in function_hints(fn)]
            self.store(key, rel)
        return [replace(h, line=h.line + s.line, col=h.col + (s.col if h.line == 0 else 0)) for h in rel]
