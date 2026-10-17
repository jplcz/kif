# Packaging kif

kif can be installed three ways. All install the same tools: `kifc`, `kif` (fmt/lint), `kif-check`, `kif-mimport`
(`.m` importer) and `kif-lsp` (language server).

| Way | Command | Installs |
| --- | --- | --- |
| pip / virtualenv / `~/.local` | `scripts/install.sh [--user] [--editable]` (or `pip install .`) | Python package and commands only (no C/C++ runtime headers) |
| CMake install | `cmake --install build --prefix /usr/local` | Runtime headers, CMake package (`find_package(jplcz_kif)`), docs, the Python package under `share/cmake/jplcz_kif/python` and launchers in `bin/` |
| CPack archives and packages | see below | The CMake install, as a `.tar.gz`, `.deb` or `.rpm` |

## CPack

```sh
cmake -S . -B build -G Ninja
cmake --build build
cd build
cpack -G TGZ          # jplcz-kif-<version>-Linux.tar.gz
cpack -G DEB          # jplcz-kif_<version>_all.deb   (needs dpkg-deb)
cpack -G RPM          # jplcz-kif-<version>.noarch.rpm (needs rpmbuild)
cpack --config CPackSourceConfig.cmake   # source tarball
```

Packages depend only on `python3 >= 3.8`; they are architecture independent. The version comes from `project(... VERSION)`
in `CMakeLists.txt`; `scripts/bump-version.py` keeps it equal to `pyproject.toml`, `conanfile.py` and the vcpkg manifest.

## Launchers

`bin/<tool>` is a small shell script (`cmake/kif-tool.sh.in`) that sets `PYTHONPATH` to the installed Python package,
relative to the script, and runs `python3 -m <module>`. The install prefix is therefore relocatable. Set `PYTHON` to use a
different interpreter.

CPack is configured only when kif is the top-level project and `JPLCZ_KIF_INSTALL_HEADERS_ONLY` is off.

## Package managers

Every route exposes the same CMake package: `find_package(jplcz_kif CONFIG REQUIRED)` gives the target
`jplcz_kif::kif` and the `add_kif_library()` / `kif_target_sources()` functions (`Kif.cmake`). kif depends on
`jplcz_reloco` and `jplcz_microfmt`; each route below supplies them in its own way.

### CPM.cmake

```cmake
include(cmake/CPM.cmake)

# Pin an immutable tag or commit, never a moving branch.
CPMAddPackage(
    NAME jplcz_kif
    GITHUB_REPOSITORY jplcz/kif
    GIT_TAG <release-tag-or-commit>
    OPTIONS
        "JPLCZ_KIF_BUILD_TESTS OFF"          # development targets are not needed by consumers
        "JPLCZ_KIF_BUILD_HEADER_CHECKS OFF"
        "JPLCZ_KIF_CHECK_NO_STD_TYPES OFF"
        "JPLCZ_KIF_INSTALL OFF"              # do not add kif to the consumer's install rules
)

# kif fetches reloco and microfmt itself (FetchContent). To use your own, define the targets first (CPM or
# add_subdirectory) or set JPLCZ_KIF_RELOCO_SOURCE_DIR / JPLCZ_KIF_MICROFMT_SOURCE_DIR to local checkouts.
add_kif_library(device_ifs SOURCES device_if.kif BACKENDS cpp)   # generated headers, linked PUBLIC
target_link_libraries(my_target PRIVATE device_ifs jplcz_kif::kif)
```

### Conan 2

`conanfile.py` (header-library type, package id cleared) builds with `JPLCZ_KIF_USE_SYSTEM_DEPS=ON`, so reloco and
microfmt come from their own Conan packages (`jplcz_reloco`, `jplcz_microfmt`).

```sh
conan export ../reloco && conan export ../microfmt   # until they are on a remote
conan create . --build=missing                       # builds the package
```

```ini
# conanfile.txt of the consumer
[requires]
jplcz_kif/0.1.0
[generators]
CMakeDeps
CMakeToolchain
```

The recipe sets `cmake_find_mode` to `none` and adds `share/cmake/jplcz_kif` to `builddirs`: kif's own config file is
found through `CMAKE_PREFIX_PATH`, so `find_package(jplcz_kif)` and `add_kif_library()` work as in a normal install.
`bin/` holds `kifc` and the other launchers, available to the consumer's build environment.

### vcpkg

The overlay port `packaging/vcpkg/ports/jplcz-kif` packages the current checkout (a registry port should switch to
`vcpkg_from_github` with a release tag and hash). It depends on the `jplcz-reloco` and `jplcz-microfmt` ports.

```sh
vcpkg install jplcz-kif \
  --overlay-ports=/path/to/kif/packaging/vcpkg/ports \
  --overlay-ports=/path/to/reloco/packaging/vcpkg/ports \
  --overlay-ports=/path/to/microfmt/packaging/vcpkg/ports
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake"
```

### `JPLCZ_KIF_USE_SYSTEM_DEPS`

CMake option (default `OFF`) behind the Conan and vcpkg routes: reloco and microfmt are found with `find_package()`
(installed, Conan or vcpkg) instead of being fetched, and kif's install then contains only kif's own files.

## Scripts

| Script | Purpose |
| --- | --- |
| `scripts/bump-version.py` | `--bump patch\|minor\|major` or `--set X.Y.Z`: updates `CMakeLists.txt`, `conanfile.py`, `pyproject.toml` and the vcpkg manifest together |

Sibling checkouts `../reloco` and `../microfmt` are used when present; override with `JPLCZ_KIF_RELOCO_SOURCE_DIR` and
`JPLCZ_KIF_MICROFMT_SOURCE_DIR`. Both scripts only build and install locally (Conan's `create`, vcpkg's `install` with overlay ports); nothing is uploaded
to a remote. They were run with Conan 2.33 and a fresh vcpkg checkout.
