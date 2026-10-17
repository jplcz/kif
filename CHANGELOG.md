# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow semantic versioning; the version lives in
`CMakeLists.txt` and `scripts/bump-version.py` keeps the other files in step.

## 0.1.0

First release

### Language and tools

- `kif-lsp` publishes the lint rules as diagnostics (`lint`, `lintConfig`, `baselines` options) with quick fixes for KIFL016 and rule suppression.
- `m_prolog` / `m_epilog` blocks: the `.m` `PROLOG`/`EPILOG` bridge (C backend only; KIF088, KIF089, KIFW004).
- `.kif` interface language: interfaces, foreign classes, enums, structs, register blocks, attributes.
- `kifc` generates C++ (`_if.hpp/.cpp`), FreeBSD C (`_if.h/.c`), Markdown docs and mocks; manifests and `--check-with`
  for collision checks.
- `kif fmt` and `kif lint` (KIFL001-029), including ABI-stability rules against a released baseline manifest.
- `kif-mimport` converts FreeBSD `.m` files (keeps comments, leaves kobj headers to the caller); all 147 `.m` files of
  FreeBSD `sys/` import and generate.
- `kif-lsp` language server (diagnostics, completion, hover with resolved enum values, quick fixes) and editor
  grammars/VS Code extension.

### Enums

- `enum E : type` with `#[flags]`; generated `name_of`, `try_from_name`, `try_from_underlying`, value tables, flag
  operators, and microfmt formatters through a type-erased `kif::format_enum` (C++17, no per-enum template code).
- C backend: `NAME_name()` and `NAME_FLAGS_MASK`.
- Lint rules KIFL015-017 for flags enums.

### Runtime and build

- Header-only runtime in `include/kif` (checked for C++17, 20 and 23; `kobj/trace.hpp` needs C++20).
- CMake `add_kif_library()` for `.kif` and `.m` inputs, install/export package, CPack (TGZ/DEB), CPM, Conan 2 and
  vcpkg overlay port, pip/user-prefix installer.
- CI: tests, header checks, demo, packaging checks.
