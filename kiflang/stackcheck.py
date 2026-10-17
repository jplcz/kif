# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`kif stack-check` (docs/kif-stack.md section 7, stage 6): how good is the stack estimate for one build?

The estimate is kif's own (frames from the IR, no compiler). This check does not replace it: it lowers a file, compiles the generated C with
`-fstack-usage` (GCC and clang) and compares the frame the compiler used for each function with the estimate. A function where the compiler
used *more* than kif estimated is a failure: the budget checks cannot be trusted for it. Less is normal (registers, inlining, dead locals)."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ir, kifc, lower_c, passes, stack, stackgraph
from . import target as targetdef
from .symbols import analyze

# GCC writes `file:line:col:name<TAB>bytes<TAB>kind`, clang `file:line:name<TAB>bytes<TAB>kind` (no column). Both write `<object minus .o>.su` next to the object.
SU = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?(?P<name>[^\t:]+)\t(?P<bytes>\d+)\t(?P<kind>.+)$")
# GCC's interprocedural clones (`f.part.0`, `f.isra.0`, `f.constprop.0`, `f.cold`) are parts of `f`
CLONE = re.compile(r"\.(?:part|isra|constprop|cold|lto_priv|localalias)(?:\.\d+)?$")


@dataclass
class Row:
    name: str  # the kif function
    symbol: str  # its C symbol
    estimate: Optional[int]  # kif's frame; None: it cannot be sized
    measured: Optional[int]  # the compiler's frame; None: the function has no entry (inlined or removed)
    kind: str  # "static", "dynamic", "dynamic,bounded" as the compiler says
    depth_estimate: Optional[int] = None
    depth_measured: Optional[int] = None  # the chain with the compiler's frames where it has them; None: unbounded
    mixed: bool = False  # some frame of the chain is the estimate (no measurement)
    verdict: str = "ok"  # ok | UNDER (the compiler used more than estimated) | dynamic | unmeasured | unestimated


def parse_su(text: str) -> Dict[str, Tuple[int, str]]:
    """`{symbol: (bytes, kind)}` of the lines of a `.su` file, GCC's or clang's. A clone (`f.isra.0`) counts for `f`; the largest wins."""
    out: Dict[str, Tuple[int, str]] = {}
    for line in text.splitlines():
        m = SU.match(line)
        if not m:
            continue
        name = CLONE.sub("", CLONE.sub("", m["name"]))
        n = int(m["bytes"])
        if name not in out or n > out[name][0]:
            out[name] = (n, m["kind"].strip())
    return out


def read_su(paths: List[str]) -> Tuple[Dict[str, Tuple[int, str]], List[str]]:
    """`.su` files given by name, or found under the given directories (the build tree of a project): what the compiler measured in the real build."""
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                files += [os.path.join(root, n) for n in sorted(names) if n.endswith(".su")]
        else:
            files.append(p)
            if not os.path.exists(p):
                return {}, [f"{p}: no such file or directory"]
    out: Dict[str, Tuple[int, str]] = {}
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for k, v in parse_su(fh.read()).items():
                    if k not in out or v[0] > out[k][0]:
                        out[k] = v
        except OSError as e:
            return out, [f"{f}: {e.strerror}"]
    if not files:
        return out, [f"no .su files under {', '.join(paths)}: compile the generated C with -fstack-usage (gcc, clang) or use `kif stack-check` without --su"]
    return out, []


def check(path: str, target: str = "c", cc: str = "gcc", flags: Optional[List[str]] = None, keep: Optional[str] = None, su: Optional[List[str]] = None,
          cfg: Optional[List[str]] = None, profile: Optional[str] = None, sanitize: Optional[List[str]] = None, include: Optional[List[str]] = None, sysroot=None) -> Tuple[List[Row], List[str]]:
    """`(one row per function the estimate covers, problems)`; a problem is a file that does not lower or compile.

    `su` (files or directories) reads the `.su` files of a build that already compiled the generated C, so the frames are those of the
    project's own compiler and flags; without it the check compiles the C itself with `cc flags`. `cfg`, `profile`, `sanitize` and
    `include` and `sysroot` (a `sysroot.Sysroot`, the only place imports read from) must be the ones the build lowered with (they decide which code exists)."""
    t = targetdef.load(target)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    sans = [x for o in sanitize or [] for x in o.split(",") if x] or None
    m = analyze(path, src, None if sysroot else kifc.import_resolver(include or []), sysroot=sysroot, cfg_env=t.cfg_env(cfg or [], profile, sans))
    errs = [d for d in m.diagnostics if d.severity == 1]
    if errs:
        return [], [f"{path}:{d.range.start.line + 1}: {d.code}: {d.message}" for d in errs]
    stem = os.path.splitext(os.path.basename(path))[0]
    lw = lower_c.Lowerer(m, t, stem, os.path.basename(path))
    try:
        files = lw.run()
    except lower_c.LowerError as e:
        return [], [f"{path}: cannot lower to C: {e}"]
    built = ir.build_module(m.tree)
    for fn in built.functions:
        passes.run_function(fn, passes.pipeline("default"))
    plan = t.stack()
    depths = stackgraph.analyze(built, m.tree.items, plan, m.tree)
    frames = {f.name: f for f in stack.frames(built, m.tree.items, plan)}
    measured: Dict[str, Tuple[int, str]] = {}
    problems: List[str] = []
    if su is not None:
        measured, problems = read_su(su)
        if problems:  # nothing was measured: a table of "no entry" would only mislead
            return [], problems
        files = {}
    with tempfile.TemporaryDirectory() as tmp:
        d = keep or tmp
        if su is None:
            os.makedirs(d, exist_ok=True)
        for name, text in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        if su is None:
            with open(os.path.join(d, "kif_c.h"), "w", encoding="utf-8") as f:
                f.write(lower_c.common_header(t))
        units = sorted(n for n in files if n.endswith(".c")) if su is None else []
        if su is None and not units:  # everything is in the header: a unit that includes it
            units = [f"{stem}_unit.c"]
            with open(os.path.join(d, units[0]), "w", encoding="utf-8") as f:
                f.write(f'#include "{stem}_proc.h"\n')
        for name in units:
            obj = os.path.join(d, name[:-2] + ".o")
            fl = flags if flags is not None else ["-O2"]
            try:
                r = subprocess.run([cc, *fl, *([] if any(x.startswith("-std=") for x in fl) else ["-std=c11"]), "-fstack-usage", "-c", "-o", obj, "-I", d, os.path.join(d, name)],
                                   capture_output=True, text=True)
            except FileNotFoundError:
                problems.append(f"{cc}: no such compiler")
                break
            su_file = os.path.join(d, name[:-2] + ".su")
            if r.returncode != 0:
                errors = [x for x in r.stderr.splitlines() if "error" in x]
                hint = " (an imported kif module is not compiled here: build it and use --su)" if any("_proc.h" in x for x in errors) else ""
                problems.append(f"{name}: {cc} failed: {(errors or r.stderr.strip().splitlines() or [str(r.returncode)])[0].strip()}{hint}")
            elif not os.path.exists(su_file):
                problems.append(f"{name}: {cc} wrote no .su file: -fstack-usage is not supported by this compiler")
            else:
                with open(su_file, encoding="utf-8") as f:
                    measured.update(parse_su(f.read()))
    by_name = {x.name: x for x in depths}
    symbols = {x.name: lw.fn_name(x.node) for x in depths if x.node is not None and hasattr(x.node, "params")}
    rows: List[Row] = []
    for x in depths:
        sym = symbols.get(x.name)
        if sym is None or x.node is None or getattr(x.node, "body", None) is None:
            continue
        got = measured.get(sym)
        est = frames[x.name].size if x.name in frames else x.frame
        row = Row(x.name, sym, est, got[0] if got else None, got[1] if got else "", x.depth)
        if got is None:
            row.verdict = "unmeasured"
        elif est is None:
            row.verdict = "unestimated"
        elif got[1] != "static":
            row.verdict = "dynamic"
        elif got[0] > est:
            row.verdict = "UNDER"
        if not x.unbounded and x.depth is not None:
            total = 0
            for c in x.chain:
                s = symbols.get(c)
                if c not in by_name:  # generated helpers and drop glue: their frames are in the .su file under many names; the estimate stands
                    total += stackgraph.hidden_cost(plan, c) or 0
                    row.mixed = True
                elif s in measured:
                    total += measured[s][0]
                else:
                    total += by_name[c].frame or 0
                    row.mixed = row.mixed or (c in symbols and by_name[c].frame is not None)
            row.depth_measured = total
        rows.append(row)
    return rows, problems


def render(rows: List[Row], problems: List[str], cc: str, flags: List[str]) -> str:
    out = [f"stack estimate against {cc} -fstack-usage ({' '.join(flags)}):"]
    out += [f"  PROBLEM {p}" for p in problems]
    width = max((len(r.name) for r in rows), default=0)
    for r in rows:
        est = "?" if r.estimate is None else str(r.estimate)
        got = "-" if r.measured is None else str(r.measured)
        line = f"  {'FAIL' if r.verdict == 'UNDER' else 'ok  '} {r.name:<{width}}  estimate {est:>6}  compiler {got:>6}"
        if r.measured is not None and r.estimate:
            line += f"  ({r.measured * 100 // r.estimate}%)"
        if r.depth_measured is not None:
            line += f"  chain: estimate {r.depth_estimate} / compiler {r.depth_measured}{' (partly estimated)' if r.mixed else ''}"
        note = {"UNDER": "the compiler used MORE than the estimate: budgets for this function cannot be trusted",
                "dynamic": f"the compiler reports {r.kind} stack use (alloca/VLA): the estimate does not see it",
                "unmeasured": "no entry: inlined into its callers or removed at these flags (use -fno-inline to see it)",
                "unestimated": "kif cannot size this frame"}.get(r.verdict)
        out.append(line + (f"  <- {note}" if note else ""))
    if not rows and not problems:
        out.append("  no function with a body")
    if any(r.verdict == "UNDER" for r in rows):
        out.append("  note: the estimate does not model what a compiler adds to a frame: the stack-protector canary (on by default in distribution compilers), "
                   "padding, spills; raise the target's stack.frame_overhead or compare with -fno-stack-protector (docs/kif-stack.md section 8)")
    return "\n".join(out) + "\n"
