# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Command line tools: unified subcommands, completion scripts, kif.toml, --format outputs and `kif new`."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiflang import project

ROOT = Path(__file__).resolve().parents[1]
BAD = "fn f() -> i32 { let x: u8 = 300; x }\n"


def kif(*args, cwd=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "kiflang.kif", *args], capture_output=True, text=True, cwd=cwd, env=env)


def test_delegated_subcommands_in_help():
    out = kif("--help").stdout
    for name in ("check", "build", "export", "modules", "lsp", "import-m", "new", "completion", "project"):
        assert name in out


@pytest.mark.parametrize("fmt", ["text", "json", "sarif", "github"])
def test_check_formats(tmp_path, fmt):
    (tmp_path / "a.kif").write_text(BAD)
    r = kif("check", "--format", fmt, str(tmp_path / "a.kif"))
    assert r.returncode != 0
    text = r.stdout + r.stderr
    if fmt == "json":
        assert json.loads(text)[0]["file"].endswith("a.kif")
    elif fmt == "sarif":
        assert json.loads(text)["version"] == "2.1.0"
    elif fmt == "github":
        assert "::error" in text


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_scripts(shell):
    r = kif("completion", shell)
    assert r.returncode == 0
    for word in ("check", "build", "completion", "format"):
        assert word in r.stdout


def test_bash_completion_syntax(tmp_path):
    p = tmp_path / "c.bash"
    p.write_text(kif("completion", "bash").stdout)
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_mini_toml_matches_tomllib():
    text = '# c\ntarget = "x" # t\nwerror = true\ninclude = ["a", "b"]\n[build]\nsources = ["*.kif"]\nn = 3\n'
    got = project._mini_toml(text, "kif.toml")
    assert got == {"target": "x", "werror": True, "include": ["a", "b"], "build": {"sources": ["*.kif"], "n": 3}}


def test_project_errors_and_precedence(tmp_path):
    (tmp_path / "kif.toml").write_text('target = "freebsd-kernel"\nbogus = 1\n')
    r = kif("project", cwd=tmp_path)
    assert r.returncode == 2 and "bogus" in r.stderr
    (tmp_path / "kif.toml").write_text('target = "freebsd-kernel"\nprofile = "perf"\ninclude = ["inc"]\n')
    r = kif("project", "--json", cwd=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["target"] == "freebsd-kernel"
    cm = kif("project", "--cmake", cwd=tmp_path).stdout
    assert "KIF_PROJECT_TARGET" in cm and "KIF_PROJECT_INCLUDE" in cm


def test_new_refuses_overwrite(tmp_path):
    d = tmp_path / "proj"
    assert kif("new", str(d)).returncode == 0
    for rel in ("CMakeLists.txt", ".gitignore"):
        assert (d / rel).exists()
    assert not (d / "kif.toml").exists()  # CMake is the primary description; the project file is opt-in
    assert kif("new", str(tmp_path / "t"), "--toml").returncode == 0 and (tmp_path / "t" / "kif.toml").exists()
    r = kif("new", str(d))
    assert r.returncode == 1 and "already has" in r.stderr


@pytest.mark.skipif(shutil.which("cmake") is None or shutil.which("ninja") is None or shutil.which("cc") is None, reason="needs cmake, ninja, cc")
@pytest.mark.parametrize("kind", ["driver", "app", "staticlib", "solib", "library"])
def test_new_project_builds(tmp_path, kind):
    d = tmp_path / "proj"
    assert kif("new", str(d), "--kind", kind).returncode == 0
    cml = d / "CMakeLists.txt"
    cml.write_text(cml.read_text().replace("find_package(jplcz_kif REQUIRED)",
                                           f"list(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)"))
    b = d / "build"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(d), "-B", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["ninja", "-C", str(b), "-k", "0"], capture_output=True, text=True)
    # a driver needs kernel headers to compile the generated C; generating it must work either way
    assert list((b / "kif").rglob("*.h")), r.stdout + r.stderr
    if kind != "driver":
        assert r.returncode == 0, r.stdout + r.stderr
    if kind == "app":
        assert subprocess.run([str(b / "proj")]).returncode == 2
    if kind == "solib":
        assert list(b.glob("libproj.so*")) or list(b.glob("libproj.*"))


def test_watch_reruns_on_change(tmp_path, capsys):
    from kiflang import watch

    f = tmp_path / "a.kif"
    f.write_text("fn f() {}\n")
    runs = []

    def run():
        runs.append(1)
        if len(runs) == 1:
            os.utime(f, (1, 1))  # a different mtime is a change
        return 0

    assert watch.watch(run, [str(tmp_path)], interval=0.01, rounds=1) == 0
    assert len(runs) == 2 and "changed" in capsys.readouterr().err


def test_watch_flag_runs_tool_once_per_change(tmp_path):
    (tmp_path / "a.kif").write_text("fn f() {}\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, "-m", "kiflang.kif", "check", "--watch", "--interval", "0.05", "a.kif"],
                         cwd=tmp_path, env=env, stderr=subprocess.PIPE, text=True)
    import time
    time.sleep(1.0)
    (tmp_path / "a.kif").write_text("fn g() {}\n")
    os.utime(tmp_path / "a.kif", (time.time() + 5, time.time() + 5))
    time.sleep(1.0)
    p.terminate()
    err = p.communicate(timeout=5)[1]
    assert "a.kif changed" in err


MAIN_ARGS = "fn main(args: &[str_view]) -> i32 {\n    let n: i32 = 0;\n    if args.len() > 2 { return 3; }\n    n\n}\n"


def _build_main(tmp_path, src, target="c"):
    (tmp_path / "m.kif").write_text(src)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "kiflang.kifc", "--c", str(tmp_path), "--emit-source", "--target", target, str(tmp_path / "m.kif")],
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(shutil.which("cc") is None, reason="needs cc")
@pytest.mark.parametrize("src,args,rc", [(MAIN_ARGS, ["a", "b"], 3), (MAIN_ARGS, [], 0), ("fn main() -> i32 { 7 }\n", [], 7), ("fn main() { }\n", ["x"], 0)])
def test_main_entry_point(tmp_path, src, args, rc):
    assert _build_main(tmp_path, src).returncode == 0
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable, "-m", "kiflang.kifc", "--c-common", str(tmp_path), "--target", "c"], check=True, env=env)
    assert subprocess.run(["cc", "-I", str(tmp_path), "-o", str(tmp_path / "m"), str(tmp_path / "m_proc.c")], capture_output=True, text=True).returncode == 0
    assert subprocess.run([str(tmp_path / "m"), *args]).returncode == rc


@pytest.mark.parametrize("src,target,msg", [
    ("fn main(x: i32) -> i32 { x }\n", "c", "no parameters"),
    ("fn main(a: &mut [str_view]) {}\n", "c", "no parameters"),
    ("fn main() -> u8 { 1 }\n", "c", "returns"),
    ("fn main() {}\n", "freebsd-kernel", "freestanding"),
])
def test_main_entry_point_errors(tmp_path, src, target, msg):
    r = _build_main(tmp_path, src, target)
    assert r.returncode != 0 and msg in r.stderr


@pytest.mark.skipif(shutil.which("cc") is None or shutil.which("nm") is None, reason="needs cc and nm")
def test_visibility_in_shared_object(tmp_path):
    (tmp_path / "v.kif").write_text("#[export, visibility(default)]\nfn api(a: u32) -> u32 { a + 1 }\n#[visibility(hidden)]\nfn helper(a: u32) -> u32 { a }\n"
                                    "#[visibility(default)]\nfn extra(a: u32) -> u32 { a }\n")
    assert _build_main(tmp_path, (tmp_path / "v.kif").read_text(), "c").returncode == 0
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable, "-m", "kiflang.kifc", "--c-common", str(tmp_path), "--target", "c"], check=True, env=env)
    so = tmp_path / "libv.so"
    r = subprocess.run(["cc", "-shared", "-fPIC", "-fvisibility=hidden", "-I", str(tmp_path), "-o", str(so), str(tmp_path / "m_proc.c")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    syms = subprocess.run(["nm", "-D", "--defined-only", str(so)], capture_output=True, text=True).stdout
    assert " api" in syms and " m_extra" in syms and "helper" not in syms


def test_visibility_errors(tmp_path):
    assert "takes one of" in _build_main(tmp_path, "#[visibility(secret)]\nfn f() -> u32 { 1 }\n").stderr
    assert "no effect" in _build_main(tmp_path, "#[inline, visibility(hidden)]\nfn f() -> u32 { 1 }\n").stderr


SRC_VIS = "#[export]\nfn a(x: u32) -> u32 { x }\n#[visibility(hidden)]\nfn b(x: u32) -> u32 { x }\n"


def _vis_target(tmp_path, base, extra):
    t = tmp_path / "t.json"
    t.write_text('{"format": "kif-target", "version": 1, "name": "t", "extends": "%s"%s}' % (base, extra))
    return str(t)


def _text(tmp_path, target, src=SRC_VIS):
    r = _build_main(tmp_path, src, target)
    assert r.returncode == 0, r.stderr
    return (tmp_path / "m_proc.h").read_text()


def test_visibility_only_where_specified(tmp_path):
    h = _text(tmp_path, "c")
    assert "extern uint32_t a(" in h  # #[export] alone implies no attribute
    assert "KIF_VISIBILITY_HIDDEN uint32_t m_b(" in h


def test_visibility_scopes(tmp_path):
    src = ("#![visibility(hidden)]\nstruct s { n: u32, }\n#[visibility(default)]\nimpl s { fn get(&self) -> u32 { self.n } }\n"
           "impl s { fn two(&self) -> u32 { 2 } }\n#[visibility(protected)]\nfn f(x: u32) -> u32 { x }\nfn g(x: u32) -> u32 { x }\n")
    h = _text(tmp_path, "c", src)
    assert "KIF_VISIBILITY_DEFAULT uint32_t s_get(" in h  # impl scope beats the file
    assert "KIF_VISIBILITY_HIDDEN uint32_t s_two(" in h   # impl without one: the file's
    assert "KIF_VISIBILITY_PROTECTED uint32_t m_f(" in h   # function beats both


def test_visibility_follows_target_settings(tmp_path):
    assert "KIF_VISIBILITY" not in _text(tmp_path, "freebsd-kernel")  # freestanding: off, the attributes are checked and dropped
    assert "KIF_VISIBILITY_HIDDEN" in _text(tmp_path, _vis_target(tmp_path, "freebsd-kernel", ', "visibility": {"enabled": true}'))
    assert "KIF_VISIBILITY" not in _text(tmp_path, _vis_target(tmp_path, "c", ', "visibility": {"enabled": false}'))


SRC_ACCESS = ("fn f(a: [u32; 4], i: usize) -> u32 { a[2] + a[i] }\nfn h(a: [u32; 4]) -> u32 { a[7] }\n"
              "fn r(a: [u32; 4], j: usize) -> u32 { let x: &[u32] = a[1..3]; let y: &[u32] = a[0..j]; x[0] }\n")


def test_proved_accesses_drop_their_checks(tmp_path):
    c = (lambda: (tmp_path / "m_proc.c").read_text())
    assert _build_main(tmp_path, SRC_ACCESS).returncode == 0
    text = c()
    assert "bounds check elided: constant index 2 < 4" in text                           # constant in range: proved, no check
    assert text.count('"index out of bounds"') == 3         # i, the constant 7 (a run-time panic) and x[0]
    assert text.count('"slice range out of bounds"') == 1 and "range check elided: constant range ends at 3 <= 4" in text  # only a[0..j] is checked


def test_checked_tier_keeps_every_check(tmp_path):
    t = _vis_target(tmp_path, "c", ', "access": {"index": "checked", "range": "checked"}')
    assert _build_main(tmp_path, SRC_ACCESS, t).returncode == 0
    text = (tmp_path / "m_proc.c").read_text()
    assert "check elided" not in text and text.count('"slice range out of bounds"') == 2 and text.count('"index out of bounds"') == 4


def test_no_unchecked_tier(tmp_path):
    t = _vis_target(tmp_path, "c", ', "access": {"index": "unchecked"}')
    r = _build_main(tmp_path, SRC_ACCESS, t)
    assert r.returncode != 0 and "access" in r.stderr


def test_kif_ir_text_json_cbor(tmp_path):
    from kiflang import cbor, serial
    src = tmp_path / "i.kif"
    src.write_text("fn f(a: [u32; 4], i: usize) -> u32 { a[2] + a[i] }\n")
    r = kif("ir", str(src), cwd=tmp_path)
    assert r.returncode == 0 and "check index 2, 4" in r.stdout and "check index t" in r.stdout
    j = kif("ir", str(src), "--format", "json", cwd=tmp_path)
    assert serial.loads_ir(j.stdout).functions[0].name == "f"
    out = tmp_path / "i.cbor"
    assert kif("ir", str(src), "--format", "cbor", "-o", str(out), cwd=tmp_path).returncode == 0
    assert serial.loads_ir(out.read_bytes()).functions[0].name == "f"
    ast_out = tmp_path / "a.cbor"
    assert kif("ir", str(src), "--ast", "--format", "cbor", "-o", str(ast_out), cwd=tmp_path).returncode == 0
    assert serial.loads_ast(ast_out.read_bytes()).items
    assert kif("ir", str(src), "--format", "cbor", cwd=tmp_path).returncode == 2
    pr = kif("ir", str(src), "--prove", cwd=tmp_path)
    assert "check index 2, 4  [proved: constant index 2 < 4]" in pr.stdout and "check index t" in pr.stdout.split("[proved")[1]
    assert "proved: " not in kif("ir", str(src), cwd=tmp_path).stdout
    assert cbor.loads(out.read_bytes())["format"] == "kif-ir"


def test_ir_explain_checks_lists_proved_and_kept_checks(tmp_path):
    p = tmp_path / "m.kif"
    p.write_text("fn f(a: [u32; 4], i: usize) -> u32 { a[2] + a[i] }\n")
    r = kif("ir", str(p), "--no-project", "--explain-checks")
    assert r.returncode == 0, r.stderr
    assert "index check proved: constant index 2 < 4" in r.stdout and "index check kept" in r.stdout
