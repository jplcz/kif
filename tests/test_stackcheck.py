# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`kif stack-check` (docs/kif-stack.md, stage 6)."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiflang import stackcheck

needs_gcc = pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
SRC = "fn leaf(x: u32) -> u32 { let a: [u8; 600] = [0; 600]; return a[1] as u32 + x; }\nfn top(x: u32) -> u32 { return leaf(x) + 1; }\n"


def test_parse_su():
    text = "m.c:3:10:m_leaf\t1144\tstatic\nm.c:9:5:m_top\t32\tdynamic,bounded\nnoise\n"
    assert stackcheck.parse_su(text) == {"m_leaf": (1144, "static"), "m_top": (32, "dynamic,bounded")}


def run(tmp_path, flags, src=SRC):
    p = tmp_path / "m.kif"
    p.write_text(src)
    rows, problems = stackcheck.check(str(p), "c", "gcc", flags)
    return {r.name: r for r in rows}, problems


@needs_gcc
def test_estimate_covers_the_compiler_at_o0(tmp_path):
    rows, problems = run(tmp_path, ["-O0", "-fno-stack-protector"])
    assert not problems and set(rows) == {"leaf", "top"}
    assert rows["leaf"].measured is not None and rows["leaf"].measured <= rows["leaf"].estimate and rows["leaf"].verdict == "ok"
    assert rows["top"].depth_measured == rows["top"].measured + rows["leaf"].measured and not rows["top"].mixed


@needs_gcc
def test_optimizer_may_remove_a_function(tmp_path):
    rows, _ = run(tmp_path, ["-O2"])
    assert all(r.verdict in ("ok", "unmeasured") for r in rows.values())


@needs_gcc
def test_a_too_small_estimate_fails(tmp_path, monkeypatch):
    from kiflang import stack

    real = stack.frames

    def small(*a, **k):
        out = real(*a, **k)
        for f in out:
            f.size = 8
        return out

    monkeypatch.setattr(stack, "frames", small)
    rows, _ = run(tmp_path, ["-O0", "-fno-stack-protector"])
    assert rows["leaf"].verdict == "UNDER"
    assert "MORE than the estimate" in stackcheck.render(list(rows.values()), [], "gcc", ["-O0"])


def test_problems(tmp_path):
    rows, problems = run(tmp_path, ["-O0"], "fn f() -> u32 { return nosuch; }\n")
    assert rows == {} and "KIFP010" in problems[0]


@needs_gcc
def test_command_line(tmp_path):
    p = tmp_path / "m.kif"
    p.write_text(SRC)
    r = subprocess.run([sys.executable, "-m", "kiflang.kif", "stack-check", str(p), "--flags=-O0 -fno-stack-protector"], capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert r.returncode == 0 and "leaf" in r.stdout and "compiler" in r.stdout


@needs_gcc
def test_demo_measurement():
    spec = importlib.util.spec_from_file_location("stack_run", Path(__file__).parents[1] / "examples" / "stack" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.run()
    assert r["check_problems"] == [] and set(r["check"]) == {"leaf", "top", "counter::bump"} and all(v[2] == "ok" for v in r["check"].values())


def test_parse_su_gcc_clang_and_clones(tmp_path):
    """GCC writes file:line:col:name, clang file:line:name; GCC clones (.part, .isra, .constprop) fold into the base name, the largest wins."""
    from kiflang import stackcheck
    (tmp_path / "x.c.su").write_text("a/b.c:5:10:f\t48\tstatic\nb.c:7:f.part.0\t96\tstatic\nb.c:9:3:g.isra.1\t16\tdynamic,bounded\n")
    (tmp_path / "y.c.su").write_text("b.c:3:h\t24\tstatic\n")
    got, problems = stackcheck.read_su([str(tmp_path)])
    assert not problems
    assert got["f"][0] == 96 and got["g"][0] == 16 and got["h"][0] == 24
    _, problems = stackcheck.read_su([str(tmp_path / "none")])
    assert problems


def test_sysroot_import_with_su(tmp_path):
    """--sysroot is the only place imports read from; an imported module is not compiled here, so its build's .su files are used."""
    from kiflang import stackcheck, sysroot
    root = tmp_path / "root"
    root.mkdir()
    (root / "h.kif").write_text("pub fn helper() -> i32 { 7 }\n")
    (root / "reloco.kif").write_text((Path(stackcheck.__file__).parent / "prelude" / "reloco.kif").read_text())
    (tmp_path / "m.kif").write_text('import "h.kif";\nuse h::helper;\nfn top() -> i32 { helper() }\n')
    (tmp_path / "m_proc.c.su").write_text("m.kif:3:10:m_top\t16\tstatic\n")
    rows, problems = stackcheck.check(str(tmp_path / "m.kif"), su=[str(tmp_path)], sysroot=sysroot.Sysroot(str(root)))
    assert not problems and [r.name for r in rows] == ["top"]
    rows, problems = stackcheck.check(str(tmp_path / "m.kif"), su=[str(tmp_path)])
    assert problems and "not found" in problems[0]
    rows, problems = stackcheck.check(str(tmp_path / "m.kif"), cc="gcc", sysroot=sysroot.Sysroot(str(root)))
    assert any("use --su" in p for p in problems)


@needs_gcc
def test_vector_helpers_are_counted_below_the_caller(tmp_path):
    """The generated vector helpers use stack of their own: with them as hidden callees the chain covers the compiler's at -O1 -fno-inline."""
    src = "fn build(n: usize) -> result<u32> { let mut v: vector<u32> = vector::try_create(n)?; v.try_push(3)?; ok(v[0]) }\n"
    rows, problems = run(tmp_path, ["-O1", "-fno-inline", "-fno-stack-protector"], src)
    assert not problems and rows["build"].verdict == "ok"
    assert rows["build"].depth_estimate == rows["build"].depth_measured and rows["build"].mixed
