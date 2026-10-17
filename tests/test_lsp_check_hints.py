# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Inlay hints for run-time checks: what they show, and the grace period that keeps typing cheap."""

from kiflang.lsp import Server, path_to_uri

SRC = ("fn f(a: &[u32], i: usize) -> u32 { if i < a.len() { a[i] } else { a[0] } }\n"
       "fn g(a: &[u32]) -> u32 { a[1] }\n")
WHOLE = {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}}


class Rig:
    def __init__(self, checks="all", refresh=True):
        self.s = Server()
        self.t = 0.0
        self.s.now = lambda: self.t
        self.s.handle({"id": 1, "method": "initialize", "params": {
            "capabilities": {"workspace": {"inlayHint": {"refreshSupport": refresh}}},
            "initializationOptions": {"inlayHints": {"checks": checks, "delayMs": 400}}}})
        self.uri = path_to_uri("/tmp/kif_hint_test.kif")

    def open(self, text):
        self.s.handle({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": self.uri, "text": text}}})

    def change(self, text):
        self.s.handle({"method": "textDocument/didChange", "params": {"textDocument": {"uri": self.uri}, "contentChanges": [{"text": text}]}})

    def hints(self):
        out = self.s.handle({"id": 2, "method": "textDocument/inlayHint", "params": {"textDocument": {"uri": self.uri}, "range": WHOLE}})
        return [(h["position"]["line"], h["label"]) for h in out[0]["result"] if h["label"][0] in "✓⚠"]


def test_proved_and_kept_checks_are_marked():
    r = Rig("all")
    r.open(SRC)
    assert r.hints() == [(0, "✓ bounds"), (0, "⚠ bounds"), (1, "⚠ bounds")]


def test_kept_mode_shows_only_the_checks_that_stay_and_off_shows_none():
    r = Rig("kept")
    r.open(SRC)
    assert r.hints() == [(0, "⚠ bounds"), (1, "⚠ bounds")]
    o = Rig("off")
    o.open(SRC)
    assert o.hints() == []


def test_a_function_being_edited_waits_for_the_grace_period_and_the_others_keep_their_hints():
    r = Rig("all")
    r.open(SRC)
    r.hints()
    r.t = 1.0
    r.change("\n" + SRC.replace("a[1]", "a[2]"))  # a line inserted above, then `g` edited
    r.t = 1.1
    assert r.hints() == [(1, "✓ bounds"), (1, "⚠ bounds")]  # `f` moved down a line and kept its hints; `g` has none yet
    assert r.s.tick() == []  # not due yet
    assert abs(r.s.wait_time() - 0.3) < 1e-6
    r.t = 1.5
    (req,) = r.s.tick()
    assert req["method"] == "workspace/inlayHint/refresh" and "id" in req
    assert r.s.wait_time() is None and r.s.tick() == []
    assert r.hints() == [(1, "✓ bounds"), (1, "⚠ bounds"), (2, "⚠ bounds")]


def test_no_refresh_request_when_the_client_cannot_take_one():
    r = Rig("all", refresh=False)
    r.open(SRC)
    r.t = 1.0
    r.change(SRC + "\n")
    r.hints()
    r.t = 2.0
    assert r.s.tick() == []


def test_unchanged_functions_are_not_proved_again(monkeypatch):
    from kiflang import hints
    calls = []
    real = hints.function_hints
    monkeypatch.setattr(hints, "function_hints", lambda fn: calls.append(fn.name.text) or real(fn))
    r = Rig("all")
    r.open(SRC)
    r.hints()
    r.hints()
    assert calls == ["f", "g"]
    r.t = 5.0
    r.change(SRC.replace("a[1]", "a[2]"))
    r.t = 6.0
    r.hints()
    assert calls == ["f", "g", "g"]


def test_a_changed_declaration_invalidates_every_function():
    from kiflang import hints
    calls = []
    real = hints.function_hints
    r = Rig("all")
    src = "struct s { a: [u32; 4] }\nfn f(x: &s, i: usize) -> u32 { x.a[i] }\n"
    r.open(src)
    r.hints()
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(hints, "function_hints", lambda fn: calls.append(fn.name.text) or real(fn))
    r.t = 5.0
    r.change(src.replace("[u32; 4]", "[u32; 8]"))
    r.t = 6.0
    r.hints()
    mp.undo()
    assert calls == ["f"]


TAINT_SRC = ("#[untaint]\nfn s(#[tainted] x: usize) -> usize { x & 3 }\n"
             "#[tainted_result]\nfn get() -> usize { 1 }\n"
             "#[tainted_result]\nfn g(#[tainted] t: &[u32], c: usize) -> u32 {\n    spec::barrier();\n    let k = untaint!(\"masked to 0..3\", s(get()));\n    t[c] + t[k]\n}\n")


def taint_hints(r):
    out = r.s.handle({"id": 3, "method": "textDocument/inlayHint", "params": {"textDocument": {"uri": r.uri}, "range": WHOLE}})
    return [(h["position"]["line"], h["label"], h.get("tooltip", "")) for h in out[0]["result"] if h["label"][0] in "⛓🛈"]


def test_taint_hints_show_barriers_and_untaint_reasons():
    r = Rig("off")
    r.open(TAINT_SRC)
    got = taint_hints(r)
    assert (6, "⛓ barrier", "fence (explicit: spec::barrier())") in got
    assert any(l == 7 and lab == "🛈 untaint: masked to 0..3" for l, lab, _ in got)
    loads = [g for g in got if g[0] == 8]
    assert len(loads) == 2 and all("implicit: tainted element load" in tip for _, _, tip in loads)


def test_taint_hints_can_be_turned_off():
    r = Rig("off")
    r.s.set_hint_options({"taint": False})
    r.open(TAINT_SRC)
    assert taint_hints(r) == []


def test_hover_on_a_function_explains_its_taint_attributes():
    r = Rig("off")
    r.open(TAINT_SRC)
    for line, text, want in ((1, "fn s(", ["#[untaint]", "#[tainted]", "`x`"]), (3, "fn get", ["#[tainted_result]"])):
        col = (TAINT_SRC.split("\n")[line]).index(text) + 3
        out = r.s.handle({"id": 9, "method": "textDocument/hover", "params": {"textDocument": {"uri": r.uri}, "position": {"line": line, "character": col}}})
        value = out[0]["result"]["contents"]["value"]
        assert all(w in value for w in want), value


def test_code_lens_counts_the_callers_of_each_untaint_function():
    r = Rig("off")
    r.open(TAINT_SRC)
    out = r.s.handle({"id": 10, "method": "textDocument/codeLens", "params": {"textDocument": {"uri": r.uri}}})
    lenses = out[0]["result"]
    assert [l["command"]["title"] for l in lenses] == ["untaint: 1 caller"], lenses
    assert lenses[0]["range"]["start"]["line"] == 1
