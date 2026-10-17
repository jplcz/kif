# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The basic check provers (kiflang/prove.py): what they prove, and, as important, what they must not."""

import pytest

from kiflang import ir, prove, symbols


def run(src):
    m = symbols.analyze("/t/main.kif", src, None)
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    mod = ir.build_module(m.tree)
    prove.prove_module(mod)
    return {f.name: [c.proof is not None for _, c in f.checks()] for f in mod.functions}


def test_constants():
    r = run("fn f(a: [u32; 4]) -> u32 { a[0] + a[3] }\nfn g(a: [u32; 4]) -> u32 { a[4] }")
    assert r["f"] == [True, True] and r["g"] == [False]


def test_dominating_if_proves_unwrap_and_the_repeat_is_redundant():
    r = run("""fn f(o: optional<u32>) -> u32 { if o.has_value() { *o.value() + *o.value() } else { 0 } }
fn g(o: optional<u32>) -> u32 { *o.value() + *o.value() }
fn h(o: optional<u32>) -> u32 { if o.has_value() { 1 } else { *o.value() } }""")
    assert r["f"] == [True, True]  # the test proves the first, the passed check proves the second
    assert r["g"] == [False, True]  # no test: the first check stays, but once it passed the second is redundant
    assert r["h"] == [False]  # the wrong edge


def test_facts_must_hold_on_every_path_to_the_join():
    r = run("""fn f(o: optional<u32>, c: bool) -> u32 { if c { if !o.has_value() { return 0; } } *o.value() }
fn g(o: optional<u32>, c: bool) -> u32 { if !o.has_value() { return 0; } *o.value() }""")
    assert r["f"] == [False] and r["g"] == [True]


def test_pointer_null_test():
    r = run("fn f(p: ptr<u32>) -> u32 { if p.is_null() { 0 } else { *p } }\nfn g(p: ptr<u32>) -> u32 { if p.is_null() { *p } else { 0 } }")
    assert r["f"] == [True] and r["g"] == [False]


def test_index_below_len_in_a_loop_and_after_an_early_return():
    r = run("""fn loop_(v: &vector<u32>) -> u32 { let mut k: usize = 0; let mut t: u32 = 0; while k < v.len() { t += *v[k]; k += 1; } t }
fn guard(v: &vector<u32>, i: usize) -> u32 { if i >= v.len() { return 0; } *v[i] }
fn flip(v: &vector<u32>, i: usize) -> u32 { if v.len() > i { *v[i] } else { 0 } }
fn bare(v: &vector<u32>, i: usize) -> u32 { *v[i] }""")
    assert r["loop_"] == [True] and r["guard"] == [True] and r["flip"] == [True] and r["bare"] == [False]


def test_a_write_to_the_index_ends_the_fact():
    r = run("""fn f(v: &vector<u32>, i: usize) -> u32 { let mut k: usize = i; if k < v.len() { k += 1; return *v[k]; } 0 }
fn g(v: &vector<u32>, i: usize) -> u32 { let mut k: usize = i; if k < v.len() { return *v[k]; } 0 }""")
    assert r["f"] == [False] and r["g"] == [True]


def test_a_mutable_borrow_or_write_ends_the_fact():
    r = run("""fn push(v: &mut vector<u32>, i: usize) -> u32 { if i < v.len() { v.try_push(1); return *v[i]; } 0 }
fn keep(v: &mut vector<u32>, i: usize) -> u32 { if i < v.len() { return *v[i]; } 0 }
fn clob(o: &mut optional<u32>) -> u32 { if o.has_value() { *o = optional::none(); return *o.value(); } 0 }
fn store(o: optional<u32>, p: optional<u32>) -> u32 { let mut q = o; if q.has_value() { q = p; return *q.value(); } 0 }""")
    assert r["push"] == [False] and r["keep"] == [True]
    assert r["clob"] == [False] and r["store"] == [False]


def test_signed_index_is_not_proved_from_a_bound_above():
    r = run("fn f(v: &vector<u32>, i: i32) -> u32 { if (i as usize) < v.len() { return *v[i as usize]; } 0 }\n"
            "fn g(a: [u32; 4], i: i32) -> u32 { a[i as usize] }")
    assert r["g"] == [False]


def test_non_empty_guard_proves_index_zero():
    r = run("""fn f(v: &vector<u32>) -> u32 { if v.is_empty() { 0 } else { *v[0] } }
fn g(v: &vector<u32>) -> u32 { if v.len() > 2 { *v[2] + *v[0] } else { *v[0] } }""")
    assert r["f"] == [True] and r["g"] == [True, True, False]


def test_ranges():
    r = run("""fn f(a: &[u32]) -> usize { let s = &a[..]; let t = &a[0..a.len()]; s.len() + t.len() }
fn g(a: &[u32], i: usize) -> usize { let s = &a[0..i]; s.len() }
fn h(a: &[u32], i: usize) -> usize { if i < a.len() { let s = &a[0..i]; return s.len(); } 0 }
fn k(a: [u32; 4]) -> usize { let s = &a[1..3]; let t = &a[2..5]; s.len() + t.len() }""")
    assert r["f"] == [True, True] and r["g"] == [False] and r["h"] == [True] and r["k"] == [True, False]


def test_prove_is_idempotent_and_keeps_existing_proofs():
    m = symbols.analyze("/t/main.kif", "fn f(a: [u32; 4]) -> u32 { a[0] + a[9] }", None)
    mod = ir.build_module(m.tree)
    assert prove.prove_module(mod) == 1 and prove.prove_module(mod) == 0
    f = mod.functions[0]
    next(c for _, c in f.checks()).proof = "supplied by someone else"
    prove.prove_module(mod)
    assert next(c for _, c in f.checks()).proof == "supplied by someone else"


def test_constant_slots():
    r = run("""fn a(v: [u32; 4]) -> u32 { let n: usize = 3; v[n] }
fn b(v: [u32; 4]) -> u32 { let n: usize = 4; v[n] }
fn c(v: [u32; 4]) -> u32 { let n: usize = 1; v[n + 2] }
fn d(v: [u32; 4]) -> u32 { let mut n: usize = 1; n += 5; v[n] }
fn e(v: [u32; 4], k: usize) -> u32 { let mut n: usize = 1; if k > 2 { n = k; } v[n] }
fn f(v: [u32; 4]) -> u32 { let mut n: usize = 0; let mut t: u32 = 0; while n < 3 { t += v[n]; n += 1; } t }""")
    assert r["a"] == [True] and r["b"] == [False] and r["c"] == [True]
    assert r["d"] == [False]            # 6 is out of range
    assert r["e"] == [False]            # n is 1 or k: not a constant at the join
    assert r["f"] == [True]             # the loop counter is not a constant, but n < 3 holds in the body


def test_for_over_a_range_bounds_the_variable():
    r = run("""fn a(v: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..v.len() { t += v[i]; } t }
fn b(v: [u32; 4]) -> u32 { let mut t: u32 = 0; for i in 0..4 { t += v[i]; } t }
fn c(v: [u32; 4]) -> u32 { let mut t: u32 = 0; for i in 0..5 { t += v[i]; } t }
fn d(v: &[u32], w: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..v.len() { t += w[i]; } t }
fn e(v: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..v.len() { if t > 9 { continue; } t += v[i]; } t }
fn f(v: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..v.len() { t += v[i + 1]; } t }""")
    assert r["a"] == [True] and r["b"] == [True] and r["c"] == [False] and r["e"] == [True]
    assert r["d"] == [False]            # the bound is another slice
    assert r["f"] == [False]            # i + 1 can be len


def test_offset_bounds():
    r = run("""fn a(v: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..v.len() - 1 { t += v[i] + v[i + 1]; } t }
fn b(v: &[u32], i: usize) -> u32 { if i + 1 < v.len() { v[i + 1] + v[i] } else { 0 } }
fn c(v: &[u32], i: usize) -> u32 { if i + 1 <= v.len() { v[i + 1] } else { 0 } }
fn d(v: &[u32], i: usize) -> u32 { if i > 0 && i <= v.len() { v[i - 1] } else { 0 } }
fn e(v: &[u32]) -> u32 { if !v.is_empty() { v[v.len() - 1] } else { 0 } }
fn f(v: &[u32], i: usize) -> u32 { if i < v.len() { v[i + 1] } else { 0 } }""")
    assert r["a"] == [True, True] and r["b"] == [True, True]
    assert r["c"] == [False]            # i + 1 <= len allows i + 1 == len
    assert r["d"] == [True]
    assert r["e"] == [True]
    assert r["f"] == [False]


def test_a_write_to_the_bound_or_the_counter_forgets():
    r = run("""fn a(v: &mut vector<u32>) -> u32 { let mut t: u32 = 0; for i in 0..v.len() { v.clear(); t += v[i]; } t }
fn b(v: &[u32], n: usize) -> u32 { let mut i: usize = 0; let mut t: u32 = 0; while i < v.len() { i += 1; t += v[i]; } t }""")
    assert r["a"] == [False] and r["b"] == [False]


def test_range_checks():
    r = run("""fn a(v: &[u32], i: usize) -> usize { if i < v.len() { let s: &[u32] = v[i..]; s.len() } else { 0 } }
fn b(v: &[u32], i: usize) -> usize { if i < v.len() { let s: &[u32] = v[0..i]; s.len() } else { 0 } }
fn c(v: &[u32], i: usize) -> usize { if i < v.len() { let s: &[u32] = v[i..i + 1]; s.len() } else { 0 } }
fn d(v: &[u32], i: usize) -> usize { let s: &[u32] = v[0..i]; s.len() }
fn e(v: &[u32], i: usize) -> usize { if i < v.len() { let s: &[u32] = v[i + 1..i]; s.len() } else { 0 } }""")
    assert r["a"] == [True] and r["b"] == [True] and r["c"] == [True]
    assert r["d"] == [False] and r["e"] == [False]


def test_conditions_with_and_or_not_guard_each_part():
    r = run("""fn a(v: &[u32], i: usize) -> u32 { if i < v.len() && v[i] > 3 { v[i] } else { 0 } }
fn b(v: &[u32], i: usize) -> u32 { if i >= v.len() || v[i] == 0 { 0 } else { v[i] } }
fn c(v: &[u32], i: usize) -> u32 { if !(i < v.len()) { 0 } else { v[i] } }
fn d(v: &[u32], i: usize) -> u32 { if i < v.len() || v.len() == 0 { v[i] } else { 0 } }
fn e(v: &[u32], i: usize) -> u32 { if v[i] > 3 && i < v.len() { 1 } else { 0 } }""")
    assert r["a"] == [True, True] and r["b"] == [True, True] and r["c"] == [True]
    assert r["d"] == [False]            # either side may have held
    assert r["e"] == [False]            # the check comes before the guard


def test_division_checks():
    r = run("""fn k(x: u32) -> u32 { x / 4 + x % 8 }
fn z(x: u32) -> u32 { x / 0 }
fn m1(x: i32) -> i32 { x / -1 }
fn nz(x: u32, d: u32) -> u32 { if d != 0 { x / d } else { 0 } }
fn snz(x: i32, d: i32) -> i32 { if d != 0 { x / d } else { 0 } }
fn spos(x: i32, d: i32) -> i32 { if d > 0 { x / d } else { 0 } }
fn ge1(x: i32, d: i32) -> i32 { if d >= 1 { x % d } else { 0 } }
fn ge0(x: i32, d: i32) -> i32 { if d >= 0 { x / d } else { 0 } }
fn bare(x: u32, d: u32) -> u32 { x / d }
fn again(x: u32, d: u32) -> u32 { x / d / d }
fn sagain(x: i32, d: i32) -> i32 { x / d / d }
fn wrong(x: u32, d: u32) -> u32 { if d == 0 { x / d } else { 0 } }
fn after_write(x: u32, d: u32) -> u32 { let mut e = d; if e != 0 { e = 0; return x / e; } 0 }""")
    assert r["k"] == [True, True] and r["z"] == [False] and r["m1"] == [False]
    assert r["nz"] == [True]
    assert r["snz"] == [False]  # a signed divisor can still be -1
    assert r["spos"] == [True] and r["ge1"] == [True] and r["ge0"] == [False]
    assert r["bare"] == [False] and r["again"] == [False, True] and r["sagain"] == [False, False]
    assert r["wrong"] == [False] and r["after_write"] == [False]


def test_shift_checks():
    r = run("""fn k(x: u32) -> u32 { x << 3 }
fn big(x: u32) -> u32 { x << 32 }
fn neg(x: i32) -> i32 { x << -1 }
fn m(x: u32, n: u32) -> u32 { x >> (n & 31) }
fn m32(x: u32, n: u32) -> u32 { x >> (n & 32) }
fn r(x: u32, n: u32) -> u32 { x << (n % 32) }
fn sg(x: u32, n: i32) -> u32 { x << (n & 15) }
fn sr(x: u32, n: i32) -> u32 { x << (n % 32) }
fn bare(x: u32, n: u32) -> u32 { x << n }
fn cmp(x: u32, n: u32) -> u32 { if n < 32 { x << n } else { 0 } }
fn le(x: u8, n: u32) -> u8 { if n <= 7 { x << n } else { 0 } }
fn wide(x: u8, n: u32) -> u8 { if n <= 8 { x << n } else { 0 } }
fn twice(x: u32, n: u32) -> u32 { (x << n) >> n }
fn slot(x: u32, n: u32) -> u32 { let s = n & 7; x << s }
fn written(x: u32, n: u32) -> u32 { let mut s = n & 7; s = n; x << s }""")
    assert r["k"] == [True] and r["big"] == [False] and r["neg"] == [False]
    assert r["m"] == [True] and r["m32"] == [False] and r["r"] == [True, True]
    assert r["sg"] == [True] and r["sr"] == [True, False]  # a signed % can be negative: the division is fine, the shift amount is not bounded below
    assert r["bare"] == [False] and r["cmp"] == [True]
    assert r["le"] == [True] and r["wide"] == [False]  # the width of the shifted type counts
    assert r["twice"] == [False, True] and r["slot"] == [True] and r["written"] == [False]


def test_masked_and_reduced_indexes():
    r = run("""fn a(t: [u32; 8], h: usize) -> u32 { t[h & 7] }
fn b(t: [u32; 8], h: usize) -> u32 { t[h % 8] }
fn c(t: [u32; 8], h: usize) -> u32 { t[h & 15] }
fn d(t: [u32; 8], h: usize) -> u32 { t[(h & 3) + 4] }
fn e(t: [u32; 8], h: usize) -> u32 { t[(h & 7) + 1] }
fn f(t: [u32; 8], h: usize) -> u32 { let k = h & 7; t[k] }
fn g(t: [u32; 8], h: usize) -> u32 { let mut k = h & 7; k = h; t[k] }
fn s(t: [u32; 8], h: i64) -> u32 { t[(h % 8) as usize] }""")
    assert r["a"] == [True] and r["b"] == [True, True] and r["c"] == [False]
    assert r["d"] == [True] and r["e"] == [False] and r["f"] == [True] and r["g"] == [False]


FIELDS = """struct s { a: [u32; 4], n: usize, o: optional<u32>, p: pair }
struct pair { x: usize, y: usize }
fn bump(x: &mut s) { x.n = 100; }
fn setn(n: &mut usize) { *n = 100; }
"""


def runf(body):
    return run(FIELDS + body)


def test_facts_about_fields_of_a_reference():
    r = runf("""fn arr(x: &s, i: usize) -> u32 { if i < 4 { x.a[i] + x.a[i] } else { 0 } }
fn opt(x: &s) -> u32 { if x.o.has_value() { *x.o.value() } else { 0 } }
fn len(x: &s, v: &[u32]) -> u32 { if x.n < v.len() { v[x.n] + v[x.n] } else { 0 } }
fn deep(x: &s, v: &[u32]) -> u32 { if x.p.y < v.len() { v[x.p.y] } else { 0 } }
fn other(x: &s, v: &[u32]) -> u32 { if x.p.x < v.len() { v[x.p.y] } else { 0 } }
fn two(x: &s, y: &s, v: &[u32]) -> u32 { if x.n < v.len() { v[y.n] } else { 0 } }
fn self_len(x: &s) -> u32 { x.a[x.n] }""")
    assert r["arr"] == [True, True] and r["opt"] == [True] and r["len"] == [True, True] and r["deep"] == [True]
    assert r["other"] == [False] and r["two"] == [False] and r["self_len"] == [False]


def test_a_write_to_the_place_forgets_its_fields():
    r = runf("""fn assign(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { x.n = 100; v[x.n] } else { 0 } }
fn whole(x: &mut s, y: s, v: &[u32]) -> u32 { if x.n < v.len() { *x = y; v[x.n] } else { 0 } }
fn call(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { bump(x); v[x.n] } else { 0 } }
fn field_ref(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { setn(&mut x.n); v[x.n] } else { 0 } }
fn sibling(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { x.p.x = 7; v[x.n] } else { 0 } }
fn opt(x: &mut s) -> u32 { if x.o.has_value() { x.o = none; *x.o.value() } else { 0 } }
fn root(x: &s, y: &s, v: &[u32]) -> u32 { let mut r = x; if r.n < v.len() { r = y; v[r.n] } else { 0 } }
fn local(v: &[u32]) -> u32 { let mut st = pair { x: 0, y: 0 }; if st.x < v.len() { st.x = 50; v[st.x] } else { 0 } }""")
    assert r["assign"] == [False] and r["whole"] == [False] and r["call"] == [False] and r["field_ref"] == [False]
    assert r["opt"] == [False] and r["root"] == [False] and r["local"] == [False]
    # a write to a sibling still clobbers the root: sound, if cautious
    assert r["sibling"] == [False]


def test_other_ways_to_write_through_a_reference_forget_fields():
    r = runf("""impl s { fn reset(&mut self) { self.n = 100; } fn get(&self) -> usize { self.n } }
fn m1(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { x.reset(); v[x.n] } else { 0 } }
fn m2(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { let r = &mut *x; bump(r); v[x.n] } else { 0 } }
fn m3(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { let r = &mut x.n; *r = 100; v[x.n] } else { 0 } }
fn m4(x: &mut s, v: &[u32]) -> u32 { let mut i: usize = 0; while i < 3 { if x.n < v.len() { x.n += 1; } i += 1; } v[x.n] }
fn m5(x: &mut s, v: &[u32]) -> u32 { if x.n < v.len() { x.n += 1; v[x.n] } else { 0 } }
fn shared(x: &s, v: &[u32]) -> u32 { if x.n < v.len() { let k = x.get(); v[x.n] + v[x.n] } else { 0 } }""")
    for name in ("m1", "m2", "m3", "m4", "m5"):
        assert not any(r[name]), name
    assert r["shared"] == [True, True]  # a shared reference cannot be written through


def test_match_on_an_optional_proves_some_in_the_arms():
    r = run("""fn a(o: optional<u32>) -> u32 { match o { Some(v) => v + *o.value(), None => 0 } }
fn b(o: optional<u32>) -> u32 { match o { None => *o.value(), Some(_) => 1 } }
fn c(o: optional<u32>) -> u32 { match o { None => 0, _ => *o.value() } }
fn d(o: optional<u32>) -> u32 { match o { Some(_) => 1, None => 2 } }""")
    assert r["a"] == [True] and r["b"] == [False] and r["c"] == [True] and r["d"] == []


def test_equal_lengths_carry_index_proofs_across_slices():
    r = run("""fn cp(a: &[u32], b: &mut [u32]) { if a.len() == b.len() { let mut i: usize = 0; while i < a.len() { b[i] = a[i]; i += 1; } } }
fn first(a: &[u32], b: &[u32]) -> u32 { if a.len() == b.len() && a.len() > 0 { a[0] + b[0] } else { 0 } }
fn ne(a: &[u32], b: &[u32]) -> u32 { if a.len() != b.len() { return 0; } if a.len() > 0 { a[0] + b[0] } else { 0 } }
fn lt(a: &[u32], b: &[u32]) -> u32 { if a.len() <= b.len() && a.len() > 0 { a[0] + b[0] } else { 0 } }
fn none(a: &[u32], b: &[u32], i: usize) -> u32 { if i < a.len() { a[i] + b[i] } else { 0 } }
fn moved(a: &[u32], b: &[u32], c: &[u32]) -> u32 { let mut x = b; if a.len() == x.len() && a.len() > 0 { x = c; a[0] + x[0] } else { 0 } }""")
    assert r["cp"] == [True, True] and r["first"] == [True, True] and r["ne"] == [True, True]
    assert r["lt"] == [True, False] and r["none"] == [True, False] and r["moved"] == [True, False]


def test_last_element_needs_a_non_empty_proof_because_subtraction_wraps():
    r = run("""fn last(s: &[u32]) -> u32 { if !s.is_empty() { s[s.len() - 1] } else { 0 } }
fn last2(s: &[u32]) -> u32 { if s.len() > 0 { s[s.len() - 1] } else { 0 } }
fn last3(s: &[u32]) -> u32 { if s.len() >= 2 { s[s.len() - 2] + s[s.len() - 1] } else { 0 } }
fn bad(s: &[u32]) -> u32 { s[s.len() - 1] }
fn bad2(s: &[u32]) -> u32 { if s.len() >= 1 { s[s.len() - 2] } else { 0 } }
fn head(s: &[u32]) -> usize { if s.len() > 0 { let t = &s[..s.len() - 1]; t.len() } else { 0 } }
fn bad3(s: &[u32]) -> usize { let t = &s[..s.len() - 1]; t.len() }""")
    assert r["last"] == [True] and r["last2"] == [True] and r["last3"] == [True, True]
    assert r["bad"] == [False] and r["bad2"] == [False]
    assert r["head"] == [True] and r["bad3"] == [False]


def test_ranges_through_add_mul_shift_and_constant_minus():
    r = run("""fn a(t: [u8; 8], x: usize) -> u8 { t[(x & 3) * 2] }
fn b(t: [u8; 8], x: usize) -> u8 { t[(x % 4) + (x & 3)] }
fn c(t: [u8; 8], x: usize) -> u8 { t[(x & 7) >> 1] }
fn e(t: [u8; 8], x: usize) -> u8 { if x < 4 { t[x * 2 + 1] } else { 0 } }
fn g(t: [u8; 8], x: usize) -> u8 { let n = x % 8; t[7 - n] }
fn bad1(t: [u8; 8], x: usize) -> u8 { t[(x & 3) * 3] }
fn bad2(t: [u8; 8], x: usize) -> u8 { t[(x & 7) + 1] }
fn bad3(t: [u8; 8], x: usize) -> u8 { let n = x % 9; t[7 - n] }""")
    for f in "abceg":
        assert r[f][-1] is True, f
    for f in ("bad1", "bad2", "bad3"):
        assert r[f][-1] is False, f
