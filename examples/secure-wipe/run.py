#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
"""Secure wipe demo runner, shaped like tests/test_c_files.py.

    ./run.py [--target T]       demo.kif: lower, write everything to out/<target>/, compile driver.c, run it, compare with demo.out
                                errors.kif: write its diagnostics

Written to out/<target>/ (git-ignored): diagnostics.txt, demo.ir, demo_proc.h/.c, kif_c.h, wipe-plan.txt, gcc.log, driver, driver.out.
The summary printed at the end lists them; open them to read what the compiler generated.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from kiflang import kifc, lower_c, target  # noqa: E402
from kiflang.symbols import analyze  # noqa: E402


def diagnostics(path: Path, t) -> tuple:
    m = analyze(str(path), path.read_text(), kifc.import_resolver([]), cfg_env=t.cfg_env())
    return m, m.diagnostics


def run(target_name: str = "c") -> dict:
    """Builds both programs into out/<target>/ and returns what happened (used by tests/test_wipe_demo.py)."""
    t = target.load(target_name)
    out = HERE / "out" / target_name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    result: dict = {"out": out}

    _, errs = diagnostics(HERE / "errors.kif", t)
    (out / "errors.txt").write_text("".join(f"{d.code} line {d.range.start.line + 1}: {d.message}\n" for d in errs))
    result["error_codes"] = sorted(d.code for d in errs)

    demo = HERE / "demo.kif"
    m, diags = diagnostics(demo, t)
    (out / "diagnostics.txt").write_text("".join(f"{d.code}: {d.message}\n" for d in diags))
    result["diagnostics"] = [d.code for d in diags]
    ir = subprocess.run([sys.executable, "-m", "kiflang.kif", "ir", str(demo), "--passes", "default", "--target", target_name],
                        capture_output=True, text=True, cwd=HERE.parents[1])
    (out / "demo.ir").write_text(ir.stdout + ir.stderr)

    p = t.wipe()
    (out / "wipe-plan.txt").write_text(f"macro   : {p.macro}\nbody    : {p.body}\nvolatile: {p.volatile_body}\nheader  : {p.header}\nerror   : {p.error or 'none'}\n")

    for name, text in lower_c.lower(m, t, demo.stem).items():
        (out / name).write_text(text)
    (out / "kif_c.h").write_text(lower_c.common_header(t))
    shutil.copy(HERE / "driver.c", out / "driver.c")
    cc = subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-fsanitize=address,undefined",
                         "-fno-sanitize-recover=all", "-I", str(out), str(out / "driver.c"), *map(str, sorted(out.glob("*_proc.c"))),
                         "-o", str(out / "driver")], capture_output=True, text=True)
    (out / "gcc.log").write_text(cc.stderr)
    result["gcc"] = cc.returncode
    if cc.returncode == 0:
        r = subprocess.run([str(out / "driver")], capture_output=True, text=True)
        (out / "driver.out").write_text(r.stdout)
        result["run"] = r.returncode
        result["output"] = r.stdout
    result["expected"] = (HERE / "demo.out").read_text()
    result["wipes_in_ir"] = (out / "demo.ir").read_text().count("wipe %")
    result["wipes_in_c"] = sum(f.read_text().count(p.macro + "(&") for f in [*out.glob("*_proc.c"), *out.glob("*_proc.h")])
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="c")
    res = run(ap.parse_args().target)
    out = res["out"]
    ok = res.get("gcc") == 0 and res.get("run") == 0 and res.get("output") == res["expected"]
    print(f"errors.kif   : {', '.join(res['error_codes'])}")
    print(f"demo.kif     : diagnostics {res['diagnostics'] or 'none'}; wipes in IR {res['wipes_in_ir']}, in C {res['wipes_in_c']}")
    print(f"driver       : {'OK, output matches demo.out' if ok else 'FAILED (see gcc.log / driver.out)'}")
    print(f"written to   : {out}")
    for f in sorted(out.iterdir()):
        print(f"  {f.name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
