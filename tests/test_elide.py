# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The `elide` pass (kiflang/elide.py, docs/kif-ir-passes.md): proved checks leave the IR, calls learn whether they may skip the guard."""

import pytest

from kiflang import ir, lower_c, passes, serial, symbols, target

SRC = """fn need(#[non_empty] v: &[u32]) -> u32 { v[0] }
fn safe(v: &[u32]) -> u32 { if v.is_empty() { 0 } else { need(v) } }
fn unsafe_(v: &[u32]) -> u32 { need(v) }
fn fixed(a: [u32; 4], i: usize) -> u32 { a[2] + a[i] }
"""


def run(src=SRC, access=None, names="default"):
    m = symbols.analyze("/t/main.kif", src, None)
    mod = ir.build_module(m.tree)
    return {f.name: passes.run_function(f, passes.pipeline(names), mod.resolver, {"access": access or {}}).fn for f in mod.functions}, mod


def kinds(f):
    return [(c.kind, c.proof is not None) for _, c in f.checks()]


def test_a_proved_check_leaves_the_ir_and_is_recorded():
    f = run()[0]["fixed"]
    assert kinds(f) == [("index", False)]  # a[i] stays
    assert [(c.kind, c.proof) for c in f.elided] == [("index", "constant index 2 < 4")]
    assert "// elided: check index" in ir.dump(f)


def test_the_checked_tier_keeps_every_check_of_its_class():
    f = run(access={"index": "checked"})[0]["fixed"]
    assert kinds(f) == [("index", True), ("index", False)] and not f.elided


def test_a_call_whose_requirements_are_proved_skips_the_guard():
    fns = run()[0]
    (call,) = [i for _, i in fns["safe"].instrs() if isinstance(i, ir.Call) and i.callee == "need"]
    assert call.unguarded and not [c for _, c in fns["safe"].checks() if c.call]
    (call,) = [i for _, i in fns["unsafe_"].instrs() if isinstance(i, ir.Call) and i.callee == "need"]
    assert not call.unguarded and not [c for _, c in fns["unsafe_"].checks() if c.call]  # the callee's guard checks it
    assert [c.proof for c in fns["unsafe_"].elided] == [None]


def test_the_checked_tier_keeps_the_guard_of_a_call():
    (call,) = [i for _, i in run(access={"optional": "checked", "index": "checked"})[0]["safe"].instrs() if isinstance(i, ir.Call) and i.callee == "need"]
    assert not call.unguarded


def test_entry_assumptions_are_always_removed():
    f = run(access={"index": "checked", "optional": "checked"})[0]["need"]
    assert not [c for _, c in f.checks() if c.tag]


@pytest.mark.parametrize("as_cbor", [False, True])
def test_elision_survives_the_data_encoding(as_cbor):
    _, mod = run()
    back = serial.loads_ir(serial.dumps_ir(mod, as_cbor=as_cbor))
    assert ir.dump_module(back) == ir.dump_module(mod)
    assert [c.kind for c in back.functions[3].elided] == [c.kind for c in mod.functions[3].elided]


def lowered_body(src, **over):
    t = target.load("c")
    t.data.update({**over})
    m = symbols.analyze("t.kif", src, cfg_env=t.cfg_env())
    return "\n".join(lower_c.lower(m, t, "t").values())


def test_the_emitter_writes_what_is_left():
    src = "fn f(a: [u32; 4], i: usize) -> u32 { a[2] + a[i] }"
    text = lowered_body(src)
    assert text.count("KIF_PANIC") == 1  # a[2] was proved and removed; a[i] stays
    text = lowered_body(src, access={"index": "checked"})
    assert text.count("KIF_PANIC") == 2
