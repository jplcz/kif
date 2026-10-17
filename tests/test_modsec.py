# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The `security` section of a `.kifm` (docs/kif-module-contracts.md, sections 3, 4 and 8): writer, reader, tamper evidence."""

import copy
import os
import shutil

import pytest

from kiflang import __version__, cbor, modfile, prover_id, target
from kiflang.symbols import analyze
from kiflang.sysroot import Sysroot

VENDOR = """
namespace ddk;
#[tainted]
struct packet { payload: u32 = 0 }
struct frame { #[tainted] raw: u32 = 0, n: u32 = 0 }
#[untaint]
fn clean(#[tainted] x: usize) -> usize { x & 3 }
#[untaint(trusted)]
fn vouched(#[tainted] x: usize) -> usize { x }
#[tainted_result]
fn read_packet(n: usize) -> usize { n }
#[export]
#[tainted_result]
fn handle(buf: &[u32], n: usize) -> u32 { buf[0] + (n as u32) }
#[trusted]
#[export]
fn open(n: usize) -> usize { n }
fn first(#[len_ge(1)] a: &[u32]) -> u32 { a[0] }
fn unproved(a: &[u32], i: usize) -> u32 { a[i] }
fn proved(a: &[u32], i: usize) -> u32 { if i < a.len() { a[i] } else { 0 } }
fn id<T: copy>(x: T) -> T { x }
struct ring { n: u32 = 0 }
impl ring { fn len(&self) -> u32 { self.n } }
"""


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "sysroot"
    r.mkdir()
    for name in ("reloco.kif", "microfmt.kif"):
        shutil.copy(os.path.join(os.path.dirname(__import__("kiflang").__file__), "prelude", name), r / name)
    return r


def export(src=VENDOR, **kw):
    mod = analyze("/v/ddk.kif", src, cfg_env=kw.pop("env", None))
    assert not [d for d in mod.diagnostics if d.severity == 1], mod.diagnostics
    return modfile.export(mod, **kw)


def section(src=VENDOR, **kw):
    return modfile.decode(export(src, **kw))["security"]


def import_codes(root, data):
    with open(root / "ddk.kifm", "wb") as f:
        f.write(data)
    return [d.code for d in analyze("/p/m.kif", 'import "ddk.kif";', sysroot=Sysroot(str(root))).diagnostics]


def reencode(data, edit, fix_digest=True):
    h = cbor.loads(data)
    edit(h)
    if fix_digest:
        h["security_digest"] = modfile.security_digest(h["security"])
    return cbor.dumps(h)


def test_section_records_the_effective_contracts():
    s = section()
    fns = s["functions"]
    assert s["target"] == "c" and s["profile"] == "debug" and s["checked"] is True
    assert s["taint"] == {"exports": "declared", "sinks": ["div", "rem", "shift"]}
    assert fns["clean"]["untaint"] == "checked" and fns["vouched"]["untaint"] == "trusted" and fns["handle"]["untaint"] is None
    assert fns["clean"]["tainted_params"] == ["x"] and fns["read_packet"]["tainted_result"] is True
    assert fns["handle"]["export"] and fns["handle"]["tainted_params"] == []
    assert fns["open"]["trusted"] is True
    assert fns["first"]["contracts"] == ["len_ge(1)@a"]
    assert fns["id"] == {"embedded": True}
    assert "ring::len" in fns
    assert s["types"] == {"packet": {"tainted": True, "tainted_fields": []}, "frame": {"tainted": False, "tainted_fields": ["raw"]}}


def test_exports_policy_makes_exports_tainted_in_the_section():
    from kiflang.cfg import Env

    s = section(env=Env({"taint_exports": {"all"}}, set()))
    assert s["taint"]["exports"] == "all"
    assert s["functions"]["handle"]["tainted_params"] == ["buf", "n"]
    assert s["functions"]["open"]["tainted_params"] == []          # #[trusted]: exempt


def test_analysis_results_of_the_compiled_bodies():
    fns = section()["functions"]
    assert fns["unproved"]["kept_checks"] == ["index"] and fns["unproved"]["proved"] == []
    assert fns["proved"]["kept_checks"] == [] and fns["proved"]["proved"] == ["index"]
    assert fns["unproved"]["ir_hash"].startswith("sha256:") and fns["unproved"]["ir_hash"] != fns["proved"]["ir_hash"]
    assert fns["ring::len"]["kept_checks"] == [] and fns["ring::len"]["ir_hash"].startswith("sha256:")   # methods are built to IR too


def test_methods_are_keyed_by_their_declaration_not_their_name():
    """Two types with a method of one name, and a free function of that name, each get their own facts and hash."""
    fns = section("""
struct a { n: u32 = 0 }
struct b { n: u32 = 0 }
impl a { fn get(&self, v: &[u32], i: usize) -> u32 { v[i] } }
impl b { fn get(&self, v: &[u32], i: usize) -> u32 { if i < v.len() { v[i] } else { 0 } } }
fn get(x: u32) -> u32 { 2 * x }
""")["functions"]
    assert fns["a::get"]["kept_checks"] == ["index"] and fns["b::get"]["kept_checks"] == [] and fns["b::get"]["proved"] == ["index"]
    assert len({fns[k]["ir_hash"] for k in ("a::get", "b::get", "get")}) == 3


def test_header_has_compiler_identity_and_digests():
    h = modfile.decode(export())
    assert h["compiler"] == {"name": "kif", "version": __version__, "prover": prover_id.prover_id(), "security_schema": 1}
    assert h["security_digest"] == modfile.security_digest(h["security"])
    assert target.load("c").name == h["security"]["target"]


def test_version_matches_pyproject():
    import re

    text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")).read()
    assert re.search(r'^version = "%s"' % re.escape(__version__), text, re.M)


def test_deterministic():
    assert export() == export()
    # a no-op edit of a body leaves the section unchanged
    assert section() == section(VENDOR.replace("fn first(#[len_ge(1)] a: &[u32]) -> u32 { a[0] }", "fn first(#[len_ge(1)] a: &[u32]) -> u32 {\n    // note\n    a[0]\n}"))


def test_clean_import_has_no_diagnostics(root):
    assert import_codes(root, export()) == []


@pytest.mark.parametrize("edit", [
    lambda h: h["security"]["functions"]["clean"].update(untaint="trusted"),
    lambda h: h["security"]["functions"]["vouched"].update(untaint="checked"),
    lambda h: h["security"]["functions"]["clean"].update(tainted_params=[]),
    lambda h: h["security"]["functions"]["first"].update(contracts=[]),
    lambda h: h["security"]["functions"]["read_packet"].update(tainted_result=False),
    lambda h: h["security"]["functions"]["open"].update(trusted=False),
    lambda h: h["security"]["functions"].pop("clean"),
    lambda h: h["security"]["functions"].update(ghost=copy.deepcopy(h["security"]["functions"]["clean"])),
    lambda h: h["security"]["types"].pop("packet"),
    lambda h: h["security"]["taint"].update(exports="all"),     # makes `handle`'s effective set differ from the declarations
], ids=["flip-untaint", "flip-trusted-evidence", "drop-tainted-params", "edit-contracts", "drop-tainted-result", "flip-trusted", "drop-entry",
        "extra-entry", "drop-type", "policy"])
def test_inconsistent_section_is_kif014_even_with_a_fixed_digest(root, edit):
    assert "KIF014" in import_codes(root, reencode(export(), edit))


def test_section_edit_without_fixing_digest_is_rejected(root):
    data = reencode(export(), lambda h: h["security"]["functions"]["clean"].update(untaint="trusted"), fix_digest=False)
    with pytest.raises(modfile.ModuleFileError, match="digest"):
        modfile.decode(data)
    assert "KIF014" in import_codes(root, data)


@pytest.mark.parametrize("key", ["security", "security_digest", "compiler"])
def test_missing_required_key_is_malformed(root, key):
    data = reencode(export(), lambda h: h.pop(key), fix_digest=False)
    with pytest.raises(modfile.ModuleFileError):
        modfile.decode(data)
    assert "KIF014" in import_codes(root, data)


def test_unknown_security_keys_are_ignored_and_bad_shapes_rejected():
    data = reencode(export(), lambda h: h["security"].update(extra="x"))
    assert modfile.decode(data)["security"]["extra"] == "x"
    for edit in (lambda h: h["security"].update(target=3), lambda h: h["security"]["functions"]["clean"].update(untaint="maybe"),
                 lambda h: h["security"]["functions"]["first"].update(kept_checks="some"), lambda h: h["compiler"].update(security_schema=9)):
        with pytest.raises(modfile.ModuleFileError):
            modfile.decode(reencode(export(), edit))


def test_export_records_the_target_and_profile():
    s = section(target_name="c", profile="release")
    assert s["target"] == "c" and s["profile"] == "release"


# -- stage 2: the importer's checks read the same facts from source and from a .kifm (design section 8, tests 1 and 2)

LIB = """
namespace ddk;
struct frame { #[tainted] raw: usize = 0, n: usize = 0 }
#[untaint]
fn clean(#[tainted] x: usize) -> usize { x & 3 }
#[tainted_result]
fn read(n: usize) -> usize { n }
fn plain(n: usize) -> usize { n }
fn hardened(#[tainted] n: usize) -> usize { n & 1 }
#[export]
#[tainted_result]
fn entry(n: usize) -> usize { n }
fn take(f: frame) -> usize { f.n }
"""

USES = {
    "tainted-to-plain": "fn f(#[tainted] n: usize) -> usize { ddk::plain(n) }",
    "tainted-to-hardened": "fn f(#[tainted] n: usize) -> usize { let _ = ddk::hardened(n); 0 }",
    "tainted-to-export": "fn f(#[tainted] n: usize) -> usize { let _ = ddk::entry(n); 0 }",
    "sanitizer-without-reason": "fn f(#[tainted] n: usize) -> usize { ddk::clean(n) }",
    "sanitizer-with-reason": 'fn f(#[tainted] n: usize, t: &[u32]) -> u32 { let a = untaint!("masked to 0..3", ddk::clean(n)); t[a] }',
    "result-is-tainted": "fn f(t: &[u32], c: usize) -> u32 { let a = ddk::read(c); t[a] }",
    "tainted-struct": "fn f(#[tainted] n: usize) -> usize { ddk::take(ddk::frame { raw: n, n: 0 }) }",
}


def both(root, uses, exports_all=False, importer_all=None):
    """The diagnostics of `uses` against the library as source and as its .kifm: `(source, kifm)` lists of (code, message)."""
    from kiflang.cfg import Env

    def env(all_):
        return Env({"taint_exports": {"all" if all_ else "declared"}}, set())

    sr = Sysroot(str(root))
    importer_all = exports_all if importer_all is None else importer_all
    (root / "ddk.kifm").unlink(missing_ok=True)
    (root / "ddk.kif").write_text(LIB)
    lib = analyze("/v/ddk.kif", LIB, sysroot=sr, cfg_env=env(exports_all))
    data = modfile.export(lib)
    out = []
    for kind in ("source", "kifm"):
        if kind == "kifm":
            (root / "ddk.kif").unlink()
            (root / "ddk.kifm").write_bytes(data)
        m = analyze("/p/m.kif", 'import "ddk.kif";\n' + uses, sysroot=sr, cfg_env=env(importer_all))
        out.append(sorted((d.code, d.message.replace("ddk.kifm", "ddk.kif")) for d in m.diagnostics))
    return out


@pytest.mark.parametrize("name", sorted(USES))
@pytest.mark.parametrize("policy", [False, True], ids=["declared", "exports-all"])
def test_source_and_kifm_imports_agree(root, name, policy):
    src, pre = both(root, USES[name], exports_all=policy)
    assert src == pre


EXPECTED = {
    "result-is-tainted": ["KIFT002"], "sanitizer-with-reason": [], "sanitizer-without-reason": ["KIFT010"], "tainted-to-hardened": [],
    "tainted-to-plain": ["KIFT003", "KIFT005"], "tainted-struct": ["KIFT003", "KIFT005"],
}


def test_expected_diagnostics_of_the_corpus(root):
    for name, codes in EXPECTED.items():
        assert sorted({c for c, _ in both(root, USES[name])[1]}) == codes, name


def test_exports_all_vendor_accepts_tainted_into_its_exports_whatever_the_importer_says(root):
    _, pre = both(root, USES["tainted-to-export"], exports_all=True, importer_all=False)
    assert not any(c == "KIFT003" for c, _ in pre)
    _, declared = both(root, USES["tainted-to-export"], exports_all=False, importer_all=False)
    assert any(c == "KIFT003" for c, _ in declared)


# ---------------------------------------------------------------- stage 3: use of the recorded contracts

def diags(root, data, src, env=None):
    with open(root / "ddk.kifm", "wb") as f:
        f.write(data)
    return [d.code for d in analyze("/p/m.kif", src, sysroot=Sysroot(str(root)), cfg_env=env).diagnostics]


PROVED_USE = 'import "ddk.kif";\n#[require_proved(index)]\nfn f(a: &[u32], i: usize) -> u32 { %s(a, i) }'


def test_kept_check_in_vendor_body_is_kifi040(root):
    assert diags(root, export(), PROVED_USE % "ddk::unproved") == ["KIFI040"]
    assert diags(root, export(), PROVED_USE % "ddk::proved") == []


def test_no_demand_no_diagnostic(root):
    assert diags(root, export(), 'import "ddk.kif";\nfn f(a: &[u32], i: usize) -> u32 { ddk::unproved(a, i) }') == []


def test_other_prover_is_kifi041_warning_and_error_under_must_match(root):
    other = reencode(export(), lambda h: h["compiler"].update(prover="sha256:" + "0" * 64), fix_digest=False)
    assert diags(root, other, PROVED_USE % "ddk::proved") == ["KIFI041"]
    env = target.load("c").cfg_env([], None, [])
    env.values["prover_must_match"] = {"1"}
    with open(root / "ddk.kifm", "wb") as f:
        f.write(other)
    ds = analyze("/p/m.kif", PROVED_USE % "ddk::proved", sysroot=Sysroot(str(root)), cfg_env=env).diagnostics
    assert [(d.code, d.severity) for d in ds] == [("KIFI041", 1)]


def test_other_language_is_kif016_and_other_target_kif017(root):
    cxx = export(env=target.load("cxx-user").cfg_env([], None, []), target_name="cxx-user")
    src = 'import "ddk.kif";'
    assert diags(root, cxx, src, target.load("c").cfg_env([], None, [])) == ["KIF016"]
    plain = VENDOR.replace("fn id<T: copy>(x: T) -> T { x }\n", "")
    fb = export(plain, env=target.load("freebsd-kernel").cfg_env([], None, []), target_name="freebsd-kernel")
    assert diags(root, fb, src, target.load("c").cfg_env([], None, [])) == ["KIF017"]
    assert diags(root, export(), src, target.load("c").cfg_env([], None, [])) == []


def test_stricter_importer_sinks_are_kift012_info(root):
    sinks = target.load("c").cfg_env([], None, [])
    sinks.values["taint_sinks"] = {"div", "rem", "shift", "index"}
    assert diags(root, export(), 'import "ddk.kif";', sinks) == ["KIFT012"]


# ---------------------------------------------------------------- stage 4: audit

def test_inspect_prints_the_recorded_contract(root, capsys):
    from kiflang.__main__ import main

    with open(root / "ddk.kifm", "wb") as f:
        f.write(export())
    assert main(["inspect", str(root / "ddk.kifm")]) == 0
    out = capsys.readouterr().out
    assert "module ddk" in out and "prover sha256:" in out and "target c (c)" in out
    assert "clean: untaint(checked) tainted_params=x" in out and "vouched: untaint(trusted)" in out
    assert "unproved: kept=index" in out and "id: embedded" in out


def test_inspect_rejects_a_bad_file(tmp_path, capsys):
    from kiflang.__main__ import main

    (tmp_path / "x.kifm").write_bytes(b"junk")
    assert main(["inspect", str(tmp_path / "x.kifm")]) == 1


def test_taint_report_labels_vendor_evidence(root):
    from kiflang import passes as passmgr, taint_ir

    with open(root / "ddk.kifm", "wb") as f:
        f.write(export())
    mod = analyze("/p/m.kif", 'import "ddk.kif";\nfn f(n: usize) -> usize { n }', sysroot=Sysroot(str(root)))
    text = taint_ir.report(passmgr.run_module(mod.tree, []).module, "m.kif", mod.imported)
    assert "sanitizer clean (from ddk.kif, vendor-checked)" in text
    assert "sanitizer vouched (from ddk.kif, vendor-trusted)" in text
    assert "#[trusted] export open (from ddk.kif)" in text and "tainted result read_packet (from ddk.kif)" in text
