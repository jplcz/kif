# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The IR borrow check (kiflang/borrowck.py)."""

from kiflang import passes
from kiflang.symbols import analyze


def codes(src):
    m = analyze("t.kif", src)
    return sorted({d.code for d in passes.run_module(m.tree, ["borrows"]).diagnostics})


def test_reference_through_if_result_is_tracked():
    assert codes("""
fn f(c: bool) -> u32 {
    let mut x: u32 = 1;
    let mut y: u32 = 5;
    let r = if c { &x } else { &y };
    x = 3;
    *r
}""") == ["KIFI020"]


def test_a_dead_reference_does_not_conflict():
    assert codes("""
fn f(c: bool) -> u32 {
    let mut x: u32 = 1;
    let mut y: u32 = 5;
    let r = if c { &x } else { &y };
    let v = *r;
    x = 3;
    v + y
}""") == []


def test_loan_carried_round_a_loop():
    assert codes("""
fn f() -> u32 {
    let mut x: u32 = 1;
    let y: u32 = 2;
    let mut k: u32 = 0;
    let mut r = &y;
    while k < 3 {
        x = 1;
        let v = *r;
        r = &x;
        k += 1;
    }
    0
}""") == ["KIFI020"]


def test_receiver_borrow_is_two_phase():
    assert codes("""
struct c { n: u32 }
impl c {
    fn get(&self) -> u32 { self.n }
    fn set(&mut self, v: u32) { self.n = v; }
}
fn f() { let mut a = c { n: 1 }; a.set(a.get() + 1); }""") == []


def test_disjoint_fields_do_not_conflict():
    assert codes("""
struct p { a: u32, b: u32 }
fn f() -> u32 {
    let mut s = p { a: 1, b: 2 };
    let r = &mut s.a;
    let q = &mut s.b;
    *r = 3;
    *q = 4;
    s.a + s.b
}""") == []


def test_same_field_twice_conflicts():
    assert codes("""
struct p { a: u32, b: u32 }
fn f() -> u32 {
    let mut s = p { a: 1, b: 2 };
    let r = &mut s.a;
    let q = &mut s.a;
    *r = 3;
    *q = 4;
    0
}""") == ["KIFI020"]


def test_two_constant_elements_do_not_conflict():
    assert codes("""
fn f() -> u32 {
    let mut a: [u32; 2] = [1, 2];
    let r = &mut a[0];
    let q = &mut a[1];
    *r = 3;
    *q = 4;
    0
}""") == []


def test_variable_index_may_hit_any_element():
    assert codes("""
fn f(i: usize) -> u32 {
    let mut a: [u32; 2] = [1, 2];
    let r = &mut a[0];
    let q = &mut a[i];
    *r = 3;
    *q = 4;
    0
}""") == ["KIFI020"]


def test_shared_borrows_coexist():
    assert codes("""
fn f() -> u32 {
    let x: u32 = 1;
    let r = &x;
    let q = &x;
    *r + *q
}""") == []


def test_write_while_shared_borrow_is_live_in_a_branch():
    assert codes("""
fn f(c: bool) -> u32 {
    let mut x: u32 = 1;
    let r = &x;
    if c { x = 2; }
    *r
}""") == ["KIFI020"]


def test_returning_a_borrow_of_a_parameter_by_value_is_rejected():
    assert codes("""
fn f(x: u32) -> &u32 { &x }
""") == ["KIFI021"]
