# The kif mid-level IR

`kiflang/ir.py` is a small control-flow-graph IR. It is where analyses and transformations live (proving a run-time check cannot
fail, dropping it, later inlining and more), so none of them has to work on the syntax tree or on generated C.

**Status.** The IR is built from a checked function and can be printed, verified, analysed and encoded as JSON or CBOR. The C
C backend writes every function body from it (`kif-ir-codegen.md`). The basic check provers (section 4) run on it (`kif ir --prove`) and the `elide` pass removes the checks they
prove (`kif-ir-passes.md`).

## 1. Shape

```
fn f(%a: [u32; 4], %i: usize) -> u32 {          // parameters are locals (slots), written %name
  b0.entry:                                      // a block: b<id>.<label>, a list of instructions and one terminator
    t1 = load %a                                 // t<n> is a temp: defined once, by one instruction
    check index 2, 4                             // the run-time test of `a[2]`: needs 2 < 4, panics otherwise
    t2 = access index t1, 2                      // the access itself, unchecked, valid only after its check
    t3 = load %a
    t4 = load %i
    check index t4, 4                            // `a[i]`: the index is not a constant, the check stays
    t5 = access index t3, t4
    t6 = t2 + t5
    return t6                                    // every block ends in exactly one terminator
}
```

- **Locals and temps.** Mutable state lives in `Local` slots and is touched only by `Load` and `Store`. Operands are `Temp`
  (single assignment) or `Const`. There are no phi nodes: a dataflow fact about a variable is a fact about one slot.
- **Checks are explicit.** An operation that can panic is a `Check` plus an unchecked `Access`. A pass that proves the check cannot
  fail sets `Check.proof` to the reason (printed as `[proved: constant]`); nothing is deleted, so the IR stays a record of what
  the source asked for and `kif check --explain-checks` can list what was removed and why.
- **Source mapping.** Every instruction keeps `site`, the syntax node it came from.

| Instruction | Meaning |
|-------------|---------|
| `Load(dst, local)`, `Store(local, src, init)` | read / write a slot; `init` marks the first store of a declaration, as opposed to a reassignment; `drop_old` (set by `drops`, same words as `Dead.drop`) says whether the overwritten value must be dropped first |
| `Move(dst, local)` | a `Load` that hands the value on (the checker marked the use as a move); the slot is empty until stored again. It *is a* `Load`, so a pass that ignores ownership needs no rule for it |
| `Live(local)`, `Dead(local, drop)` | the slot comes into scope (`let`, a pattern binding, a loop variable) / goes out of scope (block end, and every `return`, `break`, `continue` and `?` that leaves it, innermost first, after the leaving value is computed). Parameters never die. `Dead.drop` is filled by the `drops` pass: `""` nothing to drop, `"drop"` drop it, `"flag"` drop it if the slot's drop flag is set (the value was moved on some paths only) |
| `Barrier` | a speculation barrier: `fence` (statement) or `value` (`dst` is `a`, opaque to later speculation); `why` is set for one the compiler inserted. Changes timing, not state: passes keep their facts across it and never remove or move it |
| `Unary`, `Binary`, `Cast` | arithmetic, comparison and conversion (overflow behaviour is the language's, not modelled here) |
| `Call(dst, callee, args, recv, kind, decl, id)` | a call; `dst` is None for unit. `kind` is `free`, `method`, `assoc` (`T::f`, `Self::f`), `value` (a callee that is a value), `macro` (`callee` ends in `!`; `panic!` and its kin are followed by `Unreachable`) or `unresolved`; `decl` is the qualified name of the declaration when the resolver found one; `id` numbers the calls of a function from 1; `flows` lists the operands the result may borrow from (-1 receiver, k the k-th argument; None: unknown, assume all; the borrow check reads it); `ensures` is the callee's `#[ensures]` as `(fact, n)` (`some`, `non_null` with n 0; `len_ge` with the least length) and `preserves` its `#[preserves]` operands, both read by the provers; `unguarded` (set by `elide`) means every fact the callee requires is proved here, so it may skip the callee's guard |
| `Len(dst, base)` | element count of a vector, slice, string or array; the destination is a `usize` |
| `Test(dst, kind, base)` | side-effect-free question: `some`, `non_null`, `non_empty` (the facts of `4.19a`), `failed` (a `result` holds an error: the test of `?`), `has_next` (an iterator has an item) |
| `Check(kind, args, proof, ty, call)` | `index (i, len)`, `range (lo, hi, len)`, `some (v)`, `non_null (v)`, `div (a, b)` and `shift (n)` (type `ty`); `call` is the `Call.id` a requirement check (`fact:param`) guards, 0 for an ordinary check |
| `Access(dst, kind, base, args, name)` | unchecked `index`, `range`, `unwrap`, `deref`, `field`, and `ok` / `error` (the two sides of a `result`, after `?` tested it; the `error` is an `i32` status) and `item` (what an iterator yields) |
| `Borrow(dst, local, mut, path, rng, idx)` | `&local` / `&mut local` (of a field or element too: the root slot is named, `path` says which part). `rng` is `[lo, hi]` for `&a[lo..hi]`, `idx` the element index for `&a[i]` |
| `Cell(dst, name, mut)` | `dst` = the address of a `#[tls]`/`#[pcpu]` cell in this thread's or CPU's block (`CELL.with`, `with_mut`) |
| `Release(value, drop)` | drop an owned temporary nobody holds (`tempdrops` pass); `drop` is `drop` or `flag` |
| `Clobber(local, src, elem, path, idx)` | a write into part of a slot: `a.f = x`, `a[i] = x`, `*p = x`; `local` is None when the root is not a local; `idx` is the index of an element write (`a[i] = x`), which `path` (`[]`) does not hold |
| `Str(dst, text)`, `Global(dst, name)` | a string literal; a named item used as a value (constant, static, enum member, function) |
| `Make(dst, kind, args, names)` | a built value: `struct` (`names[k]` is the field of `args[k]`, a last `..` is the `..base`), `array`, `repeat` (`[x; n]`), `tuple`, `range`, `range_inclusive`, `some` (an optional holding `args[0]`), `none` (an empty optional; the type of `dst` says which) |
| `PatTest(dst, base, pattern)` | whether a value matches a pattern (an optional's `Some` / `None` are `Test`s instead) |
| `Project(dst, base, path)` | the part of a matched value a pattern binds: steps `.i` or `Variant.i` (`Some.0`, `.1`) |
| `Opaque(dst, what, args)` | what is left: closures, macros, `cfg` conditions, the borrow of a place that is not a local |

Terminators: `Jump(target)`, `Branch(cond, then, otherwise)`, `Return(value)`, `Unreachable`.

## 2. API

```python
from kiflang import ir, symbols

mod = symbols.analyze("/p/main.kif", text, None)   # the checked module (types are attached to the tree)
m = ir.build_module(mod.tree)                      # ir.Module: .functions and .skipped [(name, why)]
f = m.functions[0]

print(ir.dump(f))                                  # the listing above
assert ir.verify(f) == []                          # list of problems: missing terminator, bad check, a use that its definition does not dominate

cfg = ir.CFG(f)                                    # successors/predecessors, reverse post-order, dominators
cfg.dominates(f.entry, f.blocks[3])                # every path to the block passes through the entry
cfg.loop_headers()                                 # targets of back edges
for block, check in f.checks():                    # every Check of the function
    if check.proof is None:                        # a pass would set check.proof = "constant" when it proves the check cannot fail
        pass
```

`build_function(fn)` builds one declaration and raises `ir.Unsupported` for a construct it cannot handle; `build_module` catches it and
lists the function in `Module.skipped`, so such code is simply left to the existing backend.

### Types

`Local.ty` and `Temp.ty` are text (it is what the data encoding carries); `Local.type`, `Temp.type` and `Function.ret_type` give the
same as a structured, cached `irtypes.Type`, so nothing compares strings:

```python
from kiflang import irtypes

t = f.locals[0].type                  # e.g. the local `v: &mut [u32]`
t.kind                                # "ref"; the others: scalar, unit, slice, array, tuple, fn, dyn, named, unknown
t.mut, t.target                       # True, the `[u32]` slice type (what a reference, slice, array, vector, optional or ptr holds)
t.target.target.is_unsigned           # True: u32;  also is_scalar, is_integer, is_signed, bits (usize/isize are 64), is_sequence
irtypes.parse_type("optional<u8>")    # a type from text; `str(parse_type(s)) == s`; text it cannot parse is `unknown`, never an error
```

The builder writes the canonical text, generic arguments and tuple members included (`result<vector<u8>>`, `(u32, &str)`), an
array's constant length is kept on the `Local` (`Local.type.length`), and a type the checker does not know is `?` (`unknown`).
A method's `self` is still `&Self` / `&mut Self`, the impl's type is not resolved yet.

### Ownership and scope nodes

`Move`, `Live`, `Dead` and the `path` of a `Borrow` / `Clobber` carry what the AST checker already knows, so a pass does not have to
rediscover it from the tree. The builder reads them straight off the checked AST: `Move` where the checker set `moved` on a use,
`Live` / `Dead` from the scopes it walks, `path` from the place expression (`.f` a field, `[]` any element, `*` a pointer target,
joined: `.items[]`).

```
fn scopes(n: u32) -> u32 { let mut k: u32 = 0; while k < n { let t: u32 = k; if t == 3 { break; } k += 1; } k }

b2.body:
  live %t                     // `t` comes into scope: facts and loans from the last trip round the loop are gone
  store %t, t4  // init
  branch t6, b4.then, b5.join
b4.then:
  dead %t                     // `break` leaves the block that declared `t`: a reference to %t is dangling from here on
  jump b3.done
b5.join:
  ...
  dead %t                     // the normal end of the body
  jump b1.while
```

What a pass can do with them: a `Borrow` whose temp is still stored in a local when the borrowed slot `Dead`s is a dangling
reference (KIFP021); a `Load` or `Borrow` of a slot between a `Move` and the next `Store` is a use after move (KIFP023); a loan
taken on `%b.w[]` conflicts with a write to `%b.w[]` or `%b` but not with one to `%b.n`. The existing provers treat `Move` as a
`Load` and `Live` as "forget what you knew of the slot", so they are unchanged otherwise. Drops are decided by the `drops` pass (docs/kif-ir-passes.md): a `Local` with `owns` set (a vector, a string, a struct with drop glue
or with a field that has) is dropped where `Dead.drop` / `Store.drop_old` say so; a backend that runs destructors by scope (C++) can ignore them.
Not modelled yet: a field moved out of a struct (`let x = s.f`) is not a `Move`.

### What the builder is for

The IR exists for **safety-check elision**, not to replace the C backend, so it models exactly what an elision analysis needs and
nothing more: control flow, every operation that can panic (as `Check` + `Access`), and every way a slot's value can change.
The rule that keeps the analysis sound is: **a slot changes only through `Store`, `Clobber`, or a `Borrow` with `mut`**; everything
else is `Opaque`, which reads values and writes no slot.

```
fn mv(p: &mut pt, q: pt) -> u32 { p.x = 1; let r = &mut p.y; *r = 5; p.x + q.x }

b0.entry:
  clobber %p, 1               // `p.x = 1` writes into part of %p: facts about %p are gone
  t6 = borrow mut %p          // `&mut p.y` borrows the root slot mutably: facts about %p are gone
  store %r, t6
  clobber %r, 5               // `*r = 5` writes through the reference
```

A `mut` borrow can simply forget the facts because the borrow rules forbid reading the slot while the reference lives: a fact about
the slot can only be learned again after the reference is dead. A method call borrows its receiver `mut` when the method takes
`&mut self` (the checker records this as `recv_mode` on the call) or when the checker could not resolve it; a `&self` method or a
move changes nothing. A bare place passed to a macro is borrowed `mut` too, since a macro may write it.

Everything of the language builds: `let` (also destructuring), assignment to any place (`+=` etc. too), `if`, `while`, `loop`, `for`,
`match` (guards, or-patterns, bindings), `break`, `continue`, `return`, `?`, calls (also of a function value), method calls, casts,
operators (`&&` and `||` short-circuit), field access, `&`, `&mut`, `*`, `a[i]` and `a[lo..hi]` (also `..=`) on arrays, vectors and
slices, closures, `cfg { }`, macros (`panic!` and its kin end the block with `unreachable`) and all literals. Only `gen fn` (it runs at
generation time) is left out, listed in `Module.skipped`. Modelled precisely: the checked accesses, `optional::has_value`/`value`,
`ptr::is_null`, `len`, `is_empty`. Everything else about a value (what a `match` arm tests, what a `for` yields, a struct's contents)
is an `Opaque` the analyses ignore, which is safe because it proves nothing.

## 3. Serialisation

Syntax tree and IR are plain data, so tools and modules written in other languages can read and write them.

```python
from kiflang import serial

text = serial.dumps_ir(m)                          # JSON text; serial.dumps_ir(m, as_cbor=True) gives canonical CBOR bytes
back = serial.loads_ir(text)                       # validates: unknown instruction, missing or extra field, dangling id,
                                                   # or a function ir.verify rejects, is a serial.SerialError
tree = serial.loads_ast(serial.dumps_ast(mod.tree, as_cbor=True))
```

```
kif ir FILE                          # text listing
kif ir FILE --format json            # {"format": "kif-ir", "version": 2, "functions": [...], "skipped": [...]}
kif ir FILE --format cbor -o f.cbor  # bytes are never written to a terminal, so -o is required
kif ir FILE --ast --format json      # the syntax tree: {"format": "kif-ast", "version": 1, "tree": {...}}
```

- **IR document.** Functions carry `locals`, `temps` and `blocks`, all numbered; operands are `{"t": id}` (temp), `{"c": value,
  "ty": type}` (constant), `{"l": id}` (local) and `{"b": id}` (block). An instruction is `{"instr": "Check", "kind": "index",
  "args": [...], "proof": null, "at": [line, col, line, col]}`; a terminator uses `"term"` instead of `"instr"`. Types are the
  source text (`u32`, `&[u32]`, `optional<u32>`).
- **Syntax tree document.** `{"k": "<node class>", "<field>": ...}` for every `Node` of `kiflang/ast.py`, ranges as
  `{"range": [line, col, line, col]}`. Only syntax is stored, not what the checker computed (types, moves, resolved names), so a
  consumer checks the tree again. The decoder demands exactly the fields of the class.
- **Versioning.** `version` is checked on decode; a change of the field set of a node or instruction bumps it.
- **Limits.** CBOR nesting is bounded (4096 for a tree, 64 for the flat IR) and a deeper tree is a `SerialError`, not a crash.
- **Why not pickle.** The encoding is the contract: a stable, validated, language-neutral form that a precompiled module (`.kifm`)
  can later embed, with no code execution on load.

## 4. Proving checks

`kiflang/prove.py` finds the `Check`s that cannot fail and sets `Check.proof` to the reason. It deletes nothing itself; the C backend
reads the proofs and drops the check, leaving a `/* kif: ... check elided: why */` comment (`docs/kif-c-backends.md`).

```python
from kiflang import prove

n = prove.prove_function(f)      # sets Check.proof on every provable check of one function; returns how many are new
n = prove.prove_module(m)        # the same for a whole ir.Module
# a check that already has a proof keeps it, so running the pass twice is harmless; checks in unreachable blocks are left alone
```

```
kif ir demo.kif --prove          # the listing with [proved: reason] after each proved check
```

```
check some t3  [proved: some(%o) is known on every path here]      // after `if o.has_value()`
check index t13, t14  [proved: %k < len(%v) is known]              // inside `while k < v.len()`
check index t23, t24                                                // `v[i]` with nothing known about i: the check stays
```

**How it works.** A forward "must" analysis over the CFG: a fact is known at a point only if it holds on every path that reaches
it (facts are intersected at joins; a loop is iterated to a fixed point). Facts are about **slots**, never temps, because only a
slot changes:

| Fact | Meaning | Learned from |
|------|---------|--------------|
| `some(L)` | the optional in `L` holds a value | `Test some` + `Branch` (`has_value`), or a `check some` that passed |
| `non_null(L)` | the pointer in `L` is not null | `Test non_null` + `Branch` (`is_null`, through `!`), or a `check non_null` that passed |
| `lt(K, len(V), c)` | `K + c < len(V)`, `K` an **unsigned** slot, `c` an offset (0 for `k < len`) | `k < v.len()`, `v.len() > k`, `!(k >= v.len())`, `i + 1 < v.len()` (c = 1), `i <= v.len()` (c = -1) + `Branch`, a `check index` that passed, or the counter of `for i in 0..v.len()` |
| `lt(K, n, c)` | `K + c < n` for a constant `n` | `k < 4` + `Branch`, `k <= 3` |
| `eq(L, n)` | the slot holds the constant `n` | `let n = 4`, `n = 4`, `n == 4` + `Branch`; forgotten at any other write, and at a join where the paths disagree |
| `len_gt(V, c)` | `len(V) > c` | `v.len() > c`, `c < v.len()`, `!v.is_empty()` + `Branch` |
| `pos(L)` | the slot holds a value `> 0` | `0 < d`, `d > 0`, `d >= 1`, `d > 5` + `Branch` (the constant must be `>= 0` for `<`, `>= 1` for `<=`) |
| `max(L, h)` | the slot holds a value in `0..=h` | `let s = n & 7` (a masked or reduced value: `x & m` for a constant `m >= 0`, an unsigned `x % k`), or a `shift` check that passed (`h` = bits - 1) |
| `nz(L)` | the slot holds a value `!= 0` | `d != 0` (the other edge of `d == 0`) + `Branch`, or an unsigned `div` check that passed |

A `Branch` on a comparison gives one set of facts on the edge taken and another (the negation) on the other edge. A `check` that did
not panic leaves its own condition true afterwards, so a repeated check is redundant.

**Forgetting.** A slot's facts are dropped by `Store`, `Clobber` and a `Borrow` with `mut` (docs section 1: the only ways a slot
changes), and so is what a temp stood for. A call needs no rule of its own: a call that may change a slot has already borrowed it
`mut`.

| Check | Proved when |
|-------|-------------|
| `index (i, n)` | `n` a constant and `i` is bounded by the same rules as a `shift` amount, to below `n` (`t[h & 7]`, `t[h % 8]` and `t[(h & 3) + 4]` on `[T; 8]`); both constants (a slot holding a constant counts) and `0 <= i < n`; `i` constant and `len_gt(V, c)` with `c >= i`; `i` is `K + k` and `lt(K, len(V), c)` or `lt(K, m, c)` with `m <= n` holds for some `c >= k`; `i` is `len(V) - k` (k > 0) and `n` is `len(V)` |
| `range (lo, hi, n)` | `lo` is 0, `hi`, a constant `<= hi`, `K + a` with `hi = K + b`, `a <= b`, or `K + a` below the bound `hi` sits on **and** `hi` is the length itself, a constant `<= n`, `len(V) >= hi` is known, `len(V) - k`, or `K + b` with `lt(K, len(V), c)`, `b <= c + 1` |
| `some (v)`, `non_null (v)` | the matching fact holds for the slot `v` was loaded from |
| `shift (n)` | `n` is known in `0..=h` with `h` below the bit width of the shifted type `ty`: a constant, `n & m`, an unsigned `n % k`, a slot with a `max` fact or `K < m` (`if n < 32`), any of those plus a non-negative offset. Signed amounts count too, since every such bound includes `0 <=` |
| `div (a, b)` | `b` is a constant other than 0 (and other than -1 for a signed type); or a slot with `pos`; or, for an **unsigned** type, `nz`; or `len(V)` with `len_gt(V, c)`, `c >= 0`. A signed divisor that is merely non-zero can still be `-1` with the dividend `MIN`, so that check stays |

**Deliberately not used.**
- **Signed indexes.** A signed slot below the length might be negative, so no `lt` fact is learned for it.
- **Nothing is taken on trust from `#[ensures]` / `#[preserves]`.** They are consumed (`Call.ensures`, `Call.preserves`), but `ensures` is backed by a run-time `Check` tagged `ensures:<fact>` before each `return` and `preserves` by the `contracts` pass (KIFI030). The *parameter* facts are described under "Parameter facts" below.
- **Anything about memory behind a reference** that another path could change. Facts about a `&mut` parameter are dropped by every
  `mut` reborrow, which is every call that takes it mutably.

**Last element.** `-` wraps, so `len(s) - k` is below `len(s)` only when `len(s) >= k`. `s[s.len() - 1]` is proved after
`!s.is_empty()` or `s.len() > 0`, `s[s.len() - 2]` after `s.len() >= 2`, and `&s[..s.len() - 1]` likewise; with no such fact
the check stays and traps on an empty slice. (Earlier versions wrongly proved these unconditionally.)

**Equal lengths.** `a.len() == b.len()` (true branch, or the false branch of `!=`) gives the fact `len_eq(a, b)`. An index proof
about `len(a)` then also covers `b`: after `if a.len() == b.len()`, `b[i]` with `i < a.len()` and `b[0]` with `a.len() > 0` lose
their checks (the C comment ends `, and len(a) == len(b)`). Reassigning either slice, or a call that borrows it `mut`, forgets
the fact. Writing one element (`b[i] = x`, `b[i] += x` on an array, slice or vector) does not: it cannot change a length, so
the copy loop `while i < a.len() { b[i] = a[i]; i += 1; }` keeps both proofs.

**`match` on an optional.** `Some(..)` and `None` arms are real `some` tests, so inside a `Some` arm (or any arm after a `None`
arm) `o.value()` on the scrutinee needs no check; in a `None` arm it keeps it. Other patterns stay opaque. (`if let`/`while let`
do not exist in kif, and `?` applies to results only.)

**Fields as slots.** A field read (`x.n`, `x.p.y`, `x.o`) of a local, a parameter or a reference behaves like a slot of its own: it
has every fact above (`some`, `lt`, `len_gt`, `eq`, `max`, ...), so `if x.n < v.len() { v[x.n] + v[x.n] }` and
`if x.o.has_value() { *x.o.value() }` lose their checks, and so does `x.a[i]` once `i < 4` (the length of an array field is read
from the struct declaration). A field is forgotten with its root, and on three events:

```kif
// the reference slot `x` itself is unchanged below; what changes is what it points at
fn f(x: &mut s, v: &[u32]) -> u32 {
    if x.n < v.len() {   // learns: x.n < len(v)
        x.n = 100;       // a write into any part of the place is a Clobber of its root: every field fact of x goes
        v[x.n]           // check stays
    } else { 0 }
}
```

- a `Clobber` or a `mut` `Borrow` of the root (a write into any part of it, `&mut x.n`, a method with `&mut self` on a value);
- a call that is handed the root or one of its fields as anything but a scalar or a shared reference (`bump(x)` with `x: &mut s`,
  or a struct that may hold a reference): the callee may write through it;
- a write to the root slot itself (`r = other`).

A shared reference (`&s`) cannot be written through, so facts about its fields survive calls that only receive shared
references. Fields whose type is `slot`, `cell`, `atomic` or `volatile` get no facts, because they can change under a shared
reference. Facts are per field, not per element: `a[i].f` is not tracked.

**Constants through slots.** `let n = 3; a[n]`, and `n + 2`, fold to constants while the slot is not written; at a join the
paths must agree. A loop counter changes in the loop, so it is not a constant there (its bound `k < 3` still is a fact).

**Loops.** `for i in lo..hi` over an unsigned integer (or a literal range `0..4`, whose counter starts at a literal and only grows)
is built as a counting loop on a hidden slot `it`: the head tests `it < hi`, the body copies `it` into `i`, and a `step` block adds
one (`continue` goes there). The copy hands `i` every bound `it` has, so `for i in 0..v.len() { v[i] }` loses its check, as does
`v[i + 1]` when the range is `0..v.len() - 1`. `hi` is evaluated once, like the language says; if the bound is later written the
fact is dropped with it. Other `for` loops stay opaque.

**Conditions.** `&&`, `||` and `!` in an `if` or `while` condition are built as branches, not as a boolean in a slot, so
`if i < v.len() && v[i] > 3 { v[i] }` proves both accesses (the second operand is only reached when the first held). `a || b` gives
`a` false on the way to `b`.

**Parameter facts.** `build_function(fn, resolve)` emits, for each requirement of `facts.requirements(fn)`, an entry `Check` that
is born proved (`proof = "assumed: #[some] on parameter 'o'"`) so the provers learn the fact for the body, and, at each call to
a function `resolve` knows, a `Check` with `tag = "some:o"` per requirement of the callee, `site` = the call. They are proved
by the usual logic, or `refuted` when a constant index meets a known length (`Check.refuted`): `some`/`non_null` use the optional/pointer facts, `non_empty`/`len_ge(N)` are `index(N-1, len(arg))`, and
`lt_len(v)` is `index(arg, len(v))`. The C backend keys them `(call, "req:" + tag)`: all proved means the call goes to the unguarded twin `F__nofacts`, any refuted
one is an error, anything else calls the guarded `F`. Only twinned functions are resolved (`entry=False` otherwise), because
only those enforce their facts.
Tagged checks do not produce editor hints.

## Passes

Analyses run through the pass manager (`kiflang/passes.py`): each pass names what it needs (`requires`) and may say where it
sits relative to others (`after`/`before`); the manager orders them, runs each once per function and caches analyses such as the
CFG. The `default` pipeline is every registered pass (`--passes default,-name` leaves one out), so a new pass runs unless excluded. The standard passes are `cfg`, `verify` and `prove`;
`kif ir --list-passes` lists them, `kif ir F --passes prove --stats` runs some. The IR also complements the AST checker
(kiflang/bodies.py, which stays the authority): a *check* pass may add diagnostics (codes `KIFI###`), never remove one. A
function the builder skips is reported in `ModuleResult.skipped`. Code generation runs the whole pipeline first (the target's `ir.passes`,
default every pass) and is just the final consumer of what the passes left behind. To add a pass, see [kif-ir-passes.md](kif-ir-passes.md).

## 5. Planned

`preserves` to keep facts across a `&mut` call; an interval domain (masks, overflow and division checks); `--explain-checks` on
`kif check`; and generating C from the IR itself rather than reading its proofs through the AST.
