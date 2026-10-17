# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""How the command line tools print diagnostics (`--format`, docs/kif-tools.md, "Output formats").

    text    `path:line:col: error KIF001: message`, one per line (the default; what the VS Code problem matcher reads)
    json    one JSON array of objects {file, line, column, endLine, endColumn, severity, code, message}, 1-based
    sarif   a SARIF 2.1.0 log (GitHub code scanning, many CI viewers)
    github  GitHub Actions workflow commands (`::error file=..,line=..,col=..,title=CODE::message`), shown as annotations

`text` and `github` print as findings arrive; `json` and `sarif` are one document, so they print at `finish()`.
"""

from __future__ import annotations

import json
import sys
from typing import IO, List, Optional, Tuple

from .diagnostics import ERROR, Diagnostic

FORMATS = ("text", "json", "sarif", "github")


def _escape(s: str, prop: bool = False) -> str:
    """Workflow commands: `%`, CR and LF in the message (and `:` and `,` in properties) must be escaped."""
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


class Reporter:
    """Collects or prints the diagnostics of a run. `stream` is where they go (standard error for compilers, standard output
    for linters, which is where their text output always went)."""

    def __init__(self, fmt: str = "text", stream: Optional[IO[str]] = None, tool: str = "kif") -> None:
        if fmt not in FORMATS:
            raise ValueError(f"unknown output format {fmt!r} (one of {', '.join(FORMATS)})")
        self.fmt = fmt
        self.stream = stream or sys.stderr
        self.tool = tool
        self.items: List[Tuple[str, Diagnostic]] = []

    def add(self, path: str, d: Diagnostic) -> None:
        self.items.append((path, d))
        if self.fmt == "text":
            print(d.format(path), file=self.stream)
        elif self.fmt == "github":
            kind = "error" if d.severity == ERROR else "warning"
            props = f"file={_escape(path, True)},line={d.range.start.line + 1},col={d.range.start.col + 1}," \
                    f"endLine={d.range.end.line + 1},endColumn={d.range.end.col + 1},title={_escape(d.code, True)}"
            print(f"::{kind} {props}::{_escape(d.message)}", file=self.stream)

    def failed(self, werror: bool = False) -> bool:
        return any(d.severity == ERROR or werror for _p, d in self.items)

    def finish(self) -> None:
        if self.fmt == "json":
            print(json.dumps([_record(p, d) for p, d in self.items], indent=2), file=self.stream)
        elif self.fmt == "sarif":
            print(json.dumps(self.sarif(), indent=2), file=self.stream)

    def sarif(self) -> dict:
        codes = sorted({d.code for _p, d in self.items})
        results = []
        for path, d in self.items:
            r = _record(path, d)
            results.append({
                "ruleId": d.code,
                "level": "error" if d.severity == ERROR else "warning",
                "message": {"text": d.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": path},
                    "region": {"startLine": r["line"], "startColumn": r["column"], "endLine": r["endLine"], "endColumn": r["endColumn"]},
                }}],
            })
        return {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": self.tool, "rules": [{"id": c} for c in codes]}}, "results": results}],
        }


def _record(path: str, d: Diagnostic) -> dict:
    return {"file": path, "line": d.range.start.line + 1, "column": d.range.start.col + 1, "endLine": d.range.end.line + 1,
            "endColumn": d.range.end.col + 1, "severity": "error" if d.severity == ERROR else "warning", "code": d.code,
            "message": d.message}


def add_format_option(parser) -> None:
    parser.add_argument("--format", dest="out_format", choices=FORMATS, default="text",
                        help="how diagnostics are printed: text (default), json, sarif (SARIF 2.1.0) or github (Actions annotations)")
