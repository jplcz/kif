# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Lexer (docs/kif-proc-language.md section 3). Comments are `// ...`; `#[` and `#![` open attributes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from . import diagnostics as dg
from .source import Pos, Range

IDENT = "ident"
INT = "int"
STRING = "string"
LIFETIME = "lifetime"
SYSPATH = "syspath"
VERBATIM = "verbatim"
PUNCT = "punct"
EOF = "eof"

KEYWORDS = frozenset(
    "fn gen let mut if else while loop for in match break continue return as unsafe extern macro type cfg export "
    "impl private Self const true false struct enum use".split()
)

VERBATIM_WORDS = ("header", "code", "m_prolog", "m_epilog")

# Words the parser treats as keywords only in one position (so they stay usable as names elsewhere); the editor
# highlighters (kif-editor/gen_syntax.py) colour them as keywords.
CONTEXTUAL_WORDS = (
    "interface method namespace import include foreign class override static before after default".split()
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)(?:[iu](?:8|16|32|64|size))?"
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_LIFETIME_RE = re.compile(r"'[A-Za-z_][A-Za-z0-9_]*")
_STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_SYSPATH_RE = re.compile(r"[ \t]*(<[^>\n]*>)")
_VERBATIM_OPEN_RE = re.compile(r"[ \t]*\{[ \t]*\r?$")
_VERBATIM_TAG_RE = re.compile(r"[ \t]*\([ \t]*([A-Za-z_][A-Za-z0-9_+]*)[ \t]*\)(?=[ \t]*\{[ \t]*\r?$)")
_VERBATIM_END_RE = re.compile(r"\}[ \t]*\r?$")

# `>>`, `>=` and `>>=` are not lexed: the parser joins adjacent `>`/`=` tokens, so `Vec<Vec<T>>` needs no splitting.
_PUNCT3 = ("<<=", "..=", "...")
_PUNCT2 = ("->", "=>", "::", "..", "==", "!=", "<=", "&&", "||", "<<", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=")
_PUNCT1 = "{}()[]<>,:;*=-+/%&|^!?.@"

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"'}


def comment_text(line: str) -> "str | None":
    """The text of a `// ...` comment line (a `///` works the same), or None when the line is not a comment line."""
    stripped = line.lstrip()
    if not stripped.startswith("//"):
        return None
    text = stripped[2:]
    if text.startswith("/"):
        text = text[1:]
    return text[1:] if text.startswith(" ") else text


def unescape(literal: str) -> "str | None":
    """Value of a string token (with quotes), or None when it holds an unknown or malformed escape."""
    body = literal[1:-1]
    out: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if nxt == "x":
            digits = body[i + 2 : i + 4]
            if len(digits) != 2 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                return None
            out.append(chr(int(digits, 16)))
            i += 4
        elif nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
        else:
            return None
    return "".join(out)


def parse_int(text: str) -> Tuple[int, str]:
    """Split an INT token into (value, suffix); the suffix is "" when absent."""
    m = re.fullmatch(r"(.*?)((?:[iu](?:8|16|32|64|size))?)", text)
    return int(m.group(1).replace("_", ""), 0), m.group(2)


@dataclass
class Token:
    kind: str
    text: str
    range: Range


def lex(text: str) -> Tuple[List[Token], List[dg.Diagnostic]]:
    """Tokenise a .kif source. Never raises: problems become diagnostics and lexing continues."""
    tokens: List[Token] = []
    diags: List[dg.Diagnostic] = []
    lines = text.split("\n")
    line_no = 0
    while line_no < len(lines):
        line = lines[line_no]
        col = 0
        jumped = False
        while col < len(line):
            ch = line[col]
            if ch in " \t\r":
                col += 1
                continue
            if line.startswith("//", col):
                break
            start = Pos(line_no, col)
            if ch == "#":
                opener = "#![" if line.startswith("#![", col) else "#[" if line.startswith("#[", col) else ""
                if not opener:
                    diags.append(dg.error(Range(start, Pos(line_no, col + 1)), "KIF005",
                                          "comments start with '//' ('#' only opens an attribute, '#[' or '#![')"))
                    break
                col += len(opener)
                tokens.append(Token(PUNCT, opener, Range(start, Pos(line_no, col))))
                continue
            if ch.isascii() and ch.isdigit():
                m = _INT_RE.match(line, col)
                col = m.end()
                if col < len(line) and (line[col].isalnum() or line[col] == "_"):
                    # "9abc", "10u7": swallow the whole word so that one diagnostic covers it.
                    col = _WORD_RE.match(line, start.col).end()
                    diags.append(dg.error(Range(start, Pos(line_no, col)), "KIF006",
                                          f"invalid number literal {line[start.col:col]!r}"))
                    continue
                tokens.append(Token(INT, m.group(0), Range(start, Pos(line_no, col))))
                continue
            if ch.isascii() and (ch.isalpha() or ch == "_"):
                m = _IDENT_RE.match(line, col)
                word = m.group(0)
                col = m.end()
                tokens.append(Token(IDENT, word, Range(start, Pos(line_no, col))))
                if word == "include":
                    sm = _SYSPATH_RE.match(line, col)
                    if sm:
                        tokens.append(
                            Token(SYSPATH, sm.group(1), Range(Pos(line_no, sm.start(1)), Pos(line_no, sm.end(1))))
                        )
                        col = sm.end(1)
                elif word in VERBATIM_WORDS and (tag := _VERBATIM_TAG_RE.match(line, col)):
                    # header(c) { / code(cxx) {: a language-tagged raw block
                    po, pc = line.index("(", col), line.index(")", col)
                    tokens.append(Token(PUNCT, "(", Range(Pos(line_no, po), Pos(line_no, po + 1))))
                    tokens.append(Token(IDENT, tag.group(1), Range(Pos(line_no, tag.start(1)), Pos(line_no, tag.end(1)))))
                    tokens.append(Token(PUNCT, ")", Range(Pos(line_no, pc), Pos(line_no, pc + 1))))
                    brace = line.index("{", pc)
                    tokens.append(Token(PUNCT, "{", Range(Pos(line_no, brace), Pos(line_no, brace + 1))))
                    line_no = _verbatim(lines, line_no, brace, tokens, diags)
                    jumped = True
                    break
                elif word in VERBATIM_WORDS and _VERBATIM_OPEN_RE.match(line, col):
                    brace = line.index("{", col)
                    tokens.append(Token(PUNCT, "{", Range(Pos(line_no, brace), Pos(line_no, brace + 1))))
                    line_no = _verbatim(lines, line_no, brace, tokens, diags)
                    jumped = True
                    break
                continue
            if ch == '"':
                m = _STRING_RE.match(line, col)
                if not m:
                    diags.append(
                        dg.error(Range(start, Pos(line_no, len(line))), "KIF001", "unterminated string literal")
                    )
                    col = len(line)
                    continue
                col = m.end()
                if unescape(m.group(0)) is None:
                    diags.append(dg.error(Range(start, Pos(line_no, col)), "KIF007",
                                          "invalid escape in string literal (valid: \\n \\t \\r \\0 \\\\ \\\" \\xNN)"))
                tokens.append(Token(STRING, m.group(0), Range(start, Pos(line_no, col))))
                continue
            if ch == "'":
                m = _LIFETIME_RE.match(line, col)
                if m:
                    col = m.end()
                    tokens.append(Token(LIFETIME, m.group(0), Range(start, Pos(line_no, col))))
                    continue
            width = 3 if line.startswith(_PUNCT3, col) else 2 if line.startswith(_PUNCT2, col) else 1
            if width > 1 or ch in _PUNCT1:
                tokens.append(Token(PUNCT, line[col : col + width], Range(start, Pos(line_no, col + width))))
                col += width
                continue
            diags.append(dg.error(Range(start, Pos(line_no, col + 1)), "KIF004", f"invalid character {ch!r}"))
            col += 1
        if not jumped:
            line_no += 1
    end = Pos(len(lines) - 1, len(lines[-1]))
    tokens.append(Token(EOF, "", Range(end, end)))
    return tokens, diags


def _verbatim(lines: List[str], open_line: int, brace_col: int, tokens: List[Token], diags: List[dg.Diagnostic]) -> int:
    """Consume a verbatim body that starts on the line after open_line. Returns the next line index to lex."""
    body_start = Pos(open_line + 1, 0)
    for i in range(open_line + 1, len(lines)):
        if _VERBATIM_END_RE.match(lines[i]):
            body = "\n".join(lines[open_line + 1 : i])
            tokens.append(Token(VERBATIM, body, Range(body_start, Pos(i, 0))))
            tokens.append(Token(PUNCT, "}", Range(Pos(i, 0), Pos(i, 1))))
            return i + 1
    diags.append(
        dg.error(
            Range(Pos(open_line, brace_col), Pos(open_line, brace_col + 1)),
            "KIF002",
            "unterminated verbatim block (no line consisting only of '}' in column 1)",
        )
    )
    last = len(lines) - 1
    tokens.append(
        Token(VERBATIM, "\n".join(lines[open_line + 1 :]), Range(body_start, Pos(last, len(lines[last]))))
    )
    return len(lines)
