# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kif new NAME [--kind driver|app|staticlib|solib] [--target T] [--toml]`: a CMake project that builds with `cmake -S . -B build`.

    NAME/CMakeLists.txt   find_package(jplcz_kif), add_kif_library(NAME SOURCES ... KIF_TARGET ...): the primary way to build
    NAME/kif.toml         only with --toml: settings shared with the kif command line tools (docs/kif-project.md)
    NAME/src/NAME.kif     a starting point of the chosen kind
    NAME/.gitignore       the build directory

The files are never overwritten: `kif new` refuses a directory that already has one of them.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List

KINDS = ("driver", "app", "staticlib", "solib")
ALIASES = {"library": "staticlib"}  # the earlier name of `staticlib`
# kind -> (CMake command line start, backends, default target, source template name)
CMAKE_COMMAND = {"driver": "add_kif_library", "app": "add_kif_executable", "staticlib": "add_kif_library", "solib": "add_kif_library"}
CMAKE_TYPE = {"staticlib": "    TYPE STATIC\n", "solib": "    TYPE SHARED\n    VISIBILITY hidden   # -fvisibility=hidden: only #[visibility(default)] functions are in the .so\n"}
DEFAULT_TARGET = {"driver": "freebsd-kernel", "app": "c", "staticlib": "c", "solib": "c"}

CMAKE = """\
cmake_minimum_required(VERSION 3.20)
project({name} LANGUAGES C CXX)

# Kif.cmake comes with the jplcz_kif package; when kif is a sibling checkout, set CMAKE_PREFIX_PATH or add its cmake/ directory:
#   list(APPEND CMAKE_MODULE_PATH "${{CMAKE_CURRENT_SOURCE_DIR}}/../kif/cmake")  then  include(Kif)
find_package(jplcz_kif REQUIRED)
{use_project}
# Generates <build>/kif/{name}/{{{backend_dirs}}}/*.h and *.c from the sources and compiles them into a library.
# KIF_TARGET: the target definition (kif target list); KIF_PROFILE (debug|perf|release|sanitize) is derived from CMAKE_BUILD_TYPE when
# omitted; also KIF_CFG, KIF_SANITIZE, KIF_SYSROOT, KIF_INCLUDE_DIRECTORIES and KIF_WERROR. -DKIF_TARGET=... overrides at configure time.
{command}({name}
    SOURCES src/{name}.kif
{type_line}    BACKENDS {backends}
    KIF_TARGET {target}
    # KIF_CFG smp
    # KIF_INCLUDE_DIRECTORIES ${{CMAKE_CURRENT_SOURCE_DIR}}/src
)

{usage}
"""

USE_PROJECT = """
# Reads kif.toml (shared settings for the kif command line tools too). Optional: everything can live in this file instead.
kif_use_project()
"""

TOML = """\
# Optional kif project file (docs/kif-project.md): lets the kif command line tools (`kif check`, `kif fmt`, ...) and CMake
# (kif_use_project()) share settings. CMake projects do not need it; the settings in CMakeLists.txt are enough.
target = "{target}"          # a built-in target (kif target list) or the path of a .json file
# profile = "debug"          # debug | perf | release | sanitize; CMake derives it from CMAKE_BUILD_TYPE when unset
# cfg = ["smp"]              # extra `#[cfg(..)]` flags / key=value pairs
# include = ["src"]          # import search directories (-I), relative to this file
"""

DRIVER = """\
// A kobj interface and one implementation of it. `kif build` / CMake turns the interface into <name>_if.h/.c and the
// `impl` into a kobj class (<name>_kobj.c); the callers include the kobj headers of their kernel themselves.
extern "C" type device_t;

#[kobj, self(device_t)]
interface {ident} {{
    // Called when the device is found; return 0 on success or an errno.
    method attach() -> i32;
}}

struct softc {{
    attached: u32,
}}

impl {ident} for softc {{
    fn attach(&mut self) -> i32 {{
        self.attached = 1;
        0
    }}
}}
"""

APP = """\
// A program. `main` is the entry point; kifc writes the C `int main(int argc, char **argv)` around it, and `args` is argv as
// string views (args[0] is the program name). The returned value is the exit status.
fn main(args: &[str_view]) -> i32 {{
    if args.len() < 2 {{
        return 2;
    }}
    0
}}
"""

LIBRARY = """\
// A library: the C backend writes <name>_proc.h (prototypes) and <name>_proc.c (the bodies).
// `visibility(default)` keeps `twice` in the .so's exports when it is built with -fvisibility=hidden.
#[export, visibility(default)]
fn twice(value: u32) -> u32 {{
    value * 2
}}
"""

GITIGNORE = "/build/\n"


def ident(name: str) -> str:
    """A snake_case identifier for `name`: `My-Driver` gives `my_driver`."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return out if out and not out[0].isdigit() else "_" + out


def files(name: str, kind: str, target: str, toml: bool = False) -> Dict[str, str]:
    """relative path -> content of every file of the new project."""
    kind = ALIASES.get(kind, kind)
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} (one of {', '.join(KINDS)})")
    ident_name = ident(name)
    backends = "c freebsd" if kind == "driver" else "c"
    body = {"driver": DRIVER, "app": APP}.get(kind, LIBRARY).format(ident=ident_name, name=ident_name)
    usage = {"app": "# Run it: ./build/{n}", "driver": "# Link {n} into your kernel module: target_link_libraries(my_module PRIVATE {n})"}.get(
        kind, "# Link {n} into your own code: target_link_libraries(my_module PRIVATE {n})").format(n=ident_name)
    out = {
        "CMakeLists.txt": CMAKE.format(name=ident_name, backends=backends, target=target, backend_dirs=backends.replace(" ", ","),
                                       command=CMAKE_COMMAND[kind], type_line=CMAKE_TYPE.get(kind, ""), usage=usage,
                                       use_project=USE_PROJECT if toml else ""),
        f"src/{ident_name}.kif": body,
        ".gitignore": GITIGNORE,
    }
    if toml:
        out["kif.toml"] = TOML.format(target=target)
    return out


def create(directory: str, kind: str = "driver", target: str = "", toml: bool = False) -> List[str]:
    """Write the project into `directory` (created when missing); returns the files written. Nothing is overwritten."""
    name = os.path.basename(os.path.abspath(directory))
    kind = ALIASES.get(kind, kind)
    content = files(name, kind, target or DEFAULT_TARGET.get(kind, "c"), toml)
    clash = [p for p in content if os.path.exists(os.path.join(directory, p))]
    if clash:
        raise FileExistsError(f"{directory}: already has {', '.join(sorted(clash))}")
    for rel, text in content.items():
        path = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return sorted(content)
