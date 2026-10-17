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
