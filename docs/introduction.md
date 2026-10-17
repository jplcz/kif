# Introduction to kif

## What it is

**kif** is a small declaration language and compiler for *kernel interfaces*. You describe an interface once,
in a `.kif` file: its methods, their types, defaults, and how errors are returned. `kifc` then generates the code
for each target you need, so the declaration is the single source of truth and the generated pieces cannot drift
apart.

```
#[errno(positive)] interface probe_dev {      // an interface, and how its int results encode errors
  method probe() -> cresult<int>;         // a method: name, typed parameters, return type
  method attach(l: cptr<limits>) -> cresult<void>;
}
```

The name started as "**k**obj **i**nter**f**ace": the first target is FreeBSD's kobj object system.

## Why

Kernels are full of interfaces that are written down two or three times by hand: a C ops table, the C++ wrapper
that calls it, the signature checks, the documentation. Each copy is a chance to get a type, a sign convention or a
default wrong, and they only disagree at run time. kif removes the copies:

- **One declaration**, checked (types, defaults, name collisions, signature hashes) before anything is generated.
- **Several languages from it**: C that matches FreeBSD's `makeobjops` layout, and C++ that is type-checked at
  compile time, calling each other across the C ABI.
- **Kernel conventions are explicit**: who owns the softc, whether `int` means `+errno` (FreeBSD) or `-errno`, what
  a missing method does.
- **Adoptable step by step**: existing FreeBSD `.m` files import, and hand-written ops tables can be ported one
  class at a time.

## How it fits together

```
 .kif ──▶ kiflang (parse, check, ids) ──▶ backends ──▶ generated code ──▶ runtime
                                           ├ --cpp      <stem>_if.hpp/.cpp   kif/kobj/kobj.hpp (C++, reloco)
                                           └ --freebsd  <stem>_if.h/.c       <sys/kobj.h> drop-in + kobj_c.cpp
          └▶ kiflang.lsp (diagnostics, hover, completion)      kif-editor (highlighting for every editor)
```

- **Front end** (`kiflang/`): language, diagnostics `KIFnnn`, method ids, signature hashes.
- **Backends** (`kiflang/cxxpreview.py`, `freebsd.py`): turn the checked model into source text. They are the only
  target-specific part.
- **Runtimes** (`include/kif/kobj/`): the small library the generated code calls into. Built on
  [reloco](https://github.com/jplcz/reloco) (allocators, results, lifetime annotations).
- **Tooling**: language server, editor grammars generated from one keyword list, CMake integration.

## Where it is going

Today kif generates **kobj** interfaces (object-oriented method dispatch: newbus-style drivers, devices, buses).
The aim is a front end for *kernel interface generation in general*, with kobj as the first backend rather than the
definition of the language. Directions that the design already leaves room for (none implemented yet):

| Interface kind | What kif would generate |
| --- | --- |
| kobj interfaces (**done**) | C and C++ method tables, handles, defaults; kif structs as kobj classes (C, `kif-kobj-lifecycle.md`) |
| System-call and ioctl tables | Numbers, argument structs, user/kernel marshalling, validation stubs |
| Driver and subsystem operation vectors | Ops structs and registration, without the kobj dispatch |
| Message / IPC / RPC protocols | Message layouts, encoders/decoders, handler tables |
| Register and memory-mapped blocks | Accessors and bit-field helpers (`register`, `block` are reserved words) |
| Documentation and bindings | Reference pages, other language bindings, test doubles |

What this means for the language today: declarations (`enum`, `struct`, typed methods, `cresult<>` with an `errno`
style, `version`) describe *interfaces*, not kobj specifically. A backend decides what to emit from them. New
backends should not need new syntax for what exists, and anything kobj-specific (`self`, `softc`, `foreign class`)
is an attribute on an interface a backend can ignore. The list above is a direction, not a promise: each item arrives when a
concrete consumer needs it, as v1 of the language still changes freely.

## Next steps

| You want to... | Read |
| --- | --- |
| Learn the language and look up a diagnostic | [`kif-proc-language.md`](kif-proc-language.md) |
| Format, lint, graph, document, quick-fix, serve the editor | [`kif-tools.md`](kif-tools.md) |
| Keep a `#[kobj]` interface ABI stable | [`kif-abi.md`](kif-abi.md) |
| Understand object lifetime, `drop` and idiomatic construction | [`kif-lifetime-and-construction.md`](kif-lifetime-and-construction.md) |
| Run `kifc` or use it from CMake | [`kif-cmake.md`](kif-cmake.md) |
| Use the C++ or C runtime | [`kobj-runtime.md`](kobj-runtime.md) |
