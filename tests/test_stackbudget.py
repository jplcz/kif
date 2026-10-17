# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack budgets (docs/kif-stack.md, stage 4): #[small_stack], --small-stack, KIFW010, KIFP084."""
import importlib.util
from pathlib import Path

import pytest

from kiflang import ir, passes, stackbudget, stackgraph, symbols, target

PLAN = target.load("c").stack()
BODY = "fn leaf() -> u32 { let a: [u8; 600] = [0; 600]; a[1] as u32 }\n"


def codes(src, cli=None, cli_set=False):
    m = symbols.analyze("/t/m.kif", src, None)
    built = ir.build_module(m.tree)
    for f in built.functions:
        passes.run_function(f, passes.pipeline("default"))
    ds = stackgraph.analyze(built, m.tree.items, PLAN, m.tree)
    b, bad = stackbudget.resolve(m.tree.items, m.tree, cli, cli_set, PLAN)
    return [(d.code, d.message) for d in bad + stackbudget.check(ds, b)]


def names(found):
    return sorted(c for c, _ in found)


def test_no_budget_no_warning():
    assert codes(BODY) == []


def test_fn_budget_warns_with_chain_and_variable():
    found = codes("#[small_stack(300)]\nfn top() -> u32 { leaf() }\n" + BODY)
    (c, msg), = found
    assert c == "KIFW010" and "leaf::a 600" in msg and "chain top -> leaf" in msg


def test_big_variable_is_error():
    assert names(codes("#[small_stack(100)]\n" + BODY)) == ["KIFP084", "KIFW010"]


def test_within_budget_is_quiet():
    assert codes("#[small_stack(4096)]\n" + BODY) == []


def test_file_budget_and_off():
    assert names(codes("#![small_stack(100)]\n" + BODY)) == ["KIFP084", "KIFW010"]
    assert codes("#![small_stack(100)]\n#[small_stack(off)]\n" + BODY) == []


def test_cli_budget_is_wider_than_attributes():
    assert names(codes(BODY, 100, True)) == ["KIFP084", "KIFW010"]
    assert codes(BODY, None, True) == []
    assert codes("#[small_stack(4096)]\n" + BODY, 100, True) == []


def test_impl_and_method_scopes():
    src = "struct s { n: u32 }\n#[small_stack(100)]\nimpl s {\n fn a(&self) -> u32 { let b: [u8; 200] = [0; 200]; b[1] as u32 }\n #[small_stack(4096)]\n fn c(&self) -> u32 { let b: [u8; 200] = [0; 200]; b[1] as u32 }\n}\n"
    found = codes(src)
    assert names(found) == ["KIFP084", "KIFW010"] and all("s::a" in m for _, m in found)


@pytest.mark.parametrize("arg", ["", "(0)", "(a, b)", "(x)"])
def test_malformed_attribute(arg):
    assert names(codes(f"#[small_stack{arg}]\nfn f() {{}}\n")) == ["KIFW016"]


def test_parse_option():
    assert stackbudget.parse_option("off") is None and stackbudget.parse_option("0x100") == 256
    for bad in ("0", "-4", "x"):
        with pytest.raises(ValueError):
            stackbudget.parse_option(bad)


def test_demo_budgets():
    spec = importlib.util.spec_from_file_location("stack_run", Path(__file__).parents[1] / "examples" / "stack" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.run()["budget"] == [("KIFP084", "huge_local"), ("KIFW010", "counter::bump"), ("KIFW010", "huge_local"), ("KIFW010", "over")]


def kifc_run(tmp_path, *extra, src="fn leaf() -> u32 { let a: [u8; 600] = [0; 600]; return a[1] as u32; }\nfn top() -> u32 { return leaf(); }\n"):
    import subprocess
    import sys

    p = tmp_path / "n.kif"
    p.write_text(src)
    r = subprocess.run([sys.executable, "-m", "kiflang.kifc", str(p), "--c", str(tmp_path / "out"), *extra], capture_output=True, text=True, cwd=Path(__file__).parents[1])
    return r.returncode, r.stderr


def test_kifc_small_stack(tmp_path):
    assert kifc_run(tmp_path) == (0, "")
    rc, err = kifc_run(tmp_path, "--small-stack=300")
    assert rc == 1 and "KIFP084" in err and "KIFW010" in err  # the variable alone is over: an error
    assert kifc_run(tmp_path, "--small-stack=2000") == (0, "")
    assert kifc_run(tmp_path, "--small-stack=off") == (0, "")
    rc, err = kifc_run(tmp_path, "--small-stack=1100")  # only the chain is over: a warning, the build goes on
    assert rc == 0 and "KIFW010" in err and "KIFP084" not in err
    assert kifc_run(tmp_path, "--small-stack=1100", "--Werror")[0] == 1
    assert kifc_run(tmp_path, "--small-stack=0")[0] == 2


def test_kifw015_budget_on_unsizable_function():
    """KIFW015: a function with its own #[small_stack] whose frame cannot be sized is not checked, and says so; a sized one is."""
    got = codes("#[small_stack(100)]\nfn id<T>(x: T) -> T { x }\n")
    assert [c for c, _ in got] == ["KIFW015"]
    assert codes("#[small_stack(100)]\nfn f() -> i32 { 1 }\n") == []
    assert codes("fn id<T>(x: T) -> T { x }\n", cli=100, cli_set=True) == []
