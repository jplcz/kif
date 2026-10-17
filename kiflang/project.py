# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kif.toml`: the options a project would otherwise repeat on every command line (docs/kif-project.md).

The nearest `kif.toml` above the current directory is read by `kif`, `kif-check`, `kifc` and `kif-lint`; `--project FILE` picks
another and `--no-project` ignores it. A value in the file is only a default: an option on the command line wins, and the
list options (`cfg`, `include`, `sanitize`, `baseline`) add to the file's list. Relative paths are relative to the file.

TOML is read with `tomllib` (Python 3.11+); older Pythons use a small reader of the subset this file needs (tables, strings,
integers, booleans, arrays of those, comments).
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Dict, List, Optional, Tuple

FILE_NAME = "kif.toml"

# key -> (kind, argparse dest the value becomes the default of); kinds: str, path, bool, int, list (strings), paths (list of paths)
SHARED: Dict[str, Tuple[str, str]] = {
    "target": ("str", "target"), "profile": ("str", "profile"), "sanitize": ("list", "sanitize"), "cfg": ("list", "cfg"),
    "include": ("paths", "include"), "sysroot": ("path", "sysroot"), "format": ("str", "out_format"), "werror": ("bool", "Werror"),
}
SECTIONS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "build": {
        "c": ("path", "c"), "freebsd": ("path", "freebsd"), "emit-source": ("bool", "emit_source"),
        "manifest": ("path", "manifest"), "depfile": ("path", "depfile"), "sources": ("paths", "sources"),
    },
    "lint": {"config": ("path", "config"), "baseline": ("paths", "baseline")},
    "fmt": {"indent": ("int", "indent")},
    "signing": {"trust": ("paths", "trust"), "revoked": ("paths", "revoked"), "allow-revoked": ("list", "allow_revoked"),
                "mode": ("str", "signing"), "prover": ("str", "prover")},
    "watch": {"interval": ("float", "interval")},
}


class ProjectError(Exception):
    """An unreadable `kif.toml`, an unknown key or a value of the wrong kind (the message names the file and key)."""


def find(start: str) -> Optional[str]:
    """The nearest `kif.toml` in `start` or a directory above it."""
    d = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
    while True:
        cand = os.path.join(d, FILE_NAME)
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# -- reading

def _scalar(text: str, where: str) -> Any:
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return re.sub(r'\\(["\\])', r"\1", text[1:-1])
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.\d+", text):
        return float(text)
    raise ProjectError(f"{where}: cannot read the value {text!r}")


def _strip_comment(line: str) -> str:
    out, quote = [], ""
    for ch in line:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _split_array(body: str, where: str) -> List[Any]:
    items, cur, quote = [], "", ""
    for ch in body:
        if quote:
            cur += ch
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            cur += ch
            quote = ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [_scalar(i, where) for i in items if i.strip()]


def _mini_toml(text: str, path: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    cur = root
    pending = ""
    pending_key = ""
    for n, raw in enumerate(text.split("\n"), 1):
        where = f"{path}:{n}"
        line = _strip_comment(raw).strip()
        if pending_key:  # an array continued over several lines
            pending += " " + line
            if line.endswith("]"):
                cur[pending_key] = _split_array(pending.strip()[1:-1], where)
                pending_key = ""
            continue
        if not line:
            continue
        m = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            cur = root
            for part in m.group(1).split("."):
                cur = cur.setdefault(part, {})
            continue
        m = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.*)", line)
        if not m:
            raise ProjectError(f"{where}: cannot read {line!r}")
        key, value = m.groups()
        if value.startswith("["):
            if value.endswith("]"):
                cur[key] = _split_array(value[1:-1], where)
            else:
                pending_key, pending = key, value
        else:
            cur[key] = _scalar(value, where)
    if pending_key:
        raise ProjectError(f"{path}: the array {pending_key!r} is not closed")
    return root


def parse(text: str, path: str) -> Dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return _mini_toml(text, path)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"{path}: {e}") from None


# -- checking and turning into argparse defaults

def _convert(kind: str, value: Any, base: str, where: str) -> Any:
    def need(ok: bool, what: str) -> None:
        if not ok:
            raise ProjectError(f"{where}: expected {what}, got {value!r}")

    def rel(p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    if kind == "str":
        need(isinstance(value, str), "a string")
        return value
    if kind == "path":
        need(isinstance(value, str), "a path string")
        return rel(value)
    if kind == "bool":
        need(isinstance(value, bool), "true or false")
        return value
    if kind == "int":
        need(isinstance(value, int) and not isinstance(value, bool), "an integer")
        return value
    if kind == "float":
        need(isinstance(value, (int, float)) and not isinstance(value, bool), "a number")
        return float(value)
    need(isinstance(value, list) and all(isinstance(v, str) for v in value), "an array of strings")
    return [rel(v) for v in value] if kind == "paths" else list(value)


class Project:
    def __init__(self, path: str, data: Dict[str, Any]) -> None:
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        self.shared: Dict[str, Any] = {}
        self.sections: Dict[str, Dict[str, Any]] = {s: {} for s in SECTIONS}
        for key, value in data.items():
            if key in SECTIONS:
                if not isinstance(value, dict):
                    raise ProjectError(f"{path}: [{key}] must be a table")
                for k, v in value.items():
                    if k not in SECTIONS[key]:
                        raise ProjectError(f"{path}: unknown key {k!r} in [{key}] (one of {', '.join(sorted(SECTIONS[key]))})")
                    kind, dest = SECTIONS[key][k]
                    self.sections[key][dest] = _convert(kind, v, self.dir, f"{path}: [{key}] {k}")
            elif key in SHARED:
                kind, dest = SHARED[key]
                self.shared[dest] = _convert(kind, value, self.dir, f"{path}: {key}")
            else:
                known = sorted(set(SHARED) | set(SECTIONS))
                raise ProjectError(f"{path}: unknown key {key!r} (one of {', '.join(known)})")
        self._check_signing()

    def _check_signing(self) -> None:
        for dest, allowed in (("signing", ("off", "warn", "require")), ("prover", ("any", "same"))):
            v = self.sections["signing"].get(dest)
            if v is not None and v not in allowed:
                raise ProjectError(f"{self.path}: [signing] {'mode' if dest == 'signing' else dest} must be one of {', '.join(allowed)}, got {v!r}")

    def defaults(self, *sections: str) -> Dict[str, Any]:
        """The argparse defaults for a tool: the shared keys, then the tool's own sections."""
        out = dict(self.shared)
        for s in sections:
            out.update(self.sections.get(s, {}))
        return out

    def sources(self) -> List[str]:
        """The `.kif` files `[build] sources` names (globs allowed), sorted, relative paths made absolute."""
        out: List[str] = []
        for pattern in self.sections["build"].get("sources", []):
            out += sorted(glob.glob(pattern, recursive=True))
        return out


def load(path: str) -> Project:
    try:
        with open(path, encoding="utf-8") as f:
            return Project(path, parse(f.read(), path))
    except OSError as e:
        raise ProjectError(f"{path}: {e}") from None


def preload(argv: List[str], cwd: Optional[str] = None) -> Tuple[Optional[Project], List[str]]:
    """The project for this command line, and `argv` without `--project FILE` / `--no-project`."""
    rest, explicit, off, i = [], None, False, 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-project":
            off = True
        elif a == "--project" and i + 1 < len(argv):
            explicit = argv[i + 1]
            i += 1
        elif a.startswith("--project="):
            explicit = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    if off:
        return None, rest
    path = explicit or find(cwd or os.getcwd())
    return (load(path) if path else None), rest


def add_options(parser) -> None:
    """Document `--project` / `--no-project` in a parser's help (`preload` has already taken them out of the command line)."""
    parser.add_argument("--project", metavar="FILE", help=f"project file (default: the nearest {FILE_NAME})")
    parser.add_argument("--no-project", action="store_true", help=f"ignore {FILE_NAME}")
