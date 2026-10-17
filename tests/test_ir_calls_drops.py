# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Call identity, `Check.call` linking, macros as calls and the `drops` pass (docs/kif-ir.md, docs/kif-ir-passes.md)."""

import pytest

from kiflang import drops, ir, serial, symbols


def mod_of(src):
    m = symbols.analyze("/t/main.kif", src, None)
    return ir.build_module(m.tree)


def fns(src):
    mod = mod_of(src)
    assert not mod.skipped
    return {f.name: f for f in mod.functions}, mod


def of(f, cls):
    return [i for b in f.blocks for i in b.instrs if isinstance(i, cls)]


CALLS = """struct pt { x: u32 }
impl pt { fn new(x: u32) -> pt { pt { x: x } } fn get(&self) -> u32 { self.x } fn twice(&self) -> u32 { Self::dbl(self.x) } fn dbl(v: u32) -> u32 { v * 2 } }
fn id(v: u32) -> u32 { v }
fn use_all(p: &pt) -> u32 { let q = pt::new(1); panic!("x"); id(p.get()) }
"""


def test_calls_have_identity_kind_and_declaration():
    out, _ = fns(CALLS)
    calls = of(out["use_all"], ir.Call)
    assert [c.id for c in calls] == list(range(1, len(calls) + 1))
    by = {c.callee: c for c in calls}
    assert by["pt::new"].kind == "assoc" and by["pt::new"].decl == "pt::new"
    assert by["id"].kind == "free"
    assert any(c.kind == "method" for c in calls)
    m = [c for c in calls if c.kind == "macro"]
    assert [c.callee for c in m] == ["panic!"]


def test_a_macro_that_does_not_return_is_followed_by_unreachable():
    out, _ = fns(CALLS)
    b = next(b for b in out["use_all"].blocks if any(isinstance(i, ir.Call) and i.kind == "macro" for i in b.instrs))
    assert isinstance(b.term, ir.Unreachable) or any(isinstance(i, ir.Unreachable) for i in b.instrs)


def test_self_paths_resolve_to_the_impl():
    out, _ = fns(CALLS)
    (c,) = [c for c in of(out["twice"], ir.Call)]
    assert c.decl == "pt::dbl" and c.kind == "assoc"


REQ = """fn need(#[non_empty] v: &[u32]) -> u32 { v[0] }
fn caller(v: &[u32]) -> u32 { need(v) }
"""


def test_requirement_checks_name_the_call_they_guard():
    out, _ = fns(REQ)
    f = out["caller"]
    (call,) = [c for c in of(f, ir.Call) if c.callee == "need"]
    guards = [c for c in of(f, ir.Check) if c.call]
    assert guards and all(c.call == call.id for c in guards)
    assert ir.verify(f) == []


OWN = """fn mk(n: usize) -> result<vector<u8>> { let v: vector<u8> = vector::try_create(n)?; ok(v) }
fn keep(n: usize) -> result<usize> { let v: vector<u8> = vector::try_create(n)?; ok(v.len()) }
fn cond(n: usize, f: bool) -> result<usize> { let v: vector<u8> = vector::try_create(n)?; if f { let w = v; } ok(1) }
fn again() -> result<usize> { let mut s: vector<u8> = vector::try_create(1)?; s = vector::try_create(2)?; ok(s.len()) }
fn plain(a: u32) -> u32 { let b = a + 1; b }
"""


def analysed(src):
    out, mod = fns(src)
    for f in mod.functions:
        drops.analyze(f, ir.CFG(f))
    return out, mod


def words(f):
    return [(str(d.local), d.drop) for d in of(f, ir.Dead) if d.local.owns]


def test_owning_locals_are_marked():
    out, _ = fns(OWN)
    assert [l.owns for l in out["keep"].locals if l.name == "v"] == [True]
    assert not any(l.owns for l in out["plain"].locals)


def test_a_moved_value_is_not_dropped_and_a_kept_one_is():
    out, _ = analysed(OWN)
    assert words(out["mk"]) == [("%v", "")]
    assert ("%v", "drop") in words(out["keep"])


def test_a_conditional_move_needs_a_flag():
    out, _ = analysed(OWN)
    assert ("%v", "flag") in words(out["cond"])


def test_reassignment_drops_the_old_value():
    out, _ = analysed(OWN)
    stores = [s for s in of(out["again"], ir.Store) if str(s.local) == "%s"]
    assert [s.drop_old for s in stores] == ["", "drop"]


@pytest.mark.parametrize("as_cbor", [False, True])
def test_calls_drops_and_ownership_round_trip(as_cbor):
    _, mod = analysed(OWN + CALLS.replace("struct pt", "struct pt"))
    back = serial.loads_ir(serial.dumps_ir(mod, as_cbor=as_cbor))
    assert ir.dump_module(back) == ir.dump_module(mod)
    f = next(f for f in back.functions if f.name == "cond")
    assert ("%v", "flag") in words(f) and any(l.owns for l in f.locals)
