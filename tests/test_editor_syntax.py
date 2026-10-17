# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Editor support: the generated highlighter word lists, the samples and the snippets must track the compiler."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiflang import fmt, lexer as lx, parser, symbols
from kiflang import lsp
from kiflang.diagnostics import ERROR

ROOT = Path(__file__).resolve().parent.parent
EDITOR = ROOT / "kif-editor"
SAMPLES = EDITOR / "samples"


def test_generated_files_are_up_to_date():
    r = subprocess.run([sys.executable, str(EDITOR / "gen_syntax.py"), "--check"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_gen_region_is_closed():
    for path in EDITOR.rglob("*"):
        if path.is_file() and "node_modules" not in path.parts and path.suffix in {".vim", ".el", ".scm", ".js", ".sublime-syntax"}:
            text = path.read_text()
            begins = re.findall(r"GEN-BEGIN (\w+)", text)
            ends = re.findall(r"GEN-END (\w+)", text)
            assert begins == ends, path


def test_tmlanguage_is_valid_and_its_regexes_compile():
    doc = json.loads((EDITOR / "vscode/syntaxes/kif.tmLanguage.json").read_text())
    assert doc["scopeName"] == "source.kif"
    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"match", "begin", "end"} and isinstance(value, str):
                    # \h and \G are Oniguruma-only; the rest is close enough to Python's dialect
                    re.compile(value.replace("\\h", "[0-9a-fA-F]").replace("\\G", ""))
                    count += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    assert count > 20


def test_generated_lists_cover_the_compiler():
    tm = (EDITOR / "vscode/syntaxes/kif.tmLanguage.json").read_text()
    vim = (EDITOR / "vim/syntax/kif.vim").read_text()
    for word in lx.KEYWORDS:
        assert re.search(rf"\b{word}\b", tm), word
        assert re.search(rf"\b{word}\b", vim) or word in {"true", "false", "Self"}, word
    for word in symbols.SCALARS:
        assert re.search(rf"\b{word}\b", tm), word
    for attr in lsp.ATTRIBUTES:
        name = attr if isinstance(attr, str) else attr[0]
        assert re.search(rf"\b{re.escape(name)}\b", tm), name


@pytest.mark.parametrize("name", ["example.kif", "device.kif"])
def test_samples_are_clean_and_formatted(name):
    text = (SAMPLES / name).read_text()
    file, diags = parser.parse(text)
    assert not [d for d in diags if d.severity == ERROR], diags
    assert fmt.format_text(text) == text


MEMBERS = {"method", "methoddef", "static", "before", "after", "m_prolog", "m_epilog"}
STATEMENTS = {"if", "while", "for", "match", "letm"}


def expand(body):
    text = "\n".join(body)
    text = re.sub(r"\$\{\d+:([^}]*)\}", lambda m: re.sub(r"\$\{\d+:([^}]*)\}", r"\1", m.group(1)), text)
    return re.sub(r"\$\{?\d+\}?", "", text)


def test_snippets_parse():
    snippets = json.loads((EDITOR / "vscode/snippets/kif.code-snippets").read_text())
    assert len(snippets) > 20
    for name, snip in snippets.items():
        text = expand(snip["body"] if isinstance(snip["body"], list) else [snip["body"]])
        prefix = snip["prefix"]
        if prefix in MEMBERS:
            text = f"interface i {{\n{text}\nmethod z();\n}}\n"
        elif prefix in STATEMENTS:
            text = f"fn f(n: u32) -> u32 {{\n{text}\n0 }}\n"
        _file, diags = parser.parse(text)
        assert not [d for d in diags if d.severity == ERROR], (name, text, diags)
