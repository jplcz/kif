# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""C-mode restrictions (KIFP053) and construction-protocol signatures (KIFP086)."""

from kiflang.symbols import analyze

C = "#![target(c)]\n"


def codes(src: str):
    return sorted(d.code for d in analyze("t.kif", src).diagnostics)


def test_c_mode_allows_the_widened_subset():
    src = C + """
struct ring { buf: str, n: usize }
impl ring {
    fn try_construct(&mut self, cap: usize) -> result<()> { ok(()) }
    fn push(&mut self, b: u8) -> result<()> { ok(()) }
    fn size(&self) -> usize { self.n }
    fn drop(&mut self) {}
}
fn f() -> result<()> { let a: [u8; 4] = [0; 4]; ok(()) }
"""
    assert codes(src) == []


def test_c_mode_refusals():
    assert codes(C + "fn f<T>(x: T) {}") == ["KIFP053"]
    assert codes(C + "interface i { method m() -> u32; }\nstruct s {}\nimpl i for s { fn m(&self) -> u32 { 1 } }") == ["KIFP053"]
    assert codes(C + "fn f(a: allocator_ref) {}") == ["KIFP053"]
    assert codes(C + 'extern "C++" fn g();') == ["KIFP053"]


def test_cxx_only_code_is_skipped_in_c_mode():
    assert codes(C + "#[cfg(cxx)] fn f<T>(x: T) {}") == []


def test_cxx_mode_unrestricted():
    assert codes("fn f<T>(x: T) {}") == []


def test_protocol_shapes():
    ok = """struct s { n: usize }
impl s {
    fn try_create(n: usize) -> result<Self> { ok(Self { n: n }) }
    fn try_clone(&self) -> result<Self> { ok(Self { n: self.n }) }
    fn try_construct(&mut self, n: usize) -> result<()> { ok(()) }
    fn try_construct_at(mem: c_ptr<Self>, n: usize) -> result<()> { ok(()) }
}"""
    assert codes(ok) == []
    assert codes("struct s {}\nimpl s { fn try_create() -> usize { 0 } }") == ["KIFP086"]
    assert codes("struct s {}\nimpl s { fn try_construct(&self) -> result<()> { ok(()) } }") == ["KIFP086"]
    assert codes("struct s {}\nimpl s { fn try_construct_at(m: c_ptr<Self>) -> result<()> { ok(()) } }") == ["KIFP086"]
    assert codes("struct s {}\nimpl s { fn try_construct_at(mem: c_ptr<Self>) -> usize { 0 } }") == ["KIFP086"]


def test_kobj_interface_needs_a_kobj_target():
    from kiflang import target
    src = "#[kobj] interface i { method m() -> u32; }"

    def run(name):
        env = target.load(name).cfg_env()
        return sorted(d.code for d in analyze("t.kif", src, cfg_env=env).diagnostics)

    assert "KIFP051" in run("linux-kernel")
    assert "KIFP051" in run("c")
    assert "KIFP051" not in run("freebsd-kernel")
    assert "KIFP051" not in codes(src)


def test_layout_and_ops_attributes():
    assert codes("#[typedef] #[repr(c, packed)] #[align(16)] struct s { a: u8 }\n#[typedef] enum e: u8 { a }") == []
    assert codes("#[typedef] fn f() {}") == ["KIFP087"]
    assert codes("#[align(3)] struct s {}") == ["KIFP087"]
    assert codes("#[align(8)] enum e: u8 { a }") == ["KIFP087"]
    assert codes("#[repr(weird)] struct s {}") == ["KIFP087"]
    assert codes("#[ops] interface i { method m() -> u32; }") == []
    assert codes("#[ops] #[kobj] interface i { method m() -> u32; }") == ["KIFP087"]


def test_ops_interface_may_be_implemented_in_c_mode():
    src = C + "#[ops] interface i { method m() -> u32; }\nstruct s {}\nimpl i for s { fn m(&self) -> u32 { 1 } }"
    assert codes(src) == []
