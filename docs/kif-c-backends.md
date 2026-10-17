# C backends: what is generated and exported

**Status: the contract is approved and implemented in `kiflang/lower_c.py`** (run through `kifc --c`). What is lowered, by area
(worked examples with expected output are in `tests/c/`):

- **Core:** functions, plain structs, enums, constants, arrays, `let`, `if`/`match`/`loop`/`while`/`for`, casts and checked arithmetic.
- **Objects and results:** struct `impl` blocks, `result<T>`/`?`/`ok`/`err`, `S_INIT`/`S_init`, `try_construct_at`.
  - `result` lowers to an `int` plus a trailing `T *kif_out` (first for `try_create`/`try_allocate`/`try_clone`).
  - Errors map to errno names (`allocation_failed` to `ENOMEM`, `invalid_argument` to `EINVAL`, ...; the sign comes from the target's `errno.style`, the names from `ERROR_ERRNO` and `errno.map`).
  - In C the error payload is that `int`: `match r { ok(v) => .., err(e) => .. }` on a call or on a `let r = call;` local lowers to `rc == 0` / `rc != 0`; `e == error::busy` and `match e { error::busy => .. }` compare with the errno constant; `let _ = call;` discards on purpose.
  - Refused: a `result` call whose error would be dropped, `match` arms whose error names share one errno, and array-from-array copies in struct literals.
- **`vector<T>`** for integer, `bool`, `char`, enum and struct elements (`struct kif_vec_<T> { ptr, len, cap }`).
  - Operations: `vector::try_create`, `try_clone`, `try_push`, `try_pop`, `try_reserve`, `try_resize`, `try_insert_at`, `try_erase_at`, `try_at` (`result<&T>`: `out_of_range` instead of a trap), `len`, `is_empty`, `capacity`, `clear` and checked `v[i]`, built on the target's `alloc.realloc`/`alloc.free`.
  - Elements are copied bitwise and never dropped, so a vector of structs that own resources is refused; the vector itself is dropped by the compiler (3.2).
  - Name the element type in the `let`: `let v: vector<u8> = vector::try_create(8)?;`.
- **Strings:** `str_view` and string literals, and owned `str` (example `tests/c/str.kif`).
  - `struct kif_strview { const char *ptr; size_t len; }` is not NUL-guaranteed; it has `len`, `is_empty`, `starts_with`, `ends_with`, `contains`.
  - `struct kif_str { char *ptr; size_t len; size_t cap; }` is always NUL-terminated (`cap` counts the terminator) and lives on the target allocator, dropped by the compiler; it has `str::try_create`, `try_clone`, `try_assign`, `try_append`, `try_push`, `try_pop`, `try_insert`, `try_erase`, `try_reserve`, `view`, `len`, `is_empty`, `capacity`, `clear`, `starts_with`, `contains`.
- **Slices** `&[T]` / `&mut [T]` lower to `struct kif_cspan_<T>` / `struct kif_span_<T> { ptr, len }` (integer, `bool`, `char`, enum, struct and `str` elements) as parameters, locals, fields and return values (example `tests/c/span.kif`).
  - `&a`, `&mut a`, `&v` (array or vector) and `&x[a..b]`, `&x[a..]`, `&x[..b]`, `&x[a..=b]` (range checked, traps like an index) convert where a slice is expected; `xs[i]` is bounds checked; `xs.len()` / `xs.is_empty()` work.
  - `for x in <slice>`, `for x in &a` / `&mut a` / `&v` give `x` a reference to each element (`const T *` / `T *`, example `tests/c/forslice.kif`). `for x in v` would move the vector and is refused; an array by value yields copies.
  - `x.as_slice()` / `x.as_mut_slice()` on an array, vector or slice is `&x[..]` (an argument, a typed or untyped `let`, or a `for` source; the checker types it `&[T]` / `&mut [T]`).
  - `&[str]` / `&mut [str]` work (`struct kif_cspan_str { const struct kif_str *ptr; size_t len; }`): the slice borrows owned strings, so a C caller can pass an array of `struct kif_str`. An array of owning elements (`[str; N]`, as a local or a field) is refused: its elements would not be dropped.
- **Heap objects:** `ptr<S>` (a struct with an `impl` block) is `struct S *` (example `tests/c/heap.kif`).
  - `ptr::try_allocate(default_allocator(), args)?` / `ptr::try_create(args)?` call the generated `int S_try_new(struct S **out, args)` (target `alloc.malloc`, then `S_init` plus `try_construct_at`, freed again if the constructor fails).
  - `p.reset()` calls `S_delete` (`S_drop`, then `alloc.free`) and nulls the pointer; a `ptr` local or field is deleted by the compiler (3.2); `p.is_null()`, `*p`, `p.field` and `p.method()` check for null first (panic).
  - `vector`/`str`/`ptr` `try_allocate(default_allocator(), ..)` is `try_create(..)` (the target's `alloc` is the allocator); an `allocator_ref` value is refused by the checker (`KIFP053`), as C has no allocator handles.
  - `ptr<scalar>` is refused with a hint: wrap the scalar in a struct with an `impl` block.
- **Automatic drops** (RAII: scope end, early `?`/`return`/`break`/`continue`, moves, reassignment, by-value parameters, partly built objects): see 3.2 and `tests/c/autodrop.kif`.
- **Also lowered:** `optional<T>` (3.1), `#[ops]` interfaces and `dyn` (5.1), kobj interfaces and classes (5.2), imports (3.3) and raw `#[tls]` cells (3.4).
- **Not lowered yet** (fails with `KIFC001` and a message): `slot<T>` cells and the `freebsd`/`linux` cell providers.

This document is the contract the
backends will be written against; the spec is `docs/kif-proc-language.md` (section 9.1 is the C subset), the target
definitions are `docs/kif-targets.md`. The earlier open questions are decided (typedef, enumerator prefix, constants, packed/align, flags, Linux TLS, `#[ops]`). Anything not already in the
spec (exact macro names, file names) is my proposal, not a decision. The ABI
manifest is not a backend output: it comes from `kif manifest` (`docs/kif-abi.md`).

**This draft widens the C subset of spec 9.1** (your decision): the C backend generates `.h` and `.c` files, and C mode
accepts `impl` blocks, constructors, `str`, `vector`, slices and `result<T>`, lowered to plain structs and functions that
lowered to plain structs and functions, with **drops inserted by the compiler** (RAII, like the C++ output; no hidden allocation, and no `drop` call in kif source). Spec 9.1 and its KIFP053 row are updated, and the checker (`kiflang/cmode.py`) follows them.

There is one C backend with three built-in targets. What differs is read from the target JSON, never hard-coded:

| | `c` (plain hosted C) | `linux-kernel` | `freebsd-kernel` |
|---|---|---|---|
| Standard / hosting | C11, hosted | GNU C, freestanding | GNU C, freestanding |
| Scalar types | `<stdint.h>` (`uint32_t`) | `<linux/types.h>` (`u32`, `s32`) | `<stdint.h>` names |
| Allocator (`alloc`) | `malloc`/`free` | `kmalloc`/`kfree` (`GFP_KERNEL`) | `malloc(9)`/`free(9)` with `M_KIF` |
| Panic / assert | `__builtin_trap()` | `pr_emerg` + `BUG()`, `WARN_ONCE` | `panic(9)`, `KASSERT(9)` |
| `cresult` | `-errno` | `-errno` | `+errno` |
| 64-bit `/` `%` | allowed | refused (KIFP051) | allowed |
| Floats | allowed | refused | refused |
| TLS / per-CPU cells | `thread_local`-style / none | none (until a provider is given) | `osd(9)` / `uma_zalloc_pcpu(9)` |
| **kobj emitter** | **no** | **no** | **yes** (`freebsd` flavor) |

Everything below is "common" unless a section says otherwise.

## 1. Files

For `foo.kif` (stem `foo`) the backend writes into the output directory:

| File | When | Contents |
|------|------|----------|
| `foo_proc.h` | any free `fn`, struct, enum, const | The module's types, constants, `#[inline]` free functions, compiler boilerplate (vec/optional helpers, drop wrappers) and `extern` prototypes of every other function. |
| `foo_proc.c` | any method, `#[export]` function or `static` cell | External definitions: **every method of every struct** (on every target), `#[export]` functions and cell storage. Absent when empty (`--emit-source` writes a stub). Link it with the code that includes `foo_proc.h`. |
| `foo_if.h`, `foo_if.c` | `#[kobj]` interface, **FreeBSD target only** | makeobjops-compatible: method ids, `kobjop_desc`s, wrapper macros/inline functions, default methods (section 5). |
| `foo_kobj.h`, `foo_kobj.c` | `impl <kobj iface> for S`, FreeBSD target only | The kobj class of each such struct: thunks, method table, `DEFINE_CLASS_0` (section 5.2). |
| `foo_enums.h` | an enum is used by an interface | Enumerators of the module's enums (as today). |

Generated files start with a "generated from foo.kif, do not edit" banner, carry `#line` directives (target feature
`line_directives`), and use include guards `FOO_PROC_H` (stem upper-cased, non-alnum to `_`; the case follows
`naming.macro_case`).

**Include order** of every generated header, so that a target can plug in anything:

1. the target's `includes.header`, then `includes.when` entries for flags that are true;
2. `#include "kif_c.h"` (section 3.0);
3. the headers named by the target's `panic.header`, `assert.header`, `errno.header`, `alloc.header` and `cells.header` that the file actually uses;
4. only in `foo_if.h`: the kobj header from the target (`kobj.header`, `kobj.quote`), then `kobj.extra_headers`. **No other generated or hand-written file includes it**; code that needs kobj types includes `foo_if.h` (the user's rule: kobj headers differ per kernel and only the interface file names them);
5. the headers of imported modules (`bar_proc.h` for `import "bar.kif"`; `kifc` lowers each file separately and the importer only includes the result);

The target's `defines` are emitted right after the guard, before any include (in `kif_c.h` too).

## 2. Names and ABI of exported symbols

- A free function `f` in stem `foo` is `foo_f` (`naming.prefix = stem`), or `f` with `#[no_prefix]` or `naming.prefix = none`.
- A struct or enum keeps its kif name as `struct name` / `enum name` (kernels use tags). `#[typedef]` on a struct or enum also
  emits `typedef struct name name;` (KIFP087 anywhere else, or with arguments).
- Enumerators are `<enum>_<NAME>` by default, so `enum mode { fast, slow }` gives `mode_fast`, `mode_slow` and kernel-wide
  clashes are avoided. `#[no_prefix]` on the enum emits the bare `NAME`.
- `#[export]` makes the function an external symbol with exactly that name (no prefix): the symbol other C code links to.
  Without it a free function is `extern` too (prototype in the header, body in `foo_proc.c`); `#[inline]` keeps a small function `static __inline` in the header. **Methods** (`fn` in an `impl` block) are always external:
  `S_m` is declared `extern` in `foo_proc.h` and defined in `foo_proc.c`, so its address can go into a table (an `#[ops]` table, a kobj class)
  and the header needs no types a method body would pull in. Cost: a method is not inlined into other files.
- `extern "C" fn x(..)` / `extern "C" type`: only a prototype or the name is used; nothing is defined. The header that declares
  it is the one the user names (`#[header("...")]`); kif never invents it. In a file with a `#[kobj]` interface the type is usable in
  signatures and `<stem>_proc.h` includes `<iface>_if.h`, which brings the kernel headers. `tests/test_freebsd_compile.py` compiles the
  output against a real FreeBSD tree (`KIF_FREEBSD_SRC`).
- Constants: `const N: u32 = 4;` is `#define FOO_N ((uint32_t)4)` (typed and parenthesised, prefixed like functions). A `static`
  is a real object and stays one.
- Layout: struct fields are emitted in declaration order, no reordering. `#[repr(packed)]` maps to `__attribute__((packed))` and
  `#[align(n)]` (a power of two, structs only) to `__attribute__((aligned(n)))`; both are checked (KIFP087) and recorded in the ABI
  manifest layout. The attribute spelling comes from the target's `annotations`. A struct is always plain data; behaviour is separate functions (section 3.2), never hidden constructors or destructors.

## 3. Language lowering (all targets)

The checker has already refused what C cannot represent even with the model of sections 3.1 and 3.2 (KIFP053: generic
functions, `impl iface for S` outside kobj, `extern "C++"`, `cfg(cxx)`-only code). What remains lowers like this:

| kif | C |
|-----|---|
| `fn f(a: u32) -> u64` | `uint64_t foo_f(uint32_t a)` in `foo_proc.c`, `extern` prototype in the header; `#[inline]` gives `static __inline` in the header (types from the target's `types`) |
| `let`, blocks, `loop`, `while`, `for i in a..b`, `for x in &v`, `match` | declarations at first use, `for (;;)`, counted `for`, `switch` |
| block or `match` used as a value | a temporary declared just before the statement; evaluation order stays left to right |
| `+ - *` | plain where C already wraps: `u32`, `u64`, `usize`, and `u8`/`u16` for `+ -` (cast back, because of promotion to `int`). `u8`/`u16` `*` and signed types: computed unsigned and converted back (`KIF_ADD_I32` etc.), unless the target sets `wrapping: builtin` (below) |
| `/ %`, shifts, `-x`, `a[i]` | `KIF_DIV_*`, `KIF_SHL_*`, `KIF_NEG_*`, `KIF_INDEX` (trap on bad input) |
| `panic!`, a trap | `KIF_PANIC(file, line, msg)` |
| `assert!`/debug assertion | `KIF_ASSERT(cond, file, line, msg)`, compiled out unless the profile has `debug_assertions` |
| `cresult<T>`, `result<T>` | `int` carrying `-errno` or `+errno` per target (0 is success); the value goes through a trailing out-parameter `T *out`. `e?` is `if ((rc = e) != 0) return rc;` |
| `unsafe { }` | plain C; the block is only a checker construct, with a `/* unsafe */` comment |
| `cfg(..) { }` / `#[cfg(..)]` | resolved at generation time against the target + profile + `--cfg` (section 4); the inactive branch is **not emitted** |
| `slot<T>` cells | section 6 |
| `#[atomic_*]` attributes | the target's `annotations` macro (e.g. `__might_sleep`), else a comment (the checker has already enforced them) |
| `str`, `vector`, slices, objects | sections 3.1 and 3.2 |
| allocation (growable buffers, boxes, clones that C++ gets from reloco) | the target's `alloc.*` templates (section 7) |

### 3.0 `kif_c.h`: the common code, generated from the target

Generated C needs no runtime library to install or link, but every generated header includes **`kif_c.h`**, which kifc
(the SDK) **writes from the target JSON**; it does not depend on any source file, so all files of one target/profile get
identical text. Produce it once per target with:

```
kifc --target freebsd-kernel --c-common build/gen      # writes build/gen/kif_c.h (only if the content changed)
```

It holds what is common to all code: the target's `defines` and includes (`includes.header`, `panic.header`, `alloc.header`,
`errno.header`), `KIF_PANIC` (the target's `panic.macro`, wrapped in `#ifndef` so the includer can override it), all checked
arithmetic helpers (`KIF_ADD_*`, `KIF_SUB_*`, `KIF_MUL_*`, `KIF_DIV_*`, `KIF_REM_*`, `KIF_SHL_*`, `KIF_SHR_*`, `KIF_NEG_*`),
`KIF_IDX`, `KIF_RANGE`, `KIF_NONNEG`, `KIF_NN` (all declared `KIF_INLINE`, below), and `struct kif_strview` / `struct kif_str` with their functions (allocator
and errno values from the target). Scalar types come from the target's `types`, which is why it is generated and not static.

**Specializations are generated into each `foo_proc.h`**: `kif_vec_<tag>` and the spans per element type, enum conversions
(`KIF_TOENUM_<enum>`), `S_try_new`, drop wrappers, user functions. Nothing there is shared, so it stays with its file.

A target that lacks something a file needs (no `alloc` for a `vector`, no `panic.macro` where the code can trap) is refused
at generation time with a message naming the target and the missing key, not patched with a kif default. The built-in
targets define what they need; a derived target overrides it in its JSON.

### 3.1 Data types

| kif | C |
|-----|---|
| `[T; N]` | a plain C array `T a[N]`, indexing keeps its bounds check (`KIF_INDEX(a, i, N)`). C cannot pass an array by value, so an array parameter or result is a `T *` (the length is the type's `N`, known statically); an array inside a struct is copied with the struct |
| `&[T]`, `&mut [T]` (slice of an array, `vector` or `str`) | a pair of pointer and length: `struct kif_span_u8 { uint8_t *ptr; size_t len; }`, one struct per element type (`kif_cspan_<T>` holds `const T *ptr`). Each is emitted in the prologue of the headers that use it (guarded per type). Indexing checks `i < len` |
| string views (`&str`, `[char]` slices) | the same pair, `struct kif_cspan_char { const char *ptr; size_t len; }`, spelled `kif_strview`. Not necessarily NUL-terminated; a view never owns |
| `str` (owned string) | managed `char *` with capacity: `struct kif_str { char *ptr; size_t len; size_t cap; }`. `len` is a kept field (decided), not recomputed with `strlen`; `ptr` is always NUL-terminated (`cap` counts the terminator) so it can go to C APIs; grown with the target's `alloc`, released by `kif_str_free(&s)` |
| `vector<T>` | `struct kif_vec_T { T *ptr; size_t len; size_t cap; }`. Storage comes from the target's allocator: `alloc.zalloc` for the first allocation (that is `calloc` on plain C, `kzalloc` on Linux, `malloc(.., M_ZERO)` on FreeBSD) and `alloc.realloc` (else malloc + copy + free) for growth. A failed allocation is `-ENOMEM`/`+ENOMEM` from the `try_` operations; `kif_vec_free(&v)` releases it |
| `optional<T>` | `struct kif_opt_<tag> { bool has; T v; }` (`<tag>` is the element's scalar, enum or struct name, `str`, or `vec_<tag>`). `some(x)` / `none()` are compound literals, typed from the `let` annotation, the parameter or the function's return type. Methods: `has_value`, `value` (panics through `KIF_PANIC` when empty), `try_value` (`result<&T>`: `not_found` when empty), `value_or` (not for owning elements), `reset`. As a struct field, `optional<Struct>` is allowed: its helper struct is emitted just before the holder. An owning element (`str`, `vector`, a struct with `drop`) is dropped with the optional. `match o { Some(x) => .., None => .. }` on a variable or field lowers to `o.has` tests (`x` copies `o.v`; an owning element can only be matched with `Some(_)`). Not lowered yet: `optional<optional<..>>`, `optional<ptr<..>>` |
| `ptr<T>` (heap-owning pointer) | `T *` from the allocator, freed explicitly |
| `c_ptr<T>`, `c_cptr<T>` | `T *`, `const T *` as written |

The helpers (`kif_str_*`, `kif_vec_*`, span constructors) are `static __inline` functions emitted in the prologue (3.0),
written against the target's `alloc` templates, so nothing links against a runtime library.

### 3.2 Objects: class-like functions, dropped by the compiler

A struct with an `impl` is a **plain `struct`** (fields in declaration order; `private` is only a comment, C has no access
control) plus ordinary functions. The backend generates the wrappers; **it never calls them for you**: no destructor at
scope end, no move, no implicit copy.

```kif
struct ring { private buf: c_ptr<u8> = null, private cap: usize = 0, count: usize = 0 }
impl ring {
    fn try_construct(&mut self, cap: usize) -> result<()> { ... }
    fn try_clone(&self) -> result<Self> { ... }
    fn push(&mut self, b: u8) -> result<()> { ... }
    fn is_empty(&self) -> bool { self.count == 0 }
    fn drop(&mut self) { ... }
    fn magic() -> u32 { 42 }                               // an associated function (no self)
}
```

```c
/* ring.h: generated; every function takes the object explicitly */
struct ring { uint8_t *buf; size_t cap; size_t count; };      /* the C ABI: exactly the fields, in order */
#define RING_INIT { NULL, 0, 0 }                              /* the "shell": every field at its declared default */
static __inline void ring_init(struct ring *self);            /* the shell as a function */
int      ring_try_construct(struct ring *self, size_t cap);   /* &mut self: non-const pointer; result<()> is the int */
int      ring_try_clone(struct ring *out, const struct ring *src);   /* out-parameter first, source const */
int      ring_push(struct ring *self, uint8_t b);
bool     ring_is_empty(const struct ring *self);              /* &self: const pointer */
void     ring_drop(struct ring *self);                        /* the compiler calls it for locals; safe on a ring_init() value */
uint32_t ring_magic(void);                                    /* associated function: no self */
```

Rules:

- **Names** are `<struct>_<name>` (spec 9.1's `<stem>_` prefix applies to free functions; `#[no_prefix]` and `naming.prefix` as in section 2).
  `&self` is `const struct S *`, `&mut self` is `struct S *`, and a consuming `self` is `struct S *` too: the caller must not
  use the object afterwards (KIFP023 enforces that on kif code only).
- **Constructors** keep reloco's protocol names, in the shape the C ABI needs: `try_create`/`try_allocate(args) -> result<Self>`
  become `int S_try_create(struct S *out, args)` (written into caller-owned `out`; nothing is heap-allocated);
  `try_construct(&mut self, args)` is `int S_try_construct(struct S *self, args)` run on a shell; `try_clone(&self)` is
  `int S_try_clone(struct S *out, const struct S *src)`. The heap form, `ptr<S>::try_allocate(..)`, is
  `int S_try_new(struct S **out, args)` (target `alloc`, then the construction) with `S_delete(struct S *)` (runs `drop`, then frees).
- **Building an object in C-owned memory: `try_construct_at`.** An associated function named `try_construct_at(mem: c_ptr<Self>, args) -> result<()>`
  (the `_at` form of reloco's protocol, like `try_clone_at`) constructs the object **in memory the caller owns**: a static
  buffer, a field of a C struct, a slab slot, a stack array. It first writes the shell (`S_init`), then runs your body. It lowers to
  `int S_try_construct_at(struct S *mem, args)`. The object does not own `mem`: `S_drop` releases what the object holds
  (`str`, `vector`, `ptr` fields) but never `mem` itself, and `S_delete` is for heap objects only. kif code gets
  the same function, so `S::try_construct_at(buf_ptr, 8)?` works in `unsafe` code. The checker enforces the shape (KIFP086): `mem: c_ptr<Self>` first, result `result<()>`.
- **Constructing objects with the C ABI is a language feature**, not a backend detail: in C mode every struct is `repr(c)`
  (declared order, natural alignment, no hidden fields), `Self { .. }` literals and `try_construct` work on any struct, and
  the generated `S_INIT`/`S_init` give a defined starting state to code written in C that never saw kif.
- **`drop` is automatic.** kif code never calls `drop` (a manual call is refused, and would conflict with memory safety: a double
  free or a use after free). The compiler inserts the drops itself, exactly where the C++ output runs destructors:
  - a local is dropped at the end of its scope, in reverse declaration order;
  - on every early exit: `return`, a failing `?` (the objects built so far are released, and an unbound `f()?` temporary too),
    `break` and `continue` (the scopes of the loop body);
  - a **moved** local is not dropped (passed by value, returned, `ok(x)`): the checker's move analysis (KIFP023) marks the use, and
    a local that is moved somewhere gets a `bool kif_live_x` flag that the move clears, so a move on one branch is still dropped on the other;
  - a by-value parameter and a by-value `self` are owned by the callee and dropped there;
  - assigning to an owning place builds the new value first, then drops the old one, then stores (`x = f(x)` and a failing `?` are safe);
  - `S_drop` is `S_drop_body` (the user's `drop`, which may still read the fields) followed by `S_drop_fields`, which releases the `str`,
    `vector`, `ptr` and droppable-struct fields in reverse field order (the C++ order). Every struct that owns something gets both, with or
    without an `impl`; one that only has a user `drop` uses it directly. A struct is *droppable* when it has a user `drop` or owns a droppable member.
  - `try_construct_at` runs `S_init` first; if it fails, `S_drop_fields` releases what it had built, but **not** the user `drop` (the object never became valid).
  The generated functions stay public (`S_drop`, `S_delete`), so C code that never saw kif can still release an object.
  What is refused, each with a message: a result local whose `ok` payload owns resources (`let r = mk();`: take it with `?` or `match`,
  so it cannot be forgotten), a `vector` of droppable structs (elements are not dropped), and a manual `drop()`.
  - an unbound `f()?` temporary is owned too: used as a method receiver or discarded (`f()?.len()`, `f()?;`) it is dropped at the end of its
    scope; passed by value or stored it is moved. If a later `?` in the same expression fails, the temporaries still owned are dropped and
    the ones already handed on are not. The temporaries of the right side of `&&`/`||` are dropped inside the short-circuit block, and a
    `while` condition's at the end of each iteration and on the exit (`tests/c/tempdrop.kif`).
  - a `match` on a result whose `ok` value owns resources is refused too (take it with `?`), so it can never be left undropped.
- **Moves in C.** A move is a bitwise copy and the source is not touched, but the compiler remembers the move (flag above) and skips its drop;
  kif code may not use the source anyway (KIFP023). `#[copy]` structs copy normally.
- **Interfaces.** `impl iface for S` is still refused in C mode unless the interface is `#[kobj]` on a target with a kobj
  emitter, where the implementer is a C file the user writes (section 5).

### 3.3 Imports

Each file is lowered on its own and includes the generated header of every file it imports. What the importer sees of a module:

```
// lib.kif
const LIMIT: u32 = 10;
struct point { x: i32, y: i32 }
fn twice(a: i32) -> i32 { a * 2 }

// main.kif
import "./lib.kif";                      // main_proc.h gets #include "lib_proc.h"
use lib::twice as dbl;                   // bare or aliased name for a function or constant
fn f(p: lib::point) -> i32 {             // `struct point`: type and enum tags are global, the module prefix is dropped
    lib::twice(dbl(p.x)) + lib::LIMIT as i32   // lib_twice(lib_twice(p.x)) + LIB_LIMIT
}
```

Rules: a struct or enum name must be unique across the imported modules (C tags are global; a clash is refused). The importer
lays out, and drops, an imported struct itself (its `S_drop` and `S_drop_fields` live in the library's header), but never
generates anything for it. `ptr<S>` of an imported struct works: the importer emits its own `S_try_new`/`S_delete` (guarded by `KIF_HEAP_<S>`, so the
library's copy and the importer's never clash). Refused: `use ... as` on a type, precompiled `.kifm` modules, modules with `namespace`. Imports are found by the importing file's directory,
`-I`, `KIF_PATH`, or only the sysroot (`docs/kif-cmake.md`).

### 3.4 Lowering strategies and the ABI

What a C caller (or a debugger) can rely on. Everything here follows from the rules above; this is the one place that lists it.

| Subject | Strategy | ABI consequence |
|---------|----------|-----------------|
| scalars | the target's `types` table (`u32` is `uint32_t`, `usize` is `size_t`, ...) | same width and signedness everywhere; `bool` is `bool`, `char` is the table's char type |
| arithmetic | unsigned plain, signed through unsigned helpers, division, shifts and indexing trap via `KIF_*` | no undefined behaviour reaches C; traps end in `KIF_PANIC` |
| structs | declaration order, no hidden fields; `repr(packed)` / `align(n)` become attributes | layout is the C layout; the same struct can be shared with hand-written C |
| enums | C `enum` with prefixed enumerators; `repr(T)` fixes the stored width in the manifest | passed as the integer; a cast from an integer is checked (`KIF_TOENUM_*`) |
| references `&T`, `&mut T` | `const T *` / `T *`, never null | callers pass the address of a live object |
| slices, `str_view` | `{ ptr, len }` struct by value (`kif_span_*`, `kif_cspan_*`, `kif_strview`) | two registers or a small struct, per the platform C ABI |
| arrays `[T; N]` | a C array; as parameter or result a `T *` | length is in the kif signature, not in C |
| owned `str`, `vector<T>`, `optional<T>` | `kif_str`, `kif_vec_*`, `kif_opt_*` structs by value (`{ ptr, len, cap }`, `{ has, v }`) | a moved value is bitwise copied; the source is not dropped |
| `result<T>` | `int` return (0 success, `-errno` / `+errno` per target), the value in a trailing `T *out` (first for the constructors `try_create`, `try_allocate`, `try_clone`) | one error channel; `e?` is a test and early return |
| `&dyn I` | `struct I_ref { void *self; const struct I_ops *ops; }` by value (section 5.1) | an indirect call per method; tables are `static const` |
| objects (3.2) | `<S>_init(struct S *)`, methods `<S>_<m>(struct S *self, ..)`, `<S>_drop(struct S *)` | the compiler inserts drops; C callers of exported functions drop explicitly |
| cells (3.4) | accessor over a per-target block | block layout is generated (`kif_tls_<stem>` / `kif_pcpu_<stem>`) |
| methods and exported functions | every method is the external symbol `<S>_<m>` and `#[export]` functions keep their kif name; both are defined in `<stem>_proc.c`. Free functions are external as well, unless marked `#[inline]` | methods and exports are link-stable symbols; inline free functions may change between kif versions |
| compatibility | `kifc --manifest FILE` writes the ABI manifest of exported layouts and signatures | compare manifests with `docs/kif-abi.md` |

Strategy rules the backend keeps: (1) pass small things by value and anything owning by pointer when it must not move; (2) never
generate a hidden allocation: every allocation is a `try_` call or an owned-type operation using the target's `alloc` templates;
(3) the generated header is self-contained apart from the target's headers and `kif_c.h`; (4) specialisations a target needs are
generated as C in the header, common helpers live in `kif_c.h` (3.0).

## 4. Variants, profiles and sanitizers

The backend takes the effective `cfg` environment (target, profile, sanitizers, `--cfg`) and **resolves `cfg` itself**:

- `#[cfg(p)]` items, methods and fields are emitted only when `p` holds; of several variants of one name, the active one is emitted.
- `cfg(p) { .. }` blocks are emitted in place or dropped.
- Profile (informational for now): `debug_assertions` decides whether `KIF_ASSERT` is live; `overflow_checks` is kept for
  when checked arithmetic becomes selectable. Optimization level and sanitizer names are *not* turned into compiler flags by
  kifc; they are recorded in a comment at the top of the generated header (`/* kif: target=freebsd-kernel profile=release */`)
  so a build system can read them (`kif target cfg` prints them). Decided: `kifc` never prints compiler flags; the build system
  reads `kif target cfg`.
- Stripping code in release (log and trace calls) is future work.

## 5. FreeBSD kernel: the kobj emitter (`#[kobj]` interfaces)

Only a target with a `kobj` section produces these; on `c` and `linux-kernel` a `#[kobj]` interface is a diagnostic (KIFP051,
reported when `--target` selects the target) and plain interfaces are C++ only (spec 4.15, KIFP140 in C mode).

For `#[kobj] interface hpet_if { method read(reg: u32) -> u32; ... }` the output is exactly what `makeobjops.awk` would
generate from the equivalent `.m`:

**`hpet_if.h`**
- includes the target's kobj header (`kobj.header`) and `kobj.extra_headers`;
- `extern struct kobjop_desc hpet_read_desc;` and `typedef u_int32_t hpet_read_t(device_t dev, u_int32_t reg);` per method;
- the call wrapper:
  ```c
  static __inline u_int32_t HPET_READ(device_t dev, u_int32_t reg) {
      kobjop_t _m;
      u_int32_t rc;
      KOBJOPLOOKUP(((kobj_t)dev)->ops, hpet_read);
      rc = ((hpet_read_t *) _m)(dev, reg);
      return (rc);
  }
  ```
  (the type of `dev` is `self(..)`, else the target's `kobj.self_type`);
- `#[version(n)]`: a `HPET_IF_VERSION n` define and the version check, as the ABI document describes.

**`hpet_if.c`**
- includes `hpet_if.h`, the target's `includes.source`, and the file's `include <..>;` lines;
- `struct kobjop_desc hpet_read_desc = { 0, { &hpet_read_desc, (kobjop_t)kobj_error_method } };` per method, or the function
  named by `default <symbol>` as the fallback.

File-level `header(c) { .. }` blocks go into every interface's `.h` (after the includes) and `code(c) { .. }` blocks into its
`.c`; a block tagged `cxx` is for the C++ output. `m_prolog` / `m_epilog` text is copied into the wrapper of the next method.

**Status: the interface side is implemented** (`kifc --freebsd DIR`, `kiflang/lower_kobj.py`, `tests/test_lower_kobj.py`). It
writes `<interface>_if.h` and `<interface>_if.c` for every `#[kobj]` interface that is not `cxx_only`; `--c` and `--freebsd`
can be given together. Rules the emitter keeps and refuses:

| Subject | Behaviour |
|---------|-----------|
| handle parameter | the first parameter of every non-static method is the handle, named `dev` and typed by `self(..)` (else `kobj.self_type`); a hook text that uses another name must be edited |
| static method | needs at least one parameter: the first is the class searched (`KOBJOPLOOKUP(driver->ops, ..)`), as in `.m` |
| types | scalars (the target's `types`), `bool`, `char`, `cstr` (`const char *`), `c_ptr<T>`, `c_cptr<T>`, `&T`, `extern "C" type` names, struct and enum tags (the user's header defines them); anything else is refused (KIFC001) |
| kif default body, `before`/`after` hooks, `extends` | refused with a message (not lowered to kobj C yet); use `default <c function>` and `m_prolog`/`m_epilog` |
| formatting | wrapped at 80 columns like `makeobjops.awk`, so a diff against its output shows only type spellings (`int32_t` for `int`) |
| `hpet_enums.h` | not generated yet; the header that defines the enums the signatures use is the user's |

`kif_c.h` and the procedural output never include the kobj header: only `<iface>_if.h` does, with the name the target gives.
The `--c` output skips `#[kobj]` interfaces.

Implementers: a kif struct becomes a class with `impl <kobj iface> for S` (section 5.2); C implementers are still written
by hand with `KOBJMETHOD(hpet_read, my_read)` and `DEFINE_CLASS_0`.

`.m` import/export (`kif convert`) is unaffected: it is the same data in the other direction.

## 5.1 Plain interfaces: `#[ops]` (every C target)

A plain interface marked `#[ops]` lowers to a table of function pointers, the counterpart of kobj on targets that have none
(Linux). `#[ops]` goes on a plain interface only (KIFP087 with `#[kobj]`), and `impl iface for S` is then allowed in C mode.

```kif
#[ops] interface clock { method gettime() -> u32; }
struct hpet { base: u64 }
impl clock for hpet { fn gettime(&self) -> u32 { 0 } }
```

A handle is written `&dyn clock` / `&mut dyn clock` (Rust style). `dyn` is only valid behind `&` / `&mut`; `&S` / `&mut S` coerces
to it when `impl clock for S` exists (a type error otherwise), and `&mut self` methods need `&mut dyn`.

```kif
fn read(c: &dyn clock) -> u32 { c.gettime() }      // a call through the table
fn boot() -> u32 { let h = hpet { base: 0 }; read(&h) }   // &hpet becomes &dyn clock here
```

```c
struct clock_ops {                                 /* one slot per method; the receiver is void * (constness is not part of the ABI) */
	uint32_t (*gettime)(void *self);
};
struct clock_ref { void *self; const struct clock_ops *ops; };   /* a handle: object + table, passed by value */
static __inline uint32_t clock_gettime(struct clock_ref r) { return r.ops->gettime(r.self); }
/* per implementer: a thunk casts void * back to the struct, the table is a static const object */
static uint32_t hpet_clock_gettime_thunk(void *self) { return hpet_gettime((struct hpet *)self); }
static const struct clock_ops hpet_clock_ops = { .gettime = hpet_clock_gettime_thunk };
static __inline struct clock_ref hpet_as_clock(struct hpet *s) { struct clock_ref r = { s, &hpet_clock_ops }; return r; }
```

Default bodies are emitted as `<iface>_<method>_default(struct <iface>_ref self, ..)` (inside, `self` is the handle and calls
go through the table); the thunk of an implementer that does not override points at it. The coercion is the call
`<S>_as_<iface>(&s)`, inserted by the compiler. Not lowered yet (refused with a message): `before`/`after` hooks, `result<T>`
and static methods of an `#[ops]` interface. An `#[ops]` interface of an imported module works: the library's header supplies the
table, handle and wrappers, and the importer may implement it for its own structs (`impl lib::counter for twin`). A user writing a Linux `struct file_operations`
still writes it by hand and calls the `<struct>_<method>` functions from it; the generator does not produce kernel-specific tables.
`tests/c/ops.kif` is the executable example.

## 5.2 Implementing a kobj interface in kif: `<stem>_kobj.h/.c`

`kifc --freebsd DIR` also writes `<stem>_kobj.h` and `<stem>_kobj.c` when the module has `impl <kobj iface> for S`. They hold
only the **class**; the object's lifecycle belongs to the framework (design and rationale: `kif-kobj-lifecycle.md`).

```kif
extern "C" type device_t;
extern "C" type driver_t;

#[kobj, self(device_t)]
interface uart {
  method attach() -> i32;
  method detach() -> i32;
  static method identify(driver: c_ptr<driver_t>, parent: device_t);   // class-level: no object
}

struct sc { n: u32, name: str, }

// optional: #[object("my_state({handle})")] before `impl` replaces the handle -> struct expression
impl uart for sc {
  fn attach(&mut self) -> i32 { self.n = 1; 0 }
  fn detach(&mut self) -> i32 { 0 }
  fn identify(driver: c_ptr<driver_t>, parent: device_t) { }   // all interface parameters, class first
}
```

| Generated | Meaning |
|-----------|---------|
| `extern struct kobj_class sc_class;` (`_kobj.h`) | what the C side passes to `device_set_driver`, `kobj_create`, `kobj_init`; object size is `sizeof(struct sc)` |
| `static <ret> sc_uart_<m>_kobj(handle, ..)` (`_kobj.c`) | thunk: turns the handle into `struct sc *` and calls the kif method; a static method's thunk calls it with the same arguments |
| `static kobj_method_t sc_methods[]`, `DEFINE_CLASS_0(sc, sc_class, sc_methods, sizeof(struct sc))` | the class |

Handle to object, first match wins: `#[object("expr")]` on the `impl` (`{handle}` is the handle); the target's `kobj.handles`
(`freebsd-kernel`: `device_t` is `device_get_softc({handle})`); a cast, which needs the struct's first field to be the kobj
header as an `extern "C"` type (KIFC001 otherwise).

Lifecycle: for handle types in the target's `kobj.lifecycle` (`device_t`) the `attach` and `detach` thunks wrap the kif
methods: `S_init` then `attach` (`S_drop` if it fails); `detach`, then `S_drop` if it succeeded. Both must return `i32`.
Elsewhere (or with `#[object]`) the caller runs `S_init`/`S_drop` itself; a C driver embedding the kif struct calls them as
described in `kif-kobj-lifecycle.md` section 4. kif code cannot instantiate a kobj yet. `_kobj.c` includes the kobj header
(it is a source file of the kernel module); `_kobj.h` and the procedural output do not.

## 8. What the C backends refuse

On top of the checker's KIFP053 list:

| Refusal | Why |
|---------|-----|
| generation-time constructs in a lowered `fn` (KIFP050) | they exist only at generation |
| recursion where the target forbids it (KIFP032), 64-bit `/` `%` on a target with `int64_div = false` (KIFP051) | no libgcc helper in the kernel |
| a float type on a target with `floats = false` | kernel code does not use the FPU |
| `#[kobj]` interface on a C target with no `kobj` section (KIFP051, checked when `--target` selects one) | no dispatch exists there |
| a TLS/per-CPU cell on a target whose provider is `none` | nothing to put it in |
| an allocating construct on a target without `alloc` | no allocator |

- A call that violates a parameter fact (`#[some]`, `#[non_null]`, `#[non_empty]`, `#[len_ge(N)]`, `#[lt_len(v)]`) provably, such as
  a constant index past a fixed-size array: `KIFC110: call can never succeed: #[lt_len(s)] of argument 'i' of pick() fails (5 is not
  below the length 2)`. A fact that is merely unproved is not refused (it keeps a run-time guard).

A function with parameter facts is emitted as `F` (checks the facts, then calls) and `F__nofacts` (the body; assumes them). A kif
call that proves the facts calls `F__nofacts`; see docs/kif-proc-language.md 4.19a.

## 9. Not covered here

- C++ backends (`cxx-user`, `cxx-kernel`): use reloco for panic, assert, errno, cells and allocation; target JSON does not carry them.
- Mocks and the `kifc` command line. (`kif/proc.h` is not used by the C backends; the C++ backends keep their own runtime.)
- Linux-specific interface lowering beyond `#[ops]` (section 5.1), such as generating `struct file_operations` itself.

## Elided checks are documented in the output

When the IR prover (`docs/kif-ir.md` section 4) or a constant proves a check cannot fail, the C
backend drops it and leaves a comment saying why. Unproved checks keep `KIF_IDX` and have no comment.
`access.<class>: checked` in the target keeps every check and emits no comment.

```c
/* kif source: if i >= 4 { return 0; } a[i] */
if ((i >= UINT64_C(4))) { return 0; }
/* the bound check is gone; the comment records the proof */
return a[/* kif: bounds check elided: i < 4 <= 4 is known */ i];
/* optional::value() after has_value(): the call becomes a direct field access */
return (*(&(&o)->v /* kif: has-value check elided: some(o) is known on every path here */));
/* kif source: if d != 0 { x % d } else { 7 } -- the zero-divisor test is gone (KIF_REM_U32 would have trapped) */
return ((uint32_t)(x % /* kif: division check elided: d != 0 is known */ d));
/* `5 as state` where 5 is a declared enumerator: the enumerator itself, no KIF_TOENUM_ switch */
return state_b /* kif: enum cast check elided: 5 is the enumerator state_b */;
```

## Arithmetic wrappers: when they appear

`+ - *` wrap in kif. The C output writes the plain operator wherever C already wraps and keeps a helper only where it would not:

```c
/* u32, u64, usize: unsigned wrap-around is defined in C, so no helper */
i = (i + UINT64_C(1));
/* u8, u16: promoted to int, where + and - cannot overflow; the cast narrows the result */
x = ((uint8_t)(a + b));
/* u8/u16 `*` (u16 * u16 can overflow int) and every signed type: a helper that computes unsigned and converts back */
y = KIF_ADD_I32(y, 1);
```

**`wrapping` (target).** `"helpers"` (default) is the above. `"builtin"` also writes signed `+ - *`, and `u8`/`u16` `*`, as plain
operators. That is only correct when the code is built with `-fwrapv` (or `-fno-strict-overflow`, which implies it), so a target
must opt in and promises the build flags. `linux-kernel` does (kbuild passes `-fno-strict-overflow`), and so does `freebsd-kernel` (`sys/conf/kern.mk` adds `-fwrapv` to every
kernel compile); plain `c` does not, because its build flags are not ours.

**`KIF_INLINE`.** The helpers in `kif_c.h` are declared `KIF_INLINE`, which is `static __inline __attribute__((always_inline))`
on GCC and Clang (so even `-O0` builds inline them, with no call in the debugger) and plain `static __inline` elsewhere. Define
`KIF_INLINE` before including the header to override it.
