# Target definitions (`kif target`, `kiflang/target.py`)

A **target** says what the C backends generate for one platform: the integer type spellings, the includes, how a panic is
spelled, the errno convention, whether there is a kobj emitter and under which header name, and the per-target feature
flags. Targets are JSON files; three are built in (`kiflang/targets/`) and a project can add its own or derive from them.
Nothing here generates code yet: the backends will read a `Target`. Loading is strict (unknown keys, wrong types and unknown
enum values are errors), so a typo fails at load time instead of producing wrong C.

```
python3 -m kiflang.kif target list                       # built-ins, their language and whether they have kobj
python3 -m kiflang.kif target show freebsd-kernel        # the merged definition (after `extends`) as JSON
python3 -m kiflang.kif target check my-target.json       # every problem, exit 1 if any (a built-in name works too)
```

What each C backend generates from these targets is described in `docs/kif-c-backends.md` (draft).

## What the built-in targets are

| Target | Language | kobj emitter | Notes |
|--------|----------|--------------|-------|
| `c` | C11, hosted | **no** | `<stdint.h>` types, `__builtin_trap()` panic, `-errno`. Base of the others. |
| `cxx-user` | C++20, hosted | **no** | Reloco runtime. Base of the C++ targets. |
| `cxx-kernel` | C++20, freestanding | **no** | Extends `cxx-user`: `<stdint.h>`/`<stddef.h>` only, no floats. Reloco runtime. |
| `linux-kernel` | GNU C, freestanding | **no** | `s32`/`u32`/... from `<linux/types.h>`, `pr_emerg` + `BUG()`, `WARN_ONCE`, `-errno`, no 64-bit division. |
| `freebsd-kernel` | GNU C, freestanding | **yes**, flavor `freebsd` | `<sys/kobj.h>`, `panic(9)`, `KASSERT(9)`, `+errno`, makeobjops-compatible `_if.h`/`_if.c`. |

kobj dispatch exists only for FreeBSD-like kernels. On a target whose `kobj` is `null`, a `#[kobj]` interface cannot be lowered
(planned diagnostic; plain interfaces and free `fn`s still work), and `kobj.flavor` accepts only `freebsd`. A new
kernel with its own kobj-like system would add a flavor to `target.KOBJ_FLAVORS` together with its emitter.

C++ targets are ordinary targets with `"language": "cxx"`, and the generated C++ always sits on **reloco**. Allocation,
panic/assert, errno and TLS/per-CPU cells come from reloco (its allocator and porting layer), so a C++ target may not set
`alloc`, `panic`, `assert`, `errno` or `cells` (checked); port reloco to your kernel instead. Derive your own target from
`cxx-kernel` (or `cxx-user`) and override only what differs:

`kobj` is not restricted by language: a C++ target may carry a `kobj` section once a C++ kobj emitter exists (today only the
`freebsd` flavor, which is C).

```jsonc
{
  "format": "kif-target", "version": 1,
  "name": "my-os-cxx",
  "extends": "cxx-kernel",                       // inherits language "cxx", freestanding, no libc
  "includes": { "header": ["<my/kernel.hpp>"] }, // added to every generated header
  "cfg": { "os": "myos", "arch": "x86_64" }      // cfg(os = "myos") in .kif code; `--cfg arch=arm64` overrides it
}
```

A C target supplies its own allocator through `alloc` (`kmalloc`/`kfree` on Linux, `malloc(9)` with a `M_KIF` type on FreeBSD).
C++ never needs it: it gets the same from reloco. The C backend uses it to synthesize growable buffers, boxes, clones. Python: `t.alloc("malloc", size="n")`,
`t.supports("alloc")`. The FreeBSD built-in declares it (`alloc.declare`, `MALLOC_DECLARE(M_KIF);` in `kif_c.h`) and expects the kernel module to `MALLOC_DEFINE(M_KIF, "kif", "kif objects")` once; override `alloc` to rename it.

## Writing a target

```jsonc
// jsonc for illustration only: real files are plain JSON (no comments).
{
  "format": "kif-target",              // required, always this string
  "version": 1,                        // required, the format version
  "name": "my-bsd",                    // required: lower-case name used in messages
  "extends": "freebsd-kernel",         // a built-in name, or a path relative to THIS file; keys below override the base
  "kobj": {
    "header": "bsd/kobj.h",            // the kobj header exactly as this kernel names it (never assumed by kif)
    "quote": "quote",                  // "angle" (default): #include <bsd/kobj.h>; "quote": #include "bsd/kobj.h"
    "include_dir": "inc"               // directory holding it, for -I; relative to this file
  },
  "features": { "smp": false, "has_pmap": true },        // flags: known ones plus any lower-case custom name
  "includes": {
    "header": ["<my/kernel.h>"],                          // added to every generated header
    "when": { "has_pmap": { "header": ["<vm/pmap.h>"] } } // added only while the flag is true
  }
}
```

Merging with `extends`: objects merge key by key, lists and scalars are replaced, `"kobj": null` removes the kobj emitter, and
a loop in `extends` is an error. Check with `kif target check`; show the result with `kif target show`.

## Reference

| Key | Value | Meaning |
|-----|-------|---------|
| `format` | `"kif-target"` | Required. |
| `version` | `1` | Required. |
| `name` | `[a-z][a-z0-9_-]*` | Required. |
| `description` | string | Shown by `kif target list`. |
| `extends` | name or path | The base target; see Merging. |
| `language` | `"c"` or `"cxx"` | Required (after merging): the backend that reads this target. |
| `standard` | string | `"c11"`, `"gnu11"`, `"c++20"`: what the generated code needs. |
| `hosted` | bool | `false`: freestanding, the generated code uses no libc. |
| `wrapping` | string | `helpers` (default): signed `+ - *` (and `u8`/`u16` `*`) go through `KIF_ADD_*` helpers. `builtin`: plain operators, which needs `-fwrapv` / `-fno-strict-overflow` in the build (`linux-kernel` and `freebsd-kernel` set it: both kernels build with `-fwrapv` / `-fno-strict-overflow`). See `docs/kif-c-backends.md` "Arithmetic wrappers". |
| `access` | object | Access tiers per access class (`index`, `range`, `optional`, `div`, `shift`). |
| `access.index` | string | `prove` (default) drops the check of `a[i]` on an array when `i` is a constant in range; `checked` always checks. No `unchecked` tier: that is `unsafe` code. See `docs/kif-proc-language.md` 4.19. |
| `access.range` | string | Same for `a[lo..hi]` on an array with constant bounds. |
| `access.optional` | string | Same for `optional::value()` once the IR prover shows the optional holds a value. |
| `access.div` | string | Same for `/` and `%` (and `/=`, `%=`): `prove` drops the zero-divisor / `MIN / -1` test when the divisor is a non-zero constant or is shown non-zero (`if d != 0`, `d > 0`). |
| `access.shift` | string | Same for `<<` / `>>`: `prove` drops the "shift count is not below the width" test (and the negative-count test of a signed amount) when the amount is a constant below the width, `n & 31`, an unsigned `n % 32`, or a slot bounded by `if n < 32`. |
| `visibility` | object | ELF symbol visibility (`#[visibility(..)]`, `docs/kif-proc-language.md` section 4.17a). Kernel code may or may not use it, so the target decides. |
| `ir` | object | IR pass pipeline settings. |
| `ir.passes` | string | The IR passes that run before code generation (docs/kif-ir-passes.md). Default `"default"`: every registered pass, ordered by their constraints. `"default,-drops"` leaves one out; with no `prove` and `elide` pass (`elide` requires `prove`, so leave out both) no check is elided. An error a pass reports stops the build. |
| `visibility.enabled` | bool | Emit `KIF_VISIBILITY_*` attributes. Default: the value of `hosted`. When false, `#[visibility]` is checked and ignored. Set `true` for a kernel built with `-fvisibility=hidden`. |
| `types` | map | Overrides the C spelling of a kif scalar (`bool char void i8..i64 u8..u64 usize isize`); defaults are the `<stdint.h>` names. Linux uses `s32`, `u32`, ... |
| `includes` | object | Extra includes of generated files. |
| `includes.header` | list | Added to every generated header, e.g. `"<stdint.h>"`. |
| `includes.source` | list | Added to every generated implementation file. |
| `includes.paths` | list | `-I` directories, relative to the file that lists them. |
| `includes.when` | map | `flag -> {"header": [...], "source": [...]}`; applies while that `features` flag is true. The flag must exist in `features`. |
| `defines` | map | `NAME -> text` emitted as `#define` before the generated code (e.g. `KIF_FREEBSD_KERNEL`). |
| `panic` | object | What `KIF_PANIC` (traps, `panic!`) expands to. |
| `panic.macro` | text | Placeholders `{file}`, `{line}`, `{msg}`; any other `{name}` is an error, other braces are literal C. |
| `panic.header` | string | Header declaring what the macro calls. |
| `panic.noreturn` | `true` | Required with `macro`. The macro must never return (`panic(9)`, `BUG()` and `__builtin_trap()` do not): generated code follows it with `__builtin_unreachable()` and omits the code a returning panic would need. A macro that can return (a logging-only hook) is not a valid `KIF_PANIC`; call it from a wrapper that loops or traps. The checker rejects a macro without this flag. |
| `assert` | object | Same for debug assertions. |
| `assert.macro` | text | Placeholders `{cond}`, `{file}`, `{line}`, `{msg}`. |
| `assert.header` | string | Header declaring what the macro calls. |
| `errno` | object | The `cresult` convention. |
| `errno.style` | `none`, `positive`, `negative` | Default style; an interface's `errno(..)` wins. FreeBSD returns `+errno`, Linux `-errno`. |
| `attest` | object | IR attestation (docs/kif-module-contracts.md section 13). |
| `attest.section` | string | Linker section name (e.g. `kif_attest`). Each C function then also gets a `static const char[]` `"sha256:<full hex> <name>"` in it, so the raw-IR hash (docs/kif-module-contracts.md section 13) survives into the object file. Unset: none emitted. C targets only. |
| `errno.header` | string | Where the errno numbers come from. |
| `errno.map` | map | reloco `error` name -> errno macro or number, overriding the built-in table (`allocation_failed` -> `ENOMEM`, `out_of_range` -> `ERANGE`, `not_found` -> `ENOENT`, ...; the table is `ERROR_ERRNO` in `kiflang/target.py`). Unlisted errors keep the default; an unknown error name is rejected. Example: `{"timed_out": "EWOULDBLOCK"}`. Errors that share an errno still cannot be told apart in a `match` (refused). |
| `taint` | object | Speculation barriers for the taint feature (docs/kif-taint.md section 7). Absent: code that needs one is refused at lowering with an error naming the key; nothing is silently a no-op. The built-in `c` target sets `barrier` for GCC/Clang. |
| `taint.barrier` | text | C statement hiding the variable `{v}` from the optimizer and from speculation; emitted after every `ct::` intrinsic result, e.g. `__asm__ volatile ("" : "+r"({v}))`. FreeBSD kernel: the same asm form. |
| `taint.nospec` | text | C expression clamping `{i}` below `{n}` even under speculation (the `array_index_nospec` idiom). Used by stage 6. |
| `taint.fence` | text | C statement stopping later instructions running speculatively, e.g. `__asm__ volatile ("lfence")`. Used by stage 6. |
| `taint.exports` | `"all"` / `"declared"` | `all`: every parameter of an `#[export]` function is tainted, unless the function is `#[trusted]` (docs/kif-taint.md). Default `declared`: only `#[tainted]` parameters. Applies to the checker and to the IR backstop. |
| `taint.sinks` | list | Operations where a tainted operand is an error, from `div`, `rem`, `shift`, `mul`. Default `["div", "rem", "shift"]`; a CPU with a variable-latency multiplier adds `mul`. A listed set replaces the default. |
| `wipe` | object | Secure wipe (docs/kif-secure-wipe.md section 6). Absent: `memset({p}, 0, {n})` followed by the GCC/Clang asm barrier. FreeBSD kernel: `explicit_bzero`; Linux kernel: `memzero_explicit`. |
| `wipe.call` | text | C statement wiping `{n}` bytes at `{p}`, e.g. `explicit_bzero({p}, {n})`. It need not write zeros (a hardware engine or a TRNG fill is fine). Setting it means the primitive is non-elidable, so no barrier is emitted unless `barrier_after_call` is true. |
| `wipe.barrier` | text | C statement placed after the wipe that reads `{p}`, so the write is not dead; default the GCC/Clang `__asm__ volatile ("" : : "r"({p}) : "memory")`. `""` means none, which is `KIFW006` when a barrier is needed. |
| `wipe.barrier_after_call` | bool | Emit the barrier after `call`. Default `true` for the built-in memset, `false` once `call` is set. |
| `wipe.header` | text | Include for the primitive (`<string.h>` for the default). A bare name is wrapped in angle brackets. |
| `stack` | object | Stack usage estimate (docs/kif-stack.md section 9). All keys optional. |
| `stack.pointer_bits` | 16, 32, 64 | Size of a pointer for `ptr<T>`, `&T`, slices, vectors. Default 64. |
| `stack.align` | power of two | Frames are rounded up to this. Default 16. |
| `stack.frame_overhead` | integer | Bytes per call besides the locals (return address, saved frame pointer). Default 16. |
| `stack.share` | `sum` | `sum` (default): the locals of a function add up. `liveness` (the peak of the locals live at once) is designed but not implemented. |
| `stack.default_budget` | integer or null | Bytes; the default for `--small-stack`. Default null (no budget). |
| `wipe.macro` | identifier | Name of the greppable macro every wipe is emitted through. Default `KIF_SECURE_WIPE`. |
| `wipe.volatile_call` | text | Wipe for `#[secure_wipe(volatile)]`, `{n}` bytes at `{p}`; default a `volatile unsigned char` loop. |
| `alloc` | object or `null` | The allocator of a **C** target; C++ targets use reloco and must not set it (nor `panic`, `assert`, `errno`, `cells`). `null`/absent: nothing that allocates can be synthesized for this target. |
| `alloc.malloc` | text | C expression, `{size}` bytes, NULL on failure: `kmalloc({size}, GFP_KERNEL)`. |
| `alloc.zalloc` | text | Zeroed variant (optional; else malloc + memset). |
| `alloc.realloc` | text | `{ptr}`, `{size}` (optional; else malloc + copy + free). |
| `alloc.free` | text | `{ptr}`; required when malloc or realloc is given. |
| `alloc.aligned` | text | `{size}`, `{align}` (optional). |
| `alloc.header` | string | Header declaring them. |
| `alloc.declare` | text | C emitted right after the includes of `kif_c.h`, for what the expressions need declared (FreeBSD: `MALLOC_DECLARE(M_KIF);`). |
| `kobj` | object or `null` | The kobj emitter; `null` (or absent) means this target has none. |
| `kobj.flavor` | `"freebsd"` | Required in the object. Selects the emitter. |
| `kobj.header` | string | Required. Name of the kobj header; the generated interface header is the only place it is included (kif never includes it itself elsewhere). |
| `kobj.quote` | `angle` or `quote` | `#include <h>` (default) or `#include "h"`. |
| `kobj.include_dir` | string | Directory with the header, for `-I`; relative to the file that writes it. |
| `kobj.extra_headers` | list | More headers the kobj glue needs, added right after the kobj header. |
| `kobj.self_type` | string | C type of an object (e.g. `device_t`); `#[self(..)]` on an interface wins. |
| `kobj.handles` | map | Handle C type to the C expression (`{handle}`) giving the `struct S *` a kif method receives, e.g. `"device_t": "device_get_softc({handle})"`. A type not listed is cast, so the struct must start with the kobj header. `#[object("expr")]` on an `impl` wins. See `kif-kobj-lifecycle.md`. |
| `kobj.lifecycle` | list | Handle types whose `attach`/`detach` the generated class wraps: `S_init` before `attach` (`S_drop` if it fails), `S_drop` after a successful `detach`. For a type not listed the framework or C caller runs them. |
| `annotations` | map | atomic attribute (`might_sleep`, `atomic_only`, `atomic_entry`, `atomic_scope`, `atomic_lock`, `atomic_callback`, `atomic_token`, `atomic_exempt`) to a C macro; `null` or absent: emitted as a comment only. |
| `cells` | object | Providers for `#[tls]`/`#[per_cpu]` (doc 4.17). |
| `cells.tls` | `none hosted freebsd linux custom` | `none`: `#[tls]` is refused on this target. |
| `cells.per_cpu` | `none hosted freebsd linux custom` | Same for `#[per_cpu]`. |
| `cells.header` | string | Provider header; required when either provider is `custom`. |
| `features` | object | Flags. |
| `features.int64_div` | bool | `false`: 64-bit `/` and `%` are refused (KIFP051); true needs a libgcc-free division. |
| `features.floats` | bool | Whether floating point may be used. |
| `features.line_directives` | bool | Emit `#line` directives that point at the `.kif`. |
| `features.doc_comments` | bool | Default true. The `//` comment lines directly above a function, struct, field, enum, enumerator or constant (and its attributes) become a `/** ... */` comment on its prototype or declaration in the header and on a function definition. `*/` inside the text is written `* /`. `false` omits them. |
| `features.body_comments` | bool | Default true. The `//` lines directly above a statement are written as `/* */` lines after its `#line` marker, once each (docs/kif-c-backends.md, "Source comments"). |
| `features.fold_notes` | bool | Default true. A `/* kif: kept, why */` comment after each temporary the emitter did not fold into an expression (docs/kif-ir-codegen.md, "Expression folding"). `false` leaves the C without them. |
| `features.<name>` | bool, integer or string | A **custom flag**: lower-case name of your choice. Read by `includes.when`, and by backends and `cfg(feature = "<name>")` once those exist. |
| `cfg` | map | What `cfg(key = "value")` and `cfg(flag)` see besides the built-in atoms: `name -> "value"`, `["v1", "v2"]` or `true` (a bare flag). E.g. `"os": "freebsd"`, `"arch": "x86_64"`. |
| `profile` | string | The profile used when none is chosen on the command line (default `debug`). |
| `profiles` | map | `name -> {description, opt, debug_info, assertions, overflow_checks, sanitize, cfg, defines}`; merged key by key over the built-ins, new names allowed. |
| `sanitize` | list | Sanitizers on by default, e.g. `["address", "undefined"]`; wins over the profile's own list. |
| `naming` | object | Generated names. |
| `naming.prefix` | `stem` or `none` | Free functions are `<stem>_<name>` or `<name>`. |
| `naming.macro_case` | `upper` or `lower` | Case of generated macros. |

## Code variants: `cfg`

An item, a method, a field or a block can exist for some targets only. Variants are checked **always**: every variant is
type-checked as if its target were selected, so an error in a variant for another target is still reported.

```kif
// An item per target: the two functions share a name because their predicates can never both hold.
#[cfg(target = "freebsd-kernel")]
fn cpu_id() -> u32 { 0 }
#[cfg(not(target = "freebsd-kernel"))]
fn cpu_id() -> u32 { 1 }

struct dev {
    #[cfg(kobj)] obj: u32,             // a field that exists only where the target has kobj
}

#[cfg(kobj)]
fn only_with_kobj() {}

fn run() {
    cpu_id();                          // fine: every target has one variant
    cfg(kobj) { only_with_kobj(); }    // a statement block: type unit, kept only where the predicate holds
    // only_with_kobj();               // KIFP201: not every target has it
}
```

Predicates: bare flags (`c`, `cxx`, `kobj`, any `features`/`cfg` flag or `--cfg` flag), `key = "value"` (`target`,
`feature`, `os`, `arch`, any key a target's `cfg` map or `--cfg` sets), combined with `all(..)`, `any(..)`, `not(..)`.
`target` is the target name and `feature` the true `features` flags. `c`/`cxx` are the language of the target.

Rules (all are checked against every possible environment, so they are conservative):

| Rule | Code | Why / how to satisfy it |
|------|------|------------------------|
| A predicate must be well formed. | KIFP200 | One predicate per `cfg`; combinators are `all`, `any`, `not`; values are strings. |
| Two declarations of one name are allowed only when their predicates are mutually exclusive; otherwise it is a duplicate. | KIF010 / KIFP027 | Make them exclusive (`c` / `cxx`, `p` / `not(p)`, two different `target` values). |
| An item that exists only under a predicate may be used only where the use is covered: the user is under an equal or narrower `cfg` (`#[cfg]` on the function, or a `cfg(..) { }` block), or other variants cover the remaining targets. | KIFP201 | Guard the use, or add a variant for the other targets (it may be a stub). Opt out: none, add an unconditional variant. |

A missing variant is allowed: the item simply does not exist on that target. An `#[cfg]` on an `impl` or a module is not
supported yet; put it on the items.

### Build profiles and sanitizers

Every target has the profiles below (a target's `profiles` merge over them, and may add its own, e.g. `"asan-smp"`). The
choice and the sanitizers are visible to `cfg(..)` and to the backends.

| Profile | opt | debug info | assertions | overflow checks | sanitizers |
|---------|-----|-----------|-----------|----------------|-----------|
| `debug` (default) | 0 | yes | yes | yes | - |
| `perf` | 2 | yes | no | no | - |
| `release` | 2 | no | no | yes | - |
| `sanitize` | 1 | yes | yes | yes | address, undefined |

What `cfg` sees: `profile = "debug"` and the bare `debug`/`perf`/`release` (the profile name; not for `sanitize`, see below),
`debug_assertions`, `overflow_checks`, `debug_info` (flags, set when the profile enables it), `opt_level = "2"`,
`sanitizer = "address"` (one per sanitizer) and the bare `sanitize` (any sanitizer is on), plus the profile's own `cfg` map.

```jsonc
{
  "format": "kif-target", "version": 1, "name": "my-kernel", "extends": "freebsd-kernel",
  "profile": "release",                          // what you get without --profile
  "profiles": {
    "debug": { "cfg": { "witness": true } },      // merged over the built-in debug: adds the flag `witness`
    "kasan": {                                    // a new profile
      "description": "Kernel address sanitizer build.",
      "opt": "1", "debug_info": true, "assertions": true,
      "sanitize": ["kernel-address"],             // names are free-form: the backend maps them to compiler flags
      "defines": { "KIF_KASAN": "1" }             // emitted as #define, like the top-level `defines`
    }
  }
}
```

```kif
fn check_all() {
    cfg(debug_assertions) { expensive_invariants(); }   // only where the profile enables assertions
    cfg(sanitizer = "address") { poison_redzones(); }
}
```

Precedence, lowest first: the target's `cfg`, the chosen profile, `--cfg` overrides (so `--cfg '!debug_assertions'` turns
assertions off in a debug build). `--profile P` picks the profile; `--sanitize a,b` replaces the sanitizer list (`--sanitize
none` clears it); without it the target's `sanitize` applies, else the profile's. `kif target cfg T --profile release` shows the result.

Planned: a backend will use the profile to strip code, e.g. log and trace calls in `release`; until then `cfg(debug_assertions)`
blocks are the explicit way, and only the interpreter honors the environment.

### Command-line overrides

`python -m kiflang check FILE --target NAME|FILE --cfg OPT...` selects the environment the interpreter and the backends
use (checking does not depend on it). Each `--cfg` is applied in order over the target's own set:

```sh
python3 -m kiflang check a.kif --target freebsd-kernel          # the target's own keys and flags
python3 -m kiflang check a.kif --target freebsd-kernel --profile release --sanitize undefined   # profile + sanitizers
python3 -m kiflang check a.kif --target freebsd-kernel --cfg arch=arm64   # key=value: replaces the key (arch)
python3 -m kiflang check a.kif --target freebsd-kernel --cfg debug        # flag: sets it
python3 -m kiflang check a.kif --target freebsd-kernel --cfg '!smp'       # !flag: clears it (and feature = "smp")
python3 -m kiflang check a.kif --target freebsd-kernel --cfg feature=x    # feature adds a value, it does not replace
python3 -m kiflang check a.kif --target linux-kernel --cfg arch=x --print-cfg   # show the effective set and stop
kif target cfg freebsd-kernel --cfg arch=arm64                  # the same set, without a source file
```

## Python API

```python
from kiflang import target

t = target.load("freebsd-kernel")        # a built-in name, or "path/to/my.json"; TargetError lists every problem
t.supports("kobj")                       # True: the target has a kobj emitter (also "tls", "per_cpu", or any bool flag)
t.kobj_include()                         # '<sys/kobj.h>' (or '"bsd/kobj.h"' with quote); None without a kobj emitter
t.includes("header")                     # the includes of a generated header: always + enabled `when` + kobj headers
t.include_dirs()                         # absolute -I directories (includes.paths and kobj.include_dir)
t.flag("smp")                            # any feature flag, custom ones included; False when unset
t.c_type("u32")                          # 'uint32_t' unless `types` overrides it
t.macro("panic", file='"f.kif"', line="12", msg='"oops"')   # the panic text with placeholders filled
t.annotation("might_sleep")              # the macro for an atomic attribute, or None (comment only)
```
