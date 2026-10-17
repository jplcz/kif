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
