#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
"""Stack analysis demo runner, shaped like examples/secure-wipe/run.py.

    ./run.py [--target T]       demo.kif: write what each stage computes to out/<target>/ (git-ignored) and print a summary

Written to out/<target>/: diagnostics.txt, layout.txt, stack-report.txt, stack-depth.txt (with the budget diagnostics), tools.txt, stack-check.txt (measure.kif against gcc -fstack-usage), recursion.txt (recursion.kif).
"""

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from kiflang import hints, ir, kifc, layout, passes, quickfix, stack, stackbudget, stackcheck, stackgraph, stackinfo, target  # noqa: E402
from kiflang.symbols import analyze  # noqa: E402


def depths(path: Path, t) -> tuple:
    """(module, depths, the text of their report and diagnostics) of one file; the checker's own errors do not stop the analysis."""
    m = analyze(str(path), path.read_text(), kifc.import_resolver([]), cfg_env=t.cfg_env())
    built = ir.build_module(m.tree)
    for f in built.functions:
        passes.run_function(f, passes.pipeline("default"))
    ds = stackgraph.analyze(built, m.tree.items, t.stack(), m.tree)
    text = stackgraph.report(ds, t.stack(), path.name)
    budgets, bad = stackbudget.resolve(m.tree.items, m.tree, None, False, t.stack())
    found = stackgraph.diagnostics(ds) + bad + stackbudget.check(ds, budgets)
    text += "".join(f"{path.name}:{d.range.start.line + 1}: {d.code}: {d.message}\n" for d in found)
    return m, ds, text, sorted((d.code, d.message.split("(")[0]) for d in found)


def tools(path: Path, t) -> str:
    """What the editor shows (stage 5): hover text and inlay hint per function, and the quick fixes of each KIFW010."""
    mod = analyze(str(path), path.read_text(), kifc.import_resolver([]), cfg_env=t.cfg_env())
    info = stackinfo.analyse(mod.tree, t.stack())
    out = []
    for fn in hints.functions_of(mod.tree):
        note = stackinfo.describe(info, fn)
        h = stackinfo.hint(info, fn)
        if note:
            out.append(f"{fn.name.text}: hover: {note.replace(' _docs/kif-stack.md_', '')}" + (f"\n    inlay hint: {h['label']}" if h else ""))
    qmod, diags = quickfix.check(str(path), path.read_text(), resolver=kifc.import_resolver([]))
    for d in diags:
        if d.code == "KIFW010":
            out.append(f"{d.code} at line {d.range.start.line + 1}: " + "; ".join(f.title for f in quickfix.fixes_for(qmod, d, diags)))
    return "\n".join(out) + "\n"


def run(target_name: str = "c") -> dict:
    """Runs every stage that exists and returns what happened (used by tests/test_stack_demo.py)."""
    t = target.load(target_name)
    out = HERE / "out" / target_name
    out.mkdir(parents=True, exist_ok=True)
    demo = HERE / "demo.kif"
    m = analyze(str(demo), demo.read_text(), kifc.import_resolver([]), cfg_env=t.cfg_env())
    (out / "diagnostics.txt").write_text("".join(f"{d.code}: {d.message}\n" for d in m.diagnostics))
    text = layout.report(m.tree.items, t.stack(), demo.name)
    (out / "layout.txt").write_text(text)
    built = ir.build_module(m.tree)
    for f in built.functions:
        passes.run_function(f, passes.pipeline("default"))
    fs = stack.frames(built, m.tree.items, t.stack())
    rep = stack.report(fs, t.stack(), demo.name)
    (out / "stack-report.txt").write_text(rep)
    _, ds, text_d, found = depths(demo, t)
    (out / "stack-depth.txt").write_text(text_d)
    (out / "tools.txt").write_text(tools(demo, t))
    rows, problems = stackcheck.check(str(HERE / "measure.kif"), target_name, "gcc", ["-O0", "-fno-stack-protector"]) if shutil.which("gcc") else ([], ["gcc not found"])
    (out / "stack-check.txt").write_text(stackcheck.render(rows, problems, "gcc", ["-O0", "-fno-stack-protector"]))
    rec = HERE / "recursion.kif"
    mr, dr, text_r, _ = depths(rec, t)
    text_r = "".join(f"{d.code}: {d.message}\n" for d in mr.diagnostics) + "\n" + text_r
    (out / "recursion.txt").write_text(text_r)
    return {"depths": {d.name: d.depth for d in ds}, "recursion": {d.name: d.depth for d in dr}, "recursion_codes": sorted({d.code for d in mr.diagnostics}),
            "depth_report": text_d, "check": {r.name: (r.estimate, r.measured, r.verdict) for r in rows}, "check_problems": problems, "budget": [f for f in found if f[0] in ("KIFW010", "KIFP084")], "out": out, "layout": text, "frames": {f.name: f.size for f in fs}, "report": rep, "diagnostics": [d.code for d in m.diagnostics]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="c")
    r = run(ap.parse_args().target)
    print(r["layout"], end="")
    print(r["report"], end="")
    print(r["depth_report"], end="")
    print("diagnostics:", ", ".join(r["diagnostics"]) or "none")
    print(f"written to {r['out']}:")
    for f in sorted(r["out"].iterdir()):
        print("  " + f.name)
