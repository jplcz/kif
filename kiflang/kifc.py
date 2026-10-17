# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kifc`: the kif compiler command line. Today it has the C backend (`--c DIR`, `--freebsd DIR`); see docs/kif-cmake.md.

    python3 -m kiflang.kifc --c gen --target freebsd-kernel --profile release file.kif
    python3 -m kiflang.kifc --merge-manifest lib.json a.json b.json

`--freebsd DIR` writes the kobj output of `#[kobj]` interfaces (`<iface>_if.h` / `.c`). The C++ backend (`--cpp`) is not implemented yet and fails with status 2.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional

from . import cfg as cfgmod
from . import lower_c
from . import lower_kobj
from . import manifest as abi
from . import project
from . import report
from . import target as targetdef
from .diagnostics import ERROR, WARNING
from .symbols import analyze
from .sysroot import Sysroot, SysrootError


def write_if_changed(path: str, text: str) -> bool:
    """Write `text` unless the file already has it (so a build does not recompile unchanged output)."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


stem_of = lower_c.stem_of


def import_resolver(dirs: List[str]):
    """A `symbols.FileResolver` for a build without a sysroot: the importing file's directory (for `./` and `../`, and first
    for bare names too), then the `-I` directories in order, then `KIF_PATH` (`:`-separated). Files read are recorded in `.read`."""
    read: List[str] = []

    def resolve(importer: str, path: str):
        roots = [os.path.dirname(importer)] + dirs + [d for d in os.environ.get("KIF_PATH", "").split(os.pathsep) if d]
        for root in roots[:1] if path.startswith(("./", "../")) else roots:
            full = os.path.realpath(os.path.join(root, path))
            if os.path.isfile(full):
                with open(full, encoding="utf-8") as f:
                    read.append(full)
                    return full, f.read()
        return None

    resolve.read = read  # type: ignore[attr-defined]
    return resolve


def merge_manifests(out: str, inputs: List[str]) -> int:
    merged: Dict[str, dict] = {"interfaces": {}, "types": {}, "layouts": {}}
    status = 0
    for path in inputs:
        try:
            with open(path, encoding="utf-8") as f:
                doc = abi.loads(f.read(), path)
        except (OSError, ValueError) as e:
            print(f"kifc: {e}", file=sys.stderr)
            return 2
        for section in merged:
            for name, value in doc[section].items():
                if name in merged[section] and merged[section][name] != value:
                    print(f"kifc: {path}: error KIF040: {section[:-1] if section.endswith('s') else section} {name!r} is "
                          "defined twice with different content", file=sys.stderr)
                    status = 1
                merged[section][name] = value
    if status:
        return status
    text = abi.dumps({"format": abi.FORMAT, "version": abi.FORMAT_VERSION, "file": os.path.basename(out), **merged})
    write_if_changed(out, text)
    return 0


def _imports(mod, seen=None):
    seen = {} if seen is None else seen
    for m in mod.imported.values():
        if m.key not in seen:
            seen[m.key] = m
            _imports(m, seen)
    return list(seen.values())


def compile_c(args: argparse.Namespace, path: str) -> int:
    try:
        tgt = targetdef.load(args.target)
        sanitize = [s for o in args.sanitize for s in o.split(",") if s] if args.sanitize else None
        env = tgt.cfg_env(args.cfg, args.profile, sanitize)
        profile = tgt.profile(args.profile)["name"]
        sans = tgt.sanitizers(args.profile, sanitize)
        sysroot = Sysroot(args.sysroot) if args.sysroot else None
        with open(path, encoding="utf-8") as f:
            text = f.read()
        resolver = None if sysroot else import_resolver(args.include)
        mod = analyze(os.path.realpath(path), text, resolver, sysroot=sysroot, cfg_env=env)
    except (targetdef.TargetError, cfgmod.CfgError, SysrootError, OSError) as e:
        print(f"kifc: {e}", file=sys.stderr)
        return 2
    rep = report.Reporter(args.out_format, sys.stderr, "kifc")
    for d in mod.diagnostics:
        rep.add(path, d)
    bad = any(d.severity == ERROR or (args.Werror and d.severity == WARNING) for d in mod.diagnostics)
    if bad or args.out_format in ("json", "sarif"):
        rep.finish()  # one document, also when there is nothing to report
    if bad:
        return 1
    note = f"target={tgt.name} profile={profile}" + (f" sanitize={','.join(sans)}" if sans else "")
    try:
        files = lower_c.lower(mod, tgt, stem_of(path), os.path.basename(path), note) if args.c else {}
        kobj_files = lower_kobj.lower(mod, tgt, os.path.basename(path)) if args.freebsd else {}
    except lower_c.LowerError as e:
        r = e.range
        where = f"{path}:{r.start.line + 1}:{r.start.col + 1}" if r is not None else path
        print(f"{where}: error {e.code}: {e.message}", file=sys.stderr)
        return 1
    src_name = stem_of(path) + "_proc.c"
    if args.c and args.emit_source and src_name not in files:
        files[src_name] = (f'/* generated from {os.path.basename(path)} by kifc; do not edit */\n#include "{stem_of(path)}_proc.h"\n')
    kobj_name = stem_of(path) + "_kobj.c"
    if args.freebsd and args.emit_source and kobj_name not in kobj_files:
        kobj_files[kobj_name] = f"/* generated from {os.path.basename(path)} by kifc: no kobj class here */\n"
    outs = [(args.c, n) for n in files] + [(args.freebsd, n) for n in kobj_files]
    if args.list_outputs:
        for d, name in outs:
            print(os.path.join(d, name))
        return 0
    for name, body in files.items():
        write_if_changed(os.path.join(args.c, name), body)
    for name, body in kobj_files.items():
        write_if_changed(os.path.join(args.freebsd, name), body)
    if args.manifest:
        write_if_changed(args.manifest, abi.dumps(abi.manifest_of(mod, path)))
    if args.depfile:
        outs = " ".join(os.path.join(d, n) for d, n in outs)
        deps = [path] + sorted(m.key for m in _imports(mod) if m.key != mod.key)
        write_if_changed(args.depfile, f"{outs}: " + " ".join(deps) + "\n")
    return 0


def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kifc", description="compile a .kif file (C backend)")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--c", metavar="DIR", help="write <stem>_proc.h and, with #[export] functions, <stem>_proc.c here")
    ap.add_argument("--c-common", metavar="DIR", help="write kif_c.h (the code every generated C file shares, from the target) here; no input file")
    ap.add_argument("--emit-source", action="store_true", help="always write <stem>_proc.c with --c and <stem>_kobj.c with --freebsd (a stub when there is nothing to define), so a build knows its outputs")
    ap.add_argument("--cpp", metavar="DIR", help="C++ backend (not implemented yet)")
    ap.add_argument("--freebsd", metavar="DIR", help="write <iface>_if.h and <iface>_if.c for the #[kobj] interfaces here (needs a target with a kobj section)")
    ap.add_argument("--target", default="c", metavar="T", help="a built-in target name or a .json file (default: c)")
    ap.add_argument("--profile", metavar="P", help="debug, perf, release, sanitize or one the target defines")
    ap.add_argument("--sanitize", action="append", metavar="LIST", help="comma-separated sanitizers, or `none`")
    ap.add_argument("--cfg", action="append", default=[], metavar="OPT", help="`flag` or `key=value`; wins over everything")
    ap.add_argument("-I", dest="include", action="append", default=[], metavar="DIR", help="import directory, searched in order after the importing file's own")
    ap.add_argument("--sysroot", metavar="DIR", help="the only place `import` reads from")
    report.add_format_option(ap)
    ap.add_argument("--Werror", action="store_true", help="warnings fail the build")
    ap.add_argument("--manifest", metavar="FILE", help="also write the ABI manifest of the file")
    ap.add_argument("--depfile", metavar="FILE", help="Makefile-style depfile: outputs and the source")
    ap.add_argument("--list-outputs", action="store_true", help="print the files that would be written and write nothing")
    ap.add_argument("--merge-manifest", metavar="OUT", help="merge the per-file manifests given as arguments into OUT")
    project.add_options(ap)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = make_parser()
    try:
        proj, argv = project.preload(sys.argv[1:] if argv is None else list(argv))
    except project.ProjectError as e:
        print(f"kifc: {e}", file=sys.stderr)
        return 2
    if proj:
        ap.set_defaults(**proj.defaults("build"))  # the command line still wins
    args, rest = ap.parse_known_args(argv)

    if args.merge_manifest:
        inputs = ([args.file] if args.file else []) + rest
        return merge_manifests(args.merge_manifest, inputs)
    if args.c_common:
        try:
            tgt = targetdef.load(args.target)
            write_if_changed(os.path.join(args.c_common, "kif_c.h"), lower_c.common_header(tgt))
        except (targetdef.TargetError, OSError) as e:
            print(f"kifc: {e}", file=sys.stderr)
            return 2
        return 0
    if not args.file and not rest and proj and proj.sources():
        status = 0  # `kif build` with no file: every `[build] sources` file, each as if named on the command line
        for src in proj.sources():
            args.file = src
            args.manifest = args.depfile = None  # one output name cannot serve several sources
            status = max(status, _build_one(ap, args, src))
        return status
    if rest or not args.file:
        ap.error("exactly one input .kif file is needed")
    return _build_one(ap, args, args.file)


def _build_one(ap: argparse.ArgumentParser, args: argparse.Namespace, path: str) -> int:
    if args.cpp:
        print("kifc: the C++ backend is not implemented yet; use --c DIR or --freebsd DIR", file=sys.stderr)
        return 2
    if not (args.c or args.freebsd):
        print("kifc: no backend selected (use --c DIR or --freebsd DIR)", file=sys.stderr)
        return 2
    return compile_c(args, path)


if __name__ == "__main__":
    sys.exit(main())
