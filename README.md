<p align="center"><img src="assets/logo.svg" alt="kif logo" width="160"></p>

# kif

**A memory-safe language for kernel-mode code, with first-class C and C++ interop and a build that reads exactly what you tell
it to read.**

kif started as a declaration language for kernel interfaces (the name is "**k**obj **i**nter**f**ace") and is growing into a
small, Rust-like procedural language that lives in `.kif` files next to those declarations. One front end, written in plain
Python, checks the code and lowers it to C++ (the primary target) and to C, so that kernel and driver code can be written in a
safe language and still be built, linked and called by the C and C++ that surrounds it.

kif is a complement to C and C++ in low-level systems, not a replacement for them. C and C++ remain the foundation of kernels,
firmware and their toolchains; kif is meant for the parts where explicit safety and borrow checking pay off most, such as
drivers, protocol parsers and code that handles untrusted input, and it coexists with the surrounding C/C++ in the same
build, the same binary and the same ABI. Nothing prevents a project from going further and writing whole
subsystems, or even large parts of a kernel, purely in kif; the interop is there so that this can happen gradually, not so
that it has to.

## Why kif exists

kif is the answer to five problems with writing kernel and embedded code today.

### 1. C++ is the right base for embedded systems, but it is not enough

C++ is a good fit for firmware, RTOS tasks and kernels: no mandatory runtime, deterministic destruction, zero-cost
abstractions, templates, and a toolchain for every target. It also carries decades of baggage that is hostile to kernel
work: exceptions and RTTI that must be switched off, a standard library that assumes a hosted environment, hidden
allocation and hidden calls, implicit conversions, and `thread_local` that many embedded targets simply do not have. kif keeps
C++ as the output language and the home of the runtime, and adds what C++ lacks: a small, explicit language with
ownership, lifetimes, exhaustive `match`, `result<T>` with `?`, and declarations that become kobj interfaces, register blocks,
enums and (soon) system-call tables, all from one source of truth.

The standard library has the same problem. The STL offers good primitives (`std::string`, `std::vector`, `std::thread`, `std::function`),
but it is not friendly to low-level targets: operations that allocate or start work have no explicit failure mode. They
throw, abort or invoke undefined behaviour when memory runs out, and many assume a hosted environment. In kernel mode, running
out of memory is the normal case to plan for, not an exceptional one, so every allocation, container growth and thread or
callable construction has to be able to fail visibly. kif's library and the reloco runtime beneath it provide the same
primitives with `result<T>` and `try_` forms in place of the STL's implicit ones.

### 2. `--sysroot` in C and C++ is unpredictable, and kif's is exact

In a C/C++ toolchain, `--sysroot=DIR` is a hint: it prefixes *some* search paths, while `/usr/include`, the compiler's own
resource directory, `-I`, `-isystem`, `CPATH`, `#include_next` and the current directory still take part. Which header a build
really used is something you find out with `-H` after the fact, and a build can quietly pick a host header and still "work"
while being wrong for the target.

kif refuses that. With `--sysroot DIR`, **`DIR` is the only place library modules are read from**: no search path, no
environment variable, no current directory, no fallback to files installed with the compiler (the prelude is read from the
sysroot too). A missing file is an error, two candidates for one name is an error, an import that escapes the sysroot (`..`,
absolute path, symlink) is an error. The same input and the same sysroot give the same result on every machine, and editors,
the language server, the formatter and the linter resolve imports through the same single implementation. See
[`docs/kif-proc-language.md`](docs/kif-proc-language.md) section 15.2.

### 3. Memory safety is a property of the language, not a convention

In kif, safe code cannot dereference a raw pointer, index out of bounds without a defined outcome, use a moved-from value,
read an uninitialised variable, or let a reference outlive what it borrows. Ownership, moves, borrow checking with Rust's
lifetime rules, exhaustive `match`, checked integer semantics (identical in the interpreter, C and C++) and a recursion
check are part of the compiler, and everything that can bypass them is `unsafe` and therefore reviewable by search. Send and
sync are opt-in. The checks are done by kif itself, in Python, without invoking clang.

### 4. Interop with C and C++ is a design goal, not an afterthought

Real kernels are C and C++. kif declares foreign code explicitly (`extern "C"`, `extern "C++" type`, `header`, `import`), never
by parsing headers, and lowers to plain C functions and C++ classes that use the same ABI as hand-written code. kif interfaces
are FreeBSD-`makeobjops`-compatible, existing `.m` files import, and C and C++ classes implement and call the same
interface. Anything a lowering cannot express in C is refused with a diagnostic, never silently bent.

### 5. Kernel and low-level development is the main reason

Kernel, driver and other low-level code is what kif is for, and the language is shaped by it. There are **no exceptions and no
RTTI**: neither exists in the language, and the generated C++ builds with `-fno-exceptions -fno-rtti`. Failure is always an
explicit value, `result<T>` (lowered to `reloco::result<T>`), propagated with `?` and handled by an exhaustive `match`; a
function that can fail says so in its signature, and nothing unwinds. There is no hidden allocation, no hidden call and no
runtime beyond a small header, and allocators are explicit and persistent.

Kernel constraints are checked, not documented: atomic-context annotations (`#[might_sleep]`, `#[atomic_entry]`,
`#[atomic_scope]`, `#[atomic_lock]`, `#[atomic_callback]`) are verified across interfaces, hooks and implementations, so a
function that can sleep is never called with a spinlock held or from an interrupt handler. Kernel conventions such as who owns
the softc, whether `int` means `+errno` or `-errno`, and what a missing method does are declared, not assumed. Precompiled
modules (`.kifm`) let a vendor ship a driver development kit that is importable and type-checked, without shipping the
implementation of its ordinary functions (generic code necessarily stays as source).

### Builds with the build system you already have

kif does not come with its own build system and does not ask for one. The primary build system is the same one C and C++
projects use, CMake first: `add_kif_library` and `kif_target_sources` run the kif front end as an ordinary build step that
produces C++ (or C) sources and headers, which are then compiled by the same compiler, flags, toolchain files and sysroot as
the rest of the project. `.kif` files are listed next to `.c` and `.cpp` files, with normal dependency tracking and incremental
rebuilds. Existing build systems are not rewritten or replaced: a kernel module, firmware image or driver tree adopts kif one
file at a time, and the generated code links with the existing code in both directions. The same applies to `--sysroot`:
the build passes the one sysroot to kif and to the C/C++ compiler. See [`docs/kif-cmake.md`](docs/kif-cmake.md).

### Hardening inherited from reloco

kif's C++ output and runtime are built on [reloco](https://github.com/jplcz/reloco), whose premise is that unsafe code in C
and C++ is far too easy to write: a dangling view, an uninitialised local, an out-of-bounds index or a silent copy of a
resource all compile without a complaint. reloco makes the safe form the default one and the unsafe form an explicit,
reviewable decision, and kif keeps those guarantees at the language level:

- **Failure is explicit, not undefined behaviour.** Fallible operations return `reloco::result<T>`; there are no exceptions and
  no RTTI, and nothing relies on debug-only asserts or on a trap that only fires in some build modes.
- **Type-erased allocators, no rebinding.** Containers take a two-word `allocator_ref` handle instead of an allocator template
  parameter, so `vector<T>` has the same type whether it lives on the heap, in a pool (`uma_zone`-style), on a stack or in a
  bump arena. There is no `std::allocator_traits::rebind` machinery: an allocator is never re-instantiated per node or element
  type, one backend serves every container, and allocator choice is a runtime value that never leaks into APIs, ABIs or
  `.kifm` signatures. Every allocator operation returns `result<T>`, so exhaustion is handled, never thrown.
- **Hardened containers and views.** Checked element access, bounds-aware spans and string views, fixed-capacity and
  allocator-backed containers with fallible construction (`try_` protocol), so kernel code has no unchecked access.
- **Lifetime safety.** Borrowed values and pointers carry lifetime annotations, consumed-value tracking and rvalue protection
  for accessors that alias their owner; the generated C++ keeps these annotations so clang reports dangling borrows.
- **Move-by-default and infallible move.** Types are moved, not silently copied; copying a resource is deliberate (`clone`).
- **Send/sync, locking and atomic-context discipline.** Thread-transfer and sharing safety follow Rust's `Send`/`Sync`;
  lock annotations and atomic-context checks catch sleeping in atomic sections.
- **A safety checker for the surrounding C++.** `reloco-tidy` enforces the same rules in hand-written C++ (no raw pointers or C
  arrays, `noexcept`, initialised locals, a Rust-style borrow check), with opt-in regions and explicit exemptions.



## Status

The language is under active construction, and the repository holds the front end:

| Part | State |
| --- | --- |
| `kiflang/` (new) | Lexer, recovering parser, symbols, modules, `import`/`use`, the shipped prelude, the full semantic checker (types, generics with bounds, methods and interfaces with `extends`, tuples, moves, borrows and lifetimes, exhaustive `match` with guards and bindings, `result<T>` and `?`, `unsafe`, atomic context), the sandboxed interpreter for generation-time code with the reflection API, `--sysroot`, precompiled `.kifm` modules, and `python3 -m kiflang check\|export\|modules` |
| not yet in `kiflang/` | Code generation: C++ and C lowering, `kif/proc.h`, differential tests, TLS and per-CPU data, formatter, linter and language-server updates |

The language is specified in [`docs/kif-proc-language.md`](docs/kif-proc-language.md) (goals, types, ownership, generics, atomic
context, reflection, precompiled modules, diagnostics, implementation plan) and `kiflang/README.md` describes the package.

## A taste

```kif
// Failure is a value: `result<u32>` lowers to reloco::result<u32>. No exceptions, no RTTI.
fn scale(x: u32, k: u32) -> result<u32> {
    if k == 0 { return err(error::invalid); }
    ok(x * k)
}

// `?` propagates the error; `match` must be exhaustive; guards and bindings are allowed.
fn clamp(x: u32, k: u32) -> result<u32> {
    let v = scale(x, k)?;
    match v { 0 => ok(1), n if n > 0xff => ok(0xff), n => ok(n) }
}
```

## Repository map

| Path | Contents |
| --- | --- |
| `kiflang/` | The front end and tools (`kif`, LSP): lexer, parser, checker, interpreter, reflection, sysroot, `.kifm` modules, shipped prelude (see its README) |
| `kif-editor/` | Editor support for `.kif` files (VS Code, JetBrains, Vim/Neovim, Sublime Text, Emacs, Tree-sitter) |
| `include/kif/kobj/` | Runtimes: C++ `kobj.hpp` + `status.hpp` (header-only), and the drop-in C `<sys/kobj.h>` (`compat/`) with its implementation `kobj_c.cpp` |
| `cmake/` | `Kif.cmake` (`add_kif_library`, `kif_target_sources`, `kif_install_headers`) and the package config |
| `tests/` | Python tests, registered with ctest as `Kif.python_tests` |
| `docs/` | Language specification, guide, `kifc`/CMake guide, runtime reference |
| `scripts/` | `check-no-std-types.py`, the header gate also run by the build |

## Reading list

New here? Start with [`docs/introduction.md`](docs/introduction.md). The procedural language:
[`docs/kif-proc-language.md`](docs/kif-proc-language.md). Tools (fmt, lint, graph, doc, fix, lsp, highlighters):
[`docs/kif-tools.md`](docs/kif-tools.md). ABI baselines: [`docs/kif-abi.md`](docs/kif-abi.md). The mid-level IR, its JSON/CBOR encoding and the check-elision analyses: [`docs/kif-ir.md`](docs/kif-ir.md). `kifc` and CMake (codegen pending):
[`docs/kif-cmake.md`](docs/kif-cmake.md). Runtimes: [`docs/kobj-runtime.md`](docs/kobj-runtime.md).

## Dependencies

- Python 3 (standard library only) for the compiler, language server and tests.
- [reloco](https://github.com/jplcz/reloco) for the C++/C runtimes. CMake fetches it from GitHub; use
  `-DJPLCZ_KIF_RELOCO_SOURCE_DIR=<path>` (or the environment variable of the same name) for a local checkout.
- [microfmt](https://github.com/jplcz/microfmt), used by the optional call tracing (`kif/kobj/trace.hpp`, enabled only
  with `-DKIF_KOBJ_TRACE`, C++20). Same fetch rules: `-DJPLCZ_KIF_MICROFMT_SOURCE_DIR=<path>`.
  microfmt is not needed.

## Build and test

```sh
cmake -S . -B build -G Ninja -DJPLCZ_KIF_RELOCO_SOURCE_DIR=../reloco -DJPLCZ_KIF_MICROFMT_SOURCE_DIR=../microfmt   # omit the -D to fetch
cmake --build build        # runtime header checks (C++17/20/23), std:: gate
ctest --test-dir build     # Python tests
python3 -m unittest discover -s tests    # tests alone, no CMake
```

## Install the tools

```sh
scripts/install.sh              # into the active virtualenv / pip environment
scripts/install.sh --user       # into ~/.local (commands in ~/.local/bin)
scripts/install.sh --editable   # commands follow this checkout (add --user for ~/.local)
scripts/install.sh --uninstall  # remove (add --user if installed with it)
```

See [`docs/kif-packaging.md`](docs/kif-packaging.md) for CMake install, CPack (`.tar.gz`, `.deb`, `.rpm`), CPM.cmake, Conan 2 and vcpkg.

Installs `kifc`, `kif` (fmt/lint), `kif-check`, `kif-mimport` (`.m` importer) and `kif-lsp` (language server) from
`pyproject.toml`; `pip install .` works too. The C++/C runtime headers are not installed; use the CMake package.

## Use from CMake

```cmake
# FetchContent (structo does this): brings jplcz_kif::kif and the Kif.cmake functions.
FetchContent_Declare(jplcz_kif GIT_REPOSITORY https://github.com/jplcz/kif.git GIT_TAG master)
FetchContent_MakeAvailable(jplcz_kif)

# Generated C++ links jplcz_kif::kif (the runtime, plus reloco) automatically.
add_kif_library(device_ifs SOURCES device_if.kif BACKENDS cpp)
target_link_libraries(my_driver PRIVATE device_ifs)
```

An installed package works the same way with `find_package(jplcz_kif)`.

--------
