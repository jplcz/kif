# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The .kif formatter behind `kif fmt` (docs/kif-tools.md).

It only changes layout (leading whitespace, blank lines, trailing whitespace); it never joins, splits or reorders
lines, never edits a comment's text and never touches the inside of `header`/`code`/`m_prolog`/`m_epilog` blocks.
The token stream is compared before and after, so a formatter bug cannot change what a file means.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from . import lexer as lx

OPEN = {"{": "}", "(": ")", "[": "]", "#[": "]", "#![": "]"}
CLOSE = frozenset(OPEN.values())
# A line that starts with one of these continues the previous statement; so does a line after one that ends with one.
CONT_START = frozenset({".", "?", "&&", "||", "==", "!=", "<=", "+", "/", "%", "|", "^", "=", "+=", "-=", "*=", "/=", "%=",
                        "&=", "|=", "^=", "as"})
CONT_END = frozenset({"=", "+", "-", "*", "/", "%", "&", "|", "^", "&&", "||", "==", "!=", "<=", "<<", "->", "=>", ".", "::",
                      ":", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^="})


class FormatError(ValueError):
    """The file does not lex (an unterminated string or raw block): there is nothing safe to lay out."""


def format_text(text: str, indent: int = 4) -> str:
    """Canonical layout.

    - Indent = `indent` spaces per open bracket (`{`, `(`, `[`, `#[`) at the start of the line; a line that starts with a
      closer is one level out; a line that continues an expression (after a trailing operator, or starting with `.`
      or `&&`) is one level further in.
    - Comment-only lines are indented like the code that follows them (like the code before a closing `}`).
    - No trailing whitespace or `\\r`, no tabs in indentation, at most one blank line in a row, none at the start,
      exactly one newline at the end.
    """
    text = text.replace("\r\n", "\n")
    tokens, diags = lx.lex(text)
    if diags:
        d = diags[0]
        raise FormatError(f"{d.range.start.line + 1}:{d.range.start.col + 1}: {d.code}: {d.message}")
    lines = text.split("\n")
    raw: Set[int] = set()  # lines inside (or closing) a raw block: kept byte for byte
    skip_brace: Set[int] = set()  # token indexes of the `{`/`}` around a raw block: they do not indent anything
    for i, t in enumerate(tokens):
        if t.kind == lx.VERBATIM:
            raw.update(range(t.range.start.line, t.range.end.line))
            skip_brace.update((i - 1, i + 1))
            if t.range.end.line < len(lines):
                raw.add(t.range.end.line)  # the closing `}` of a raw block must stay in column 0

    level_of: Dict[int, int] = {}  # line -> level of its first token
    inner_of: Dict[int, int] = {}  # line -> open brackets when its first token starts (a closer counts as inside)
    first: Dict[int, int] = {}  # line -> index of its first token
    depth = 0
    prev: Optional[lx.Token] = None
    for i, t in enumerate(tokens):
        if t.kind == lx.EOF:
            break
        ln = t.range.start.line
        closer = t.kind == lx.PUNCT and t.text in CLOSE and i not in skip_brace
        if ln not in first and ln not in raw:
            first[ln] = i
            inner_of[ln] = depth
            lvl = depth - 1 if closer else depth
            if prev is not None and not closer and t.kind != lx.VERBATIM and _continues(prev, t):
                lvl += 1
            level_of[ln] = max(0, lvl)
        if t.kind == lx.PUNCT and i not in skip_brace:
            if t.text in OPEN:
                depth += 1
            elif t.text in CLOSE:
                depth = max(0, depth - 1)
        prev = t

    # Comment-only lines take the level of the next line that has code.
    next_code = [0] * (len(lines) + 1)
    nxt: Optional[int] = None
    for ln in range(len(lines) - 1, -1, -1):
        if ln in first:
            nxt = ln
        next_code[ln] = -1 if nxt is None else nxt

    out: List[str] = []
    for ln, line in enumerate(lines):
        if ln in raw:
            out.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
        elif ln in level_of:
            out.append(" " * (indent * level_of[ln]) + stripped)
        else:  # a comment line
            n = next_code[ln]
            if n < 0:
                lvl = 0
            else:
                t = tokens[first[n]]
                closes = t.kind == lx.PUNCT and t.text in CLOSE and first[n] not in skip_brace
                lvl = inner_of[n] if closes else level_of[n]
            out.append(" " * (indent * lvl) + stripped)
    while out and out[-1] == "":
        out.pop()
    result = "\n".join(out) + "\n" if out else ""
    if [(t.kind, t.text) for t in lx.lex(result)[0]] != [(t.kind, t.text) for t in tokens]:
        raise FormatError("internal error: formatting would change the tokens; nothing was changed")
    return result


def _continues(prev: lx.Token, t: lx.Token) -> bool:
    if prev.kind == lx.PUNCT and prev.text in CONT_END:
        return True
    return t.text in CONT_START and t.kind in (lx.PUNCT, lx.IDENT)
