# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack estimate in the tools (docs/kif-stack.md, stage 5): diagnostics, hover, inlay hint, quick fix, `.kifm` fields."""
import pytest

from kiflang import modfile, passes, quickfix, symbols
from tests.test_lsp import Client, c, find  # noqa: F401

SRC = ("fn leaf() -> u32 { let a: [u8; 600] = [0; 600]; return a[1] as u32; }\n"
       "#[small_stack(300)]\nfn top() -> u32 { return leaf(); }\n"
       "fn plain() -> u32 { return leaf(); }\n")


def codes(text):
    mod = symbols.analyze("/t/m.kif", text, None)
    return [d.code for d in passes.check_module(mod.tree)]


def test_lint_and_editor_diagnostics_include_budgets():
    assert codes(SRC) == ["KIFW010"]
    assert codes(SRC.replace("#[small_stack(300)]\n", "")) == []


def test_open_publishes_the_budget_warning(c):
    _, diags = c.open("s.kif", SRC)
    assert [d["code"] for d in diags] == ["KIFW010"]


def test_hover_shows_frame_chain_and_budget(c):
    uri, _ = c.open("s.kif", SRC)
    line, col = find(SRC, "fn top")
    h = c.call("textDocument/hover", c.at(uri, line, col + 3))["contents"]["value"]
    assert "**Stack:** frame" in h and "top → leaf" in h and "budget 300 bytes" in h
    line, col = find(SRC, "fn plain")
    assert "budget" not in c.call("textDocument/hover", c.at(uri, line, col + 3))["contents"]["value"]


def test_inlay_hint_only_for_budgeted_functions(c):
    uri, _ = c.open("s.kif", SRC)
    hints = c.call("textDocument/inlayHint", {"textDocument": {"uri": uri}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 9, "character": 0}}})
    stack = [h for h in hints if str(h["label"]).startswith(("stack", "⚠ stack"))]
    assert len(stack) == 1 and stack[0]["label"].startswith("⚠ stack ") and stack[0]["label"].endswith("/300") and stack[0]["position"]["line"] == 2


def test_quick_fixes(c):
    uri, _ = c.open("s.kif", SRC)
    acts = c.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": {"start": {"line": 2, "character": 3}, "end": {"line": 2, "character": 3}}, "context": {"diagnostics": []}})
    titles = [a["title"] for a in acts if a["kind"] == "quickfix"]
    assert any(t.startswith("Raise this function's stack budget to") for t in titles) and any("off" in t for t in titles)
    mod, diags = quickfix.check("/t/m.kif", SRC)
    d = next(d for d in diags if d.code == "KIFW010")
    raise_fix = next(f for f in quickfix.fixes_for(mod, d, diags) if f.title.startswith("Raise"))
    fixed = quickfix.apply_edits(SRC, raise_fix.edits)
    assert codes(fixed) == [] and "small_stack(1280)" in fixed


def test_quick_fix_inserts_off_without_an_own_attribute():
    text = "#![small_stack(100)]\n" + SRC.replace("#[small_stack(300)]\n", "")
    mod, diags = quickfix.check("/t/m.kif", text)
    d = next(d for d in diags if d.code == "KIFW010" and d.message.startswith("top"))
    off = next(f for f in quickfix.fixes_for(mod, d, diags) if "off" in f.title)
    fixed = quickfix.apply_edits(text, off.edits)
    assert "#[small_stack(off)]\nfn top" in fixed
    assert not any(x.startswith("top") for x in [dd.message for dd in quickfix.check("/t/m.kif", fixed)[1] if dd.code == "KIFW010"])


def test_kifm_carries_the_estimate():
    mod = symbols.analyze("/v/m.kif", "#[export]\nfn leaf() -> u32 { let a: [u8; 600] = [0; 600]; return a[1] as u32; }\n#[export]\nfn top() -> u32 { return leaf(); }\n", None)
    fns = modfile.decode(modfile.export(mod))["security"]["functions"]
    assert fns["leaf"]["stack"] >= 600
    assert fns["top"]["stack_depth"] == fns["top"]["stack"] + fns["leaf"]["stack_depth"] and fns["top"]["stack_exact"] is True
