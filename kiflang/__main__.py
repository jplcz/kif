# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Command line: `python -m kiflang check|export|modules` (docs/kif-proc-language.md section 15).

    python -m kiflang check  FILE [--sysroot DIR] [--target NAME|FILE] [--profile P] [--sanitize LIST] [--cfg OPT]... [--print-cfg]
    python -m kiflang export FILE -o OUT.kifm [--sysroot DIR] [--target NAME|FILE] [--profile P] [--sanitize LIST] [--cfg OPT]...

`--target` picks a target definition (kiflang/targets, docs/kif-targets.md); each `--cfg` overrides what `#[cfg(..)]` sees:
`arch=arm64` sets a key, `smp` sets a flag, `!smp` clears it.
    python -m kiflang modules --sysroot DIR
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import cfg, modfile, modsign, project, report, signpolicy, target
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
    for name in ("check", "export", "modules", "inspect", "verify"):
        p = sub.add_parser(name)
        if name != "modules":
            p.add_argument("file")
        p.add_argument("--sysroot", help="the only place `import` reads from (default: the prelude shipped with the compiler)")
        if name in ("check", "export"):
            report.add_format_option(p)
            p.add_argument("--target", help="a built-in target name or a target .json file")
            p.add_argument("--cfg", action="append", default=[], metavar="OPT", help="key=value, flag or !flag (repeatable)")
            p.add_argument("--profile", help="build profile: debug (default), perf, release, sanitize, or one the target defines")
            p.add_argument("--sanitize", action="append", metavar="LIST",
                           help="comma-separated sanitizers (address,undefined,..); `none` clears; replaces the profile's list")
            signpolicy.add_options(p)
            p.add_argument("--prover-must-match", action="store_true",
                           help="same as --prover same: an error, not a warning, when a .kifm was proved by another compiler than this one and #[require_proved] relies on it (KIFI041)")
            p.add_argument("--print-cfg", action="store_true", help="print the effective cfg set and exit")
        if name == "export":
            p.add_argument("-o", "--output", required=True)
            p.add_argument("--sign", metavar="KEY.pem", help="sign the .kifm with this Ed25519 private key (PKCS#8 PEM, read from the file only)")
            p.add_argument("--cert", metavar="CERT", help="embed this certificate (PEM or DER) of the signing key; needs --sign")
        if name == "verify":
            p.add_argument("--trust", action="append", default=[], metavar="PEM", help="pinned public keys and certificates (repeatable)")
            p.add_argument("--revoked", action="append", default=[], metavar="PEM", help="revoked keys, certificates and CRLs (repeatable)")
            p.add_argument("--allow-revoked", action="append", default=[], metavar="FINGERPRINT", help="accept this revoked key or certificate anyway")
    return ap, sub


def _signer(args):
    """`(key PEM, certificate bytes or None)` for `export --sign`; None when not signing. The key is read from the file, never from the command line."""
    if args.cert and not args.sign:
        raise modsign.CryptoError("--cert needs --sign")
    if not args.sign:
        return None
    with open(args.sign, "rb") as f:
        key = f.read()
    cert = None
    if args.cert:
        with open(args.cert, "rb") as f:
            cert = f.read()
    return key, cert


def _verify(args) -> int:
    """`kiflang verify FILE.kifm`: who signed it and whether the consumer's trust list vouches for it. Exit 0 only when trusted."""
    try:
        with open(args.file, "rb") as f:
            h = modfile.decode(f.read())
        crypto = modsign.backend()
        trust = modsign.load_list(args.trust) if args.trust else None
        revoked = modsign.load_list(args.revoked) if args.revoked else None
        v = modsign.verify(h, trust, revoked, args.allow_revoked, crypto)
    except modsign.CryptoUnavailable as exc:
        print(f"kiflang: {exc}", file=sys.stderr)
        return 2
    except (OSError, modfile.ModuleFileError, modsign.CryptoError) as exc:
        print(f"kiflang: {args.file}: {exc}", file=sys.stderr)
        return 1
    if v.status == "unsigned":
        print(f"{args.file}: unsigned")
        return 1
    print(f"{args.file}: {v.status}: signed by {v.signer} ({v.fingerprint})")
    if v.detail:
        print(f"  {v.detail}")
    if v.validity:
        print(f"  certificate valid {v.validity[0]} to {v.validity[1]} (dates are reported, not enforced)")
    for n in v.notes:
        print(f"  note: {n}")
    return 0 if v.status == "trusted" else 1


def _inspect(path: str) -> int:
    """Print what a `.kifm` records (docs/kif-module-contracts.md section 8): identity, compiler, and the security contract of every function."""
    from . import modfile

    try:
        with open(path, "rb") as f:
            h = modfile.decode(f.read())
    except (OSError, modfile.ModuleFileError) as exc:
        print(f"kiflang: {path}: {exc}", file=sys.stderr)
        return 1
    sec, comp = h["security"], h["compiler"]
    print(f"{path}: module {h['name']}")
    print(f"  compiler  {comp['name']} {comp['version']}  prover {comp['prover']}")
    print(f"  built for target {sec['target']} ({sec['language']}), profile {sec['profile']}, checked={sec['checked']}")
    print(f"  taint     exports={sec['taint']['exports']}  sinks={', '.join(sec['taint']['sinks']) or '-'}")
    print(f"  digest    {h['security_digest']}")
    sig = h.get("signature")
    print("  signature " + (f"{sig['alg']} by {sig['signer']} ({modsign.fingerprint(sig['public_key'])}), not verified: use `verify`" if sig else "none (unsigned)"))
    for key, e in sorted(sec["functions"].items()):
        if e.get("embedded"):
            print(f"  {key}: embedded (generic: its source is in the file and is checked by the importer)")
            continue
        tags = [t for t, on in (("export", e["export"]), ("trusted", e["trusted"]), ("tainted_result", e["tainted_result"]), ("unsafe", e["unsafe"])) if on]
        if e["untaint"]:
            tags.append(f"untaint({e['untaint']})")
        if e["tainted_params"]:
            tags.append("tainted_params=" + ",".join(e["tainted_params"]))
        kept = e["kept_checks"]
        tags.append("kept=" + (kept if isinstance(kept, str) else ",".join(kept) or "none"))
        if e["proved"] != "unknown" and e["proved"]:
            tags.append("proved=" + ",".join(e["proved"]))
        if e["ir_hash"]:
            tags.append(e["ir_hash"][:23])
        print(f"  {key}: " + " ".join(tags))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap, sub = make_parser()
    try:
        proj, argv = project.preload(sys.argv[1:] if argv is None else list(argv))
    except project.ProjectError as exc:
        print(f"kiflang: {exc}", file=sys.stderr)
        return 2
    if proj:
        for name, sp in sub.choices.items():
            sp.set_defaults(**proj.defaults("check", "signing"))  # the command line still wins
    args = ap.parse_args(argv)
    try:
        policy = signpolicy.from_args(args) if args.cmd in ("check", "export") else None
        sysroot = Sysroot(args.sysroot, policy) if args.sysroot else Sysroot(Sysroot.shipped().root, policy)
        if args.cmd == "verify":
            return _verify(args)
        if args.cmd == "inspect":
            return _inspect(args.file)
        if args.cmd == "modules":
            print("\n".join(sysroot.modules()))
            return 0
        env = None
        if args.cmd == "export" or (args.cmd == "check" and (args.target or args.cfg or args.profile or args.sanitize or args.prover_must_match or args.prover == "same")):
            tgt = target.load(args.target or "c")
            env = tgt.cfg_env(args.cfg, args.profile, _sanitizers(args.sanitize))
        if (getattr(args, "prover_must_match", False) or getattr(args, "prover", "any") == "same") and env is not None:
            env.values["prover_must_match"] = {"1"}
        if getattr(args, "print_cfg", False):
            print("\n".join((env or cfg.Env()).lines()))
            return 0
        with open(args.file, encoding="utf-8") as f:
            mod = analyze(args.file, f.read(), sysroot=sysroot, cfg_env=env)
    except (SysrootError, OSError, cfg.CfgError, target.TargetError, modsign.CryptoError) as exc:
        print(f"kiflang: {exc}", file=sys.stderr)
        return 2
    status = _report(mod, args.file, args.out_format)
    if args.cmd == "export" and status == 0:
        try:
            sign = _signer(args)
        except (modsign.CryptoError, OSError) as exc:
            print(f"kiflang: {exc}", file=sys.stderr)
            return 2
        try:
            data = modfile.export(mod, tgt.name, args.profile, sign)
        except modsign.CryptoError as exc:
            print(f"kiflang: {exc}", file=sys.stderr)
            return 2
        with open(args.output, "wb") as out:
            out.write(data)
    return status


if __name__ == "__main__":
    sys.exit(main())
