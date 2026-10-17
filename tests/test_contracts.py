# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`#[preserves]` verification (KIFI030)."""

from kiflang import passes
from kiflang.symbols import analyze


def codes(src: str):
    m = analyze("t.kif", src)
    return sorted({d.code for d in passes.run_module(m.tree, ["contracts"]).diagnostics})


STACK = """
struct stack { n: u32, v: vector<u32> }
#[preserves]
fn keep(s: &mut stack) { s.v[0] = 1; }
"""


def test_element_write_keeps_the_promise():
    assert codes(STACK) == []


def test_field_write_breaks_it():
    assert codes("struct st { n: u32 }\n#[preserves]\nfn f(s: &mut st) { s.n = 1; }\n") == ["KIFI030"]


def test_calling_a_preserving_callee_is_fine():
    src = "struct st { n: u32 }\n#[preserves]\nfn g(s: &mut st) { }\n#[preserves]\nfn f(s: &mut st) { g(s); }\n"
    assert codes(src) == []


def test_calling_a_non_preserving_callee_breaks_it():
    src = "struct st { n: u32 }\nfn g(s: &mut st) { s.n = 2; }\n#[preserves]\nfn f(s: &mut st) { g(s); }\n"
    assert codes(src) == ["KIFI030"]


def proved(src: str, fn: str):
    """The checks of `fn` after `prove`: (kind, proof text or None)."""
    m = analyze("t.kif", src)
    r = passes.run_module(m.tree, ["prove"])
    f = next(f for f in r.functions if f.fn.name == fn).fn
    return [(c.kind, c.proof) for _, c in f.checks()]


PICK = """
#[ensures(some)]
fn pick(x: u32) -> optional<u32> { if x > 3 { some(x) } else { none() } }
fn use1(a: u32) -> u32 { let o = pick(a); *o.value() }
"""


def test_ensures_some_proves_the_value_check_of_the_caller():
    assert all(p for k, p in proved(PICK, "use1") if k == "some")


def test_ensures_is_checked_at_the_return_of_the_callee():
    checks = proved(PICK, "pick")
    assert [k for k, _ in checks] == ["some"] and checks[0][1] is None  # cannot be proved: x <= 3 returns none


def test_ensures_non_empty_proves_the_index_of_the_caller():
    src = "#[ensures(non_empty)]\nfn head(v: &[u32]) -> &[u32] { v }\nfn f(b: &[u32]) -> u32 { let h = head(b); h[0] }\n"
    assert all(p for k, p in proved(src, "f") if k == "index")


def test_without_ensures_the_caller_still_checks():
    src = "fn head(v: &[u32]) -> &[u32] { v }\nfn f(b: &[u32]) -> u32 { let h = head(b); h[0] }\n"
    assert [p for k, p in proved(src, "f") if k == "index"] == [None]


KEEP = """
{attr}
fn touch(v: &mut vector<u32>) {{ v[0] = 1; }}
fn f(v: &mut vector<u32>) -> u32 {{ if v.len() > 0 {{ touch(v); return v[0]; }} 0 }}
"""


def test_preserves_keeps_a_length_fact_across_the_call():
    assert all(p for k, p in proved(KEEP.format(attr="#[preserves]"), "f") if k == "index")


def test_without_preserves_the_call_forgets_the_fact():
    assert [p for k, p in proved(KEEP.format(attr=""), "f") if k == "index"] == [None]


def test_contract_fields_survive_ir_serialisation():
    from kiflang import ir, serial

    src = "#[ensures(some)]\nfn pick(x: u32) -> optional<u32> { some(x) }\n#[preserves]\nfn g(v: &mut vector<u32>) {}\nfn f(v: &mut vector<u32>) -> u32 { g(v); *pick(1).value() }\n"
    back = serial.loads_ir(serial.dumps_ir(ir.build_module(analyze("t.kif", src).tree)))
    calls = {i.callee: i for fn in back.functions for _, i in fn.instrs() if isinstance(i, ir.Call)}
    assert calls["pick"].ensures == ("some", 0) and calls["g"].preserves == [0]


def test_a_reference_kept_in_a_variable_is_followed():
    src = "struct st { n: u32 }\nfn g(s: &mut st) { s.n = 2; }\n#[preserves]\nfn f(s: &mut st) { let r = &mut *s; g(r); }\n"
    assert codes(src) == ["KIFI030"]


def test_a_reference_kept_in_a_struct_is_followed():
    src = ("struct st { n: u32 }\nstruct h { r: &mut st }\nfn g(x: h) { x.r.n = 2; }\n"
           "#[preserves]\nfn f(s: &mut st) { let x = h { r: &mut *s }; g(x); }\n")
    assert codes(src) == ["KIFI030"]
