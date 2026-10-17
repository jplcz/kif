# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kif`: python3 -m kiflang.kif fmt|lint|graph|doc|fix|manifest|target ... (docs/kif-tools.md, docs/kif-abi.md)."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import List, Optional

from . import doc as docgen
from . import graph as graphgen
from . import manifest as abi
from . import ir as irmod
from . import passes as passmgr
from . import project, quickfix, report, serial
from . import target as targetdef
from . import cfg as cfgmod
from .diagnostics import ERROR
from .fmt import FormatError, format_text
from .lint import RULES, Config, find_config, lint, parse_config
from .symbols import analyze
from . import signpolicy
from .sysroot import Sysroot, SysrootError


def _sysroot(args: argparse.Namespace) -> Optional[Sysroot]:
    return Sysroot(args.sysroot, signpolicy.from_args(args)) if getattr(args, "sysroot", None) else None


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _config(args: argparse.Namespace, path: str) -> Config:
    cfg_path = args.config or (None if args.no_config else find_config(path))
    return parse_config(_read(cfg_path), cfg_path) if cfg_path else Config()


def cmd_fmt(args: argparse.Namespace) -> int:
    changed = False
    for path in args.files:
        text = sys.stdin.read() if path == "-" else _read(path)
        try:
            new = format_text(text, args.indent)
        except FormatError as e:
            print(f"{path}:{e}", file=sys.stderr)
            return 2
        if path == "-":
            sys.stdout.write(new)
        elif new != text:
            changed = True
            if args.diff:
                sys.stdout.writelines(difflib.unified_diff(text.splitlines(True), new.splitlines(True), path, path + " (formatted)"))
            elif args.check:
                print(f"{path}: would be reformatted", file=sys.stderr)
            else:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new)
                print(f"reformatted {path}", file=sys.stderr)
    return 1 if changed and (args.check or args.diff) else 0


def cmd_lint(args: argparse.Namespace) -> int:
    if args.list_rules:
        for rule, (level, what) in RULES.items():
            print(f"{rule}  {level:<8} {what}")
        return 0
    if not args.files:
        print("kif lint: no input files", file=sys.stderr)
        return 2
    try:
        baselines = [abi.loads(_read(b), b) for b in args.baseline]
    except (ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    rep = report.Reporter(args.out_format, sys.stdout, "kif-lint")
    for path in args.files:
        try:
            cfg = _config(args, path)
            mod = analyze(os.path.realpath(path), _read(path), sysroot=_sysroot(args))
        except (ValueError, SysrootError, OSError) as e:
            print(e, file=sys.stderr)
            return 2
        found = sorted(mod.diagnostics, key=lambda d: (d.range.start, d.code))
        if not any(d.severity == ERROR for d in found):  # a file that does not compile has nothing meaningful to lint
            found += passmgr.check_module(mod.tree)
            found += lint(mod, cfg, abi.pick(baselines, path))
        for d in found:
            rep.add(path, d)
    rep.finish()
    return 1 if rep.failed(args.Werror) else 0


def cmd_manifest(args: argparse.Namespace) -> int:
    if len(args.files) > 1 and not args.output_dir:
        print("kif manifest: several files need --output-dir (one <stem>.json each)", file=sys.stderr)
        return 2
    for path in args.files:
        try:
            mod = analyze(os.path.realpath(path), _read(path), sysroot=_sysroot(args))
        except (SysrootError, OSError) as e:
            print(e, file=sys.stderr)
            return 2
        errors = [d for d in mod.diagnostics if d.severity == ERROR]
        for d in errors:
            print(d.format(path), file=sys.stderr)
        if errors:
            return 1
        text = abi.dumps(abi.manifest_of(mod, path))
        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            out = os.path.join(args.output_dir, os.path.splitext(os.path.basename(path))[0] + ".json")
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
    return 0


def cmd_ir(args: argparse.Namespace) -> int:
    if args.list_passes:
        for p in passmgr.REGISTRY.values():
            notes = [f"{k} {', '.join(v)}" for k, v in (("needs", p.requires), ("after", p.after), ("before", p.before)) if v]
            print(f"{p.name:10} {p.kind:9}  {p.summary}" + (f" ({'; '.join(notes)})" if notes else ""))
        return 0
    if not args.file:
        print("kif ir: give a FILE (or --list-passes)", file=sys.stderr)
        return 2
    try:
        names = passmgr.pipeline(args.passes) if args.passes else [] if args.hash else passmgr.pipeline("default") if args.explain_taint or args.taint_report or args.wipe_report else (["prove"] if args.prove else [])
        passmgr.order(names)
    except passmgr.PassError as e:
        print(f"kif ir: {e}", file=sys.stderr)
        return 2
    if args.attest and not args.output:
        print("kif ir: --attest writes bytes; give -o FILE", file=sys.stderr)
        return 2
    if (args.sign or args.cert) and not args.attest:
        print("kif ir: --sign and --cert need --attest", file=sys.stderr)
        return 2
    if args.format == "cbor" and not args.output:
        print("kif ir: --format cbor writes bytes; give -o FILE", file=sys.stderr)
        return 2
    try:
        mod = analyze(os.path.realpath(args.file), _read(args.file), sysroot=_sysroot(args))
    except (SysrootError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    errors = [d for d in mod.diagnostics if d.severity == ERROR]
    for d in errors:
        print(d.format(args.file), file=sys.stderr)
    if errors:
        return 1
    if args.attest:
        from . import attest, modsign

        try:
            if args.cert and not args.sign:
                raise modsign.CryptoError("--cert needs --sign")
            key = cert = None
            if args.sign:
                with open(args.sign, "rb") as f:
                    key = f.read()
                if args.cert:
                    with open(args.cert, "rb") as f:
                        cert = f.read()
            data = attest.build(mod, args.target or "c", args.profile, (key, cert) if key is not None else None)
        except (OSError, modsign.CryptoError, targetdef.TargetError) as e:
            print(f"kif ir: {e}", file=sys.stderr)
            return 2
        with open(args.output, "wb") as f:
            f.write(data)
        return 0
    if args.ast:
        out = serial.dumps_ast(mod.tree, args.format == "cbor") if args.format != "text" else None
        if out is None:
            print("kif ir: --ast needs --format json or cbor", file=sys.stderr)
            return 2
    else:
        ran = passmgr.run_module(mod.tree, names)
        built = ran.module
        for name, why in built.skipped:
            print(f"kif ir: {name}: skipped ({why})", file=sys.stderr)
        found = [d for r in ran.functions for d in r.diagnostics]
        for d in found:
            print(d.format(args.file), file=sys.stderr)
        if args.stats:
            total: dict = {}
            for r in ran.functions:
                for s in r.stats:
                    total[s.name] = total.get(s.name, 0.0) + s.seconds
            for n, sec in total.items():
                print(f"kif ir: pass {n}: {sec * 1000:.1f} ms over {len(ran.functions)} functions", file=sys.stderr)
        if any(d.severity == ERROR for d in found):
            return 1
        if args.hash:
            from . import irhash

            out = "".join(f"{h}  {f.name}  {args.file}:{f.node.range.start.line + 1 if f.node is not None else 0}\n"
                          for f in built.functions for h in [irhash.ir_hash(f)])
        elif args.taint_report:
            from . import taint_ir

            out = taint_ir.report(built, args.file, mod.imported)
        elif args.wipe_report:
            from . import wipes

            out = wipes.report(built, mod.tree, args.file)
        elif args.explain_taint:
            from . import taint_ir

            out = "".join(f"{args.file}:{n.line}:{n.col}: {n.kind}: {n.text} [{n.function}]\n" for f in built.functions for n in taint_ir.explain(f))
        elif args.explain_checks:
            from . import hints

            out = "".join(f"{args.file}:{h.line + 1}:{h.col + 1}: {h.kind} check {'proved: ' + h.why if h.proved else 'kept'}\n"
                          for f in hints.functions_of(mod.tree) for h in hints.function_hints(f))
        elif args.coverage:
            from . import ir_c

            out = ir_c.emitter_report(mod)
        else:
            out = irmod.dump_module(built) if args.format == "text" else serial.dumps_ir(built, args.format == "cbor")
    if args.output:
        with open(args.output, "wb" if isinstance(out, bytes) else "w", **({} if isinstance(out, bytes) else {"encoding": "utf-8", "newline": "\n"})) as f:
            f.write(out)
    else:
        sys.stdout.write(out)
    return 0


def _cmake_quote(v: object) -> str:
    return '"' + str(v).replace("\\", "/").replace('"', '\\"') + '"'


def cmd_new(args: argparse.Namespace) -> int:
    from . import scaffold

    try:
        written = scaffold.create(args.directory, args.kind, args.target, args.toml)
    except FileExistsError as e:
        print(f"kif: {e}", file=sys.stderr)
        return 1
    for w in written:
        print(f"{args.directory}/{w}")
    print(f"next: cmake -S {args.directory} -B {args.directory}/build -G Ninja && cmake --build {args.directory}/build")
    return 0


def cmd_completion(args: argparse.Namespace) -> int:
    from . import complete_sh

    sys.stdout.write(complete_sh.script(args.shell))
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    proj = args.project_obj
    if proj is None:
        print("kif project: no kif.toml found (use --project FILE)", file=sys.stderr)
        return 2
    values = {**proj.shared, **proj.sections["build"], **{f"lint_{k}": v for k, v in proj.sections["lint"].items()},
              **{f"fmt_{k}": v for k, v in proj.sections["fmt"].items()}, **proj.sections["signing"]}
    values["sources"] = proj.sources()
    if args.cmake:
        print(f"# generated by `kif project --cmake` from {proj.path}")
        # the keys Kif.cmake uses: the shared ones and the source list (output directories are CMake's business)
        names = {"target": "TARGET", "profile": "PROFILE", "sanitize": "SANITIZE", "cfg": "CFG", "include": "INCLUDE",
                 "sysroot": "SYSROOT", "Werror": "WERROR", "sources": "SOURCES",
                 "trust": "TRUST", "revoked": "REVOKED", "signing": "SIGNING", "prover": "PROVER"}
        for key, cm in names.items():
            if key in values:
                v = values[key]
                if isinstance(v, bool):
                    print(f"set(KIF_PROJECT_{cm} {'ON' if v else 'OFF'})")
                elif isinstance(v, list):
                    print(f"set(KIF_PROJECT_{cm} {' '.join(_cmake_quote(x) for x in v)})")
                else:
                    print(f"set(KIF_PROJECT_{cm} {_cmake_quote(v)})")
        return 0
    if args.json:
        print(json.dumps({"file": proj.path, **values}, indent=2))
    else:
        print(f"{proj.path}:")
        for k, v in sorted(values.items()):
            print(f"  {k} = {v}")
    return 0


def cmd_target(args: argparse.Namespace) -> int:
    if args.action == "list":
        for name in targetdef.builtin_names():
            t = targetdef.load(name)
            print(f"{name:16} {t.language:4} kobj={'yes' if t.supports('kobj') else 'no':3}  {t.get('description', '')}")
        return 0
    if not args.targets:
        print(f"kif target {args.action}: name a built-in target or a .json file", file=sys.stderr)
        return 2
    status = 0
    for ref in args.targets:
        if args.action == "check":
            problems = targetdef.check_file(ref) if os.path.isfile(ref) else []
            if not os.path.isfile(ref):
                try:
                    targetdef.load(ref)
                except targetdef.TargetError as e:
                    problems = [str(e)]
            for m in problems:
                print(f"{ref}: {m}", file=sys.stderr)
            status = 1 if problems else status
        elif args.action == "cfg":
            try:
                print(f"# {ref}")
                print("\n".join(targetdef.load(ref).cfg_env(args.cfg, args.profile, [s for o in args.sanitize for s in o.split(",") if s] if args.sanitize else None).lines()))
            except (targetdef.TargetError, cfgmod.CfgError) as e:
                print(e, file=sys.stderr)
                return 2
        else:
            try:
                sys.stdout.write(targetdef.load(ref).dumps())
            except targetdef.TargetError as e:
                print(e, file=sys.stderr)
                return 2
    return status


def cmd_graph(args: argparse.Namespace) -> int:
    try:
        mods = [analyze(os.path.realpath(p), _read(p), sysroot=_sysroot(args)) for p in args.files]
    except (SysrootError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    g = graphgen.build(mods)
    text = graphgen.to_mermaid(g, args.files_only) if args.format == "mermaid" else graphgen.to_dot(g, args.files_only)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_doc(args: argparse.Namespace) -> int:
    for path in args.files:
        try:
            mod = analyze(os.path.realpath(path), _read(path), sysroot=_sysroot(args))
        except (SysrootError, OSError) as e:
            print(e, file=sys.stderr)
            return 2
        errors = [d for d in mod.diagnostics if d.severity == ERROR]
        for d in errors:
            print(d.format(path), file=sys.stderr)
        if errors:
            return 1
        text = docgen.render(mod)
        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            with open(docgen.doc_path(args.output_dir, path), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    status = 0
    for path in args.files:
        try:
            text, cfg = _read(path), _config(args, path)
            sysroot = _sysroot(args)
        except (ValueError, SysrootError, OSError) as e:
            print(e, file=sys.stderr)
            return 2
        key = os.path.realpath(path)
        if not (args.apply or args.diff):
            mod, diags = quickfix.check(key, text, cfg, sysroot=sysroot)
            for d in diags:
                print(d.format(path))
                for n, f in enumerate(quickfix.fixes_for(mod, d, diags), 1):
                    flag = "auto" if f.auto else "preferred" if f.preferred else ""
                    print(f"    fix {n}: {f.title}" + (f"  [{flag}]" if flag else ""))
                status = 1
            continue
        new, applied = quickfix.fix_all(key, text, cfg, sysroot=sysroot, unsafe=args.unsafe)
        if args.diff:
            sys.stdout.write(quickfix.diff(text, new, path))
        elif new != text:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
        for d, f in applied:
            print(f"{path}:{d.range.start.line + 1}: {d.code}: {f.title}", file=sys.stderr)
        if any(d.severity == ERROR for d in quickfix.check(key, new, cfg, sysroot=sysroot)[1]):
            status = 1
    return status


# Subcommands that hand their whole command line to the tool that already exists (its options are that tool's own).
# name -> (help, function taking the remaining arguments)
def _delegates() -> "dict":
    def check(rest: List[str]) -> int:
        from . import __main__ as m
        return m.main(["check", *rest])

    def export(rest: List[str]) -> int:
        from . import __main__ as m
        return m.main(["export", *rest])

    def modules(rest: List[str]) -> int:
        from . import __main__ as m
        return m.main(["modules", *rest])

    def build(rest: List[str]) -> int:
        from . import kifc
        return kifc.main(rest)

    def lsp(rest: List[str]) -> int:
        from . import lsp as server
        return server.main()

    def import_m(rest: List[str]) -> int:
        from . import mimport
        return mimport.main(rest)

    return {
        "check": ("type-check a file (diagnostics, --target/--cfg/--profile, --print-cfg); same as kif-check", check),
        "build": ("generate C (and FreeBSD kobj) sources; same as kifc", build),
        "export": ("write a .kifm module file; same as `kif-check export`", export),
        "modules": ("list the modules of a sysroot; same as `kif-check modules`", modules),
        "lsp": ("run the language server on stdio; same as kif-lsp", lsp),
        "import-m": ("convert a FreeBSD kobj .m file to .kif; same as kif-mimport", import_m),
    }


def _watch(name: str, fn, argv: List[str]) -> int:
    """`kif check|build --watch [--interval SECONDS]`: rerun the tool after every change (see watch.py)."""
    from . import watch

    rest, interval, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--watch":
            pass
        elif argv[i] == "--interval" and i + 1 < len(argv):
            interval = float(argv[i + 1])
            i += 1
        else:
            rest.append(argv[i])
        i += 1
    try:
        proj, _ = project.preload(rest)
    except project.ProjectError as e:
        print(f"kif: {e}", file=sys.stderr)
        return 2
    if interval is None:
        interval = (proj.defaults("watch").get("interval") if proj else None) or watch.DEFAULT_INTERVAL
    sysroot = ""
    for j, a in enumerate(rest):
        if a == "--sysroot" and j + 1 < len(rest):
            sysroot = rest[j + 1]
    sysroot = sysroot or (proj.defaults("").get("sysroot", "") if proj else "")
    return watch.watch(lambda: fn(rest), watch.roots_for(rest, sysroot), interval)


def make_parser() -> "tuple":
    """(the top-level parser, its subparsers action): `kif completion` reads the option names from them."""
    delegates = _delegates()
    ap = argparse.ArgumentParser(prog="kif", description="Compiler front end, formatter, linter, graph, documentation and quick fixes for .kif files.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (text, _fn) in delegates.items():
        sub.add_parser(name, help=text, add_help=False)  # dispatched above: `kif NAME --help` shows that tool's help

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--sysroot", metavar="DIR", help="the only place `import` reads from (default: the shipped prelude)")
        signpolicy.add_options(p)

    def lint_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", metavar="FILE", help="rule levels (default: the nearest .kiflint)")
        p.add_argument("--no-config", action="store_true", help="ignore .kiflint files")

    f = sub.add_parser("fmt", help="format .kif files (in place; '-' filters stdin to stdout)")
    f.add_argument("files", nargs="+")
    f.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file would change")
    f.add_argument("--diff", action="store_true", help="write nothing; print a unified diff; exit 1 if a file would change")
    f.add_argument("--indent", type=int, default=4, help="spaces per level (default 4)")
    f.set_defaults(run=cmd_fmt)

    lt = sub.add_parser("lint", help="naming, ABI-shape and hygiene rules")
    lt.add_argument("files", nargs="*")
    lint_opts(lt)
    common(lt)
    lt.add_argument("--baseline", metavar="MANIFEST", action="append", default=[],
                    help="a released ABI manifest (`kif manifest`); repeatable, each is matched to its input by file name; enables KIFL020-033")
    report.add_format_option(lt)
    lt.add_argument("--Werror", action="store_true", help="warnings fail the run")
    lt.add_argument("--list-rules", action="store_true", help="print every rule with its default level")
    lt.set_defaults(run=cmd_lint)

    g = sub.add_parser("graph", help="import / extends / implements graph as Graphviz DOT or Mermaid")
    g.add_argument("files", nargs="+")
    g.add_argument("--format", choices=("dot", "mermaid"), default="dot")
    g.add_argument("--files-only", action="store_true", help="one node per file, import edges only")
    g.add_argument("-o", "--output", metavar="FILE", help="write here instead of standard output")
    common(g)
    g.set_defaults(run=cmd_graph)

    ir_ = sub.add_parser("ir", help="the mid-level IR of a file (docs/kif-ir.md) as text, JSON or CBOR; --ast dumps the syntax tree instead")
    ir_.add_argument("file", nargs="?")
    ir_.add_argument("--format", choices=("text", "json", "cbor"), default="text")
    ir_.add_argument("--ast", action="store_true", help="the syntax tree instead of the IR (json or cbor)")
    ir_.add_argument("--prove", action="store_true", help="run the check provers first: proved checks show [proved: reason] (same as --passes prove)")
    ir_.add_argument("--passes", metavar="LIST", help="run these IR passes first: a pipeline (default = every pass, verify, prove) or pass names, in order; `-name` removes one, e.g. default,-prove,mypass (docs/kif-ir-passes.md)")
    ir_.add_argument("--list-passes", action="store_true", help="list the IR passes and exit")
    ir_.add_argument("--explain-checks", action="store_true", help="list every run-time check of the file as proved (with the proof) or kept (docs/kif-ir-passes.md)")
    ir_.add_argument("--taint-report", action="store_true", help="the taint audit for a security review: sanitizers and their call sites, #[trusted] functions, every untaint! reason, every barrier and tainted source (docs/kif-taint.md)")
    ir_.add_argument("--wipe-report", action="store_true", help="the secure-wipe audit for a security review: secure structs (wiped and exempt fields), #[secure_wipe] functions, every secure variable with its wipe sites (docs/kif-secure-wipe.md)")
    ir_.add_argument("--hash", action="store_true", help="one line per function: the sha256 of its raw IR, which ignores comments, names and positions (docs/kif-module-contracts.md, section 13)")
    ir_.add_argument("--attest", action="store_true", help="write a .kifa attestation (name -> IR hash, optionally signed) to -o FILE (docs/kif-module-contracts.md, section 13.2)")
    ir_.add_argument("--target", help="with --attest: the target recorded (a built-in name or .json file; default c)")
    ir_.add_argument("--profile", help="with --attest: the profile recorded")
    ir_.add_argument("--sign", metavar="KEY.pem", help="with --attest: sign with this Ed25519 private key (PKCS#8 PEM, read from the file only)")
    ir_.add_argument("--cert", metavar="CERT", help="with --attest --sign: embed this certificate (PEM or DER) of the key")
    ir_.add_argument("--explain-taint", action="store_true", help="list the taint sources, every speculation barrier (written or inserted, with its reason) and every untaint! reason (docs/kif-taint.md)")
    ir_.add_argument("--coverage", action="store_true", help="report which functions the IR describes completely enough to emit code from (docs/kif-ir-codegen.md)")
    ir_.add_argument("--stats", action="store_true", help="print the time each pass took")
    ir_.add_argument("-o", "--output", metavar="FILE", help="write here instead of standard output (required for cbor)")
    common(ir_)
    ir_.set_defaults(run=cmd_ir)

    d = sub.add_parser("doc", help="Markdown documentation")
    d.add_argument("files", nargs="+")
    d.add_argument("-o", "--output-dir", metavar="DIR", help="write <stem>.md files here instead of standard output")
    common(d)
    d.set_defaults(run=cmd_doc)

    m = sub.add_parser("manifest", help="write the ABI manifest (JSON) of a file for `kif lint --baseline`")
    m.add_argument("files", nargs="+")
    m.add_argument("-o", "--output-dir", metavar="DIR", help="write <stem>.json files here instead of standard output")
    common(m)
    m.set_defaults(run=cmd_manifest)

    x = sub.add_parser("fix", help="list or apply quick fixes for diagnostics and lint findings")
    x.add_argument("files", nargs="+")
    x.add_argument("--apply", action="store_true", help="rewrite the files with the auto fixes (the ones that cannot change meaning)")
    x.add_argument("--unsafe", action="store_true", help="with --apply or --diff: also apply every preferred fix")
    x.add_argument("--diff", action="store_true", help="write nothing; print what --apply would change")
    lint_opts(x)
    common(x)
    x.set_defaults(run=cmd_fix)

    pr = sub.add_parser("project", help="show or check the kif.toml project file; --cmake writes what Kif.cmake reads")
    pr.add_argument("--cmake", action="store_true", help="print CMake `set()` lines (KIF_PROJECT_*) for kif_use_project()")
    pr.add_argument("--json", action="store_true", help="print the resolved values as JSON (default: a readable list)")
    pr.set_defaults(run=cmd_project)

    t = sub.add_parser("target", help="list, show (merged JSON) or check target definitions for the C/C++ backends")
    t.add_argument("action", choices=("list", "show", "check", "cfg"))
    t.add_argument("targets", nargs="*", help="a built-in name or a .json file")
    t.add_argument("--cfg", action="append", default=[], metavar="OPT",
                   help="with `cfg`: override key=value, flag or !flag, as the compiler's --cfg does")
    t.add_argument("--profile", help="with `cfg`: the build profile")
    t.add_argument("--sanitize", action="append", metavar="LIST", help="with `cfg`: comma-separated sanitizers, or `none`")
    t.set_defaults(run=cmd_target)

    n = sub.add_parser("new", help="scaffold a CMake + kif.toml project (driver, app, staticlib or solib)")
    n.add_argument("directory", help="where to create the project (its name becomes the library name)")
    n.add_argument("--kind", choices=("driver", "app", "staticlib", "solib", "library"), default="driver",
                   help="driver: kobj interface for a kernel; app: executable with `fn main`; staticlib/solib: C library (library = staticlib)")
    n.add_argument("--target", default="", help="target name (default: freebsd-kernel for a driver, c otherwise)")
    n.add_argument("--toml", action="store_true", help="also write kif.toml (shared with the kif command line tools) and call kif_use_project()")
    n.set_defaults(run=cmd_new)

    cp = sub.add_parser("completion", help="print a shell completion script (bash, zsh or fish)")
    cp.add_argument("shell", choices=("bash", "zsh", "fish"))
    cp.set_defaults(run=cmd_completion)

    return ap, sub


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    delegates = _delegates()
    if argv and argv[0] in delegates:
        if argv[0] in ("check", "build") and "--watch" in argv:
            return _watch(argv[0], delegates[argv[0]][1], argv[1:])
        return delegates[argv[0]][1](argv[1:])
    ap, sub = make_parser()
    try:
        proj, argv = project.preload(argv)
    except project.ProjectError as e:
        print(f"kif: {e}", file=sys.stderr)
        return 2
    if proj:
        for name, sp in sub.choices.items():
            sp.set_defaults(**proj.defaults({"fmt": "fmt", "lint": "lint", "fix": "lint"}.get(name, ""), "signing"))
    args = ap.parse_args(argv)
    args.project_obj = proj
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
