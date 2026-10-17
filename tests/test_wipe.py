# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`#[secure_wipe]` / `#[wipe_exempt]` declaration checks (docs/kif-secure-wipe.md, stage 1)."""

from tests.test_proc_symbols import analyze


def codes(text):
    return sorted(d.code for d in analyze(text).diagnostics)


def test_valid_declarations():
    assert codes("#[secure_wipe] struct k { b: [u8; 32], n: u64 }") == []
    assert codes("#[secure_wipe] fn f() { let a: u32 = 1; #[wipe_exempt] let b: u32 = 2; }") == []
    assert codes("fn f() { #[secure_wipe] let a: [u8; 16] = [0; 16]; }") == []
    assert codes("fn f(#[secure_wipe] x: u64) { }") == []
    assert codes("#[secure_wipe] struct a { x: u32 }\n#[secure_wipe] struct b { inner: a, raw: [a; 2] }") == []


def test_let_must_be_plain():
    assert codes("fn f(p: ptr<u8>) { #[secure_wipe] let a: ptr<u8> = p; }") == ["KIFW001"]
    assert codes("fn f(r: &u32) { #[secure_wipe] let a: &u32 = r; }") == ["KIFW001"]
    assert codes("struct g { }\nimpl g { fn drop(&mut self) { } }\nfn f() { #[secure_wipe] let a: g = g { }; }") == ["KIFW001"]
    assert codes("fn f() { #[secure_wipe] let (a, b) = (1, 2); }") != []


def test_param_must_be_plain():
    assert codes("fn f(#[secure_wipe] x: ptr<u8>) { }") == ["KIFW001"]


def test_secure_struct_with_drop():
    assert codes("#[secure_wipe] struct g { x: u32 }\nimpl g { fn drop(&mut self) { } }") == ["KIFW002"]
    assert codes("struct g { x: u32 }\nimpl g { fn drop(&mut self) { } }") == []


def test_secure_struct_fields():
    assert codes("#[secure_wipe] struct s { p: ptr<u8>, x: u32 }") == ["KIFW003"]
    assert codes("#[secure_wipe] struct s { #[wipe_exempt] p: ptr<u8>, x: u32 }") == []
    assert codes("struct g { x: u32 }\nimpl g { fn drop(&mut self) { } }\n#[secure_wipe] struct s { g: g }") == ["KIFW003"]


def test_exempt_without_effect():
    assert codes("struct s { #[wipe_exempt] x: u32 }") == ["KIFW004"]
    assert codes("#[secure_wipe] struct s { #[wipe_exempt] x: u32 }") == ["KIFW004"]
    assert codes("fn f() { #[wipe_exempt] let a: u32 = 1; }") == ["KIFW004"]
    assert codes("#[wipe_exempt] fn f() { }") == ["KIFW004"]
    assert codes("#[secure_wipe] enum e { a }") == ["KIFW004"]
    assert codes("struct s { #[secure_wipe] x: u32 }") == ["KIFW004"]


def test_shared_variables():
    assert codes("#[secure_wipe] #[tls] static X: u32 = 0;") == ["KIFW005"]


def test_target_wipe_defaults_and_overrides():
    from kiflang import target

    plan = target.load("c").wipe()
    assert plan.default and plan.error is None and plan.macro == "KIF_SECURE_WIPE"
    assert plan.body.startswith("memset((p), 0, (n)); __asm__ volatile") and plan.header == "<string.h>"
    plan = target.load("freebsd-kernel").wipe()
    assert plan.body == "explicit_bzero((p), (n))" and plan.header == "<sys/systm.h>" and not plan.default
    assert "volatile unsigned char" in plan.volatile_body


def test_target_wipe_barrier_rules():
    from kiflang import target

    t = target.load("c")
    t.data["wipe"] = {"barrier": ""}
    assert "no way to wipe" in t.wipe().error
    t.data["wipe"] = {"call": "hw_erase({p}, {n})", "barrier_after_call": True, "macro": "ZERO"}
    plan = t.wipe()
    assert plan.error is None and plan.macro == "ZERO" and plan.body.endswith("__asm__ volatile (\"\" : : \"r\"((p)) : \"memory\")")
    assert target.validate({"wipe": {"call": "f({x})"}}) != []


def test_wipe_instruction_text_hash_and_serialization():
    from kiflang import ir, irhash, serial

    loc = ir.Local(0, "k", "[u8; 32]", wipe=True)
    w = ir.Wipe(loc, ".bytes", "[u8; 32]", why="end of live range")
    assert ir._instr(w) == "wipe %k.bytes  // end of live range"
    assert irhash.HASHED["Wipe"] == ("local", "path", "ty", "volatile") and irhash.classified("Wipe")
    assert "wipe" in irhash.HASHED["Local"]
    assert ir.Wipe in set(serial._INSTRS.values())


def test_secure_struct_drop_is_found_wherever_it_is_declared():
    assert codes("impl g { fn drop(&mut self) { } }\n#[secure_wipe] struct g { x: u32 }") == ["KIFW002"]
    assert codes("#[secure_wipe] struct g { x: u32 }\nimpl g { fn a(&self) { } }\nimpl g { fn drop(&mut self) { } }") == ["KIFW002"]
    assert codes("#[secure_wipe] struct g<T> { x: u32 }\nimpl<T> g<T> { fn drop(&mut self) { } }") == ["KIFW002"]
    assert codes("#[secure_wipe] struct g { x: u32 }\nimpl g { fn new() -> g { g { x: 0 } } fn drop(&mut self) { } }") == ["KIFW002"]


# ---- the `wipes` pass (stage 2)

def wiped(src, name="f"):
    """The IR text of function `name` after the `wipes` pass, with the wipe lines as (text, line index) for order checks."""
    from kiflang import ir, symbols, wipes

    mod = ir.build_module(symbols.analyze("/t/main.kif", src, None).tree)
    assert not mod.skipped
    fn = next(f for f in mod.functions if f.name == name)
    wipes.analyze(fn, ir.CFG(fn))
    assert ir.verify(fn) == []
    return ir.dump(fn)


def wipe_lines(text):
    return [l.strip() for l in text.splitlines() if "wipe %" in l]


def test_wipe_goes_after_the_last_use_not_at_scope_end():
    t = wiped("fn f() -> u32 { #[secure_wipe] let a: u32 = 1; let b: u32 = a + 1; let c: u32 = b + 2; c }")
    lines = t.splitlines()
    w = next(n for n, l in enumerate(lines) if "wipe %a" in l)
    assert "load %a" in lines[w - 1] and "dead %a" not in "".join(lines[:w])
    assert len(wipe_lines(t)) == 1


def test_a_variable_read_in_a_loop_is_wiped_once_after_it():
    t = wiped("fn f(n: u32) -> u32 { #[secure_wipe] let mut a: u32 = 0; let mut i: u32 = 0; while i < n { a = a + i; i = i + 1; } let r: u32 = a; r }")
    assert len(wipe_lines(t)) == 1
    assert t.index("wipe %a") > t.index("b3.done")


def test_a_branch_that_does_not_use_it_wipes_on_its_edge():
    t = wiped("fn f(c: bool) -> u32 { #[secure_wipe] let a: u32 = 7; let mut r: u32 = 0; if c { r = a; } r }")
    assert len(wipe_lines(t)) == 2


def test_a_dead_store_is_wiped_right_after_it():
    t = wiped("fn f() { #[secure_wipe] let a: u32 = 7; }")
    lines = t.splitlines()
    w = next(n for n, l in enumerate(lines) if "wipe %a" in l)
    assert "store %a" in lines[w - 1] and "dead store" in lines[w]


def test_unused_parameter_is_wiped_at_entry():
    t = wiped("fn f(#[secure_wipe] k: u64, n: u64) -> u64 { n }")
    assert wipe_lines(t) == ["wipe %k  // entry: unused parameter of 'k'"]
    assert t.index("wipe %k") < t.index("load %n")


def test_function_level_attribute_covers_locals_only():
    t = wiped("#[secure_wipe] fn f(p: u64) -> u64 { let a: u64 = p + 1; #[wipe_exempt] let b: u64 = 2; a + b }")
    assert [l.split()[1] for l in wipe_lines(t)] == ["%a"]


def test_a_value_read_through_an_array_alias_is_wiped_after_the_read():
    t = wiped("fn f() -> u8 { #[secure_wipe] let buf: [u8; 4] = [1; 4]; buf[1] }")
    assert t.index("access index") < t.index("wipe %buf")


def test_a_kept_reference_wipes_at_scope_exit():
    t = wiped("fn g(r: &[u8; 4]) -> u8 { r[0] }\nfn f() -> u8 { #[secure_wipe] let buf: [u8; 4] = [1; 4]; let r: &[u8; 4] = &buf; g(r) }")
    assert wipe_lines(t) and t.index("call g") < t.index("wipe %buf")
