<!--
SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>

SPDX-License-Identifier: BSD-2-Clause
-->

# kif procedural language (design)

Scope: kif started as an interface definition language for kernel objects. It is growing into a language in which **parts of
a kernel can be written in a memory-safe way** (drivers, protocol code, tracing, syscall glue), next to the interface
declarations. The name stays "kif"; this document describes the procedural half.

Status: **design, not implemented.** Nothing here is accepted by `kifc` yet. This document fixes the goals, the
language, the C and C++ lowering, the generation-time API and the implementation plan, so the pieces can be built and
tested one by one.

## 1. Goals

1. A small, safe, Rust-like procedural language inside `.kif` files, with **one syntax and one semantics** for two uses:
   - **Lowered code** (`fn`): compiled by `kifc` into C and C++ function bodies, for example interface default
     implementations, tracing hooks, argument checks and small helpers.
   - **Generation-time code** (`gen fn`): run by an interpreter inside `kifc` while it processes a `.kif` file. It reads the
     parsed declarations (interfaces, methods, enums, a future `syscalltable`) and emits more declarations or text. This is
     how a `#[derive(...)]` attribute customises generation.
2. The subset used by `fn` must behave **identically** in the interpreter, in the generated C and in the generated C++
   (checked by differential tests, section 12).
3. Kernel-friendly output: no allocation, no hidden calls, no exceptions, no runtime library (C++ output uses reloco; C output includes a `kif_c.h` generated from the target), no undefined behaviour in the source language.
4. Two trust levels. **Generation-time code** (`gen fn`, run by the Python interpreter) is sandboxed: it cannot touch the host
   (no files, network, environment, clock) nor run forever. **Lowered code** (`fn`, method bodies, hooks) is *not* sandboxed:
   it is real kernel/driver code that runs wherever the generated C or C++ runs, and it interoperates freely with the
   surrounding C and C++ (section 4.6). Its safety comes from the language rules and the reloco-based lowering, not from
   isolation.
5. **C++ is the primary target.** The C backend supports a documented subset (section 9.1) and refuses the rest with a clear
   diagnostic (KIFP053); no language feature is held back or bent to fit C.
6. **Python-only tooling.** Everything in `kif`, `kifc` and `kif-lsp` is plain Python: the checker, the interpreter and both
   lowerings. No tool invokes clang, libclang or a C/C++ compiler to understand declarations (compilers are used only to
   build the generated output, in tests and by the user). Foreign declarations are written by the developer
   (`extern`, `header`, `import`, section 4.6), never discovered.
7. **Predictable inputs.** The compiler reads library modules from one place, the `--sysroot` directory, and from nowhere
   else: no implicit include paths, environment variables, current directory or compiler-relative defaults. This is the
   opposite of what C/C++ toolchains do with `--sysroot` (section 15.2), and avoiding that unpredictability is one of the founding reasons for the language.
8. **Deliverable without sources.** A vendor can ship precompiled modules (`.kifm`, section 15) that are importable and
   type-checked but contain no implementation of ordinary functions.
9. No back-compat burden: there are no users yet, so existing constructs may be reshaped to fit (section 11).

Non-goals: a general-purpose language, generic structs, impls or traits (generic *free functions* exist, section 4.9; generic C++ types come only from
`extern "C++" type` declarations such as the `reloco.kif` prelude, section 4.10), capturing closures, threads,
user-defined operators, a standard library.

## 2. Overview by example

```kif
// ---- lowered code: becomes C and C++ ---------------------------------------------------------------

// A plain helper. `fn` bodies are lowered, so only the shared subset (section 4) is allowed.
fn align_up(value: u64, align: u64) -> u64 {
    (value + align - 1) & !(align - 1)          // the last expression is the result, as in Rust
}

interface mmio {
    method read(off: u32, width: u8) -> u64;

    // A default implementation written in kif instead of `default some_c_symbol`.
    // `self` is the object; `self.read(..)` lowers to the MMIO_READ(...) wrapper of the C backend
    // and to a virtual-style call in the C++ backend.
    method read_aligned(off: u32) -> u64 {
        let base = align_up(off as u64, 8) as u32;
        self.read(base, 8)
    }
}

// ---- generation-time code: runs inside kifc -----------------------------------------------------------

// A generator. `#[derive(trace)]` on an interface calls `derive_trace(iface, out)` for it.
// `Interface` and `Emitter` are built-in generation-time types; `gen fn` may use strings, arrays,
// reflection and `out.*`, which a lowered `fn` may not.
gen fn derive_trace(iface: &Interface, out: &mut Emitter) {
    for m in iface.methods() {                  // reflection: the declared methods, in source order
        out.kif(                                // `kif(...)` text is parsed and checked like source
            "fn {}_{}_trace({}) { trace_call(\"{}.{}\"); }",
            iface.name(), m.name(), m.params_decl(), iface.name(), m.name(),
        );
    }
}

#[derive(trace)]
interface timer {
    method start(ns: u64) -> i32;
}
```

## 3. Lexical structure and items

Same lexer as the rest of kif (`//` comments, identifiers `[A-Za-z_][A-Za-z0-9_]*`, decimal/hex/binary/octal integer
literals with optional `_` separators and a type suffix such as `10u8`, string literals with `\n \t \\ \" \xNN` escapes).
New keywords (reserved everywhere): `fn gen let mut if else while loop for in match break continue return as unsafe
extern macro type cfg export impl private Self const true false struct enum`. `panic!` and `unreachable!` are built-in macros. `struct` and `enum` keep their current meaning (section 4.5).

New top-level items:

| Item | Meaning |
| --- | --- |
| `fn name(params) -> type { body }` | Lowered function (C and C++), also callable by `gen fn` code. |
| `gen fn name(params) -> type { body }` | Generation-time function; never lowered. |
| `const NAME: type = expr;` | Constant. `expr` is evaluated at generation time, so it can be anything the interpreter can compute; the value is emitted as a literal. |
| `extern "C" fn name(params) -> type;` | Declares an existing C function so lowered code may call it (`printf`, `kassert`, a tracing macro wrapper); also `extern "C" macro`, `extern "C++" fn`, `extern "C++" type` (section 4.6). |
| `method name(params) -> type { body }` | Interface method with a default body (section 7). |
| `header(c) { .. }` / `header(cxx) { .. }` / `header { .. }` | Raw text copied into the generated header of that language; no tag means both outputs (each backend emits it). The body is opaque and ends at a line with `}` in column 1. `code(..) { .. }` is the same for the implementation file. Allowed at top level and inside an interface. Tags are `c` and `cxx`; anything else is KIF090. |

`fn` and `gen fn` are exported to importing files like interfaces are; `import "x.kif";` and `use` bring them in (section 3.2).

### 3.1 Grammar (EBNF, additions)

```
item        = fn_item | gen_item | const_item | extern_item | impl_item | type_alias ;
fn_item     = [ "private" ] "fn" ident [ type_params ] "(" [ param { "," param } [ "," ] ] ")" [ "->" type ] block ;
gen_item    = "gen" fn_item ;
const_item  = "const" ident ":" type "=" expr ";" ;
impl_item   = "impl" [ ident "for" ] ident "{" { [ "private" ] fn_item | const_item } "}" ;   (* `impl S {}` or `impl iface for S {}` *)
extern_item = "extern" string ( "fn" | "macro" ) ident "(" [ param { "," param } [ "," "..." ] ] ")" [ "->" type ] ";"
            | "extern" string "type" path [ gen_params ] ";"
            | "extern" string "impl" [ gen_params ] path [ gen_params ] "{" { [ attrs ] fn_decl } "}"
            | "type" ident [ gen_params ] "=" type ";" ;
gen_params  = "<" gen_param { "," gen_param } ">" ;
gen_param   = ident [ "=" type ] | "const" ident ":" type [ "=" expr ] ;
type_params = "<" ident [ ":" bound { "+" bound } ] { "," ident [ ":" bound { "+" bound } ] } ">" ;
bound       = "integer" | "signed" | "unsigned" | "copy" | "cloneable" | "eq" | "ord" | "hash" | interface_name ;
param       = ident ":" type | "self" | "&" "self" | "&" "mut" "self" ;          (* the self forms only inside impl *)
postfix     = expr "?" ;                                                         (* early return of a result error *)
block       = "{" { stmt } [ expr ] "}" ;
stmt        = let | expr_stmt | item ;
let         = "let" [ "mut" ] ident [ ":" type ] [ "=" expr ] ";" ;
expr_stmt   = expr ";" | block_expr ;                 (* if/while/loop/for/match/unsafe/block need no ';' *)
block_expr  = if | while | loop | for | match | "unsafe" block | block ;
if          = "if" expr block [ "else" ( if | block ) ] ;
while       = "while" expr block ;
loop        = "loop" block ;
for         = "for" ident "in" expr block ;           (* expr: a range `a..b`/`a..=b`, a borrowed sequence (`&v`, `&mut v`, a slice, an array) or, in gen fn, an array *)
match       = "match" expr "{" { pattern "=>" ( expr "," | block ) } "}" ;
pattern     = int_lit | enum_path | "_" | pattern "|" pattern | int_lit "..=" int_lit ;
expr        = assign | binary | unary | cast | call | method_call | field | index | literal | path | block_expr
              | "return" [ expr ] | "break" | "continue" ;
```

Operator precedence and associativity are Rust's (unary `! - * &`, `as`, `* / %`, `+ -`, `<< >>`, `&`, `^`, `|`,
comparisons (non-associative), `&&`, `||`, ranges, assignment and compound assignment `+= -= *= /= %= &= |= ^= <<= >>=`).

### 3.2 Modules, `import` and `use`

Every file is a **module**. Its name is its `namespace a::b;` declaration, or the file stem when there is none, and its items
are addressed as `a::b::name`. Items are public by default; `private` hides an item from every other module (and, inside a
struct, from code outside its `impl`, section 4.7).

```kif
// util.kif -- module `util` (no namespace declaration: the file stem names it)
fn clamp(x: u32, hi: u32) -> u32 { if x > hi { hi } else { x } }
private fn helper() -> u32 { 7 }            // only visible inside util.kif

// main.kif
namespace app::main;                          // module name `app::main`
import "./util.kif";                         // makes the module reachable by path; binds only the name `util`
import "microfmt.kif";                       // library module: read from the sysroot (section 15)

use util::clamp;                             // brings one name in unqualified
use util::{clamp as limit, clamp};           // group form, `as` renames (the two bindings are different names)
use microfmt::*;                             // glob: every public item of the module, module path required

fn f() -> u32 { util::clamp(9, 4) + clamp(1, 2) }   // qualified and unqualified spellings both work
```

- **`import "path";`** loads a module. It binds the *module name* only: it never brings names in unqualified and is not
  re-exported by the importer (a module that needs `a` imports `a` itself). `./x.kif` and `../x.kif` are relative to the
  importing file; any other path is looked up in the sysroot (section 15). A file may be imported many times (once per
  importer, loaded once); a cycle is KIF012, as is a path that cannot be found.
- **`use path::name;`**, **`use path::{a, b as c};`**, **`use path::*;`** bind names unqualified in the current module.
  `use a::b;` where `a::b` is a module binds the module (so `b::name` works); `as` renames. The module in the path must be
  imported (KIFP010 otherwise, also for a name the module does not declare). A `private` item is KIFP016. Types and values
  live in separate tables and a `use` of a name that exists in both brings both.
- **Name clashes.** A module's own items and its `use` bindings share one scope: a clash is KIF010. A module's own items
  shadow the prelude silently (the prelude is the lowest priority, so you can declare your own `str` in a test file).
- **The prelude.** `reloco.kif` is the one **global** module: its names (`result`, `str`, `vector`, `ptr`, ...) are visible
  everywhere without `import`, `use` or a `reloco::` prefix (section 4.10). `microfmt.kif` is an ordinary module
  (`namespace microfmt;`) that must be imported (section 4.16). Built-in scalars (`bool char void i8..u64 usize isize`) are
  part of the compiler, not of any file.
- **Multi-segment type paths** (`util::point`, `microfmt::log::level`) resolve by the longest imported module-name prefix
  followed by one item name.
- **C++ spelling.** In a namespaced module the C++ name of an item defaults to `<namespace>::<name>`; an `extern "C++" type`
  keeps the path it was declared with (`#[cxx_name("..")]` overrides, section 4.6).
- **Precompiled modules.** `import` of `x.kif` also finds `x.kifm` (section 15); `use` works identically on both.

## 4. The shared (lowerable) subset

Everything in this section is allowed in `fn`, in method bodies and in `gen fn`, and means the same in all of them.

### 4.1 Types

| Type | C | C++ |
| --- | --- | --- |
| `i8 i16 i32 i64`, `u8 u16 u32 u64` | `int8_t` ... `uint64_t` | same, from `<cstdint>` |
| `isize usize` | `intptr_t` / `uintptr_t` | same; lint KIFL012 warns when used in a method signature |
| `bool` | `bool` (`_Bool`) | `bool` |
| `char` | `char` (byte) | `char` |
| `&[T]`, `&mut [T]` (slice) | `{ T *ptr; usize len }` (a `c_ptr` plus length pair) | `reloco::span<const T>` / `reloco::span<T>` |
| `[T; N]` (N a constant) | `T name[N]` (in structs and locals; parameters are `c_ptr<T>` plus a length) | `reloco::array<T, N>`; parameters are `reloco::span<T>` / `reloco::span<const T>` |
| `struct s { ... }` | `struct s` | `struct s` |
| `enum e : u8 { ... }` | the existing enum typedef | the existing `enum class` |
| `c_ptr<T>` / `c_cptr<T>` | `T *` / `const T *` | `T *` / `const T *` inside `RELOCO_BEGIN_UNSAFE_CODE` regions only (section 9.2) |
| `fn(args) -> ret` | a function pointer typedef | `ret (*)(args)` |
| `()` (unit) | `void` | `void` |

Tuples `(T, U)` are checked values: `(1, true)`, field access `t.0`, `let (a, mut b) = t;` and tuple patterns in `match`
(exhaustiveness is checked per combination, KIFP030; arity mismatches are KIFP012, `t.5` out of range is KIFP010).
They are `#[copy]`-like only if every element is copy.

```kif
fn swap(t: (u32, u32)) -> (u32, u32) {
    let (a, b) = t;          // destructure; `mut` allowed per name
    (b, a)
}
```

There is no `Vec`, `Option`, generation-time string value in lowered code (`str`, `str_view` and `cstr` exist, with the meaning of 4.14). Types are static and explicit across calls;
`let` infers from the initialiser. **There are no implicit numeric conversions**, only `as` (section 4.3), except that an
unsuffixed integer literal takes the type its context needs, and is an error if it does not fit.

### 4.2 Values, ownership and mutation

- Variables are immutable unless `let mut`. Assigning to an immutable variable is KIFP020.
- Scalars, enums and `#[copy]` structs are **values** (copied on assignment and on passing). **Structs are move-only by
  default** (like Rust): assigning or passing one moves it, and using the moved-from variable is KIFP023. `#[copy]` is
  allowed only on plain-data structs (no `impl` with `drop`, only scalar, enum, `#[copy]`-struct and array-of-those fields). Arrays
  follow their element type. A parameter that must be changed by
  the callee is passed as `&mut T` (lowered to `T *` in C, `T &` in C++). `&T` is a read-only reference (`const T *` /
  `const T &`). References may be returned and stored in structs when their lifetime is named (see "Lifetimes" below).
- **Lifetimes.** A lifetime is written `'a` (`'static` and `'_` are built in) and declared like a generic parameter, before
  the type parameters: `fn first<'a, T>(v: &'a vector<T>) -> &'a T`, `struct window<'a> { buf: &'a mut u8 }`,
  `impl<'a> window<'a> { .. }`; `'b: 'a` says `'b` outlives `'a`. Types take them as arguments: `window<'a>`, or `window<'_>`
  to elide. In `fn` signatures they may be elided as in Rust: every elided parameter lifetime is a fresh one, and an elided
  *result* lifetime is that of `&self`/`&mut self`, else of the only lifetime among the parameters (KIFP162 when there is none
  or several). Struct fields never elide (KIFP161); an undeclared name is KIFP160. A reference that outlives its referent is
  rejected by the body checker (a later step). Lowering: `&'a T` is a plain `const T &`/`T &`, and the lifetime becomes
  `RELOCO_LIFETIMEBOUND` on the result (or `RELOCO_LIFETIME_CAPTURE_BY_THIS` for a stored reference) in C++; the C
  backend drops it. Allocators are persistent: an `allocator_ref` (and the context behind it) lives for the whole program, so it
  is `'static` and neither carries nor needs a lifetime; a stateful allocator context must be a static or otherwise
  program-lifetime object (the C++ constructor's `RELOCO_LIFETIMEBOUND` still checks that). `str_view` stays out of struct fields (KIFP122); a borrowed field is `&'a str`.
- **Slices.** `&[T]` and `&mut [T]` are borrowed views of contiguous elements (the lifetime rules above apply:
  `&'a [T]`). There is no separate `span` type: slicing is the span, and it lowers to `reloco::span` (a `{ptr, len}` pair in
  C), so it is also what an `extern` function takes for a `reloco::span` parameter. Arrays `[T; N]`, `vector<T>`, `str`,
  `str_view` and slices all borrow as a slice: `v[a..b]`, `v[..]`, or `v.as_slice()` / `v.as_mut_slice()`. Slicing is bounds
  checked (the `checked` tier; `try_slice(a, b)` returns `result`). A run of `char` (`[char; N]`, `vector<char>`, `&[char]`)
  also borrows as `str_view` with `as_str_view()` for reloco calls; it is **not** a `cstr` (no NUL guarantee). A `&[T]` is
  never a struct field without a named lifetime (KIFP161) and, like `str_view`, cannot cross a C-visible interface method
  unless the target is C++ (KIFP122/KIFP053).
- A local must be definitely assigned before use (KIFP022); lowering initialises nothing implicitly.
- A struct is plain data unless it has an `impl` block (4.7), which gives it methods, constructors and a destructor.

### 4.3 Operators and integer semantics (identical in interpreter, C and C++)

Integer behaviour is **fully defined**, so no backend inherits C's undefined behaviour:

| Operation | Semantics | Lowering |
| --- | --- | --- |
| `+ - *` | wrap modulo 2^N (two's complement), signed and unsigned (always; checked or saturating arithmetic uses the classes above) | unsigned types: plain operators. Signed types: computed in the unsigned type and converted back (`KIF_ADD_I32(a, b)` and friends, emitted into the generated C per file, or `kif/proc.h` in C++) |
| `/ %` | divisor 0 or `MIN / -1` is a **trap** (below) | `KIF_DIV_I32(a, b)` checks, then divides |
| `<< >>` | shift count >= bit width is a trap; `>>` is arithmetic for signed, logical for unsigned | `KIF_SHL_U32(a, n)` checks the count |
| `!x` | bitwise not for integers, logical not for `bool` | `~x` / `!x` |
| `-x` | wrapping negation | `KIF_NEG_I32(x)` |
| `&& ||` | short circuit, `bool` only | same |
| `== != < <= > >=` | same-type operands only | same |
| `a as T` | integer to integer: truncate or sign/zero-extend as C does for conversions *after* wrapping; integer to `bool` is not allowed (write `a != 0`); `bool` to integer gives 0/1; enum to integer and `u8` to enum of that underlying type are allowed only when the value is a declared enumerator (else a trap) | casts |
| `a[i]` | `i < N` or a trap; `i` is `usize` | `KIF_INDEX(a, i, N)` |

A **trap** is a runtime failure that panics with a message and a source location (`"integer division by zero"`,
`"index out of bounds"`, ...). Programs raise their own with the built-in `panic!("message")`, which has the never type
(it coerces to any type, so it can end a branch of `match` or `if`); `unreachable!()` is `panic!("unreachable")`.

- in the interpreter a panic aborts the generation with an error (KIFP100);
- in C it calls `KIF_PANIC(file, line, "message")`, which the including code defines (default: `__builtin_trap()`; in a
  kernel, `panic()`);
- in C++ `KIF_PANIC(file, line, "message")` is defined by `kif/proc.h` as the relevant reloco call: `RELOCO_ASSERT_MSG_LOC(false,
  file, line, message)` in builds with assertions, which reaches reloco's assert handler or `RELOCO_KERNEL_PANIC`, and
  `RELOCO_TRAP()` when assertions are compiled out, so hardened containers and kif code report through the same hook.

`kif/proc.h` is the C++ side only. Generated C includes `kif_c.h`, which kifc writes from the target (`kifc --c-common DIR`); per-type code is generated into
each file (`docs/kif-c-backends.md` 3.0).

**Other arithmetic is a different type, not a different operator.** Plain integers always wrap. Code that needs saturation or
overflow tracking uses the prelude classes (4.10): `wrapping<T>` (explicit wrapping, same behaviour as plain integers),
`saturating<T>` (clamps), `checked_value<T>` (remembers overflow, must be consumed). They are reloco's types
(`reloco::wrapping`, `saturating`, `checked_value`) with their operators; there is no `checked_add`-style operator or built-in.

### 4.4 Statements and control flow

`let`, assignment, `if`/`else`, `while`, `loop`, `for i in a..b` and `a..=b`, and `for x in &v` / `&mut v` / a slice (`x` is `&T` or `&mut T`; a vector is only iterated through a borrow),
`match` on integers and enums, `break`, `continue`, `return`, blocks as expressions.

- `match` must be exhaustive (KIFP030); an integer match needs a `_` arm. It lowers to `switch` in C and C++, and
  adjacent `|` patterns to fall-through-free grouped cases.
- Every path of a non-unit function returns a value (KIFP031).
- Recursion is **not allowed** in `fn` (direct or mutual; KIFP032): stack depth in a kernel must be a build-time fact.
  `gen fn` may recurse (depth limit, section 8).
- No `goto`, labels, `switch` fall-through, pointer arithmetic or unions.

`match` arms may have a guard and bind names: `n if n > 10 => ..`, `k @ 1..=5 => ..`, `x @ (a | b) => ..`. A lone lowercase name
that is not a known constant is a binding. The guard must be `bool` (KIFP012); a guarded arm does not count toward exhaustiveness
(KIFP030), and a name binds by copy.

### 4.5 Structs and enums declared in kif

The `struct` and `enum` declarations of kif (docs/kif-language.md section 4.2, also from imported files) are the data types of
the procedural language, in `fn`, method bodies, hooks and `gen fn`. Nothing is redeclared: the same declaration gives the
C type, the C++ type and the type seen by the interpreter, so values cross the boundary without conversion.

```kif
enum state : u8 { idle = 0, busy = 1, failed = 2 }

#[flags]
enum perm : u8 { none = 0, read = 1, write = 2, exec = 4 }

struct request { id: u32, flags: perm, st: state, payload: [u8; 16] }

// A struct is a value: passed by value, or by `&`/`&mut` when the callee reads/changes it.
fn mark_failed(r: &mut request) {
    r.st = state::failed;                       // enumerator path `enum::name`; a field of a `&mut` is assignable
    r.flags = r.flags & !perm::write;           // flags enums support | & ^ ! (same enum on both sides)
}

fn make(id: u32) -> request {
    request {                                   // struct literal: every field is given, in any order
        id: id,
        flags: perm::read | perm::write,
        st: state::idle,
        payload: [0; 16],                       // array literal `[value; N]` or `[a, b, c]`
    }
}

fn describe(s: state) -> u32 {
    match s {                                   // exhaustive over the declared enumerators, no `_` needed
        state::idle => 0,
        state::busy => 1,
        state::failed => 2,
    }
}

fn touched(r: &request) -> bool {
    has_flag(r.flags, perm::read) && r.payload[0] != 0   // `payload[0]` is bounds-checked
}
```

Rules:

- **Construction and update.** A struct value is built with a literal that names every field (KIFP014 for a missing or
  unknown one) or with `..base` to copy the rest from another value: `request { id: 7, ..make(0) }`. Fields are
  assigned only through a `mut` binding or a `&mut` reference. Methods, constructors and the destructor are declared in `impl` blocks (4.7).
- **Nesting.** Struct fields may be scalars, enums, arrays and other structs (declared earlier or imported; a cycle is the
  existing struct error). `a.b.c[i].d` works and every index is checked.
- **Enums.** `e::name` is the only way to write an enumerator. `match` is exhaustive over the enumerators (KIFP030).
  Conversions are explicit: `e as u8` yields the underlying value; `u8 as state` is allowed and traps if the value is not
  an enumerator (4.3); for a `#[flags]` enum the cast is allowed for any value made only of the enum's bits.
  `state::from_value(x)` returns `optional<state>` in lowered code (`Option<state>` in `gen fn`), for callers who
  must not trap.
- **Flags.** `#[flags]` enums add `|`, `&`, `^`, `!` (complement within the enum's bits), `has_flag(v, f)`, `set_flag` and
  `clear_flag` as built-ins. Mixing two different enums is KIFP012.
- **Names.** The generated `NAME_name()` (C) and the `name_of`/formatter (C++) of the enum add-ons are callable from bodies
  as `name_of(v)` (returns `c_cptr<char>` in lowered code, `str` in `gen fn`), which is what tracing hooks use.
- **Layout.** A struct or enum used in a body has exactly the layout the declaration already generates (the ABI manifest
  covers it), so pointers to it can be passed to `extern "C"` functions and received from the kernel as `c_ptr<request>`.
- **C++.** Structs and enums are the generated C++ types; the lowered code uses their members directly. Enum arithmetic goes
  through the generated flag operators, so no raw integer casts appear.
- **Generation time.** In `gen fn` the same values exist (the interpreter represents them structurally), plus reflection
  (`Struct`, `Enum`, section 6.1), so a generator can both inspect a declaration and compute with its values.
- **Lint.** Passing a struct by value in a *method signature* stays KIFL013; inside `fn` bodies by-value passing is fine, and an
  off-by-default rule (planned KIFL031, "large struct by value in a lowered fn", size limit configurable in `.kiflint`)
  can flag big ones.

### 4.6 Pointers, `unsafe` and C/C++ interoperation (FFI)

Lowered code is ordinary kernel code, so it must be able to use the rest of the kernel: call existing C and C++ functions,
use their types, be called by them, and do the unchecked things (registers, raw memory) that drivers do. All of that is
allowed, but **visible**: it is either a declared interface (`extern`, `export`) or inside an `unsafe` block. Nothing here
exists at generation time (section 5): `gen fn` cannot call foreign code.

#### Calling C

```kif
// Declares an existing C function. kif checks calls against this signature; the header is added to the includes of the
// generated file, so the C compiler checks it again against the real prototype.
#[header("sys/systm.h")]
extern "C" fn kassert_fail(msg: c_cptr<char>) -> ();

// Variadic C functions: the fixed parameters are typed, the rest follow the C default promotions. Calling one is `unsafe`.
#[header("sys/systm.h")]
extern "C" fn printf(fmt: c_cptr<char>, ...) -> i32;

// A C macro or an inline asm sequence is wrapped in a C function by the user; kif cannot call macros. If wrapping is
// impractical, `extern "C" macro` declares a function-like macro: arguments are evaluated once, as in a function call
// (kif copies them to locals first), and the result type is the declared one. The header must define it.
#[header("sys/systm.h")]
extern "C" macro curthread_id() -> u32;

fn first_byte(p: c_cptr<u8>, len: usize) -> u8 {
    if p == null || len == 0 { return 0; }                // `null` is the only pointer literal
    unsafe { *p }                                          // the one unchecked operation
}

fn log_value(v: u32) {
    unsafe { printf("value=%u\n", v); }                    // `unsafe`: variadic C, unchecked format
}
```

- `extern "C" fn` / `extern "C" macro` declare a name, signature and, optionally, headers (`#[header("...")]`, repeatable)
  and a C symbol different from the kif name (`#[link_name("kern_printf")]`).
- Calling an extern function is safe when every parameter and the result are plain data (integers, `bool`, `char`,
  enums, kif structs by value, `fn` pointers to other externs/`fn`s) **and** the declaration is marked `#[safe]` by the
  author. Otherwise a call needs `unsafe` (KIFP070). The default is "needs `unsafe`", so declaring is never an accident.
- Types cross the boundary with the layout the declarations already generate. `c_ptr<T>` is `T *`; a pointer to a kif struct
  or enum is a normal C pointer (ABI manifest covers the layout).

#### Calling C++

```kif
// A C++ function, member or static member: declared with its C++ spelling. Only meaningful for the C++ backend; the C
// backend refuses a file that calls it (KIFP051) unless the call sits in a `cfg(cxx)` block.
#[header("net/packet.hpp")]
extern "C++" fn net::packet_size(p: &net::packet) -> usize;

// A C++ type is opaque to kif: name it so it can be passed by reference or pointer; kif never looks inside.
extern "C++" type net::packet;
```

- `extern "C++" fn` may name a namespace-qualified function or a static member; the spelling after `fn` is pasted into the
  C++ output, parameters of kif types use the generated C++ types, `&T`/`&mut T` map to `const T &`/`T &`.
- `extern "C++" type X;` declares an opaque type (without a body; with a body it is a wrapped type, 4.10.1) usable behind `&`, `&mut`, `c_ptr<>` and `c_cptr<>` only (no by-value use, no
  field access).
- Templates are not instantiated by kif: pass the concrete spelling (`extern "C++" fn reloco::min<u32>(a: u32, b: u32) -> u32`).
- `cfg(cxx)` / `cfg(c)` blocks pick the backend a statement is emitted for, for the rare body that must differ:
  `cfg(cxx) { use_c++_thing(); } cfg(c) { use_c_thing(); }`. A body with a `cfg` is excluded from differential tests
  that need both backends to agree (section 12), and linted (planned KIFL032).

#### Being called from C and C++

```kif
// `#[export]` fixes the C symbol and ABI of a lowered fn, so C and C++ can call it by name. Without it the function is
// `static inline` (C) / an inline function (C++) and its name is derived from the kif name (section 9).
#[export, link_name("my_driver_checksum")]
fn checksum(p: c_cptr<u8>, len: usize) -> u32 { ... }

// A function pointer type usable as a callback parameter; the lowered fn passed in must have exactly this signature.
fn each(items: c_ptr<request>, n: usize, cb: fn(&request) -> ()) { ... }
```

- `#[export]` functions are emitted with external linkage in the generated `.c` / `.cpp` and declared in the header; for the
  C++ backend they get `extern "C"` linkage unless `#[export(cxx)]` is given (then they follow C++ mangling).
- `fn` pointers (`fn(args) -> ret`) are C function pointers. Only `fn`s and `extern` functions can be converted to them; there
  are no capturing closures and no captured state, so the ABI is exactly C's.
- **Lambdas.** `|a: T, b: U| expr` (or `|a| -> R { block }`, `|| expr`) is a *non-capturing* function value: it may use only its
  parameters, constants and `fn` items, never locals of the enclosing function (KIFP170, checked with the bodies). It
  coerces to a matching `fn(..)` pointer and lowers to a `static` function (C) or a captureless lambda (C++), so the ABI stays
  C's. Parameter types may be omitted when the target `fn(..)` type fixes them. The syntax is parsed now; capturing
  lambdas (borrowed into `fn_ref`) are a later step.

```kif
// `|r| ()` takes the parameter type from `cb`'s `fn(&request) -> ()`; it uses nothing from `each`'s locals.
fn walk(items: c_ptr<request>, n: usize) { unsafe { each(items, n, |r| ()); } }
```
- Interface methods with kif bodies (7.1) are called as before through the generated wrappers, from C or C++.

#### Raw memory and hardware

- `c_ptr<T>`/`c_cptr<T>` support `==`, `!=`, `null`, `*p`, `*p = v`, `p.field`, `&x`, `&mut x`, and `as` to and from `usize`.
- Pointer arithmetic and indexing are explicit unsafe built-ins: `ptr_offset(p, n)` (`p + n`), `ptr_index(p, i)` (`p[i]`),
  `ptr_diff(a, b)`, `ptr_cast<U>(p)`; all need `unsafe`.
- Volatile and atomic access are built-ins, not qualifiers: `volatile_read(p)`, `volatile_write(p, v)`, `atomic_load`,
  `atomic_store`, `atomic_add`, `atomic_cas` with an ordering argument (`ordering::acquire`...). They lower to the
  corresponding C11 atomics / `reloco` atomic helpers and `volatile` accesses.
- `unsafe` blocks lower to `RELOCO_BEGIN_UNSAFE_CODE` / `RELOCO_END_UNSAFE_CODE` in C++ (section 9.2) and to a plain block
  with a comment in C.
- Inline assembly and compiler intrinsics are not part of the language: write a small C or C++ function (or macro with
  `extern "C" macro`) and call it.
- A pointer is never dereferenced at generation time (the interpreter has no memory): `*p` in a `gen fn` evaluation is a
  trap (KIFP101), so only pure `fn`s (no `unsafe`, no `extern`) may be called from `gen fn` code and `const` (KIFP052).

### 4.7 Structs with methods, constructors and ownership

A struct gets behaviour from `impl` blocks, in the Rust style. This is what makes it a real C++ class in the primary backend.

```kif
// Everything is public by default (fields, methods, associated functions). `private` is an optional modifier that limits
// a field or method to the `impl` blocks of its struct. A field default is what a *shell* (below) and a *moved-from*
// value look like, so a struct with a `drop` or a `try_construct` gives its scalar, pointer and enum fields defaults.
// Fields may also be other objects (`vector<u8>`, `ptr<T>`, structs with an `impl`), as in Rust.
struct ring {
    private buf: c_ptr<u8> = null,
    private cap: usize = 0,
    private head: usize = 0,
    count: usize = 0,                           // public: readable and writable from outside
    private alloc: allocator_ref = allocator_ref::none(),
    log: vector<u8>,                            // object field: destroyed by the generated destructor after `drop`
}

impl ring {
    // ---- construction: reloco's fallible patterns; there are no throwing constructors ------------------------------
    // `try_create`: factory using the default allocator. Lowers to `static reloco::result<ring> try_create(usize) noexcept`.
    fn try_create(cap: usize) -> result<Self> {
        Self::try_allocate(default_allocator(), cap)
    }

    // `try_allocate`: factory with an explicit allocator. `?` returns early with the error (as in Rust).
    fn try_allocate(alloc: allocator_ref, cap: usize) -> result<Self> {
        if cap == 0 || cap > 4096 { return err(error::invalid_argument); }
        let buf = alloc.try_allocate_bytes(cap)?;        // result<c_ptr<u8>>; `?` unwraps or returns the error
        ok(Self { buf: buf, cap: cap, head: 0, count: 0, alloc: alloc })   // `Self { .. }` literal: only inside the impl
    }

    // `try_construct`: two-phase, in place. The shell (every field at its default) already exists; this fills it.
    // Lowers to `reloco::result<void> try_construct(usize) noexcept` and the shell constructor is generated `noexcept`.
    fn try_construct(&mut self, cap: usize) -> result<()> {
        self.buf = self.alloc.try_allocate_bytes(cap)?;
        self.cap = cap;
        ok(())
    }

    // `try_clone`: independent copy that can fail; `&self` makes it a const member. (A struct is not copyable otherwise.)
    fn try_clone(&self) -> result<Self> {
        let mut c = Self::try_allocate(self.alloc, self.cap)?;
        // ... copy the bytes ...
        ok(c)
    }

    // ---- destructor ---------------------------------------------------------------------------------------------
    // `drop` runs when a value goes out of scope, unless it was moved. It must be safe on a default-state value
    // (a moved-from one is reset to the field defaults).
    fn drop(&mut self) {
        if self.buf != null { self.alloc.free_bytes(self.buf); }
    }

    // ---- methods: `self` forms ---------------------------------------------------------------------------------
    fn is_empty(&self) -> bool { self.count == 0 }             // const member
    fn push(&mut self, b: u8) -> result<()> { ... }              // non-const member
    fn into_raw(self) -> c_ptr<u8> { ... }                         // consumes the value: `&&`-qualified member
}
```

**Rules**

- **Constructors.** A function in an `impl` named `try_create`, `try_allocate`, `try_construct` or `try_clone` (or the `_at`
  form `try_clone_at`) follows reloco's protocol (`docs/fallible-construction.md`) and is lowered to exactly that member or
  static member, so the type satisfies `has_try_create_v` / `has_try_allocate_v` / `has_try_construct_v` / `has_try_clone_v`
  and composes with `reloco::unique_ptr<T>::try_create`, `reloco::construction_helpers` and containers. Their result types
  are checked: `try_create`/`try_allocate`/`try_clone` return `result<Self>`, `try_construct` takes `&mut self` and returns
  `result<()>`. Any other `fn` without `self` is an ordinary associated function (`Self::name(..)`, a static member).
- **No exceptions, no partial objects.** Fallible work is never done in a constructor. The only constructors generated are
  the `noexcept` shell (all fields at their defaults; present only when every field has a default), the private
  field-wise constructor used by `Self { .. }`, and the move constructor.
- **`result<T>`, `?`, `ok`, `err`.** `result<T>` is `reloco::result<T>` (`reloco::expected<T, reloco::error>`); `error::..` is
  reloco's one error enum. `e?` on a `result<U>` evaluates to the value or returns the error from the enclosing function
  (whose result must be a `result`). `ok(v)` / `err(e)` build one. `optional<T>` is `reloco::optional<T>`.
  `some(x)` / `none()` build an `optional<T>` in the same way (prelude functions, not keywords); `T` comes from the expected type
  (annotation, parameter, return type), and a `none()` with no expected type is KIFP012:

  ```kif
  // `pick` returns an optional: `some(v)` holds a value, `none()` is empty. The element type is `u32`, from the return type.
  fn pick(x: u32) -> optional<u32> { if x > 1 { some(x * 2) } else { none() } }
  fn use_it() -> u32 {
      let o: optional<u32> = pick(3);          // annotation names the element type
      if o.has_value() { *o.value() } else { 0 }   // `value()` panics when empty; `value_or(d)` supplies a default
  }
  ```

  An optional that owns something (`optional<str>`, `optional<vector<T>>`) is dropped with its payload (4.2, and
  `docs/kif-lifetime-and-construction.md`). C lowering and its current limits: `docs/kif-c-backends.md` 3.1.
- **Allocation.** `allocator_ref`, `default_allocator()` and the allocator methods are reloco's, declared by the
  `reloco.kif` prelude that ships with kif (section 4.10). `ptr<T>` is
  `reloco::unique_ptr<T>`: `ptr<ring>::try_allocate(alloc, 64)` is the heap form of the same construction protocol, and
  `*o`/`o.field` dereference it. `vector<T>` is `reloco::vector<T>` with its fallible `try_` operations. Nothing is allocated implicitly.
- **Move-only; pass by reference or move.** In C++ a struct value is either **borrowed** (`&T` / `&mut T`, a C++ reference,
  the common case) or **moved** (passed, returned or assigned by value). There is no implicit copy: the generated class has
  `noexcept` move construction and assignment and **deleted copy** operations. Generated moves are member-wise, so object
  fields (`vector<u8>`, `ptr<T>`, other structs) move with their own move operations; when the struct has a user `drop`, its
  scalar, pointer and enum fields are additionally reset to their defaults in the source, so a moved-from value is a valid
  default-state value whose `drop` is a no-op. kif rejects use after move at compile time (KIFP023), moving out of a reference
  or a field of a borrowed value (KIFP024), and a partial move out of a struct that has a `drop` (KIFP024). `#[copy]`
  plain-data structs are copied as before. A copy of anything else is only ever the explicit `try_clone`.
- **Moved-from state exists only in C++.** The interpreter and C have no moved-from values (C structs are trivial, `gen fn`
  values are never moved; C tracks a move with a drop flag so the source is not dropped). Every type used as a field must be movable (KIFP029 when it is not); prelude types are, by
  reloco's contract. kif's checker rejects use after move statically (KIFP023). Where it cannot prove the use safe (a
  value moved on one path, a moved-from `optional<T>` or `ptr<T>` read through a field or a loop), the generated C++
  keeps reloco's own runtime check: reading the value of a moved-from `optional<T>`, dereferencing a moved-from
  `ptr<T>` or indexing a moved-from `vector<T>` **panics** through `KIF_PANIC`/`RELOCO_TRAP` rather than being
  undefined behaviour. The static check is an early diagnostic; the runtime check is the guarantee.
- **Destructor.** `fn drop(&mut self)` becomes `~ring() noexcept`; object fields are destroyed after it runs, in reverse
  declaration order. A struct with no `drop` still destroys its fields. Inside `drop` the fields may be read and cleared; the
  struct cannot be moved from. A user `drop` must tolerate the default state.
- **Relocatability.** By default a generated class makes **no** relocatability claim. `#[unsafe(relocatable)]` on a struct
  emits `template <> struct reloco::is_trivially_relocatable<S> : std::true_type {};` (reloco's customisation point,
  `relocatable.hpp`), so reloco containers may move it with `memcpy`. kif does not verify the claim: it is the author's
  responsibility that the object has no self-references, no registered address and no identity (the `unsafe(..)` spelling
  makes that visible in review, and the attribute is accepted only where unsafe code is allowed). Without the attribute
  nothing is inferred from the fields. Using it on a `#[heap]` struct (4.11), which is never moved, or on a struct that
  implements an interface (4.8), whose identity is its address, is KIFP085. C and `gen fn` ignore the attribute.
- **Methods and qualifiers.** `&self` → `const` member; `&mut self` → non-const member with an lvalue (`&`) qualifier and a
  deleted `&&` overload (reloco's rvalue-safety rule for accessors that alias the object); `self` → `&&`-qualified, consuming.
  Methods returning references/pointers into the object carry `RELOCO_LIFETIMEBOUND`; the constructors, `drop` and the
  consuming methods are annotated for the checker as in `docs/lifetime-safety.md`. All members are `noexcept` and
  `[[nodiscard]]` when they return a value; a `result`-returning method is always `[[nodiscard]]`.
- **Visibility.** Everything is public by default. `private` on a field, method or associated function limits it to the `impl`
  blocks of that struct; using one elsewhere is KIFP015, as is a struct literal that must name a `private` field outside the
  `impl`. In C++ these are `private:` members.
- **Static data.** `impl` blocks may contain `const NAME: T = expr;`, emitted as `static constexpr`.
- **Several `impl` blocks** for one struct are allowed (all merge into one class); a method name may appear once (KIFP027).
- **Layout.** A struct with an `impl`, an object field or an interface implementation is a C++ class. A struct with none of
  those is the plain-data struct of 4.5 and is also usable from C. `#[repr(c)]` is allowed only on plain-data structs.
- **Generation time.** Structs with an `impl` exist in `gen fn` only as reflected declarations (`Struct`, with `.methods()`);
  they cannot be constructed or have methods called there (KIFP052).

### 4.8 Structs implementing interfaces

A struct implements one or more interfaces with `impl <interface> for <struct>`. The generator emits a complete C++ class
with the interface's method table, built with the existing kobj runtime (`kif/kobj/kobj.hpp`). This describes a `#[kobj]`
interface; for a plain interface (4.15) the class only satisfies the interface's concept and has no table or `make_class`.

```kif
#[kobj, version(1)]
interface clock {
    method gettime(ts: c_ptr<timespec>) -> i32;
    method resolution() -> u64 { 1 }                     // has a default body (7.1): the struct need not implement it
}

struct hpet {
    base: u64 = 0,
    ticks: u64 = 0,
}

impl hpet {
    fn try_create(base: u64) -> result<Self> { ok(Self { base: base, ticks: 0 }) }
}

// Implements `clock`: every method without a default must be given, with exactly the interface's signature
// (the `self` of the interface becomes the receiver: `&self` or `&mut self`, as the interface method's signature allows).
impl clock for hpet {
    fn gettime(&self, ts: c_ptr<timespec>) -> i32 {
        unsafe { read_hpet(self.base, ts) }
    }
}
```

- **Generated class.** The struct becomes `class hpet`, deriving nothing by default, with the members from all its `impl`
  blocks, plus for each implemented interface: a `static inline const auto klass` built with
  `kif::kobj::make_class<hpet>(method<clock_if::gettime, &hpet::gettime>, ...)` (inherited and overridden tags follow
  `canonical`), and an accessor `as_clock()` (shape per `docs/kobj-runtime.md`) returning a `handle` to the object with
  that class, which is how callers invoke the interface. A `static_assert` checks the implemented tags against the
  interface tags, like the existing `foreign class` assertions do.
- **Construction** is the same reloco-style protocol of 4.7; the interface adds nothing to it. The class is move-only and has the
  deleted copy of 4.7, so a `handle` to it is lifetime-bound to the object (`RELOCO_LIFETIMEBOUND` on `as_clock()`).
- **Extends** is written `#[extends(base)] interface derived { .. }` (single base, which may extend another). The methods of
  every base are part of the interface: callable on implementers and through a `T: derived` bound (a `T: base` bound is also
  satisfied by an implementer of `derived`). Unknown base: KIFP010; a loop: KIFP142. Implementing an interface that `extends` another requires (and registers) the base's methods too; a base
  method implemented in the same `impl` block is allowed: `impl clock for hpet { fn ... }` may define methods of `clock`'s bases.
- **Several interfaces** are several `impl X for S` blocks. Two interfaces that declare a method with the same name need the
  qualified form `fn clock::resolution(&self) -> u64` (KIFP026 otherwise).
- **Foreign classes.** `#[foreign(name)]` on the struct names a `foreign class` (docs/kif-language.md 3.4); the generated class
  then starts with that driver's softc at offset 0 and `klass` chains to the C driver, as the existing
  foreign-class support does. The struct fields come after the softc.
- **Errors.** Missing method without a default, a signature that does not match, an unknown method, or an unknown interface:
  KIFP026. `impl <interface> for` a `#[copy]` struct: KIFP028 (an implementer must be move-only: its identity is its address).
- **Mocks and docs** treat an implementing struct as an ordinary class; nothing in the ABI manifest changes (interfaces are
  the ABI; implementers are not).

### 4.9 Generic functions

Free `fn`s (not methods, structs or impls) may be generic, **in C++ output only** (KIFP053 in C). Type parameters have bounds from a fixed set (integer, signed, unsigned, copy, cloneable, eq, ord, hash) plus interface names; user-defined bounds do not exist:

```kif
// `T: integer` accepts any integer type; the call site picks T by inference or by turbofish `max::<u32>(a, b)`.
// Bounds: integer, signed, unsigned, copy, cloneable, eq, ord, hash, or the name of an interface (T then has its methods).
fn max<T: ord>(a: T, b: T) -> T { if a > b { a } else { b } }

fn clamp_add<T: unsigned>(a: T, b: T, hi: T) -> T { let s: T = a + b; if s > hi { hi } else { s } }  // wraps, then clamps
```

- The body is checked **once, at the definition**, against the bounds (like Rust, not per instantiation): `+ - * / % & | ^ << >>`
  and `!` need `integer`, `signed` or `unsigned` (unary `-` needs `signed`), `< <= > >=` need `ord` (or an integer bound), `==` and
  `!=` need `eq` (or `ord`, or an integer bound), and `x.m()` must be a method of a bounding interface (`cloneable` gives
  `try_clone`); anything else is KIFP064. A parameter without `copy` (integer bounds imply it) is moved when used by value.
  Calls infer the type arguments from the arguments (or the turbofish); an untyped literal takes the type the expected result
  asks for. The arguments are checked against the bounds (KIFP062; a generic caller's own bounds must imply them) and a
  parameter that appears only in the result and has no turbofish is KIFP063. The interpreter runs generic bodies with the
  instantiated types, so wrapping follows the argument type.
- C++ output is a function template constrained with a C++20 concept per bound (`reloco::integral`-style, or the interface
  concept). There is no C lowering: a file that is lowered to C and uses a generic `fn` fails with KIFP053.
- In `gen fn` generics are not needed (values are dynamic); a `gen fn` may call a generic `fn` only with concrete types.

### 4.10 The `reloco.kif` prelude

`reloco.kif` ships with kif and declares reloco types as `extern "C++"` generic types, so code names them like built-ins:

| kif | C++ | Notes |
| --- | --- | --- |
| `allocator_ref`, `default_allocator()` | `reloco::allocator_ref`, ... | construction protocol (4.7) |
| `result<T>` | `reloco::result<T>` | language item: `?`, `ok`, `err` |
| `ptr<T>` | `reloco::unique_ptr<T>` | owning, unique; `try_allocate`, deref |
| `arc<T>` | `reloco::shared_ptr<T>` | shared, atomic count |
| `rc<T>` | `reloco::rc<T>` | shared, non-atomic count (not `Send`/`Sync`) |
| `weak<T>` | `reloco::weak_ptr<T>` | non-owning observer of an `arc<T>` |
| `weak_rc<T>` | `reloco::weak_rc<T>` | non-owning observer of an `rc<T>` |
| `optional<T>` | `reloco::optional<T>` | |
| `vector<T>` | `reloco::vector<T>` | fallible `try_` operations |
| `wrapping<T>`, `saturating<T>`, `checked_value<T>` | reloco's classes of the same name | alternative arithmetic (4.3) |

The prelude marks `result` with `#[lang(result)]`. Prelude types do not exist in C (KIFP053) or in `gen fn`. The developer
adds the real reloco includes; kif never parses headers.

#### 4.10.1 Declaring wrapped C++ types

The prelude is written in the same language any project can use to wrap its own C++ types. Two features make that possible.

**Generic parameters on `extern "C++" type`**: several type parameters, defaults and non-type (constant) parameters.

```kif
// Type parameters, a default, and a constant parameter. kif never instantiates the template; it only pastes the
// spelling and checks uses against this declaration. `usize` parameters become `std::size_t` template arguments.
extern "C++" type reloco::flat_map<K, V, A = allocator_ref>;
extern "C++" type reloco::inline_vector<T, const N: usize>;
extern "C++" type reloco::expected<T, E = error>;       // `result<T>` is an alias of this with the default error

// Aliases give a short kif name; they add nothing at runtime.
type result<T> = reloco::expected<T, error>;
```

- Parameters: `<T, U = D, const N: usize>` (types, optional default, constant integers or `bool`). A use supplies all non-defaulted
  arguments (`flat_map<u32, str>`, `inline_vector<u8, 16>`); too few or too many is KIFP130. A constant argument must be a
  constant expression (a literal or a `const`).
- An argument is checked only for being a type (or a constant of the declared type). Template constraints stay C++'s: the
  C++ compiler reports a violated one (kif does not read headers).
- A `type` alias may be generic. It is expanded by the checker and lowered to the aliased spelling.
- `extern "C++" type` is the only place generic types are introduced (no generic structs).

**Methods and annotations on extern types**, declared in an `impl` block with the `extern "C++"` marker:

```kif
// Declares what kif code may call. Each entry is a real C++ member; kif pastes `name`, so overloads stay C++ overloads
// only if listed separately. Receivers, lifetimes and tiers are explicit so kif's checker can enforce the same rules as
// for its own structs.
extern "C++" impl<K, V, A> reloco::flat_map<K, V, A> {
    // `&mut self`: lvalue-qualified member. `&self`: const member. `self`: `&&`-qualified, consumes the value.
    fn try_insert(&mut self, key: K, value: V) -> result<()>;          // fallible tier: returns result
    fn get(&self, key: &K) -> optional<&V>;                            // result borrows from `self` (lifetimebound)
    #[checked] fn at(&self, key: &K) -> &V;                            // panics on a missing key
    #[unsafe] fn unsafe_at(&self, key: &K) -> &V;                      // callable only in `unsafe` code
    fn len(&self) -> usize;
    fn clear(&mut self) -> ();
    #[might_sleep] fn try_reserve(&mut self, n: usize) -> result<()>;  // atomic-context rules apply (4.13)
    // Construction follows the protocol of 4.7: declared like a struct's own, lowered to the reloco members.
    fn try_create() -> result<Self>;
    fn try_clone(&self) -> result<Self>;                                // gives the type `cloneable` (4.12)
    fn drop(&mut self);                                                  // destructor exists; kif never calls it by hand
}
```

- Each `fn` is lowered to a member call with the receiver mapped as above; the parameter and result types are written with kif
  types or other wrapped types. Lifetime relationships follow the elision rules of 4.4 and add `RELOCO_LIFETIMEBOUND` to the
  generated call wrappers where the C++ declaration has them.
- **Tiers.** `#[checked]`, `#[unsafe]` and the plain/`try_` forms mirror reloco's tiers: a `#[unsafe]` member needs an `unsafe`
  block (KIFP131), a `#[checked]` one is a documented panic point (it is the declaration's contract that it traps rather than UB).
- **Annotations.** The attributes of 4.13 (`#[might_sleep]`, `#[atomic_only]`, `#[enter_atomic]`, ...) apply to member
  declarations, and `#[atomic_lock]`/`#[atomic_scope]`/`#[atomic_token]` apply to the `extern "C++" type` itself.
  Thread safety is **opt in** (kif is safe by default): a wrapped C/C++ type is neither `Send` nor `Sync` unless its declaration
  says `#[send]` and/or `#[sync]` (reloco's `is_send`/`is_sync` facts). `#[send(T, U)]` means "send when `T` and `U` are send"
  (likewise `sync`), as for `ptr<T>` or `vector<T>`. There are no `not_send`/`not_sync` attributes; omission is the negative.
  Declaring one is the developer's claim, like every `extern`. Checking across threads is a later feature; the facts are recorded now.
- **Language items.** `#[lang(result)]` marks the type that `?`, `ok`, `err` use; `#[lang(optional)]` marks the optional type.
  Items needed by later features (iteration, indexing) get more `lang` names when those features are designed; a `lang`
  name that kif does not know is KIFP132.
- **Construction protocol.** `try_create`/`try_allocate`/`try_construct`/`try_clone` declared in an `extern "C++" impl` follow
  the rules of 4.7 and make the wrapped type usable in struct fields, `ptr<T>::try_allocate` and `#[derive(clone)]`. A wrapped
  type with no declared `drop` and no declared move operation is a KIFP133 when used as a field (kif cannot
  know it is movable).
- **What kif trusts.** The declaration is a contract the developer writes and is responsible for (like every `extern`);
  nothing is generated from headers. A mismatch is found by the C++ compiler when the generated code is built.
- **C mode.** Generic or wrapped types are not available in C (KIFP053).
- **The prelude** (the table above) is exactly this: a shipped `reloco.kif` of `extern "C++" type` and `extern "C++" impl`
  declarations, so every prelude type is declared with the facilities any user can use.

#### Attribute reference

Every attribute the toolchain knows (the list the language server completes from), where it goes and where it is specified.
The three that only the shipped preludes use (`provided`, `forward_args`, `operator`) are explained in full below the table.

| Attribute | On | Meaning | Section |
| --- | --- | --- | --- |
| `#[cxx_name("ns::name")]`, `#[header("x.hpp")]` | `extern "C++"` type, fn, impl; interface; kif items | the C++ spelling and the header that declares it; `header` is repeatable | 3.2, 4.6, 4.10 |
| `#[lang(item)]` | prelude types and members | marks a language item (`result`, `optional`, `deref`, `c_ptr`, `c_cptr`); an unknown name is KIFP132 | 4.10 |
| `#[copy]`, `#[send]`, `#[sync]` (also `send(T)`, `sync(T)`) | struct, `extern` type | copy semantics; thread-safety facts (opt in, omission is the negative) | 4.1, 4.10 |
| `#[zeroable]` | `extern "C++"` type | all-zero bytes is a valid value, so it may be a cell type (KIFP180) | 4.17 |
| `#[checked]`, `#[unsafe]`, `#[safe]` | fn, method, `extern` fn | tiers: a documented panic point; needs an `unsafe` block (KIFP131); a C function callable without `unsafe` | 4.6, 4.10 |
| `#[allocates]`, `#[might_sleep]`, `#[atomic_*]`, `#[enter_atomic]`, `#[leave_atomic]` | see 4.13 | allocation and atomic-context annotations | 4.13 |
| `#[forward_args]` | `extern "C++"` factory taking `...` | **the call's extra arguments are checked against the constructed type's own protocol**, not against this declaration | below |
| `#[operator("op")]` | `extern "C++"` method | the method is the C++ operator `op` of the wrapped type | below |
| `#[provided]` | interface method with no body | implementers get the method for free from the provider (the declaration is its contract) | below |
| `#[derive(..)]`, `#[count]`, `#[trace]` | struct, enum, interface, method | generation-time customisation | 7 |
| `#[visibility(..)]`, `#[export]`, `#[link_name("..")]`, `#[no_prefix]`, `#[typedef]`, `#[ops]`, `#![target(c)]` | fn, struct, enum, interface, file | C output naming and shape | 4.6, 4.15, 9.1 |
| `#[kobj]`, `#[version(n)]`, `#[extends(b)]`, `#[foreign]`, `#[self]` | interface | kobj interfaces | 4.8, 4.15 |
| `#[repr(c)]`, `#[repr(packed)]`, `#[align(n)]`, `#[heap]`, `#[small_stack(N)]`, `#[stack(N)]` | struct, fn, file | layout, heap-only types and stack budgets | 4.7, 4.11 |
| `#[flags]`, `#[format]` | enum, `extern` type | bit-flag enum; a microfmt formatter exists | 4.5, 4.16 |
| `#[caller_location]`, `#[track_caller]` | parameter, fn | the callee receives the caller's source location | 4.16 |
| `#[tls]`, `#[per_cpu]` | `static` | thread-local and per-CPU cells | 4.17 |

```kif
// #[forward_args]: `...` stands for whatever T's own try_create/try_construct takes. `ptr<T>` is declared once for every T, so
// its factory cannot name the arguments; the checker validates them against T (4.7) and C++ forwards them with perfect forwarding.
extern "C++" impl<T> ptr<T> {
    #[allocates] #[forward_args] fn try_create(...) -> result<Self>;
}

// #[operator]: the method IS the C++ operator, so `a + b` and `a < b` on a wrapped type call it (the C++ backend emits the
// operator, not a method call). The string is the operator token; one method per operator.
extern "C++" impl duration {
    #[operator("+")] fn add(self, rhs: duration) -> duration;
    #[operator("<")] fn lt(&self, rhs: &duration) -> bool;
}

// #[provided]: an interface method that implementers do not write. `as_sink` builds the type-erased `sink` from `self`,
// so every `impl text_sink for S` has it, and its result borrows the implementer (4.4 lifetime rules).
interface text_sink {
    method write(&mut self, text: str_view) -> ();
    #[provided] method as_sink(&mut self) -> sink;
}
```

Status: the checker records `forward_args`, `operator` and `provided` (the ABI manifest lists a `provided` method among the
interface's defaults, and the linter and `Method.has_default()` treat it as having a body) but does not yet resolve `a + b`
through `#[operator]` or type the `...` arguments of `#[forward_args]`; both become checks when the C++ backend lands.

### 4.11 Pointers and heap-only types

| Type | Meaning | C++ |
| --- | --- | --- |
| `ptr<T>` | owning, unique | `reloco::unique_ptr<T>` |
| `arc<T>`, `rc<T>` | shared ownership (atomic / non-atomic count) | `reloco::shared_ptr<T>`, `reloco::rc<T>` |
| `weak<T>`, `weak_rc<T>` | non-owning observers of `arc<T>` / `rc<T>` | `reloco::weak_ptr<T>`, `reloco::weak_rc<T>` |
| `c_ptr<T>`, `c_cptr<T>` | raw `T *` / `const T *`, unsafe code and `extern "C"` only | `T *` / `const T *` in `RELOCO_BEGIN_UNSAFE_CODE` regions |

Safe code never sees a raw pointer: it borrows with `&T` / `&mut T` or holds one of the owning types.

**Heap-only structs** keep large objects off kernel stacks. The struct is marked `#[heap]`:

```kif
// A ring with an inline 64 KiB buffer. `#[heap]` means a value of `ring` can only exist inside a heap allocation made by
// `ptr<ring>::try_allocate(alloc, ..)` (or arc/rc); it is never a local, a by-value parameter or result, or a by-value field.
#[heap]
struct ring {
    data: [u8; 65536],
    head: usize = 0,
}

impl ring {
    // For a `#[heap]` struct the construction protocol is `try_construct` only: it runs on the final heap storage and
    // returns `result<()>`; the object is never built on the stack and then moved.
    fn try_construct(&mut self, head: usize) -> result<()> { self.head = head; ok(()) }
}

fn make(alloc: allocator_ref) -> result<ptr<ring>> { ptr<ring>::try_allocate(alloc, 0) }
```

- Rules: a `#[heap]` value cannot be a local, a by-value parameter, a return value or a by-value field of a non-heap struct
  (KIFP080); it may be a field of another `#[heap]` struct, and `&`/`&mut` borrows are free. It is not movable (KIFP081);
  `ptr<ring>` itself moves as a pointer. `try_create`/`try_allocate` as factories returning `Self` and a by-value `try_clone` are
  not allowed on it (KIFP082; clone it in place with `try_clone_at`, 4.12), and the `Self { .. }` literal is rejected (KIFP083); `try_construct` is required.
- C++: the class has deleted copy and move operations and a private shell constructor reachable only through
  `reloco::construction_helpers`/`try_allocate`, so even handwritten C++ cannot place it on the stack.
- **Stack budget (`#[small_stack(N)]`).** `N` is a byte limit for stack use. It is set per function, per `impl`/struct, or
  **globally** for a file or module with an inner attribute `#![small_stack(N)]`, and the command line gives a default for
  everything: `kifc --small-stack=N` (also a project/CMake option). The narrowest setting wins; `#[small_stack(off)]` lifts it.
  The checker estimates the frame of each function: sizes of locals, by-value parameters and temporaries whose layout kif knows
  (scalars, arrays, structs, enums), plus the **deepest call chain** through kif functions that have a known frame (recursion
  is "unbounded" and is always reported). Over the limit it warns KIFW010 (design: docs/kif-stack.md) naming the biggest locals and suggesting `#[heap]` or a
  borrow; a single local larger than N is KIFP084. `extern` callees count as 0 unless annotated `#[stack(N)]`. The estimate is
  conservative about layout, not about the compiler's register allocation, so it is a warning, never ABI.
- C: `#[heap]` is a plain struct; with an `impl` it gets `S_try_new`/`S_delete` functions that the compiler pairs with automatic drops of `ptr<S>` (`docs/kif-c-backends.md` 3.2).

### 4.12 Cloning (deep copy)

Structs are move-only; the only way to duplicate one is an explicit, fallible **deep clone** (it may allocate), following
reloco's `try_clone` protocol (`docs/fallible-construction.md` section 4).

```kif
// Written by hand, as in 4.7: an independent copy that can fail.
impl ring { fn try_clone(&self) -> result<Self> { ... } }

// Or generated: member-wise deep clone. Every field must itself be cloneable (KIFP090 names the field that is not).
#[derive(clone)]
struct session { id: u32, name: vector<u8>, ring: ptr<ring> }

fn dup(s: &session) -> result<session> { s.try_clone() }      // `s` stays valid; the result is a new, independent value
fn dup_in(s: &session, a: allocator_ref) -> result<session> { s.try_clone_in(a) }   // explicit allocator
```

- **Cloneable types:** scalars, enums and `#[copy]` structs (plain copy); `optional<T>`, `vector<T>` and `ptr<T>` when `T` is
  cloneable (`ptr<T>` allocates a new `T` and clones into it, which is a **deep** copy); structs with a hand-written
  `try_clone`/`try_clone_at` or `#[derive(clone)]`. `arc<T>`/`rc<T>` are cloned by sharing (a count increment, `.share()`, which
  cannot fail); a deep copy of the pointee is written explicitly (`arc::try_create(x.try_clone()?)`). `weak<T>` clones by `.share()` too.
- **`#[derive(clone)]`** generates `try_clone()` (default allocator) and `try_clone_in(alloc)` (allocator-aware) that clone the
  fields in declaration order; if a later field fails, the already cloned fields are destroyed and the error is returned (no
  partial object). In C++ they are the reloco members, so `has_try_clone_v<S>` holds and `reloco::vector<S>::try_clone` works.
  Fields that are not cloneable (`c_ptr<T>`, a `drop`-owned raw resource) must be handled by a hand-written `try_clone` (KIFP090).
- **Generic bound:** `cloneable` joins the bound set of 4.9; `fn dup<T: cloneable>(x: &T) -> result<T>`.
- **`#[heap]` structs** (4.11) are cloned in place: they write `fn try_clone_at(&self, dst: &mut Self) -> result<()>` (reloco's
  `try_clone_at`), and `ptr<T>::try_clone` is "allocate, then `try_clone_at`", so the clone is never built on the stack.
  `#[derive(clone)]` on a `#[heap]` struct generates `try_clone_at`.
- Moves never clone, and nothing clones implicitly (KIFP023 explains a use after move with a hint to `try_clone`).
- C and `gen fn`: no cloning (trivial structs are copied with `=`); KIFP053 for `try_clone` there.

### 4.13 Atomic-context annotations

Kernel code must not sleep in interrupt handlers, with preemption disabled or under a spinlock. kif has the same annotations
as reloco's `reloco-atomic-context` check (`../reloco/docs/atomic-context.md`) and **checks them itself** in the Python
checker (no clang), so the rule holds for kif code, for `extern` declarations and for interface methods.

| Attribute | On | Meaning | C++ lowering |
| --- | --- | --- | --- |
| `#[might_sleep]` | `fn`, method, `extern` fn, `impl` | may block or reschedule; calling it in an atomic scope is an error | `RELOCO_MIGHT_SLEEP` |
| `#[atomic_only]` | `fn`, method, `extern` fn | callable only in atomic context (per-CPU data); body is checked as atomic | `RELOCO_ATOMIC_ONLY` |
| `#[atomic_entry]` | `fn`, method | entered by hardware or foreign code in atomic context (interrupt handler); body checked as atomic, callers need not be | `RELOCO_ATOMIC_ENTRY` |
| `#[atomic_scope]` | struct with `drop` | a local of this type holds an atomic scope until the end of its block (IRQ, preemption or spinlock guard) | `RELOCO_ATOMIC_SCOPE` |
| `#[atomic_lock]` | struct | a lock whose holder is atomic (spinlock); a guard over it is an atomic scope | `RELOCO_ATOMIC_LOCK` |
| `#[enter_atomic]` / `#[leave_atomic]` | `extern` fn | the call starts / ends an atomic scope (legacy `critical_enter()`/`critical_exit()`) | `RELOCO_ENTER_ATOMIC` / `RELOCO_LEAVE_ATOMIC` |
| `#[atomic_callback]` | `fn`-pointer parameter | a function passed for it runs atomically | `RELOCO_ATOMIC_CALLBACK` |
| `#[atomic_token]` | struct | proof of atomic context; a function taking `&token` is checked as atomic | `RELOCO_ATOMIC_TOKEN` |
| `#[allocates]` | `fn`, method, `extern` fn | allocates memory; calling it (directly or transitively) from a per-CPU slot's init is KIFP186 (4.17). Every allocating member of the prelude containers, strings and pointers carries it | (documentation and checker only) |
| `#[atomic_exempt]` | `fn`, struct | body is not checked and never inferred to sleep; needs a `// reason` comment (KIFL lint) | `RELOCO_ATOMIC_EXEMPT` |

```kif
// Interface methods carry the annotation, so every implementer and every caller sees it (and it is in the ABI manifest).
interface timer {
    // May be called from the timer interrupt: implementations are checked as atomic.
    #[atomic_entry] method tick(now: u64) -> ();
    // Sleeps (waits for hardware); callers in atomic context are rejected.
    #[might_sleep]  method calibrate() -> i32;
}

// Legacy C functions: say what they do, kif never parses headers.
#[enter_atomic] extern "C" fn critical_enter() -> ();
#[leave_atomic] extern "C" fn critical_exit() -> ();
#[might_sleep]  extern "C" fn malloc_wait(n: usize) -> c_ptr<u8>;

#[atomic_scope]
struct irq_guard { flags: usize = 0 }
impl irq_guard { fn new() -> Self { ... } fn drop(&mut self) { ... } }

fn update(t: &mut hpet) -> i32 {
    let _g = irq_guard::new();          // atomic from here to the end of the block
    t.ticks += 1;
    t.calibrate()                        // KIFP110: calling a might_sleep method in atomic context
}
```

- **Inference.** A function that calls a `#[might_sleep]` function is itself sleepy without annotation; only the leaf
  declarations (`extern`, interface methods and the lowest-level `fn`s) need them. `#[atomic_entry]` and `#[atomic_only]`
  bodies are checked as atomic, and calling a sleepy function from them is KIFP110. Calling an `#[atomic_only]` function outside an atomic
  context is KIFP111. Entering a scope without leaving it on every path is KIFP112 (an `#[atomic_scope]` local cannot be
  moved out of its block, KIFP113).
- **Interface methods.** An implementation must not be *less* atomic-safe than the declaration: implementing an
  `#[atomic_entry]` method with a sleepy body is KIFP110, and a method without annotation that sleeps in an `impl iface for S`
  is inferred and reported to callers that are atomic. `.m` imports carry annotations through `#[...]`-style comments in the
  generated `.kif` only; `.m` itself has none.
- **Hooks (7.2).** `before`/`after` hooks run in the caller's context; a `#[atomic_entry]` method's hooks are checked as atomic,
  which stops a tracing hook from taking a sleeping lock in an interrupt.
- **Checked forms.** `#[might_sleep] impl S { .. }` makes every method of the impl sleepy. A struct that holds a reference to an
  `#[atomic_lock]` struct is a lock guard: a local of it is an atomic scope like `#[atomic_scope]`. A function passed by name for an
  `#[atomic_callback]` parameter must not be sleepy (KIFP110 at the argument). Hook bodies are checked as atomic when the method they
  wrap is `#[atomic_entry]`/`#[atomic_only]`, and sleepy calls in any hook are inferred like in a function.
- **`gen fn`.** Not applicable; the annotations are ignored there.
- **C.** The attributes are checked by kif and emitted as comments in the generated C (C has no checker for them).
- **Manifest and lint.** The ABI manifest records `might_sleep`/`atomic_only`/`atomic_entry` per method; changing one on a
  published method is an ABI change (KIFL-class rule alongside the existing ABI rules).

### 4.14 Strings

Three string types, with ownership made explicit:

| Type | Meaning | C++ | C mode |
| --- | --- | --- | --- |
| `str` | **owned, heap-allocated** string; move-only; an empty value never allocates | `reloco::string` | no |
| `str_view` | **borrowed** immutable view (pointer and length, not NUL-terminated) | `reloco::string_view` | no |
| `cstr` | NUL-terminated C string with **static lifetime**, never owned or freed by kif | `const char *` | `const char *` |

```kif
extern "C++" fn log_view(msg: str_view) -> ();         // a view: the callee must not keep it past the call
extern "C"   fn kprintf(fmt: cstr, ...) -> i32;        // a C string: the callee must not free or keep it
extern "C"   fn dev_name(id: u32) -> cstr;             // FFI may RETURN a cstr: static lifetime by the declaration's contract

struct label { text: str, id: u32 }                    // `str` may be a field (the struct is then a C++-only class)

impl label {
    fn try_create(text: str_view, id: u32) -> result<Self> {   // a view is an ordinary borrowed parameter
        ok(Self { text: text.try_clone()?, id: id })           // a view (or cstr) is "cloned" into an owned str
    }
    fn rename(&mut self, text: str) { self.text = text; }      // `str` as a method argument: moved in
    fn view(&self) -> str_view { self.text.view() }            // borrow of the field, tied to `self` (lifetimebound)
}

fn describe(id: u32) -> result<str> {
    let mut s: str = dev_name(id).try_clone()?;        // cstr -> str: an explicit deep copy
    s.try_append(" up")?;                              // literals are cstr; coerce to str_view
    kprintf("%s\n", &s);                              // `str` borrowed as cstr: allowed only as an FFI call argument
    log_view(s.view());                                // `str` borrowed as str_view
    ok(s)
}
```

- **Literals.** `"abc"` has type `cstr` (static, NUL-terminated) and coerces to `str_view`. It is never a `str`.
- **`str`** is reloco's string with its tiers: `len()`, `is_empty()`, `s[i]` (checked: panics out of range), `try_append`,
  `try_reserve`, `try_clone` (4.12), `view() -> str_view`. Anything that allocates is fallible, and there is no `+`. It is
  move-only (4.7) and may be a struct field, a local, a parameter (moved or `&str`/`&mut str`) and a result.
- **`str_view`** is an ordinary borrow, not an FFI special: a local, a parameter or a result, with the usual borrow rules (it
  cannot outlive what it views; results are tied to their inputs). It **cannot be a struct field** (KIFP122; store a `str`,
  or a `cstr` if static). It has read-only operations (`len()`, `is_empty()`, `v[i]`, `starts_with`, `find`, `sub(a, b)`,
  comparisons) and `try_clone() -> result<str>`.
- **Conversions.**

  | From | To | Allowed |
  | --- | --- | --- |
  | `str` | `str_view` | borrow, anywhere: `s.view()` (or `&s` coerces where a view is expected); lifetime follows `s` |
  | `str` | `cstr` | borrow, **only as a direct argument of an `extern` call** (FFI); cannot be stored or returned (KIFP120) because it is not static |
  | `str_view` | `cstr` | **never** (KIFP121): a view is not NUL-terminated and not static; copy into a `str` and pass that to FFI |
  | `cstr` | `str_view` | always (static text) |
  | `cstr`, `str_view` | `str` | only by the explicit deep copy `try_clone()` (or `str::try_create(v)`); it allocates, so it returns `result<str>` |

- **`cstr` from the FFI world.** An `extern` function may return or take `cstr`. Both are *non-owning*: kif never frees,
  mutates or takes ownership of a `cstr`, and a function taking one must not take ownership either. A returned `cstr` is
  trusted to be static (the declaration's contract); text with another lifetime is `c_cptr<char>`, which is unsafe (4.11).
- **Interface methods.** Methods of C-visible interfaces keep today's text types (`cstr`/`c_cptr<char>` plus a length). `str` and
  `str_view` are allowed in `impl` methods, free `fn`s and methods of `cxx_only` interfaces, which are C++ only anyway; a
  C-visible interface method that uses them is KIFP122.
- **`gen fn`.** All three are the same immutable interpreter string; the conversions are no-ops there.
- **C mode** (9.1): `str` is `{char *, len, cap}` and `str_view` is `{ptr, len}` (`docs/kif-c-backends.md` 3.1); `cstr` stays the NUL-terminated form.

### 4.15 Interfaces: kobj is optional (`#[kobj]`)

An interface does **not** have to be a kobj. Many interfaces exist only so C++ code can be written against a contract and
**devirtualised** (called statically, inlined); kobj dispatch (method ids, slots, class tables, C symbols, ABI manifest) is
only needed when the implementation is chosen at run time or must be reachable from C. So kobj is opt-in:

```kif
// Plain interface: no kobj machinery at all. C++ only. Calls are resolved at compile time (concept + CRTP).
interface clock {
    method gettime(ts: ptr<timespec>) -> i32;
    method resolution() -> u64 { 1 }                 // default body (4.2 / section 7)
}

// kobj interface: dispatch through kobj classes, ids and slots, ABI-checked, usable from C and `.m` as today.
#[kobj, self(device_t), softc(struct hpet_softc), version(2)]
interface hpet_if {
    method read(reg: u32) -> u32;
}
```

| Feature | Plain interface | `#[kobj]` interface |
| --- | --- | --- |
| methods, doc comments, `static` methods, default bodies, `extends` | yes | yes |
| `before`/`after` hooks (7.2), atomic annotations (4.13), `#[derive]` (7.3) | yes | yes |
| `impl iface for S` (4.8) | S gets the contract; no class table | S gets a `kclass` and `make_class` |
| call dispatch | static: C++ concept `clock_like<T>` and CRTP base `clock_base<Derived>` carrying defaults and hooks; inlined | `kobj` call through the class table |
| run-time polymorphism (a handle that holds "some clock") | not provided (use a generic `fn<T: clock>` or a `#[kobj]` interface) | yes (`ref<I>` / kobj handle) |
| `self(..)`, `softc(..)`, `errno(..)`, `version(..)` | **KIFP140** | yes |
| method ids, signature hashes, ABI manifest, ABI lint (KIFL rules) | not produced | yes |
| C and FreeBSD (`--freebsd`) output, `.m` import and export, `m_prolog`/`m_epilog` | **KIFP140** (C++ only) | yes (C-visible unless `cxx_only`) |
| `foreign class`, `implements(..)`, `super<>` | not applicable (KIFP140) | yes |
| mock and docs generation | yes (a mock class satisfying the concept) | yes |

- **`cxx_only`** keeps its present meaning on a `#[kobj]` interface (kobj dispatch, but C++ types and no C output). A plain
  interface is C++ only by definition, so `cxx_only` on it is redundant (KIFW011).
- **Generic bound.** A plain interface name is a bound for generic functions (4.9): `fn tick<T: clock>(c: &T)` is a C++ template
  constrained by `clock_like<T>`; it works for plain and kobj interfaces alike (a kobj implementer also satisfies the concept).
- **`extends`** works within one kind: a kobj may extend only a kobj and a plain interface only a plain one (KIFP141), since a
  kobj needs the base's ids and slots.
- **Choosing.** Written as `interface` with no `#[kobj]`, the default is **plain**. Existing kif files and `.m` imports
  therefore get `#[kobj]` added (the `.m` importer emits it; there are no users to migrate, section 11).
- **Same text, two lowerings.** Adding `#[kobj]` to a plain interface that uses only shared features is a one-word change;
  the bodies, hooks and implementers do not change.
- **C mode** (9.1): `#[kobj]` interfaces lower only on targets with a `kobj` section; a plain `#[ops]` interface lowers to a table of function pointers (`docs/kif-c-backends.md` 5.1).
- **`dyn` handles**: an `#[ops]` interface is used through `&dyn I` / `&mut dyn I` (a handle: object plus table). `dyn` is valid only behind `&` / `&mut`; `&S` / `&mut S` coerces to it when `impl I for S` exists, and `&mut self` methods need `&mut dyn I`. Inside a default body `self` is the handle.

```kif
#[ops] interface counter { method add(&mut self, n: u32) -> u32; }
struct simple { n: u32 }
impl counter for simple { fn add(&mut self, n: u32) -> u32 { self.n += n; self.n } }
// `c` is a handle: any implementer of `counter`, borrowed mutably
fn bump(c: &mut dyn counter) -> u32 { c.add(1) }
fn go() -> u32 { let mut s = simple { n: 0 }; bump(&mut s) }  // &mut simple coerces to &mut dyn counter
```

### 4.16 Formatting, sinks and source locations

**All formatting in lowered code is microfmt**, which is allocation-free, exception-free and checks arguments at compile time. There is
no `printf`-style formatting in safe code, and no way to format "into the air": **text is always written to a sink**.

```kif
// `&sink` is `const microfmt::sink &`: any destination (UART, ring buffer, log, bounded buffer). The function does not
// know or care which; the caller owns the sink context and decides where text goes.
#[Debug]                                      // generates a microfmt formatter for the struct (below)
struct sample { id: u32, state: state }       // `state` is a kif enum: formats as its enumerator name

fn report(out: &sink, s: &sample, name: str_view) {
    // `write!(sink, "format", args...)` lowers to `microfmt::format_to(out, MICROFMT_STRING("..."), args...)`.
    // The format string must be a literal or a `const cstr` (KIFP150): microfmt then checks it at compile time.
    write!(out, "sample {} ({}) is {:08x}", name, s.id, s.id);
    write!(out, "full: {}\n", s);            // uses the `#[Debug]` formatter: `{id: 7, state: ready}`
}

// A growing destination is a fallible sink: `try_write!` returns `result<()>` (allocation can fail), `write!` does not exist
// for it. `str` and `vector<u8>` are such containers (microfmt's container sinks).
fn describe(s: &sample) -> result<str> {
    let mut text: str = str::try_create("")?;
    try_write!(&mut text, "sample {}", s.id)?;
    ok(text)
}

// Source location: `here!()` is the location of the expression, as reloco's `call_location_ref`.
fn fail(out: &sink, why: str_view) {
    let at: call_location_ref = here!();      // a reference to a constant in .rodata: {"drivers/hpet.kif", 17}
    write!(out, "{}:{}: {}", at.file(), at.line(), why);
}

// `#[caller_location]` asks the CALLER for its location instead of the callee's own (like Rust's `#[track_caller]`).
fn check(cond: bool, #[caller_location] at: call_location_ref) {
    if !cond { panic_at!(at, "check failed"); }
}
```

- **Sink types.** `microfmt.kif`, shipped next to `reloco.kif`, declares the wrapped types (4.10.1): `sink` (use it as `&sink`),
  bounded buffers (`memory_buffer<N>`, `c_string_span_sink`), ring-buffer sinks, `log` contexts. A project adds its own with
  `extern "C++" type` + `impl`. Whether a sink may block is its declaration's `#[might_sleep]`, so the atomic-context rules
  (4.13) apply to `write!` into a sleeping sink.
- **`write!` / `try_write!`.** `write!(sink, fmt, args...)` has type `()` and cannot fail (a full bounded sink truncates, as
  documented by that sink). `try_write!(&mut container, fmt, args...)` returns `result<()>` and is for containers that grow.
  `writeln!` and `try_writeln!` append `\n`. Logging is `info!`, `warn!`, `error!`, `debug!`, `trace!`(logger, fmt, args...),
  lowered to `MICROFMT_LOGGER_*`; the logger is the sink context.
- **Type safety.** The checker verifies the number of `{}`/`{n}` fields against the arguments (KIFP151) and that every argument
  type is formattable (KIFP152): integers, `bool`, `char`, `str`/`str_view`/`cstr`, `c_ptr<T>` (`{:p}`), enums (enumerator name;
  `#[flags]` as `a | b`), structs with `#[Debug]`, and wrapped types declared `#[format]`. Format specs (`{:08x}`) are checked by
  microfmt at C++ compile time. A format string that is not constant is allowed only in `unsafe` (microfmt supports runtime
  strings), otherwise KIFP150.
- **`#[Debug]`** on a struct or enum generates `template <> struct microfmt::formatter<T>`: a struct renders as
  `{name: value, ...}` (the same shape as microfmt's `MICROFMT_REFLECT_FORMAT`, without reflection, since kif knows the fields),
  an enum as its enumerator name (or the number for an undeclared value), `#[flags]` as `a | b`. Every field must itself be
  formattable (KIFP152 names it); `#[Debug(skip(field))]` leaves a field out. It applies to structs and enums, not interfaces. A hand-written
  microfmt formatter for a wrapped C++ type is declared with `#[format]` on its `extern "C++" type`.
- **Source locations.** `here!()` evaluates to a `reloco::call_location_ref` pointing at a `static constexpr
  reloco::call_location{file, line}` that the generator places in `.rodata` (one per use; identical file/line pairs are
  shared). The file is the path as given to `kifc`, rewritable with `--file-prefix-map=old=new` (reproducible builds, no host
  paths in the kernel). A parameter marked `#[caller_location]` is filled by every call site with its own `here!()`; in
  C++ it is the defaulted `call_location_ref::current()` argument, so wrapped and handwritten code sees the same value.
  `panic!` and checked operations already use the location this way (`KIF_PANIC(file, line, msg)`, 4.3); `panic_at!(loc, msg)`
  reports a given one. With `--no-locations` (and in builds where reloco compiles locations out) `here!()` is
  `call_location_ref::none()`.
- **`gen fn`.** `format!("..", args)` stays the generation-time string builder of section 5 (it returns a `str`). `write!`
  and the `here!()` family do not exist there (the generator knows the real source; use `item.location()`).
- **C mode.** microfmt is C++, so `write!`, `try_write!`, the logging macros and `#[Debug]` are KIFP053; `here!()` is
  allowed (`const struct kif_loc { const char *file; int line; }` in `.rodata`).

### 4.17 Thread-local and per-CPU cells (`#[tls]`, `#[per_cpu]`)

Kernels have no `thread_local`: the compiler's TLS model needs a loader, a runtime and a thread pointer that early boot,
interrupt paths and new CPUs do not have. kif therefore has two kinds of *cell*, both declared with a `static` item and both
lowered to a per-module **block** that the OS provider places and instantiates (no `thread_local`, no `__thread`, no
constructors).

```kif
// A per-thread cell. The value after `=` is the template every thread's block starts from. It is a constant, so the
// block can be created by copying bytes, and a thread's first access needs no initialisation code.
#[tls] static LAST_ERROR: i32 = 0;

// A per-CPU cell: one instance per CPU, reachable only from atomic context (no migration or preemption between
// reading the CPU's identity and using the cell). Maps to FreeBSD DPCPU_DEFINE_STATIC().
#[per_cpu] static RX_PACKETS: u64 = 0;

// TLS (not per-CPU) may own a resource: `drop` runs when the thread exits. Its zero state is "nothing owned".
#[tls] static SCRATCH: scratch = scratch::empty();

#[atomic_only]
fn count_rx() {
    // `with_mut` hands the closure `&mut T` for this CPU. The reference exists only inside the closure and the
    // closure's result must not borrow from it (KIFP185), so no reference to a cell outlives the access.
    RX_PACKETS.with_mut(|n| { *n = *n + 1; });
}

fn last_error() -> i32 {
    LAST_ERROR.with(|e| *e)      // `with` gives `&T`; the result is copied out
}
```

**Rules.**

1. **Zero-initialised memory must be a valid cell.** A cell can be read before any initialisation ran: a new thread's block, a
   CPU that has just come up, early boot, and a crash handler all see zeroed bytes. The cell type must therefore be
   *zeroable*: integers, floats, `bool`, nullable raw pointers, arrays, tuples and structs of zeroable types, enums that have
   a member with value 0 (checked: the first member, or an explicit literal 0), kif structs and arrays/tuples whose fields are
   all zeroable (checked field by field), `c_ptr`/`c_cptr`, `extern "C++"` types marked `#[zeroable]` (an explicit claim the
   checker cannot verify), and `slot<T>` for any `T` (see "Slots" below). The prelude marks the reloco types whose zero value is
   valid: `optional` (zero is empty, whatever `T` is), `duration`, `instant`, `fixed_int`, `fixed_point`, `wrapping`, `saturating`,
   `checked_value` and `call_location_ref`. Not zeroable are `error` (no enumerator 0), `non_zero`, `allocator_ref` and the
   owning containers and pointers (`vector`, `ptr`, `str`, ...): hold those in a `slot`. References, owning
   pointers, function pointers and anything with a non-null niche are not zeroable (KIFP180). A type with `drop` is allowed only
   if its all-zero state is a valid, empty value that `drop` handles.
2. **The initial image is trivially copyable.** The initialiser is a constant expression (no calls except `const fn`, no
   `gen fn` results that are not constants) and its bytes are copied verbatim into every block, so creating a block is a
   `memcpy` (or nothing at all when the image is all zero, in which case it is placed in the zero-filled part of the block).
   There are no per-cell constructors, no lazy initialisation and no initialisation order between cells (KIFP181).
3. **Destructors: TLS only.** A `#[tls]` cell whose type has `drop` is destroyed when its thread exits, in unspecified order
   between cells, in process (not atomic) context. A `drop` that touches another TLS cell, directly or through the call
   graph (including the `drop` of every field, element and generic argument of the cell's type), is KIFP183, because that cell may already be destroyed. A `#[per_cpu]` cell **never** runs `drop` (a CPU does not
   exit), so a `drop` type in a per-CPU cell is rejected rather than silently skipped (KIFP182).
4. **Scoped access only.** A cell has no address and no name usable as a value. `NAME.with(|v| ..)` gives `&T`,
   `NAME.with_mut(|v| ..)` gives `&mut T`, and nothing else exists. The closure's result may not borrow from `v` (KIFP185), so
   references cannot outlive the access, escape to another thread or CPU, or survive a context switch.
5. **Per-CPU access is atomic-only.** `with` and `with_mut` of a `#[per_cpu]` cell behave like `#[atomic_only]` calls: outside
   atomic context they are KIFP111 (hold an `#[atomic_scope]` guard such as a critical section, or take an `&atomic_token`).
   A `with_mut` is additionally rejected (KIFP184) when the same cell is also reached, through the call graph, from an
   `#[atomic_entry]` function (an interrupt handler could interrupt the closure and alias the `&mut`). The cure is a guard that
   masks the interrupt, `#[per_cpu(irq)]` (accessors then take a `&irq_token`), or an atomic type for the cell.
6. **TLS access is not atomic-only,** but it is never allowed in an `#[atomic_entry]` body unless the cell is
   `#[tls(atomic_ok)]`, because an interrupt handler borrows whichever thread was interrupted, not a thread of its own.
7. **Cells are `'static` but not `Send` or `Sync` values:** the cell is not a value, so no sharing rule applies; the `T` need
   not be `Send`.
8. **Generation time.** The interpreter has no cells: a `gen fn` that names one is KIFP190, and a cell is never evaluated
   at generation time. A generator can still *list* cells: `Module.cells()` returns `Cell` handles (name, kind, type, slot-ness; section 6.1), which is how a `derive` or a layout report sees them without evaluating them.
9. `static` has no other use: there are no mutable globals, `static mut` or `static` items without `#[tls]`/`#[per_cpu]`
   (KIFP188). `static` is a contextual word: it starts an item, and stays usable as an identifier elsewhere.
   Process-wide state is an object owned by the module or a driver `softc`.

**Slots: complex objects in cells (`slot<T>`).** A raw cell holds only zeroable constants. For anything else (a struct with
invariants, a vector, a buffer, a lock, a non-zero default) the prelude provides `slot<T>`, a class whose own representation
is always zeroable and trivially copyable (aligned storage for a `T` plus a one-byte state: 0 = empty, 1 = initialising,
2 = ready), whatever `T` is. A zeroed slot is therefore a valid, empty slot, so every rule above holds for it, and `T` is
built by an *init function* given in the declaration. `slot<T>` exists only as the type of a `#[tls]` or `#[per_cpu]`
static (KIFP187).

```kif
struct conn { buf: vec<u8>, id: u32 }
impl conn { fn make() -> result<conn> { ... } }          // may allocate: TLS only

struct stats { rx: u64, last: u64 }
impl stats { fn init() -> stats { stats { rx: 0, last: 1 } } }   // non-trivial init, but no allocation: per-CPU ok

// TLS slot: `T` is built lazily, on the thread's first `try_with`, by the init function (returns `result<T>`, because
// it may allocate and fail). It may sleep. It is destroyed (`drop`) when the thread exits, only if it became ready.
#[tls] static CONN: slot<conn> = slot::new(conn::make);

// Per-CPU slot: `T` is built eagerly for each CPU by the provider when that CPU's block is created (CPU bring-up, or
// module load for CPUs already running), before any code of that CPU can reach it. Init cannot allocate (KIFP186),
// cannot fail (it returns `T`, not `result`), and no `drop` ever runs (KIFP182).
#[per_cpu] static STATS: slot<stats> = slot::new(stats::init);

fn send(b: &[u8]) -> result<()> {
    // `try_with` may initialise (allocate, sleep) and so is `#[might_sleep]`: calling it in atomic context is KIFP110.
    // It returns `result<R>`: the init error (or `error::busy` for a re-entrant access) comes back as a value.
    CONN.try_with(|c| c.buf.try_extend(b))?
}

fn poll() -> optional<u32> {
    // `with_ready` never initialises, never sleeps and is allowed in atomic context; `none` if the slot is not ready.
    CONN.with_ready(|c| c.id)
}

#[atomic_only]
fn count(n: u64) {
    STATS.with_mut(|s| { s.rx = s.rx + n; });          // per-CPU: infallible, atomic context only
}
```

| API | On | Meaning |
| --- | --- | --- |
| `slot::new(init)` | static initialiser | the init function: `fn() -> result<T>` for TLS, `fn() -> T` for per-CPU; a constant, stored in the block's registration, not in the slot bytes |
| `try_with(\|v\| ..) -> result<R>` / `try_with_mut` | TLS slot | initialise if empty, then run the closure; `#[might_sleep]`; init failure or recursion (`error::busy`) is returned |
| `with_ready(\|v\| ..) -> optional<R>` / `with_ready_mut` | TLS slot | run the closure only if ready; no init, no sleep, atomic-safe (not in `#[atomic_entry]` unless `#[tls(atomic_ok)]`, as above) |
| `with(\|v\| ..) -> R` / `with_mut` | per-CPU slot | run the closure on this CPU's value; atomic context only (KIFP111); ready by construction |

Rules for slots:

1. **TLS init may allocate and call anything** (including `#[might_sleep]` functions), runs in the accessing thread's
   process context on first use, and may fail. Subsequent accesses just read the state byte. `drop` runs at thread exit only
   for ready slots and may free what init allocated (it may also call allocating or sleeping functions, since thread
   exit is process context); it still must not touch other TLS cells (KIFP183).
2. **Per-CPU slots have no allocator.** The init function and everything it calls may not allocate: a call to a function
   marked `#[allocates]` (every allocating member of the prelude's containers, allocators and `arc`/`rc` constructors carries
   it) or any use of an `allocator` is KIFP186, checked through the call graph exactly like a sleep. Per-CPU init also
   must not sleep (it runs atomically) and cannot access other cells. A per-CPU `T` may therefore be non-trivial (invariants,
   non-zero defaults, embedded locks, fixed-capacity inline buffers such as `array`/`inline_vec<T, N>`) but owns no
   heap memory, and no `drop` ever runs for it (a `T` with `drop`, directly or in a field, is KIFP182).
3. **Initialisation is by the provider, not by user code.** For per-CPU the provider calls the init function with the CPU's
   block already copied from the template image; for TLS the state byte moves 0 -> 1 -> 2, and a re-entrant access from
   inside init sees state 1 (`error::busy`, `with_ready` returns `none`), so there is no recursion and no deadlock.
4. **The zeroed slot stays valid until ready,** so code that runs before init (early boot, a crash handler on a CPU
   that has not finished bring-up) can use `with_ready`; it never sees a half-built `T`.
5. The closure rules of 4.17 apply unchanged: scoped access only, closure result may not borrow (KIFP185).

**Lowering (design).**

- **The C++ runtime lives in reloco, not in kif.** `slot<T>`, the blocks, the state machine, the provider interface and the
  drop registry are base code in `../reloco` (`reloco/tls_slot.hpp`, `reloco/per_cpu_slot.hpp`, building on
  the **existing** `tls_provider<T, Tag>` (`tls_provider.hpp`, with its `RELOCO_TLS_MODEL_OS` porting hook): TLS needs no new
  provider, since each module's block is simply `tls_provider<kif_tls_<module>, module_tag>` (fallible `get()` with an
  `allocator_ref`, `Tag`-differentiated, one key per module). Only the per-CPU side is new: `per_cpu_provider<T, Tag>` (`per_cpu_provider.hpp`,
  `RELOCO_PER_CPU_MODEL`, with a `detail/porting/per_cpu_provider.template.hpp` FreeBSD kernel scaffold). Guides: `../reloco/docs/tls-slot.md`, `../reloco/docs/per-cpu-slot.md`), with reloco's own safety annotations, `result<T>`
  failure modes, docs and tests. Hand-written C++ gets the same classes. `slot<T>` lowers to `reloco::tls_slot<T>` (a thin
  state-machine layer over that provider's block; init `reloco::result<T>(*)() noexcept`, `try_with`/`try_with_mut`, `with_ready`/`with_ready_mut` (which return `bool` for a `void` closure), `destroy()` called by the block's owner at thread exit) and `reloco::per_cpu_slot<T>` (init `T(*)() noexcept`, `init`/`with`/`with_mut`, no destroy); `kif/proc.h`
  contains only thin glue and the C-mode fallback, never the implementation. The kif checker's rules (KIFP180-187) mirror what
  reloco enforces with its own annotations and `reloco-tidy`.
- **One block per module, one provider key per module.** All `#[tls]` cells of a module, with their `slot` state bytes, are
  laid out in a single chunk (`struct kif_tls_<module>`), and all `#[per_cpu]` cells in another (`struct kif_pcpu_<module>`),
  in declaration order, aligned to the largest member. The module owns exactly **one** TLS key (or one per-CPU area) however
  many cells it declares, so the number of keys the OS must hand out grows with the number of *modules*, never with the number
  of variables. That avoids the unbounded-key problem of pthread-style TLS (`pthread_key_create` per variable, a small fixed
  key table, a key leaked per `kldload`) and keeps a cell access to `block_base + constant_offset`: the offset is fixed at
  compile time, so there is no lookup by name or key. Block size and alignment are known when the module registers, so the
  provider reserves the space once. The zero-image bytes of all cells are one `static const` template in `.rodata` (omitted
  when entirely zero).
- **Lifecycle of a TLS block.** The module registers its block (size, align, image, drop, init functions) when it loads and
  unregisters when it unloads, which destroys the ready slots of every thread that still has a block. A thread's block is
  created on that thread's first TLS access by any cell of the module: this allocation can fail, so `try_with` returns the
  error and `with_ready` returns `none` while the thread has no block. A thread that never touches the module pays nothing.
  Exit destroys the block's ready slots (in reverse declaration order) and frees it.
- **Lifecycle of a per-CPU block.** The per-CPU area is reserved from the OS per-CPU allocator at module load for every
  present CPU, copied from the template, and initialised (slot init functions run in declaration order); a CPU that starts
  later gets its block as part of its bring-up. Nothing is allocated lazily.
- The provider interface (reloco, surfaced through `kif/proc.h` macros): `KIF_TLS_BLOCK(module)` and `KIF_PCPU_BLOCK(module)` return the current
  thread's or CPU's block, and `kif_tls_register(module, size, align, image)` / `kif_pcpu_register(...)` let the provider
  reserve the module's single key or area. Drop functions are registered the same way (`kif_tls_register_drop`).
- The block is located through reloco's `tls_provider` for TLS (`RELOCO_TLS_MODEL_OS` in kernels: a slot reached from
  `curthread`; `RELOCO_TLS_MODEL_THREAD_LOCAL` is for hosted tests only) and through an OS per-CPU hook for per-CPU. Under
  FreeBSD `DPCPU_DEFINE_STATIC` needs a literal identifier, so it cannot be generated from a template; the reloco scaffold
  instead uses `uma_zalloc_pcpu(9)` with `M_ZERO` (one zero-filled copy per CPU, including late-started ones) and
  `zpcpu_get(9)` / `zpcpu_get_cpu(9)` under `critical_enter(9)`, which is the zero-initialisable model above. Per-thread data
  is reached through the thread (an `osd(9)` slot: `osd_thread_register` with a destructor, `osd_thread_set`/`osd_thread_get`),
  with the registered `drop` called from that destructor at thread teardown.
- C lowering is identical (plain structs, plain functions); `with`/`with_mut` become a `KIF_TLS_BLOCK(m)->cell` access
  wrapped around the closure body, which is inlined (closures here are never stored).
- Porting scaffolds are provided as `kif/porting/tls.template.h` (FreeBSD kernel example).

**Implementation status:** the checker implements all of this (`kiflang/cells.py`). The C backend lowers raw `#[tls]` cells on the
`hosted` provider and on `custom` providers (`docs/kif-c-backends.md` section 6); `slot<T>`, the FreeBSD/Linux providers, the C++
runtime classes and the porting scaffolds are not written yet.

**Diagnostics:** KIFP180 (type not zeroable), KIFP181 (initialiser not constant, or type not trivially copyable as an image),
KIFP182 (`drop` in a per-CPU cell), KIFP183 (TLS access from a TLS `drop`), KIFP184 (`with_mut` of a cell also reached from
an atomic entry), KIFP185 (closure result borrows from the cell), KIFP186 (per-CPU init allocates, directly or through
calls), KIFP187 (`slot<T>` used outside a `#[tls]`/`#[per_cpu]` static), KIFP188 (a `static` without exactly one of `#[tls]`/`#[per_cpu]`), KIFP189 (TLS access in an `#[atomic_entry]` body), KIFP190 (cell used by a `gen fn`), KIFP191 (cell used as a value). Reused: KIFP111 (per-CPU access outside atomic context).

### 4.17a Symbol visibility (`#[visibility(..)]`)

`#[visibility(default|hidden|protected|internal)]` sets the ELF symbol visibility of the C symbols of functions, so kif code works
with `gcc -fvisibility=...` and the exports of a `.so`. It lowers to `KIF_VISIBILITY_<NAME>` (defined in `kif_c.h` as
`__attribute__((visibility("..")))` on GCC/Clang) on both the prototype and the definition. **Nothing is emitted where no scope
specifies a visibility**: the compiler's `-fvisibility` decides, and `#[export]` does not imply one.

```kif
#![visibility(hidden)]        // file scope: every external function below defaults to hidden

#[visibility(default)]        // function scope: the public API of the .so; beats the file
fn parse(text: str_view) -> i32 { 0 }

#[visibility(default)]        // impl scope: every method of this block
impl codec { fn reset(&mut self) { } }

fn helper(x: u32) -> u32 { x }  // inherits hidden from the file
```

Rules:
- The innermost scope that names a visibility wins: function, then its `impl` block, then the file. No scope, no attribute.
- **The target decides** whether visibility is used (kernel code may or may not): `"visibility": {"enabled": bool}` in the target
  JSON, default the value of `hosted`. Where it is false, `KIF_VISIBILITY_*` is not defined and every `#[visibility]` is checked
  and then ignored, so one source builds for a `.so` and for a kernel module. A kernel built with `-fvisibility=hidden` (or whose
  headers use the attribute) sets `"enabled": true`.
- The argument is one of the four words (KIFC001 otherwise); `#[visibility]` on an `#[inline]` function without `#[export]` is an
  error: it has no symbol, so the attribute could not do anything. A file-scope default skips such functions silently.
- It applies to functions (including methods) only; variables and `#[tls]` cells follow the compiler's `-fvisibility`.
- CMake: `add_kif_library(NAME TYPE SHARED VISIBILITY hidden ...)` sets `C_VISIBILITY_PRESET`; `kif new --kind solib` does this.
  Mark the API with `#[visibility(default)]`.

### 4.18 Entry point (`fn main`)

A program on a **hosted** target (`"hosted": true`, e.g. `c`) starts at a free function named `main`. The C backend lowers it as the
ordinary function `<stem>_main` (declared in `<stem>_proc.h`) and writes the C entry `int main(int argc, char **argv)` into
`<stem>_proc.c`; the wrapper builds the `&[str_view]` over `argv` (no copy, no allocation beyond the stack array) and passes it on.

```kif
// Accepted forms (the C wrapper is generated; you never write argc/argv):
fn main() { }                                  // exit status 0
fn main() -> i32 { 3 }                         // exit status = the returned value
fn main(args: &[str_view]) -> i32 {            // args[0] is the program name, as argv[0]; len() == argc
    if args.len() < 2 { return 2; }            // usage error
    let first = args[1];                       // a str_view into argv: valid for the whole program run
    0
}
```

Rules (all reported as KIFC001 by `kifc --c`):
- The parameter list is empty or exactly one `&[str_view]` (shared; a `&mut` slice would let the program rewrite argv). Rationale:
  the program cannot misuse `argc` against `argv`; the slice carries its length.
- The return type is absent, `()` (exit status 0) or `i32`.
- Not generic, not `gen fn`, not a method, and on a freestanding target there is no entry point (kernel modules use `#[kobj]`
  interfaces or `#[export]` functions): the error names the target.
- `#[export]`/`#[no_prefix]` on `main` are ignored: the lowered name is always `<stem>_main`, so two files may each have a `main`
  (only one may be linked into the program through the generated C `main`; the linker reports a second).
- Lint treats `main` as used (no unused-private-function finding).

### 4.19 Access tiers and proved accesses

Indexing and slicing are *checked accesses*: a failed check calls `panic`. The target's `access` object (`docs/kif-targets.md`)
picks the tier per access class (`index`, `range`):

- `prove` (default): the check is dropped where the compiler **proves** it cannot fail, and kept everywhere else.
- `checked`: the check is always emitted (audits, sanitizer builds, comparing generated code).

There is deliberately **no tier that checks nothing**: an access without a check or a proof is `unsafe` code, written as such
(`unsafe { .. }`, the `unsafe_` tier), so it is visible in the source and in review, never a build setting.

What is proved today is only what constants and cfg decide. Items disabled by `#[cfg(..)]` are removed before lowering, so a
length or index that is a constant depending on cfg is already a number here:

```kif
const N: usize = 4;                           // may itself be chosen by #[cfg(..)]
fn f(a: [u32; N], i: usize) -> u32 {
    a[2]          // proved: constant 2 < N, plain a[2] in C
    + a[i]        // not provable: KIF_IDX check stays
}
fn g(a: [u32; N]) -> u32 { a[7] }             // constant out of range: kept as a check, it panics when run
fn h(a: [u32; N]) { let s: &[u32] = a[1..3]; }  // proved: 0 <= 1 <= 3 <= N, no KIF_RANGE
```

Rules: the index (or both bounds of a range) and the array length must be integer literals or constants; an inclusive range adds
one to its end; vectors, slices, `str` and optionals have run-time lengths and are always checked. Proofs from control flow
(`if i < n`, loop bounds) are not done yet; the plan is to add them under the same `prove` tier, so no source or target changes.

### 4.19a Fact attributes (hints for the `prove` tier)

A function boundary hides what the callee knows. Fact attributes say it out loud, so the optimiser can later drop a check on
the far side. They are checked against the signature they sit on (a wrong one is an error).

**Status.** The *parameter* facts `#[some]` (an `optional<T>`, by value or `&`), `#[non_null]` (a plain `ptr<T>`),
`#[non_empty]` / `#[len_ge(N)]` (a `&[T]` or `vector<T>`, by value or `&`) and `#[lt_len(v)]` (a `usize` against such a `v`) are
**preconditions, and the C backend always checks them**:

- A function with such facts is emitted twice. `F` is the entry every outside caller uses: it traps (`argument 's' of first()
  violates #[non_empty]`) when a fact does not hold, then calls `F__nofacts`, the body, which **assumes** the facts and so drops
  the checks they cover.
- A call from kif goes to `F__nofacts` when the IR provers prove every fact at the call (a dominating test, a loop bound, an
  earlier check, a constant), and the target's `access.*` tier allows eliding (`checked` keeps the guards). Otherwise it calls
  `F` and the guard runs before the call. Nothing is ever trusted: no prover, no elision, only the run-time check.
- A fact the provers can show **never holds** is an error, not a trap: `KIFC110: call can never succeed: #[lt_len(s)] of argument
  'i' of pick() fails (5 is not below the length 2)`. Today that is a constant index against a known length (an array, reached
  as `&a`, `&a[..]` or `a.as_slice()`).
- A function pointer / function value, an `#[export]` function called from C, and a kobj table all use `F`, so they are guarded.
- Facts on other types stay validated hints. `#[ensures]` and `#[preserves]` are consumed by the provers (see the rules below).

```kif
// Result facts: `#[ensures(..)]` on the function. The result is known to have the property.
#[ensures(some)]                      // optional<T>: always holds a value, so `.value()` needs no check at the call site
fn first_free(t: &table) -> optional<u32> { .. }
#[ensures(len_ge(1))]                 // vector / slice / str: at least 1 element, so `[0]` needs no check
fn header(buf: &[u8]) -> &[u8] { .. }

// Parameter facts (preconditions): `#[..]` on the parameter. Callers must establish them; the callee may rely on them.
fn read(#[some] o: &optional<u32>,    // an optional that holds a value
        #[non_null] p: ptr<node>,     // ptr / c_ptr / c_cptr that is not null
        #[non_empty] v: &[u32],       // not empty (same as len_ge(1))
        #[len_ge(4)] w: &[u32],       // at least 4 elements, N is an integer literal
        #[lt_len(v)] i: usize)        // i < v.len(), `v` is another parameter (vector, slice, str or array)
        -> u32 { v[i] }               // with lt_len(v) this index needs no check

// `&mut` arguments normally lose every fact the caller knew (the callee may empty an optional, shrink a vector).
// `#[preserves]` promises it does not, so the caller keeps its facts across the call.
#[preserves]                          // every `&mut` parameter and `&mut self`
fn bump(v: &mut [u32]) { .. }
#[preserves(v)]                       // only these names; `self` names the receiver
fn fill(v: &mut [u32], n: u32) { .. }
```

| Attribute | On | Argument | Fits | Meaning |
|-----------|----|----------|------|---------|
| `some` | parameter | none | `optional<T>`, `&optional<T>`, `&mut optional<T>` | holds a value |
| `non_null` | parameter | none | `ptr<T>`, `c_ptr<T>`, `c_cptr<T>` | not null |
| `non_empty` | parameter | none | `vector<T>`, `&[T]`, `str`, `str_view` | at least one element |
| `len_ge(N)` | parameter | integer literal | as `non_empty` | at least N elements |
| `lt_len(v)` | parameter | another parameter's name | integer; `v` is a vector, slice, str or array | value is below `v.len()` |
| `ensures(f)` | function | `some`, `non_null`, `non_empty` or `len_ge(N)` | the return type must fit `f` as above | the result has the fact |
| `preserves` | function | none, or `&mut` parameter names (`self` for `&mut self`) | needs a `&mut` parameter | those arguments keep the caller's facts |

Rules and rationale:

- **Checked against the signature.** A fact on the wrong type is an error (KIFP211) rather than a silent no-op, so a rename or a
  type change cannot leave a dead hint behind. Unknown fact KIFP210, bad arguments KIFP212, a `lt_len` / `preserves` name that is
  not a suitable parameter KIFP213, `ensures` without a matching return type KIFP214.
- **Never trusted blindly.** A `requires` is established by the call site (a dominating test, a loop bound, a constant) or the
  callee's guarded entry checks it at run time; one that provably fails is a compile error. An `ensures` is checked once at each
  `return` of the callee (a run-time check, `result of f() violates #[ensures(..)]`, that the provers remove where they can show it
  holds), so callers may rely on it. A `preserves` on a function with a body is verified (KIFI030): the body must not write a field
  of the preserved referent, nor pass it to a callee that does not preserve it; writing one element is fine. Only a body-less
  declaration (the prelude's externs) is trusted, because it *is* the contract.
- **Why `preserves` is opt-in.** Exclusive `&mut` makes "who may change this" a local question, but a callee that takes `&mut`
  may change anything it can reach. Dropping facts at such a call is the safe default; `preserves` is the cheap, reviewable way
  to keep them.
- **Interior mutability never carries facts.** Cells, atomics and slots can change behind a shared borrow.

#### Standard-library facts (API attributes)

The prelude (`kiflang/prelude/reloco.kif`) describes its own checked operations with three more attributes, so a caller that
has proved a fact can skip the check. Any wrapper of a checked operation can use them; they are validated like the others
(KIFP212 to KIFP216) and likewise only hints.

```kif
extern "C++" impl<T> vector<T> {
    #[len_of]                       // returns the element count: `i < v.len()` is the fact lt_len(v)
    fn len(&self) -> usize;
    #[when(false, non_empty)]       // a bool method: when it returns false, the receiver has the fact (true/false, then a fact)
    fn is_empty(&self) -> bool;
    #[lang(index),                  // `v[i]` panics when out of range ...
     unchecked_form(unsafe_at, lt_len(i))]   // ... but may call this #[unsafe] sibling instead once `i < v.len()` is proved
    fn index(&self, i: usize) -> &T;
    #[unsafe] fn unsafe_at(&self, i: usize) -> &T;   // the named sibling: same parameters and result, must be #[unsafe] (KIFP216)
    #[allocates, preserves]         // growing keeps lt_len / non_empty facts about `self`
    fn try_push(&mut self, value: T) -> result<()>;
}
```

| Attribute | On | Meaning |
|-----------|----|---------|
| `when(true\|false, f)` | `bool` method with a receiver | `f` (`some`, `non_null`, `non_empty`) holds for the receiver when the result equals the first argument |
| `len_of` | `usize` method, no parameters | the result is the receiver's element count |
| `unchecked_form(name, f)` | method with a receiver | the call may become `#[unsafe]` method `name` when `f` is proved: `some`, `non_null`, `non_empty` for the receiver, or `lt_len(p)` for parameter `p` against the receiver's length |

What the prelude declares today:

| Type | `when` | `len_of` | `unchecked_form` | `preserves` |
|------|--------|----------|------------------|-------------|
| `optional<T>` | `has_value`: true gives `some` | | `value` to `unsafe_value` when `some` (`unsafe_value` is new) | |
| `ptr<T>` | `is_null`: false gives `non_null` | | `deref` to `unsafe_get` when `non_null` | `deref_mut` |
| `vector<T>` | `is_empty`: false gives `non_empty` | `len` | `index` to `unsafe_at` when `lt_len(i)` | `index_mut`, `try_push`, `try_reserve`, `try_insert_at` |
| `str` | `is_empty` | `len` | | `try_reserve`, `try_append`, `try_push`, `try_insert` |
| `str_view` | `is_empty` | `len` | | |

Operations that can shrink or empty the receiver (`reset`, `clear`, `try_pop`, `try_erase_at`, `try_resize`, `try_assign`) are
deliberately not `preserves`. An out-of-prelude type is described the same way; a `.kifm` precompiled module carries its
attributes with it.

Planned, not accepted yet (reserved so the names stay free): facts derived by the compiler itself from a dominating `if` /
`match` / early return (`if !o.has_value() { return; }`), from `i < v.len()` and from loop bounds, killed by an assignment, a
move, a `&mut` borrow or a call that does not `#[preserves]`; `#[invariant(..)]` on a type, for values that are non-empty or
non-null by construction (a `non_empty_vector`); `#[ensures(lt_len(v))]`; `#[pure]`; and `kif check --explain-checks`, which
lists each removed check with the fact that removed it. All of it works under the existing `prove` tier, with no source or target
changes. The analyses run on the mid-level IR (`kif ir`, [`kif-ir.md`](kif-ir.md)), which exists today, together with the basic check provers (`kif ir --prove`, `kif-ir.md` section 4). Nothing consumes
the proofs yet, so code generation is unchanged.

## 5. Generation-time language (`gen fn`)

`gen fn` is the same language plus the things that cannot be lowered. All of this is evaluated by the interpreter.

| Extra | Meaning |
| --- | --- |
| `str` (in `gen fn`) | Immutable UTF-8 string value (in lowered code `str` is the heap string of 4.14, with a smaller API): `+`, `==`, `.len()`, `.contains(s)`, `.starts_with(s)`, `.replace(a, b)`, `.upper()`, `.lower()`, `.split(sep)`, `.to_string()`, `parse_int(s)`. |
| `Vec<T>` | Growable array of generation-time values: `.push`, `.len()`, `v[i]`, `for x in v`, `.join(sep)` for `Vec<str>`. |
| `Option<T>` | `Some(x)` / `None`, `.is_some()`, `.unwrap_or(d)`, `match` on both. Returned by reflection lookups. |
| `i128`-free integers | Integers are 64-bit for both signed and unsigned, with the same wrapping rules; `parse_int` traps on overflow. |
| `format!("{} {:x}", a, b)` | String formatting. Placeholders: `{}`, `{:x}`, `{:08x}`, `{:?}` (debug form). The argument count is checked at generation time (KIFP040). |
| `fail!("msg")` | Report an error at the span of the item the generator was invoked on (KIFP102) and stop that generator. |
| `warn!("msg")` | Same, as a warning. |
| Reflection | The built-in item types of section 6. |
| `Emitter` | The output handle (section 6.2). |

A `gen fn` may call `fn`s (executed by the interpreter), but a `fn` may never call a `gen fn` (KIFP050): that would make the
lowered code depend on generation state.

## 6. Reflection and emission


### 4.19b Requiring proofs: `#[require_proved]`

A check the prover cannot discharge stays in the generated code and may panic at run time. `#[require_proved]` makes that a build
error (`KIFI011`) for the code it covers, for code that must not panic (an interrupt handler, a hot path):

```kif
#![require_proved(div)]            // a whole file: every division needs a divisor the prover knows is non-zero

#[require_proved]                  // an `impl` or one function: every check kind
fn at(t: [u8; 8], i: usize) -> u8 {
    t[i & 7]                       // proved: `i & 7` is at most 7, below the length 8
}

#[require_proved(index, shift)]    // only these kinds
fn mix(t: [u8; 8], x: usize, n: u32) -> u8 { t[x & 7] }
```

Scopes (file, `impl`, function) add up; kinds are `index`, `range`, `some`, `non_null`, `div`, `shift` (a bare attribute means all, an
unknown kind is KIFP217); it is an error rather than a warning because the point is a guarantee. Details and rationale:
docs/kif-ir-passes.md, "Requiring proofs".
### 6.1 Reflection API (read-only)

All reflection types are opaque handles; every accessor is pure. Names are the kif source names.

```
Module     : .name() -> str, .interfaces() -> Vec<&Interface>, .enums() -> Vec<&Enum>, .structs() -> Vec<&Struct>,
             .functions() -> Vec<&Function>, .cells() -> Vec<&Cell>, .find_interface(str) -> Option<&Interface>
Interface  : .name(), .doc() -> str, .version() -> u32, .extends() -> Option<&Interface>, .methods() -> Vec<&Method>,
             .attr(str) -> Option<&Attr>, .attrs() -> Vec<&Attr>, .is_cxx_only() -> bool
Method     : .name(), .doc(), .params() -> Vec<&Param>, .ret() -> &Type, .is_static() -> bool, .has_default() -> bool,
             .attr(str) -> Option<&Attr>, .index() -> u32        // position in the interface (stable per source order)
Param      : .name(), .ty() -> &Type
Type       : .kind() -> TypeKind, .name() -> str, .inner() -> Option<&Type>, .size_bits() -> Option<u32>,
             .c_spelling() -> str, .cpp_spelling() -> str
Attr       : .name(), .has(str) -> bool, .get(str) -> Option<str>, .get_int(str) -> Option<i64>
Enum       : .name(), .underlying() -> &Type, .members() -> Vec<&EnumMember>, .is_flags() -> bool
Struct     : .name(), .fields() -> Vec<&Param>
Cell       : .name(), .doc(), .kind() -> str ("tls" or "per_cpu"), .ty() -> &Type, .is_slot() -> bool, .value_ty() -> &Type,
             .is_atomic_ok() -> bool, .attr(str) -> Option<&Attr>, .attrs() -> Vec<&Attr>    // `#[tls]`/`#[per_cpu]` statics (4.17)
```

Implemented in `kiflang/reflect.py` (`module_handle(mod)` makes the `Module` a generator receives; the type names above are
accepted in `gen fn` signatures). Details of the current implementation:

- `.doc()` is the `//`/`///` comment lines directly above the declaration (attributes in between are skipped), joined by `\n`.
- `Struct.fields()` yields `Param` handles (`.name()`, `.ty()`); `EnumMember` has `.name()`, `.doc()`, `.value() -> Option<i64>`
  (explicit literal values only) and `.attr(str)`.
- `Type.kind()` is one of `"int"`, `"bool"`, `"ptr"`, `"str"`, `"named"`, `"ref"`, `"slice"`, `"array"`, `"fn"`, `"tuple"`,
  `"unit"` (a `str` for now, compare with `==`). `.c_spelling()`/`.cpp_spelling()` map scalars to `<stdint.h>` names, `ptr<T>`
  to `T *` and `&T` to `T *` (C) or `T &` (C++); other names are returned as written.
- `Attr.has(k)`/`.get(k)`/`.get_int(k)` look at the `k` or `k = value` arguments of the attribute. `Interface.version()` is the
  `#[version(n)]` attribute, default 1; `.extends()` follows `#[extends(base)]`.
- An accessor a handle type does not have traps with KIFP101.

Convenience renderers that return source text so generators do not rebuild declarations by hand:
`m.params_decl()` (`a: u32, b: u64`), `m.params_names()` (`a, b`), `m.signature()`, `t.c_spelling()`.

### 6.2 Emission

The `Emitter` appends output. It has four channels:

| Call | Effect |
| --- | --- |
| `out.kif(fmt, args...)` | The text is **parsed and checked as kif** and added to the current module after the item the generator was invoked on (so it can declare `fn`s, methods, structs, enums). Errors are reported with a note pointing at the generator. This is the preferred channel: whatever it adds is lowered and ABI-checked like handwritten code. |
| `out.c(fmt, args...)` | Raw text appended to the generated C header/source (`<stem>_if.h` for the FreeBSD backend). Ignored by the C++ backend. |
| `out.cpp(fmt, args...)` | Raw text appended to the generated C++ header. Ignored by the C backend. |
| `out.doc(fmt, args...)` | Text appended to the generated documentation. |

Output is deterministic: no timestamps, no host paths, no iteration over unordered collections. Two runs on the same input
give byte-identical output (a requirement of the manifest and hash mechanism).

## 7. Where the language plugs in

### 7.1 Method bodies (default implementations)

```kif
interface timer {
    method read() -> u64;

    // Today: `method now() -> u64 default timer_now_default;` plus a `code { ... }` block of C.
    // New: write the body in kif. Both backends get a native function for it.
    method now() -> u64 {
        self.read()
    }
}
```

- `method name(params) -> type { body }` means "this method has a default" and replaces `default <symbol>` for new code;
  `default <symbol>` stays for already existing C (`.m` import still produces it).
- `self` has the interface's object type. The only things callable on it are the interface's own methods (and base
  interface methods): `self.read()`.
- C backend: the body becomes `static` function `<if>_<method>_default(<self_type> self, ...)` in `<stem>_if.c`, and the
  kobj method table points at it exactly like a `default <symbol>`. C++ backend: it becomes the default of the
  corresponding method in the generated base class (`virtual` or CRTP exactly as for `default` today).
- C++ backend: the method is a **real C++ member function**, not a free function with a `self` argument. `self` is `*this`,
  `self.read(base, 8)` is `this->read(base, 8)` (a base-interface method is called the same way), and the generated
  member is `noexcept`, `[[nodiscard]]` for non-unit results, and carries the reloco annotations of the signature.
- ABI: the manifest records only the signature and `has default`, never the body.

### 7.2 Hooks: tracing and checks around methods

```kif
// Replaces the raw-C m_prolog/m_epilog for new code. `before`/`after` bodies are kif and
// are lowered into the C wrapper (and the C++ call wrapper), so they also work with C++.
interface clock {
    // `before`/`after` apply to the NEXT method only, like m_prolog/m_epilog do today.
    before { trace_enter("clock.gettime", ts as usize); }
    after  { trace_leave("clock.gettime", result as usize); }
    method gettime(ts: c_ptr<timespec>) -> i32;
}
```

- Inside `before`: the method's parameters (read-only) and `self`. Inside `after`: the same plus `result` (absent for `void`).
- **Hooks never change control flow.** A `before` cannot skip the call and a hook cannot change the result: `return` is not
  allowed in a hook (KIFP071) and `after` sees `result` read-only. A hook that refuses bad arguments uses `panic!("...")`. The
  `.m` bridge's `m_prolog`/`m_epilog` are raw C and can do anything; they are not kif hooks. Mocks ignore hooks.
- A hook may call `fn`s and `extern` functions (4.6), under the same `unsafe` rules as any other body.
- C++ backend: the hook code is emitted inside the generated C++ method itself (the member that the caller invokes, with
  the hook bodies around the call into the implementation), so tracing and checks are ordinary C++ methods too; there is
  no separate wrapper object. `before`/`after` locals and parameters are the member's own.
- `#[trace]`-style attributes (7.3) generate hooks for a whole interface.

### 7.3 `#[derive(name)]`: customising generation

```kif
// Generator signature is fixed: `gen fn derive_<name>(item: &<ItemType>, out: &mut Emitter)`.
// The ItemType decides what the attribute may be put on: Interface, Enum, Struct or (later) SyscallTable.
gen fn derive_stats(iface: &Interface, out: &mut Emitter) {
    let mut n = 0;
    for m in iface.methods() {
        if m.attr("count").is_some() {
            out.c("#define {}_{}_COUNTED 1\n", iface.name().upper(), m.name().upper());
            n += 1;
        }
    }
    if n == 0 { warn!("no #[count] methods on {}", iface.name()); }
}

#[derive(stats)]
interface dev {
    #[count] method probe() -> i32;
}
```

- `#[derive(a, b)]` runs the generators in the written order, after the file has been checked and before backends run.
- Generators are found among the `gen fn`s of the file and of its imports (a library ships its generators as a `.kif`
  file the user imports). Unknown derive: KIFP060. Wrong item type: KIFP061.
- Attribute arguments reach the generator through `item.attr("name")` (e.g. `#[derive(stats), counter_prefix(dev)]`).
- A generator cannot modify existing declarations, only add: this keeps the declared interface the source of truth and the
  manifest meaningful.

### 7.4 Syscall tables (motivating future use)

```kif
// The syscalltable item (separate design) exposes its entries through the same reflection API, so
// "emit the sysent table in my kernel's layout" is an ordinary derive:
gen fn derive_my_sysent(t: &SyscallTable, out: &mut Emitter) {
    out.c("struct sysent my_sysent[] = {\n");
    for c in t.calls() {
        out.c("  {{ .sy_narg = {}, .sy_call = (sy_call_t *)sys_{} }},\n", c.params().len(), c.name());
    }
    out.c("};\n");
}
```

## 8. Sandbox and limits (generation time)

This section applies to `gen fn`, `const` evaluation and generators run by the Python interpreter. Lowered code (`fn`, method
bodies, hooks) is not sandboxed: it is kernel code and uses C/C++ freely (4.6).

- No I/O: no files, environment, network, clock, randomness, process access. The interpreter has no way to reach them.
- Step limit: 10,000,000 evaluated nodes per generator (`--proc-steps N` to change); exceeded: KIFP103.
- Call depth 64, `Vec` length 1,000,000, string length 16 MiB, emitted output 64 MiB per generator.
- Integer arithmetic traps and everything in section 4.3 apply (a trap is KIFP100).
- Determinism (section 6.2). Hash randomisation is disabled; reflection collections come in source order.
- Scripts do not run in the language server on every keystroke: the LSP checks and type-checks `fn` and `gen fn`
  bodies, and only runs generators on demand (a "kif: run generators" command), with the same limits.

## 9. Lowering

### 9.1 C

**C is an explicit choice; the default is C++.** A file or project opts in with the module attribute `#![target(c)]` (or
`kifc --target=c`, a CMake option). In C mode the checker restricts the language to what C can represent, **while checking**, so
every unsupported construct is reported at its source line (KIFP053, naming the construct), including the ones that would be
fine in a C++ file: no generics, no interface impls. Without the opt-in, C is not generated from procedural
code. The full contract (files, names, ABI, lowering, what each target emits) is `docs/kif-c-backends.md`; the subset is:

| Supported in C | Not supported in C (KIFP053) |
| --- | --- |
| free `fn` (not generic), `#[export]`, `extern "C" fn/macro`, `fn` pointers | generic `fn`, `extern "C++"`, `cfg(cxx)`-only code, `ptr<T>`, `allocator_ref`, any other prelude type that is not listed here |
| interface method default bodies and `before`/`after` hooks (section 7) | `impl ... for` a plain interface unless it is `#[ops]` (docs/kif-c-backends.md 5.1) |
| structs and enums, **all lowered as `repr(c)`**; arrays (plain C arrays), `match`, loops, panics; `result<T>`, `?`, `ok`, `err` (an `int` return plus a `T *kif_out` out-parameter, 3.2 of the C backend doc) | the `error` payload of `result<T>` (it is an `int` errno in C, so `match` on `err(e)` is refused) |
| `span<T>`/`str_view` as `{ptr, len}`; `str` as `{char *, len, cap}`; `vector<T>` on the target allocator | `drop` by hand (refused: the compiler drops values itself, 3.2 of the C backend doc); `vector<S>` of structs that own resources |
| struct `impl` blocks: methods and `drop` become plain functions `S_name(struct S *self, ..)`; the compiler calls `drop` for you (auto-drop, see below); `S_drop` stays public for C callers | `impl` generics, interface-typed fields |
| construction protocol: `S_INIT`, `S_init`, `try_construct_at(mem: c_ptr<Self>, ..)` for C-owned memory, heap `S_try_new`/`S_delete` | |
| `unsafe`, volatile/atomic built-ins | |

Generated C includes `kif_c.h`, generated from the target by kifc (3.0 of the C backend doc); no runtime library.
In C the compiler inserts the drops (scope end, early exits, moves, reassignment), as the C++ output does with destructors; kif code never calls `drop` (3.2 of the C backend doc).

A file written for C++ and then built in C mode fails with a precise list of C-incompatible items; the `.m` bridge and `--freebsd`
output of plain interfaces are unaffected by the choice.

Lowering rules for the supported subset:

- `fn f(a: u32) -> u64 { ... }` becomes `uint64_t f(uint32_t a) { ... }` in `<stem>_proc.c` with an `extern` prototype in `<stem>_proc.h`;
  `#[inline]` makes it `static __inline` in the header instead. `#[ops]` thunks, `const` vtables and default bodies are in the `.c` too.
- Names: `<stem>_<name>` for free functions (the stem prefix avoids kernel-wide clashes; `#[no_prefix]` opts out).
- Locals are declared at first use (C99); `match` is `switch`; `for` is a counting `for`; `loop` is `for (;;)`.
- Expressions that need a statement (blocks as values, `match` as a value) are lowered into a temporary declared just before
  the statement, in order; evaluation order is always left to right (kif defines it; the C is written so the result does not
  depend on the compiler's order of evaluation).
- Source lines are preserved with `#line` directives (switchable), so compiler errors and debugger steps point at the `.kif`.

### 9.2 C++

**Language standard: C++20.** Code lowered from the procedural language (free `fn`s, struct `impl` blocks, `impl iface for S`,
hooks) requires C++20 and the generator may use it freely: concepts for generic bounds (4.9), designated initialisers for
struct literals, `consteval`/`constexpr` for constants, `<=>` for derived ordering and `[[likely]]`/`[[unlikely]]`. The
existing generated interface headers and the `include/kif` runtime stay C++17, so a project that uses only interfaces is
unaffected; a header with procedural code states its minimum standard in a `#if __cplusplus < 202002L` `#error`. The CMake
helpers set `cxx_std_20` on targets that use `.kif` files with procedural items. The existing tracing support (`kif/kobj/trace.hpp`) already needs C++20, so there is precedent.

The C++ output is meant to be idiomatic, reloco-style C++ that **passes `reloco-tidy` without suppressions**, except
inside explicit `unsafe` regions. The generator is free to use anything in `reloco` and `microfmt`; whenever one of them offers
a facility, it is used instead of the STL or a hand-written construct. The generated code follows reloco's
`docs/lifetime-safety.md`, `docs/hardened-containers.md`, `docs/safety-checker.md` and `docs/extending.md`.

| kif | Generated C++ |
| --- | --- |
| trap (4.3), bounds, bad enum cast | `RELOCO_TRAP()`; where a message helps, `RELOCO_ASSERT_MSG` / `RELOCO_ASSERT_LOC` with the `.kif` file and line (`#line` is also emitted) |
| `a + b` etc. on integers | `reloco::wrapping_add`, `wrapping_sub`, `wrapping_mul`, and the shift/division/negation helpers of `reloco/int_ops.hpp` (`checked_div`, `checked_rem`, `checked_neg`, `checked_cast`); a failed check goes to `RELOCO_TRAP()`. `reloco::wrapping<T>` is not used for locals, to keep types plain |
| `checked_add(a, b)` and friends (built-ins) | `reloco::checked_*` returning `reloco::result<T>`; `kif` unwrapping (`.unwrap()` in kif) lowers to `reloco::result`'s `unwrap()`, never a bare `*` |
| `a as T` between integer types | `static_cast` when the conversion cannot lose information; otherwise the wrapping conversion is spelled out explicitly (kif's `as` truncates by definition, so it is emitted as a masked/`wrapping` conversion, not a bare cast); `checked_cast<T>(x)` is a kif built-in that lowers to `reloco::checked_cast<T>` |
| `[T; N]`, `a[i]` | `reloco::array<T, N>` with the checked accessor (hardened-container tier "checked"; the bounds failure is `RELOCO_TRAP()`) |
| `&[T]` / `&mut [T]` (slice; an array, `vector`, `str` borrowed) | `reloco::span<const T>` / `reloco::span<T>`; `[char]` also `reloco::string_view` |
| `&T`, `&mut T` parameter | `const T &`, `T &`; `RELOCO_LIFETIMEBOUND` when a result may alias the parameter; no rvalue access on aliasing accessors (`&`/`const &` qualifiers or `RELOCO_BLOCK_RVALUE_ACCESS`) |
| `c_ptr<T>`, `c_cptr<T>` | raw `T *` / `const T *` only for `unsafe` code and for `extern "C"` interop; the region is wrapped in `RELOCO_BEGIN_UNSAFE_CODE` / `RELOCO_END_UNSAFE_CODE`. Safe code receives `reloco::span`/references instead |
| `call_location` | `reloco::call_location_ref::current()` as a trailing default argument of generated assertion-style helpers |
| `format!` in generated tracing | `microfmt` formatting; the generated code includes only the `microfmt` headers it uses |
| `fn` result of kind `cresult<T>` | `reloco::result<T>` / `reloco::error_errno` as appropriate for the interface's `errno` style |
| `unsafe { ... }` | `RELOCO_BEGIN_UNSAFE_CODE ... RELOCO_END_UNSAFE_CODE` around exactly that block |
| generated types | carry reloco safety annotations (`RELOCO_POINTER`/`RELOCO_OWNER`, `RELOCO_LIFETIMEBOUND`, `RELOCO_LIFETIME_CAPTURE_BY_THIS`, ...) where the declaration warrants them; `[[nodiscard]]`, `noexcept`, `constexpr` where valid |

- Locals are always initialised (`auto x = ...;` or `T x{};`): the checker's "uninitialised local" rule never fires.
- No C arrays, no `new`/`delete`, no C casts, no `memcpy` of non-trivial types; copies of non-trivially-copyable data are explicit.
- Method bodies and hooks become proper C++ member functions of the generated interface classes (sections 7.1 and 7.2):
  `self` is `*this`, calls on `self` are member calls, and `return` in a body is a plain C++ `return`. Only free `fn`s
  (no `self`) become namespace-scope `inline` functions.
- Free `fn`s are `constexpr` when the body only uses constexpr-able operations (`reloco::array`, wrapping and checked
  helpers are), so the same function can be evaluated by the compiler.
- **Verification:** the generator's own output for the test corpus is compiled with `-Wall -Wextra -Werror` (clang++-24, also
  `-Wdangling-gsl -Wlifetime-safety`) and checked with `reloco-tidy`; the build fails on any finding. The "within reason"
  exceptions are the generated `unsafe` regions and `extern "C"` declarations, which carry `RELOCO_BEGIN_UNSAFE_CODE` or a
  generated `// NOLINT(reloco-...)` with the reason, never a blanket suppression.

### 9.3 What lowering refuses

A `fn` that uses anything from section 5 (the generation-time string methods, `Vec`, `format!`, reflection, `Emitter`, a `gen fn` call): KIFP050. A `fn`
whose body would need more than the target allows (recursion, 64-bit division on a 32-bit-only target flagged with
`#[target(no_i64_div)]`): KIFP032 / KIFP051.

## 10. Diagnostics (planned)

Prefix `KIFP`, so they never collide with `KIFnnn` or `KIFLnnn`.

| Code | Meaning |
| --- | --- |
| KIF001-007 | Lexical errors: unterminated string (001) or verbatim body (002), invalid identifier (003), invalid character (004), stray `#` (005), bad number (006), bad escape (007). KIF001 is also the generic syntax error. |
| KIF010 | Duplicate name in a scope (items, `use` bindings, parameters, generic parameters, lambda parameters). |
| KIF012 | `import` path not found, or an import cycle. |
| KIFP016 | `use` or a qualified path names a `private` item of another module. |
| KIF038 | A number is expected and something else is written (e.g. interface `version`). |
| KIF090 | Unknown language tag in `header(..)` / `code(..)` (only `c` and `cxx`), or a tag on `m_prolog` / `m_epilog` (always C). |
| KIF088 | `m_prolog` / `m_epilog` with no method after it in the interface: a hook applies to the NEXT method only, so it would never run. |
| KIF089 | The same hook (`m_prolog` or `m_epilog`) twice before one method; the first would be silently replaced. |
| KIF086 / KIF087 | Attributes on something that takes none; an old-style clause (`interface x cxx_only {`) instead of attributes. |
| KIFP001-009 | Lexical and syntax errors in `fn`/`gen fn`/`method` bodies. |
| KIFP010 | Unknown name or function. |
| KIFP011 | Wrong number or type of arguments. |
| KIFP012 | Type mismatch (no implicit conversions). |
| KIFP013 | Literal does not fit its type. |
| KIFP014 | Struct literal with a missing, unknown or repeated field. |
| KIFP020 | Assignment to an immutable variable. |
| KIFP021 | Reference outlives its referent (borrow checker). |
| KIFP033 | A reference is used after the value it borrows was modified, moved or borrowed incompatibly; conflicting borrows among the arguments of one call. |
| KIFP160 | Undeclared lifetime. |
| KIFP161 | Reference (or lifetime-parameterised type) in a struct field without a named lifetime. |
| KIFP162 | Result lifetime cannot be elided. |
| KIFP170 | Lambda uses a local of the enclosing function (it is non-capturing). `bodies.py`. |
| KIFP022 | Use of a possibly unassigned variable. |
| KIFP030 | Non-exhaustive `match`. |
| KIFP031 | A path of a non-unit function does not return. |
| KIFP032 | Recursion in a lowered function. |
| KIFP040 | `format!` argument count or placeholder error. |
| KIFP041 | `?` on a value that is not a `result`, or in a function that does not return one. |
| KIFP050 | Generation-time feature used in lowered code. |
| KIFP051 | Construct not available for the target, e.g. a `#[kobj]` interface when `--target` selects a C target without a `kobj` section (`cmode.py`). |
| KIFP015 | `private` field, method or struct literal naming a private field used outside an `impl` of the struct. |
| KIFP023 | Use of a moved value. |
| KIFP024 | Move out of a reference, of a borrowed value, or partial move out of a struct with `drop`. |
| KIFP025 | Scalar, pointer or enum field of a struct with `drop`/`try_construct` has no default. |
| KIFP029 | Field type is not movable. |
| KIFP026 | `impl <interface> for S`: missing, mismatching, ambiguous or unknown method or interface. |
| KIFP027 | Duplicate method or associated function in the `impl` blocks of one struct. |
| KIFP028 | `impl <interface> for` a `#[copy]` struct, or `#[copy]`/`#[repr(c)]` on a struct that cannot have it. |
| KIFP053 | Construct not supported by the C backend (section 9.1): generic `fn` or `impl`, `impl` of a plain interface that is not `#[ops]`, `extern "C++"`, `allocator_ref`/`arc`/`weak`/`rc`/`unique`, `write!` and the logging macros. C mode only; items under a `#[cfg]` that excludes C are skipped. `cmode.py`. |
| KIFP087 | Layout or ops attribute misused: `#[typedef]` off a struct/enum or with arguments, `#[align(n)]` off a struct or `n` not a power of two, `#[repr(..)]` other than `c`/`packed` on a struct (or on an enum), `#[ops]` on a `#[kobj]` interface, a non-interface or with arguments. `cmode.py`. |
| KIFP086 | A construction-protocol function with the wrong shape: `try_create`/`try_allocate` (no receiver, `result<Self>`), `try_clone` (`&self`, `result<Self>`), `try_construct` (`&mut self`, `result<()>`), `try_construct_at` (`mem: c_ptr<Self>` first, `result<()>`). All modes. `cmode.py`. |
| KIFP110 | Sleepy call (or body) in atomic context. |
| KIFP111 | `#[atomic_only]` function called outside atomic context. |
| KIFP112 | Atomic scope entered without being left on every path. |
| KIFP113 | `#[atomic_scope]` value moved out of its block. |
| KIFP130 | Wrong number or kind of generic arguments to an `extern "C++" type` or alias. |
| KIFP131 | `#[unsafe]` member of a wrapped type used outside `unsafe`. |
| KIFP132 | Unknown `#[lang(..)]` item. |
| KIFP133 | Wrapped type used as a field without a declared `drop`/move guarantee. |
| KIFP140 | kobj-only attribute or feature (`self`, `softc`, `errno`, `version`, C/`.m` output, `foreign class`, `implements`, `super<>`) on an interface without `#[kobj]`. |
| KIFP141 | `extends` between a plain and a `#[kobj]` interface. |
| KIFP142 | An interface extends itself, directly or through its bases. |
| KIFP180 | `#[tls]`/`#[per_cpu]` cell type is not zeroable (4.17). |
| KIFP181 | Cell initialiser is not a constant, or the type is not trivially copyable as an image. |
| KIFP182 | A `#[per_cpu]` cell has a type with `drop` (it would never run). |
| KIFP183 | TLS cell accessed from a TLS `drop`. |
| KIFP184 | `with_mut` of a per-CPU cell that an `#[atomic_entry]` function can also reach. |
| KIFP185 | A `with`/`with_mut` closure result borrows from the cell. |
| KIFP186 | A per-CPU `slot` init function allocates (calls an `#[allocates]` function), directly or transitively. |
| KIFP187 | `slot<T>` used anywhere except as the type of a `#[tls]`/`#[per_cpu]` static. |
| KIFP188 | A `static` without exactly one of `#[tls]` and `#[per_cpu]`. |
| KIFP189 | A TLS cell accessed in an `#[atomic_entry]` body (unless `#[tls(atomic_ok)]`). |
| KIFP200 | A malformed `#[cfg(..)]` / `cfg(..) { }` predicate (docs/kif-targets.md, "Code variants"). |
| KIFP201 | An item that exists only under a `cfg` predicate used where it may not exist. |
| KIFP210 | Unknown fact attribute argument (4.19a). |
| KIFP211 | A fact attribute on a type it does not fit. |
| KIFP212 | Bad arguments to a fact attribute. |
| KIFP213 | `lt_len` / `preserves` names a parameter that does not qualify. |
| KIFP214 | `ensures` / `when` / `len_of` without a matching return type. |
| KIFP215 | `when` / `len_of` / `unchecked_form` on a function without a receiver. |
| KIFP216 | `unchecked_form` names a method that is not an `#[unsafe]` sibling in the same `impl`. |
| KIFP190 | A cell named by a `gen fn` (the interpreter has no cells). |
| KIFP191 | A cell used as a value instead of through `with`/`try_with`/... |
| KIFP150 | Format string is not a literal or `const cstr` (outside `unsafe`). |
| KIFP151 | Number of format fields does not match the arguments. |
| KIFP152 | Argument (or `#[Debug]` field) type is not formattable. |
| KIFP090 | `#[derive(clone)]` on a struct with a field that is not cloneable. |
| KIFP120 | `str` converted to `str_view`/`cstr` outside a direct `extern` call argument, or the converted value stored. |
| KIFP121 | `str_view` converted to `cstr`. |
| KIFP122 | `str_view` used as a struct field, or `str`/`str_view` in a method of a C-visible interface. |
| KIFP085 | `#[unsafe(relocatable)]` on a `#[heap]` struct or on a struct that implements an interface. |
| KIFP084 | A single local or by-value parameter is larger than the `small_stack` limit. |
| KIFW010 | The stack depth of a function (its frame plus the deepest callee chain) is over its `small_stack` budget (docs/kif-stack.md). |
| KIFW015 | A function with its own `#[small_stack]` whose frame cannot be sized (a generic) is not checked against it (note). |
| KIFW016 | `#[small_stack]` takes one positive byte count or `off`. |
| KIFP080 | `#[heap]` struct used by value as a local, parameter, result or field of a non-heap struct. |
| KIFP081 | Move of a `#[heap]` struct value. |
| KIFP082 | `#[heap]` struct with a by-value factory (`try_create`/`try_allocate`/`try_clone` returning `Self`), or without `try_construct`. |
| KIFP083 | `Self { .. }` literal of a `#[heap]` struct. |
| KIFP071 | `return` inside a `before`/`after` hook. |
| KIFP054 | Value with a `drop` goes out of scope in C-lowered code (no automatic call). |
| KIFP060 | Unknown `derive`. |
| KIFP061 | `derive` on an item of the wrong kind. |
| KIFP064 | A generic body does something its type parameter's bounds do not provide (operator or method). |
| KIFP062 | Generic argument does not satisfy its bound. |
| KIFP063 | Type parameter of a generic `fn` cannot be inferred. |
| KIFP070 | `unsafe` operation outside an `unsafe` block. |
| KIFP052 | `gen fn` or `const` evaluation reaches a function that uses `unsafe` or `extern`. |
| KIF013 | An `import` path is not allowed with `--sysroot` (absolute, `..`, `\\`, a symlink out of the sysroot, or `x.kif` and `x.kifm` both present), or the sysroot has no `reloco.kif`. |
| KIF014 | A `.kifm` is malformed, truncated, modified (digest mismatch) or of a newer format version. |
| KIF016 | A `.kifm` was compiled for another language (`c` vs `cxx`) than the importing build: its object code cannot be linked. |
| KIF017 | Warning: a `.kifm` was compiled for another target of the same language; its barrier snippets and overflow behaviour are the vendor's. |
| KIF018 | A `.kifm` carries a signature that does not verify (the file changed after signing). Always an error. |
| KIF019 | A `.kifm` is unsigned, or signed by a key the consumer did not pin (`--trust`). Warning under `--signing warn` (default), error under `require`, silent under `off`. Names the import path through which the file was reached. |
| KIF020 | A `.kifm` is signed by a revoked key or certificate (`--revoked`). Always an error. |
| KIF021 | A signature has to be checked but the host has neither the Python `cryptography` package nor OpenSSL 3, or the trust or revocation list cannot be read. Always an error; never "accept because crypto is missing". |
| KIFI040 | `#[require_proved]` calls into a `.kifm` function whose recorded `kept_checks` holds a demanded kind, or that has no recorded analysis. |
| KIFI041 | The `.kifm` was proved by another compiler (`compiler.prover` differs). Warning; error with `--prover-must-match`. |
| KIF015 | A precompiled module was built against a different version of one of its imports. |
| KIFP017 | A `fn` without a body in a source file (only `.kifm` modules declare functions that way). |
| KIFP100 | Trap while evaluating (overflow-checked operation, index, division, shift, bad enum cast). |
| KIFP101 | Memory access at generation time. |
| KIFP102 | `fail!` raised by a generator. |
| KIFP103 | Step, depth or size limit exceeded. |

Lint rules (`KIFL`, off by default unless noted): a `fn` never called (warning), a `gen fn` never used by a `derive`
(warning), `unsafe` block larger than N statements (off), shadowed `let` (off).

## 11. Interaction with existing constructs

| Existing | Becomes |
| --- | --- |
| `default <c_symbol>` | Stays (needed for the `.m` importer). `method ... { body }` is the preferred form for new code. |
| `code { ... }` / `header { ... }` | Stay as raw C for things the language cannot say (inline asm, macros). |
| `m_prolog` / `m_epilog` | Stay as the `.m` bridge. `before`/`after` (7.2) are the kif-native equivalent; the importer may later convert simple blocks. |
| `#[flags]`, `cresult<>`, `errno` | Unchanged; usable from bodies (`cresult_ok(x)` etc. as built-in functions). |
| `kif fmt` | Formats the new syntax with Rust-style 4-space indentation per level; verbatim blocks are untouched as today. |
| `kif lint` | Gets the rules above; ABI manifests ignore bodies. |
| `kif graph` | Unchanged. |

## 12. Testing strategy

- **Differential tests**: a corpus of small `fn` programs with expected results is (1) run by the interpreter, (2) compiled
  to C with `cc` and run, (3) compiled to C++ with `c++` and run. All three must agree, including traps (each trap test
  checks the exit through `KIF_PANIC`). Overflow, shift, division, array index and enum-cast edge cases get dedicated cases.
- Checker tests per diagnostic code (one positive and one negative case each).
- Sandbox tests: infinite loop, deep recursion, huge `Vec` and huge output all fail with KIFP103.
- Determinism test: two runs, byte-identical outputs.
- Round-trip tests: `out.kif(...)` output is formatted by `kif fmt` and re-checks clean.
- Generation tests: `derive` on interface/enum/struct; unknown derive; wrong kind; ordering.
- Existing suites must stay green: lowering only activates for the new constructs.

## 13. Implementation plan

1. Lexer/parser: expressions, statements, `fn`, `gen fn`, `const`, `extern`, `method` bodies, `before`/`after`. AST in
   `kiflang/ast.py`, parser in `kiflang/parser.py`. **Done** for everything except the `kif-editor` grammar: the new
   `kiflang/` package (see its README) holds lexer, AST and recovering parser; the interface-only front end moved to
   `kiflang-old/` and is ported in later steps.
2. Checker: name resolution, types, mutability, definite assignment, exhaustiveness, recursion, `unsafe`; KIFP codes.
   Done: declarations, modules and lifetimes (`symbols.py`); body scopes, `KIFP010`, `KIFP020` for locals and `KIFP170`
   (`bodies.py`). Expression types (`types.py`, gradual: only plain data is strict): `KIFP011-014`, `KIFP031`.
   Methods and impls (`methods.py`): `x.m(..)` and `Type::f(..)` resolve through `impl`, `extern "C++" impl` and the methods an
   interface provides; `KIFP026-028`, `KIFP020` through fields, derefs and `&mut self` calls.
   Moves and definite assignment (path-sensitive through `if`, `match`, loops, `break`/`return`): `KIFP022-024`.
   Borrows: `&x`/`&mut x` and auto-borrowed receivers create loans carried through `let`, assignment, literals and call
   results (by the result's lifetime); `KIFP021` (returned/stored reference to a local, use after the referent's block) and
   `KIFP033` (use after a conflicting write/move/borrow, flow-sensitive like NLL). A loan names the part it borrows, so `&mut s.a`
   with `&mut s.b`, or `&mut a[0]` with `&mut a[1]` (two different constant indexes), do not conflict; the same part, a variable index
   or the whole value does (`kiflang/places.py`). Not tracked: loop-carried loans, borrows
   through `if`/`match`/block results, associated-function calls, reborrows through references.
   Exhaustiveness (`KIFP030`): enums need every enumerator, `bool` both values, integers the whole range (literals and
   `a..=b` ranges are merged, so `0..=127` plus `128..=255` covers `u8`), anything else a `_` arm; `|` alternatives count.
   Recursion (`KIFP032`): a call graph over `fn`/`method` bodies (calls, `Type::f` and function values); direct and mutual
   cycles are reported at the call site in each member, `gen fn` is exempt. Calls through `fn` pointers and interfaces
   are not followed.
   `unsafe`: calling a `#[unsafe]` function or method outside an `unsafe` block is `KIFP131`; calling an `extern "C"`
   function that is not `#[safe]` (variadics never are) or dereferencing a `c_ptr`/`c_cptr` is `KIFP070`. The body of a
   `#[unsafe]` function is itself an unsafe context.
   Atomic context (`KIFP110-113`, `bodies.py`): a body is atomic when it is `#[atomic_entry]`/`#[atomic_only]` (also through the
   interface method it implements) or takes a `&token` of an `#[atomic_token]` struct; inside a body, a local of an
   `#[atomic_scope]` struct and the span between `#[enter_atomic]` and `#[leave_atomic]` calls are atomic. Sleepiness is
   inferred over the call graph from `#[might_sleep]` leaves (`#[atomic_exempt]` stops it). Not yet checked:
   `#[atomic_callback]`, `#[atomic_lock]`, `#[might_sleep]` on an `impl`, hooks and calls through `fn` pointers.
   Step 3a (interpreter, plain-data subset: integers with wrap/trap semantics, structs, enums, arrays, references, methods, lambdas, `match`, loops, `panic!`, step/depth limits KIFP100/101/103) is in `kiflang/interp.py`; the checker annotates every expression with `.ty`.

Step 3b (generation-time built-ins, `kiflang/interp.py`): `str` (a Python string; `len` counts UTF-8 bytes, `+`, `contains`,
   `starts_with`, `ends_with`, `replace`, `upper`, `lower`, `split`, `find`, `trim`), `Vec<T>` (`Vec::new`, `push`, `pop`, `len`,
   `join`, `for`), `Option<T>` (`Some`, `None`, `is_some`, `unwrap`, `unwrap_or`, `match` with `Some(x)`/`None` patterns, which
   the parser reads as `VariantPat`/`BindPat` and the checker requires to be exhaustive), `parse_int(s)` (returns an integer;
   a malformed or out-of-range string traps, KIFP100), `format!` with `{}`, `{:x}`, `{:08x}`, `{:?}` (placeholder and argument
   mismatches are KIFP040), `fail!` (KIFP102, stops the generator), `warn!` (collected in `Interp.warnings`), and the
   `Vec` length and string length limits (KIFP103). Not yet: reflection and `Emitter`.

Results: `result<T>` (the `#[lang(result)]` alias) keeps its payload type, `ok(v)`/`err(e)` build one (the payload of `ok` is
   checked against the declared `T`, which also types untyped literals), `e?` unwraps or returns the error (KIFP041 when the
   operand or the enclosing function is not a result; lambdas are checked loosely), `match` takes `ok(x)`/`err(e)` arms and
   must cover both (KIFP030). The interpreter has `Res` values, `error::name` values, `is_ok`/`is_err`/`unwrap`/`unwrap_or`
   and `str.try_clone()`.

Generic bounds: see 4.9 (KIFP062-064); calls through `T: Interface` resolve to the interface's methods.

Interface `extends` (`methods.py`: `base_of`, `chain`, `inherited`; `check_interfaces`): KIFP010/141/142, a missing base method is KIFP026.

Precompiled modules and `--sysroot` are done (section 15). Pre-codegen language features are complete; TLS/per-CPU cells are checked (4.17, `kiflang/cells.py`); their lowering and the reloco classes are not written. Next: code generation (Emitter, C/C++ lowering), only when asked.
3. Interpreter (`kiflang/interp.py`): values, traps, limits, built-ins, strings/`Vec`/`Option`, `format!`.
4. Lowering to C and C++ (`kiflang/lower_c.py`, `lower_cpp.py`) and `kif/proc.h`; differential test harness.
5. Method default bodies and `before`/`after` hooks in both backends.
5a. Struct `impl` blocks, move checking, reloco-style construction and `impl <interface> for` (C++ first; C subset after).
6. Reflection API, `Emitter`, `#[derive]` and the generator driver in `kifc`; docs and manifests.
7. Tooling: formatter, lint rules, LSP (diagnostics, hover, completion, go to definition for `fn`), VS Code and other
   editor grammars (`kif-editor/gen_syntax.py` keyword groups), CHANGELOG.
8. First consumer: the `syscalltable` item and its derives (separate design).

## 14. Decisions and open questions

Decided with the design review:

1. Everything is public by default; `private` is optional (KIFP015).
2. Panics carry a message: `KIF_PANIC(file, line, "msg")`, mapped to reloco's assert/kernel-panic/trap in C++; `panic!("msg")`.
3. The `reloco.kif` prelude (4.10) starts with allocators, `result`, `ptr`/`arc`/`rc`/`weak`, `optional` and `vector`.
4. Generic free `fn`s are supported (4.9).
5. Integer arithmetic always wraps; checked and saturating arithmetic are separate classes (`checked_value`, `saturating`).
6. `before`/`after` hooks cannot skip the call (7.2).
7. The language stays "kif", for kernel interfaces and for memory-safe procedural kernel code.
8. Interface implementation syntax is `impl iface for S`.
9. C++ structs: borrowed or moved, may contain objects, relocatability is the explicit `#[unsafe(relocatable)]` claim; C structs are trivial.
10. `#[heap]` is the attribute for heap-only types; `#[small_stack(N)]` / `#![small_stack(N)]` / `--small-stack=N` give the stack budget.
11. Generic functions have a fixed bound set (no user-defined bounds) and exist only in C++ output.
12. Interfaces are plain by default; kobj representation is the opt-in `#[kobj]` and gates the kobj-only features (4.15).
13. All formatting is microfmt and always targets a sink (`write!`/`try_write!`); `#[Debug]` derives a formatter; `here!()` and `#[caller_location]` expose reloco call locations from `.rodata` (4.16).

No open questions remain; implementation starts with the plan in section 13.

## 15. Precompiled modules (`.kifm`) and `--sysroot`

A vendor ships a driver development kit as precompiled modules plus object code; the consumer imports them like source
but never receives the implementation.

### 15.1 What a `.kifm` contains

`python -m kiflang export ddk.kif -o ddk.kifm` writes a deterministic CBOR map (`kiflang/cbor.py`, RFC 8949 canonical form,
decoded strictly) with: `magic` `"KIFM"`, `version` (1), `name`, `text`, `digest` (sha256 of `text`), `deps`
(import path -> digest of the imported module as exported), `stripped` (bodies removed) and `embedded` (names that stay in
source form). `text` is the module's source in which the **body of every ordinary function and method is replaced by `;`**,
so the importer type-checks calls, lowers them to calls of the vendor's compiled symbol, and cannot run them (the interpreter
traps).

| Part | In the `.kifm` | Why |
| --- | --- | --- |
| Structs, enums, interfaces, aliases, `extern` items, `use`/`import`, signatures, doc comments | as written | the interface itself |
| Non-generic `fn`, methods of non-generic impls | signature only | implementation lives in the object code |
| **Generic** `fn`, methods of generic impls | **full source** | instantiated in the importer, cannot be compiled ahead of time |
| Interface default methods, hooks, `gen fn`, derives | **full source** | expanded into the importer's code or run at generation time |
| `const` | as written | importers evaluate constants |
| Non-doc comments outside bodies | kept | keep secrets out of them |

`embedded` lists the bodies that stay readable so a vendor can review what a release exposes.

Three more keys carry the safety contracts and security boundaries that the stripped bodies no longer show (design and rationale:
`docs/kif-module-contracts.md`): `compiler` (the exporting compiler: `name`, `version`, `prover` identity, `security_schema`),
`security` (target, profile and taint policy the vendor compiled under; per function the *effective* tainted parameters, tainted
result, sanitizer evidence `checked`/`trusted`, fact contracts, `unsafe`, speculation barrier counts, the check kinds the compiled body
`kept` or `proved`, and the IR hash; per type the taint it carries) and `security_digest` (sha256 of the canonical encoding of
`security`). `export` takes the same `--target`, `--profile`, `--sanitize` and `--cfg` as `check`, because the section records what the module was
compiled for. All three are required: a file without them is malformed, never read as "nothing is tainted".

`export --sign KEY.pem [--cert CERT]` signs the file with the vendor's Ed25519 private key (PKCS#8 PEM, read from the file only); the header gets a
`signature` (algorithm, signer, public key, optional certificate, value) over all other keys, so changing any byte of it, `security`
included, is detected. Signing is deterministic. `python -m kiflang verify OUT.kifm --trust vendors.pem [--revoked revoked.pem]` prints who
signed it and whether the consumer's own trust list vouches for that key (exit 0 only then); the file's embedded key proves nothing by
itself. Signing needs the Python `cryptography` package or the `openssl` tool on the host; kif contains no cryptography of its own.

`python -m kiflang inspect OUT.kifm` prints the header for a human (module, compiler and prover, target, profile, taint policy,
digest, and per function its tainted parameters, sanitizer evidence, kept and proved checks and IR hash); it validates the file like
an import does and exits 1 when it is bad. A function that is generic stays in source and is listed as `embedded`.

The importer verifies the file: a wrong magic, truncation, a newer `version`, a `text` that does not match `digest`, a
`security` section that does not match `security_digest` or that disagrees with the declarations in `text` is
KIF014. `deps` records the digest of each import *as exported*, which is the same whether the dependency is present as source or as
`.kifm`; if the importer's copy differs the import fails with KIF015 (the typical "base module was updated, the vendor module
was not rebuilt" error).

### 15.2 `--sysroot`: what it means here, and what it means in C/C++

In C and C++ toolchains `--sysroot=DIR` is a *hint*, not a boundary. GCC and clang prefix some of their built-in search paths
with `DIR`, but also search paths that are not rewritten (`/usr/include`, `/usr/local/include`, the compiler's own resource
directory), honour `-I`, `-isystem`, `-idirafter`, `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH` and `--gcc-toolchain`, may
resolve `<foo.h>` and `"foo.h"` differently, follow `#include_next`, differ between GCC and clang and between a native and a
cross build, change meaning when the compiler is relocated or built with a different `--with-sysroot`, and treat the
"sysroot" as a prefix that may be absent. Which header a build really used is only discoverable with `-H` or `-M`. A
build can therefore silently pick a host header and "work" while being wrong for the target; this is the failure mode kif
refuses to have.

| | C/C++ `--sysroot` | kif `--sysroot` |
| --- | --- | --- |
| What it is | a prefix applied to *some* default search paths | the complete set of library modules |
| Other places searched | `/usr/include`, `/usr/local/include`, compiler resource dir, `-I`/`-isystem`/`-idirafter` | none |
| Environment variables | `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`, `SDKROOT`, ... | none are read |
| Current directory | searched for `"..."` includes | never searched |
| Missing file | falls through to the next path, possibly to the host's copy | error (KIF012) naming the import path |
| Two candidates | first match wins, by search order | error (KIF013), never a silent choice |
| Symlinks out of the tree | followed | rejected (KIF013) |
| Global prelude / base library | taken from wherever the compiler was installed | read from the sysroot too |
| Result | depends on the machine | depends on the sysroot and the sources only |

```
python -m kiflang check main.kif --sysroot /opt/ddk-sysroot
python -m kiflang modules --sysroot /opt/ddk-sysroot     # what is importable (tools use Sysroot.modules())
```

Usage and rules (the implementation is `kiflang/sysroot.py`; `tests/test_proc_sysroot.py` pins each rule). With
`--sysroot DIR`, **`DIR` is the only place `import` reads library modules from**:

- `import "a/b.kif";` is `DIR/a/b.kif`, or `DIR/a/b.kifm` if that exists instead; both existing is an error (KIF013), never a silent choice;
- no search path, no environment variable, no current directory, no compiler-relative fallback; the global prelude
  (`reloco.kif`) and `microfmt.kif` are read from `DIR` as well, and a sysroot without `reloco.kif` is KIF013;
- absolute paths, `..`, `\` and symlinks whose real path leaves `DIR` are rejected (KIF013);
- the project's own files are imported with explicit relative paths, `import "./util.kif";` / `import "../x.kif";`, resolved
  against the importing file (a `.kifm` next to it works the same);
- not found is KIF012 naming the import path; nothing else is consulted;
- the result of a check depends only on the sysroot contents and the project sources, so two machines with the same sysroot agree.

Without `--sysroot` the default sysroot is the prelude directory shipped with the compiler, so the rules are the same.

### 15.3 For tools

`kiflang.sysroot.Sysroot(root)` is the single implementation: `.resolver()` is the `FileResolver` to pass (via
`analyze(key, text, sysroot=...)`), `.modules()` lists importable modules, `.read(path)` returns `(key, text, header)` (the
header is set for a `.kifm`; for it `text` is the exported text, so hover, go-to-definition, completion, the formatter and the
linter see signatures and docs without bodies) and `.prelude()` the global prelude. The LSP and every other tool take the
sysroot from one option (`--sysroot`, or the editor setting) and must not resolve imports any other way.

```python
from kiflang.symbols import analyze
from kiflang.sysroot import Sysroot
from kiflang import modfile

sr = Sysroot("/opt/ddk-sysroot")                    # the only source of library modules
mod = analyze("main.kif", open("main.kif").read(), sysroot=sr)
blob = modfile.export(mod)                          # bytes of main.kifm (vendor side)
```
