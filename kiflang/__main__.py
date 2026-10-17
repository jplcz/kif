# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Command line: `python -m kiflang check|export|modules` (docs/kif-proc-language.md section 15).

    python -m kiflang check  FILE [--sysroot DIR] [--target NAME|FILE] [--profile P] [--sanitize LIST] [--cfg OPT]... [--print-cfg]
    python -m kiflang export FILE -o OUT.kifm [--sysroot DIR]

`--target` picks a target definition (kiflang/targets, docs/kif-targets.md); each `--cfg` overrides what `#[cfg(..)]` sees:
`arch=arm64` sets a key, `smp` sets a flag, `!smp` clears it.
    python -m kiflang modules --sysroot DIR
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import cfg, modfile, project, report, target
from .symbols import analyze
from .sysroot import Sysroot, SysrootError


def _report(mod, path: str, fmt: str = "text") -> int:
    seen = set()
    rep = report.Reporter(fmt, sys.stderr, "kif-check")
    pending = [mod]
    while pending:
        m = pending.pop()
        if id(m) in seen:
            continue
        seen.add(id(m))
        pending.extend(m.imported.values())
        for d in m.diagnostics:
            rep.add(m.key, d)
    rep.finish()
    return 1 if rep.failed() else 0


def _sanitizers(opts: Optional[List[str]]) -> Optional[List[str]]:
    return None if not opts else [s for o in opts for s in o.split(",") if s]


def make_parser() -> "tuple":
    ap = argparse.ArgumentParser(prog="kiflang")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "export", "modules"):
        p = sub.add_parser(name)
        if name != "modules":
            p.add_argument("file")
        p.add_argument("--sysroot", help="the only place `import` reads from (default: the prelude shipped with the compiler)")
        if name in ("check", "export"):
            report.add_format_option(p)
        if name == "check":
            p.add_argument("--target", help="a built-in target name or a target .json file")
            p.add_argument("--cfg", action="append", default=[], metavar="OPT", help="key=value, flag or !flag (repeatable)")
            p.add_argument("--profile", help="build profile: debug (default), perf, release, sanitize, or one the target defines")
            p.add_argument("--sanitize", action="append", metavar="LIST",
                           help="comma-separated sanitizers (address,undefined,..); `none` clears; replaces the profile's list")
            p.add_argument("--print-cfg", action="store_true", help="print the effective cfg set and exit")
        if name == "export":
            p.add_argument("-o", "--output", required=True)
    return ap, sub


def main(argv: Optional[List[str]] = None) -> int:
    ap, sub = make_parser()
    try:
        proj, argv = project.preload(sys.argv[1:] if argv is None else list(argv))
    except project.ProjectError as exc:
        print(f"kiflang: {exc}", file=sys.stderr)
        return 2
    if proj:
        for name, sp in sub.choices.items():
            sp.set_defaults(**proj.defaults("check"))  # the command line still wins
    args = ap.parse_args(argv)
    try:
        sysroot = Sysroot(args.sysroot) if args.sysroot else Sysroot.shipped()
        if args.cmd == "modules":
            print("\n".join(sysroot.modules()))
            return 0
        env = None
        if args.cmd == "check" and (args.target or args.cfg or args.profile or args.sanitize):
            tgt = target.load(args.target or "c")
            env = tgt.cfg_env(args.cfg, args.profile, _sanitizers(args.sanitize))
        if getattr(args, "print_cfg", False):
            print("\n".join((env or cfg.Env()).lines()))
            return 0
        with open(args.file, encoding="utf-8") as f:
            mod = analyze(args.file, f.read(), sysroot=sysroot, cfg_env=env)
    except (SysrootError, OSError, cfg.CfgError, target.TargetError) as exc:
        print(f"kiflang: {exc}", file=sys.stderr)
        return 2
    status = _report(mod, args.file, args.out_format)
    if args.cmd == "export" and status == 0:
        with open(args.output, "wb") as out:
            out.write(modfile.export(mod))
    return status


if __name__ == "__main__":
    sys.exit(main())
