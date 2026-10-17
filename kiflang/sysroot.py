# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`--sysroot`: the one place `import` looks (docs/kif-proc-language.md section 15).

Unlike a C compiler's sysroot, this is exact. With a sysroot, `import "a/b.kif";` means the file `<sysroot>/a/b.kif` or the
precompiled module `<sysroot>/a/b.kifm` and nothing else: no search path, no environment variable, no current directory, no
fallback to a copy shipped with the compiler (the global prelude and `microfmt.kif` are read from the sysroot too). The
project's own files are reached with explicit relative imports (`import "./x.kif";`, `import "../y.kif";`), which are
resolved against the importing file and never against the sysroot.

A path that is absolute, contains `..` (outside a leading `./`/`../` project import), `\\`, a NUL byte, or whose real path
leaves the sysroot (a symlink) is rejected. A name that exists both as `.kif` and `.kifm` is ambiguous and rejected.

Tools (checker, LSP, formatter, linter, documentation generators) get the same behaviour by taking a `Sysroot` and using its
`resolver()`; `modules()` lists what is importable and `read()` gives any file's text.
"""

from __future__ import annotations

import os
import posixpath
from typing import Callable, Dict, List, Optional, Tuple

from . import modfile

PRELUDE_FILE = "reloco.kif"


class SysrootError(Exception):
    """The sysroot does not exist, lacks the prelude, or an import path is not allowed; `code` is the diagnostic code."""

    def __init__(self, message: str, code: str = "KIF013") -> None:
        super().__init__(message)
        self.code = code


class Sysroot:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise SysrootError(f"sysroot {root!r} is not a directory")

    @staticmethod
    def shipped() -> "Sysroot":
        """The default sysroot: the prelude directory that ships with the compiler."""
        from .symbols import PRELUDE_DIR

        return Sysroot(PRELUDE_DIR)

    # -- lookup

    def _inside(self, rel: str) -> str:
        if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or rel != posixpath.normpath(rel) \
                or ".." in rel.split("/"):
            raise SysrootError(f"import path {rel!r} is not a plain relative path inside the sysroot")
        full = os.path.realpath(os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise SysrootError(f"import path {rel!r} leaves the sysroot")
        return full

    def read(self, rel: str) -> Optional[Tuple[str, str, Optional[dict]]]:
        """(key, text, header) of `rel` (`.kif` source, or the `.kifm` next to its stem); None if absent. `header` is set for `.kifm`."""
        stem = rel[:-4] if rel.endswith(".kif") else rel
        src, pre = self._inside(stem + ".kif"), self._inside(stem + ".kifm")
        has_src, has_pre = os.path.isfile(src), os.path.isfile(pre)
        if has_src and has_pre:
            raise SysrootError(f"{rel!r} exists as both source and precompiled module in the sysroot; remove one")
        if has_pre:
            with open(pre, "rb") as f:
                header = modfile.decode(f.read())  # raises modfile.ModuleFileError
            return pre, header["text"], header
        if has_src:
            with open(src, encoding="utf-8") as f:
                return src, f.read(), None
        return None

    def modules(self) -> List[str]:
        """Every importable module as a sysroot-relative `.kif` path, sorted (a `.kifm` is listed under its `.kif` name)."""
        out = set()
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if name.endswith((".kif", ".kifm")):
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/")
                    out.add(rel[:-1] if rel.endswith(".kifm") else rel)
        return sorted(out)

    def prelude(self) -> Tuple[str, str]:
        found = self.read(PRELUDE_FILE)
        if found is None:
            raise SysrootError(f"sysroot {self.root!r} has no {PRELUDE_FILE}")
        return found[0], found[1]

    # -- resolver

    def resolver(self) -> "SysrootResolver":
        return SysrootResolver(self)


class SysrootResolver:
    """A `symbols.FileResolver`: `./`/`../` imports against the importing file, everything else against the sysroot only."""

    exclusive = True  # `symbols` never falls back to the compiler's shipped files when a resolver says so

    def __init__(self, sysroot: Sysroot) -> None:
        self.sysroot = sysroot
        self.headers: Dict[str, dict] = {}  # key -> `.kifm` header, for dependency checks

    def __call__(self, importer: str, path: str):
        if path.startswith(("./", "../")):
            return self.project(importer, path)
        found = self.sysroot.read(path)
        if found is None:
            return None
        key, text, header = found
        if header is not None:
            self.headers[key] = header
        return key, text

    def project(self, importer: str, path: str):
        if "\\" in path or "\0" in path:
            raise SysrootError(f"import path {path!r} is not allowed")
        base = os.path.dirname(os.path.abspath(importer))
        full = os.path.normpath(os.path.join(base, path))
        stem = full[:-4] if full.endswith(".kif") else full
        has_src, has_pre = os.path.isfile(stem + ".kif"), os.path.isfile(stem + ".kifm")
        if has_src and has_pre:
            raise SysrootError(f"{path!r} exists as both source and precompiled module; remove one")
        if has_pre:
            with open(stem + ".kifm", "rb") as f:
                header = modfile.decode(f.read())
            self.headers[stem + ".kifm"] = header
            return stem + ".kifm", header["text"]
        if has_src:
            with open(stem + ".kif", encoding="utf-8") as f:
                return stem + ".kif", f.read()
        return None
