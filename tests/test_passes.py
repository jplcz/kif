# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The IR pass manager (kiflang/passes.py)."""

import pytest

from kiflang import ir, passes, symbols

SRC = "fn f(s: &[u32]) -> u32 { if s.is_empty() { return 0; } s[0] }\nfn g(s: &[u32]) -> u32 { s[0] }\n"


def tree(src=SRC):
    return symbols.analyze("/t/main.kif", src, None).tree


@pytest.fixture
def scratch(monkeypatch):
    """A registry copy that a test may add passes to."""
    monkeypatch.setattr(passes, "REGISTRY", dict(passes.REGISTRY))
    return passes.REGISTRY


def test_dependencies_come_first_and_run_once():
    assert passes.order(["prove", "verify"]) == ["cfg", "prove", "verify"]
    r = passes.run_function(ir.build_module(tree()).functions[0], ["prove", "verify", "cfg"])
    assert [s.name for s in r.stats if not s.cached] == ["cfg", "prove", "verify"]


def test_prove_through_the_manager_matches_the_prover():
    mod = ir.build_module(tree())
    f, g = (passes.run_function(x, passes.pipeline("prove")) for x in mod.functions)
    assert [c.proof is not None for _, c in f.fn.checks()] == [True]
    assert [c.proof is not None for _, c in g.fn.checks()] == [False]
    assert not f.diagnostics and not g.diagnostics


def test_a_check_pass_reports_and_a_transform_invalidates(scratch):
    seen = []

    @passes.register
    class Count(passes.Pass):
        name, kind, requires = "count", "check", ("cfg",)

        def run(self, ctx):
            seen.append(len(ctx.get("cfg").succ))
            ctx.report("KIFI900", f"{ctx.fn.name} has {seen[-1]} blocks", ctx.fn.node, error=False)

    @passes.register
    class Rewrite(passes.Pass):
        name, kind, invalidates = "rewrite", "transform", ("cfg",)

        def run(self, ctx):
            return None

    res = passes.run_module(tree(), ["count"])
    assert [d.code for d in res.diagnostics] == ["KIFI900", "KIFI900"] and len(seen) == 2
    r = passes.run_function(res.module.functions[0], ["count", "rewrite"])
    assert "cfg" not in r.results and "rewrite" in r.results


def test_bad_names_and_cycles_are_reported(scratch):
    with pytest.raises(passes.PassError, match="no pass named 'nope'"):
        passes.order(["nope"])

    @passes.register
    class A(passes.Pass):
        name, requires = "a", ("b",)

    @passes.register
    class B(passes.Pass):
        name, requires = "b", ("a",)

    with pytest.raises(passes.PassError, match="a -> b -> a"):
        passes.order(["a"])
    with pytest.raises(passes.PassError, match="registered twice"):
        passes.register(A)


def test_unsupported_functions_are_listed_not_run():
    res = passes.run_module(tree("gen fn k() { }\nfn ok() -> u32 { 1 }"), passes.pipeline("default"))
    assert [r.fn.name for r in res.functions] == ["ok"] and [n for n, _ in res.skipped] == ["k"]


def test_soft_constraints_and_listed_order_decide_the_order(scratch):
    def add(n, **kw):
        passes.register(type(n, (passes.Pass,), {"name": n, "kind": "check", **kw}))

    add("late", after=("early",))
    add("early")
    add("first", before=("early",))
    add("other")
    assert passes.order(["late", "early"]) == ["early", "late"]               # `after` beats the listed order
    assert passes.order(["other", "late", "early"]) == ["other", "early", "late"]  # unconstrained passes keep the listed order
    assert passes.order(["early", "first"]) == ["first", "early"]             # `before` of a pass that is present
    assert passes.order(["late"]) == ["late"]                                  # a constraint on an absent pass is ignored
    add("x", after=("y",))
    add("y", after=("x",))
    with pytest.raises(passes.PassError, match="conflict"):
        passes.order(["x", "y"])


def test_pipeline_specs_add_and_remove_passes(scratch):
    assert passes.pipeline("default") == ["cfg", "verify", "prove", "refuted", "require_proved", "borrows", "contracts", "drops", "tempdrops", "elide"]  # the default is every registered pass
    assert passes.pipeline("default,-prove") == ["cfg", "verify", "refuted", "require_proved", "borrows", "contracts", "drops", "tempdrops", "elide"]
    assert passes.pipeline("prove,verify") == ["prove", "verify"]

    @passes.register
    class Extra(passes.Pass):
        name, kind, after = "extra", "check", ("prove",)

    assert passes.order(passes.pipeline("default")) == ["cfg", "verify", "prove", "refuted", "require_proved", "borrows", "contracts", "drops", "tempdrops", "elide", "extra"]  # a new pass joins the default run
    assert passes.order(passes.pipeline("default,-extra")) == ["cfg", "verify", "prove", "refuted", "require_proved", "borrows", "contracts", "drops", "tempdrops", "elide"]


def test_an_invalidated_analysis_is_recomputed_only_for_the_next_user(scratch):
    runs = []

    @passes.register
    class Rewrite(passes.Pass):
        name, kind, requires, invalidates = "rewrite", "transform", ("cfg",), ("cfg",)

        def run(self, ctx):
            runs.append("rewrite")

    @passes.register
    class User(passes.Pass):
        name, kind, requires, after = "user", "check", ("cfg",), ("rewrite",)

        def run(self, ctx):
            runs.append("user")
            assert ctx.get("cfg").fn is ctx.fn

    @passes.register
    class Quiet(passes.Pass):
        name, kind, after = "quiet", "check", ("rewrite",)

        def run(self, ctx):
            runs.append("quiet")

    fn = ir.build_module(tree()).functions[0]
    r = passes.run_function(fn, ["rewrite", "quiet"])
    assert "cfg" not in r.results and sum(s.name == "cfg" and not s.cached for s in r.stats) == 1  # nobody needed it again
    r = passes.run_function(fn, ["rewrite", "user"])
    assert sum(s.name == "cfg" and not s.cached for s in r.stats) == 2 and "cfg" in r.results      # `user` got a fresh one


def test_refuted_pass_warns_about_a_constant_index_past_the_end():
    from kiflang import diagnostics as dg
    from kiflang import symbols

    m = symbols.analyze("/t/m.kif", "fn f() -> u8 { let a: [u8; 4] = [1, 2, 3, 4]; a[7] }\nfn g() -> u8 { let a: [u8; 4] = [1, 2, 3, 4]; a[3] }\n", None)
    found = passes.run_module(m.tree, ["refuted"]).diagnostics
    assert [(d.code, d.severity) for d in found] == [("KIFI010", dg.WARNING)]
    assert "7 is not below the length 4" in found[0].message


def test_check_module_feeds_lint_and_the_editor():
    from kiflang import symbols

    m = symbols.analyze("/t/m.kif", "fn f() -> u8 { let a: [u8; 4] = [1, 2, 3, 4]; a[7] }\n", None)
    assert [d.code for d in passes.check_module(m.tree)] == ["KIFI010"]


def _require(src):
    from kiflang import symbols

    m = symbols.analyze("/t/m.kif", src, None)
    return [d.code for d in m.diagnostics] + [d.code for d in passes.check_module(m.tree)]


def test_require_proved_fails_only_the_checks_it_demands_in_scope():
    assert _require("#[require_proved]\nfn a(t: [u8; 8], x: usize) -> u8 { t[x & 7] }") == []
    assert _require("#[require_proved]\nfn a(t: [u8; 8], x: usize) -> u8 { t[x] }") == ["KIFI011"]
    assert _require("#[require_proved(div)]\nfn a(t: [u8; 8], x: usize) -> u8 { t[x] }") == []  # another kind
    assert _require("fn a(t: [u8; 8], x: usize) -> u8 { t[x] }") == []  # no scope
    # an impl and a file are scopes too
    assert _require("struct s { x: u32 }\n#[require_proved]\nimpl s { fn a(&self, t: [u8; 8], x: usize) -> u8 { t[x] } }") == ["KIFI011"]
    assert _require("#![require_proved(index)]\nfn a(t: [u8; 8], x: usize) -> u8 { t[x] }\nfn b(t: [u8; 8]) -> u8 { t[1] }") == ["KIFI011"]


def test_require_proved_names_only_check_kinds():
    assert _require("#[require_proved(nope)]\nfn a() -> u32 { 1 }") == ["KIFP217"]
    assert _require("#![require_proved(nope)]\nfn a() -> u32 { 1 }") == ["KIFP217"]


def _refusals(src):
    r = passes.run_function(ir.build_module(tree(src)).functions[-1], ["c_refusals"], None, {})
    return [d.code for d in r.diagnostics]


def test_c_refusals_is_a_target_pass_outside_the_default_pipeline():
    assert "c_refusals" in passes.REGISTRY and "c_refusals" not in passes.pipeline("default")
    assert "c_refusals" in passes.pipeline("default,c_refusals")


def test_c_refusals_reports_what_the_c_backend_will_not_lower():
    assert _refusals("fn f() -> result<()> { let mut v: vector<u8> = vector::try_create(2)?; v.drop(); ok(()) }") == ["KIFC101"]
    assert _refusals("fn g() -> result<()> { ok(()) }\nfn k() { g(); }") == ["KIFC102"]
    assert _refusals("fn mk() -> result<vector<u8>> { let v: vector<u8> = vector::try_create(2)?; ok(v) }\nfn g() { let r = mk(); }") == ["KIFC103"]
    assert _refusals("fn k(e: error) -> u32 { match e { error::allocation_failed => 1, error::in_place_growth_failed => 2, _ => 3 } }") == ["KIFC104"]
    assert _refusals("fn g() -> result<()> { ok(()) }\nfn k() -> result<()> { g()?; ok(()) }") == []


def test_c_refusals_for_optionals_that_own_or_are_temporaries():
    assert _refusals("fn g() -> u32 { let o: optional<str> = none(); let p = o.value_or(1); 1 }") == ["KIFC105"]
    assert _refusals("fn g() -> usize { let o: optional<str> = none(); match o { Some(s) => 1, None => 0 } }") == ["KIFC106"]
    assert _refusals("fn f() -> optional<u32> { none() }\nfn g() -> u32 { match f() { Some(v) => v, None => 0 } }") == ["KIFC107"]
    assert _refusals("fn g() -> u32 { let o: optional<u32> = none(); match o { Some(v) => v, None => 0 } }") == []


def test_c_refusals_for_a_moving_for_and_a_match_on_an_owning_result():
    mk = "fn mk() -> result<vector<u8>> { let v: vector<u8> = vector::try_create(2)?; ok(v) }\n"
    assert _refusals("fn a(v: vector<u8>) -> usize { let mut t: usize = 0; for x in v { t += 1; } t }") == ["KIFC108"]
    assert _refusals("fn a(v: &vector<u8>) -> usize { let mut t: usize = 0; for x in v { t += 1; } t }") == []
    assert _refusals(mk + "fn b() -> usize { match mk() { ok(v) => 1, err(e) => 0 } }") == ["KIFC109"]
