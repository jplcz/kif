# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The `ct::` optimizer guard (docs/kif-taint.md section 7a): rules G1-G7, KIFT012."""

import pytest

from kiflang import ctguard, ir, passes
from kiflang.symbols import analyze

SAN = "#[untaint]\nfn clamp(#[tainted] i: usize, len: usize) -> usize { ct::select(ct::lt(i, len), i, 0) }\n"
PLAIN = "fn plain(a: usize) -> usize { a + 1 }\n"
USE = "fn use_it(#[tainted] i: usize, flag: bool) -> usize {\n    let mut r = 0;\n    if flag { r = 1; }\n    r + untaint!(\"clamped\", clamp(i, 8))\n}\n"


def build(src):
    m = analyze("t.kif", src)
    assert not [d for d in m.diagnostics if d.severity == 1], m.diagnostics
    return m.tree


def run(src, names):
    return passes.run_module(build(src), names)


def codes(res):
    return [d.code for d in res.diagnostics]


@pytest.fixture
def rogue(monkeypatch):
    monkeypatch.setattr(passes, "REGISTRY", dict(passes.REGISTRY))

    def make(fn_body):
        @passes.register
        class Rogue(passes.Pass):
            name, kind, default = "rogue", "transform", False

            def run(self, ctx):
                fn_body(ctx.fn)

        return "rogue"

    return make


def test_the_region_is_the_ct_calls_and_the_whole_body_of_a_checked_sanitizer():
    res = run(SAN + PLAIN + USE, [])
    fns = {f.name: f for f in res.module.functions}
    assert ctguard.has_region(fns["clamp"]) and all(i.ct for _b, i in fns["clamp"].instrs())
    assert not ctguard.has_region(fns["plain"])
    assert not any(getattr(i, "ct", False) for _b, i in fns["use_it"].instrs())   # only a call of the sanitizer


def test_the_standard_passes_keep_the_shape():
    assert "KIFT012" not in codes(run(SAN + USE, passes.named_pipeline("default")))


def test_a_new_branch_is_g1(rogue):
    def add_branch(fn):
        b = fn.blocks[0]
        b.term = ir.Branch(ir.Const(True, "bool"), b, b)
    name = rogue(add_branch)
    res = run(SAN, [name])
    assert any("G1" in d.message and "'rogue'" in d.message for d in res.diagnostics), res.diagnostics


def test_removing_a_ct_operation_is_g3(rogue):
    def drop(fn):
        for b in fn.blocks:
            b.instrs = [i for i in b.instrs if not ctguard.is_ct_call(i)]
    res = run(SAN, [rogue(drop)])
    assert any("G3" in d.message and "ct::select" in d.message for d in res.diagnostics), res.diagnostics


def test_reordering_ct_operations_is_flagged(rogue):
    def swap(fn):
        b = next(b for b in fn.blocks if sum(ctguard.is_ct_call(i) for i in b.instrs) >= 2)
        ks = [k for k, i in enumerate(b.instrs) if ctguard.is_ct_call(i)]
        b.instrs[ks[0]], b.instrs[ks[1]] = b.instrs[ks[1]], b.instrs[ks[0]]
    res = run(SAN, [rogue(swap)])
    assert any("G4" in d.message for d in res.diagnostics), res.diagnostics


def test_removing_a_barrier_is_g4(rogue):
    def drop(fn):
        for b in fn.blocks:
            b.instrs = [i for i in b.instrs if not isinstance(i, ir.Barrier)]
    src = "fn tab(a: usize) -> usize {\n    spec::barrier();\n    ct::mask(true) + a\n}\n"
    res = run(src, [rogue(drop)])
    assert any("G4" in d.message and "barrier" in d.message for d in res.diagnostics), res.diagnostics


def test_a_new_taint_finding_is_g2(rogue):
    def retarget(fn):
        tainted = next(i.dst for _b, i in fn.instrs() if isinstance(i, ir.Load) and i.local.name == "t")
        for b in fn.blocks:
            if isinstance(b.term, ir.Branch):
                b.term.cond = tainted
    src = "fn f(#[tainted] t: bool, flag: bool) -> usize {\n    let c = t;\n    let r = ct::mask(flag);\n    if flag { r + 1 } else { r }\n}\n"
    res = run(src, [rogue(retarget)])
    assert any("G2" in d.message and "KIFT001" in d.message for d in res.diagnostics), res.diagnostics


def test_text_check_g7():
    barrier = lambda v: f'__asm__ volatile ("" : "+r"({v}))'
    good = ["/* kif: ct intrinsic ct::lt(): branch-free, value barrier after it (t.kif:3) */", "\tkif_t4 = (u64)(x);", f"\t{barrier('kif_t4')};"]
    assert ctguard.verify_text(good, barrier) == []
    assert ctguard.verify_text(good[:2] + ["\treturn 0;"], barrier)
    assert ctguard.verify_text([good[0], good[2], good[1]], barrier)


def test_c_output_keeps_every_ct_barrier():
    from kiflang import lower_c, target
    t = target.load("c")
    m = analyze("t.kif", SAN, cfg_env=t.cfg_env())
    out = "\n".join(lower_c.lower(m, t, "t").values())
    assert "kif: ct intrinsic ct::select" in out and "__asm__" in out
