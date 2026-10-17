# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The ABI manifest and the baseline lint rules KIFL020-029 and KIFL033 (docs/kif-abi.md)."""

import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout

import pytest

from kiflang import manifest as abi
from kiflang.kif import main
from kiflang.lint import RULES, Config, lint
from kiflang.lsp import Server, path_to_uri
from kiflang.symbols import analyze

ROOT = os.path.join(os.path.dirname(__file__), "..")


def mod_of(src, name="dev.kif"):
    return analyze("/t/" + name, src)


def release(src):
    mod = mod_of(src)
    assert not mod.diagnostics, mod.diagnostics
    return abi.loads(abi.dumps(abi.manifest_of(mod, "dev.kif")))


def found(old, new, cfg=None):
    return [d.code for d in lint(mod_of(new), cfg, release(old)) if d.code in RULES and d.code != "KIFL010"]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


BASE = """
#[kobj, self(device_t), version(1)]
interface dev {
    method read(off: u32) -> u32;
    method write(off: u32, value: u32) -> i32;
    method reset() -> i32 { 0 }
}
enum mode: u8 { off = 0, on = 1 }
struct conf { base: u64, mode: mode }
"""


def test_manifest_contents():
    m = release(BASE)
    d = m["interfaces"]["dev"]
    assert m["file"] == "dev.kif" and m["format"] == abi.FORMAT
    assert d["version"] == 1 and d["self"] == "device_t" and d["extends"] is None
    assert d["c_symbols"] == ["dev_read", "dev_write", "dev_reset"]
    assert d["ids"]["read"] == f"0x{abi.method_id('dev', 'read'):016x}"
    assert d["defaults"] == ["reset"]
    assert m["layouts"]["mode"] == "u8:off=0,on=1"
    assert m["layouts"]["conf"] == "base:u64,mode:mode"
    assert m["types"] == {"mode": "enum", "conf": "struct"}


def test_identical_source_has_no_findings():
    assert found(BASE, BASE) == []


def test_bodies_whitespace_and_docs_are_not_abi():
    new = BASE.replace("{ 0 }", "{ 1 + 1 - 2 }").replace("(off: u32)", "( off :  u32 )")
    new = "// docs added\n" + new.replace("interface dev", "// the device\ninterface dev")
    assert found(BASE, new) == []


def test_non_kobj_interfaces_are_not_in_the_manifest():
    assert release("interface plain { method f() -> i32; }")["interfaces"] == {}


def test_kifl020_interface_gone_or_no_longer_kobj():
    assert found(BASE, BASE.replace("#[kobj, self(device_t), version(1)]\ninterface dev", "interface dev")) == ["KIFL020"]
    only_types = "enum mode: u8 { off = 0, on = 1 }\nstruct conf { base: u64, mode: mode }"
    assert found(BASE, only_types) == ["KIFL020"]


def test_kifl021_method_removed():
    new = BASE.replace("    method write(off: u32, value: u32) -> i32;\n", "")
    assert found(BASE, new) == ["KIFL021"]
    assert found(BASE, new.replace("version(1)", "version(2)")) == []


def test_kifl022_signature_changed():
    for change in ("method read(off: u64) -> u32;", "method read(off: u32) -> u64;", "static method read(off: u32) -> u32;",
                   "#[might_sleep] method read(off: u32) -> u32;"):
        new = BASE.replace("method read(off: u32) -> u32;", change)
        assert found(BASE, new) == ["KIFL022"], change
        assert found(BASE, new.replace("version(1)", "version(2)")) == [], change


def test_kifl023_added_without_default():
    new = BASE.replace("    method reset", "    method extra() -> i32;\n    method reset")
    assert found(BASE, new) == ["KIFL023"]
    ok = BASE.replace("    method reset", "    method extra() -> i32 { 0 }\n    method reset")
    assert found(BASE, ok) == []


def test_kifl024_version_down():
    old = BASE.replace("version(1)", "version(3)")
    assert found(old, BASE) == ["KIFL024"]


def test_kifl025_off_by_default_and_opt_in():
    new = BASE.replace("    method reset", "    method extra() -> i32 { 0 }\n    method reset")
    assert found(BASE, new) == []
    assert found(BASE, new, Config(levels={"KIFL025": "warning"})) == ["KIFL025"]


def test_kifl026_extends_changed():
    base = "#[kobj, version(1)]\ninterface a { method f() -> i32; }\n#[kobj, version(1)]\ninterface b { method g() -> i32; }\n"
    old = base + "#[kobj, version(1), extends(a)]\ninterface c { method h() -> i32; }\n"
    assert found(old, old.replace("extends(a)", "extends(b)")) == ["KIFL026"]
    assert found(old, old.replace(", extends(a)", "")) == ["KIFL026"]


def test_kifl027_type_gone():
    assert found(BASE, BASE.replace("enum mode: u8 { off = 0, on = 1 }\n", "").replace("mode: mode", "mode: u8")) == ["KIFL027", "KIFL028"]
    assert found(BASE, BASE.replace("struct conf { base: u64, mode: mode }", "")) == ["KIFL027"]


def test_kifl028_layout_changes():
    assert found(BASE, BASE.replace("on = 1", "on = 1, auto = 2")) == []  # appending is compatible
    for new in (BASE.replace("on = 1", "on = 2"), BASE.replace("{ off = 0, on = 1 }", "{ on = 1, off = 0 }"),
                BASE.replace("mode: u8", "mode: u16"), BASE.replace("off = 0,", "none = 0,"),
                BASE.replace("{ off = 0, on = 1 }", "{ on = 1 }"),
                BASE.replace("base: u64,", "base: u32,"), BASE.replace("{ base: u64, mode: mode }", "{ base: u64, mode: mode, extra: u8 }"),
                BASE.replace("{ base: u64, mode: mode }", "{ mode: mode, base: u64 }")):
        assert found(BASE, new) == ["KIFL028"], new


def test_kifl028_implicit_values_follow_the_previous_one():
    old = "enum e: u8 { a, b, c }\n"
    assert found(old, "enum e: u8 { a, b, c, d }\n") == []
    assert found(old, "enum e: u8 { a, b = 5, c }\n") == ["KIFL028"]


def test_kifl029_default_removed():
    assert found(BASE, BASE.replace("method reset() -> i32 { 0 }", "method reset() -> i32;")) == ["KIFL029"]
    assert found(BASE, BASE.replace("method reset() -> i32 { 0 }", "method reset() -> i32;").replace("version(1)", "version(2)")) == []


def test_kifl033_self_changed():
    new = BASE.replace("self(device_t)", "self(other_t)")
    assert found(BASE, new) == ["KIFL033"]
    assert found(BASE, new.replace("version(1)", "version(2)")) == []


def test_bump_silences_method_rules_but_not_type_rules():
    new = BASE.replace("version(1)", "version(2)").replace("method write(off: u32, value: u32) -> i32;", "").replace("on = 1", "on = 7")
    assert found(BASE, new) == ["KIFL028"]


def test_levels_and_suppression_comments():
    new = BASE.replace("method write(off: u32, value: u32) -> i32;", "")
    assert found(BASE, new, Config(levels={"KIFL021": "off"})) == []
    assert found(BASE, "// kif-lint: disable-file=KIFL021\n" + new) == []


def test_no_baseline_no_baseline_rules():
    assert not [d for d in lint(mod_of(BASE)) if d.code in {f"KIFL02{n}" for n in range(10)}]


def test_loads_rejects_foreign_documents(tmp_path):
    with pytest.raises(ValueError, match="not a kif ABI manifest"):
        abi.loads("{}")
    with pytest.raises(ValueError, match="not JSON"):
        abi.loads("nope")
    with pytest.raises(ValueError, match="not supported"):
        abi.loads(json.dumps({"format": abi.FORMAT, "version": 1}))


def test_signature_hash_ignores_lifetimes_and_unrelated_attributes():
    a = "#[kobj, version(1)]\ninterface d { method f(p: &u8) -> i32; }\n"
    b = "#[kobj, version(1)]\ninterface d { #[trace] method f(p: &'static u8) -> i32; }\n"
    assert release(a)["interfaces"]["d"]["signatures"] == release(b)["interfaces"]["d"]["signatures"]


# ---------------------------------------------------------------- command line and editor


def test_cli_manifest_and_baseline(tmp_path):
    src = tmp_path / "dev.kif"
    src.write_text(BASE)
    status, out, _ = run("manifest", str(src))
    assert status == 0 and json.loads(out)["interfaces"]["dev"]["version"] == 1
    status, _, _ = run("manifest", str(src), "-o", str(tmp_path / "rel"))
    manifest = tmp_path / "rel" / "dev.json"
    assert status == 0 and manifest.exists()

    assert run("lint", "--baseline", str(manifest), str(src))[0] == 0
    src.write_text(BASE.replace("    method write(off: u32, value: u32) -> i32;\n", ""))
    status, out, _ = run("lint", "--baseline", str(manifest), str(src))
    assert status == 1 and "KIFL021" in out

    # a manifest for another file name is ignored
    other = tmp_path / "other.kif"
    other.write_text(BASE.replace("    method write(off: u32, value: u32) -> i32;\n", ""))
    assert "KIFL021" not in run("lint", "--baseline", str(manifest), str(other))[1]


def test_cli_manifest_errors(tmp_path):
    a, b = tmp_path / "a.kif", tmp_path / "b.kif"
    a.write_text(BASE)
    b.write_text(BASE)
    assert run("manifest", str(a), str(b))[0] == 2
    bad = tmp_path / "bad.kif"
    bad.write_text("fn f( {")
    assert run("manifest", str(bad))[0] == 1
    junk = tmp_path / "junk.json"
    junk.write_text("{}")
    assert run("lint", "--baseline", str(junk), str(a))[0] == 2


def test_lsp_baselines(tmp_path):
    src = tmp_path / "dev.kif"
    src.write_text(BASE)
    run("manifest", str(src), "-o", str(tmp_path))
    changed = BASE.replace("    method write(off: u32, value: u32) -> i32;\n", "")
    uri = path_to_uri(str(src))
    srv = Server()
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"initializationOptions": {"baselines": [str(tmp_path / "dev.json")]}}})
    out = srv.handle({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "text": changed}}})
    diags = [d for m in out if m.get("method") == "textDocument/publishDiagnostics" for d in m["params"]["diagnostics"]]
    assert any(d["code"] == "KIFL021" for d in diags)
    out = srv.handle({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {"textDocument": {"uri": uri}, "contentChanges": [{"text": BASE}]}})
    diags = [d for m in out if m.get("method") == "textDocument/publishDiagnostics" for d in m["params"]["diagnostics"]]
    assert not any(d["code"].startswith("KIFL02") for d in diags)


def test_every_baseline_rule_is_documented():
    text = open(os.path.join(ROOT, "docs", "kif-abi.md"), encoding="utf-8").read()
    for rule in RULES:
        if rule in {f"KIFL02{n}" for n in range(10)} | {"KIFL033"}:
            assert re.search(rf"^\| {rule} \|", text, re.M), rule
