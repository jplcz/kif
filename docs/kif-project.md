# kif project settings: CMake first, `kif.toml` optional

Two ways to say "which target, profile, include dirs":

1. **CMake (primary).** `kif new` writes a `CMakeLists.txt` that carries the settings (`KIF_TARGET`, `KIF_CFG`, ...) as keywords or
   target properties. See `docs/kif-cmake.md` for every setting and the precedence.
2. **`kif.toml` (optional).** For command line use (`kif check`, `kif fmt`, editor, CI) without CMake, or to share settings between
   CMake and those tools through `kif_use_project()`. Projects that only build through CMake do not need it.

## `kif new`

```
kif new uart --kind driver            # CMakeLists.txt, src/uart.kif, .gitignore (target freebsd-kernel, backends c + freebsd)
kif new tool --kind app               # executable with `fn main(args: &[str_view]) -> i32` (add_kif_executable, target c)
kif new mylib --kind staticlib        # static C library (add_kif_library TYPE STATIC; `library` is the old name)
kif new mylib --kind solib            # shared C library (TYPE SHARED)
kif new uart --target linux-kernel    # another built-in target (kif target list)
kif new uart --toml                   # also kif.toml and a kif_use_project() call
cmake -S uart -B uart/build -G Ninja && cmake --build uart/build
```

It never overwrites: a directory that already has one of the files is refused (exit 1).

## `kif.toml` keys

The nearest `kif.toml` at or above the current directory is used. `--project FILE` picks another, `--no-project` ignores it.
Relative paths are relative to the file. The command line wins over the file; list options (`cfg`, `include`, `sanitize`,
`baseline`) add to the file's list. Unknown keys and wrongly typed values are errors (exit 2) naming file and key: a typo must not
silently disable a setting.

```toml
# Shared by every tool.
target   = "freebsd-kernel"   # built-in target name or a .json file                      (--target)
profile  = "release"          # debug | perf | release | sanitize                          (--profile)
sanitize = ["undefined"]      # sanitizers; `none` clears                                  (--sanitize)
cfg      = ["smp"]            # #[cfg(..)] flags or key=value                              (--cfg)
include  = ["kif"]            # import search directories                                  (-I)
sysroot  = "sysroot"          # the only place imports are read from                       (--sysroot)
format   = "text"             # text | json | sarif | github                               (--format)
werror   = false              # warnings are errors                                        (--Werror)

[build]                       # kif build / kifc
c            = "gen/c"        # output directory of the C backend                          (--c)
freebsd      = "gen/bsd"      # output directory of the FreeBSD backend                    (--freebsd)
emit-source  = true           # also write the function bodies                             (--emit-source)
manifest     = "m.json"       # ABI manifest                                               (--manifest)
depfile      = "d.d"          # dependency file                                            (--depfile)
sources      = ["src/*.kif"]  # what `kif build` with no file builds; add_kif_library's default SOURCES

[lint]
config   = ".kiflint"         # rule levels                                                (--config)
baseline = ["base.json"]      # ABI manifests to compare against                           (--baseline)

[fmt]
indent   = 4                  #                                                            (--indent)

[watch]
interval = 0.5                # seconds between polls of `--watch`
```

## CMake integration of `kif.toml`

`kif_use_project([FILE kif.toml])` runs `kif project --cmake`, includes the generated fragment (variables `KIF_PROJECT_TARGET`,
`_PROFILE`, `_SANITIZE`, `_CFG`, `_INCLUDE`, `_SYSROOT`, `_WERROR`, `_SOURCES`) and re-runs configure when the file changes.
Those are the lowest-priority defaults: keyword > target property > `-DKIF_*` > `kif.toml` > `CMAKE_BUILD_TYPE`. Output
directories (`[build] c/freebsd`) are command line only; CMake chooses its own. Generation always runs `kifc --no-project`, so
the build does not depend on whichever `kif.toml` is nearest to the build tree.

```cmake
find_package(jplcz_kif REQUIRED)
kif_use_project()                          # optional; reads kif.toml next to this CMakeLists.txt
add_kif_library(uart BACKENDS c freebsd)   # SOURCES default to [build] sources
add_kif_executable(tool SOURCES main.kif BACKENDS c)   # TYPE EXECUTABLE: the sources define `fn main`
```

## Other command line features

See `docs/kif-tools.md`: `kif completion`, `--watch`, `--format`, `kif project`.
