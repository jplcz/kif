# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Parser tests for the procedural language (docs/kif-proc-language.md section 3.1)."""

import pathlib
import re

import pytest

from kiflang import ast, parse

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "kif-proc-language.md"


def ok(text):
    f, diags = parse(text)
    assert not diags, [d.format("t") for d in diags]
    return f


def body_of(stmt_text):
    """The block of `fn t() { <stmt_text> }`."""
    f = ok("fn t() { " + stmt_text + " }")
    return f.items[0].body


def tail_of(expr_text):
    return body_of(expr_text).tail


def show(e):
    """Fully parenthesised rendering, to check precedence."""
    if isinstance(e, ast.Binary):
        return f"({show(e.left)} {e.op} {show(e.right)})"
    if isinstance(e, ast.Unary):
        return f"({e.op}{show(e.operand)})"
    if isinstance(e, ast.Cast):
        return f"({show(e.expr)} as {e.ty.text})"
    if isinstance(e, ast.Assign):
        return f"({show(e.target)} {e.op} {show(e.value)})"
    if isinstance(e, ast.RangeExpr):
        return f"({show(e.lo) if e.lo else ''}{'..=' if e.inclusive else '..'}{show(e.hi) if e.hi else ''})"
    if isinstance(e, ast.PathExpr):
        return e.text
    if isinstance(e, ast.Lit):
        return str(e.value)
    if isinstance(e, ast.Try):
        return f"({show(e.expr)}?)"
    return type(e).__name__


# ---------------------------------------------------------------- doc examples


def doc_blocks():
    blocks = re.findall(r"```kif\n(.*?)```", DOC.read_text(), re.S)
    # `{ ... }` stands for "elided" in the document; those blocks are not source.
    return [b for b in blocks if not re.search(r"\.\.\.\s*\}|\(\.\.\.\)|, \.\.\.[^)]", b) or 'extern "C' in b]


def test_doc_examples_parse():
    for n, b in enumerate(doc_blocks()):
        _, diags = parse(b)
        real = [d for d in diags if "'...'" not in d.message]
        assert not real, (n, [d.format("doc") for d in real], b)


# ---------------------------------------------------------------- items


def test_namespace_import_include_verbatim():
    f = ok('namespace a::b;\nimport "x.kif";\ninclude <sys/param.h>;\ninclude "y.h";\nheader {\nint x;\n}\n')
    assert [type(i).__name__ for i in f.items] == ["Namespace", "Import", "Include", "Include", "Verbatim"]
    assert [n.text for n in f.items[0].path] == ["a", "b"]
    assert f.items[2].system and not f.items[3].system
    assert f.items[4].text == "int x;"


def test_fn_generic_with_bounds_private_and_gen():
    f = ok("private fn a<T: integer + copy, U>(x: T, y: &mut U) -> T { x }\ngen fn g(i: &Interface) {}")
    a, g = f.items
    assert a.private and [p.name.text for p in a.generics] == ["T", "U"]
    assert [b.text for b in a.generics[0].bounds] == ["integer", "copy"]
    assert isinstance(a.params[1].ty, ast.RefType) and a.params[1].ty.mut
    assert g.is_gen and not g.private


def test_const_and_type_alias():
    f = ok("const N: usize = 4 * 2;\ntype result<T> = reloco::expected<T, error>;")
    assert isinstance(f.items[0], ast.ConstDecl) and show(f.items[0].value) == "(4 * 2)"
    alias = f.items[1]
    assert alias.name.text == "result" and [g.name.text for g in alias.generics] == ["T"]
    assert alias.ty.text == "reloco::expected"


def test_struct_with_defaults_private_and_attrs():
    f = ok("#[heap] struct ring { private head: u32 = 0, #[copy] data: [u8; 16], n: ptr<ring>; }")
    s = f.items[0]
    assert s.attrs[0].name.text == "heap"
    assert [x.name.text for x in s.fields] == ["head", "data", "n"]
    assert s.fields[0].private and s.fields[0].default is not None
    assert isinstance(s.fields[1].ty, ast.ArrayType)
    assert s.fields[1].attrs[0].name.text == "copy"


def test_enum_with_repr_and_values():
    f = ok("#[flags] enum perm : u8 { none = 0, read = 1, write = 2 }")
    e = f.items[0]
    assert e.repr.text == "u8" and [m.name.text for m in e.members] == ["none", "read", "write"]
    assert e.members[2].value.value == 2


def test_impl_and_impl_for():
    f = ok("impl ring { fn new() -> Self { ring { head: 0 } } fn len(&self) -> u32 { self.head } "
           "fn drop(&mut self) {} const K: u32 = 1; }\n"
           "impl clock for hpet { fn now(&self) -> u64 { 0 } }")
    plain, forr = f.items
    assert plain.iface is None and plain.target.text == "ring"
    assert [type(i).__name__ for i in plain.items] == ["FnDecl", "FnDecl", "FnDecl", "ConstDecl"]
    assert [p.receiver for p in plain.items[1].params + plain.items[2].params] == ["ref", "mut"]
    assert forr.iface.text == "clock" and forr.target.text == "hpet"


def test_extern_items():
    f = ok('extern "C" fn printf(fmt: cstr, ...) -> i32;\n'
           'extern "C" macro curthread_id() -> u32;\n'
           'extern "C++" fn net::packet_size(p: &net::packet) -> usize;\n'
           'extern "C++" type reloco::flat_map<K, V, A = allocator_ref>;\n'
           'extern "C++" type reloco::inline_vector<T, const N: usize>;\n'
           'extern "C++" impl<K, V> reloco::flat_map<K, V> { #[checked] fn len(&self) -> usize; }')
    printf, macro, pk, fm, iv, impl = f.items
    assert printf.decl.variadic and printf.abi == "C" and printf.kind == "fn"
    assert macro.kind == "macro"
    assert pk.decl.name.text == "net::packet_size" and pk.abi == "C++"
    assert [g.name.text for g in fm.generics] == ["K", "V", "A"] and fm.generics[2].default.text == "allocator_ref"
    assert iv.generics[1].const_ty.text == "usize"
    assert impl.fns[0].name.text == "len" and impl.fns[0].attrs[0].name.text == "checked"


def test_interface_old_and_new_members():
    f = ok('#[kobj, self(struct foo *), version(2)]\n'
           'interface mmio {\n'
           '  m_prolog {\n  raw c\n}\n'
           '  #[nosleep, deprecated("old")] override method a(off: u32) -> u64;\n'
           '  static method b() -> void default;\n'
           '  method c() -> i32 default impl_c;\n'
           '  before { trace(1); }\n'
           '  method d(x: u32) -> u32 { self.a(x) }\n'
           '}')
    i = f.items[0]
    assert [a.name.text for a in i.attrs] == ["kobj", "self", "version"]
    assert i.attrs[1].args is None and i.attrs[1].raw == "struct foo *"
    assert i.attrs[2].args[0].value.value == 2
    kinds = [type(m).__name__ for m in i.members]
    assert kinds == ["Verbatim", "Method", "Method", "Method", "Hook", "Method"]
    a, b, c, hook, d = i.methods[0], i.methods[1], i.methods[2], i.members[4], i.methods[3]
    assert a.override and a.attrs[0].name.text == "nosleep"
    assert b.static and b.has_default and b.default_sym is None
    assert c.default_sym.text == "impl_c"
    assert hook.kind == "before"
    assert d.body is not None and d.has_default


def test_old_clause_syntax_is_reported():
    _, diags = parse("interface a { self int; method m() -> void; }")
    assert any(d.code == "KIF087" for d in diags)


def test_foreign_and_register_block():
    f = ok('#[driver(x)] foreign class dev;\n'
           '#[size(4)] block uart { #[offset(0)] register ctl: u32 { enable: 0..0; mode: 1..3; } }')
    assert f.items[0].name.text == "dev"
    b = f.items[1]
    assert b.registers[0].ty.text == "u32"
    assert [(x.name.text, x.lo, x.hi) for x in b.registers[0].fields] == [("enable", 0, 0), ("mode", 1, 3)]


def test_inner_attributes_and_attribute_forms():
    f = ok('#![small_stack(4096)]\n#[unsafe(relocatable)] struct s { a: u32 }\n'
           '#[deprecated = "x", header("a.h"), derive(clone, Debug)] fn f() {}')
    assert f.inner_attrs[0].inner and f.inner_attrs[0].args[0].value.value == 4096
    assert f.items[0].attrs[0].name.text == "unsafe" and f.items[0].attrs[0].args[0].name == "relocatable"
    a = f.items[1].attrs
    assert a[0].raw == '"x"' and a[1].args[0].value.value == "a.h"
    assert [x.name for x in a[2].args] == ["clone", "Debug"]


def test_param_attributes():
    f = ok("fn check(cond: bool, #[caller_location] at: call_location_ref) {}")
    assert f.items[0].params[1].attrs[0].name.text == "caller_location"


# ---------------------------------------------------------------- types


def test_types():
    f = ok("fn t(a: &u8, b: &mut [u32; 4], c: fn(u8, u16) -> i32, d: (), e: vector<vector<u8>>, "
           "f: reloco::flat_map<K, 4, {N + 1}>, g: Self) {}")
    t = [p.ty for p in f.items[0].params]
    assert isinstance(t[0], ast.RefType) and not t[0].mut
    assert isinstance(t[1], ast.RefType) and t[1].mut and isinstance(t[1].inner, ast.ArrayType)
    assert isinstance(t[2], ast.FnType) and len(t[2].params) == 2 and t[2].ret.text == "i32"
    assert isinstance(t[3], ast.TupleType) and not t[3].elems
    assert t[4].segments[0].args[0].segments[0].args[0].text == "u8"
    args = t[5].segments[-1].args
    assert isinstance(args[1], ast.ConstArg) and isinstance(args[2], ast.ConstArg)
    assert t[6].text == "Self"


# ---------------------------------------------------------------- expressions


@pytest.mark.parametrize("src, want", [
    ("a + b * c", "(a + (b * c))"),
    ("a * b + c", "((a * b) + c)"),
    ("a - b - c", "((a - b) - c)"),
    ("a << 1 + 2", "(a << (1 + 2))"),
    ("a & b == c", "((a & b) == c)"),
    ("a || b && c", "(a || (b && c))"),
    ("a | b ^ c & d", "(a | (b ^ (c & d)))"),
    ("-a as u8", "((-a) as u8)"),
    ("!a == b", "((!a) == b)"),
    ("a as u8 + 1", "((a as u8) + 1)"),
    ("a >> 2", "(a >> 2)"),
    ("a >= 2", "(a >= 2)"),
    ("a > b", "(a > b)"),
    ("a = b = c", "(a = (b = c))"),
    ("a >>= 1", "(a >>= 1)"),
    ("a <<= 1", "(a <<= 1)"),
    ("x += 1 + 2", "(x += (1 + 2))"),
    ("0..n + 1", "(0..(n + 1))"),
    ("0..=9", "(0..=9)"),
    ("*p = 3", "((*p) = 3)"),
    ("f()?", "(Call?)"),
])
def test_precedence(src, want):
    assert show(tail_of(src)) == want


def test_comparison_chain_is_an_error():
    _, diags = parse("fn t() { a < b < c; }")
    assert [d.code for d in diags] == ["KIF001"]


def test_unary_ref_forms():
    e = tail_of("&mut x")
    assert isinstance(e, ast.Unary) and e.op == "&mut"
    e = tail_of("&&x")
    assert e.op == "&" and e.operand.op == "&"


def test_postfix_chain_calls_fields_index_turbofish():
    e = tail_of("a.b.c(1, 2)[3].d::<u8>(x)?")
    assert isinstance(e, ast.Try)
    m = e.expr
    assert isinstance(m, ast.MethodCall) and m.name.text == "d" and m.turbofish[0].text == "u8"
    idx = m.receiver
    assert isinstance(idx, ast.Index) and isinstance(idx.expr, ast.MethodCall)
    assert isinstance(idx.expr.receiver, ast.FieldAccess)


def test_path_calls_and_type_prefixed_paths():
    e = tail_of("Self::new(1)")
    assert isinstance(e, ast.Call) and e.callee.text == "Self::new"
    e = tail_of("ptr<ring>::try_allocate(a, 0)")
    assert e.callee.segments[0].args[0].text == "ring" and e.callee.text == "ptr::try_allocate"
    e = tail_of("f::<u8>(x)")
    assert e.callee.segments[0].args[0].text == "u8"
    e = tail_of("a < b")
    assert show(e) == "(a < b)"


def test_literals():
    assert tail_of("10u8").suffix == "u8"
    assert tail_of('"a\\n"').value == "a\n"
    assert tail_of("true").value is True
    assert isinstance(tail_of("()"), ast.TupleExpr)
    assert show(tail_of("(a + b) * c")) == "((a + b) * c)"


def test_arrays_and_struct_literals():
    a = tail_of("[0; 16]")
    assert isinstance(a, ast.ArrayLit) and a.repeat is not None and len(a.elems) == 1
    assert len(tail_of("[1, 2, 3]").elems) == 3
    s = tail_of("S { a: 1, b, ..base }")
    assert isinstance(s, ast.StructLit) and [f.name.text for f in s.fields] == ["a", "b"]
    assert s.fields[1].value is None and s.base is not None
    assert isinstance(tail_of("S {}"), ast.StructLit)


def test_struct_literal_not_taken_in_conditions():
    f = ok("fn t() { if x == y { a } else { b } while c { } for i in 0..n { } match v { _ => 1 } }")
    stmts = f.items[0].body.stmts
    assert [type(s.expr).__name__ for s in stmts] == ["If", "While", "For"]
    assert isinstance(f.items[0].body.tail, ast.Match)


def test_macros():
    e = tail_of('write!(sink, "{}", x)')
    assert isinstance(e, ast.MacroCall) and e.name.text == "write" and len(e.args) == 3
    e = tail_of("here!()")
    assert isinstance(e, ast.MacroCall) and not e.args


def test_match_patterns():
    m = tail_of("match a { 1 | 2 => x(), 3..=5 => { y() } state::idle => 0, -1 => 1, _ => panic!(\"no\"), }")
    kinds = [type(a.pattern).__name__ for a in m.arms]
    assert kinds == ["OrPat", "RangePat", "PathPat", "LitPat", "WildPat"]
    assert m.arms[2].pattern.path.text == "state::idle"
    assert m.arms[3].pattern.lit.value == -1


def test_if_else_chain_and_loops():
    e = tail_of("if a { 1 } else if b { 2 } else { 3 }")
    assert isinstance(e, ast.If) and isinstance(e.otherwise, ast.If) and isinstance(e.otherwise.otherwise, ast.Block)
    f = ok("fn t() { loop { break; } while i < 3 { continue; } for j in 0..=3 { return; } }")
    body = f.items[0].body
    assert [type(s.expr).__name__ for s in body.stmts] == ["Loop", "While"] and isinstance(body.tail, ast.For)
    r = tail_of("return")
    assert isinstance(r, ast.Return) and r.value is None
    r = tail_of("return 1 + 2")
    assert show(r.value) == "(1 + 2)"


def test_statements_let_item_unsafe_and_tail():
    b = body_of("let a = 1; let mut b: u8; fn inner() {} unsafe { w(&mut a) } a + b")
    assert [type(s).__name__ for s in b.stmts] == ["Let", "Let", "ItemStmt", "ExprStmt"]
    assert b.stmts[1].mut and b.stmts[1].ty.text == "u8" and b.stmts[1].init is None
    assert isinstance(b.stmts[3].expr, ast.UnsafeBlock)
    assert show(b.tail) == "(a + b)"


def test_block_like_statement_does_not_swallow_next_line():
    b = body_of("if a { x(); }\n*p = 3;\n")
    assert [type(s.expr).__name__ for s in b.stmts] == ["If", "Assign"]


def test_block_like_as_value_in_let():
    b = body_of("let v = if a { 1 } else { 2 }; v")
    assert isinstance(b.stmts[0].init, ast.If)


# ---------------------------------------------------------------- recovery and diagnostics


def test_recovers_after_bad_statement():
    f, diags = parse("fn t() { let = 3; let ok = 1; ok }\nfn after() {}")
    assert [d.code for d in diags] == ["KIF001"]
    assert [i.name.text for i in f.items] == ["t", "after"]
    assert len(f.items[0].body.stmts) == 1 and f.items[0].body.tail is not None


def test_recovers_after_bad_item():
    f, diags = parse("fn { oops }\nenum e { a }\nfn good() {}")
    assert diags
    assert [type(i).__name__ for i in f.items] == ["EnumDecl", "FnDecl"]


def test_recovers_inside_interface_and_impl():
    f, diags = parse("interface a { method ; method m() -> i32; }\nimpl s { fn ( ); fn ok() {} }")
    assert diags
    assert [m.name.text for m in f.items[0].methods] == ["m"]
    assert [i.name.text for i in f.items[1].items] == ["ok"]


def test_keyword_as_name_and_bad_names():
    _, diags = parse("fn match() {}")
    assert [d.code for d in diags] == ["KIF003"]
    _, diags = parse("interface Bad { }")
    assert [d.code for d in diags] == ["KIF003"]
    _, diags = parse("interface a__b { }")
    assert [d.code for d in diags] == ["KIF003"]


def test_attributes_on_non_items_are_reported():
    _, diags = parse('#[x] import "a.kif";')
    assert [d.code for d in diags] == ["KIF086"]


def test_unterminated_things_do_not_hang():
    for src in ["fn t() {", "interface a {", "struct s {", "enum e {", "impl s {", "fn t() { match x {", "#[a", "extern"]:
        parse(src)


def test_truncated_and_suffix_inputs_never_crash():
    for b in doc_blocks():
        for k in range(0, len(b), 23):
            parse(b[:k])
            parse(b[k:])


def test_lifetime_syntax():
    tree, d = parse("fn f<'a, 'b: 'a, T>(x: &'a mut T, s: &'b u8) -> &'a T { x }\n"
                    "impl<'a> v<'a> { fn g(&'a self) {} fn h(&'a mut self) {} }\nstruct v<'a> { r: &'a u8, w: w<'a, u8> }")
    assert d == []
    f = tree.items[0]
    assert [g.name.text for g in f.generics] == ["'a", "'b", "T"]
    assert f.generics[0].is_lifetime and [o.text for o in f.generics[1].outlives] == ["'a"]
    assert f.params[0].ty.lifetime.text == "'a" and f.params[0].ty.mut
    assert tree.items[1].items[0].params[0].lifetime.text == "'a"


def test_lone_quote_is_still_invalid():
    _, d = parse("fn f(x: &' u8) {}")
    assert d and d[0].code == "KIF004"


def test_slice_type():
    tree, d = parse("fn f(a: &'a mut [u8], b: &[[u8; 4]], c: [u8; 2]) {}")
    assert d == []
    ps = tree.items[0].params
    assert isinstance(ps[0].ty.inner, ast.SliceType) and ps[0].ty.mut
    assert isinstance(ps[1].ty.inner.elem, ast.ArrayType)
    assert isinstance(ps[2].ty, ast.ArrayType)


def test_lambdas():
    e = tail_of("|a: i32, b: i32| a + b")
    assert isinstance(e, ast.Lambda) and [p.name.text for p in e.params] == ["a", "b"]
    assert isinstance(e.body, ast.Binary) and e.ret is None
    assert tail_of("|| 1").params == []
    assert isinstance(tail_of("|x| -> i32 { x }").body, ast.Block)
    call = tail_of("each(items, 4, |r: &request| ())")
    assert isinstance(call.args[2], ast.Lambda)
    assert isinstance(tail_of("a || b"), ast.Binary)  # `||` is still the binary operator in infix position
    assert isinstance(tail_of("|a| a | 1").body, ast.Binary)
    _, diags = parse("fn t() { |a, | a }")
    assert not diags
    _, diags = parse("fn t() { |a a }")
    assert diags


# -- `use` (docs/kif-proc-language.md 3.2)

def uses(text):
    return [(["::".join(n.text for n in e.path)], e.alias.text if e.alias else None, e.glob)
            for it in ok(text).items for e in it.entries]


def test_use_single_alias_and_glob():
    assert uses("use a::b::c;") == [(["a::b::c"], None, False)]
    assert uses("use a::b as d;") == [(["a::b"], "d", False)]
    assert uses("use a::b::*;") == [(["a::b"], None, True)]


def test_use_group_is_flattened_and_nests():
    assert uses("use a::{b, c as d, e::*, f::{g, h}};") == [
        (["a::b"], None, False), (["a::c"], "d", False), (["a::e"], None, True),
        (["a::f::g"], None, False), (["a::f::h"], None, False)]
    assert uses("use a::{b, c,};") == [(["a::b"], None, False), (["a::c"], None, False)]  # trailing comma


@pytest.mark.parametrize("text", ["use *;", "use ;", "use a::;", "use a::{b", "use a::b as;", "use a::b"])
def test_use_errors_are_reported_not_raised(text):
    _, diags = parse(text)
    assert diags


def test_use_is_a_keyword_and_attributes_are_rejected():
    _, diags = parse("#[x] use a::b;")
    assert [d.code for d in diags] == ["KIF086"]
    _, diags = parse("fn use() {}")
    assert diags[0].code == "KIF003"


def test_language_tagged_raw_blocks():
    f = ok("header(c) {\nint c_only;\n}\ncode(cxx) {\nint cxx_only;\n}\nheader {\nint both;\n}\n"
           "interface i {\n  code(c) {\nint x;\n}\n  method m() -> ();\n}\n")
    v = [i for i in f.items if isinstance(i, ast.Verbatim)]
    assert [(x.kind, x.target, x.text) for x in v] == [
        ("header", "c", "int c_only;"), ("code", "cxx", "int cxx_only;"), ("header", None, "int both;")]
    inner = f.items[-1].members[0]
    assert (inner.kind, inner.target) == ("code", "c")


def test_language_tag_errors():
    for src, code in (("header(rust) {\nx\n}\n", "KIF090"), ("interface i {\n  m_prolog(c) {\nx\n}\n  method m() -> ();\n}\n", "KIF090")):
        _, diags = parse(src)
        assert [d.code for d in diags] == [code], src


def test_parser_recovers_from_a_stray_closer_in_an_interface():
    _, diags = parse("interface a { method m(x: c_ptr<struct res>) -> (); method n() -> (); }")
    assert diags  # `struct` is a keyword; the point is that parsing terminates
