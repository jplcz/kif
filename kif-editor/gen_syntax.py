#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause
"""Generates the word lists of every kif highlighter from the compiler (the single source of truth).

    python3 gen_syntax.py           rewrite the editor files in place
    python3 gen_syntax.py --check   exit 1 if any file is out of date (tests/test_editor_syntax.py runs this)

Sources: `kiflang.lexer` (KEYWORDS, CONTEXTUAL_WORDS, VERBATIM_WORDS), `kiflang.symbols.SCALARS` (builtin types) and
`kiflang.lsp.ATTRIBUTES` (attribute names). Two kinds of output:

* `vscode/syntaxes/kif.tmLanguage.json` is generated whole (it is also the grammar JetBrains IDEs use);
* every other file keeps its hand-written rules and has regions between `GEN-BEGIN <name>` and `GEN-END <name>`
  lines (in that file's comment syntax) that this script owns. Edit the rules outside the markers freely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from typing import Callable, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from kiflang import lexer as lx  # noqa: E402
from kiflang.lsp import ATTRIBUTES  # noqa: E402
from kiflang.symbols import SCALARS  # noqa: E402

CONSTANTS = ["true", "false"]
SELF_WORDS = ["self", "Self"]
TYPES = "void bool char i8 i16 i32 i64 u8 u16 u32 u64 usize isize".split()
assert set(TYPES) == set(SCALARS), "TYPES is out of date with kiflang.symbols.SCALARS"
# Words that are keywords wherever they appear, and the ones the parser only recognises in one position.
KEYWORDS = sorted(set(lx.KEYWORDS) - set(CONSTANTS) - set(SELF_WORDS))
CONTEXTUAL = sorted(lx.CONTEXTUAL_WORDS)
VERBATIM = list(lx.VERBATIM_WORDS)
ATTRS = list(ATTRIBUTES)
ALL_KEYWORDS = sorted(set(KEYWORDS) | set(CONTEXTUAL))


def alt(words: List[str]) -> str:
    return "|".join(words)


def wrap(prefix: str, words: List[str], width: int = 110) -> str:
    return "\n".join(textwrap.wrap(" ".join(words), width=width, initial_indent=prefix, subsequent_indent=prefix))


def strings(words: List[str], width: int = 100) -> str:
    return "\n ".join(textwrap.wrap(" ".join(f'"{w}"' for w in words), width))


# ---------------------------------------------------------------- marker regions


def region(text: str, name: str, comment: str, body: str, path: str) -> str:
    """Replace the lines between `<comment> GEN-BEGIN <name>` and `<comment> GEN-END <name>`."""
    pat = re.compile(
        r"(^[ \t]*" + re.escape(comment) + r" GEN-BEGIN " + re.escape(name) + r"[^\n]*\n)(.*?)(^[ \t]*"
        + re.escape(comment) + r" GEN-END " + re.escape(name) + r")",
        re.S | re.M,
    )
    if not pat.search(text):
        raise SystemExit(f"gen_syntax: {path}: no GEN-BEGIN {name} region")
    return pat.sub(lambda m: m.group(1) + body.rstrip("\n") + "\n" + m.group(3), text, count=1)


def vim(text: str) -> str:
    words = "\n".join(
        [
            wrap("syn keyword kifKeyword      ", ALL_KEYWORDS),
            wrap("syn keyword kifConstant     ", CONSTANTS),
            wrap("syn keyword kifSelf         ", SELF_WORDS),
            wrap("syn keyword kifType         ", TYPES),
        ]
    )
    text = region(text, "words", '"', words, "vim")
    return region(text, "verbatim", '"', f"let s:verbatim = '{alt(VERBATIM)}'", "vim")


def emacs(text: str) -> str:
    def lisp(name: str, words: List[str]) -> str:
        return f"(defconst {name}\n  '(" + "\n    ".join(textwrap.wrap(" ".join(f'"{w}"' for w in words), 100)) + "))"

    body = "\n\n".join(
        [
            lisp("kif--keywords", ALL_KEYWORDS),
            lisp("kif--constants", CONSTANTS),
            lisp("kif--self", SELF_WORDS),
            lisp("kif--types", TYPES),
            lisp("kif--verbatim", VERBATIM),
        ]
    )
    return region(text, "words", ";;", body, "emacs")


def sublime(text: str) -> str:
    def rule(words: List[str], scope: str) -> str:
        return f"    - match: '\\b({alt(words)})\\b'\n      scope: {scope}"

    body = "\n".join(
        [
            rule(CONSTANTS, "constant.language.kif"),
            rule(SELF_WORDS, "variable.language.kif"),
            rule(ALL_KEYWORDS, "keyword.other.kif"),
            rule(TYPES, "support.type.builtin.kif"),
        ]
    )
    text = region(text, "words", "#", body, "sublime")
    return region(text, "verbatim", "#", f"  verbatim_words: '{alt(VERBATIM)}'", "sublime")


def highlights_scm(text: str) -> str:
    def any_of(capture: str, words: List[str]) -> str:
        return f'((identifier) @{capture} (#any-of? @{capture} {" ".join(f"{chr(34)}{w}{chr(34)}" for w in words)}))'

    body = "\n".join(
        [
            f"[{strings(KEYWORDS)}] @keyword",
            "",
            "; contextual words, types and constants are plain identifiers to the grammar",
            any_of("keyword", CONTEXTUAL),
            any_of("type.builtin", TYPES),
            any_of("constant.builtin", CONSTANTS),
            any_of("variable.builtin", SELF_WORDS),
        ]
    )
    return region(text, "words", ";", body, "highlights")


def grammar_js(text: str) -> str:
    kws = textwrap.wrap(", ".join(f"'{w}'" for w in KEYWORDS) + ",", 100, initial_indent="      ", subsequent_indent="      ")
    body = "\n".join(
        ["    keyword: _ => choice(", *kws, "    ),",
         "    verbatim_word: _ => choice(" + ", ".join(f"'{w}'" for w in VERBATIM) + "),"]
    )
    return region(text, "words", "//", body, "grammar")


# ---------------------------------------------------------------- the TextMate grammar (generated whole)

IDENT = "[A-Za-z_][A-Za-z0-9_]*"


def textmate() -> dict:
    def decl(keyword: str, name_scope: str, key_scope: str) -> dict:
        return {"match": rf"\b({keyword})\b\s+({IDENT})", "captures": {"1": {"name": key_scope}, "2": {"name": name_scope}}}

    def verbatim(tag: str, source: str, name: str) -> dict:
        word = alt(VERBATIM)
        caps = {"1": {"name": "keyword.control.verbatim.kif"}}
        if tag:
            start = rf"^\s*({word})\s*(\()\s*({tag})\s*(\))\s*(\{{)\s*$"
            caps.update({"2": {"name": "punctuation.section.parens.begin.kif"}, "3": {"name": "entity.name.tag.language.kif"},
                         "4": {"name": "punctuation.section.parens.end.kif"}, "5": {"name": "punctuation.section.block.begin.kif"}})
        else:
            start = rf"^\s*({word})\s*(\{{)\s*$"
            caps["2"] = {"name": "punctuation.section.block.begin.kif"}
        return {"begin": start, "beginCaptures": caps, "end": r"^(\})[ \t]*$",
                "endCaptures": {"1": {"name": "punctuation.section.block.end.kif"}},
                "contentName": f"meta.embedded.block.{name}", "patterns": [{"include": source}]}

    repo = {
        "comment": {"patterns": [
            {"name": "comment.line.documentation.kif", "match": r"///.*$"},
            {"name": "comment.line.double-slash.kif", "match": r"//.*$"},
        ]},
        # `header(cxx) {`, `code(c) {`, `m_prolog {`: raw text up to a `}` in column 1 (the lexer's rule).
        "verbatim": {"patterns": [verbatim("cxx", "source.cpp", "cpp"), verbatim("c", "source.c", "c"), verbatim("", "source.c", "c")]},
        "include-path": {"match": r"\b(include)\b\s*(<[^>\n]*>)", "captures": {
            "1": {"name": "keyword.control.import.kif"}, "2": {"name": "string.quoted.other.lt-gt.include.kif"}}},
        "attribute": {"begin": r"(#!?\[)", "end": r"(\])",
                      "beginCaptures": {"1": {"name": "punctuation.definition.attribute.kif"}},
                      "endCaptures": {"1": {"name": "punctuation.definition.attribute.kif"}},
                      "name": "meta.attribute.kif",
                      "patterns": [
                          {"include": "#string"}, {"include": "#number"}, {"include": "#constant"},
                          {"match": rf"\b({alt(ATTRS)})\b(?=\s*[(,\]=]|\s*$)", "name": "entity.other.attribute-name.kif"},
                          {"match": rf"\b{IDENT}\b(?=\s*[(,\]=])", "name": "entity.other.attribute-name.unknown.kif"},
                          {"include": "#type"}]},
        "string": {"begin": '"', "end": '"', "name": "string.quoted.double.kif", "patterns": [
            {"name": "constant.character.escape.kif", "match": r"\\(?:[ntr0\\\"]|x[0-9a-fA-F]{2})"},
            {"name": "invalid.illegal.escape.kif", "match": r"\\."}]},
        "number": {"name": "constant.numeric.kif",
                   "match": r"\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)(?:[iu](?:8|16|32|64|size))?\b"},
        "lifetime": {"name": "storage.modifier.lifetime.kif", "match": rf"'{IDENT}\b"},
        "constant": {"name": "constant.language.kif", "match": rf"\b({alt(CONSTANTS)})\b"},
        "self": {"name": "variable.language.self.kif", "match": rf"\b({alt(SELF_WORDS)})\b"},
        "declarations": {"patterns": [
            decl("interface", "entity.name.type.interface.kif", "keyword.declaration.interface.kif"),
            decl("struct", "entity.name.type.struct.kif", "keyword.declaration.struct.kif"),
            decl("enum", "entity.name.type.enum.kif", "keyword.declaration.enum.kif"),
            decl("type", "entity.name.type.alias.kif", "keyword.declaration.type.kif"),
            decl("method", "entity.name.function.method.kif", "keyword.declaration.method.kif"),
            decl("fn|macro", "entity.name.function.kif", "keyword.declaration.function.kif"),
            decl("const", "entity.name.constant.kif", "keyword.declaration.const.kif"),
            decl(r"foreign\s+class", "entity.name.type.class.kif", "keyword.declaration.foreign.kif"),
            {"match": rf"\b(impl)\b\s+(?:({IDENT})\s+(for)\s+)?({IDENT})", "captures": {
                "1": {"name": "keyword.declaration.impl.kif"}, "2": {"name": "entity.name.type.interface.kif"},
                "3": {"name": "keyword.other.kif"}, "4": {"name": "entity.name.type.kif"}}},
            {"match": rf"\b(namespace)\b\s+({IDENT}(?:::{IDENT})*)", "captures": {
                "1": {"name": "keyword.declaration.namespace.kif"}, "2": {"name": "entity.name.namespace.kif"}}},
        ]},
        "keyword": {"match": rf"\b({alt(ALL_KEYWORDS)})\b", "name": "keyword.other.kif"},
        "type": {"match": rf"\b({alt(TYPES)})\b", "name": "support.type.builtin.kif"},
        "macro-call": {"match": rf"\b({IDENT})!(?=\s*[(\[{{])", "name": "support.function.macro.kif"},
        "parameter": {"match": rf"\b({IDENT})\s*(:)(?!:)", "captures": {
            "1": {"name": "variable.parameter.kif"}, "2": {"name": "punctuation.separator.colon.kif"}}},
        "operator": {"patterns": [
            {"match": r"->|=>", "name": "keyword.operator.arrow.kif"},
            {"match": r"::", "name": "punctuation.separator.namespace.kif"},
            {"match": r"\.\.=?|\.\.\.", "name": "keyword.operator.range.kif"},
            {"match": r"<<=?|>>=?|&&|\|\||[-+*/%&|^]=|[=!<>]=|[-+*/%&|^!<>=?]", "name": "keyword.operator.kif"}]},
    }
    order = ["comment", "verbatim", "include-path", "attribute", "string", "number", "lifetime", "declarations", "macro-call",
             "constant", "self", "keyword", "type", "parameter", "operator"]
    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "comment": "Generated by kif-editor/gen_syntax.py from the compiler's word lists; do not edit.",
        "name": "kif",
        "scopeName": "source.kif",
        "patterns": [{"include": "#" + k} for k in order],
        "repository": repo,
    }


def vscode(_text: str) -> str:
    return json.dumps(textmate(), indent=2, ensure_ascii=False) + "\n"


TARGETS: Dict[str, Callable[[str], str]] = {
    "vim/syntax/kif.vim": vim,
    "emacs/kif-mode.el": emacs,
    "vscode/syntaxes/kif.tmLanguage.json": vscode,
    "sublime/kif.sublime-syntax": sublime,
    "tree-sitter/queries/highlights.scm": highlights_scm,
    "tree-sitter/grammar.js": grammar_js,
}


def run(check: bool) -> List[str]:
    """Returns the files that differ from what the generator produces (and rewrites them unless `check`)."""
    out = []
    for rel, fn in TARGETS.items():
        path = os.path.join(HERE, rel)
        old = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                old = fh.read()
        new = fn(old)
        if new != old:
            out.append(rel)
            if not check:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the kif highlighters' word lists from the compiler")
    ap.add_argument("--check", action="store_true", help="fail if a generated file is out of date")
    args = ap.parse_args()
    changed = run(args.check)
    for rel in changed:
        if args.check:
            print(f"gen_syntax: {rel} is out of date; run kif-editor/gen_syntax.py", file=sys.stderr)
        else:
            print(f"updated {rel}")
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
