# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Thread-local and per-CPU cells (docs section 4.17)."""

from tests.test_proc_symbols import analyze

CONN = ("struct c { x: u32 }\nfn make() -> result<c> { err(error::invalid) }\n"
        "#[tls] static C: slot<c> = slot::new(make);\n")
STATS = ("struct s { x: u64 }\nfn init() -> s { s { x: 1 } }\n#[per_cpu] static S: slot<s> = slot::new(init);\n")


def codes(text):
    return sorted(d.code for d in analyze(text).diagnostics)


def test_valid_cells():
    assert codes("#[tls] static E: i32 = 0;\nfn last() -> i32 { E.with(|e| *e) }") == []
    assert codes("#[per_cpu] static N: u64 = 0;\n#[atomic_only] fn f() { N.with_mut(|n| { *n = *n + 1; }); }") == []
    assert codes("const K: u32 = 4;\nenum e { a, b }\nstruct s { a: u32, b: bool, c: e }\n"
                 "#[tls] static X: s = s { a: K + 1, b: true, c: e::b };") == []
    assert codes(CONN + "fn g() -> result<u32> { C.try_with(|c| c.x) }\nfn h() -> optional<u32> { C.with_ready(|c| c.x) }") == []
    assert codes(STATS + "#[atomic_only] fn g() { S.with_mut(|s| { s.x = 2; }); }") == []


def test_reloco_types_that_are_zeroable():
    for ty in ("duration", "instant", "optional<u32>", "wrapping<u32>", "saturating<u8>", "checked_value<i32>"):
        assert codes(f"#[tls] static X: {ty} = 0;") != ["KIFP180"], ty
        assert "KIFP180" not in codes(f"#[tls] static X: {ty} = 0;"), ty
    for ty in ("error", "non_zero<u32>", "allocator_ref", "ptr<u8>", "str"):
        assert "KIFP180" in codes(f"#[tls] static X: {ty} = 0;"), ty


def test_static_needs_exactly_one_kind():
    assert codes("static X: i32 = 0;") == ["KIFP188"]
    assert codes("#[tls] #[per_cpu] static X: i32 = 0;") == ["KIFP188"]


def test_zeroable_and_constant_initialisers():
    assert codes("#[tls] static X: &u8 = 0;") == ["KIFP180"]
    assert codes("enum e { a = 1, b }\n#[tls] static X: e = e::a;") == ["KIFP180"]
    assert codes("#[tls] static X: vector<u8> = 0;") == ["KIFP180"]
    assert codes("struct s<'a> { r: &'a u8 }\n#[tls] static X: s = 0;") == ["KIFP180"]
    assert codes("fn f() -> i32 { 1 }\n#[tls] static X: i32 = f();") == ["KIFP181"]
    assert codes("#[tls] static X: i32 = true;") == ["KIFP012"]


def test_slot_init_function():
    assert codes("struct c { x: u32 }\nfn make() -> c { c { x: 1 } }\n#[tls] static C: slot<c> = slot::new(make);") == ["KIFP181"]
    assert codes("struct s { x: u64 }\nfn init() -> result<s> { err(error::invalid) }\n"
                 "#[per_cpu] static S: slot<s> = slot::new(init);") == ["KIFP181"]
    assert codes("struct c { x: u32 }\n#[tls] static C: slot<c> = 0;") == ["KIFP181"]
    assert codes("fn f(x: slot<u8>) {}") == ["KIFP187"]
    assert "KIFP187" in codes("#[tls] static X: [slot<u8>; 2] = 0;")


def test_per_cpu_has_no_drop_and_no_allocator():
    assert codes("struct s { x: u64 }\nimpl s { fn drop(&mut self) {} }\n" + STATS.split("\n", 1)[1]) == ["KIFP182"]
    assert codes("struct s { x: u64 }\nfn init() -> s { let v = vector::<u8>::try_create(4); s { x: 1 } }\n"
                 "#[per_cpu] static S: slot<s> = slot::new(init);") == ["KIFP186"]
    assert codes("struct s { x: u64 }\nfn grow() -> u64 { let v = vector::<u8>::try_create(4); 1 }\n"
                 "fn init() -> s { s { x: grow() } }\n#[per_cpu] static S: slot<s> = slot::new(init);") == ["KIFP186"]
    assert codes("struct s { x: u64 }\n#[might_sleep] fn init() -> s { s { x: 1 } }\n"
                 "#[per_cpu] static S: slot<s> = slot::new(init);") == ["KIFP110"]
    # TLS init may allocate, and a TLS value may have a drop
    assert codes("struct s { v: vector<u8> }\nimpl s { fn drop(&mut self) {} }\n"
                 "fn init() -> result<s> { err(error::invalid) }\n#[tls] static S: slot<s> = slot::new(init);") == []


def test_atomic_rules():
    assert codes("#[per_cpu] static N: u64 = 0;\nfn g() { N.with_mut(|n| { *n = 1; }); }") == ["KIFP111"]
    assert codes(CONN + "#[atomic_only] fn g() { C.try_with(|c| c.x); }") == ["KIFP110"]
    assert codes(CONN + "#[atomic_only] fn g() { C.with_ready(|c| c.x); }") == []
    assert codes("#[tls] static N: u32 = 1;\n#[atomic_entry] fn g() { N.with(|n| 1); }") == ["KIFP189"]
    assert codes("#[tls(atomic_ok)] static N: u32 = 1;\n#[atomic_entry] fn g() { N.with(|n| 1); }") == []
    assert codes("#[per_cpu] static N: u64 = 0;\n#[atomic_entry] fn irq() { N.with(|n| 1); }\n"
                 "#[atomic_only] fn g() { N.with_mut(|n| { *n = 1; }); }") == ["KIFP184"]


def test_accessor_shape():
    assert codes("struct s { x: u64 }\n#[tls] static N: s = s { x: 1 };\nfn g<'a>() -> &'a u64 { N.with(|n| &n.x) }") == ["KIFP185"]
    assert codes("#[tls] static N: u32 = 1;\nfn g() -> u32 { N }") == ["KIFP191"]
    assert codes("#[tls] static N: u32 = 1;\nfn g() { N.foo(|n| 1); }") == ["KIFP010"]
    assert codes("#[tls] static N: u32 = 1;\nfn g() { N.with(1); }") == ["KIFP011"]
    assert codes("#[tls] static N: u32 = 1;\ngen fn g() -> u32 { N.with(|n| *n) }") == ["KIFP190"]


def test_tls_drop_must_not_touch_tls():
    assert codes("struct s { x: u64 }\n#[tls] static A: u32 = 0;\nimpl s { fn drop(&mut self) { A.with(|a| 1); } }\n"
                 "fn init() -> result<s> { err(error::invalid) }\n#[tls] static S: slot<s> = slot::new(init);") == ["KIFP183"]


INIT = "fn init() -> result<s> { err(error::invalid) }\n"


def test_tls_drop_reaches_cell_through_calls_and_fields():
    # through a helper the drop calls
    assert codes("struct s { x: u64 }\n#[tls] static A: u32 = 0;\nfn peek() { A.with(|a| 1); }\n"
                 "impl s { fn drop(&mut self) { peek(); } }\n" + INIT + "#[tls] static S: slot<s> = slot::new(init);") == ["KIFP183"]
    # through the drop of a field: the container itself has no drop
    assert codes("struct inner { x: u64 }\nstruct s { i: inner }\n#[tls] static A: u32 = 0;\n"
                 "impl inner { fn drop(&mut self) { A.with(|a| 1); } }\n" + INIT +
                 "#[tls] static S: slot<s> = slot::new(init);") == ["KIFP183"]
    # a drop that touches no cell is fine
    assert codes("struct s { x: u64 }\n#[tls] static A: u32 = 0;\nimpl s { fn drop(&mut self) { } }\n" + INIT +
                 "#[tls] static S: slot<s> = slot::new(init);") == []


def test_reflection_lists_cells():
    from kiflang.interp import run
    from kiflang.reflect import module_handle
    from kiflang.symbols import analyze
    m = analyze("t.kif", """
        /// Counts interrupts.
        #[per_cpu] static irqs: u64 = 0;
        #[tls(atomic_ok)] static depth: u32 = 0;
        fn make() -> result<u32> { err(error::invalid) }
        #[tls] static cache: slot<u32> = slot::new(make);

        gen fn describe(m: &Module) -> str {
            let mut out = "";
            for c in m.cells() {
                out = out + format!("{} {} {} slot={} atomic_ok={} ty={} doc={:?}\\n", c.name(), c.kind(), c.ty().name(),
                                    c.is_slot(), c.is_atomic_ok(), c.value_ty().name(), c.doc());
            }
            out
        }
    """)
    assert m.diagnostics == []
    out = run(m, "describe", module_handle(m))
    assert 'irqs per_cpu u64 slot=false atomic_ok=false ty=u64 doc="Counts interrupts."' in out
    assert "depth tls u32 slot=false atomic_ok=true ty=u32" in out
    assert "cache tls slot<u32> slot=true atomic_ok=false ty=u32" in out
