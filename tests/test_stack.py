# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack estimate (docs/kif-stack.md): frames (stage 2)."""
import importlib.util
from pathlib import Path

from kiflang import ir, passes, stack, symbols, target

PLAN = target.load("c").stack()


def frames(src):
    m = symbols.analyze("/t/m.kif", src, None)
    built = ir.build_module(m.tree)
    for f in built.functions:
        passes.run_function(f, passes.pipeline("default"))
    return {f.name: f for f in stack.frames(built, m.tree.items, PLAN)}


def test_frame_is_locals_rounded_plus_overhead():
    f = frames("fn f(x: u32) -> u32 { x }")["f"]
    assert f.size == 16 + 16  # 4 rounded up to the 16-byte alignment, plus the overhead


def test_by_value_struct_parameter_counts():
    f = frames("struct s { a: [u8; 60], n: usize }\nfn f(p: s) -> usize { p.n }")["f"]
    assert f.parts == [("p", 72)] and f.size == 72 + 8 + 16


def test_array_literal_temporary_is_counted_with_its_slot():
    f = frames("fn f() -> u8 { let b: [u8; 256] = [0; 256]; b[3] }")["f"]
    assert [p for _, p in f.parts[:2]] == [256, 256]


def test_loads_and_field_accesses_are_not_counted():
    f = frames("struct s { a: [u8; 60], n: usize }\nfn f(p: s) -> usize { p.n }")["f"]
    assert len(f.parts) == 1


def test_unknown_local_makes_the_frame_unknown_not_zero():
    f = frames("struct h<T> { v: T }\nfn f<T>(x: h<T>) -> u32 { 0 }")["f"]
    assert f.size is None and "cannot size x" in f.why


def test_declared_stack_overrides_the_estimate():
    f = frames("#[stack(512)]\nfn f(x: u32) -> u32 { x }")["f"]
    assert f.size == 512 and f.declared


def test_overhead_and_alignment_come_from_the_target(tmp_path):
    import json
    t = tmp_path / "t.json"
    t.write_text(json.dumps({"name": "t", "extends": "c", "stack": {"align": 8, "frame_overhead": 8}}))
    plan = target.load(str(t)).stack()
    m = symbols.analyze("/t/m.kif", "fn f(x: u32) -> u32 { x }", None)
    built = ir.build_module(m.tree)
    assert stack.frames(built, m.tree.items, plan)[0].size == 8 + 8


def test_report_lists_biggest_first_and_unknown_apart():
    m = symbols.analyze("/t/m.kif", "struct h<T> { v: T }\nfn small() -> u32 { 1 }\nfn big() -> u8 { let b: [u8; 64] = [0; 64]; b[1] }\nfn g<T>(x: h<T>) -> u32 { 0 }", None)
    built = ir.build_module(m.tree)
    r = stack.report(stack.frames(built, m.tree.items, PLAN), PLAN, "m.kif")
    assert r.index(" big:") < r.index(" small:") < r.index("Unknown frames:") < r.index(" g:")


def test_demo_frames():
    spec = importlib.util.spec_from_file_location("stack_run", Path(__file__).parents[1] / "examples" / "stack" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.run()
    assert {k: r["frames"][k] for k in ("leaf", "buffer", "wrap", "external_cost", "generic")} == {"leaf": 32, "buffer": 544, "wrap": 96, "external_cost": 512, "generic": None}
