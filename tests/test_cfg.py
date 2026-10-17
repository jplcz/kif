# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Target variants: #[cfg], cfg(..) { } blocks, the cfg environment and the --cfg overrides."""

import io
import contextlib

import pytest

from kiflang import cfg, target
from kiflang.__main__ import main
from kiflang.symbols import analyze


def codes(src: str):
    return sorted(d.code for d in analyze("t.kif", src).diagnostics)


def pred(text: str):
    mod = analyze("t.kif", f"#[cfg({text})] fn f() {{}}")
    return cfg.of_attrs(mod.tree.items[0].attrs)


def test_exclusive_variants_coexist():
    assert codes('#[cfg(c)] fn f() {}\n#[cfg(cxx)] fn f() {}') == []
    assert codes('#[cfg(target = "a")] fn f() {}\n#[cfg(not(target = "a"))] fn f() {}') == []
    assert codes('struct s { #[cfg(c)] a: i32, #[cfg(cxx)] a: i64 }') == []


def test_overlapping_or_plain_duplicates_still_error():
    assert codes('fn f() {}\nfn f() {}') == ["KIF010"]
    assert codes('#[cfg(kobj)] fn f() {}\n#[cfg(c)] fn f() {}') == ["KIF010"]
    assert codes('#[cfg(c)] fn f() {}\nfn f() {}') == ["KIF010"]


def test_malformed_predicate():
    assert "KIFP200" in codes("#[cfg(foo(1))] fn f() {}")
    assert "KIFP200" in codes("#[cfg(a, b)] fn f() {}")
    assert "KIFP200" in codes("fn f() { cfg(not()) { } }")


def test_use_of_conditional_item():
    src = "#[cfg(kobj)] fn only() {}\n"
    assert "KIFP201" in codes(src + "fn f() { only(); }")
    assert codes(src + "fn f() { cfg(kobj) { only(); } }") == []
    assert codes(src + "#[cfg(kobj)] fn f() { only(); }") == []
    assert codes(src + "#[cfg(all(kobj, c))] fn f() { only(); }") == []
    assert "KIFP201" in codes(src + "#[cfg(c)] fn f() { only(); }")
    assert codes("#[cfg(c)] fn p() {}\n#[cfg(not(c))] fn p() {}\nfn f() { p(); }") == []


def test_cfg_block_is_checked_everywhere():
    assert codes("fn f() { cfg(kobj) { let x: i32 = true; } }") == ["KIFP012"]


def test_env_and_overrides():
    env = target.load("freebsd-kernel").cfg_env()
    assert env.test(pred('target = "freebsd-kernel"')) and env.test(pred("kobj")) and env.test(pred('os = "freebsd"'))
    env = target.load("freebsd-kernel").cfg_env(["os=netbsd", "!kobj", "extra"])
    assert env.test(pred('os = "netbsd"')) and not env.test(pred('os = "freebsd"'))
    assert not env.test(pred("kobj")) and env.test(pred("extra"))
    assert env.test(pred("any(extra, c)")) and not env.test(pred("all(extra, not(c))"))


def test_profiles_and_sanitizers():
    t = target.load("freebsd-kernel")
    dbg = t.cfg_env()
    assert dbg.test(pred('profile = "debug"')) and dbg.test(pred("debug")) and dbg.test(pred("debug_assertions"))
    assert not dbg.test(pred("sanitize"))
    rel = t.cfg_env(profile="release")
    assert not rel.test(pred("debug_assertions")) and rel.test(pred("overflow_checks")) and rel.test(pred('opt_level = "2"'))
    san = t.cfg_env(profile="sanitize")
    assert san.test(pred('sanitizer = "address"')) and san.test(pred("sanitize"))
    assert not t.cfg_env(profile="sanitize", sanitize=["none"]).test(pred("sanitize"))
    assert t.cfg_env(sanitize=["thread"]).test(pred('sanitizer = "thread"'))
    assert not t.cfg_env(["!debug_assertions"]).test(pred("debug_assertions"))


def test_target_defined_profile(tmp_path):
    p = tmp_path / "t.json"
    p.write_text('{"format": "kif-target", "version": 1, "name": "k", "extends": "c", "profile": "kasan", "sanitize": ["kernel-address"],'
                 ' "profiles": {"kasan": {"opt": "1", "assertions": true, "cfg": {"extra": true}},'
                 ' "debug": {"cfg": {"witness": true}}}}')
    t = target.load(str(p))
    env = t.cfg_env()
    assert env.test(pred("extra")) and env.test(pred('sanitizer = "kernel-address"')) and env.test(pred('profile = "kasan"'))
    assert t.cfg_env(profile="debug").test(pred("witness")) and t.profile("debug")["opt"] == "0"
    with pytest.raises(target.TargetError):
        t.profile("nope")
    p.write_text('{"format": "kif-target", "version": 1, "name": "k", "extends": "c", "profiles": {"x": {"opt": "9"}}}')
    assert target.check_file(str(p))


def test_cli_profile(tmp_path):
    f = tmp_path / "a.kif"
    f.write_text("fn f() {}\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["check", str(f), "--target", "c", "--profile", "perf", "--sanitize", "address,undefined", "--print-cfg"]) == 0
    text = out.getvalue()
    assert 'profile = "perf"' in text and 'sanitizer = "undefined"' in text and "debug_assertions" not in text
    assert main(["check", str(f), "--profile", "nope"]) == 2


def test_bad_option():
    with pytest.raises(cfg.CfgError):
        cfg.parse_option("!a=b")
    with pytest.raises(cfg.CfgError):
        cfg.parse_option("1x")


def test_interpreter_runs_only_active_cfg_block(tmp_path):
    from kiflang import interp

    src = "fn run() -> i32 { let mut n = 0; cfg(arch = \"arm64\") { n = 1; } cfg(arch = \"x86\") { n = 2; } n }"
    for arch, want in (("arm64", 1), ("x86", 2), ("riscv", 0)):
        mod = analyze("t.kif", src, cfg_env=cfg.Env().override([f"arch={arch}"]))
        assert [d for d in mod.diagnostics if d.severity == 1] == []
        assert interp.Interp(mod).call("run", []) == want


def test_cli_cfg_and_print(tmp_path):
    f = tmp_path / "a.kif"
    f.write_text("fn f() {}\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["check", str(f), "--target", "linux-kernel", "--cfg", "arch=arm64", "--print-cfg"]) == 0
    assert 'arch = "arm64"' in out.getvalue() and 'os = "linux"' in out.getvalue()
    assert main(["check", str(f), "--cfg", "!1"]) == 2


def lint_codes(src: str, env=None):
    from kiflang.lint import lint

    mod = analyze("t.kif", src, cfg_env=env)
    return [d.code for d in lint(mod)]


LEAK = """
struct ring { n: u32 = 0 }
impl ring { fn drop(&mut self) {} fn touch(&mut self) {} fn make() -> ring { ring { n: 1 } } }
fn f() { let mut r = ring::make(); r.touch(); }
"""


def test_no_lint_for_undropped_local_in_c_mode():
    # the C backend drops locals itself (docs/kif-c-backends.md 3.2), so KIFL034 no longer exists
    assert "KIFL034" not in lint_codes("#![target(c)]\n" + LEAK)
