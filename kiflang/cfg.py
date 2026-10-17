# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Target variants: `#[cfg(..)]` and `cfg(..) { }` predicates, the configuration they are tested against (docs/kif-targets.md).

A predicate is built from atoms and combinators:

    c                    a bare flag (`c`, `cxx`, `kobj`, anything a target or `--cfg` sets)
    arch = "x86_64"      a key with a value (`target`, `feature`, `arch`, `os`, ...: any key)
    all(p, q)  any(p, q)  not(p)

`Env` is the set of keys and flags in force: it comes from a target definition and from command-line `--cfg` options, which
override it. The checker does not need an `Env`: it checks every variant, and uses `exclusive`/`covers` (which look at all
possible environments) to decide whether two variants can coexist and whether a use is covered. A backend or the interpreter
uses `Env.test` to pick the variant that is active.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import ast

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Pred:
    kind: str  # "flag" | "eq" | "all" | "any" | "not"
    key: str = ""
    value: str = ""
    args: Tuple["Pred", ...] = ()

    def __str__(self) -> str:
        if self.kind == "flag":
            return self.key
        if self.kind == "eq":
            return f'{self.key} = "{self.value}"'
        return f"{self.kind}({', '.join(str(a) for a in self.args)})"


class CfgError(Exception):
    """A malformed predicate or option; `range` is the source range when there is one."""

    def __init__(self, message: str, range=None):
        super().__init__(message)
        self.range = range


# -------------------------------------------------------------------------------------------------- parsing


def parse_arg(a: ast.AttrArg) -> Pred:
    """The predicate in one attribute argument (`c`, `arch = "x"`, `not(..)`, ...); raises CfgError."""
    if a.name is None:
        raise CfgError("expected a name, `key = \"value\"`, all(..), any(..) or not(..)", a.range)
    if a.name in ("all", "any", "not"):
        if a.value is not None or (not a.args and a.name != "all"):
            raise CfgError(f"{a.name}(..) needs arguments", a.range)
        if a.name == "not" and len(a.args) != 1:
            raise CfgError("not(..) takes exactly one predicate", a.range)
        return Pred(a.name, args=tuple(parse_arg(x) for x in a.args))
    if a.args:
        raise CfgError(f"unknown combinator {a.name}(..); use all, any or not", a.range)
    if a.value is not None:
        if a.value.kind != "str":
            raise CfgError(f"{a.name} = ..: the value must be a string", a.value.range)
        return Pred("eq", a.name, str(a.value.value))
    return Pred("flag", a.name)


def parse_attribute(attr: ast.Attribute) -> Pred:
    """`#[cfg(p)]` / the head of `cfg(p) { .. }`: exactly one predicate."""
    if not attr.args or len(attr.args) != 1:
        raise CfgError("cfg takes exactly one predicate: cfg(c), cfg(arch = \"x86_64\"), cfg(not(kobj))", attr.range)
    return parse_arg(attr.args[0])


def of_attrs(attrs: Iterable[ast.Attribute]) -> Optional[Pred]:
    """The predicate that guards an item (all its `#[cfg(..)]` attributes together), or None when it is unconditional.
    A malformed one is skipped here; the checker reports it (KIFP200)."""
    preds: List[Pred] = []
    for a in attrs:
        if a.name.text == "cfg":
            try:
                preds.append(parse_attribute(a))
            except CfgError:
                pass
    if not preds:
        return None
    return preds[0] if len(preds) == 1 else Pred("all", args=tuple(preds))


def parse_option(text: str) -> Tuple[str, Optional[str], bool]:
    """A command-line `--cfg`: `flag`, `!flag`, `key=value` -> (key, value, enabled). Raises CfgError."""
    neg = text.startswith("!")
    body = text[1:] if neg else text
    key, eq, value = body.partition("=")
    key = key.strip()
    if not NAME.fullmatch(key):
        raise CfgError(f"--cfg {text!r}: expected flag, !flag or key=value (a name like arch, not {key!r})")
    if neg and eq:
        raise CfgError(f"--cfg {text!r}: `!` goes with a flag, not with key=value")
    if eq:
        value = value.strip().strip('"')
        return key, value, True
    return key, None, not neg


# -------------------------------------------------------------------------------------------------- environment


@dataclass
class Env:
    """The keys and flags in force. `values[key]` is a set (`feature` can be several), `flags` are bare names."""

    values: Dict[str, Set[str]] = field(default_factory=dict)
    flags: Set[str] = field(default_factory=set)

    def test(self, p: Pred) -> bool:
        if p.kind == "flag":
            return p.key in self.flags
        if p.kind == "eq":
            return p.value in self.values.get(p.key, ())
        if p.kind == "all":
            return all(self.test(a) for a in p.args)
        if p.kind == "any":
            return any(self.test(a) for a in p.args)
        return not self.test(p.args[0])

    def override(self, options: Iterable[str]) -> "Env":
        """Apply `--cfg` options in order, returning a new Env: `key=value` replaces every earlier value of key (`feature=x`
        adds one instead), a flag sets, `!flag` clears the flag, the key's values and the feature of that name."""
        out = Env({k: set(v) for k, v in self.values.items()}, set(self.flags))
        for text in options:
            key, value, on = parse_option(text)
            if value is not None:
                if key in _MULTI:
                    out.values.setdefault(key, set()).add(value)
                else:
                    out.values[key] = {value}
            elif on:
                out.flags.add(key)
            else:
                out.flags.discard(key)
                out.values.pop(key, None)
                for multi in _MULTI:  # `!smp` also turns the feature off
                    out.values.get(multi, set()).discard(key)
        return out

    def lines(self) -> List[str]:
        """The configuration as `key="value"` / `flag` lines, sorted: what `kif target cfg` prints."""
        out = sorted(self.flags)
        out += [f'{k} = "{v}"' for k in sorted(self.values) for v in sorted(self.values[k])]
        return out


def env_of_target(t) -> Env:
    """The configuration a `Target` (target.py) provides: `target`, the language (`c`/`cxx`), `kobj`, one `feature` per
    true flag (a flag `smp` is both `feature = "smp"` and the bare `smp`), `tls`/`per_cpu` providers, and its `cfg` section."""
    env = Env()
    env.values["target"] = {t.name}
    env.flags.add(t.language)
    if t.supports("kobj"):
        env.flags.add("kobj")
    for k, v in (t.data.get("features") or {}).items():
        if v is True:
            env.values.setdefault("feature", set()).add(k)
            env.flags.add(k)
        elif v not in (False, None):
            env.values[k] = {str(v)}
    for k, v in (t.data.get("cfg") or {}).items():
        if v is True:
            env.flags.add(k)
        elif isinstance(v, list):
            env.values[k] = {str(x) for x in v}
        elif v is not False:
            env.values[k] = {str(v)}
    return env


def apply_profile(env: Env, prof: dict, sanitizers: List[str]) -> None:
    """Add a build profile to `env`: `profile = "debug"` and the bare `debug` (not for `sanitize`); `debug_assertions`, `overflow_checks`,
    `debug_info` flags; `opt_level = "2"`; `sanitizer = "address"` per sanitizer plus the bare `sanitize`; then the profile's `cfg`."""
    env.values["profile"] = {prof["name"]}
    if prof["name"] != "sanitize":  # the bare `sanitize` means "a sanitizer is on", whatever the profile
        env.flags.add(prof["name"])
    for key, flag in (("assertions", "debug_assertions"), ("overflow_checks", "overflow_checks"), ("debug_info", "debug_info")):
        if prof.get(key):
            env.flags.add(flag)
    if "opt" in prof:
        env.values["opt_level"] = {prof["opt"]}
    if sanitizers:
        env.flags.add("sanitize")
        env.values["sanitizer"] = set(sanitizers)
    for k, v in (prof.get("cfg") or {}).items():
        if v is True:
            env.flags.add(k)
        elif isinstance(v, list):
            env.values[k] = {str(x) for x in v}
        elif v is not False:
            env.values[k] = {str(v)}


# -------------------------------------------------------------------------------------------------- reasoning over all environments

_OTHER = "<other>"


def _atoms(p: Pred, flags: Set[str], eqs: Dict[str, Set[str]]) -> None:
    if p.kind == "flag":
        flags.add(p.key)
    elif p.kind == "eq":
        eqs.setdefault(p.key, set()).add(p.value)
    for a in p.args:
        _atoms(a, flags, eqs)


# `c` and `cxx` are the two backends: a build is exactly one of them. `target` has one name; every other key is
# treated as single-valued too, except `feature`, which may hold several values.
_MULTI = {"feature"}


def _worlds(preds: Iterable[Pred]):
    """Every environment that matters for these predicates (as Env objects)."""
    flags: Set[str] = set()
    eqs: Dict[str, Set[str]] = {}
    for p in preds:
        _atoms(p, flags, eqs)
    lang = [f for f in ("c", "cxx") if f in flags]
    other_flags = sorted(flags - {"c", "cxx"})
    keys = sorted(eqs)
    langs = [{f} for f in lang] + ([set()] if len(lang) < 2 else [])
    if not lang:
        langs = [set()]
    per_key: List[List[Set[str]]] = []
    for k in keys:
        vals = sorted(eqs[k])
        if k in _MULTI:
            per_key.append([set(c) for r in range(len(vals) + 1) for c in itertools.combinations(vals, r)])
        else:
            per_key.append([{v} for v in vals] + [{_OTHER}])
    for lg in langs:
        for fl in itertools.product((False, True), repeat=len(other_flags)):
            for vs in itertools.product(*per_key) if per_key else [()]:
                env = Env({k: set(v) for k, v in zip(keys, vs)}, set(lg) | {f for f, on in zip(other_flags, fl) if on})
                yield env


def exclusive(a: Pred, b: Pred) -> bool:
    """True when no environment satisfies both: two variants of one name may coexist."""
    return not any(env.test(a) and env.test(b) for env in _worlds([a, b]))


def covers(context: Optional[Pred], variants: List[Optional[Pred]]) -> bool:
    """True when in every environment where `context` holds at least one variant holds: a use under `context` always finds
    the item. An unconditional variant (None) covers everything."""
    if any(v is None for v in variants):
        return True
    preds = [p for p in [context, *variants] if p is not None]
    return all(
        any(env.test(v) for v in variants if v is not None)
        for env in _worlds(preds) if context is None or env.test(context)
    )


def conj(a: Optional[Pred], b: Optional[Pred]) -> Optional[Pred]:
    if a is None:
        return b
    if b is None:
        return a
    return Pred("all", args=(a, b))
