# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The secure-wipe object check (`kif wipe-check`, docs/kif-secure-wipe.md section 13, stage 8).

kif does not choose the C compiler's flags, so whether a wipe survives optimization is a fact about one build. This check makes it
visible: it lowers a file, compiles the generated C to assembly with the flags you give, and for every function whose C text contains
wipe sites it looks for as much evidence of wiping in the function's assembly as there are sites. Evidence is a barrier (an inline
`asm` barrier, which the check marks with a comment) or a call to a wipe primitive (`memset`, `explicit_bzero`, ...), whichever is more frequent, or your own
regular expression (`--evidence`). Only the wipes written directly in a function body are counted; wipes in the generated struct
drops and `secure_vector` helpers are not (they may be inlined or not, depending on flags).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import kifc, lower_c
from . import target as targetdef
from .symbols import analyze

MARK = "kifwipe-barrier"
CALLS = r"^\s*(?:call|bl|jal|jmp|b)\w*\s+(?:\*?)\w*(?:memset|bzero|explicit|zero|wipe)\w*"


@dataclass
class Row:
    """One function: its wipe sites in the C text, the evidence found in the assembly (None: no symbol), and the verdict."""
    file: str
    name: str
    sites: int
    evidence: Optional[int]
    ok: bool
    note: str = ""


def sites_per_function(text: str, macro: str) -> Dict[str, int]:
    """Wipe macro calls (not the `_VOLATILE` form: volatile stores are guaranteed by the C standard) written directly in each function body of generated C (functions open with a `{` and close with a `}` at column 0)."""
    out: Dict[str, int] = {}
    lines = text.split("\n")
    name, depth = None, 0
    for n, line in enumerate(lines):
        if name is None:
            if line == "{" and n > 0:
                m = re.search(r"\b(\w+)\([^;]*\)\s*$", lines[n - 1])
                name = m.group(1) if m else None
                depth = 0
            continue
        if line == "}":
            name = None
            continue
        c = len(re.findall(rf"\b{re.escape(macro)}\(", line))
        if c and name:
            out[name] = out.get(name, 0) + c
    return out


def stamp(text: str) -> str:
    """Give every empty `asm volatile ("" ...)` barrier a comment, so the compiler prints it in the assembly (an empty template prints nothing)."""
    return re.sub(r'(__asm__\s+__volatile__\s*\(|__asm__\s+volatile\s*\(|asm\s+volatile\s*\()\s*""', lambda m: m.group(1) + '"# ' + MARK + '"', text)


def evidence_per_function(asm: str, extra: Optional[str]) -> Dict[str, int]:
    """Evidence of wiping in each function of GNU-style assembly (`name:` ... `.size name`)."""
    out: Dict[str, int] = {}
    cur, buf = None, []
    for line in asm.split("\n"):
        m = re.match(r"^(\w+):\s*$", line)
        if m and cur is None and not m.group(1).startswith("."):
            cur, buf = m.group(1), []
            continue
        if cur is not None and line.startswith(f"\t.size\t{cur},"):
            if extra:
                out[cur] = sum(1 for b in buf if re.search(extra, b))
            else:
                out[cur] = max(sum(1 for b in buf if MARK in b), sum(1 for b in buf if re.search(CALLS, b)))
            cur = None
        elif cur is not None:
            buf.append(line)
    return out


def check(path: str, target: str = "c", cc: str = "gcc", flags: Optional[List[str]] = None, evidence: Optional[str] = None,
          keep: Optional[str] = None) -> Tuple[List[Row], List[str]]:
    """Lower `path`, compile it to assembly with `cc flags` and return one row per function with wipe sites, plus problems (a file that does not lower or compile)."""
    t = targetdef.load(target)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    m = analyze(path, src, kifc.import_resolver([]), cfg_env=t.cfg_env())
    errs = [d for d in m.diagnostics if d.severity == 1]
    if errs:
        return [], [f"{path}:{d.range.start.line + 1}: {d.code}: {d.message}" for d in errs]
    stem = os.path.splitext(os.path.basename(path))[0]
    files = lower_c.lower(m, t, stem)
    macro = t.wipe().macro
    rows: List[Row] = []
    problems: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        d = keep or tmp
        os.makedirs(d, exist_ok=True)
        for name, text in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(stamp(text))
        with open(os.path.join(d, "kif_c.h"), "w", encoding="utf-8") as f:
            f.write(lower_c.common_header(t))
        for name in sorted(n for n in files if n.endswith(".c")):
            asm = os.path.join(d, name[:-2] + ".s")
            r = subprocess.run([cc, *(flags if flags is not None else ["-O2"]), "-std=c11", "-S", "-o", asm, "-I", d, os.path.join(d, name)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                problems.append(f"{name}: {cc} failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
                continue
            with open(asm, encoding="utf-8") as f:
                ev = evidence_per_function(f.read(), evidence)
            for fn, n in sorted(sites_per_function(files[name], macro).items()):
                got = ev.get(fn)
                if got is None:
                    rows.append(Row(name, fn, n, None, True, "no symbol in the object (inlined or removed): its wipes are in its callers"))
                else:
                    rows.append(Row(name, fn, n, got, got >= n, "" if got >= n else "FEWER wipes survive than the source has"))
    return rows, problems


def render(rows: List[Row], problems: List[str], flags: List[str]) -> str:
    out = [f"secure-wipe object check ({' '.join(flags)}):"]
    out += [f"  PROBLEM {p}" for p in problems]
    for r in rows:
        ev = "-" if r.evidence is None else str(r.evidence)
        out.append(f"  {'ok  ' if r.ok else 'FAIL'} {r.name}: {r.file}  sites {r.sites}, surviving {ev}" + (f"  ({r.note})" if r.note else ""))
    if not rows and not problems:
        out.append("  no function has a wipe site written directly in its body")
    return "\n".join(out) + "\n"
