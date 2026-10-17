# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The `security` section of a `.kifm` (docs/kif-module-contracts.md, sections 3 and 4).

The writer (`build`) records every safety contract and security-boundary fact an importer's checks depend on. The reader (`check`)
recomputes what can be derived from the declarations in the file's own `text` and compares, so a fact edited without the vendor's
build is a KIF014.

Facts split in two:

* **declared**: derived from the declarations alone (`DERIVED`), so the reader can verify them against `text`;
* **informational**: results of the vendor's analysis of bodies the importer never sees (`barriers`, `kept_checks`, `proved`,
  `ir_hash`). They are bound to the file by `security_digest` and, when signed, by the signature, not by the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import ast, manifest

SCHEMA = 1
DERIVED = ("export", "tainted_params", "tainted_result", "untaint", "trusted", "contracts", "unsafe")
INFORMATIONAL = ("barriers", "kept_checks", "proved", "ir_hash")
UNKNOWN = "unknown"
_FACT_ATTRS = ("some", "non_null", "non_empty", "len_ge", "lt_len")
_FN_FACT_ATTRS = ("ensures", "preserves", "when", "len_of", "unchecked_form")


def _has(attrs, name: str) -> bool:
    return any(a.name.text == name for a in attrs)


def _spelled(a: ast.Attribute) -> str:
    return a.name.text + (f"({''.join(a.raw.split())})" if a.raw else "")


def _is_embedded(f, impl_generic: bool = False) -> bool:
    """Whether a function stays in full source in a `.kifm` (same rule as `modfile.strip`)."""
    return bool(f.is_gen or any(not g.is_lifetime for g in f.generics) or impl_generic)


def _entry(f, exports_all: bool) -> Dict[str, Any]:
    attrs = f.attrs
    export, trusted = _has(attrs, "export"), _has(attrs, "trusted")
    all_tainted = exports_all and export and not trusted
    untaint: Optional[str] = None
    for a in attrs:
        if a.name.text == "untaint":
            untaint = "trusted" if a.raw is not None else "checked"
    facts: List[Tuple[int, str, str]] = []
    for pos, p in enumerate(f.params):
        for a in p.attrs:
            if a.name.text in _FACT_ATTRS:
                facts.append((pos, a.name.text, f"{_spelled(a)}@{p.name.text}"))
    for a in attrs:
        if a.name.text in _FN_FACT_ATTRS:
            facts.append((len(f.params), a.name.text, _spelled(a)))
    return {
        "export": export,
        "tainted_params": [p.name.text for p in f.params if _has(p.attrs, "tainted") or (all_tainted and not p.receiver)],
        "tainted_result": _has(attrs, "tainted_result"),
        "untaint": untaint,
        "trusted": trusted,
        "contracts": [t for _, _, t in sorted(facts)],
        "unsafe": _has(attrs, "unsafe"),
    }


def _owner(target, iface=None) -> str:
    """The key prefix of the methods of an impl: `Type::` or, for an interface impl, `<Type as Iface>::`."""
    name = target.text if isinstance(target, ast.PathType) else manifest.type_text(target)
    return f"<{name} as {iface.text}>::" if iface is not None else name + "::"


@dataclass(frozen=True)
class Vendor:
    """Where a precompiled declaration came from and what its vendor recorded (`FnDecl.vendor`, set by `annotate`)."""

    module: str
    key: str
    entry: Dict[str, Any]
    compiler: Dict[str, Any]


def _decls(tree):
    """`(key, declaration, embedded)` for every function, method and interface method of a parsed module."""
    for it in tree.items:
        if isinstance(it, ast.FnDecl):
            yield it.name.text, it, _is_embedded(it)
        elif isinstance(it, ast.ExternFn):
            yield it.decl.name.text, it.decl, False
        elif isinstance(it, ast.ImplDecl):
            generic = any(not g.is_lifetime for g in it.generics)
            for m in it.items:
                if isinstance(m, ast.FnDecl):
                    yield _owner(it.target, it.iface) + m.name.text, m, _is_embedded(m, generic)
        elif isinstance(it, ast.ExternImpl):
            for m in it.fns:
                yield _owner(it.target) + m.name.text, m, False
        elif isinstance(it, ast.Interface):
            for m in it.methods:
                yield f"{it.name.text}::{m.name.text}", m, m.body is not None


def annotate(mod) -> None:
    """Mark the parameters that take tainted data (`Param.effective_taint`), so a call checks its arguments against the *effective* set:
    the attributes plus the exports policy. For a `.kifm` the set is the one the vendor recorded (the importer never re-applies a policy to
    a function it did not compile); for source it is the module's own policy."""
    from . import taint

    header = getattr(mod, "header", None)
    exports_all = taint.policy(getattr(mod, "cfg", None))[0]
    recorded = header["security"]["functions"] if header else None
    for key, f, embedded in _decls(mod.tree):
        if recorded is not None:
            names = [] if embedded else recorded.get(key, {}).get("tainted_params", [])
        else:
            names = _entry(f, exports_all)["tainted_params"]
        for p in f.params:
            if p.name.text in names:
                p.effective_taint = True
        if recorded is not None and not embedded and key in recorded:
            f.vendor = Vendor(mod.name, key, recorded[key], header["compiler"])
    if header:
        mod.security = header["security"]


def declared(tree, exports_all: bool) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """`(functions, types)` derived from the declarations of a parsed module: the part of `security` the reader can check."""
    fns: Dict[str, Dict[str, Any]] = {key: {"embedded": True} if embedded else _entry(f, exports_all) for key, f, embedded in _decls(tree)}
    types: Dict[str, Dict[str, Any]] = {}
    for it in tree.items:
        if isinstance(it, ast.StructDecl):
            fields = [f.name.text for f in it.fields if _has(f.attrs, "tainted")]
            if _has(it.attrs, "tainted") or fields:
                types[it.name.text] = {"tainted": _has(it.attrs, "tainted"), "tainted_fields": fields}
        elif isinstance(it, ast.EnumDecl) and _has(it.attrs, "tainted"):
            types[it.name.text] = {"tainted": True, "tainted_fields": []}
    return fns, types


# ---------------------------------------------------------------- the vendor's analysis of the compiled bodies


def _analysis(tree) -> Dict[str, Dict[str, Any]]:
    """`barriers`, `kept_checks`, `proved` and `ir_hash` per function the IR builder handles; the rest are `unknown`."""
    from . import irhash, ir, passes

    raw = {name: h for name, h in irhash.module_hashes(passes.run_module(tree, []).module)}
    out: Dict[str, Dict[str, Any]] = {}
    for f in passes.run_module(tree, passes.pipeline("default")).module.functions:
        kept, proved = set(), set()
        explicit = implicit = 0
        for b in f.blocks:
            for i in b.instrs:
                if isinstance(i, ir.Barrier):
                    explicit, implicit = (explicit, implicit + 1) if i.why else (explicit + 1, implicit)
                elif isinstance(i, ir.Check) and not (i.tag and not i.call):  # an entry assumption is the caller's to prove
                    (proved if i.proof is not None else kept).add(i.kind)
        for c in f.elided:
            if not (c.tag and not c.call):
                (proved if c.proof is not None else kept).add(c.kind)
        out[f.name] = {"barriers": {"explicit": explicit, "implicit": implicit}, "kept_checks": sorted(kept), "proved": sorted(proved - kept),
                       "ir_hash": raw.get(f.name)}
    return out


def build(mod, target_name: str, profile_name: str, language: str) -> Dict[str, Any]:
    """The `security` section of an analysed module."""
    from . import taint

    exports_all, sinks = taint.policy(getattr(mod, "cfg", None))
    fns, types = declared(mod.tree, exports_all)
    info = _analysis(mod.tree)
    for name, entry in fns.items():
        if entry.get("embedded"):
            continue
        entry.update(info.get(name, {"barriers": None, "kept_checks": UNKNOWN, "proved": UNKNOWN, "ir_hash": None}))
    return {
        "target": target_name, "profile": profile_name, "language": language,
        "taint": {"exports": "all" if exports_all else "declared", "sinks": sorted(sinks)},
        "checked": True, "functions": fns, "types": types,
    }


# ---------------------------------------------------------------- the reader


def validate(sec: Any) -> None:
    """Shape of the section; raises `ValueError` naming what is wrong."""
    def need(d: Any, key: str, kind: type, where: str) -> Any:
        if not isinstance(d, dict) or not isinstance(d.get(key), kind) or (kind is int and isinstance(d[key], bool)):
            raise ValueError(f"{where}{key!r} is missing or has the wrong type")
        return d[key]

    need(sec, "target", str, "security.")
    need(sec, "profile", str, "security.")
    need(sec, "language", str, "security.")
    taint = need(sec, "taint", dict, "security.")
    if need(taint, "exports", str, "security.taint.") not in ("all", "declared"):
        raise ValueError("security.taint.exports must be 'all' or 'declared'")
    sinks = need(taint, "sinks", list, "security.taint.")
    if not all(isinstance(s, str) for s in sinks):
        raise ValueError("security.taint.sinks must be a list of strings")
    need(sec, "checked", bool, "security.")
    for name, e in need(sec, "functions", dict, "security.").items():
        if not isinstance(e, dict):
            raise ValueError(f"security.functions[{name!r}] must be a map")
        if e.get("embedded") is True:
            continue
        for k, kind in (("export", bool), ("tainted_params", list), ("tainted_result", bool), ("trusted", bool), ("contracts", list), ("unsafe", bool)):
            need(e, k, kind, f"security.functions[{name!r}].")
        if e.get("untaint") not in (None, "checked", "trusted"):
            raise ValueError(f"security.functions[{name!r}].'untaint' must be null, 'checked' or 'trusted'")
        for k in ("kept_checks", "proved"):
            if not (e.get(k) == UNKNOWN or isinstance(e.get(k), list)):
                raise ValueError(f"security.functions[{name!r}].{k!r} must be a list or 'unknown'")
    for name, t in need(sec, "types", dict, "security.").items():
        need(t, "tainted", bool, f"security.types[{name!r}].")
        need(t, "tainted_fields", list, f"security.types[{name!r}].")


def check(tree, sec: Dict[str, Any]) -> List[str]:
    """What in `sec` disagrees with the declarations of the parsed `text`: one message per difference (empty: consistent)."""
    fns, types = declared(tree, sec["taint"]["exports"] == "all")
    out: List[str] = []
    have = sec["functions"]
    for name in sorted(set(fns) | set(have)):
        if name not in have:
            out.append(f"security section has no entry for {name!r}")
        elif name not in fns:
            out.append(f"security section describes {name!r}, which the module does not declare")
        elif fns[name].get("embedded") or have[name].get("embedded"):
            if bool(fns[name].get("embedded")) != bool(have[name].get("embedded")):
                out.append(f"security entry of {name!r} disagrees with the text about it being embedded")
        else:
            for k in DERIVED:
                if have[name].get(k) != fns[name][k]:
                    out.append(f"security.{name}.{k} is {have[name].get(k)!r} but the declarations say {fns[name][k]!r}")
    if sec["types"] != types:
        out.append("security.types does not match the tainted types and fields the declarations carry")
    return out


# ---------------------------------------------------------------- import-time compatibility (section 5)


def compat(importer_cfg, sec: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """`(severity, code, message)` for a `.kifm` imported by a build with cfg environment `importer_cfg` (None: nothing is known about it).

    KIF016 (error): another language, so the vendor's object code cannot be linked. KIF017 (warning): another target of the same
    language. KIFT012 (info): the importer's taint policy is stricter than the one the vendor's bodies were checked under."""
    from . import taint

    out: List[Tuple[str, str, str]] = []
    if importer_cfg is not None:
        values = importer_cfg.values
        language = "cxx" if "cxx" in importer_cfg.flags else "c" if "c" in importer_cfg.flags else None
        mine = next(iter(values.get("target", ())), None)
        if language is not None and sec["language"] != language:
            out.append(("error", "KIF016", f"compiled for the {sec['language']} target {sec['target']!r}, but this build targets {language}: its object code cannot be linked"))
        elif mine is not None and mine != sec["target"]:
            out.append(("warning", "KIF017", f"compiled for target {sec['target']!r} (profile {sec['profile']!r}), this build targets {mine!r}: the barrier "
                        "snippets and overflow behaviour inside its object code are the vendor's, not this target's"))
    exports_all, sinks = taint.policy(importer_cfg)
    theirs = set(sec["taint"]["sinks"])
    extra = sorted(sinks - theirs)
    if extra or (exports_all and sec["taint"]["exports"] != "all"):
        what = [f"sinks {extra}"] if extra else []
        if exports_all and sec["taint"]["exports"] != "all":
            what.append("`taint.exports: all`")
        out.append(("info", "KIFT012", f"its bodies were checked without {' and '.join(what)}, which this build's taint policy adds: the vendor's code was not checked for them"))
    return out
