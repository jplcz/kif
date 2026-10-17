# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
import pytest

from kiflang.interp import Interp, Struct, Trap, run
from kiflang.symbols import analyze


def mod(text):
    return analyze("t.kif", text)


def trap(text, name, *args, **kw):
    with pytest.raises(Trap) as e:
        run(mod(text), name, *args, **kw)
    return e.value.code


def test_integer_wrapping():
    m = mod("fn f(a: u8, b: u8) -> u8 { a + b }\nfn g(a: i8) -> i8 { a * 2 }\nfn h(a: u32) -> u32 { !a }")
    assert run(m, "f", 250, 10) == 4
    assert run(m, "g", 100) == -56
    assert run(m, "h", 0) == 0xFFFFFFFF


def test_division_and_traps():
    m = mod("fn d(a: i32, b: i32) -> i32 { a / b }\nfn r(a: i32, b: i32) -> i32 { a % b }")
    assert run(m, "d", -7, 2) == -3
    assert run(m, "r", -7, 2) == -1
    assert trap("fn d(a: i32, b: i32) -> i32 { a / b }", "d", 1, 0) == "KIFP100"
    assert trap("fn d(a: i32, b: i32) -> i32 { a / b }", "d", -2**31, -1) == "KIFP100"


def test_shifts():
    t = "fn s(a: u32, n: u32) -> u32 { a << n }"
    assert run(mod(t), "s", 1, 31) == 2**31
    assert trap(t, "s", 1, 32) == "KIFP100"


def test_control_flow_and_loops():
    m = mod("""
fn sum() -> u32 { let mut s: u32 = 0; for i in 0..10 { s += i; } s }
fn w() -> u32 { let mut i: u32 = 0; while i < 5 { i += 1; if i == 3 { continue; } } i }
fn l() -> u32 { let mut i: u32 = 0; loop { i += 1; if i > 7 { break; } } i }
fn early(a: u32) -> u32 { if a > 1 { return 10; } 20 }
""")
    assert run(m, "sum") == 45
    assert run(m, "w") == 5
    assert run(m, "l") == 8
    assert (run(m, "early", 2), run(m, "early", 0)) == (10, 20)


def test_match_and_enums():
    m = mod("""
enum Color { Red, Green = 5, Blue }
fn v(c: Color) -> u32 { match c { Color::Red => 1, Color::Green => 2, Color::Blue => 3 } }
fn n() -> u32 { Color::Blue as u32 }
fn from(x: u32) -> Color { x as Color }
fn r(x: u32) -> u32 { match x { 0..=3 => 1, 4 | 5 => 2, _ => 3 } }
""")
    assert run(m, "n") == 6
    assert run(m, "v", run(m, "from", 5)) == 2
    assert [run(m, "r", i) for i in (2, 5, 9)] == [1, 2, 3]
    assert trap("enum C { A }\nfn f(x: u32) -> C { x as C }", "f", 4) == "KIFP100"


def test_structs_methods_and_refs():
    m = mod("""
#[copy] struct P { x: i32, y: i32 }
impl P {
    fn sum(&self) -> i32 { self.x + self.y }
    fn bump(&mut self) { self.x += 1; }
}
fn f() -> i32 { let mut p = P { x: 1, y: 2 }; let q = p; p.bump(); p.sum() * 10 + q.x }
fn g(r: &mut i32) { *r = *r + 5; }
fn h() -> i32 { let mut a: i32 = 1; g(&mut a); a }
""")
    assert run(m, "f") == 41
    assert run(m, "h") == 6


def test_arrays_and_bounds():
    t = "fn f(i: u32) -> u32 { let mut a = [1u32, 2, 3]; a[1] = 9; a[i] }"
    m = mod(t)
    assert run(m, "f", 1) == 9
    assert trap(t, "f", 3) == "KIFP100"


def test_lambda_and_panic():
    m = mod("fn f() -> u32 { let k: u32 = 4; let g = |a: u32| a + k; g(3) }")
    assert run(m, "f") == 7
    assert trap('fn f() { panic!("boom"); }', "f") == "KIFP100"


def test_limits():
    assert trap("fn f() { loop { } }", "f", steps=1000) == "KIFP103"
    assert trap("fn f(n: u32) -> u32 { f(n + 1) }", "f", 0, depth=16) == "KIFP103"


def test_not_available_at_generation_time():
    t = 'extern "C" { #[safe] fn puts(x: u32); }\nfn f() { puts(1); }'
    assert trap(t, "f") == "KIFP101"


GEN = '''
gen fn g(x: u32) -> str {
    let mut v: Vec<str> = Vec::new(); v.push("a"); v.push("b");
    let o = if x > 1 { Some(x) } else { None };
    let t = match o { Some(n) => format!("{}:{:04x}:{:?}", n, 255, "q"), None => "none" };
    v.join(",") + t
}
gen fn bad() { fail!("nope {}", 3); }
gen fn p() -> i64 { parse_int("0x10") + parse_int("5") }
gen fn s() -> u32 { let s = "a-b-c"; let parts = s.split("-"); parts.len() }
gen fn big() { let mut v: Vec<u32> = Vec::new(); loop { v.push(1); } }
gen fn u() -> u32 { let o: Option<u32> = None; o.unwrap() }
'''


def test_gen_strings_vec_option_format():
    m = mod(GEN)
    assert m.diagnostics == []
    assert run(m, "g", 3) == 'a,b3:00ff:"q"'
    assert run(m, "g", 0) == "a,bnone"
    assert run(m, "p") == 21
    assert run(m, "s") == 3


def test_gen_fail_and_limits():
    m = mod(GEN)
    with pytest.raises(Trap) as e:
        run(m, "bad")
    assert (e.value.code, e.value.message) == ("KIFP102", "nope 3")
    assert trap(GEN, "big", steps=10**9) == "KIFP103"
    assert trap(GEN, "u") == "KIFP100"


def test_format_checks_and_option_exhaustiveness():
    from kiflang.symbols import analyze as a
    codes = lambda t: [d.code for d in a("t.kif", t).diagnostics]
    assert codes('gen fn f() -> str { format!("{} {}", 1) }') == ["KIFP040"]
    assert codes('gen fn f() -> str { format!("{:q}", 1) }') == ["KIFP040"]
    assert codes('gen fn f() -> str { format!("{{}} {}", 1) }') == []
    assert codes("gen fn f(o: Option<u32>) -> u32 { match o { Some(n) => n } }") == ["KIFP030"]
    assert codes("gen fn f(o: Option<u32>) -> u32 { match o { Some(n) => n, None => 0 } }") == []


RES = '''
fn a(x: u32) -> result<u32> { if x > 1 { return err(error::nomem); } ok(x) }
fn b(x: u32) -> result<u32> { let v = a(x)?; ok(v + 1) }
fn c(x: u32) -> u32 { match b(x) { ok(v) => v, err(_) => 99 } }
fn d() -> result<str> { let s = "x".try_clone()?; ok(s) }
fn e(x: u8) -> result<u8> { ok(255 + x) }
fn l() -> result<u32> { let f = |x: u32| -> result<u32> { let v = a(x)?; ok(v * 2) }; f(5) }
'''


def test_result_and_try():
    from kiflang.interp import Res, ErrVal
    m = mod(RES)
    assert m.diagnostics == []
    assert run(m, "b", 1) == Res(True, 2)
    assert run(m, "b", 5) == Res(False, ErrVal("nomem"))
    assert [run(m, "c", 0), run(m, "c", 7)] == [1, 99]
    assert run(m, "d") == Res(True, "x")
    assert run(m, "e", 3) == Res(True, 2)
    assert run(m, "l") == Res(False, ErrVal("nomem"))


def test_result_checks():
    from kiflang.symbols import analyze as a
    codes = lambda t: [d.code for d in a("t.kif", t).diagnostics]
    assert codes("fn c(x: u32) -> result<u32> { let v = x?; ok(v) }") == ["KIFP041"]
    assert codes("fn a() -> result<u32> { ok(1) }\nfn c() -> u32 { let v = a()?; v }") == ["KIFP041"]
    assert codes("fn a() -> result<u32> { ok(true) }") == ["KIFP012"]
    assert codes("fn a() -> result<u32> { ok(1) }\nfn f() -> u32 { match a() { ok(v) => v } }") == ["KIFP030"]


GENERIC = '''
fn max<T: ord>(a: T, b: T) -> T { if a > b { a } else { b } }
fn wrap_add<T: unsigned>(a: T, b: T) -> T { a + b }
fn neg<T: signed>(a: T) -> T { -a }
fn twice<T: unsigned>(a: T) -> T { wrap_add(a, a) }
fn zero<T: integer>() -> T { 0 }
fn conv<T: integer>(x: u32) -> T { x as T }
fn go() -> u8 { wrap_add(250u8, 10) }
fn go2() -> u8 { twice(200u8) }
fn go3() -> i8 { neg(-127i8) }
fn go4() -> u16 { conv::<u16>(70000) }
fn go5() -> i32 { max(3, 9) }
'''


def test_generic_calls_use_the_instantiated_type():
    m = mod(GENERIC)
    assert m.diagnostics == []
    assert run(m, "go") == 4
    assert run(m, "go2") == 144
    assert run(m, "go3") == 127
    assert run(m, "go4") == 70000 % 65536
    assert run(m, "go5") == 9


def test_generic_bounds_checked_at_the_definition_and_the_call():
    from kiflang.symbols import analyze as a
    codes = lambda t: [d.code for d in a("t.kif", t).diagnostics]
    pre = "fn ints<T: integer>(a: T) -> T { a }\nfn mk<T: integer>() -> T { 0 }\n"
    assert codes("fn s<T: ord>(a: T, b: T) -> T { a + b }") == ["KIFP064"]
    assert codes("fn s<T>(a: T) -> bool { a == a }") == ["KIFP064"]
    assert codes("fn s<T: unsigned>(a: T) -> T { -a }") == ["KIFP064"]
    assert codes("fn s<T>(a: T) -> T { let b = a; a }") == ["KIFP023"]
    assert codes(pre + "fn f() -> bool { ints(true) }") == ["KIFP062"]
    assert codes(pre + "fn f<U>(x: U) -> U { ints(x) }") == ["KIFP062"]
    assert codes(pre + "fn f<U: signed>(x: U) -> U { ints(x) }") == []
    assert codes(pre + "fn f() -> u32 { mk() }") == ["KIFP063"]
    assert codes(pre + "fn f() -> u32 { mk::<u32>() }") == []
    assert codes("fn max<T: ord>(a: T, b: T) -> T { a }\nfn f() -> u32 { max(1u32, 2u8) }") == ["KIFP011"]


def test_literal_only_generic_call_takes_the_expected_type():
    m = mod("fn wrap_add<T: unsigned>(a: T, b: T) -> T { a + b }\nfn go() -> u8 { wrap_add(250, 10) }")
    assert m.diagnostics == []
    assert run(m, "go") == 4


IFACE = '''
interface clock { method gettime() -> u32; }
struct h { v: u32 = 7 }
impl clock for h { fn gettime(&self) -> u32 { self.v } }
struct n {}
fn read<T: clock>(c: &T) -> u32 { c.gettime() + 1 }
fn bad<T: clock>(c: &T) -> u32 { c.nope() }
fn ok1() -> u32 { let x = h {}; read(&x) }
fn ok2() -> u32 { let x = n {}; read(&x) }
'''


def test_interface_bound_methods():
    from kiflang.symbols import analyze as a
    d = [(x.code, x.range.start.line) for x in a("t.kif", IFACE).diagnostics]
    assert d == [("KIFP064", 6), ("KIFP062", 8)]
    m = mod(IFACE.replace("fn bad", "fn bad_").replace("c.nope()", "0").replace("fn ok2() -> u32 { let x = n {}; read(&x) }", ""))
    assert m.diagnostics == []
    assert run(m, "ok1") == 8


EXT = '''
interface base { method a() -> u32; method d() -> u32 { 5 } }
#[extends(base)] interface derived { method b() -> u32; }
struct h { v: u32 = 3 }
impl derived for h { fn b(&self) -> u32 { 1 } fn a(&self) -> u32 { self.v } }
fn viaderived<T: derived>(x: &T) -> u32 { x.a() + x.b() + x.d() }
fn viabase<T: base>(x: &T) -> u32 { x.a() }
fn go() -> u32 { let x = h {}; viaderived(&x) * 10 + viabase(&x) }
'''


def test_interface_extends():
    from kiflang.symbols import analyze as a
    m = a("t.kif", EXT)
    assert m.diagnostics == []
    assert run(m, "go") == 93
    codes = lambda t: [d.code for d in a("t.kif", t).diagnostics]
    pre = "interface base { method a() -> u32; }\n"
    assert codes(pre + "#[extends(base)] interface d { method b() -> u32; }\nstruct h {}\n"
                       "impl d for h { fn b(&self) -> u32 { 1 } }") == ["KIFP026"]
    assert codes("#[extends(nope)] interface d { method b() -> u32; }") == ["KIFP010"]
    assert codes("#[extends(d)] interface d { method b() -> u32; }") == ["KIFP142"]
    assert codes(pre + "#[kobj, extends(base)] interface d { method b() -> u32; }") == ["KIFP141"]
    assert codes(pre + "#[extends(base)] interface d { method b() -> u32; }\nstruct h {}\n"
                       "impl d for h { fn b(&self) -> u32 { 1 } fn a(&self) -> u32 { 2 } }\n"
                       "fn f<T: base>(x: &T) -> u32 { x.a() }\nfn g() -> u32 { let x = h {}; f(&x) }") == []


def test_tuples():
    m = mod("""
        fn swap(t: (u32, u32)) -> (u32, u32) { let (a, b) = t; (b, a) }
        fn f() -> u32 {
            let mut t = (1, 2);
            t.0 = 10;
            let (x, mut y) = swap(t);
            y += 1;
            x * 100 + y + t.0
        }
        fn g(a: bool, b: bool) -> u32 {
            match (a, b) { (true, true) => 1, (true, false) => 2, (false, _) => 3 }
        }
        fn h(o: Option<u32>, f: bool) -> u32 {
            match (o, f) { (Some(n), true) => n, (Some(_), false) => 0, (None, _) => 9 }
        }
    """)
    assert run(m, "f") == 221
    assert [run(m, "g", a, b) for a, b in ((True, True), (True, False), (False, True))] == [1, 2, 3]


def test_tuple_errors():
    from tests.test_proc_symbols import codes

    assert "KIFP030" in codes("fn g(a: bool, b: bool) -> u8 { match (a, b) { (true, true) => 1, (false, _) => 3 } }")
    assert "KIFP010" in codes("fn g(t: (u8, u8)) -> u8 { t.2 }")
    assert "KIFP012" in codes("fn g(t: (u8, u8)) -> u8 { let (a, b, c) = t; a }")
    assert "KIFP012" in codes("fn g() -> (u8, u8) { (1, true) }")


def test_atomic_gaps():
    from tests.test_proc_symbols import codes
    # impl-level might_sleep: every method of the impl sleeps
    assert "KIFP110" in codes("""
        #[atomic_scope] struct g { x: u8 = 0 }
        impl g { fn drop(&mut self) {} }
        struct s { x: u8 = 0 }
        #[might_sleep] impl s { fn wait(&self) {} }
        fn f(v: &s) { let _a = g { x: 0 }; v.wait(); }
    """)
    # a guard holding a reference to an atomic_lock struct is an atomic scope
    assert "KIFP110" in codes("""
        #[atomic_lock] struct spin { x: u8 = 0 }
        struct held<'a> { l: &'a spin }
        impl<'a> held<'a> { fn drop(&mut self) {} }
        #[might_sleep] extern "C" fn nap() -> ();
        fn f(l: &spin) { let _h = held { l: l }; unsafe { nap(); } }
    """)
    # a sleepy function passed for an atomic_callback parameter
    assert "KIFP110" in codes("""
        #[might_sleep] extern "C" fn nap() -> ();
        fn slow() { unsafe { nap(); } }
        fn fast() {}
        fn reg(#[atomic_callback] cb: fn() -> ()) {}
        fn f() { reg(fast); reg(slow); }
    """)
    assert "KIFP110" not in codes("""
        fn fast() {}
        fn reg(#[atomic_callback] cb: fn() -> ()) {}
        fn f() { reg(fast); }
    """)
    # hooks of an atomic_entry method are checked as atomic
    assert "KIFP110" in codes("""
        #[might_sleep] extern "C" fn nap() -> ();
        interface i { before { unsafe { nap(); } } #[atomic_entry] method tick(now: u64) -> (); }
    """)


REFLECT_SRC = """
/// A clock.
/// Second line.
#[kobj, version(2)]
interface clock {
    /// Read the time.
    #[must_use(strict)] method now(a: u32, b: ptr<u8>) -> u64;
    static method make() -> ();
}
enum colour { red = 1, green }
struct pt { x: u32 = 0, y: u32 = 0 }

gen fn describe(m: &Module) -> str {
    let mut out = "";
    for i in m.interfaces() {
        out = out + format!("{} v{} doc={:?}\\n", i.name(), i.version(), i.doc());
        for me in i.methods() {
            out = out + format!(" {}#{} {} static={} ret={} attr={}\\n", me.name(), me.index(), me.signature(), me.is_static(),
                                me.ret().c_spelling(), me.attr("must_use").is_some());
            for p in me.params() {
                out = out + format!("  {}: {} {} {}\\n", p.name(), p.ty().kind(), p.ty().c_spelling(), p.ty().size_bits().unwrap_or(0));
            }
        }
    }
    for e in m.enums() { out = out + format!("enum {} {}\\n", e.name(), e.members().len()); }
    for s in m.structs() { out = out + format!("struct {} {}\\n", s.name(), s.fields().len()); }
    out
}
"""


def test_reflection():
    from kiflang.reflect import module_handle
    m = mod(REFLECT_SRC)
    assert m.diagnostics == []
    out = run(m, "describe", module_handle(m))
    assert 'clock v2 doc="A clock.\nSecond line."' in out
    assert " now#0 now(a: u32, b: ptr<u8>) -> u64 static=false ret=uint64_t attr=true" in out
    assert "  a: int uint32_t 32" in out and "  b: ptr uint8_t * 0" in out
    assert " make#1 make() -> () static=true ret=void attr=false" in out
    assert "enum colour 2" in out and "struct pt 2" in out


def test_match_guards_and_bindings():
    from tests.test_proc_symbols import codes
    m = mod("""
        enum colour { red, green, blue }
        fn f(n: u32) -> u32 {
            match n { 0 => 100, k if k > 10 => k * 2, k @ 1..=5 => k + 1000, other => other }
        }
        fn g(c: colour, bright: bool) -> u32 {
            match c { colour::red if bright => 1, colour::red => 2, x @ (colour::green | colour::blue) => 3 }
        }
        fn h(t: (u32, bool)) -> u32 {
            match t { (n, true) if n > 5 => 1, (_, true) => 2, (_, false) => 3 }
        }
    """)
    assert m.diagnostics == []
    assert [run(m, "f", n) for n in (0, 11, 3, 8)] == [100, 22, 1003, 8]
    assert [run(m, "h", t) for t in ((9, True), (1, True), (1, False))] == [1, 2, 3]
    # a guarded arm never counts toward exhaustiveness
    assert "KIFP030" in codes("fn f(b: bool) -> u8 { match b { true => 1, false if b => 2 } }")
    assert "KIFP012" in codes("fn f(n: u8) -> u8 { match n { k if k => 1, _ => 2 } }")
