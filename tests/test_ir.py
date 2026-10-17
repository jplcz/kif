# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The mid-level IR (kiflang/ir.py) and its data encoding (kiflang/serial.py)."""

import json
import pathlib

import pytest

from kiflang import ir, serial, symbols
from kiflang.parser import parse

SRC = """fn f(a: [u32; 4], i: usize, o: optional<u32>, v: &[u32]) -> u32 {
    let mut s: u32 = a[2];
    if o.has_value() { let x: u32 = o.value(); }
    let mut k: usize = 0;
    while k < v.len() { s = v[k]; k += 1; }
    a[i]
}
fn g(p: ptr<u32>) -> bool { p.is_null() }
"""


def build(src=SRC):
    m = symbols.analyze("/t/main.kif", src, None)
    return ir.build_module(m.tree), m


def test_build_and_verify():
    mod, _ = build()
    assert [f.name for f in mod.functions] == ["f", "g"] and not mod.skipped
    for f in mod.functions:
        assert ir.verify(f) == []
    text = ir.dump(mod.functions[0])
    assert "check index 2, 4" in text and "check some" in text and "access unwrap" in text and "len " in text


def test_checks_are_explicit():
    f = build()[0].functions[0]
    kinds = [(c.kind, [str(a) for a in c.args][1:]) for _, c in f.checks()]
    assert ("index", ["4"]) in kinds and sum(1 for k, _ in kinds if k == "index") == 3 and ("some", []) in kinds
    assert all(c.proof is None for _, c in f.checks())


def test_cfg_dominators_and_loops():
    f = build()[0].functions[0]
    cfg = ir.CFG(f)
    byname = {b.label: b for b in f.blocks if b.label}
    assert cfg.dominates(f.entry, byname["body"]) and not cfg.dominates(byname["then"], byname["done"])
    assert cfg.dominates(byname["while"], byname["body"])
    assert cfg.loop_headers() == {byname["while"]}


def test_unsupported_function_is_skipped_not_an_error():
    mod, _ = build('gen fn h() -> u32 { 1 }\nfn ok() -> u32 { 1 }')
    assert [f.name for f in mod.functions] == ["ok"] and mod.skipped[0][0] == "h"


SAFETY_SRC = """enum mode { a, b }
struct pt { x: u32, y: u32 }
impl pt { fn bump(&mut self) { self.x += 1; } fn get(&self) -> u32 { self.x } }
fn pick(m: mode, v: i32) -> i32 { match m { mode::a => v / 2, mode::b if v > 3 => -v, _ => v << 1 } }
fn sum(n: u32) -> u32 { let mut t: u32 = 0; for i in 0..n { t += i; } t }
fn mv(p: &mut pt, q: pt) -> u32 { p.x = 1; let r = &mut p.y; *r = 5; p.x + q.x }
fn el(a: &mut [u32], i: usize) { a[i] = 3; a[i] += 1; let s = &a[1..i]; let u = &a[..=i]; }
fn pv(v: &mut vector<u32>, o: optional<u32>) { v.try_push(1); let s = "hi"; panic!("x"); }
fn pd(p: ptr<u32>) -> u32 { *p }
fn rd(v: &vector<u32>) -> usize { v.len() }
"""


def instrs(f, cls):
    return [i for b in f.blocks for i in b.instrs if isinstance(i, cls)]


def test_everything_that_changes_a_slot_is_visible():
    mod, _ = build(SAFETY_SRC)
    assert not mod.skipped and all(ir.verify(f) == [] for f in mod.functions)
    fns = {f.name: f for f in mod.functions}
    assert {"bump", "get"} <= set(fns)
    # `p.x = 1` and `a[i] = 3` write into a slot without storing the whole of it
    assert [str(c.local) for c in instrs(fns["mv"], ir.Clobber)] == ["%p", "%r"]
    # a `&mut self` method call and `&mut p.y` borrow the root mutably; a `&self` method does not
    assert [(str(b.local), b.mut) for b in instrs(fns["mv"], ir.Borrow)] == [("%p", True)]
    assert [(str(b.local), b.mut) for b in instrs(fns["pv"], ir.Borrow)] == [("%v", True)]
    assert [(str(b.local), b.mut) for b in instrs(fns["el"], ir.Borrow)] == [("%a", False), ("%a", False)]


def test_writes_and_ranges_keep_their_checks():
    fns = {f.name: f for f in build(SAFETY_SRC)[0].functions}
    # a[i] = 3 and a[i] += 1 each check the index, and both range borrows check their range
    assert [c.kind for _, c in fns["el"].checks()] == ["index", "index", "range", "range"]
    assert [c.kind for _, c in fns["pd"].checks()] == ["non_null"]


def test_control_flow_constructs_form_a_cfg():
    fns = {f.name: f for f in build(SAFETY_SRC)[0].functions}
    assert ir.CFG(fns["sum"]).loop_headers() == {next(b for b in fns["sum"].blocks if b.label == "for")}
    assert not instrs(fns["pick"], ir.Opaque)
    assert [p.pattern for p in instrs(fns["pick"], ir.PatTest)] == ["mode::a", "mode::b"]
    assert isinstance(next(b for b in fns["pv"].blocks).term, ir.Unreachable)  # panic! diverges


def test_try_closures_and_literals_build():
    mod, _ = build("fn a(r: result<u32>) -> result<u32> { let x = r?; let f = |y: u32| -> u32 { y }; let t: [u32; 2] = [1, 2]; ok(x) }")
    assert not mod.skipped and ir.verify(mod.functions[0]) == []
    f = mod.functions[0]
    assert [o.what for o in instrs(f, ir.Opaque)] == ["lambda"]
    assert [i.kind for i in kinds(f, ir.Make)] == ["array"] and [i.kind for i in kinds(f, ir.Test)] == ["failed"]
    assert sum(isinstance(b.term, ir.Return) for b in f.blocks) == 2  # the `?` early return and the final one


@pytest.mark.parametrize("as_cbor", [False, True])
def test_new_instructions_round_trip(as_cbor):
    mod, _ = build(SAFETY_SRC)
    back = serial.loads_ir(serial.dumps_ir(mod, as_cbor=as_cbor))
    assert [ir.dump(f) for f in back.functions] == [ir.dump(f) for f in mod.functions]


def test_verify_catches_damage():
    f = build()[0].functions[1]
    f.blocks[0].term = None
    assert "no terminator" in ir.verify(f)[0]
    f = build()[0].functions[0]
    f.blocks[0].instrs.append(ir.Check("index", []))
    assert any("bad check" in e for e in ir.verify(f))
    f = build()[0].functions[1]
    f.blocks[0].instrs.insert(0, ir.Store(f.params[0], ir.Temp(999)))
    assert any("never defined" in e for e in ir.verify(f))


@pytest.mark.parametrize("as_cbor", [False, True])
def test_ir_round_trip(as_cbor):
    mod, _ = build()
    next(iter(mod.functions[0].checks()))[1].proof = "constant"
    data = serial.dumps_ir(mod, as_cbor)
    assert isinstance(data, bytes if as_cbor else str)
    back = serial.loads_ir(data)
    assert ir.dump_module(back) == ir.dump_module(mod)
    assert next(iter(back.functions[0].checks()))[1].proof == "constant"
    assert back.functions[0].blocks[0].instrs[0].site.range == mod.functions[0].blocks[0].instrs[0].site.range


def test_ir_decode_rejects_bad_documents():
    mod, _ = build()
    d = serial.ir_to_data(mod)
    for edit, msg in ((lambda x: x.update(format="nope"), "not a"), (lambda x: x.update(version=9), "version"),
                      (lambda x: x["functions"][0]["blocks"][0]["instrs"][0].update(instr="Bogus"), "unknown instr"),
                      (lambda x: x["functions"][0]["blocks"][0]["instrs"][0].update(dst={"t": 4242}), "dangling"),
                      (lambda x: x["functions"][0]["blocks"][0]["instrs"][0].update(extra=1), "do not match"),
                      (lambda x: x["functions"][0]["blocks"][0].update(term=None), "no terminator")):
        bad = json.loads(json.dumps(d))
        edit(bad)
        with pytest.raises(serial.SerialError, match=msg):
            serial.ir_from_data(bad)


@pytest.mark.parametrize("as_cbor", [False, True])
def test_ast_round_trip(as_cbor):
    for text in (SRC, (pathlib.Path(__file__).parent.parent / "kiflang" / "prelude" / "reloco.kif").read_text()):
        tree = parse(text)[0] if isinstance(parse(text), tuple) else parse(text)
        back = serial.loads_ast(serial.dumps_ast(tree, as_cbor))
        assert serial.ast_to_data(back) == serial.ast_to_data(tree)
        assert back == tree


def test_ast_decode_rejects_bad_documents():
    tree = parse(SRC)[0] if isinstance(parse(SRC), tuple) else parse(SRC)
    d = serial.ast_to_data(tree)
    with pytest.raises(serial.SerialError):
        serial.ast_from_data({"k": "Nope"})
    bad = json.loads(json.dumps(d))
    bad["items"][0].pop("name")
    with pytest.raises(serial.SerialError, match="do not match"):
        serial.ast_from_data(bad)
    with pytest.raises(serial.SerialError):
        serial.loads_ast("{not json")


OWN = """#[derive(Debug)]
struct buf { n: u32, w: [u32; 2] }
fn eat(b: buf) -> u32 { b.n }
fn pass(b: buf) -> u32 {
    let c = b;
    eat(c)
}
fn scopes(n: u32) -> u32 {
    let mut s: u32 = 0;
    let mut k: u32 = 0;
    while k < n {
        let t: u32 = k;
        if t == 3 { break; }
        if t == 4 { continue; }
        s += t;
        k += 1;
    }
    { let inner: u32 = 1; s += inner; }
    s
}
fn parts(n: u32) -> u32 {
    let mut b = buf { n: n, w: [0, 0] };
    b.n = 1;
    b.w[0] = 2;
    let r = &b.n;
    let m = &mut b.w[1];
    *m = 3;
    *r
}
"""


def kinds(f, cls):
    return [i for _, i in f.instrs() if isinstance(i, cls)]


def named(src):
    mod, _ = build(src)
    return {f.name: f for f in mod.functions}, mod


def test_a_move_is_a_load_that_hands_the_value_on():
    fns, _ = named(OWN)
    mv = kinds(fns["pass"], ir.Move)
    assert [str(i.local) for i in mv] == ["%b", "%c"]
    assert all(isinstance(i, ir.Load) for i in mv)
    assert "= move %c" in ir.dump(fns["pass"])
    assert not kinds(fns["scopes"], ir.Move)  # integers are copied, never moved


def test_every_binding_goes_live_and_dies_with_its_scope():
    fns, mod = named(OWN)
    f = fns["scopes"]
    assert ir.verify(f) == []
    lives, deads = [str(i.local) for i in kinds(f, ir.Live)], [str(i.local) for i in kinds(f, ir.Dead)]
    assert lives[:2] == ["%s", "%k"] and "%t" in lives and "%inner" in lives
    assert "%t" in deads and "%inner" in deads and "%s" in deads and "%n" not in deads  # parameters never die
    inits = {str(i.local) for i in kinds(f, ir.Store) if i.init}
    assert {"%s", "%t"} <= inits and "// init" in ir.dump(f)
    assert any(not i.init for i in kinds(f, ir.Store))  # `s += t` is a reassignment
    # a `break` and a `continue` kill `t` before they leave the loop body
    for b in f.blocks:
        if b.term is not None and isinstance(b.term, ir.Jump) and b.term.site is not None and type(b.term.site).__name__ in ("Break", "Continue"):
            assert any(isinstance(i, ir.Dead) and str(i.local) == "%t" for i in b.instrs)


def test_a_return_kills_the_locals_after_computing_its_value():
    fns, _ = named("fn f(a: u32) -> u32 { let x: u32 = a; if x == 1 { let y: u32 = 2; return y; } x }")
    f = fns["f"]
    ret = next(b for b in f.blocks if isinstance(b.term, ir.Return) and b.term.site is not None)
    tail = [str(i.local) for i in ret.instrs if isinstance(i, ir.Dead)]
    assert tail == ["%y", "%x"]  # innermost scope first, then the enclosing one
    loads = [i for i in ret.instrs if isinstance(i, ir.Load)]
    assert loads and ret.instrs.index(loads[-1]) < ret.instrs.index(next(i for i in ret.instrs if isinstance(i, ir.Dead)))


def test_borrows_and_writes_name_the_part_of_the_slot():
    fns, _ = named(OWN)
    f = fns["parts"]
    assert [(str(i.local), i.mut, i.path) for i in kinds(f, ir.Borrow)] == [("%b", False, ".n"), ("%b", True, ".w[]")]
    assert [i.path for i in kinds(f, ir.Clobber)] == [".n", ".w[]", "*"]
    assert "borrow mut %b.w[]" in ir.dump(f)


def test_the_new_instructions_survive_the_data_encoding():
    _, mod = named(OWN)
    back = serial.loads_ir(serial.dumps_ir(mod))
    assert ir.dump_module(back) == ir.dump_module(mod)
    assert [type(i).__name__ for _, i in back.functions[0].instrs()] == [type(i).__name__ for _, i in mod.functions[0].instrs()]


def test_types_are_structured_and_round_trip():
    from kiflang import irtypes as it

    for text in ("u32", "bool", "()", "&u32", "&mut [u8]", "[u32; 4]", "[u32; _]", "optional<u32>", "result<vector<u8>, error>",
                 "ptr<buf>", "(u32, &str)", "fn(u32, bool) -> u8", "fn()", "dyn clock", "?", "vector<optional<u8>>"):
        assert str(it.parse_type(text)) == text, text
    t = it.parse_type("&mut [u32]")
    assert t.kind == "ref" and t.mut and t.target.kind == "slice" and t.target.target.is_unsigned and t.target.target.bits == 32
    assert it.parse_type("i64").is_signed and it.parse_type("usize").bits == 64 and not it.parse_type("bool").is_integer
    assert it.parse_type("vector<u8>").is_sequence and it.parse_type("vector<u8>").target.name == "u8"
    assert it.parse_type("[u32;").is_unknown and it.parse_type("u32 u32").is_unknown and it.parse_type(None).is_unknown


def test_locals_and_temps_carry_their_type():
    fns, _ = named("fn f(a: [u32; 4], v: &[u32], o: optional<u32>, i: usize) -> u32 { let x = o; let n = v.len(); a[i] }")
    f = fns["f"]
    by = {str(p): p for p in f.locals}
    assert str(by["%a"].type) == "[u32; 4]" and by["%a"].type.length == 4
    assert str(by["%v"].type) == "&[u32]" and by["%v"].type.target.target.name == "u32"
    assert str(by["%o"].type) == "optional<u32>" and by["%o"].type.target.name == "u32"
    assert by["%i"].type.is_unsigned and by["%x"].type.name == "optional"
    assert f.ret_type.name == "u32" and named("fn g() { }")[0]["g"].ret_type.kind == "unit"
    assert all(i.dst.type.is_unknown or str(i.dst.type) for _, i in f.instrs() if getattr(i, "dst", None) is not None)


VALUES = """#[derive(Debug)]
struct pt { x: u32, y: u32 }
enum mode { a, b }
fn text() -> str_view { "hi" }
fn named() -> mode { mode::b }
fn build(n: u32) -> u32 {
    let p = pt { x: n, y: 2 };
    let q = pt { x: 1, ..p };
    let t = (n, p.y);
    let a = [n, 2, 3];
    let z = [0; 4];
    let r = 1..n;
    let (u, v) = t;
    u + v + a[0] + z[1] + q.x
}
fn opt(o: optional<u32>) -> u32 { match o { Some(x) => x, None => 0 } }
fn pat(m: u32) -> u32 { match m { 0 => 1, 1..=9 => 2, _ => 3 } }
fn each(v: &[u32]) -> u32 { let mut s: u32 = 0; for x in v { s += *x; } s }
"""


def test_values_are_real_nodes_not_opaque():
    fns, _ = named(VALUES)
    assert [i.text for i in kinds(fns["text"], ir.Str)] == ["hi"]
    assert [i.name for i in kinds(fns["named"], ir.Global)] == ["mode::b"]
    mk = [(i.kind, i.names) for i in kinds(fns["build"], ir.Make)]
    assert mk == [("struct", ["x", "y"]), ("struct", ["x", ".."]), ("tuple", []), ("array", []), ("repeat", []), ("range", ["lo", "hi"])]
    assert [i.path for i in kinds(fns["build"], ir.Project)] == [".0", ".1"]  # `let (u, v) = t`
    for f in fns.values():
        assert not kinds(f, ir.Opaque), f.name
        assert ir.verify(f) == []
    text = ir.dump(fns["build"])
    assert "make struct x: t1, y: 2" in text and "make struct x: 1, ..t3" in text and "make repeat 0, 4" in text


def test_patterns_and_iteration_are_explicit():
    fns, _ = named(VALUES)
    assert [p.pattern for p in kinds(fns["pat"], ir.PatTest)] == ["0", "1..=9"]
    assert [(i.kind) for i in kinds(fns["opt"], ir.Test)] == ["some"]  # `Some` stays a real test of the optional; the last arm needs none
    assert [i.path for i in kinds(fns["opt"], ir.Project)] == ["Some.0"]
    assert [i.kind for i in kinds(fns["each"], ir.Test)] == ["has_next"]
    assert [i.kind for i in kinds(fns["each"], ir.Access) if i.kind == "item"] == ["item"]


def test_question_mark_is_a_test_and_two_accesses():
    fns, _ = named("fn g() -> result<u32, u32> { 1 }\nfn f() -> result<u32, u32> { let v = g()?; v }")
    f = fns["f"]
    assert [i.kind for i in kinds(f, ir.Test)] == ["failed"]
    assert sorted(i.kind for i in kinds(f, ir.Access)) == ["error", "ok"]
    assert not kinds(f, ir.Opaque) and ir.verify(f) == []


def test_the_new_values_survive_the_data_encoding():
    _, mod = named(VALUES)
    back = serial.loads_ir(serial.dumps_ir(mod))
    assert ir.dump_module(back) == ir.dump_module(mod)
