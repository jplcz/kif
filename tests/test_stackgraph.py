# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack estimate (docs/kif-stack.md): call graph and depth (stage 3)."""
import importlib.util
from pathlib import Path

from kiflang import ir, passes, stackgraph, symbols, target

PLAN = target.load("c").stack()


def depths(src):
    m = symbols.analyze("/t/m.kif", src, None)
    built = ir.build_module(m.tree)
    for f in built.functions:
        passes.run_function(f, passes.pipeline("default"))
    ds = stackgraph.analyze(built, m.tree.items, PLAN, m.tree)
    return {d.name: d for d in ds}, ds


def test_depth_is_frame_plus_deepest_callee():
    d, _ = depths("fn a() -> u32 { 1 }\nfn b(x: u32) -> u32 { a() + x }\nfn c(x: u32) -> u32 { b(x) + a() }")
    assert d["c"].chain == ["c", "b", "a"] and d["c"].depth == d["c"].frame + d["b"].depth


def test_the_deepest_branch_wins():
    d, _ = depths("fn small() -> u32 { 1 }\nfn big() -> u8 { let b: [u8; 200] = [0; 200]; b[1] }\nfn top() -> u32 { small() + (big() as u32) }")
    assert d["top"].chain == ["top", "big"]


def test_extern_with_stack_counts_and_without_makes_a_lower_bound():
    src = '#[safe]\n#[stack(100)]\nextern "C" fn known(x: u32) -> u32;\n#[safe]\nextern "C" fn blind(x: u32) -> u32;\nfn f(x: u32) -> u32 { known(x) }\nfn g(x: u32) -> u32 { blind(x) }'
    d, ds = depths(src)
    assert d["f"].depth == d["f"].frame + 100 and not d["f"].incomplete
    assert d["g"].depth == d["g"].frame and "blind() has no #[stack(N)]" in d["g"].incomplete[0]
    assert [x.code for x in stackgraph.diagnostics(ds)] == ["KIFW014"]


def test_method_calls_use_the_qualified_name():
    d, _ = depths("struct s { a: u32 }\nimpl s { fn get(&self) -> u32 { return helper(self.a); } }\nfn helper(x: u32) -> u32 { x }\nfn top(v: s) -> u32 { v.get() }")
    assert d["top"].chain == ["top", "s::get", "helper"]


def test_recursion_is_unbounded_and_so_is_everything_reaching_it():
    d, ds = depths("fn rec(n: u32) -> u32 { rec(n) }\nfn ping(n: u32) -> u32 { pong(n) }\nfn pong(n: u32) -> u32 { ping(n) }\nfn user() -> u32 { rec(1) }\nfn fine() -> u32 { 1 }")
    assert [n for n, x in d.items() if x.unbounded] == ["rec", "ping", "pong", "user"]
    assert d["ping"].cycle == ["ping", "pong"] and d["fine"].depth == d["fine"].frame
    assert sorted(x.code for x in stackgraph.diagnostics(ds)) == ["KIFW013"] * 4


def test_a_call_through_a_function_value_is_incomplete():
    d, _ = depths("fn top(f: fn(u32) -> u32) -> u32 { f(1) }")
    assert "function value" in d["top"].incomplete[0]


def test_a_user_drop_is_a_hidden_call():
    d, _ = depths("struct g { a: u32 }\nimpl g { fn drop(&mut self) { let b: [u8; 200] = [0; 200]; } }\nfn top() -> u32 { let x = g { a: 1 }; return 0; }")
    assert d["top"].chain == ["top", "g::drop"]


def test_a_very_deep_chain_does_not_hit_the_recursion_limit():
    n = 1500
    src = "fn f0() -> u32 { 1 }\n" + "".join(f"fn f{i}() -> u32 {{ f{i - 1}() }}\n" for i in range(1, n))
    d, _ = depths(src)
    assert len(d[f"f{n - 1}"].chain) == n


def test_demo_depths():
    spec = importlib.util.spec_from_file_location("stack_run", Path(__file__).parents[1] / "examples" / "stack" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.run()
    assert r["depths"]["level1"] == 32 + 32 + 32 + 200 and r["depths"]["leaf"] == 32
    assert r["recursion"] == {"countdown": None, "ping": None, "pong": None, "caller_of_recursion": None}
    assert r["recursion_codes"] == ["KIFP032"]


def test_generated_helpers_and_drop_glue_are_hidden_callees():
    """A vector helper (`try_push`, ...) and the drop glue of a vector are functions in the generated C: the depth counts them below the caller,
    at the cost the target's backend gives them (kiflang/stackhelpers.py), and `stack.helper_frame` replaces that."""
    src = ("fn build(n: usize) -> result<u32> { let mut v: vector<u32> = vector::try_create(n)?; v.try_push(3)?; ok(v[0]) }\n"
           "fn plain(x: u32) -> u32 { x }\n")
    d, _ = depths(src)
    assert d["build"].chain[1].startswith(stackgraph.HELPER) and "vector.try_push" in d["build"].chain[1]
    assert d["build"].depth == d["build"].frame + PLAN.helpers["vector.try_push"] and PLAN.helpers["vector.try_push"] > 0
    assert d["plain"].chain == ["plain"]

    def with_plan(plan):
        m = symbols.analyze("/t/m.kif", src, None)
        built = ir.build_module(m.tree)
        for f in built.functions:
            passes.run_function(f, passes.pipeline("default"))
        return {x.name: x for x in stackgraph.analyze(built, m.tree.items, plan, m.tree)}["build"]

    fixed = with_plan(PLAN._replace(helper_frame=500, drop_frame=0))
    assert fixed.depth == fixed.frame + 500
    # a backend with no estimator (C++: kif has no C++ backend) cannot say: the depth is a lower bound, and the note says why
    blind = with_plan(PLAN._replace(helpers=None, drops=None))
    assert blind.depth == blind.frame and any("no stack estimator" in x for x in blind.incomplete)


def test_helper_costs_come_from_the_generated_code():
    """The C estimator reads the helpers `lower_c` writes: `try_push` calls `grow` calls `try_reserve`, so it costs more than any of them alone."""
    h = PLAN.helpers
    assert h["vector.try_push"] > h["vector.grow"] > h["vector.try_reserve"] > 0
    assert PLAN.drops["vector"] > 0 and "str.try_append" in h
    from kiflang import stackhelpers
    assert stackhelpers.estimate(target.load("c"), PLAN) == stackhelpers.Costs(PLAN.helpers, PLAN.drops)
    assert stackhelpers.ESTIMATORS.keys() == {"c"}  # `cxx` has none yet
