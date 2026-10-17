# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Fact attributes (`#[some]`, `#[ensures(..)]`, `#[preserves]`, ...): accepted when they fit the signature, rejected otherwise."""

from test_proc_symbols import codes


def test_valid_facts_are_clean():
    src = ("#[ensures(some)]\nfn pick(x: u32) -> optional<u32> { some(x) }\n"
           "#[ensures(len_ge(1))]\nfn make(v: &[u32]) -> &[u32] { v }\n"
           "fn use_it(#[some] o: &optional<u32>, #[non_null] p: ptr<u32>, #[non_empty] v: &[u32], #[lt_len(v)] i: usize) -> u32 { 0 }\n"
           "#[preserves]\nfn bump(v: &mut [u32]) {}\n#[preserves(v)]\nfn bump2(v: &mut [u32], n: u32) {}\n")
    assert codes(src) == []


def test_bad_facts():
    assert codes("#[ensures(none)]\nfn a() -> u32 { 1 }") == ["KIFP210"]
    assert codes("#[ensures(some)]\nfn b() -> u32 { 1 }") == ["KIFP214"]
    assert codes("#[ensures(len_ge(x))]\nfn c(v: &[u32]) -> &[u32] { v }") == ["KIFP212"]
    assert codes("fn d(#[some] x: u32) {}") == ["KIFP211"]
    assert codes("fn d(#[lt_len(zz)] i: usize) {}") == ["KIFP213"]
    assert codes("fn d(#[len_ge] v: &[u32]) {}") == ["KIFP212"]
    assert codes("#[preserves(q)]\nfn e(a: &[u32]) {}") == ["KIFP213"]
    assert codes("#[preserves]\nfn f(a: u32) {}") == ["KIFP213"]


def test_api_attributes_in_an_extern_impl():
    ok = ('extern "C++" type box<T>;\nextern "C++" impl<T> box<T> {\n    #[when(true, some)] fn has(&self) -> bool;\n'
          '    #[len_of] fn len(&self) -> usize;\n    #[unchecked_form(raw_at, lt_len(i))] fn at(&self, i: usize) -> &T;\n'
          '    #[unsafe] fn raw_at(&self, i: usize) -> &T;\n}\n')
    assert codes(ok) == []
    bad = ok.replace("#[unsafe] fn raw_at", "fn raw_at")
    assert codes(bad) == ["KIFP216"]
    assert codes(ok.replace("when(true, some)", "when(true, bogus)")) == ["KIFP212"]
    assert codes(ok.replace("fn has(&self) -> bool", "fn has(&self) -> u32")) == ["KIFP214"]
    assert codes(ok.replace("lt_len(i)", "lt_len(j)")) == ["KIFP213"]
    assert codes(ok.replace("fn len(&self)", "fn len()")) == ["KIFP215"]


def test_prelude_uses_the_facts():
    import pathlib
    text = (pathlib.Path(__file__).parent.parent / "kiflang" / "prelude" / "reloco.kif").read_text()
    for want in ("#[when(true, some)] fn has_value", "unchecked_form(unsafe_value, some)", "unchecked_form(unsafe_get, non_null)",
                 "unchecked_form(unsafe_at, lt_len(i))", "#[when(false, non_empty)] fn is_empty", "#[len_of] fn len"):
        assert want in text
    assert codes(text) == []
