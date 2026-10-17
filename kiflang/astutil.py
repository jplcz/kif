# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Helpers shared by the tools built on the syntax tree (lint, doc, graph, quickfix; docs/kif-tools.md)."""

from __future__ import annotations

import dataclasses
from typing import Iterator, List, Optional

from . import ast
from . import lexer as lx
from .source import Pos, Range


def walk(node) -> Iterator[ast.Node]:
    """Every node below `node` (and `node` itself), parents before children, in source order of the fields."""
    if isinstance(node, list):
        for x in node:
            yield from walk(x)
    elif isinstance(node, ast.Node):
        yield node
        for f in dataclasses.fields(node):
            yield from walk(getattr(node, f.name))


def find_attr(attrs: List[ast.Attribute], name: str) -> Optional[ast.Attribute]:
    return next((a for a in attrs if a.name.text == name), None)


def has_attr(attrs: List[ast.Attribute], name: str) -> bool:
    return find_attr(attrs, name) is not None


def attr_name_arg(attrs: List[ast.Attribute], name: str) -> Optional[str]:
    """The first argument of `#[name(arg)]` when it is a bare name (`extends(base)`), else None."""
    a = find_attr(attrs, name)
    return a.args[0].name if a is not None and a.args and a.args[0].name else None


def slice_text(lines: List[str], r: Range) -> str:
    """The source text covered by `r` (lines are `text.split("\\n")`)."""
    if r.start.line == r.end.line:
        return lines[r.start.line][r.start.col : r.end.col]
    parts = [lines[r.start.line][r.start.col :]] + lines[r.start.line + 1 : r.end.line] + [lines[r.end.line][: r.end.col]]
    return "\n".join(parts)


def decl_line(lines: List[str], node: ast.Node) -> int:
    """The first line of a declaration including the `#[..]` attribute lines written above it."""
    line = node.range.start.line
    while line > 0 and lines[line - 1].lstrip().startswith(("#[", "#![")):
        line -= 1
    return line


def doc_above(lines: List[str], node: ast.Node) -> str:
    """The doc comment of a declaration: the run of `//` lines directly above it (or above its attributes).

    The text after `//` (and one optional space) is Markdown. A blank line detaches the comment, so it is not a doc.
    """
    out: List[str] = []
    i = decl_line(lines, node) - 1
    while i >= 0 and (text := lx.comment_text(lines[i])) is not None:
        out.append(text)
        i -= 1
    return "\n".join(reversed(out)).rstrip()


def offset_of(lines: List[str], pos: Pos) -> int:
    """Character offset of `pos` in `"\\n".join(lines)`."""
    return sum(len(x) + 1 for x in lines[: pos.line]) + pos.col
