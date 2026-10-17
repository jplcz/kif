# kif command line and CMake guide

> **Status:** the C backend (`kifc --c DIR`) and the FreeBSD kobj interface output (`kifc --freebsd DIR`, the interface side
> only: `docs/kif-c-backends.md` section 5) exist. `--cpp` fails with status 2 until that backend is written, so
> `add_kif_library` with it does not build yet. The rest of this page describes the previous
> interface, which the new backends are meant to reproduce. Tools that work today: `docs/kif-tools.md`, `docs/kif-abi.md`.

## kifc --c (C backend)

```sh
python3 -m kiflang.kifc \
    --c gen                  `# write <stem>_proc.h (+ <stem>_proc.c if the file has methods, #[export] fns or cells) here` \
    --target freebsd-kernel  `# a built-in target or a .json file (default: c); see docs/kif-targets.md` \
    --profile release        `# debug | perf | release | sanitize | one the target defines` \
    --sanitize address       `# comma-separated, or none` \
    --cfg my_flag            `# extra cfg flag or key=value; wins over everything` \
    --manifest gen/x.json    `# also write the ABI manifest of the file` \
    --depfile gen/x.d        `# outputs: the source and every module it imports, transitively` \
    -I include               `# import directory, after the importing file's own; KIF_PATH follows (see below)` \
    --emit-source            `# always write <stem>_proc.c (a stub if empty), so a build knows its outputs` \
    --Werror --list-outputs  `# warnings fail; print the files and write nothing` \
    x.kif
```

`<stem>` is the file name without `.kif` and a trailing `_if`. Diagnostics go to stderr as `file:line:col: error CODE: ...`;
exit 0 ok, 1 errors (nothing is written), 2 bad command line or unavailable backend. A construct the backend cannot lower yet
is `KIFC001`. The header records the selection in `/* kif: target=... profile=... [sanitize=...] */`. Output files are
rewritten only when their content changed. Imports are lowered (next paragraph).
`kifc --target T --c-common DIR` writes `kif_c.h`, the target's common C code that every `--c` output includes; generate it once
per target and put `DIR` on the include path.

**Imports.** `import "lib.kif"` is searched like in the C++ path (importing file's directory, `-I` in order, `KIF_PATH`; with
`--sysroot` only the sysroot). The importing `x_proc.h` includes `lib_proc.h`, so **each imported file must be compiled by its
own `kifc --c` run** (`add_kif_library(... BACKENDS c)` does that for all its sources) and the output directory is on the include
path. Types and enums keep their global tags; `lib::f` becomes `lib_f` and `lib::K` becomes `LIB_K`. `use lib::f as g` works for
functions and constants, not for types (C tags cannot be renamed). Not lowered yet: `ptr<S>` of an imported struct,
precompiled `.kifm` modules and modules with `namespace`.

`kifc` compiles `.kif` files into C++ (`<stem>_if.hpp`/`.cpp`) and FreeBSD C (`<stem>_if.h`/`.c` plus `<stem>_enums.h`, makeobjops-style).
`<stem>` is the file name without `.kif` and without a trailing `_if`.

Note: the C++ output includes `<kif/kobj/kobj.hpp>`, kif's kobj runtime (see `docs/kobj-runtime.md`; link
against `jplcz_kif::kif`, which brings reloco's include path). The FreeBSD output needs the
FreeBSD kobj headers (`sys/kobj.h`). `code {}` bodies are compiled as C++ in the `.cpp`, so they must be valid C++.

## kifc

```sh
# Run from the kif checkout, or set PYTHONPATH to it.
python3 -m kiflang.kifc \
    --cpp gen/cpp            `# write <stem>_if.hpp/.cpp here` \
    --freebsd gen/bsd        `# write <stem>_if.h/.c here` \
    --doc gen/docs           `# write <stem>.md from the // comments` \
    -I include               `# extra import directory (repeatable; -Iinclude works too, like cc)` \
    --Werror                 `# warnings fail the build` \
    --manifest lib.json      `# write interface names, C symbols and method ids (one input file)` \
    --check-with other.json  `# fail (KIF040/KIF041) on collisions with another library (repeatable)` \
    --depfile gen/cpp.d      `# Makefile-style depfile: outputs + transitive imports` \
    widget_if.kif

# Merge per-file manifests into the manifest of a library (what Kif.cmake does); a name defined twice is KIF040.
python3 -m kiflang.kifc --merge-manifest lib.json a.json b.json

# KIF_PATH is the CPATH of kif: extra import directories, searched after every -I.
KIF_PATH=/opt/sdk/kif:/usr/local/share/kif python3 -m kiflang.kifc --cpp gen widget_if.kif

python3 -m kiflang.kifc --cpp gen --list-outputs widget_if.kif   # print the files that would be written
```

| Rule | Behaviour |
| --- | --- |
| Exit status | 0 ok, 1 diagnostics with errors (or warnings with `--Werror`) |
| Errors | Nothing is written; diagnostics go to stderr as `file:line:col: error KIFnnn: ...` |
| Import search order | Like `#include "x"` in C: the importing file's directory, then `-I` directories in command-line order, then the `KIF_PATH` directories (`:`-separated, `;` on Windows). The language server uses the same order |
| Unchanged output | Files are rewritten only when the content changed (no needless rebuilds) |
| `cxx_only` interfaces | Skipped by the FreeBSD backend |
| Collisions | The C symbol is `<iface>_<method>`; `a_b`+`c` collides with `a`+`b_c` (KIF040), equal method ids give KIF041 |

## CMake

The C backend: `add_kif_library(name SOURCES a.kif b.kif BACKENDS c KIFC_ARGS --target freebsd-kernel --profile release)`
(also `kif_target_sources(... BACKEND c KIFC_ARGS ...)`). It runs `kifc --c` per file (generating `<stem>_proc.h`/`.c`, with
`--emit-source`) and one rule for `kif_c.h` per library (`--c-common`), which every output depends on. The headers, `kif_c.h`
included, are in `KIF_GENERATED_HEADERS`, so `kif_install_headers` ships them. Imports between the sources are tracked by the
per-file depfile.

```cmake
# From an installed package, find_package(jplcz_kif) already includes Kif.cmake (Python3 is required).
find_package(jplcz_kif REQUIRED)
# Via FetchContent / add_subdirectory (as structo does), Kif.cmake is included for you as well.
# By hand: list(APPEND CMAKE_MODULE_PATH "${jplcz_kif_SOURCE_DIR}/cmake") then include(Kif).
# KIF_ROOT (the directory holding the kiflang package) defaults to the checkout; override to use another one.

# A library from .kif files. BACKENDS defaults to cpp. The generated include dirs are PUBLIC.
add_kif_library(device_ifs
    SOURCES      device_if.kif bus_if.kif
    BACKENDS     cpp freebsd        # cpp, freebsd, or both
    INCLUDE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/kif   # import search path (-I)
    OUTPUT_DIR   ${CMAKE_CURRENT_BINARY_DIR}/gen   # default: <build>/kif/<name>
    LINK_LIBRARIES some_lib         # linked PUBLIC
    CHECK_WITH   other_ifs          # fail if names/ids collide with that library's manifest
    WERROR)

# FreeBSD kobj interfaces (.m) are accepted next to .kif files: each is converted by kif-mimport into
# <OUTPUT_DIR>/m/<name>.kif (comments and #include lines kept, see kiflang/README.md) and then compiled like any .kif.
# Conversion warnings are printed during the build. The generated files are named after the .m file (clock_if.m
# gives clock_if.hpp/.h). Same for kif_target_sources().
add_kif_library(bsd_ifs SOURCES ${FREEBSD_SRC}/sys/kern/clock_if.m BACKENDS cpp freebsd)

# BACKENDS c freebsd with a kernel target: the freebsd backend also writes <stem>_kobj.c (the kobj class of
# `impl <kobj iface> for S`; a stub comment when the file has none), so the build always knows its outputs. Both
# backends need the target: KIFC_ARGS applies to c and freebsd. The module defines M_KIF itself (kif_c.h only declares it).
add_kif_library(drv SOURCES uart.kif BACKENDS c freebsd KIFC_ARGS --target freebsd-kernel)

# Attach generated files to a library you already have (BACKEND defaults to cpp).
add_library(mydrv drv.cpp)
kif_target_sources(mydrv SOURCES led_if.kif BACKEND cpp)

# Install the generated headers into an SDK include directory (COMPONENT is optional).
kif_install_headers(device_ifs DESTINATION sdk/include COMPONENT sdk)
```

## Per-target settings and the project file

CMake is the primary way to configure a build; `kif.toml` (see `docs/kif-project.md`) is optional. Every setting can be given
for one target, as a keyword of `add_kif_library()` / `kif_target_sources()` or, for a target that already exists, as a **target
property** of the same name that `kif_target_sources()` reads:

| Setting | Meaning (kifc option) |
|---|---|
| `KIF_TARGET` | target definition (`--target`) |
| `KIF_PROFILE` | `debug`/`perf`/`release`/`sanitize` (`--profile`) |
| `KIF_SYSROOT` | the only import root (`--sysroot`) |
| `KIF_SANITIZE`, `KIF_CFG` | lists (`--sanitize`, `--cfg`) |
| `KIF_INCLUDE_DIRECTORIES` | import search dirs (`-I`) |
| `KIF_WERROR` | warnings are errors (`--Werror`) |
| `KIF_TRUST` | PEM files of pinned keys/CA certificates (`--trust`); relative paths are resolved against the calling directory |
| `KIF_REVOKED` | PEM files of revoked keys/certificates/CRLs (`--revoked`) |
| `KIF_SIGNING` | `off`, `warn` (default) or `require` (`--signing`) for precompiled `.kifm` imports |
| `KIF_SMALL_STACK` | stack budget in bytes or `off` (`--small-stack`, docs/kif-stack.md); keyword of `add_kif_library`/`kif_target_sources` too |
| `KIF_PROVER` | `any` or `same` (`--prover`); `same` makes a different vendor prover KIFI041 an error |

Trust and revocation files are dependencies of every generated file, so changing a key re-runs the checks.
Variables (`-DKIF_TRUST=...`), `kif.toml` `[signing]` (`KIF_PROJECT_TRUST/REVOKED/SIGNING/PROVER`), target properties
and keywords (`KIF_TRUST`, `KIF_REVOKED`, `KIF_SIGNING` of `add_kif_library`/`kif_target_sources`) follow the usual precedence.

```cmake
# Require every precompiled import to be signed by a key under our pinned vendor CA.
add_kif_library(drv SOURCES drv.kif BACKENDS c
    KIF_TRUST   keys/vendor-ca.pem   # pinned CA; a signer's certificate must be issued by it
    KIF_REVOKED keys/revoked.pem     # offline CRL / revoked fingerprints
    KIF_SIGNING require)             # unsigned or untrusted imports are errors (default: warn)
```

Precedence, highest first: keyword > target property > `-DKIF_*` > `kif.toml` (`kif_use_project()`) > `CMAKE_BUILD_TYPE`
(Debug->debug, RelWithDebInfo->perf, Release/MinSizeRel->release). List settings add up across levels. `kifc` always runs
with `--no-project`, so the build never depends on a stray `kif.toml`.

```cmake
# Keywords: the library does not exist yet when its rules are made, so a property set afterwards cannot reach them.
add_kif_library(uart SOURCES uart.kif BACKENDS c freebsd
    KIF_TARGET freebsd-kernel          # which target definition to lower for
    KIF_CFG    smp                     # extra #[cfg(smp)] flag
    KIF_INCLUDE_DIRECTORIES ${CMAKE_CURRENT_SOURCE_DIR}/kif)

# Properties: for a target you created yourself, set them before kif_target_sources().
add_library(mod STATIC mod.c)
set_target_properties(mod PROPERTIES KIF_TARGET freebsd-kernel KIF_SANITIZE undefined)
kif_target_sources(mod SOURCES mod.kif BACKEND c)

kif_use_project()   # optional: read kif.toml once; its keys become KIF_PROJECT_* defaults
```

The keyword values are also stored as properties on libraries made by `add_kif_library()`.

### Incremental builds

`Kif.cmake` runs `kifc` once **per source file and backend**, so after a change only what depends on it is redone:

| You change | What runs again |
| --- | --- |
| `a.kif` | `kifc` for `a.kif` only; the library manifest is merged again; the C/C++ files whose generated content really changed are recompiled |
| `common.kif`, imported by `b.kif` | `kifc` for `b.kif` (and any other importer, directly or transitively), found through the per-file depfile `<out>/<backend>/<stem>.d` (Ninja and Makefile generators; other generators track only the source itself) |
| A `kiflang/*.py` compiler file | every file (the compiler is a dependency of each command) |
| A manifest listed in `CHECK_WITH` | every file of the library (its collisions are checked again) |
| Nothing | nothing |

A command that reruns but produces byte-identical output leaves the file's timestamp alone (`restat`), so nothing
downstream is recompiled. Note that the generated header records a hash of its own `.kif`, so editing a file, even only
a comment, rewrites its own outputs but never those of other files.

Per-file outputs: `<out>/<backend>/<stem>_if.*`, `<out>/manifests/<stem>.json` (a per-file manifest) and
`<out>/<name>.manifest.json`, merged from them. Target properties: `KIF_MANIFEST`, `KIF_FILE_MANIFESTS`,
`KIF_GENERATED_HEADERS`. Collision checks (KIF040/KIF041) between the files of one library happen while merging
the manifests; against other libraries (`CHECK_WITH`) in each `kifc` run.
Generated headers carry the input path as given; pass relative paths if reproducible output matters.

## Examples



## Stack check (`kif_stack_check`)

```cmake
add_kif_library(drv SOURCES drv.kif BACKENDS c)
# Compare the compiler's frames with kif's estimate (docs/kif-stack.md).
#   TARGET drv  adds -fstack-usage (GCC or clang) to drv and reads the .su files of its objects
#               under <bindir>/CMakeFiles/drv.dir; the frames are those of your compiler and flags
#   SOURCES     the .kif files, lowered again to map C symbols back to kif functions
#   FLAGS       only without TARGET: extra flags for the private compile with CMAKE_C_COMPILER
#   ALL         run as part of the default build
kif_stack_check(drv_stack SOURCES drv.kif TARGET drv)
# cmake --build . --target drv_stack   (fails when the compiler used more than the estimate)
```
