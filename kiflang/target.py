# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Target definitions: a JSON file describes what a C/C++ backend generates for one platform (docs/kif-targets.md).

    t = load("freebsd-kernel")            # a built-in (kiflang/targets/*.json) or a path to a .json file
    t.get("kobj.flavor")                  # dotted lookup into the resolved definition
    t.supports("kobj")                    # True only when the target has a kobj emitter
    t.kobj_include()                      # '<sys/kobj.h>': the header name and quoting come from the target
    t.includes("header")                  # every #include the target adds to generated headers, flags applied
    t.flag("smp")                         # a custom feature flag of the target
    t.macro("panic", file="f", line="l", msg="m")

Nothing here generates code; the backends read a `Target`. Unknown keys, wrong types and unknown enum values are errors,
so a typo in a custom target fails at load time instead of producing wrong C.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional

from .symbols import SCALARS

FORMAT = "kif-target"
FORMAT_VERSION = 1
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets")

ATOMIC_ATTRIBUTES = (
    "might_sleep", "atomic_only", "atomic_entry", "atomic_scope", "atomic_lock", "atomic_callback", "atomic_token",
    "atomic_exempt",
)
LANGUAGES = ("c", "cxx")
ERRNO_STYLES = ("none", "positive", "negative")
KOBJ_FLAVORS = ("freebsd",)  # the kobj emitter exists only for FreeBSD-like kernels
CELL_PROVIDERS = ("none", "hosted", "freebsd", "linux", "custom")
# reloco's one error enum -> the errno a C `result` carries by default (names both Linux and FreeBSD define); `errno.map` overrides entries.
ERROR_ERRNO = {
    "allocation_failed": "ENOMEM", "in_place_growth_failed": "ENOMEM", "unsupported_operation": "EOPNOTSUPP",
    "out_of_range": "ERANGE", "invalid_argument": "EINVAL", "already_exists": "EEXIST", "empty_pointer": "EFAULT",
    "pointer_expired": "ESTALE", "no_owner": "EINVAL", "out_of_bounds": "EINVAL", "deadlock": "EDEADLK",
    "invalid_owner": "EPERM", "still_locked": "EBUSY", "not_locked": "EINVAL", "timed_out": "ETIMEDOUT",
    "try_again": "EAGAIN", "not_initialized": "EINVAL", "container_empty": "ENOENT", "not_found": "ENOENT",
    "integer_overflow": "EOVERFLOW", "division_by_zero": "EDOM", "capacity_exceeded": "ENOSPC", "invalid_state": "EINVAL",
    "permission_denied": "EACCES", "interrupted": "EINTR", "resource_exhausted": "ENFILE", "busy": "EBUSY",
    "io_error": "EIO", "operation_canceled": "ECANCELED", "security_violation": "EPERM", "page_fault": "EFAULT",
}
PLACEHOLDERS = {"panic": ("file", "line", "msg"), "assert": ("cond", "file", "line", "msg")}


class TargetError(Exception):
    """Unreadable file, unknown built-in, a loop in `extends`, or an invalid definition (the message lists every problem)."""


ALLOC_PLACEHOLDERS = {"malloc": ("size",), "zalloc": ("size",), "realloc": ("ptr", "size"), "free": ("ptr",),
                      "aligned": ("size", "align")}


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _str_map(v: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())


def _when_ok(v: Any) -> bool:
    return isinstance(v, dict) and all(
        re.fullmatch(r"[a-z][a-z0-9_]*", k) is not None and isinstance(x, dict) and set(x) <= {"header", "source"}
        and all(_str_list(y) for y in x.values())
        for k, x in v.items()
    )


# key -> (checker, description of the accepted value). A nested dict is a section; `None` values may delete an inherited key.
# Build profiles every target has; a target's own `profiles` entries merge over these key by key.
BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "debug": {"description": "No optimization, full debug info, assertions and overflow checks.",
              "opt": "0", "debug_info": True, "assertions": True, "overflow_checks": True},
    "perf": {"description": "Optimized with debug info; assertions and overflow checks off. For profiling.",
             "opt": "2", "debug_info": True, "assertions": False, "overflow_checks": False},
    "release": {"description": "Optimized, no debug info, no assertions; overflow checks stay on.",
                "opt": "2", "debug_info": False, "assertions": False, "overflow_checks": True},
    "sanitize": {"description": "Light optimization with debug info, assertions and the address+undefined sanitizers.",
                 "opt": "1", "debug_info": True, "assertions": True, "overflow_checks": True,
                 "sanitize": ["address", "undefined"]},
}
OPT_LEVELS = ("0", "1", "2", "3", "s", "z")
_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_PROFILE_KEYS = {
    "description": _is_str,
    "opt": lambda v: v in OPT_LEVELS,
    "debug_info": lambda v: isinstance(v, bool),
    "assertions": lambda v: isinstance(v, bool),
    "overflow_checks": lambda v: isinstance(v, bool),
    "sanitize": lambda v: _str_list(v) and all(_NAME.fullmatch(x) for x in v),
    "cfg": lambda v: isinstance(v, dict) and all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and (isinstance(x, (bool, str)) or _str_list(x)) for k, x in v.items()),
    "defines": lambda v: isinstance(v, dict) and all(isinstance(x, str) for x in v.values()),
}


def _profiles_ok(v: Any) -> bool:
    return isinstance(v, dict) and all(
        _NAME.fullmatch(n) and isinstance(p, dict) and all(k in _PROFILE_KEYS and _PROFILE_KEYS[k](x) for k, x in p.items())
        for n, p in v.items())


WRAPPING = ("helpers", "builtin")  # `+ - *` on signed types: a helper (default) or the plain operator under -fwrapv
ACCESS_TIERS = ("checked", "prove")  # no "unchecked": an access without a check or a proof is `unsafe` code
ACCESS_CLASSES = ("index", "range", "optional", "div", "shift")  # accesses whose check can be proved away; see docs/kif-targets.md

def _passes_ok(v: Any) -> bool:
    from . import passes

    if not _is_str(v):
        return False
    try:
        passes.order(passes.pipeline(v))
    except passes.PassError:
        return False
    return True


_SPEC: Dict[str, Any] = {
    "format": (lambda v: v == FORMAT, f'"{FORMAT}"'),
    "version": (lambda v: v == FORMAT_VERSION, str(FORMAT_VERSION)),
    "name": (lambda v: _is_str(v) and re.fullmatch(r"[a-z][a-z0-9_-]*", v) is not None, "a lower-case name (a-z, 0-9, - and _)"),
    "description": (_is_str, "a string"),
    "extends": (_is_str, "a built-in name or a path relative to this file"),
    "language": (lambda v: v in LANGUAGES, f"one of {LANGUAGES}"),
    "standard": (_is_str, 'a language standard such as "c11", "gnu11" or "c++20"'),
    "hosted": (lambda v: isinstance(v, bool), "true or false (false: freestanding, no libc)"),
    "wrapping": (lambda v: v in WRAPPING, f"one of {WRAPPING}"),
    "access": {k: (lambda v: v in ACCESS_TIERS, f"one of {ACCESS_TIERS}") for k in ACCESS_CLASSES},
    "ir": {
        "passes": (_passes_ok, 'a pass list: "default" (every IR pass), names, `-name` to leave one out, e.g. "default,-prove" (docs/kif-ir-passes.md)'),
    },
    "visibility": {
        "enabled": (lambda v: isinstance(v, bool), "true or false: emit ELF symbol visibility attributes (default: the value of `hosted`)"),
    },
    "types": (lambda v: _str_map(v) and all(k in SCALARS for k in v), f"a map from {sorted(SCALARS)} to a C spelling"),
    "includes": {
        "header": (_str_list, 'a list such as ["<stdint.h>"]: added to every generated header'),
        "source": (_str_list, "a list: added to every generated implementation file"),
        "paths": (_str_list, "directories for -I, relative to the file that lists them"),
        "when": (_when_ok, 'a map: feature flag -> {"header": [...], "source": [...]}, added while the flag is true'),
    },
    "defines": (_str_map, "a map NAME -> replacement text, emitted as #define before the generated code"),
    "panic": {
        "macro": (_is_str, "KIF_PANIC replacement text using {file}, {line} and {msg}"),
        "header": (_is_str, "a header that declares what the macro calls"),
        "noreturn": (lambda v: v is True, "true: the macro never returns (generated code relies on it); required with `macro`"),
    },
    "assert": {
        "macro": (_is_str, "replacement text using {cond}, {file}, {line} and {msg}"),
        "header": (_is_str, "a header that declares what the macro calls"),
    },
    "errno": {
        "style": (lambda v: v in ERRNO_STYLES, f"one of {ERRNO_STYLES}: the default for `cresult` (an interface `errno(..)` wins)"),
        "header": (_is_str, "the header that defines the errno numbers"),
        "map": (lambda v: _str_map(v) and all(k in ERROR_ERRNO for k in v) and all(re.fullmatch(r"[A-Za-z_]\w*|\d+", x) for x in v.values()),
                "a map from a reloco `error` name to an errno macro or number, e.g. {\"timed_out\": \"EWOULDBLOCK\"}; unlisted errors use the defaults"),
    },
    "alloc": {  # the whole section may be null: no allocator, nothing that allocates can be synthesized
        "malloc": (_is_str, "C expression allocating {size} bytes, NULL on failure, e.g. `kmalloc({size}, GFP_KERNEL)`"),
        "zalloc": (_is_str, "like malloc but zeroed; optional (kif then uses malloc + memset)"),
        "realloc": (_is_str, "C expression resizing {ptr} to {size} bytes; optional (kif then uses malloc + copy + free)"),
        "free": (_is_str, "C statement releasing {ptr}, e.g. `kfree({ptr})`"),
        "aligned": (_is_str, "C expression allocating {size} bytes aligned to {align}; optional"),
        "header": (_is_str, "the header that declares these"),
        "declare": (_is_str, "C text emitted right after the includes of kif_c.h, e.g. `MALLOC_DECLARE(M_KIF);` for the malloc type the expressions use"),
    },
    "kobj": {  # the whole section may be null: no kobj emitter on this target
        "flavor": (lambda v: v in KOBJ_FLAVORS, f"one of {KOBJ_FLAVORS}"),
        "header": (_is_str, 'the kobj header as the target names it, e.g. "sys/kobj.h" or "bsd/kobj.h"'),
        "quote": (lambda v: v in ("angle", "quote"), '"angle" (#include <h>, default) or "quote" (#include "h")'),
        "include_dir": (_is_str, "the directory that holds the header, for -I; relative to this file (optional)"),
        "extra_headers": (_str_list, 'more headers the kobj glue needs, e.g. ["<sys/bus.h>"]'),
        "self_type": (_is_str, "the C type of an object (an interface `self(..)` wins), e.g. device_t"),
        "handles": (lambda v: isinstance(v, dict) and all(_is_str(k) and _is_str(x) for k, x in v.items()),
                    "map from a handle C type to the C expression ({handle}) giving the `struct S *` of the implementing class, "
                    'e.g. {"device_t": "device_get_softc({handle})"}; a type not listed is cast (the struct starts with the kobj header)'),
        "lifecycle": (_str_list, 'handle types whose attach/detach are wrapped so the class runs `S_init`/`S_drop` itself, e.g. ["device_t"]'),
    },
    "annotations": (
        lambda v: isinstance(v, dict) and all(k in ATOMIC_ATTRIBUTES and (x is None or _is_str(x)) for k, x in v.items()),
        f"a map from {ATOMIC_ATTRIBUTES} to a C macro, or null for a comment only",
    ),
    "cells": {
        "tls": (lambda v: v in CELL_PROVIDERS, f"one of {CELL_PROVIDERS}"),
        "per_cpu": (lambda v: v in CELL_PROVIDERS, f"one of {CELL_PROVIDERS}"),
        "header": (_is_str, "the provider header (needed for `custom`)"),
    },
    # Known flags are listed; any other lower-case name is a custom flag of the target (bool, integer or string).
    "features": {
        "int64_div": (lambda v: isinstance(v, bool), "true or false (false: 64-bit division is refused, KIFP051)"),
        "floats": (lambda v: isinstance(v, bool), "true or false"),
        "line_directives": (lambda v: isinstance(v, bool), "true or false: #line directives pointing at the .kif"),
        "fold_notes": (lambda v: isinstance(v, bool), "true or false: a `/* kif: kept, why */` comment where a temporary was not folded (default true)"),
        "*": (lambda v: isinstance(v, (bool, int, str)), "a bool, integer or string (a custom flag)"),
    },
    "cfg": (
        lambda v: isinstance(v, dict) and all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and (isinstance(x, (bool, str)) or _str_list(x)) for k, x in v.items()),
        "a map: name -> true (a flag), a string or a list of strings (values); what `cfg(name = \"v\")` sees, e.g. arch, os",
    ),
    "profile": (_is_str, "the profile used when none is chosen on the command line (default: debug)"),
    "profiles": (_profiles_ok, "name -> {description, opt (0 1 2 3 s z), debug_info, assertions, overflow_checks, sanitize (list), "
                               "cfg (map), defines (map)}; merged over the built-ins debug, perf, release, sanitize"),
    "sanitize": (lambda v: _str_list(v) and all(_NAME.fullmatch(x) for x in v),
                 'sanitizers on by default, e.g. ["address", "undefined"]; overrides the profile\'s own list'),
    "naming": {
        "prefix": (lambda v: v in ("stem", "none"), 'one of ("stem", "none"): free functions are <stem>_<name> or <name>'),
        "macro_case": (lambda v: v in ("upper", "lower"), 'one of ("upper", "lower")'),
    },
}
_NULLABLE_SECTIONS = ("kobj", "alloc")


def validate(data: Any, where: str = "") -> List[str]:
    """Every problem in an already-merged or raw definition, as `path: message` strings (empty when valid)."""
    out: List[str] = []

    def walk(d: Any, spec: Dict[str, Any], path: str) -> None:
        if not isinstance(d, dict):
            out.append(f"{path or '<root>'}: expected an object")
            return
        for key, val in d.items():
            here = f"{path}.{key}" if path else key
            if key not in spec and "*" in spec and re.fullmatch(r"[a-z][a-z0-9_]*", key):
                if not spec["*"][0](val):
                    out.append(f"{here}: expected {spec['*'][1]}")
                continue
            if key not in spec:
                out.append(f"{here}: unknown key (allowed: {', '.join(sorted(spec))})")
                continue
            rule = spec[key]
            if isinstance(rule, dict):
                if val is None and here in _NULLABLE_SECTIONS:
                    continue
                walk(val, rule, here)
            elif not rule[0](val):
                out.append(f"{here}: expected {rule[1]}")

    walk(data, _SPEC, "")
    for key in ("format", "version", "name", "language"):
        if isinstance(data, dict) and key not in data:
            out.append(f"{key}: required")
    if isinstance(data, dict):
        kobj = data.get("kobj")
        if isinstance(kobj, dict) and "flavor" not in kobj:
            out.append("kobj.flavor: required (or set kobj to null)")
        if isinstance(kobj, dict) and "flavor" in kobj and "header" not in kobj:
            out.append("kobj.header: required (the kobj header name is never assumed)")
        for flag in ((data.get("includes") or {}).get("when") or {}):
            if flag not in (data.get("features") or {}):
                out.append(f"includes.when.{flag}: no such flag in features")
        pan = data.get("panic")
        if isinstance(pan, dict) and "macro" in pan and pan.get("noreturn") is not True:
            out.append("panic.noreturn: required (true) with panic.macro: generated code treats KIF_PANIC as a call that never returns")
        cells = data.get("cells") or {}
        if "custom" in (cells.get("tls"), cells.get("per_cpu")) and not cells.get("header"):
            out.append("cells.header: required when a provider is custom")
        if data.get("language") == "cxx":
            for sect in ("alloc", "panic", "assert", "errno", "cells"):
                if data.get(sect):
                    out.append(f"{sect}: C++ targets run on reloco, which provides this (its porting layer); the section is for C targets")
        alloc = data.get("alloc")
        if isinstance(alloc, dict):
            if ("malloc" in alloc or "realloc" in alloc) and "free" not in alloc:
                out.append("alloc.free: required when malloc or realloc is given")
            for k, names in ALLOC_PLACEHOLDERS.items():
                if isinstance(alloc.get(k), str):
                    for ph in re.findall(r"\{(\w*)\}", alloc[k]):
                        if ph not in names:
                            out.append(f"alloc.{k}: unknown placeholder {{{ph}}} (allowed: {', '.join(names)})")
        for sect, names in PLACEHOLDERS.items():
            macro = (data.get(sect) or {}).get("macro")
            if isinstance(macro, str):
                for ph in re.findall(r"\{(\w*)\}", macro):
                    if ph not in names:
                        out.append(f"{sect}.macro: unknown placeholder {{{ph}}} (allowed: {', '.join(names)})")
    return [f"{where}: {m}" if where else m for m in out]


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """Objects merge key by key, everything else (lists included) is replaced, null removes an inherited key."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is None and k not in _NULLABLE_SECTIONS and k != "annotations":
            out.pop(k, None)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Target:
    """A resolved target definition. `data` is the merged JSON; `chain` lists the files it came from, base first."""

    def __init__(self, data: Dict[str, Any], chain: List[str], dirs: Optional[List[str]] = None):
        self.data = data
        self.chain = chain
        self.dirs = dirs or []

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def language(self) -> str:
        return self.data["language"]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    def supports(self, feature: str) -> bool:
        """`kobj` (an emitter exists), `int64_div`, `floats`, `tls`, `per_cpu`."""
        if feature == "alloc":
            return bool(self.get("alloc.malloc"))
        if feature == "kobj":
            return bool(self.data.get("kobj"))
        if feature in ("tls", "per_cpu"):
            return self.get(f"cells.{feature}", "none") != "none"
        return bool(self.get(f"features.{feature}", False))

    def profile_names(self) -> List[str]:
        return sorted(set(BUILTIN_PROFILES) | set(self.data.get("profiles") or {}))

    def default_profile(self) -> str:
        return self.data.get("profile") or "debug"

    def profile(self, name: Optional[str] = None) -> Dict[str, Any]:
        """The build profile `name` (default: the target's `profile`, else debug): built-in values merged with this target's."""
        name = name or self.default_profile()
        if name not in self.profile_names():
            raise TargetError(f"{self.name}: no profile {name!r} (have: {', '.join(self.profile_names())})")
        out = _merge(BUILTIN_PROFILES.get(name, {}), (self.data.get("profiles") or {}).get(name, {}))
        out["name"] = name
        return out

    def sanitizers(self, profile: Optional[str] = None, override: Optional[List[str]] = None) -> List[str]:
        """Sanitizers in force: the command line (`["none"]` clears), else the target's `sanitize`, else the profile's."""
        if override is not None:
            return [] if override == ["none"] else sorted(set(override))
        base = self.data.get("sanitize")
        return sorted(set(base if base is not None else self.profile(profile).get("sanitize", [])))

    def cfg_env(self, overrides: "Optional[List[str]]" = None, profile: Optional[str] = None,
                sanitize: "Optional[List[str]]" = None):
        """The `cfg` environment (kiflang/cfg.py): this target's keys and flags, the build profile and sanitizers, then
        command-line `--cfg` overrides (which win over everything)."""
        from . import cfg

        env = cfg.env_of_target(self)
        cfg.apply_profile(env, self.profile(profile), self.sanitizers(profile, sanitize))
        return env.override(overrides or [])

    def flag(self, name: str, default: Any = False) -> Any:
        """A feature flag, custom ones included (`features.<name>`)."""
        return self.get(f"features.{name}", default)

    def kobj_include(self) -> Optional[str]:
        """The `#include` argument for the kobj header, e.g. `<sys/kobj.h>`; None when the target has no kobj emitter."""
        k = self.data.get("kobj")
        if not k:
            return None
        return f'"{k["header"]}"' if k.get("quote") == "quote" else f'<{k["header"]}>'

    def includes(self, kind: str, kobj: bool = True) -> List[str]:
        """Includes the target adds to `header` or `source` files: the always list, then each enabled `when` entry, then
        (`kobj`) the kobj header. Only interface files include it: the procedural C output passes `kobj=False`."""
        inc = self.data.get("includes") or {}
        out = list(inc.get(kind, []))
        for flag, entry in (inc.get("when") or {}).items():
            if self.flag(flag):
                out += entry.get(kind, [])
        k = self.data.get("kobj")
        if k and kind == "header" and kobj:
            out += [self.kobj_include()] + list(k.get("extra_headers", []))
        return out

    def include_dirs(self) -> List[str]:
        """Directories for -I (`includes.paths` and `kobj.include_dir`), absolute, resolved against their own file."""
        return list(self.dirs)

    def c_type(self, scalar: str) -> str:
        """The C spelling of a kif scalar type (`u32` -> `uint32_t` unless the target overrides it)."""
        return self.get(f"types.{scalar}", _DEFAULT_TYPES[scalar])

    def macro(self, which: str, **args: str) -> Optional[str]:
        """`panic` or `assert` replacement text with its placeholders filled; None when the target does not define one."""
        text = self.get(f"{which}.macro")
        return None if text is None else re.sub(r"\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), text)

    def alloc(self, which: str, **args: str) -> Optional[str]:
        """The allocator text (`malloc`, `zalloc`, `realloc`, `free`, `aligned`) with its placeholders filled; None when the
        target does not provide it. A C backend synthesizes allocating code (vectors, boxes, clones) from these."""
        text = self.get(f"alloc.{which}")
        return None if text is None else re.sub(r"\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), text)

    def annotation(self, attr: str) -> Optional[str]:
        return (self.data.get("annotations") or {}).get(attr)

    def dumps(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


_DEFAULT_TYPES = {
    "bool": "bool", "char": "char", "void": "void", "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t", "usize": "uintptr_t", "isize": "intptr_t",
}


def builtin_names() -> List[str]:
    if not os.path.isdir(BUILTIN_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(BUILTIN_DIR) if f.endswith(".json"))


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise TargetError(f"{path}: {e.strerror or e}") from e
    except json.JSONDecodeError as e:
        raise TargetError(f"{path}:{e.lineno}:{e.colno}: invalid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise TargetError(f"{path}: expected a JSON object")
    return data


def _resolve_ref(ref: str, relative_to: Optional[str]) -> str:
    """A built-in name, or a path (relative to the file that mentions it)."""
    if ref in builtin_names() and not ref.endswith(".json"):
        return os.path.join(BUILTIN_DIR, ref + ".json")
    path = ref if os.path.isabs(ref) or relative_to is None else os.path.join(os.path.dirname(relative_to), ref)
    if not os.path.isfile(path):
        raise TargetError(f"unknown target {ref!r}: not a built-in ({', '.join(builtin_names())}) and not a file")
    return path


def _load_chain(path: str, seen: List[str]) -> "tuple[Dict[str, Any], List[str]]":
    real = os.path.realpath(path)
    if real in seen:
        raise TargetError("`extends` loop: " + " -> ".join(seen + [real]))
    raw = _read(path)
    base_data: Dict[str, Any] = {}
    chain: List[str] = []
    if "extends" in raw:
        ref = raw["extends"]
        if not isinstance(ref, str):
            raise TargetError(f"{path}: extends: expected a built-in name or a path")
        base_data, chain = _load_chain(_resolve_ref(ref, path), seen + [real])
    merged = _merge(base_data, {k: v for k, v in raw.items() if k != "extends"})
    return merged, chain + [path]


def _dirs(chain: List[str]) -> List[str]:
    """Include directories, each resolved against the file that wrote it (the last file to set a key wins)."""
    paths: List[str] = []
    kobj_dir: Optional[str] = None
    for p in chain:
        raw = _read(p)
        base = os.path.dirname(os.path.realpath(p))
        if "paths" in (raw.get("includes") or {}):
            paths = [os.path.normpath(os.path.join(base, d)) for d in raw["includes"]["paths"]]
        k = raw.get("kobj")
        if isinstance(k, dict) and "include_dir" in k:
            kobj_dir = os.path.normpath(os.path.join(base, k["include_dir"]))
        elif k is None and "kobj" in raw:
            kobj_dir = None
    return paths + ([kobj_dir] if kobj_dir else [])


def load(ref: str) -> Target:
    """Load a built-in target by name or a `.json` file by path; `extends` chains are merged, base first."""
    path = _resolve_ref(ref, None)
    data, chain = _load_chain(path, [])
    problems = validate(data)
    if problems:
        raise TargetError(f"{path}: invalid target:\n  " + "\n  ".join(problems))
    return Target(data, chain, _dirs(chain))


def check_file(path: str) -> List[str]:
    """Problems of one file: its own mistakes (typos, wrong types) first, then whatever the merged result still lacks."""
    try:
        raw = _read(path)
    except TargetError as e:
        return [str(e)]
    own = [m for m in validate({k: v for k, v in raw.items() if k != "extends"}) if "required" not in m or "extends" not in raw]
    if own:
        return own
    try:
        load(path)
    except TargetError as e:
        return [m.strip() for m in str(e).splitlines()[1:]] or [str(e)]
    return []
