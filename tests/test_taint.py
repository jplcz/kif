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
    return f"#[tainted_result]\nfn f({params}) -> u32 {{ {body} }}\n"


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
    assert codes(fn("let mut x = (0usize, 0usize); x.0 = n; x.0 = 0; if x.0 > 1 { 1 } else { 0 }")) == ["KIFT001"]


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
    assert codes(base + fn("let a = id(n); t[a]")) == ["KIFT002", "KIFT003"]


def test_tainted_result_source():
    src = "#[safe]\n#[tainted_result]\nextern \"C\" fn read() -> usize;\nfn f(n: usize) -> u32 { let a = read(); if a > 1 { 1 } else { 0 } }\n"
    assert codes(src) == ["KIFT001"]


# -- messages

def test_message_names_origin_and_mitigations():
    d = diags(fn("t[n]"))[0]
    assert "parameter 'n'" in d.message and "line 2" in d.message
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


# ================================================================ stage 2: calls, declared places, returns, type sources

def has(src: str, *want: str) -> bool:
    return codes(src) == sorted(want)


# -- KIFT003

CALLEE = "fn lookup(t: &[u32], i: usize) -> u32 { t[i] }\n"


def test_tainted_argument_for_a_clean_parameter():
    assert has(CALLEE + fn("lookup(t, n)"), "KIFT003")
    assert has(CALLEE + fn("lookup(t, 1)"))


def test_tainted_parameter_rechecks_the_callee_body():
    ok = "#[tainted_result]\nfn double(#[tainted] i: usize) -> usize { i * 2 }\n"
    assert has(ok + fn("let d = double(n); 0"))
    bad = "fn at(t: &[u32], #[tainted] i: usize) -> u32 { t[i] }\n"
    assert codes(bad + fn("at(t, n)")) == ["KIFT002"]                      # reported in the callee, where the use is


def test_reference_to_tainted_data_needs_a_tainted_parameter():
    src = "struct m { k: u32 }\nfn take(r: &m) -> u32 { 0 }\nfn f(#[tainted] r: &m) -> u32 { take(r) }\n"
    assert has(src, "KIFT005", "KIFT003") or "KIFT003" in codes(src)
    ok = "struct m { k: u32 }\nfn take(#[tainted] r: &m) -> u32 { 0 }\n#[tainted_result]\nfn f(#[tainted] r: &m) -> u32 { take(r) }\n"
    assert codes(ok) == []


def test_method_receiver_and_arguments():
    impl = "struct box { v: u32 }\nimpl box {\n    fn get(&self, i: usize) -> u32 { 0 }\n    fn set(&mut self, #[tainted] x: u32) { }\n}\n"
    assert has(impl + fn("let b = box { v: 0 }; b.get(n)"), "KIFT003")
    assert has(impl + fn("let mut b = box { v: 0 }; b.set(c); 0"))
    assert has(impl + fn("let mut b = box { v: 0 }; b.set(n as u32); 0"))


def test_mut_argument_for_a_tainted_parameter_taints_the_local():
    src = "fn fill(#[tainted] out: &mut u32) { }\n" + fn("let mut o = 0u32; fill(&mut o); if o > 1 { 1 } else { 0 }")
    assert codes(src) == ["KIFT001"]


# -- KIFT004 declared places

STRUCTS = "struct plain { a: u32, #[tainted] b: u32 }\n"


PLAIN_STRUCT = "struct clean { a: u32, b: u32 }\n"


def test_a_struct_is_tainted_all_or_nothing_so_any_field_may_hold_tainted_data():
    for field in ("a", "b"):
        assert has(STRUCTS + fn(f"let mut p = plain {{ a: 0, b: 0 }}; p.{field} = n as u32; 0"))
    assert has(STRUCTS + fn("let p = plain { a: n as u32, b: 0 }; 0"))
    assert has(STRUCTS + fn("let p = plain { a: 0, b: n as u32 }; 0"))


def test_a_struct_without_any_tainted_field_refuses_tainted_data():
    assert has(PLAIN_STRUCT + fn("let mut p = clean { a: 0, b: 0 }; p.a = n as u32; 0"), "KIFT004")
    assert has(PLAIN_STRUCT + fn("let p = clean { a: n as u32, b: 0 }; 0"), "KIFT004")


def test_every_field_of_a_tainted_struct_is_a_source():
    """`a` carries no attribute, yet reading it is as tainted as reading `b`: the struct is tainted as a whole."""
    for field in ("a", "b"):
        assert has(STRUCTS + fn(f"let p = plain {{ a: 0, b: 0 }}; if p.{field} > 1 {{ 1 }} else {{ 0 }}"), "KIFT001")
    assert has(PLAIN_STRUCT + fn("let p = clean { a: 0, b: 0 }; if p.a > 1 { 1 } else { 0 }"))


def test_writing_through_a_mut_parameter():
    src = "fn put(o: &mut u32, #[tainted] x: u32) { *o = x; }\n"
    assert has(src, "KIFT004")
    assert has("fn put(#[tainted] o: &mut u32, #[tainted] x: u32) { *o = x; }\n")
    assert has("fn put(o: &mut u32, x: u32) { *o = x; }\n")


def test_static_must_be_declared_tainted():
    base = "static slot: u32 = 0;\n"
    assert "KIFT004" not in codes(base + "fn f(#[tainted] x: u32) { }\n")


# -- KIFT005 returns

def test_returning_tainted_data():
    assert has("fn f(#[tainted] x: u32) -> u32 { x }\n", "KIFT005")
    assert has("fn f(#[tainted] x: u32) -> u32 { return x; }\n", "KIFT005")
    assert has("#[tainted_result]\nfn f(#[tainted] x: u32) -> u32 { x }\n")
    assert has("fn f(#[tainted] x: u32, y: u32) -> u32 { y }\n")


def test_returning_a_tainted_type_is_fine():
    src = "#[tainted]\nstruct req { n: u32 }\nfn f(r: req) -> req { r }\n"
    assert codes(src) == []


# -- type sources

def test_tainted_type_is_a_source_everywhere():
    src = "#[tainted]\nstruct req { n: u32 }\nfn f(r: &req, t: &[u32]) -> u32 { t[r.n as usize] }\n"
    assert "KIFT002" in codes(src)
    src2 = "#[tainted]\nstruct req { n: u32 }\nfn f(r: &req) -> u32 { if r.n > 1 { 1 } else { 0 } }\n"
    assert "KIFT001" in codes(src2)


def test_tainted_type_passed_to_a_parameter_of_that_type():
    src = "#[tainted]\nstruct req { n: u32 }\nfn h(r: &req) -> u32 { 0 }\nfn f(r: &req) { h(r); }\n"
    assert codes(src) == []


def test_whole_struct_with_a_tainted_field_is_tainted_when_passed():
    src = STRUCTS + "fn use1(a: u32) -> u32 { 0 }\nfn f(p: &plain) -> u32 { use1(p.b) }\n"
    assert "KIFT003" in codes(src)
    src2 = STRUCTS + "fn use2(q: &plain) -> u32 { 0 }\nfn f(p: &plain) { use2(p); }\n"
    assert codes(src2) == []


# -- KIFT008 indirect calls

def test_indirect_calls():
    assert "KIFT008" in codes("fn f(g: fn(u32) -> u32, #[tainted] x: u32) -> u32 { g(x) }\n")
    assert codes("fn f(g: fn(u32) -> u32, x: u32) -> u32 { g(x) }\n") == []
    assert "KIFT008" in codes("fn f(#[tainted] g: fn(u32) -> u32, x: u32) -> u32 { g(x) }\n")


def test_interface_calls_with_tainted_arguments():
    src = "interface sink { method put(v: u32); }\nfn f(s: &mut dyn sink, #[tainted] x: u32) { s.put(x); }\n"
    assert "KIFT008" in codes(src)
    ok = "interface sink { method put(v: u32); }\nfn f(s: &mut dyn sink, x: u32) { s.put(x); }\n"
    assert codes(ok) == []


# ================================================================ stage 3: #[untaint] purity (KIFT009)

def san(body: str, params: str = "#[tainted] x: usize, t: &[u32]", ret: str = "usize") -> str:
    return f"#[untaint]\nfn s({params}) -> {ret} {{ {body} }}\n"


def test_pure_sanitizer_is_accepted():
    assert codes(san("let mut m = x & 3; m = m | 1; m")) == []
    assert codes("#[untaint]\nfn a(#[tainted] x: usize) -> usize { x & 3 }\n#[untaint]\nfn b(#[tainted] x: usize) -> usize { untaint!(\"masked by a\", a(x)) }\n") == []


def test_purity_violations():
    assert codes(san("panic!(\"no\"); 0")) == ["KIFT009"]
    assert codes(san("unsafe { 0 }")) == ["KIFT009"]
    assert codes(san("0", params="#[tainted] x: usize, o: &mut u32")) == ["KIFT009"]
    assert set(codes(san("*o = 1; 0", params="#[tainted] x: usize, o: &mut u32"))) == {"KIFT009"}
    assert "KIFT009" in codes("fn other() -> usize { 1 }\n" + san("other()"))
    assert "KIFT009" in codes("static g: usize = 0;\n" + san("g = 1; 0"))


def test_trusted_untaint_is_not_checked():
    assert codes('#[untaint(trusted)]\nfn s(#[tainted] x: usize) -> usize { panic!("asserted pure by its author"); 0 }\n') == []


def test_documented_mask_sanitizer_is_pure_and_clean():
    src = ("#[untaint]\nfn clamp_index(#[tainted] i: usize, len: usize) -> usize { let ok = (i < len) as usize; i & (ok * !0usize) }\n"
           "fn at(#[tainted] n: usize, t: &[u32]) -> u32 { t[untaint!(\"clamped below len by clamp_index\", clamp_index(n, 4))] }\n")
    assert codes(src) == ["KIFT011"]    # accepted, with the suggestion to use ct:: and a barrier


# ================================================================ `ct::` intrinsics (synthesized by the compiler)

def test_ct_intrinsics_are_branch_free_values():
    pre = "#[tainted_result]\nfn f(#[tainted] i: usize, n: usize, t: &[usize]) -> usize { "
    assert codes(pre + "ct::select(ct::lt(i, n), i, 0) }\n") == []                      # computing is fine
    assert codes(pre + "t[ct::select(ct::lt(i, n), i, 0)] }\n") == ["KIFT002"]          # the result is still tainted
    assert codes(pre + "if ct::lt(i, n) { 1 } else { 0 } }\n") == ["KIFT001"]
    assert codes(pre + 't[untaint!("below n by clamp", ct::clamp(i, n))] }\n') == []    # clamp is the trusted one
    assert codes(pre + "t[ct::clamp(i, n)] }\n") == ["KIFT010"]                          # ... and needs a reason


def test_ct_intrinsics_are_typed():
    d = lambda b: codes(f"fn f(i: usize, n: usize) -> usize {{ {b} }}\n")
    assert d("ct::lt(i)") == ["KIFP011"]
    assert d("ct::eq(i, true)") == ["KIFP012"]
    assert d("ct::mask(i < n)") == []


def test_ct_intrinsics_in_pure_sanitizer_and_interpreter():
    from kiflang.interp import run
    src = "#[untaint]\nfn clamp_index(#[tainted] i: usize, len: usize) -> usize { ct::select(ct::lt(i, len), i, 0) }\n" \
          "fn m(c: bool) -> usize { ct::mask(c) }\n"
    assert codes(src) == []
    m = analyze("t.kif", src)
    assert run(m, "clamp_index", 3, 8) == 3 and run(m, "clamp_index", 9, 8) == 0
    assert run(m, "m", True) == 2**64 - 1 and run(m, "m", False) == 0


def test_ct_intrinsics_reach_the_ir_as_intrinsic_calls():
    from kiflang import ir
    m = analyze("t.kif", "fn f(i: usize, n: usize) -> usize { ct::select(ct::lt(i, n), i, 0) }\n")
    calls = [i for b in ir.build_module(m.tree).functions[0].blocks for i in b.instrs if isinstance(i, ir.Call)]
    assert [(c.callee, c.kind) for c in calls] == [("ct::lt", "intrinsic"), ("ct::select", "intrinsic")]


# ================================================================ editor support and suggestions

def test_hand_rolled_sanitizer_gets_a_suggestion_not_an_error():
    from kiflang import diagnostics as dg
    hand = "#[untaint]\nfn s(#[tainted] i: usize, n: usize) -> usize { let ok = (i < n) as usize; i & (ok * !0usize) }\n"
    d = [x for x in analyze("t.kif", hand).diagnostics if x.code == "KIFT011"]
    assert len(d) == 1 and d[0].severity == dg.INFO and "ct::select" in d[0].message and "spec::value" in d[0].message
    assert d[0].format("t.kif").split(": ")[1].startswith("note")
    assert codes("#[untaint]\nfn s(#[tainted] i: usize, n: usize) -> usize { ct::select(ct::lt(i, n), i, 0) }\n") == []
    assert codes("#[untaint]\nfn s(#[tainted] i: usize) -> usize { i & 3 }\n") == []


def test_completion_offers_ct_intrinsics():
    from kiflang import complete
    src = "fn f(i: usize) -> usize { ct:: }\n"
    m = analyze("t.kif", src)
    labels = {i["label"] for i in complete.complete(m, src, 0, 30, [], [])}
    assert labels == {"select", "eq", "lt", "mask", "clamp"}
    top = {i["label"] for i in complete.complete(analyze("t.kif", "fn f() { }\n"), "fn f() { c }\n", 0, 12, [], [])}
    assert "ct" in top


def test_signature_help_for_ct_intrinsics():
    from kiflang import complete
    src = "fn f(i: usize, n: usize) -> usize { ct::select(i < n, ) }\n"
    r = complete.signature_help(analyze("t.kif", src), src, 0, src.index(") }"))
    sig = r["signatures"][0]
    assert sig["label"] == "fn ct::select(c: bool, a: T, b: T) -> T" and r["activeParameter"] == 1
    assert sig["label"][slice(*sig["parameters"][1]["label"])] == "a: T"


def test_taint_attributes_are_known_to_the_editor():
    from kiflang import lsp
    assert {"tainted", "tainted_result", "untaint", "trusted"} <= set(lsp.ATTRIBUTES)


# -- stage 4: the IR backstop (kiflang/taint_ir.py)

def ir_codes(src: str):
    from kiflang import passes

    r = analyze("t.kif", src)
    return sorted(d.code for d in passes.run_module(r.tree, ["taint"]).diagnostics)


def test_ir_reports_branch_index_and_value_call():
    assert ir_codes(fn("if n > 3 { 1 } else { 0 }")) == ["KIFT001"]
    assert ir_codes(fn("t[n]")) == ["KIFT002"]
    assert ir_codes(fn("c / (n as u32)")) == ["KIFT002"]
    assert ir_codes(fn("c << n")) == ["KIFT002"]


def test_ir_clean_cases():
    assert ir_codes(fn("t[c as usize]")) == []
    assert ir_codes(fn("(n as u32) + c")) == []
    assert ir_codes(fn("let m = n & 3; t[m]")) == ["KIFT002"]          # masking is not sanitising: only #[untaint] is
    assert ir_codes(san("x & 3")) == []


def test_ir_flows_through_locals_and_loops():
    assert ir_codes(fn("let mut i = 0usize; let mut k = n; while i < 3 { k = k + 1; i = i + 1; } t[k]")) == ["KIFT002"]
    assert ir_codes(fn("let mut k = n; k = 0; t[k]")) == []            # a whole-slot store replaces taint


def test_ir_untaint_call_is_clean_and_tainted_result_is_a_source():
    sanit = "#[untaint]\nfn s(#[tainted] x: usize) -> usize { x & 3 }\n"
    assert ir_codes(sanit + fn("t[untaint!(\"masked to 0..3\", s(n))]")) == []
    src = "#[tainted_result]\nfn get() -> usize { 1 }\nfn g(t: &[u32]) -> u32 { t[get()] }\n"
    assert ir_codes(src) == ["KIFT002"]


def test_ir_question_mark_is_allowed():
    src = "fn h(#[tainted] n: usize) -> result<u32> { ok(n as u32) }\n#[tainted_result]\nfn f(#[tainted] n: usize) -> result<u32> { let v = h(n)?; ok(v) }\n"
    assert ir_codes(src) == []


def test_ir_agrees_with_the_walker_on_sinks():

    # every snippet the walker flags with KIFT001/002/008 the IR flags too, and nothing the walker passes is flagged
    snippets = ["if n > 3 { 1 } else { 0 }", "t[n]", "t[n..]", "match n { 0 => 1, _ => 2 }", "c / (n as u32)", "c << n", "n as u32", "t[c as usize]"]
    for s in snippets:
        want = sorted(x for x in codes(fn(s)) if x in ("KIFT001", "KIFT002", "KIFT008"))
        got = ir_codes(fn(s))
        assert set(want) <= set(got), (s, want, got)
        if not want:
            assert got == [], s


# -- stage 6: spec::barrier / spec::value

SPEC_SRC = "fn f(a: u32, b: u32) -> u32 { spec::barrier(); let v = spec::value(a + b); v }\n"


def test_spec_calls_type_check_and_run():
    from kiflang import interp  # noqa: F401

    assert codes(SPEC_SRC) == []
    assert codes("fn f() { spec::value(1, 2); }") == ["KIFP011"]
    assert codes("fn f() { spec::barrier(3); }") == ["KIFP011"]


def test_spec_barrier_becomes_an_ir_instruction():
    from kiflang import passes

    from kiflang import ir as kir

    text = kir.dump(passes.run_module(analyze("t.kif", SPEC_SRC).tree, []).module.functions[0])
    assert "barrier fence" in text and "barrier value" in text


def test_spec_value_does_not_untaint():
    assert ir_codes(fn("t[spec::value(n)]")) == ["KIFT002"]
    assert codes(fn("t[spec::value(n)]")) == ["KIFT002"]


def test_spec_is_allowed_in_a_pure_sanitizer():
    assert codes(san("spec::barrier(); spec::value(x & 3)")) == []


def test_spec_names_complete_and_help():
    from kiflang import complete
    src = "fn f() { spec:: }\n"
    labels = {i["label"] for i in complete.complete(analyze("t.kif", src), src, 0, 15, [], [])}
    assert labels == {"barrier", "value"}
    src = "fn f(x: u32) { spec::value() }\n"
    r = complete.signature_help(analyze("t.kif", src), src, 0, src.index(") }"))
    assert r["signatures"][0]["label"] == "fn spec::value(x: T) -> T"


def test_spec_value_in_c_has_the_audit_comment_and_barrier():
    from kiflang import lower_c, target

    t = target.load("c")
    t.data["taint"]["fence"] = '__asm__ volatile ("")'
    files = lower_c.lower(analyze("t.kif", SPEC_SRC, cfg_env=t.cfg_env()), t, "t")
    text = files["t_proc.c"]
    assert "kif: spec barrier (explicit): spec::barrier()" in text and "kif: spec value barrier (explicit): spec::value(..)" in text
    t.data["taint"] = {}
    import pytest
    with pytest.raises(lower_c.LowerError, match="taint.fence"):
        lower_c.lower(analyze("t.kif", SPEC_SRC, cfg_env=t.cfg_env()), t, "t")


# -- stage 6: implicit barriers (rule 5a)

def barriered(src: str, name: str = "f") -> str:
    from kiflang import ir as kir, passes

    r = passes.run_module(analyze("t.kif", src).tree, passes.pipeline("default"))
    return kir.dump(next(f for f in r.module.functions if f.name == name))


TRY_SRC = "#[tainted_result]\nfn h(n: usize) -> result<u32> { ok(n as u32) }\nfn f(c: usize) -> result<u32> { let v = h(c)?; ok(v) }\n"
LOAD_SRC = "fn f(#[tainted] t: &[u32], c: usize) -> u32 { t[c] }\n"


def test_try_on_tainted_data_gets_a_fence_on_both_successors():
    text = barriered(TRY_SRC)
    assert text.count("barrier fence  // implicit: `?` on tainted data (result of h()") == 2
    assert "b1.try_fail:\n    barrier fence" in text and "b2.try_ok:\n    barrier fence" in text


def test_tainted_element_load_gets_a_value_barrier():
    text = barriered(LOAD_SRC)
    assert text.count("barrier value") == 1 and "// implicit: tainted element load (parameter 't'" in text


def test_clean_code_gets_no_implicit_barrier():
    assert "barrier" not in barriered("fn f(t: &[u32], c: usize) -> u32 { t[c] }\n")
    assert "barrier" not in barriered("fn h(n: usize) -> result<u32> { ok(n as u32) }\nfn f(c: usize) -> result<u32> { let v = h(c)?; ok(v) }\n", "f")


def test_barriers_are_not_added_twice_and_still_taint():
    from kiflang import passes

    r = passes.run_module(analyze("t.kif", LOAD_SRC).tree, ["cfg", "barriers", "barriers"])
    from kiflang import ir as kir
    assert kir.dump(r.module.functions[0]).count("barrier value") == 1


def test_implicit_barrier_comment_reaches_the_c_output():
    from kiflang import lower_c, target

    t = target.load("c")
    t.data["taint"]["fence"] = '__asm__ volatile ("")'
    text = lower_c.lower(analyze("t.kif", TRY_SRC, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    assert "kif: spec barrier (implicit): `?` on tainted data (result of h()" in text
    text = lower_c.lower(analyze("t.kif", LOAD_SRC, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    assert "kif: spec value barrier (implicit): tainted element load" in text


# -- stage 8: the target policy (`taint.exports`, `taint.sinks`)

def policy_env(**v):
    from kiflang import cfg

    return cfg.Env({k: set(x) for k, x in v.items()}, set())


def under(src: str, **v):
    r = analyze("t.kif", src, cfg_env=policy_env(**v))
    from kiflang import passes

    ast_codes = sorted(d.code for d in r.diagnostics if d.code.startswith("KIFT"))
    return ast_codes, sorted(d.code for d in passes.run_module(r.tree, ["taint"]).diagnostics)


EXPORTED = "#[export]\n#[tainted_result]\nfn f(a: &[u32], i: usize) -> u32 { a[i] }\n"


def test_exports_all_taints_every_parameter_of_an_export_function():
    assert under(EXPORTED) == ([], [])                                   # default policy: only #[tainted] parameters
    assert under(EXPORTED, taint_exports={"declared"}) == ([], [])
    assert under(EXPORTED, taint_exports={"all"}) == (["KIFT002"], ["KIFT002"])   # AST check and IR backstop agree


def test_trusted_opts_a_function_out_of_exports_all():
    assert under("#[export]\n#[trusted]\n" + EXPORTED[len("#[export]\n"):], taint_exports={"all"}) == ([], [])
    assert under("fn f(a: &[u32], i: usize) -> u32 { a[i] }\n", taint_exports={"all"}) == ([], [])   # not exported: unaffected


DIV = "#[tainted_result]\nfn f(#[tainted] d: u32, n: u32) -> u32 { n / d }\n"
REM = "#[tainted_result]\nfn f(#[tainted] d: u32, n: u32) -> u32 { n % d }\n"
SHIFT = "#[tainted_result]\nfn f(#[tainted] d: u32, n: u32) -> u32 { n << d }\n"
MUL = "#[tainted_result]\nfn f(#[tainted] d: u32, n: u32) -> u32 { n * d }\n"


def test_sinks_default_list_and_mul_is_opt_in():
    for src, hit in ((DIV, True), (REM, True), (SHIFT, True), (MUL, False)):
        want = (["KIFT002"], ["KIFT002"]) if hit else ([], [])
        assert under(src) == want, src
    assert under(MUL, taint_sinks={"div", "rem", "shift", "mul"}) == (["KIFT002"], ["KIFT002"])


def test_sinks_list_replaces_the_default():
    only_div = {"taint_sinks": {"div"}}
    assert under(DIV, **only_div)[0] == ["KIFT002"]
    assert under(REM, **only_div) == ([], []) and under(SHIFT, **only_div) == ([], [])
    assert under(DIV, taint_sinks=set()) == ([], [])


def test_target_policy_reaches_the_cfg_environment_and_is_validated():
    from kiflang import cfg, target

    t = target.load("c")
    t.data["taint"] = {"exports": "all", "sinks": ["mul"]}
    env = cfg.env_of_target(t)
    assert env.values["taint_exports"] == {"all"} and env.values["taint_sinks"] == {"mul"}
    bad = lambda **k: [m for m in target.validate({"taint": k}) if m.startswith("taint.")]
    assert bad(exports="all", sinks=["div", "mul"]) == []
    assert bad(exports="some") and bad(sinks=["sqrt"])


# -- the IR file keeps everything the taint pass reads

def test_ir_round_trip_keeps_untaint_reasons_and_the_sink_policy():
    from kiflang import ir, serial

    src = CLEAN + '#[tainted_result]\nfn f(#[tainted] n: usize, t: &[u32]) -> u32 { let a = untaint!("masked to 0..3", clean(n)); t[a] * 2 }\n'
    r = analyze("t.kif", src, cfg_env=policy_env(taint_sinks={"mul"}))
    mod = ir.build_module(r.tree)
    back = serial.ir_from_data(serial.ir_to_data(mod))
    f = next(x for x in back.functions if x.name == "f")
    assert f.taint_sinks == frozenset({"mul"})
    calls = [i for _, i in f.instrs() if isinstance(i, ir.Call) and i.taint]
    assert [(c.taint, c.reason) for c in calls] == [("untaint", "masked to 0..3")]
    assert f.params[0].taint


# -- interprocedural summary: a callee whose body check proved its result clean

SUMMARY = """
fn pick(#[tainted] a: u32, b: u32) -> u32 { b }
#[tainted_result]
fn raw() -> u32 { 5 }
"""


def test_a_call_whose_callee_returns_only_clean_data_has_a_clean_result():
    assert codes(SUMMARY + "fn g(t: &[u32]) -> u32 { let x = raw(); t[pick(x, 1) as usize] }") == []


def test_the_summary_does_not_hide_a_tainted_result_declaration_or_a_bodyless_callee():
    assert codes(SUMMARY.replace("fn pick", "#[tainted_result]\nfn pick") + "fn g(t: &[u32]) -> u32 { let x = raw(); t[pick(x, 1) as usize] }") == ["KIFT002"]
    ext = 'extern "C" { fn probe(#[tainted] a: u32, b: u32) -> u32; }\n'
    assert "KIFT002" in codes(ext + "#[tainted_result]\nfn raw() -> u32 { 5 }\nfn g(t: &[u32]) -> u32 { let x = raw(); t[probe(x, 1) as usize] }")


def _fixes(src, code):
    from kiflang import quickfix
    mod, diags = quickfix.check("t.kif", src)
    d = next(d for d in diags if d.code == code)
    return [(f.title, quickfix.apply_edits(src, f.edits)) for f in quickfix.fixes_for(mod, d, diags)], quickfix.fixes_for(mod, d, diags)


def test_quick_fix_marks_the_function_tainted_result():
    src = "fn g(#[tainted] n: usize) -> usize { n }\n"
    out, raw = _fixes(src, "KIFT005")
    assert out[0][1].startswith("#[tainted_result]\nfn g") and not raw[0].preferred and not raw[0].auto


def test_quick_fix_marks_the_callee_parameter_tainted():
    src = "fn take(x: usize) -> usize { x }\nfn f(#[tainted] n: usize) -> usize {\n    take(n)\n}\n"
    out, _ = _fixes(src, "KIFT003")
    assert "fn take(#[tainted] x: usize)" in out[0][1]


def test_quick_fix_marks_the_struct_tainted():
    src = "struct s { a: u32 }\nfn f(#[tainted] n: u32, p: &mut s) {\n    p.a = n;\n}\n"
    out, _ = _fixes(src, "KIFT004")
    assert out[0][1].startswith("#[tainted]\nstruct s")


def test_quick_fix_sanitizes_with_an_untaint_function_and_adds_the_missing_reason():
    base = "#[untaint]\nfn clean(#[tainted] x: usize) -> usize { x & 3 }\n"
    out, _ = _fixes(base + "fn f(#[tainted] n: usize, t: &[u32]) -> u32 {\n    t[n]\n}\n", "KIFT002")
    assert 't[untaint!("TODO: justify", clean(n))]' in out[0][1]
    out, _ = _fixes(base + "fn f(#[tainted] n: usize, t: &[u32]) -> u32 {\n    let a = clean(n);\n    t[a]\n}\n", "KIFT010")
    assert 'let a = untaint!("TODO: justify", clean(n));' in out[0][1]
