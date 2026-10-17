# Stack usage and bounding analysis

Status: **design, not implemented** (section 10 lists the stages; each one ends with a demo run in `examples/stack/`).

Kernel threads have small stacks (FreeBSD: 16 KiB on amd64 by default, Linux: 16 KiB), and an overflow corrupts the neighbouring
memory silently. kif estimates the stack every function needs, follows the call graph to the **deepest chain**, and compares it to a
budget the author sets. The user-visible surface is already specified in `docs/kif-proc-language.md` 4.11 (`#[small_stack(N)]`,
`#[stack(N)]`, `KIFW010`, `KIFP084`); this page is the design behind it.

Decision: the numbers come from **kif's own estimate** of the IR (layout of locals and temporaries), not from the C compiler's
`-fstack-usage`. Rationale: it is available in the editor and before any C exists, and it works for every backend. Consequence: it is
an estimate of *layout*, not of register allocation or inlining, so it is a **warning, never an ABI guarantee** (section 8).

## 1. Concepts

- **Frame**: the bytes one call of a function occupies: locals + by-value parameters + temporaries kif knows the size of, rounded up
  to the target's stack alignment, plus the return address / saved-frame overhead (`stack.frame_overhead` in the target).
- **Depth**: frame of the function + the largest depth among its callees (the *deepest chain*).
- **Budget**: the limit `N` in bytes from `#[small_stack(N)]`, `#![small_stack(N)]` or `--small-stack=N`; the narrowest wins;
  `#[small_stack(off)]` lifts it.
- **Unknown**: a callee whose frame kif cannot know (extern without `#[stack(N)]`, a call through a function value).
- **Unbounded**: recursion (direct or mutual), reported always, whatever the budget.

## 2. Surface syntax

```kif
#![small_stack(4096)]            // file default: no chain may exceed 4 KiB

// A pure-extern function: its callee's cost is stated, not measured.
#[stack(256)]
extern fn crypto_hash(data: ptr<u8>, len: usize);

#[small_stack(512)]              // narrower: this function and its callees fit in 512 bytes
fn parse(buf: &[u8]) -> u32 {
    let tmp: [u8; 128] = [0; 128];   // 128 bytes of frame
    // ...
}

#[small_stack(off)]              // lifts the file default for this function
fn big() { let scratch: [u8; 8192] = [0; 8192]; }
```

| Attribute | On | Meaning |
|-----------|----|---------|
| `#[small_stack(N)]` | fn, impl, struct, file (`#![..]`) | the depth of this function (and of each method in the impl) must be at most `N` |
| `#[small_stack(off)]` | same | lifts a wider setting |
| `#[stack(N)]` | extern fn (also any fn) | the frame the checker uses for this function instead of its estimate (an override, e.g. a measured value) |
| `--small-stack=N` | command line | default for everything not set |

## 3. Frame estimate

For every function in the IR (`ir.Function`), after the `drops` and `tempdrops` passes (so drop flags and temporaries exist):

1. Each `Local` and each `Temp` whose lifetime reaches a call gets `size`, `align` from the target layout (section 4).
2. Locals are **not** summed blindly: two locals whose live ranges never overlap may share a slot in C, but C compilers differ.
   The estimate is therefore the **sum of all locals** (conservative), unless `stack.share = "liveness"` in the target selects the
   peak of simultaneously live locals. Default: sum.
3. A by-value parameter counts; an argument passed in registers still has a home slot, so it counts too.
4. The total is rounded up to `stack.align` (default 16) and `stack.frame_overhead` (default 16: return address + saved frame pointer) is added.

A temporary that never lives across an instruction boundary (an expression temporary) is a register in practice and is **not** counted;
a temporary of an aggregate type (struct, array) is counted.

## 4. Layout: what is known

Sizes come from one function `layout.size_align(type, target)` (new module `kiflang/layout.py`), reusing the natural 64-bit rules the
`KIFW101` padding note already assumes. Known: scalars (`i8`..`u64`, `usize`, `bool`, `char`), arrays of known size, structs (field order,
natural alignment, `#[repr(c)]`, `#[repr(packed)]`, `#[align(n)]`), enums (tag + largest payload), `ptr<T>`/`&T`/`&mut T` (pointer width),
spans, `str`, `vector<T>`, `secure_vector<T>` (their header structs). Not known: generics (size depends on the instance), extern
opaque types. A local of unknown size makes the function's frame **unknown** (reported `KIFW014`, section 7), never zero.

Pointer width and alignment come from the target (`stack.pointer_bits`, default 64).

## 5. The call graph and the bound

`kiflang/stackgraph.py` builds the call graph from `ir.Call` (`kind` = free | method | assoc, with `decl`).

- Calls through function values (`kind = "value"`), and interface (`kobj`) method calls, have unknown targets: the callee is *unknown*
  unless the declared `#[stack(N)]` of the interface method says otherwise.
- Strongly connected components (Tarjan) identify recursion; any function in a cycle is **unbounded**.
- The depth of an acyclic function is computed bottom-up: `depth(f) = frame(f) + max(depth(c) for c in callees)`.
- The result for every function is one of `Bounded(n, chain)`, `Unknown(why)`, `Unbounded(cycle)`. `chain` is the deepest path (the names),
  so a diagnostic and the editor can show *why* the number is what it is.

Hidden calls count: generated drop functions (`S_drop`), vector helpers and the wipe helpers are called from the function and are part of
its chain with their own frames (kif knows these bodies; for `secure_vector` helpers the frame is a fixed small constant in the target).

Interrupt-level code (`#[atomic_entry]`) can be given its own smaller budget; it is the same attribute.

## 6. Diagnostics

| Code | Severity | Meaning | Fix |
|------|----------|---------|-----|
| `KIFP084` | error | a single local or by-value parameter is larger than the budget | `#[heap]` or pass by borrow |
| `KIFW010` | warning | the depth of the function exceeds its budget; the message names the chain and the biggest locals | move large locals to the heap, shorten the chain |
| `KIFW013` | warning | recursion: the stack is unbounded (always reported, even with no budget when `--stack-strict`) | make it iterative, or `#[stack(N)]` plus a documented bound with `#[allow(...)]` is **not** offered: recursion has no static bound |
| `KIFW014` | note | the bound is incomplete: an extern, function-value call or unsized local has no frame | add `#[stack(N)]` to the extern |
| `KIFW015` | note | `#[small_stack]` on a function the estimate cannot size at all | |
| `KIFW016` | warning | `#[small_stack]` is malformed: it takes one positive byte count or `off` | fix the argument |

*Opt out:* `#[small_stack(off)]` for a function or scope; `--small-stack=off` for the build. Nothing is silent: a function with no budget set is
simply not checked, but `kif ir --stack-report` still lists its depth.

Note on codes: the wipe guard used `KIFW010` before this feature existed; it was renamed `KIFW012` so that `KIFW010` keeps the meaning
that `docs/kif-proc-language.md` 4.11 already gives it.

## 7. Tools

- **`kif ir FILE --stack-report`**: one page per module: every function with frame, depth, deepest chain, and the budget in force; unknown
  and unbounded ones listed separately. Sorted by depth, so the worst is on top.
- **Frames** come from `stack.frames` (after the `default` pipeline, so drop flags and temporaries exist); the diagnostics come from it.
- **Contracts** (`docs/kif-module-contracts.md`): the `.kifm` per-function record gains `stack` (the frame, or `null` for unknown) and
  `stack_depth`, so an importer can bound a call into a precompiled vendor module without its source (informational, like `wipes`).
- **Editor**: hover on a function shows `Stack: frame 96 bytes, deepest chain 412 bytes (f → g → h)`; an inlay hint at the function
  name with the depth when a budget is set; `KIFW010` carries quick fixes that raise or lift the function's `#[small_stack]`. A `#[heap]` fix is deliberately not offered: the attribute is specified (`docs/kif-proc-language.md` 4.11) but its checks (KIFP080/081) are not implemented, so inserting it would change nothing and mislead.
- **`kif stack-check FILE --cc gcc --flags=-O2`** (last stage): compiles the generated C with `-fstack-usage` and **compares the
  measured frames to the estimate**, reporting functions where the compiler used more. This does not replace the estimate (decision above);
  it tests how good it is. `-fstack-usage` is GCC/clang; the check is skipped with a note on other compilers.

## 8. Limits (stated honestly)

- **Inlining** can merge frames (smaller than the estimate) or hoist a callee's locals into the caller (the sum stays the same or grows).
  The estimate is a sum over the written functions.
- **Register allocation** and **spills** change real frames in both directions; padding and `-fstack-protector` canaries add bytes.
- **Dynamic stack use**: `alloca`, VLAs, and foreign C code are not seen; `#[stack(N)]` states them.
- **Interrupts** run on the interrupted thread's stack on some targets; the budget for the base function must leave room (author's job:
  set `#[small_stack]` on the thread entry to the thread stack minus the interrupt allowance).
- The estimate is conservative about **layout** (all locals summed), not about the compiler, so it can both over- and under-report. That is why it is
  a warning and not an ABI, and why stage 6 measures it.

## 9. Target section `stack`

```json
"stack": {
  "pointer_bits": 64,        // the size of a pointer, for ptr<T>, &T, spans
  "align": 16,               // frames are rounded up to this
  "frame_overhead": 16,      // return address + saved frame pointer per call
  "share": "sum",            // sum | liveness: how locals of one function combine
  "default_budget": null     // N, the default for --small-stack; null: none
}
```

All keys optional; the defaults above apply. The FreeBSD and Linux kernel targets set `default_budget` to a value that leaves headroom on a
16 KiB stack (documented in `kif-targets.md` rows when implemented).

## 10. Stages

Each stage ends with: tests, `kif doc` / this page updated, and the demo `examples/stack/` run with its generated output beside the source.

1. **Layout.** **Done** (`kiflang/layout.py`: `size_align`, `struct_table`, `report`; `Target.stack()`; `kif-targets.md` rows; `tests/test_layout.py`; demo `examples/stack/` writes `layout.txt`). Enums are C-like in kif, so an enum is its `repr` or a C `int`; `optional<T>`/`result<T>` are a one-byte tag then the payload; `ptr`, `vector`, `str` and slices have fixed header sizes; a generic, an imported or an unknown type is `None` (unknown, never 0).
2. **Frames.** **Done** (`kiflang/stack.py`: `frame`, `frames`, `report`; `kif ir FILE --stack-report`; `#[stack(N)]` override; `tests/test_stack.py`; demo `stack-report.txt`). It is a function over the built IR, not a registered pass: it needs the module's type table and the target, which a per-function pass does not get. It runs after the `default` pipeline. What counts: every local and by-value parameter, plus *built* temporaries (array/struct/tuple literals and call results of aggregate type; a temporary stored into a slot takes the slot's type, since the C emitter materializes `kif_tN` and then copies). Loads and field/index accesses read a slot in place and are not counted. A literal whose type kif cannot recover is listed as "not sized" and not counted. `KIFP084` moved to stage 4: it needs the budget. `share = "liveness"` is implemented (`stack._ranges`, `_peak`): the frame is the peak of the variables live at once instead of their sum, with live ranges taken from instruction order (first mention to last `Dead`; parameters for the whole call; a temporary from definition to last use). It is an estimate of what an optimizing compiler does, not an upper bound: choose it per target (`stack.share`) when `stack-check` shows your compiler reuses slots.
3. **Call graph and depth.** **Done** (`kiflang/stackgraph.py`: `analyze`, `diagnostics`, `report`; `kif ir --stack-report` prints frames, then depths; `tests/test_stackgraph.py`; demo `stack-depth.txt`, `recursion.txt`). Nodes are the qualified names of `modsec._decls` (`f`, `Type::method`, `<T as I>::m`, extern functions); edges are calls the IR resolved to a declaration, `extern "C"` calls found by name (the IR builder does not resolve them), and a *hidden* call: the user `Type::drop` that a `Dead`/`Release` with drop glue runs. Generated helpers are hidden edges too, costed **by the target's backend**, not by a constant (`kiflang/stackhelpers.py`): a call of a vector/string `try_*` helper (not the inline accessors) goes to `<generated helper vector.try_push>` and the drop glue of a `vector`, `secure_vector`, `str`, or `optional` to `<generated drop glue vector>`. `ptr<T>` is costed **per type**, because `T_try_new` and `T_delete` are generated once per heap type: a `ptr::try_allocate` goes to `<generated ptr T new>`, whose own frame holds the out pointer, the arguments of `T::try_construct_at` (so it grows with their size), `p` and `rc`, and which calls that constructor; dropping a `ptr<T>` goes to `<generated ptr T delete>`, which calls `T::drop`. Both add `stack.alloc_frame`, the stack of the target's allocator (default 0: kif cannot see inside `kmalloc`). If a constructor parameter cannot be sized, the node is a lower bound. For a `c`-language target the estimator generates the very helpers `lower_c.Lowerer` writes for that target (its allocator, wipe primitive and errno values), sizes each function's parameters and locals with the target's pointer width, and follows the calls between helpers (`try_push` -> `grow` -> `try_reserve`): for `c` that gives `vector.try_push` 112, `vector.try_reserve` 48, `vector` drop 32 (measured with GCC `-O0`: 48+32+48 and 32). The C++ backends (`cxx-*` targets) have no estimator, because kif has no C++ code generator yet: the hidden cost is unknown, the depth is a lower bound and `KIFW014` says why; `stack.helper_frame` / `stack.drop_frame` give one cost for all helpers (or drop glue) and win over any estimator, which is how a project states what its own containers use. A new backend registers a function in `stackhelpers.ESTIMATORS[language]` returning its `Costs`. They appear in the chain by those names, and `stack-check` marks such a chain `partly estimated` since the real frames are many symbols. Measured: at `-O1 -fno-inline` the chain of a function that creates and pushes to a vector is 208 bytes estimated and 208 measured. Not hidden edges: generated struct drops of secure structs. Strongly connected components use an iterative Tarjan (a long chain must not hit Python's recursion limit); a function in a cycle, or that reaches one, is unbounded. An extern costs its `#[stack(N)]` (on the `extern` item), else 0 with a note, so the depth is then a *lower bound* and `KIFW014` says which calls are missing; a call through a function value, an unresolved call or a function whose own frame is unknown does the same. A call with no declaration (a prelude helper such as `min`) is inline code and costs nothing. Note: kif already refuses recursion in lowered code (`KIFP032`), so `KIFW013` is a safety net for cycles it cannot see (a function value, an interface), and the `recursion.kif` demo is an error example. The diagnostics are produced by `stackgraph.diagnostics` and printed by `--stack-report`; they join `kif lint` and the editor with the budgets in stage 4/5.
4. **Budgets.** **Done** (`kiflang/stackbudget.py`: `resolve`, `check`, `parse_option`; `kif ir --stack-report --small-stack=N|off`; `tests/test_stackbudget.py`; demo `stack-depth.txt`). Precedence, narrowest first: the function's `#[small_stack(N)]`, its `impl`, its struct, the file's `#![small_stack(N)]`, `--small-stack`, the target's `stack.default_budget`. `off` at any level lifts every wider one; `--small-stack=off` lifts the command line and target default only (attributes are narrower and still apply). `KIFW010` (warning) fires when the depth is over the budget, including a lower bound over it; an unbounded function is `KIFW013` instead. It names the chain and the biggest variables. `KIFP084` (error) fires when one variable alone exceeds the budget. A malformed `#[small_stack]` is `KIFW016`. `KIFW015` (note) says a function with its own `#[small_stack]` could not be checked because its frame cannot be sized (a generic); the diagnostics join `kif lint` and the editor in stage 5.
5. **Tools.** **Done** (`kiflang/stackinfo.py`: `analyse`, `diagnostics`, `describe`, `hint`; `tests/test_stacktools.py`; demo `tools.txt`). `analyse` runs the default pipeline, the graph and the budgets once per tree and caches the result on it. `kif lint` and the editor get `KIFW010`, `KIFP084` and `KIFW016` through `passes.check_module`, and only when some budget is in force (a file without `#[small_stack]` pays nothing); estimates use the `c` target. Hover on a function: `Stack: frame N bytes, deepest chain M bytes (a → b → c); budget B bytes (where it came from)`; unbounded and unsizable functions say so. Inlay hint after the name of a function with a budget: `stack 248/300`, `⚠ stack 280/250` when over, `+` when a lower bound. Quick fixes for `KIFW010`: raise the function's own `#[small_stack(N)]` to the depth rounded up to 64 bytes, or lift it with `#[small_stack(off)]` (a narrower budget than the one from the impl, struct or file is added the same way). The `#[heap]` fix of the first design is not offered: no such attribute exists. The `.kifm` function entries carry `stack`, `stack_depth` and `stack_exact` (informational: the importer's own estimate is what counts). `kifc --small-stack=N|off` applies the command-line budget during a build: `KIFW010` is a warning (an error with `--Werror`), `KIFP084` an error. `kifc` prints these only when some budget is in force, using the build's target for the estimate.
6. **Measurement.** **Done** (`kiflang/stackcheck.py`: `check`, `render`, `parse_su`; `kif stack-check FILE [--cc gcc] [--flags=\"-O0\"] [--keep DIR] [--target T]`; `tests/test_stackcheck.py`; demo `stack-check.txt`). It lowers the file to C, compiles every unit with `-fstack-usage -c` and reads the `.su` files. Per function it prints kif's frame, the compiler's frame and the chain depth with the compiler's frames substituted (`partly estimated` where a callee has no measurement). Verdicts: `ok`; `FAIL` (the compiler used **more** than the estimate: budget checks for that function cannot be trusted; exit status 1); `dynamic` (alloca/VLA, which the estimate does not see); `unmeasured` (inlined or removed at those flags, use `-fno-inline`); `unestimated`. The file must lower to C, so a function the C backend refuses (a generic) fails the whole check. Pass the flags as one `--flags=...` string, with the `=`. A finding worth knowing: on distribution GCC (stack protector on by default) the check FAILs a 600-byte array function by about 120 bytes; with `-fno-stack-protector` it passes at 92%. The canary is a build choice, so either raise the target's `stack.frame_overhead` or measure without it.
   **Your own build, GCC or clang:** `kif stack-check FILE --su DIR_OR_FILE` skips the private compile and reads the `.su` files your build wrote (`--su` repeats; a directory is searched recursively). GCC and clang both write `<object minus .o>.su` next to the object (CMake: `x.c.o` gives `x.c.su`); they differ only in the line format (GCC `file:line:col:name<TAB>bytes<TAB>kind`, clang `file:line:name<TAB>bytes<TAB>kind`), and both are parsed. GCC's clones (`f.isra.0`, `.part.N`, `.constprop.N`, `.cold`) fold into `f`, the largest frame winning. The frames are the real compiler's (for one function gcc gave 352 and clang 312), so a FAIL under one compiler can pass under the other. Pass `--profile`, `--cfg`, `--sanitize` and `-I` as the build passed them to `kifc`; `--sysroot DIR` (the only place imports read from; then `-I` is unused) is supported for the analysis. An imported kif module is not compiled by the private compile (its `_proc.h` is missing: the problem says so), so for programs with imports build them and use `--su`. **CMake:** `kif_stack_check(<name> SOURCES a.kif... [TARGET t] [FLAGS f...] [ALL])` adds the custom target `<name>`; with `TARGET` it adds `-fstack-usage` to that target and reads its objects' `.su` files (build the target first), without it compiles privately with `CMAKE_C_COMPILER` and the C flags (`docs/kif-cmake.md`). `tests/test_cmake_freebsd.py` covers both compilers. **Known gap at `-O0`:** the compiler gives every scalar temporary its own slot, which the estimate (aggregates only) does not count, so tiny functions can FAIL at `-O0` (a function with a vector: 160 measured against 96 estimated) while they pass at `-O1`/`-O2`; measure with the flags you ship. Unhandled: LTO and `-S` runs, which place `.su` files elsewhere.

## 11. Alternatives considered

- **Only the compiler's `-fstack-usage` plus a call-graph script**: accurate for one build, but unavailable in the editor, tied to
  GCC/clang, and blind to the source-level cause ("this local is 4 KiB"). Kept as stage 6, as a cross-check.
- **Liveness-based sharing by default**: closer to what optimizers do, but unsound as an upper bound when the C compiler does not
  share slots (`-O0`). Sum is the default; `share = "liveness"` is opt-in.
- **Making the limit an error**: the estimate is not exact, so an error would reject correct programs; a warning plus a
  measuring stage is the honest contract.
