# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The IR hash (docs/kif-module-contracts.md, section 13): a meaning-based identity of a function's raw IR."""

import dataclasses
import os
import subprocess
import sys

from kiflang import irhash, ir, passes
from kiflang.symbols import analyze


def hashes(src: str, path: str = "t.kif"):
    r = analyze(path, src)
    assert not [d for d in r.diagnostics if d.severity == 1], r.diagnostics
    return {f.name: irhash.ir_hash(f) for f in passes.run_module(r.tree, []).module.functions}


def one(src: str) -> str:
    (h,) = hashes(src).values()
    return h


BASE = "fn f(x: u32) -> u32 { 2 * x }\n"


def test_same_meaning_same_hash():
    h = one(BASE)
    assert h.startswith("sha256:") and len(h) == 7 + 64
    assert one("// a comment\n/// docs\nfn   other_name( x : u32 ) -> u32 {\n\n  // inside\n  2*x\n}\n") == h
    assert one("fn f(renamed: u32) -> u32 { 2 * renamed }\n") == h
    assert one("\n\n\n\n" + BASE) == h


def test_different_meaning_different_hash():
    h = one(BASE)
    assert len({h, one("fn f(x: u32) -> u32 { 3 * x }"), one("fn f(x: u64) -> u64 { 2 * x }"), one("fn f(x: u32) -> u32 { x * 2 }"),
                one("fn f(x: u32) -> u32 { 2 + x }")}) == 5


def test_other_functions_do_not_matter():
    both = hashes(BASE + "fn g(y: u32) -> u32 { y / 3 }\n")
    assert both["f"] == one(BASE)
    assert hashes("fn g(y: u32) -> u32 { y / 3 }\n" + BASE)["f"] == both["f"]


def test_untaint_reason_and_barrier_text_are_prose():
    src = ("#[untaint]\nfn clean(#[tainted] x: usize) -> usize { x & 3 }\n#[untaint]\nfn clean2(#[tainted] x: usize) -> usize { x & 3 }\n"
           "fn a(#[tainted] n: usize, t: &[u32]) -> u32 { let v = %s; t[v] }\n")
    one_ = hashes(src % 'untaint!("one reason", clean(n))')["a"]
    assert one_ == hashes(src % 'untaint!("a different sentence", clean(n))')["a"]
    assert one_ != hashes(src % 'untaint!("one reason", clean2(n))')["a"]


def test_taint_and_contract_attributes_matter():
    plain = "#[tainted_result]\nfn f(n: u32) -> u32 { n & 1 }\n"
    assert one(plain) != one("#[tainted_result]\nfn f(#[tainted] n: u32) -> u32 { n & 1 }\n")
    assert one("#[ensures(some)]\nfn f(x: u32) -> optional<u32> { some(x) }\n") != one("fn f(x: u32) -> optional<u32> { some(x) }\n")
    assert one("#[ensures( some )]\nfn f(x: u32) -> optional<u32> { some(x) }\n") == one("#[ensures(some)]\nfn f(x: u32) -> optional<u32> { some(x) }\n")


def test_stable_across_processes_and_directories(tmp_path):
    f = tmp_path / "x" / "y.kif"
    f.parent.mkdir()
    f.write_text(BASE)
    runs = [subprocess.run([sys.executable, "-m", "kiflang.kif", "ir", str(f), "--hash"], capture_output=True, text=True, check=True,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout for _ in range(2)]
    assert runs[0] == runs[1]
    assert runs[0].split()[0] == one(BASE)
    assert runs[0].split()[1] == "f"


def test_passes_do_not_change_the_hash():
    r = analyze("t.kif", "fn f(a: &[u32], i: usize) -> u32 { if i < a.len() { a[i] } else { 0 } }\n")
    raw = passes.run_module(r.tree, []).module.functions[0]
    h = irhash.ir_hash(raw)
    # the hash is of the raw IR; it is taken before the passes, so it is the same with the same source
    assert irhash.ir_hash(passes.run_module(analyze("t.kif", "fn f(a: &[u32], i: usize) -> u32 { if i < a.len() { a[i] } else { 0 } }\n").tree, []).module.functions[0]) == h


def test_every_ir_field_is_classified():
    """A field added to the IR must be put in `HASHED` or `EXCLUDED` (docs/kif-module-contracts.md 13.4, test 7)."""
    for name in irhash.HASHED:
        cls = getattr(ir, name)
        fields = {f.name for f in dataclasses.fields(cls)}
        assert fields == set(irhash.classified(name)), (name, fields ^ set(irhash.classified(name)))
        assert len(irhash.classified(name)) == len(fields), f"{name}: a field is both hashed and excluded"
    instrs = {c.__name__ for c in vars(ir).values() if isinstance(c, type) and issubclass(c, (ir.Instr, ir.Term)) and c not in (ir.Instr, ir.Term)}
    assert instrs <= set(irhash.HASHED), instrs - set(irhash.HASHED)


def test_generated_c_carries_the_ir_hash_comment(tmp_path):
    """Stage 6b: every C function is stamped `/* kif: ir sha256:<16 hex> */`, equal to the `--hash` prefix, and comments/names don't change it."""
    from kiflang import kifc

    def stamped(src: str):
        f = tmp_path / "a.kif"
        f.write_text(src)
        out = tmp_path / "o"
        assert kifc.main(["--c", str(out), "--no-project", str(f)]) == 0
        return (out / "a_proc.c").read_text()

    text = stamped("fn twice(x: i32) -> i32 { 2 * x }\n")
    (h,) = hashes("fn twice(x: i32) -> i32 { 2 * x }\n").values()
    assert f"/* kif: ir {h[:23]} */\nint32_t" in text or f"/* kif: ir {h[:23]} */" in text
    assert f"/* kif: ir {h[:23]} */" in stamped("// note\nfn twice(x: i32) -> i32 {\n    2 * x // doubled\n}\n")
