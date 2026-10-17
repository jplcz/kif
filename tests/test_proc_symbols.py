# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Checker step 1: symbols, duplicates, type-name resolution."""

from kiflang import symbols
from kiflang.symbols import prelude


def analyze(text, files=None):
    """Analyse `text` as /t/main.kif; `files` maps import paths to source text."""
    files = files or {}
    return symbols.analyze("/t/main.kif", text, lambda _k, path: (("/t/" + path), files[path]) if path in files else None)


def codes(text):
    return [d.code for d in analyze(text).diagnostics]


def test_clean_module_and_symbol_tables():
    m = analyze("struct s { a: u32, p: ptr<s> }\nenum e : u8 { a }\nfn f(x: &s, y: e) -> u64 { 0 }\nconst K: usize = 1;")
    assert not m.diagnostics
    assert set(m.types) == {"s", "e"} and set(m.values) == {"f", "K"}


def test_duplicates():
    assert codes("struct a {}\nenum a { x }") == ["KIF010"]
    assert codes("fn f() {}\nfn f() {}") == ["KIF010"]
    assert codes("struct s { a: u8, a: u8 }") == ["KIF010"]
    assert codes("enum e { a, a }") == ["KIF010"]
    assert codes("fn f(a: u8, a: u8) {}") == ["KIF010"]
    assert codes("interface i { method m(); method m(); }") == ["KIF010"]
    assert codes("fn f<T, T>(x: T) {}") == ["KIF010"]
    assert codes("struct s {} impl s { fn a(&self) {} fn a(&self) {} }") == ["KIF010"]


def test_unknown_type():
    assert codes("fn f(a: nope) {}") == ["KIFP010"]
    assert codes("struct s { a: vector<nope> }") == ["KIFP010"]
    assert codes("fn f() -> [nope; 2] { loop {} }") == ["KIFP010"]


def test_generic_params_and_self_scope():
    assert not codes("fn f<T: copy>(x: T) -> T { x }")
    assert not codes("struct s {} impl s { fn new() -> Self { s {} } }")
    assert codes("fn f() -> Self { }") == ["KIFP010"]
    assert codes("fn f<T: nope>(x: T) {}") == ["KIFP010"]
    assert codes("fn f(x: T) {}") == ["KIFP010"]


def test_impl_interface_must_exist():
    assert not codes("interface clock { method now() -> u64; } struct h {} impl clock for h { fn now(&self) -> u64 { 0 } }")
    assert codes("struct h {} impl clock for h { }") == ["KIFP010"]


def test_generic_arity():
    assert codes("fn f(a: ptr<u8, u8>) {}") == ["KIFP130"]
    assert codes("fn f(a: u8<u8>) {}") == ["KIFP130"]
    assert codes("struct g<T> { a: T } fn f(a: g) {}") == ["KIFP130"]
    assert not codes("struct g<T> { a: T } fn f(a: g<u8>) {}")
    src = 'extern "C++" type reloco::expected<T, E = error>;\n'
    assert not codes(src + "fn f(a: reloco::expected<u8>, b: reloco::expected<u8, u16>) {}")
    assert codes(src + "fn f(a: reloco::expected) {}") == ["KIFP130"]


def test_extern_impl_and_alias():
    src = 'extern "C++" type reloco::flat_map<K, V>;\ntype map<K> = reloco::flat_map<K, u8>;\n'
    assert not codes(src + 'extern "C++" impl<K, V> reloco::flat_map<K, V> { fn len(&self) -> usize; }\nfn f(m: map<u8>) {}')
    assert codes(src + "fn f(m: map<u8, u8>) {}") == ["KIFP130"]


def test_syntax_errors_are_kept():
    assert "KIF001" in codes("fn {")


def test_prelude_is_clean_and_global():
    assert not prelude().diagnostics
    assert not analyze("fn f(a: vector<u8>, b: &str, c: ptr<u32>) {}").diagnostics


def test_own_name_shadows_prelude():
    m = analyze("struct str {}\nfn f(a: str) {}")
    assert not m.diagnostics and m.types["str"].module is m


def test_import_qualified_and_use():
    mf = 'import "microfmt.kif";\n'
    assert not analyze(mf + "fn f(s: &microfmt::sink, b: microfmt::memory_buffer<16>) {}").diagnostics
    assert not analyze(mf + "use microfmt::{sink, logger as lg};\nfn f(s: &sink, l: lg) {}").diagnostics
    assert not analyze(mf + "use microfmt::*;\nfn f(s: &sink) {}").diagnostics
    assert codes("fn f(s: &microfmt::sink) {}") == ["KIFP010"]  # not imported
    assert codes(mf + "use microfmt::nope;") == ["KIFP010"]
    assert codes("use nomod::x;") == ["KIFP010"]
    assert codes('import "missing.kif";') == ["KIF012"]


def test_use_clash_and_private():
    files = {"lib.kif": "namespace lib;\nstruct t {}\nprivate fn hidden() {}\n"}
    assert [x.code for x in analyze('import "lib.kif";\nuse lib::t;\nstruct t {}', files).diagnostics] == ["KIF010"]
    d = analyze('import "lib.kif";\nuse lib::hidden;', files).diagnostics
    assert [x.code for x in d] == ["KIFP016"]
    assert not analyze('import "lib.kif";\nfn f(a: lib::t) {}', files).diagnostics


def test_import_cycle():
    files = {"a.kif": 'namespace a;\nimport "b.kif";', "b.kif": 'namespace b;\nimport "a.kif";'}
    m = analyze('import "a.kif";', files)
    assert any(x.code == "KIF012" for x in m.modules["a"].modules["b"].diagnostics)


def test_lifetimes_declared_and_static():
    assert codes("fn f<'a>(x: &'a u8) -> &'a u8 { x }") == []
    assert codes("fn f(x: &'a u8) {}") == ["KIFP160"]
    assert codes("fn f(x: &'static u8) -> &'static u8 { x }") == []
    assert codes("struct v<'a, 'b: 'a> { x: &'a u8, y: &'b u8 }") == []
    assert codes("struct v<'a: 'z> { x: &'a u8 }") == ["KIFP160"]
    assert codes("fn f<'a, 'a>(x: &'a u8) {}") == ["KIF010"]


def test_lifetime_elision_in_results():
    assert codes("fn f(x: &u8) -> &u8 { x }") == []
    assert codes("fn f<'a>(x: &'a u8, y: &'a u8) -> &u8 { x }") == []
    assert codes("fn f(x: &u8, y: &u8) -> &u8 { x }") == ["KIFP162"]
    assert codes("fn f() -> &u8 { loop {} }") == ["KIFP162"]
    assert codes("fn f(x: u32) -> &'static u8 { loop {} }") == []
    assert codes("struct s {}\nimpl s { fn g(&self, y: &u8) -> &u8 { y } }") == []
    assert codes("struct s {}\nimpl s { fn g(&'static self) {} }") == []


def test_struct_fields_need_named_lifetimes_and_views_count():
    assert codes("struct v { x: &u8 }") == ["KIFP161"]
    assert codes("struct v<'a> { x: &'_ u8 }") == ["KIFP161"]
    src = "struct view<'a> { x: &'a u8 }\n"
    assert codes(src + "fn f(v: view<'_>) -> view { v }") == []
    assert codes(src + "fn f<'a>(v: view<'a>) -> view<'a> { v }") == []
    assert codes(src + "struct w { v: view }") == ["KIFP161"]
    assert codes(src + "struct w<'a> { v: view<'a> }") == []
    assert codes(src + "fn f(a: view, b: view) -> view { a }") == ["KIFP162"]


def test_lifetime_args_do_not_count_as_type_args():
    assert codes("struct view<'a, T> { x: &'a T }\nfn f<'a>(v: view<'a, u8>) {}") == []
    assert codes("struct view<'a, T> { x: &'a T }\nfn f<'a>(v: view<'a, u8, u8>) {}") == ["KIFP130"]


def test_slices():
    assert codes("fn f(x: &[u8]) -> &[u8] { x }") == []
    assert codes("fn f(x: &mut [char], n: &[vector<u8>]) {}") == []
    assert codes("struct s { x: &[u8] }") == ["KIFP161"]
    assert codes("struct s<'a> { x: &'a [u8] }") == []
    assert codes("fn f(x: &[nope]) {}") == ["KIFP010"]
    assert codes("fn f(a: &[u8], b: &[u8]) -> &[u8] { a }") == ["KIFP162"]
    assert codes("fn f(a: &[u8]) { let s = a[1..3]; let t = a[..]; }") == []
    assert not prelude().diagnostics


def test_prelude_borrowing_types_use_elision():
    assert codes("fn f(s: &str) -> str_view { s.view() }") == []
    assert codes("fn f(a: str_view, b: str_view) -> str_view { a }") == ["KIFP162"]
    assert codes("fn f<'a>(a: str_view<'a>, b: str_view<'a>) -> str_view<'a> { a }") == []
    assert codes("struct s { t: str_view }") == ["KIFP161"]
    assert codes("fn f() -> cstr { loop {} }") == []


def test_allocator_ref_never_affects_elision():
    assert codes("fn f(a: allocator_ref, x: &u8) -> &u8 { x }") == []


def test_prelude_value_types():
    assert codes("fn f(a: duration, b: instant, n: non_zero<u32>, o: ordering) {}") == []
    assert codes("fn f(x: fixed_int<128>, y: fixed_int<256, false>, p: fixed_point<i32, 16>) {}") == []
    assert codes("fn f(p: fixed_point<i32>) {}") == ["KIFP130"]
    assert codes("fn f(n: non_zero<u8, u8>) {}") == ["KIFP130"]
    assert not prelude().diagnostics


def test_microfmt_spec_enums_and_log_level():
    mf = 'import "microfmt.kif";\nuse microfmt::*;\n'
    assert codes(mf + "fn f(a: text_align, c: ansi_color, l: log_level, s: scale_base) -> str_view<'static> { log_level_name(l) }") == []
    assert codes("fn f(l: microfmt::log_level) {}") == ["KIFP010"]  # not imported, not in the prelude


def test_microfmt_views_and_extended_logger():
    mf = 'import "microfmt.kif";\nuse microfmt::*;\n'
    assert codes(mf + "fn f(d: &[u8], o: sink) { hexdump_to(o, d, 0); let v = hexdump(d, 0, 16, true); let p = raw_ptr(0); }") == []
    assert codes(mf + "fn f(l: extended_logger, m: log_meta, f: log_flag) {}") == []
    assert codes(mf + "fn f(l: extended_logger<1, 2, 3>) {}") == ["KIFP130"]
    assert codes(mf + "fn f(a: &[u8], b: &[u8]) -> hexdump_view { hexdump(a, 0, 16, true) }") == ["KIFP162"]


def test_type_erased_borrowed_callable():
    mf = 'import "microfmt.kif";\nuse microfmt::*;\n'
    assert codes(mf + "fn rd(a: usize, b: &mut [u8]) -> usize { 0 }\nfn f(r: fn_ref<fn(usize, &mut [u8]) -> usize>) -> hexdump_view { hexdump_checked(0, 8, r, 16, true) }") == []
    assert codes(mf + "fn f(r: fn_ref<fn() -> ()>, q: fn_ref<fn() -> ()>) -> hexdump_view { hexdump_checked(0, 8, r, 16, true) }") == ["KIFP162"]


def test_sink_is_always_borrowed():
    mf = 'import "microfmt.kif";\nuse microfmt::*;\n'
    assert codes(mf + "fn f(b: &mut memory_buffer) -> sink { b.as_sink() }") == []
    assert codes(mf + "fn f() -> sink { loop {} }") == ["KIFP162"]
    assert codes(mf + "fn f() -> sink<'static> { loop {} }") == []


def test_kif_defined_sink():
    mf = 'import "microfmt.kif";\nuse microfmt::*;\n'
    src = mf + "struct uart { base: usize = 0 }\nimpl text_sink for uart { fn write(&mut self, text: str_view) {} }\n"
    assert codes(src + "fn f(u: &mut uart, d: &[u8]) { hexdump_to(u.as_sink(), d, 0); }") == []
    assert codes(src + "fn f<T: text_sink>(t: &mut T) -> sink { t.as_sink() }") == []


def test_body_names_and_scopes():
    assert codes("fn f(a: i32) -> i32 { let b = a; b }") == []
    assert codes("fn f() -> i32 { x }") == ["KIFP010"]
    assert codes("fn f() { let x = x; }") == ["KIFP010"]  # the initialiser cannot see its own variable
    assert codes("fn f() { { let y = 1; } y; }") == ["KIFP010"]
    assert codes("fn f() { for i in 0..3 { i; } i; }") == ["KIFP010"]
    assert codes("const K: u32 = 1;\nfn f() -> u32 { K }") == []
    assert codes("fn g() {}\nfn f() { g(); h(); }") == ["KIFP010"]
    assert codes("fn f() { nope::x; }") == ["KIFP010"]
    assert codes("enum e : u8 { a }\nfn f() { e::a; }") == []
    assert codes("fn f<T: copy>(x: T) { T::zero(); }") == []
    assert codes("struct s { x: u32 }\nfn f(x: u32) -> s { s { x } }") == []
    assert codes("fn f() { match 1 { 0 => 1, _ => 2 }; }") == []


def test_body_mutability():
    assert codes("fn f() { let x = 1; x = 2; }") == ["KIFP020"]
    assert codes("fn f() { let mut x = 1; x = 2; x += 1; }") == []
    assert codes("fn f(a: i32) { a += 1; }") == ["KIFP020"]
    assert codes("fn f() { let x = 1; x += 1; }") == ["KIFP020"]
    assert codes("fn f() { let x = 1; let y = &mut x; }") == ["KIFP020"]
    assert codes("fn f() { let x; x = 1; }") == []
    assert codes("fn f() { let x; x = 1; x = 2; }") == ["KIFP020"]


def test_lambdas_do_not_capture():
    src = "fn each(cb: fn(i32) -> i32) {}\n"
    assert codes(src + "fn f() { each(|a| a + 1); }") == []
    assert codes(src + "fn f() { let n = 2; each(|a| a + n); }") == ["KIFP170"]
    assert codes(src + "fn f(n: i32) { each(|a| a + n); }") == ["KIFP170"]
    assert codes(src + "const N: i32 = 2;\nfn f() { each(|a| a + N); }") == []
    assert codes(src + "fn f() { each(|a| each(|b| a)); }") == ["KIFP170"]
    assert codes(src + "fn f() { each(|a, a| a); }") == ["KIF010", "KIFP011"]
    assert codes(src + "fn f() { each(|a| nope); }") == ["KIFP010"]


def test_expression_types():
    assert codes("fn f() -> u32 { let x: u32 = 1; x + 2 }") == []
    assert codes("fn f(a: u32, b: u64) -> u32 { a + b }") == ["KIFP012"]
    assert codes("fn f(a: u32) -> u64 { a }") == ["KIFP012"]
    assert codes("fn f(a: u32) -> u64 { a as u64 }") == []
    assert codes("fn f() { let x: u8 = 300; }") == ["KIFP013"]
    assert codes("fn f() { let x: i8 = -128; let y: u8 = -1; }") == ["KIFP013"]
    assert codes("fn f() -> u8 { 256 }") == ["KIFP013"]
    assert codes("fn f(a: u32) { if a { } }") == ["KIFP012"]
    assert codes("fn f(a: u32) -> bool { a == 1 && a != 2 }") == []
    assert codes("fn f(a: u32, b: bool) -> bool { a == b }") == ["KIFP012"]
    assert codes("fn f(a: bool) -> bool { a + a }") == ["KIFP012"]
    assert codes("fn f(a: u32) -> u32 { a << 2u8 }") == []
    assert codes("fn f(a: u32) -> bool { a as bool }") == ["KIFP012"]
    assert codes("fn f(a: i32) -> i32 { if a == 0 { 1 } else { 2 } }") == []
    assert codes("fn f(a: i32) -> i32 { if a == 0 { 1 } else { true } }") == ["KIFP012"]
    assert codes("fn f(a: i32) -> i32 { return a; }") == []
    assert codes("fn f(a: i32) -> bool { return a; }") == ["KIFP012"]
    assert codes("fn f() -> u32 { return; }") == ["KIFP012"]
    assert codes("fn f(a: u32) -> u32 { let mut x = 0; x = a; x }") == []
    assert codes("fn f(a: u32) -> u32 { let mut x: bool = true; x = a; 0 }") == ["KIFP012"]
    assert codes("fn f(a: u8) { let mut r = [0u8; 4]; r[a] = 1; }") == ["KIFP012"]
    assert codes("fn f(a: usize, r: &[u8]) -> u8 { r[a] }") == []


def test_control_flow_returns():
    assert codes("fn f() -> u32 { }") == ["KIFP031"]
    assert codes("fn f() -> u32 { loop {} }") == []
    assert codes("fn f() -> u32 { loop { break; } }") == ["KIFP012"]
    assert codes("fn f(a: bool) -> u32 { if a { return 1; } else { return 2; } }") == []
    assert codes("fn f() -> u32 { panic!(\"no\"); }") == []
    assert codes("fn f(a: u32) -> u32 { match a { 0 => 1, _ => panic!(\"x\") } }") == []


def test_calls_and_structs():
    src = "struct p { x: u32, y: u32 = 0 }\nenum e : u8 { a, b }\nfn g(a: u32, b: &p) -> u32 { a }\n"
    assert codes(src + "fn f(q: &p) -> u32 { g(1, q) }") == []
    assert codes(src + "fn f(q: &p) -> u32 { g(1) }") == ["KIFP011"]
    assert codes(src + "fn f(q: &p) -> u32 { g(true, q) }") == ["KIFP011"]
    assert codes(src + "fn f(q: &p) -> u32 { g(1, p { x: 1 }) }") == ["KIFP011"]  # a value where a reference is wanted
    assert codes(src + "fn f() -> p { p { x: 1 } }") == []
    assert codes(src + "fn f() -> p { p { y: 1 } }") == ["KIFP014"]
    assert codes(src + "fn f() -> p { p { x: 1, z: 2 } }") == ["KIFP014"]
    assert codes(src + "fn f() -> p { p { x: 1, x: 2 } }") == ["KIFP014"]
    assert codes(src + "fn f() -> p { p { x: true } }") == ["KIFP012"]
    assert codes(src + "fn f(q: &p) -> u32 { q.x }") == []
    assert codes(src + "fn f(q: &p) -> bool { q.x }") == ["KIFP012"]
    assert codes(src + "fn f(q: &p) -> u32 { q.nope }") == ["KIFP010"]
    assert codes(src + "fn f() -> e { e::a }") == []
    assert codes(src + "fn f() -> e { e::c }") == ["KIFP010"]
    assert codes(src + "fn f() -> u32 { e::a }") == ["KIFP012"]
    assert codes(src + "fn f(m: &mut p) -> u32 { g(1, m) }") == []
    assert codes(src + "fn f(m: &p) { h(m); }\nfn h(m: &mut p) {}") == ["KIFP011"]


def test_lambda_against_fn_pointer():
    src = "fn each(cb: fn(i32) -> i32) {}\n"
    assert codes(src + "fn f() { each(|a| a); each(|a: i32| a); }") == []
    assert codes(src + "fn f() { each(|a, b| a); }") == ["KIFP011"]
    assert codes(src + "fn f() { each(|a: bool| 1); }") == ["KIFP011"]


def test_methods_and_associated_functions():
    src = ("struct c { n: u32 }\n"
           "impl c {\n  fn new(n: u32) -> Self { Self { n: n } }\n  fn get(&self) -> u32 { self.n }\n"
           "  fn bump(&mut self, by: u32) { self.n = self.n + by; }\n}\n")
    assert codes(src + "fn f() -> u32 { let mut x = c::new(1); x.bump(2); x.get() }") == []
    assert codes(src + "fn f() { let x = c::new(1); x.bump(2); }") == ["KIFP020"]
    assert codes(src + "fn f(x: &c) { x.bump(2); }") == ["KIFP020"]
    assert codes(src + "fn f(x: &mut c) { x.bump(2); }") == []
    assert codes(src + "fn f(x: &c) -> bool { x.get() }") == ["KIFP012"]
    assert codes(src + "fn f(x: &mut c) { x.bump(); }") == ["KIFP011"]
    assert codes(src + "fn f(x: &c) { x.nope(); }") == ["KIFP010"]
    assert codes(src + "fn f(x: &c) { x.new(1); }") == ["KIFP011"]
    assert codes(src + "fn f() { c::nope(); }") == ["KIFP010"]
    assert codes(src + "fn f() { c::new(true); }") == ["KIFP011"]
    assert codes(src + "fn f() -> bool { c::new(1) }") == ["KIFP012"]
    assert codes(src + "fn f() -> u32 { c::new(1).n }") == []


def test_mutation_through_fields_and_derefs():
    s = "struct p { x: u32 }\n"
    assert codes(s + "fn f(q: &mut p) { q.x = 1; }") == []
    assert codes(s + "fn f(q: &p) { q.x = 1; }") == ["KIFP020"]
    assert codes(s + "fn f(q: p) { q.x = 1; }") == ["KIFP020"]
    assert codes(s + "fn f(mut_ok: &mut u32) { *mut_ok = 1; }") == []
    assert codes(s + "fn f(r: &u32) { *r = 1; }") == ["KIFP020"]
    assert codes(s + "fn f() { let a = [0u8; 4]; a[0] = 1; }") == ["KIFP020"]
    assert codes(s + "fn f() { let mut a = [0u8; 4]; a[0] = 1; }") == []


def test_impl_interface_checks():
    i = "interface clock { method gettime() -> u32; method res() -> u64 { 1 } }\n"
    assert codes(i + "struct h {}\nimpl clock for h { fn gettime(&self) -> u32 { 0 } }") == []
    assert codes(i + "struct h {}\nimpl clock for h { }") == ["KIFP026"]
    assert codes(i + "struct h {}\nimpl clock for h { fn gettime(&self) -> u64 { 0 } }") == ["KIFP026"]
    assert codes(i + "struct h {}\nimpl clock for h { fn gettime(&self, a: u8) -> u32 { 0 } }") == ["KIFP026"]
    assert codes(i + "struct h {}\nimpl clock for h { fn gettime(&self) -> u32 { 0 } fn zzz(&self) {} }") == ["KIFP026"]
    assert codes(i + "#[copy]\nstruct h {}\nimpl clock for h { fn gettime(&self) -> u32 { 0 } }") == ["KIFP028"]
    assert codes("struct h {}\nimpl h { fn a(&self) {} }\nimpl h { fn a(&self) {} }") == ["KIFP027"]
    # provided/default interface methods are callable on the implementer
    assert codes(i + "struct h {}\nimpl clock for h { fn gettime(&self) -> u32 { 0 } }\nfn f(x: &h) -> u64 { x.res() }") == []


def test_extern_impl_methods_resolve():
    assert codes("fn f(v: &vector<u8>) -> usize { v.len() }") == []
    assert codes("fn f(v: &vector<u8>) -> bool { v.len() }") == ["KIFP012"]


MV = "struct m { n: u32 }\n#[copy]\nstruct c { n: u32 }\nfn take(x: m) {}\nfn peek(x: &m) {}\nfn takec(x: c) {}\n"


def test_moves():
    assert codes(MV + "fn f(a: m) { take(a); }") == []
    assert codes(MV + "fn f(a: m) { take(a); take(a); }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { let b = a; let c = a; }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { peek(&a); peek(&a); take(a); }") == []
    assert codes(MV + "fn f(a: c) { takec(a); takec(a); }") == []  # #[copy] structs copy
    assert codes(MV + "fn f(a: &m) { take(a); }") == ["KIFP011"]
    assert codes(MV + "fn f(a: m) { take(a); a.n; }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) -> m { a }") == []
    assert codes(MV + "fn f(a: m) -> m { let b = a; a }") == ["KIFP023"]
    assert codes(MV + "fn f() { let mut a = m { n: 1 }; take(a); a = m { n: 2 }; take(a); }") == []
    assert codes(MV + "fn f(a: m) { let b = m { n: a.n }; take(a); }") == []  # reading a copy field is not a move


def test_moves_through_branches_and_loops():
    assert codes(MV + "fn f(a: m, k: bool) { if k { take(a); } take(a); }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m, k: bool) { if k { take(a); } else { take(a); } }") == []
    assert codes(MV + "fn f(a: m, k: bool) { if k { take(a); return; } take(a); }") == []
    assert codes(MV + "fn f(a: m, k: u32) { match k { 0 => take(a), _ => peek(&a) } take(a); }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m, k: bool) { while k { take(a); } }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { loop { take(a); break; } }") == []
    assert codes(MV + "fn f(a: m) { loop { take(a); } }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { for i in 0..3 { take(a); } }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { loop { let mut b = a; break; } take(a); }") == ["KIFP023"]
    assert codes(MV + "fn f(a: m) { each(|x: m| take(x)); }\nfn each(cb: fn(m)) {}") == []


def test_move_out_of_borrows_and_fields():
    assert codes(MV + "struct w { a: m, b: m }\nfn f(x: w) { take(x.a); take(x.b); }") == []
    assert codes(MV + "struct w { a: m, b: m }\nfn f(x: w) { take(x.a); take(x.a); }") == ["KIFP023"]
    assert codes(MV + "struct w { a: m, b: m }\nfn f(x: w) { take(x.a); let y = &x; }") == ["KIFP023"]
    assert codes(MV + "struct w { a: m, b: m }\nfn f(x: &w) { take(x.a); }") == ["KIFP024"]
    assert codes(MV + "fn f(x: &m) { let y = *x; }") == ["KIFP024"]
    assert codes(MV + "struct w { a: m }\nfn f(y: w) { let mut x = y; take(x.a); x.a = m { n: 1 }; take(x.a); }") == []
    assert codes(MV + "struct w { a: m }\nimpl w { fn drop(&mut self) {} }\nfn f(x: w) { take(x.a); }") == ["KIFP024"]


def test_definite_assignment():
    assert codes("fn f() -> u32 { let x; x = 1; x }") == []
    assert codes("fn f() -> u32 { let x: u32; x }") == ["KIFP022"]
    assert codes("fn f(k: bool) -> u32 { let x: u32; if k { x = 1; } x }") == ["KIFP022"]
    assert codes("fn f(k: bool) -> u32 { let x: u32; if k { x = 1; } else { x = 2; } x }") == []
    assert codes("fn f(k: bool) -> u32 { let x: u32; if k { x = 1; } else { return 0; } x }") == []
    assert codes("fn f(k: u32) -> u32 { let x: u32; match k { 0 => x = 1, _ => x = 2 } x }") == []


def test_methods_move_self_and_args():
    s = "struct m { n: u32 }\nimpl m {\n  fn done(self) {}\n  fn look(&self) {}\n  fn eat(&self, o: m) {}\n}\n"
    assert codes(s + "fn f(a: m) { a.done(); a.look(); }") == ["KIFP023"]
    assert codes(s + "fn f(a: m, b: m) { a.eat(b); a.eat(b); }") == ["KIFP023"]
    assert codes(s + "fn f(a: m) { a.look(); a.look(); a.done(); }") == []


def test_dangling_references():
    assert codes("fn f<'a>() -> &'a u32 { let x = 1; &x }") == ["KIFP021"]
    assert codes("fn f<'a>() -> &'a u32 { let x = 1; let r = &x; r }") == ["KIFP021"]
    assert codes("fn f<'a>() -> &'a u32 { let x = 1; return &x; }") == ["KIFP021"]
    assert codes("fn f<'a>(x: u32) -> &'a u32 { &x }") == ["KIFP021"]
    assert codes("fn f(x: &u32) -> &u32 { x }") == []
    assert codes("struct w { a: u32 }\nfn f(x: &w) -> &u32 { &x.a }") == []
    assert codes("fn f(x: &u32) -> &u32 { let r = &*x; r }") == []
    assert codes("fn f() -> &'static u32 { let x = 1; &x }") == ["KIFP021"]
    assert codes("fn f() { let r; { let x = 1; r = &x; } let y = *r; }") == ["KIFP021"]
    assert codes("fn f() { let r; { let x = 1; r = &x; } }") == []
    assert codes("fn f() { let x = 1; let r; { r = &x; } let y = *r; }") == []


def test_stored_references_escape():
    s = "struct h<'a> { r: &'a u32 }\n"
    assert codes(s + "fn f(o: &mut h) { let x = 1; o.r = &x; }") == ["KIFP021"]
    assert codes(s + "fn f<'a>(o: &mut h<'a>, y: &'a u32) { o.r = y; }") == []
    assert codes(s + "fn f<'a>() -> h<'a> { let x = 1; h { r: &x } }") == ["KIFP021"]
    assert codes(s + "fn f<'a>(y: &'a u32) -> h<'a> { h { r: y } }") == []


def test_call_results_borrow_by_lifetime():
    src = ("fn pick<'a, 'b>(x: &'a u32, y: &'b u32) -> &'a u32 { x }\n"
           "fn first(x: &u32) -> &u32 { x }\n")
    assert codes(src + "fn f<'a>() -> &'a u32 { let a = 1; let b = 2; pick(&a, &b) }") == ["KIFP021"]
    assert codes(src + "fn f(a: &u32) -> &u32 { let b = 2; pick(a, &b) }") == []
    assert codes(src + "fn f<'a>() -> &'a u32 { let a = 1; first(&a) }") == ["KIFP021"]
    m = "struct s { n: u32 }\nimpl s {\n  fn get(&self) -> &u32 { &self.n }\n  fn set(&mut self, v: u32) { self.n = v; }\n}\n"
    assert codes(m + "fn f(x: &s) -> &u32 { x.get() }") == []
    assert codes(m + "fn f<'a>() -> &'a u32 { let x = s { n: 1 }; x.get() }") == ["KIFP021"]


def test_conflicting_borrows():
    assert codes("fn f() { let mut x = 1; let r = &x; x = 2; let y = *r; }") == ["KIFP033"]
    assert codes("fn f() { let mut x = 1; let r = &x; x = 2; }") == []  # r is never used again
    assert codes("fn f() { let mut x = 1; let r = &mut x; let y = x; *r = 3; }") == ["KIFP033"]
    assert codes("fn f() { let mut x = 1; let r = &x; let y = x; let z = *r; }") == []  # reads share
    assert codes("fn f() { let mut x = 1; let r = &mut x; *r = 3; x = 4; }") == []
    assert codes("fn f() { let mut x = 1; let r = &x; let q = &mut x; let z = *r; }") == ["KIFP033"]
    assert codes("fn f() { let mut x = 1; let r = &mut x; let q = &mut x; *r = 1; }") == ["KIFP033"]
    assert codes("fn f() { let mut x = 1; let r = &x; let q = &x; let z = *r + *q; }") == []
    assert codes("struct m { n: u32 }\nfn eat(x: m) {}\nfn f(a: m) { let r = &a; eat(a); let z = r.n; }") == ["KIFP033"]
    assert codes("fn f(k: bool) { let mut x = 1; let r = &x; if k { x = 2; } else { let z = *r; } }") == []
    assert codes("fn f(k: bool) { let mut x = 1; let r = &x; if k { x = 2; } let z = *r; }") == ["KIFP033"]
    assert codes("fn f() { let mut x = 1; let mut r = &x; x = 2; r = &x; let z = *r; }") == []


def test_borrows_of_disjoint_parts_do_not_conflict():
    p = "struct p { a: u32, b: u32 }\n"
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; let q = &mut s.b; *r = 3; *q = 4; }") == []
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; s.b = 5; *r = 1; }") == []
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; let v = s.b; *r = v; }") == []
    assert codes("fn f() { let mut a: [u32; 2] = [1, 2]; let r = &mut a[0]; let q = &mut a[1]; *r = 3; *q = 4; }") == []
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; let q = &mut s.a; *r = 1; }") == ["KIFP033"]
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; s.a = 5; *r = 1; }") == ["KIFP033"]
    assert codes(p + "fn f() { let mut s = p { a: 1, b: 2 }; let r = &mut s.a; let t = s; *r = 3; }") == ["KIFP033"]  # the whole value overlaps every part
    assert codes("fn f(i: usize) { let mut a: [u32; 2] = [1, 2]; let r = &mut a[0]; let q = &mut a[i]; *r = 3; *q = 4; }") == ["KIFP033"]  # a variable index may hit any element
    assert codes("fn f() { let mut a: [u32; 2] = [1, 2]; let r = &mut a[0]; let q = &mut a[0]; *r = 3; *q = 4; }") == ["KIFP033"]


def test_conflicting_borrows_in_calls_and_methods():
    s = "struct s { n: u32 }\nimpl s {\n  fn get(&self) -> &u32 { &self.n }\n  fn set(&mut self, v: u32) { self.n = v; }\n}\n"
    assert codes(s + "fn two(a: &mut u32, b: &u32) {}\nfn f() { let mut x = 1; two(&mut x, &x); }") == ["KIFP033"]
    assert codes(s + "fn two(a: &u32, b: &u32) {}\nfn f() { let x = 1; two(&x, &x); }") == []
    assert codes(s + "fn f() { let mut x = s { n: 1 }; let r = x.get(); x.set(2); let z = *r; }") == ["KIFP033"]
    assert codes(s + "fn f() { let mut x = s { n: 1 }; let r = x.get(); let z = *r; x.set(2); }") == []
    assert codes(s + "fn f(x: &mut s) { let r = x.get(); x.set(2); let z = *r; }") == []  # not tracked through references


EN = "enum col { red, green, blue }\n"


def test_match_exhaustiveness():
    assert codes(EN + "fn f(c: col) -> u32 { match c { col::red => 1, col::green => 2, col::blue => 3 } }") == []
    assert codes(EN + "fn f(c: col) -> u32 { match c { col::red => 1, col::green => 2 } }") == ["KIFP030"]
    assert codes(EN + "fn f(c: col) -> u32 { match c { col::red | col::green => 1, _ => 2 } }") == []
    assert codes(EN + "fn f(c: col) -> u32 { match c { col::red | col::green => 1, col::blue => 2 } }") == []
    assert codes(EN + "fn f(c: &col) -> u32 { match c { col::red => 1 } }") == ["KIFP030"]
    assert codes("fn f(b: bool) -> u32 { match b { true => 1, false => 0 } }") == []
    assert codes("fn f(b: bool) -> u32 { match b { true => 1 } }") == ["KIFP030"]
    assert codes("fn f(n: u32) -> u32 { match n { 0 => 1, 1 => 2 } }") == ["KIFP030"]
    assert codes("fn f(n: u32) -> u32 { match n { 0 => 1, _ => 2 } }") == []
    assert codes("fn f(n: u8) -> u32 { match n { 0..=127 => 1, 128..=255 => 2 } }") == []
    assert codes("fn f(n: u8) -> u32 { match n { 0..=100 => 1, 128..=255 => 2 } }") == ["KIFP030"]
    assert codes("fn f(n: i8) -> u32 { match n { -128..=-1 => 1, 0..=127 => 2 } }") == []
    assert codes("fn f(n: u32) { match n { _ => {} } }") == []


def test_recursion():
    assert codes("fn f(n: u32) -> u32 { f(n) }") == ["KIFP032"]
    assert codes("fn a(n: u32) -> u32 { b(n) }\nfn b(n: u32) -> u32 { a(n) }") == ["KIFP032", "KIFP032"]
    assert codes("fn a(n: u32) -> u32 { b(n) }\nfn b(n: u32) -> u32 { n }") == []
    assert codes("fn a(n: u32) -> u32 { b(n) + b(n) }\nfn b(n: u32) -> u32 { n }") == []
    assert codes("gen fn f(n: u32) -> u32 { f(n) }") == []
    s = "struct s { n: u32 }\nimpl s {\n  fn up(&self) -> u32 { self.up() }\n  fn ok(&self) -> u32 { self.n }\n}\n"
    assert codes(s) == ["KIFP032"]
    assert codes("struct s { n: u32 }\nimpl s {\n  fn make() -> u32 { s::make() }\n}\n") == ["KIFP032"]
    assert codes("fn a(n: u32) -> u32 { let g = a; 1 }") == ["KIFP032"]


def test_unsafe_operations():
    ext = 'extern "C" fn raw(x: u32) -> u32;\n#[safe]\nextern "C" fn ok(x: u32) -> u32;\nextern "C" fn pf(f: u32, ...) -> i32;\n'
    assert codes(ext + "fn f() { raw(1); }") == ["KIFP070"]
    assert codes(ext + "fn f() { unsafe { raw(1); } }") == []
    assert codes(ext + "fn f() { ok(1); }") == []
    assert codes(ext + "fn f() { pf(1, 2); }") == ["KIFP070"]
    assert codes(ext + "fn f() { unsafe { pf(1, 2); } }") == []
    assert codes(ext + "fn f() { unsafe { each(|x: u32| raw(x)); } }\nfn each(cb: fn(u32) -> u32) {}") == []
    s = "struct s { n: u32 }\nimpl s {\n  #[unsafe] fn peek(&self) -> u32 { self.n }\n  #[unsafe] fn make() -> u32 { 1 }\n}\n"
    assert codes(s + "fn f(x: &s) { x.peek(); }") == ["KIFP131"]
    assert codes(s + "fn f(x: &s) { unsafe { x.peek(); } }") == []
    assert codes(s + "fn f() { s::make(); }") == ["KIFP131"]
    assert codes(s + "#[unsafe]\nfn g(x: &s) { x.peek(); }") == []
    assert codes("fn f(p: c_ptr<u8>) -> u8 { *p }") == ["KIFP070"]
    assert codes("fn f(p: c_ptr<u8>) -> u8 { unsafe { *p } }") == []
    assert codes("fn f(p: c_cptr<u8>) -> u8 { *p }") == ["KIFP070"]


ATOM = ('#[might_sleep]\nextern "C" fn nap() -> ();\n#[safe]\n#[enter_atomic]\nextern "C" fn enter() -> ();\n'
        '#[safe]\n#[leave_atomic]\nextern "C" fn leave() -> ();\n#[atomic_only]\nextern "C" fn percpu() -> ();\n'
        '#[atomic_scope]\nstruct guard { n: u32 = 0 }\nimpl guard { fn new() -> Self { guard { n: 1 } } fn drop(&mut self) {} }\n')


def test_atomic_context():
    assert codes(ATOM + "fn f() { unsafe { nap(); } }") == []
    assert codes(ATOM + "#[atomic_entry]\nfn f() { unsafe { nap(); } }") == ["KIFP110"]
    assert codes(ATOM + "fn f() { let _g = guard::new(); unsafe { nap(); } }") == ["KIFP110"]
    assert codes(ATOM + "fn f() { { let _g = guard::new(); } unsafe { nap(); } }") == []
    assert codes(ATOM + "fn f() { enter(); unsafe { nap(); } leave(); }") == ["KIFP110"]
    assert codes(ATOM + "fn f() { enter(); leave(); unsafe { nap(); } }") == []
    # inference: a function calling a sleepy one is sleepy
    assert codes(ATOM + "fn slow() { unsafe { nap(); } }\nfn f() { let _g = guard::new(); slow(); }") == ["KIFP110"]
    assert codes(ATOM + "fn slow() { unsafe { nap(); } }\nfn mid() { slow(); }\n#[atomic_entry]\nfn f() { mid(); }") == ["KIFP110"]
    assert codes(ATOM + "#[atomic_exempt]\nfn slow() { unsafe { nap(); } }\n#[atomic_entry]\nfn f() { slow(); }") == []


def test_atomic_only_and_scopes():
    assert codes(ATOM + "fn f() { unsafe { percpu(); } }") == ["KIFP111"]
    assert codes(ATOM + "fn f() { let _g = guard::new(); unsafe { percpu(); } }") == []
    assert codes(ATOM + "#[atomic_entry]\nfn f() { unsafe { percpu(); } }") == []
    assert codes(ATOM + "#[atomic_only]\nfn h() {}\nfn f() { h(); }") == ["KIFP111"]
    assert codes(ATOM + "fn f() { enter(); }") == ["KIFP112"]
    assert codes(ATOM + "fn f(k: bool) { enter(); if k { leave(); } }") == ["KIFP112"]
    assert codes(ATOM + "fn f(k: bool) { enter(); if k { leave(); } else { leave(); } }") == []
    assert codes(ATOM + "fn f() { leave(); }") == ["KIFP112"]
    assert codes(ATOM + "fn f(k: bool) { enter(); if k { return; } leave(); }") == ["KIFP112"]
    assert codes(ATOM + "fn take(g: guard) {}\nfn f() { let g = guard::new(); take(g); }") == ["KIFP113"]


def test_atomic_interface_methods():
    src = ('#[might_sleep]\nextern "C" fn nap() -> ();\n'
           "interface timer {\n  #[atomic_entry] method tick(now: u64) -> ();\n  #[might_sleep] method calibrate() -> i32;\n}\n"
           "struct hpet { n: u32 }\n")
    assert codes(src + "impl timer for hpet {\n  fn tick(&self, now: u64) { unsafe { nap(); } }\n  fn calibrate(&self) -> i32 { 0 }\n}") == ["KIFP110"]
    assert codes(src + "impl timer for hpet {\n  fn tick(&self, now: u64) {}\n  fn calibrate(&self) -> i32 { 0 }\n}\n"
                 "#[atomic_entry]\nfn irq(h: &hpet) { let _x = h.calibrate(); }") == ["KIFP110"]
    assert codes(src + "impl timer for hpet {\n  fn tick(&self, now: u64) {}\n  fn calibrate(&self) -> i32 { 0 }\n}") == []


def test_some_none_typing():
    assert codes("fn f() -> optional<u32> { some(1_u32) }") == []
    assert codes("fn f() -> optional<u32> { none() }") == []
    assert codes("fn f() -> optional<u32> { some(1_u32, 2_u32) }") == ["KIFP011"]
    assert codes("fn f() -> optional<u32> { some(true) }") == ["KIFP012"]


def test_dyn_handles():
    base = "#[ops] interface c { method get(&self) -> u32; method add(&mut self, n: u32) -> u32; }\nstruct s { n: u32 }\nstruct o { n: u32 }\n"
    ok = base + "impl c for s { fn get(&self) -> u32 { self.n } fn add(&mut self, n: u32) -> u32 { self.n } }\n"
    assert codes(ok + "fn r(h: &mut dyn c) -> u32 { h.add(1) }\nfn f() -> u32 { let mut x = s { n: 0 }; r(&mut x) }") == []
    # no impl for o
    assert codes(ok + "fn r(h: &dyn c) -> u32 { h.get() }\nfn f() -> u32 { let x = o { n: 0 }; r(&x) }") != []
    # a shared borrow cannot become &mut dyn, and a &dyn cannot call a &mut self method
    assert codes(ok + "fn r(h: &mut dyn c) -> u32 { h.get() }\nfn f() -> u32 { let x = s { n: 0 }; r(&x) }") != []
    assert codes(ok + "fn r(h: &dyn c) -> u32 { h.add(1) }") != []
