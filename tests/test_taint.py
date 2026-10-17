# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Taint, stage 1 (docs/kif-taint.md): attributes (KIFT007) and the sinks of locals and parameters (KIFT001, KIFT002)."""

from kiflang.symbols import analyze


def diags(src: str):
    return analyze("t.kif", src).diagnostics


def codes(src: str):
    return sorted(d.code for d in diags(src))


def fn(body: str, params: str = "#[tainted] n: usize, t: &[u32], c: u32") -> str:
    return f"fn f({params}) -> u32 {{ {body} }}\n"


# -- clean code is untouched

def test_no_source_means_no_taint():
    assert codes("fn f(n: usize, t: &[u32]) -> u32 { if n > 3 { t[n] } else { 0 } }") == []


def test_arithmetic_on_tainted_data_is_allowed():
    assert codes(fn("let x = n * 2 + 1; let y = x ^ 7; c + (y as u32)")) == []


def test_a_clean_decision_in_a_function_with_a_source_is_fine():
    assert codes(fn("if c > 3 { t[1] } else { 0 }")) == []


# -- KIFT001 decisions

def test_if_condition():
    assert codes(fn("if n > 3 { 1 } else { 0 }")) == ["KIFT001"]


def test_while_condition_and_for_bound():
    assert codes(fn("let mut i = 0; while i < n { i += 1; } 0")) == ["KIFT001"]
    assert codes(fn("let mut s = 0; for i in 0..n { s += 1; } s")) == ["KIFT001"]


def test_match_scrutinee_and_guard():
    assert codes(fn("match n { 0 => 1, _ => 2 }")) == ["KIFT001"]
    assert codes(fn("match c { 0 => 1, _ if n > 2 => 2, _ => 3 }")) == ["KIFT001"]


def test_short_circuit_operands():
    assert codes(fn("let b = c > 1 && n > 2; 0")) == ["KIFT001"]
    assert codes(fn("let b = n > 2 || c > 1; 0")) == ["KIFT001"]


# -- KIFT002 addresses and sizes

def test_index_and_slice_range():
    assert codes(fn("t[n]")) == ["KIFT002"]
    assert codes(fn("let s = &t[0..n]; 0")) == ["KIFT002"]


def test_divisor_and_shift_count():
    assert codes(fn("c / (n as u32)")) == ["KIFT002"]
    assert codes(fn("c % (n as u32)")) == ["KIFT002"]
    assert codes(fn("c << (n as u32)")) == ["KIFT002"]
    assert codes(fn("(n as u32) << 3")) == []       # a tainted dividend or shifted value is fine


def test_index_write_and_array_length():
    assert codes(fn("let mut a = [0u32; 4]; a[n] = 1; 0")) == ["KIFT002"]
    assert codes(fn("let a = [0u32; n]; 0")) == ["KIFT002"]


# -- propagation

def test_taint_flows_through_locals_casts_and_fields():
    assert codes(fn("let a = n; let b = (a + 1) as u32; if b > 1 { 1 } else { 0 }")) == ["KIFT001"]
    assert codes(fn("let p = (n, 1usize); if p.0 > 1 { 1 } else { 0 }")) == ["KIFT001"]


def test_whole_assignment_cleans_a_local_and_a_join_unions():
    assert codes(fn("let mut a = n; a = 0; if a > 1 { 1 } else { 0 }")) == []
    assert codes(fn("let mut a = 0usize; if c > 1 { a = n; } if a > 1 { 1 } else { 0 }")) == ["KIFT001"]


def test_field_write_is_a_weak_update():
    src = "struct p { a: usize, b: usize }\nfn f(#[tainted] n: usize) -> u32 { let mut x = p { a: 0, b: 0 }; x.a = n; x.a = 0; if x.b > 1 { 1 } else { 0 } }\n"
    assert codes(src) == ["KIFT001"]


def test_loop_carried_taint():
    assert codes(fn("let mut a = 0usize; let mut k = 0u32; while k < 3 { if a > 1 { k += 1; } a = n; k += 1; } 0")) == ["KIFT001"]


def test_shadowing_restores_the_outer_binding():
    assert codes(fn("let a = n; { let a = 0usize; if a > 1 { return 1; } } 0")) == []
    assert codes(fn("let a = 0usize; { let a = n; } if a > 1 { 1 } else { 0 }")) == []


def test_match_bindings_and_loop_variables_carry_taint():
    ds = diags(fn("let o = some(n); match o { Some(v) => { if v > 1 { 1 } else { 0 } } None => 0 }"))
    assert [d.code for d in ds] == ["KIFT001", "KIFT001"]
    assert "match" in ds[0].message and "branch condition" in ds[1].message     # the binding `v` is tainted too
    assert codes(fn("for i in [n, n] { if i > 1 { return 1; } } 0")) == ["KIFT001", "KIFT001"]   # the bound and the use of `i`


# -- references: the taint is the referent's

def test_reference_parameter_taints_the_contents_not_the_pointer():
    src = "struct m { k: u32 }\nfn f(#[tainted] r: &m, t: &[u32]) -> u32 { let p = r.k; t[1] }\n"
    assert codes(src) == []
    src2 = "struct m { k: u32 }\nfn f(#[tainted] r: &m) -> u32 { if r.k > 1 { 1 } else { 0 } }\n"
    assert codes(src2) == ["KIFT001"]


# -- calls (stage 1: result taint only)

def test_call_results_and_untaint():
    base = "#[untaint]\nfn clean(#[tainted] x: usize) -> usize { x & 3 }\nfn id(x: usize) -> usize { x }\n"
    assert codes(base + fn('let a = untaint!("masked to 0..3 by clean", clean(n)); t[a as usize]')) == []
    assert codes(base + fn("let a = id(n); t[a]")) == ["KIFT002"]


def test_tainted_result_source():
    src = "#[safe]\n#[tainted_result]\nextern \"C\" fn read() -> usize;\nfn f(n: usize) -> u32 { let a = read(); if a > 1 { 1 } else { 0 } }\n"
    assert codes(src) == ["KIFT001"]


# -- messages

def test_message_names_origin_and_mitigations():
    d = diags(fn("t[n]"))[0]
    assert "parameter 'n'" in d.message and "line 1" in d.message
    assert "mitigations:" in d.message and "#[untaint]" in d.message


def test_repeated_use_is_reported_once_per_site():
    assert codes(fn("let mut k = 0usize; while k < 2 { k += t[n] as usize; } 0")) == ["KIFT002"]


# -- attributes (KIFT007)

def test_attribute_validation():
    assert codes("#[tainted]\nstruct r { #[tainted] a: u32, b: u32 }\n") == []
    assert codes("#[tainted(x)]\nstruct r { a: u32 }\n") == ["KIFT007"]
    assert codes("#[untaint]\nstruct r { a: u32 }\n") == ["KIFT007"]
    assert codes("#[tainted]\nfn f(x: u32) {}\n") == ["KIFT007"]
    assert codes("fn f(#[untaint] x: u32) {}\n") == ["KIFT007"]
    assert codes("#[tainted_result]\nfn f() {}\n") == ["KIFT007"]
    assert codes("#[untaint]\nfn f(x: u32) -> u32 { x }\n") == ["KIFT007"]            # nothing to untaint
    assert codes("#[untaint(bogus)]\nfn f(#[tainted] x: u32) -> u32 { x }\n") == ["KIFT007"]
    assert codes("#[untaint]\n#[tainted_result]\nfn f(#[tainted] x: u32) -> u32 { x }\n") == ["KIFT007"]
    assert codes("#[trusted]\nfn f(x: u32) {}\n") == ["KIFT007"]                       # not an #[export] function
    assert codes("#[untaint(trusted)]\nextern \"C\" fn f(#[tainted] x: u32) -> u32;\n") == []


# -- untaint! carries the reason (KIFT010)

CLEAN = "#[untaint]\nfn clean(#[tainted] x: usize) -> usize { x & 3 }\n"


def test_untaint_call_needs_a_reason():
    ds = diags(CLEAN + fn("let a = clean(n); t[a]"))
    assert [d.code for d in ds] == ["KIFT010"] and "untaint!" in ds[0].message
    assert codes(CLEAN + fn('let a = untaint!("", clean(n)); t[a]')) == ["KIFT002", "KIFT010"]
    assert codes(CLEAN + fn("let a = untaint!(clean(n)); t[a]")) == ["KIFT002", "KIFT010"]   # no reason: not accepted as a sanitiser call
    assert codes(CLEAN + fn('let a = untaint!("why", n + 1); t[a]')) == ["KIFT002", "KIFT010"]


def test_untaint_macro_wraps_only_untaint_functions_and_keeps_the_type():
    other = "fn id(x: usize) -> usize { x }\n"
    assert "KIFT010" in codes(other + fn('let a = untaint!("why", id(n)); 0'))
    assert codes(CLEAN + fn('let a: usize = untaint!("only the low bits matter here", clean(n)); t[a]')) == []


def test_reason_is_printed_in_the_generated_c():
    from kiflang import lower_c, target
    t = target.load("c")
    src = CLEAN + "fn f(#[tainted] n: usize, t: &[u32]) -> u32 {\n    let a = untaint!(\"low bits only, see RFC 7\", clean(n));\n    t[a]\n}\n"
    m = analyze("t.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1 or "KIFT" in d.code], m.diagnostics
    out = lower_c.lower(m, t, "t")["t_proc.c"]
    assert "/* kif: untaint: clean(): low bits only, see RFC 7 (" in out
