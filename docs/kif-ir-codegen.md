# C code generation from the IR

The C generator reads the IR (docs/kif-ir.md) after the pass pipeline (docs/kif-ir-passes.md), so every pass is used by
construction. It is the **only** generator of function bodies: the former AST-walking generator is gone. `kiflang/lower_c.py` still
writes everything around the bodies (types, signatures, twins, helpers, cells, the header) and `kiflang/ir_c.py` writes the bodies.

A function the emitter cannot write is a **build error** (`` `f` is not lowered to C: <reason> ``), never a silent fallback. Two kinds of
reason exist: constructs C cannot honour (the *refusals*, below, found by the `c_refusals` pass with a diagnostic code) and
types or shapes the emitter has no form for (an array of owning elements, a closure passed to a cell, `optional<optional<T>>`, tuples,
`[str; N]`, ...), which `kif ir FILE --coverage` lists.

## Steps

1. **Coverage** (done): `kif ir FILE --coverage` / `ir_c.blockers(fn)` list what the IR cannot yet describe, per function and as
   a histogram, so each later step adds the node that unblocks the most functions.
2. **Structured types** (done): `Local.type` / `Temp.type` / `Function.ret_type` are `irtypes.Type` values (scalar, reference,
   slice, array, tuple, `fn`, `dyn`, named with arguments), parsed from the text the IR carries (docs/kif-ir.md, "Types").
   Still open: a method's `Self` is not resolved, and a struct's fields are looked up by name in the symbol table, not in the IR.
3. **Real nodes for values** (done): `Str`, `Global`, `Make` (struct, array, repeat, tuple, range), `PatTest` / `Project` for patterns,
   and `?` / `for` as `Test` (`failed`, `has_next`) plus `Access` (`ok`, `error`, `item`) (docs/kif-ir.md, section 1). `Opaque` is left
   for closures, macros and `cfg` conditions. Open: a pattern binding's local is still typed `?`, `char` and float literals are
   still `Opaque`.
4. **Resolved callees and drops** (done): `Call.kind` / `decl` / `id` (macros are `Call(kind="macro")`), `Check.call` ties a
   requirement check to its call (so the backend picks `F` or `F__nofacts` by whether all of them are proved), `Local.owns`, and the
   `drops` pass that fills `Dead.drop` and `Store.drop_old` (docs/kif-ir.md, docs/kif-ir-passes.md). Open: `decl` is empty where
   the resolver only knows twinned functions (the old backend's), generics are not instantiated, partial moves and pattern bindings
   are not tracked, and a method's `self` is still `&Self`.
5. **An emitter** (done for scalars and calls): `ir_c.emit_body` writes a `goto` per block and the temps as C locals. It handles scalar
   and array-parameter functions made of `Load`/`Store`, `Unary`/`Binary`/`Cast`, `Check` (`index`, `div`, `shift`), indexing, calls of
   free and associated functions with scalar signatures (`F`, or `F__nofacts` for an `unguarded` call), `panic!` / `unreachable!`,
   branches and returns. Structs whose fields are all scalars work by value and by reference: `Make` `struct` (every field given),
   `Access` `field` / `deref`, `Borrow` of a whole local, and `Clobber` along `.f` / `*` paths (`p->x = v`). A function with anything
   else (ranges, other macros, owned values, ...) is refused and `lower_c.py` writes it.
   C-like enums work too: `Global` `E::m`, `==` / `!=`, and `PatTest` of `E::a | E::b` or `_`; a payload pattern is refused.
   Slices of scalars (`&[T]`, `&mut [T]`) are passed as `kif_span_T` / `kif_cspan_T`: `Len` is `.len`, an index is `.ptr[i]`, and
   an element write is `Clobber` with `path` `[]` and its `idx` (a range slice is refused). A temp that is only set gets `(void)`, and
   an `{integer}` operand is cast to the type its use needs. A local or temp the checker left as `{integer}` is declared `int32_t`, as the AST generator does.
   Methods work: a `method` call passes the receiver first (a `&self` call on a value passes its address), `Self` means the
   `impl`'s struct, and a `Borrow` of a reference local is a reborrow, the same pointer. A receiver borrow's temp is typed `&mut T`.
   Optionals of scalars (`optional<T>` is `struct kif_opt_T { bool has; T v; }`): `Make` `some` / `none`, `Test` `some` is `.has`,
   `Access` `unwrap` is `.v` (its address when `value()` hands out a reference), `Check` `some` traps with "optional has no value", and
   a `Some(x)` pattern's `Project` is `.v`. The AST generator lowers no other payload enums, so there is nothing to compare them with.
   `result<T>` of a scalar `T` (or `result<()>`): the function returns the status `int` and writes the value through a last parameter
   `kif_out`; inside, every result value is two C variables, `x` (the status) and `x_v` (the value). `ok(v)` sets the status to 0 and
   `x_v`; `err(error::e)` sets the target's errno for `e` (`errno.style` / `errno.map`); `Test failed` is `x != 0`; `Access ok` reads
   `x_v`; `Access error` is the status; a `Return` of a result writes `*kif_out` when the status is 0, and the early return that `?`
   builds returns the status. A call of a result function passes `&x_v`. An `ok(..)` still typed `result<?>` takes the function's own
   result type. Not yet: results of structs, optionals and the like, `OUT_FIRST` constructors (`try_create`), and the drops a failing
   `?` has to run.
   Local arrays of scalars (`[T; N]`, `Make` `array` / `repeat`): a local is a C array `T a[N] = {0}`. C cannot copy an array with `=`,
   so a `Load` of one is only a name for it (the temp is an alias, nothing is declared), a `Make` builds into its own array temp whose
   type is taken from the local it is stored into (the checker leaves it `[{integer}; _]`), and a `Store` copies element by element.
   Indexing and element writes are as for slices. Not yet: arrays of arrays or structs, array parameters that are assigned.
   Owned values (a struct with a `drop` method): `Dead` with `drop` "drop" is `S_drop(&x);` and a `Store` with `drop_old` "drop" drops the
   old value before the assignment; the `drops` pass put them where the value is certainly still there, and a value it proves moved
   gets none (the AST generator would keep a run-time flag for it). A value moved on some paths only (`drop` "flag") gets a
   `bool kif_live_<x>`: set by every `Store` into the local, cleared by every `Move` out of it, and each drop becomes
   `if (kif_live_x) { S_drop(&x); kif_live_x = false; }`. Left to the AST generator: owners that are not plain scalar structs
   (vectors, strings) and a temporary nobody holds. A by-value parameter that owns something is dropped like a local: the IR builder lists
   it with the function's outermost locals, so it gets a `Dead` before every `return` / `?` and at the end, unless it was moved.
   The lowerer now hands `build_function` the module's drop types; before, no local counted as owning, so the IR emitter would have
   left every drop out silently.
   **Vectors of scalars.** `vector<T>` is `struct kif_vec_T` (`ptr`, `len`, `cap`). `vector::try_create(n)?` is typed `vector<?>` by the
   checker, so the emitter takes the element type from the local the value is stored into. The `try_*` methods, `len`, `capacity`,
   `is_empty`, `clear`, `v[i]` and `v[i] = x` call the same `kif_vec_T_*` helpers as the AST generator, and the `drops` markers become
   `kif_vec_T_drop(&v)` on every path. Also `try_at` (typed `result<&T>` by the emitter), `as_slice` / `as_mut_slice` (of vectors,
   arrays and slices) and vectors of non-owning structs and enums.
   **Strings.** `str` is `struct kif_str`, `str_view` is `struct kif_strview`; a string literal is a `(struct kif_strview){ "..", n }`
   built where it is used. Methods call `kif_str_*` / `kif_strview_*`; `view()` is typed `str_view` and `len` `usize` by the emitter.
   **Slices, arrays, ranges.** `&[T]` is `struct kif_(c)span_T` (`ptr`, `len`) for scalars, enums, structs and `str`. `&[T; N]` is a `T *`.
   `Borrow` carries `rng` (`[lo, hi]`) and `idx` (the element index): `check range` is `if (lo > hi || hi > n) trap`, and
   `&a[lo..hi]` is `(struct kif_(c)span_T){ base + lo, hi - lo }`. An element borrow `&v[i].f` is `&base.ptr[i].f`; `&mut *a` of an array
   reference is the same pointer. Arrays of structs are C arrays; a load is an alias and a copy is a loop (C cannot assign arrays).
   **Iterators.** `has_next` / `item` use a cursor `kif_it<id>` (`size_t`, set to 0 where the iterator temp is made) over a vector, slice or
   array, owned or behind a reference.
   **Optionals and results.** `optional<T>` of a scalar, enum or struct (also one that owns something, and `optional<str>`) is
   `struct kif_opt_T` (`has`, `v`); the owning ones are dropped with `kif_opt_T_drop`. `value_or`, `try_value` and `reset` call the
   helpers. `result<T>` is an `int` status plus `x_v`; `match` on `ok(v)` / `err(e)` tests the status, and the `error` type is an `int`
   (`error::busy` stands for its errno). Integer patterns (`a | b`, `_`, `lo..=hi`) are comparisons.
   **`#[ops]` interfaces.** `&I` / `&mut I` is `struct I_ref`; a call through it is `I_m(handle, args)`; a `&struct` passed or stored
   where a handle is wanted becomes `<struct>_as_I(&s)`.
   **Typing the checker leaves open.** Untyped temps (`vector<?>`, `&?`, `optional<?>`, `result<{integer}>`, a `len` or a method result)
   are typed by passes at the start of `run` (`type_vectors`, `unify_unknown`) from the locals and returns they meet. Assigning `ty` must
   reset the cached `type`.
   **Temporaries.** The `tempdrops` pass adds `release tN` for an owned value nobody holds (`mk()?.len()`, `mk()?;`, a value built before a
   later failing `?`); the emitter writes it as the value's drop call. It still refuses a function with a release that holds on some paths
   only (`flag`), a by-value method on an owned temporary, and a fresh owned temp that is neither handed on nor released.

   **Heap objects.** `ptr<S>` (owning) and `c_ptr<S>` / `c_cptr<S>` (not owning) are `struct S *` (`NULL` when empty), and a `ptr` counts
   as owning in the IR (`ir.owns_type`), so the `drops` pass puts `S_delete(p);` where the value dies. `ptr::try_allocate(default_allocator(), args..)?`
   is `S_try_new(&x_v, args..)` (the allocator is the target's and is not passed; the pointer type comes from the local it is stored
   into). `check non_null` is `if (p == NULL) <trap>`; a field access, a write through a field or a method call on a `ptr` is wrapped in the
   same `KIF_NN(p, file, line)` the AST generator uses (the IR holds no check for those auto-dereferences). A `Clobber` of an owning field
   or pointer drops the old value first. A `try_construct_at(mem: c_ptr<Self>, ..)` starts with `S_init(mem);` and every error return
   first runs `S_drop_fields(mem)` when the struct owns something. `T::try_allocate(default_allocator(), ..)` of a vector or string is
   `T::try_create(..)`.

   **Source lines.** With `features.line_directives` the emitter writes `#line N "file"` before the first instruction of each new source
   line, so compiler errors and debuggers point at the `.kif`.

   **Thread/CPU cells.** `CELL.with(|v| body)` / `with_mut` lowers to `cell t, NAME` (the address of this thread's or CPU's slot) plus a
   local `v` bound to it and the closure body inline; the emitter writes `kif_tN = &kif_<tls|pcpu>_<stem>_block()->NAME;` (an array cell
   is the pointer to its first element, without `&`).
   See "Step 5: the emitter" below.
6. Differential tests (done): `tests/c/*.kif` with C drivers, the interpreter-compared cases and ASan runs check behaviour; the old
   generator is retired, so there is nothing left to diff against and the tests assert the emitter's behaviour and its output.

## Structured output

The IR is a block graph; printing it as labels and `goto`s would read like assembly. `kiflang/structure.py` rebuilds `if`/`else`,
`while`/`for(;;)`, `break` and `continue` from the graph, and the emitter renders that tree.

```text
# What the structurer does, and why each rule exists
dominators          -> find natural loops from back edges (a loop is the blocks that reach its back edge)
post-dominators     -> the join point of an `if`: where both arms meet again, so it is printed once after the `if`
join outside a loop -> treated as "no join", so `break` leaves the loop instead of falling into later code
visited block again -> an explicit `goto` + label (rare: `&&`/`||` short-circuit merges)
irreducible graph   -> `structure.build` returns None and `structure.flat` prints labels and gotos (always correct)
empty then-arm      -> the condition is negated, no empty `if {}`; `else`-only-`if` chains become `else if`
loop header         -> `while (c)` when the header computes nothing, otherwise `for (;;) { ...; if (!c) break; ... }`
```

`structure.py` knows nothing about C: it returns `Blk`/`If`/`Loop`/`Break`/`Continue`/`Goto` nodes, so a C++ backend reuses it as is.

## Expression folding

The IR is three-address, so the emitter first writes one `kif_tN` per value. `kiflang/textfold.py` folds them back on the emitted statement
lines (text level, so also shareable with C++):

| Rule | Rationale |
|---|---|
| A temp used once, by the next statement, is substituted (comments and `#line` skipped; the branch condition counts as a statement) | moving evaluation one statement later cannot change what it reads |
| ...only if the two statements hold at most one impure call (`KIF_*`, `UINT64_C`, `sizeof` are pure) | two side effects could be reordered |
| A copy of a local or constant replaces all uses in the block while nothing assigns that local in between | pure renaming |
| ...and the local's address is never taken in the function | a call could change it behind our back |
| Never folded: temps with a `_v` companion, address-taken temps, left-hand-side uses, uses in other blocks, compound literals | a missed fold costs readability; a wrong one costs correctness |

A temp that stays is annotated with the reason, so a reader of the C sees why it is not an expression:

```c
kif_t3 = f(a);  /* kif: kept, other statements run between its definition and its use */
```

The reasons: it has a `_v` companion (a result pair), its address is taken, a compound literal cannot be nested, it is read in
another block, it is assigned or has its address taken later, the copied local is assigned before the last use, it is read N times
(and is not a copy), other statements run between its definition and its use, two calls would be reordered. A temp that is never
read is dropped (a final dead-store pass keeps only a call it makes, as a bare statement).

Turn the notes off with `features.fold_notes: false` in the target. The set of call-like names the folder treats as side-effect free
(`textfold.PURE_C`: `KIF_*`, `UINT64_C`, `sizeof`) is a parameter (`pure`), so another backend (C++) passes its own list and shares the rest.

Temps that no longer occur in the text are not declared. Opt-out: none; the folding is semantics-preserving, and the differential tests
and the compiled `tests/c` corpus run on the folded output.

## Coverage report

`kif ir FILE --coverage` runs the real emitter and reports which functions it writes and why it refuses the others:

```
$ kif ir tests/c/cells.kif --no-project --coverage
5 of 5 functions are emitted from the IR      // a refused function is listed as "refused NAME: reason"
```

The older `ir_c.blockers(fn)` / `coverage(module)` API (what the IR alone cannot describe: `type:T`, `opaque:what`, `call:<value>`,
`access:kind`, `clobber:memory`, `skipped:why`) is kept for analysis.

## The emitter

`lower_c.fn_body` calls `ir_c.emit_body(lowerer, fn, cnames)` for every function; there is no target key to choose a generator (the
former `ir.codegen` key is gone, a target that sets it is rejected as an unknown key). The signature, prototype, twin and names come
from `lower_c.py`.

**The emitter decides nothing.** It writes down what the IR holds:

* every `Check` that is in the IR becomes `if (bad) KIF_PANIC(file, line, "message")`;
* every operation that was checked is then emitted unchecked (`a / b`, `a << n`, `a[i]`);
* a `Call` goes where it says.

Leaving a check out is the job of the `elide` pass (docs/kif-ir-passes.md, "What `elide` removes"), which runs before code
generation, so a backend never has to know about proofs or the `access.*` tiers. With `elide` out of the pipeline the output carries
every check. Each elided check leaves a one-line C comment before its statement, so a reader of the output sees what was proved:

```c
/* kif: bounds check elided: constant index 2 < 4 */
kif_t5 = a[UINT64_C(2)];
```

A call marked `Call.unguarded` goes to the `__nofacts` twin when the callee has one.

C shape of a function: locals and temps are declared at the top (initialised, so a jump never reads garbage); each block that is
a jump target gets a label `b<N>_<label>`; a `Jump` to the next block is left out. With `features.line_directives` a `#line N "file"`
precedes the first instruction of each new source line.

Newly supported (the old generator refused or mis-handled them): inclusive ranges `lo..=hi` (overflow-safe iteration), a range slice of
a value `let x: &[u32] = a[1..3]`, `&[str_view]` slices (`fn main(args: &[str_view])`), extern types and `c_ptr` / `c_cptr`, enum
casts `x as E` (a known enumerator is written directly, with an elision note), imported free functions and `dyn` calls, by-value
`self` (C passes a pointer; the value is still dropped), and `global N` cells.

## Refusals

A refusal is valid kif that the C target will not lower. The `c_refusals` pass (`kiflang/c_refusals.py`) finds them on the IR. It is
registered but not in the `default` pipeline, because other targets may honour these programs; the C lowering appends it to the
target's `ir.passes`. Every rule is an error with its code (`KIFC001` is the generic "not lowered to C" error; the rules are `KIFC101`-`KIFC110`):

| code | refused | why |
|---|---|---|
| KIFC101 | a manual `drop` call | a value is dropped when it goes out of scope or is moved; a second drop would double free |
| KIFC102 | a call returning `result<T>` whose result is unused | C reports the error through the status; ignoring it would lose the error |
| KIFC103 | an owning `result` value stored into an owning local | the value would have two owners; take it with `?` or `match` |
| KIFC104 | `match` arms for errors that share an errno | C only has the errno number, so they cannot be told apart; match one or use `_` |
| KIFC105 | `value_or` on an optional that owns resources | it would copy the owner; test `has_value()` and use `value()` |
| KIFC106 | a `Some(x)` binding of an optional that owns resources | the binding would copy the owner |
| KIFC107 | `match` on a temporary optional | the match needs a variable or field to look at; bind it with `let` first |
| KIFC108 | `for` over a vector or string by value | it would move the container; iterate a borrow (`&v`, `&mut v`) |
| KIFC109 | `match` on an owning result with an `ok(..)` pattern | the value would not be dropped for you; use `?` |
| KIFC110 | a call whose `#[..]` requirement provably fails | the call can never succeed, so the program is wrong |

There is no opt-out: each rule protects an ownership or error-reporting invariant of the generated C. Rewrite the program as the
message says. Example:

```kif
fn f(v: vector<u32>) {
    for x in v { }          // KIFC108: iterate a borrow instead
    for x in &v { }         // fine
}
```

Tests: `tests/test_passes.py` (the rules), `tests/c/refuse_*.kif` with `.refuse` files (end to end).
