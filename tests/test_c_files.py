# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Runs every case in tests/c: lower the .kif, compile it with its .c driver, compare the output with the .out file."""
import shutil
import subprocess
from pathlib import Path

import pytest

from kiflang import kifc, lower_c, target
from kiflang.symbols import analyze

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not available")
OUT = Path(__file__).parent / "c" / "out"
# A case has a driver (.c) or an expected refusal (.refuse); other .kif files are libraries the cases import.
CASES = sorted(p for p in Path(__file__).parent.joinpath("c").glob("*.kif") if p.with_suffix(".c").exists() or p.with_suffix(".refuse").exists())


def lowered(path: Path):
    """Lower the file and everything it imports (each import to its own `<stem>_proc.h`, as the build does)."""
    t = target.load("c")
    m = analyze(str(path), path.read_text(), kifc.import_resolver([]), cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    files = lower_c.lower(m, t, path.stem)
    for lib in kifc._imports(m):
        files.update(lower_c.lower(lib, t, lower_c.stem_of(lib.key)))
    return files


@pytest.mark.parametrize("kif", CASES, ids=lambda p: p.stem)
def test_case(kif: Path):
    refuse = kif.with_suffix(".refuse")
    if refuse.exists():
        with pytest.raises(lower_c.LowerError) as ex:
            lowered(kif)
        OUT.mkdir(exist_ok=True)
        (OUT / (kif.stem + ".refused")).write_text(str(ex.value) + "\n")
        assert refuse.read_text().strip() in str(ex.value)
        return
    d = OUT / kif.stem  # kept after the run (git-ignored) so the compiler's actual output can be read
    d.mkdir(parents=True, exist_ok=True)
    for name, text in lowered(kif).items():
        (d / name).write_text(text)
    (d / "kif_c.h").write_text(lower_c.common_header(target.load("c")))
    shutil.copy(kif.with_suffix(".c"), d / "driver.c")
    r = subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-I", str(d),
                        str(d / "driver.c"), *map(str, sorted(d.glob("*_proc.c"))), "-o", str(d / "driver"), "-lpthread"],
                       capture_output=True, text=True)
    (d / "gcc.log").write_text(r.stderr)
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(d / "driver")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    (d / "driver.out").write_text(run.stdout)
    assert run.stdout == kif.with_suffix(".out").read_text()
