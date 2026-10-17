# Taint: keeping untrusted data out of decisions and addresses

Design document. Nothing here is implemented yet; section 11 lists the stages. Section 12 records the decisions taken in review. It adds one idea to the language: data that comes
from outside a trust boundary is **tainted**, and tainted data may be *computed with* but never *decided on* and never *used to
address memory*. Only a function that is explicitly an `#[untaint]` function can turn tainted data back into clean data. Such a function is a pure
verifier or a speculation barrier and nothing else. Where a decision on tainted data is unavoidable, the language has an explicit
**speculation barrier** (section 7) that the compiler lowers to the platform's primitive.

Read it with docs/kif-proc-language.md (attributes, facts), docs/kif-ir.md (the IR) and docs/kif-ir-passes.md (how passes are added).

## 1. Goal and threat model

An attacker who controls an input (a system-call argument, a packet field, a file's contents, a value a guest wrote) can use it in
two ways that go beyond "the program computes a wrong answer":

1. **As a branch condition or a memory address.** The program's *control flow* and *memory access pattern* then depend on the
   attacker's value. With speculative execution that is enough to leak data the attacker may not read: the processor runs the
   wrong side of a bounds check (Spectre v1) or loads from `table[secret_dependent_index]`, and the cache keeps a trace.
2. **As a loop bound, divisor or shift count**, which make run time depend on the value.

The feature makes (1) and the part of (2) that is a decision a **compile-time error**, so that a kernel or driver written in kif
has no gadget of that shape by construction.

What it **does** guarantee: in checked code, no value derived from a tainted source reaches a branch condition, an index or
address, a loop bound, a divisor or a shift count, unless it passed through an `#[untaint]` function.

What it does **not** guarantee (so nobody relies on it for more):

- It is not a complete Spectre defence. Indirect-branch prediction (v2), store bypass (v4), and microarchitectural data sampling
  are not about *which value the program branches on*. Use the hardening the platform offers for those.
- It says nothing about code outside kif: a C function called with a tainted value is only as safe as its declaration (rule 7).
- It does not prove that a sanitizer's result is *correct*, only that the sanitizer itself makes no tainted decision.
- It does not track timing of data-dependent arithmetic other than division and shifts (section 3, rule 6).

## 2. Concepts

| Term | Meaning |
|------|---------|
| **tainted value** | derived, by any computation, from a taint *source*. Poison: it spreads and never fades on its own |
| **source** | where tainted data enters: a `#[tainted]` parameter, a `#[tainted]` type, a `#[tainted_result]` function (rule 1) |
| **sink** | a use that tainted data must never reach: a decision, an address, a loop bound, a divisor, a shift count (rule 5) |
| **sanitizer** | an `#[untaint]` function; the only way to turn tainted into clean (rule 9) |
| **clean** | everything else |

Taint is a property of **values** (SSA temps) and of **places** (a local slot, a field, a referent). It is *not* control-dependent:
because a tainted value can never decide a branch, there is no implicit flow to track. That is the design's main simplification
and the reason for forbidding decisions instead of tolerating them with a taint on the program counter.

## 3. Rules and rationale

Every rule says what is checked and why it exists. A rule has an opt-out only where one is stated; the opt-out is always a named,
greppable attribute, never a silent default.

### Rule 1 - Sources are declared

Tainted data enters through exactly these, each an explicit attribute:

| Source | Spelling | Effect |
|--------|----------|--------|
| a parameter | `#[tainted] x: T` | every use of `x` in the body is tainted |
| a type | `#[tainted] struct req { .. }` | every value of the type, anywhere, is tainted; a field read of one is tainted |
| one field | `#[tainted] len: u32` inside a struct | reading that field is tainted; the rest of the struct is not |
| a function result | `#[tainted_result] fn read_user(..) -> u32` | the call result is tainted (this is how a body-less `extern` brings data in) |
| exported entry points | policy, see below | parameters of `#[export]` functions |

*Why explicit.* Taint that is inferred from names or from "looks like input" is guesswork; a declared source is reviewable. The
checker never taints something on its own initiative.

*Why a type-level source exists.* "Incoming variable or structure": a request buffer that is passed through ten functions should
be tainted once, in its declaration, not at every parameter.

*Exported entry points.* A function the outside world calls (`#[export]`, a kobj method, a syscall table entry) is where data
arrives. The target or the module chooses the policy:

```json
{ "taint": { "exports": "all" } }    // "all": every parameter of an #[export] function is tainted; "declared": only #[tainted] ones (default)
```

With `"all"`, an exported function whose parameters are all trusted is marked `#[trusted]` (per **function**, not per parameter: a
function that mixes trusted and untrusted parameters should be split, and the attribute stays easy to grep and review). The default
is `"declared"`; a kernel target sets `"all"`. This only chooses *what is a source*; the checking itself cannot be switched off.

### Rule 2 - Taint is poison and propagates through every computation

The result of an operation is tainted when any operand is: arithmetic, comparison, logical operators, casts, field and element
access of a tainted aggregate, struct/array/tuple construction (the aggregate is tainted when any part is, *per field* when the
fields are known), `Project` out of a tainted pattern match, and the result of a call (rule 7).

*Why poison and not "taint only the bytes that matter".* Precision lets a tainted value be laundered by a refactor ("this
comparison only used the top bit"). A single conservative rule can be explained in one sentence and checked in one pass.

*Aggregates.* A struct local has one taint per field (a field-sensitive map). Whole-value uses (a copy, a call argument) are tainted
when any field is. Element taint of an array or vector is one flag for all elements (the same "any index" simplification the
borrow check makes). An array of tainted data is fine as long as the **index** is clean; reading a tainted element through a clean
index yields a tainted value that passes through a speculation barrier (rule 5a).

### Rule 3 - Places are strong-updated, declared places are not inferred

A **local** slot's taint is inferred, flow-sensitively: storing a tainted value makes the slot tainted, storing a clean value to
the *whole* slot makes it clean again (a strong update), writing one field or element only ever adds taint (a weak update). At a
join the taint is the union.

A place with a **declared** taint is never inferred: a struct field without `#[tainted]`, a parameter without it, a `&mut` referent,
a `static`, a vector element behind a reference. Writing a tainted value into one is an error (`KIFT004`).

*Why.* A local is private; everyone who reads it sees the analysis result. A field or a `&mut` referent is shared: another
function holds a clean view of it and cannot know it just became tainted. Failing loudly at the write is the only sound answer
without a whole-program analysis. The fix is to declare the destination `#[tainted]`, which is the honest description of it.

### Rule 4 - References and aggregates keep two taints apart

For `&T` and `&mut T` the **reference value** (the address) and the **referent** are separate. The address is clean unless it was
computed from tainted data (then it is a sink violation anyway). Reading through a reference to a tainted referent yields a tainted
value. Taking `&x` of a tainted local is allowed; the referent is tainted. A `&mut` borrow of a tainted local lets the callee write
it, so the callee's parameter must be declared `#[tainted]` (rule 7) or the call is rejected.

On a reference-typed parameter, `#[tainted]` means **the contents are tainted, not the pointer**: `fn f(#[tainted] m: &msg)` accepts a
reference to tainted data and the body sees tainted reads through `m`, while `m` itself may still be used as an address. A reference
to tainted data therefore can never be passed to a parameter that is not so declared (`KIFT003`).

*Why.* Conflating the two would either forbid `&req` (the pointer is harmless) or let a tainted pointer through.

### Rule 5 - Sinks: tainted data may not reach these

| Sink | Why it is a sink |
|------|------------------|
| `if` / `while` / `for` condition or range bound, `match` scrutinee, `if let`, guard, `&&`, `\|\|` operands | a control-flow decision: speculation runs the wrong side |
| an index `a[i]`, a range `a[lo..hi]`, a pointer offset, a raw address, `*p` with a tainted `p` | a memory address: the cache shows which one |
| `as` to a pointer or `usize` that is then used as an address | same, one step earlier |
| a divisor or modulus, a shift count | variable-latency instructions on many CPUs; also their run-time check is a decision |
| the length of `[x; n]`, the capacity of `vector::try_create(n)`, any `#[lt_len]` / `#[non_empty]` argument | sizes drive loops and checks |
| a call through a tainted function value, or any indirect call (function value, interface) with a tainted argument | an indirect branch target; the target body cannot be seen (rule 7) |
| a run-time `Check` (`index`, `div`, `shift`, `some`, `range`) whose operand is tainted | every check is a conditional branch; **a proved or elided check does not launder taint** |

*Why the last row matters.* The classic Spectre v1 gadget is `if i < len { use table[i] }`: the *check* is the decision. A prover
that elides the check because `i < len` is "known" does not change that the value was attacker-chosen. So facts from the provers
(`lt`, `len_gt`, `#[ensures]`) never remove taint, and the checker runs *before* the provers' output is consulted.

### Rule 5a - `?` and tainted element loads pass through a barrier

Two uses are *allowed* on tainted data because they are common and a barrier makes them safe:

- **`?` on a tainted `result` / `optional`.** The error branch is a decision, but not a gadget by itself. The compiler inserts a
  `Barrier` at the start of both successors, so nothing after the branch executes speculatively on the wrong side.
- **Loading a tainted element** from an array or vector with a clean index. The loaded value is wrapped in a value barrier
  (`Barrier` with an operand, section 7), so the (possibly speculative) load cannot feed a later dependent access.

These barriers are implicit and reported by `kif ir --explain-taint`, so the cost is visible. Writing `if tainted_value { .. }`
stays `KIFT001`: an explicit decision is not rescued by a barrier the author did not write; the author must sanitise (rule 9).

The single opposite case is arithmetic that is *branch-free by construction*: `+ - * & | ^ ~`, comparisons that produce a `bool`
value, and the `ct` intrinsics of section 7. Those are allowed on tainted data; only their result being *decided on* is the error.

### Rule 6 - Variable-latency arithmetic is listed, not guessed

Division, modulus and shifts by a tainted amount are sinks because their latency or their trap is data-dependent on common
hardware. Multiplication depends on the **target configuration**: `"taint": { "sinks": ["div", "rem", "shift"] }` is the default list and a
CPU with a variable-latency multiplier adds `"mul"`. The list is data in the target, not code in the checker, so it can be
corrected without touching the compiler.

### Rule 7 - Calls: a tainted argument needs a tainted parameter, provable at compile time

A tainted value may be passed to a function **only for a parameter declared `#[tainted]`**, and only where the callee is known at
compile time. Otherwise `KIFT003` (or `KIFT008` for an indirect call). "Known" means a direct call to a named function or method;
the checker never relies on a run-time property.

```kif
fn lookup(table: &[u32], i: usize) -> u32 { table[i] }          // i is clean: indexing is fine
fn handle(#[tainted] n: usize, t: &[u32]) -> u32 {
    lookup(t, n)                                                // KIFT003: `n` is tainted, parameter `i` is not #[tainted]
}
```

*Why.* The callee's body was checked on the assumption that `i` is clean (it indexes with it). Passing tainted data in would
silently invalidate that. Declaring the parameter `#[tainted]` re-checks the callee body with `i` tainted, which then fails at
`table[i]` (`KIFT002`) exactly where the real problem is. So the declaration forces the author of the callee to make the callee safe
for tainted input. A function with no `#[tainted]` parameter at all can never receive tainted data.

**Results.** The result of a call is tainted when any argument of the call is tainted, *or* the callee is `#[tainted_result]`,
unless the callee is `#[untaint]` (rule 9). The "any argument" part is conservative (a callee summary computed from its body, which
says which parameters can reach the result, is a later refinement; the `Call.flows` operand numbering already exists for it).
A tainted result returned from a function that is neither `#[tainted_result]` nor `#[untaint]` is `KIFT005`.

**Bodies without a body (`extern`, C, C++, `#[ops]` interfaces).** A declaration is the contract: a parameter not marked `#[tainted]`
refuses tainted arguments; the result is tainted when any argument is. A C function that really sanitizes is declared
`#[untaint(trusted)]` (rule 9).

**Methods and `self`.** `self` is a parameter like any other: `#[tainted] self` marks it. `&mut self` receivers follow rule 4.

**Function values and interfaces.** A tainted argument (or a reference to tainted data) passed through a function value, an
interface method or any other call whose target is chosen at run time is always `KIFT008`. There is no way to declare it
acceptable, so function types and interface declarations carry **no taint syntax and need no taint dataflow**; a tainted value that
must cross an interface is first sanitised (rule 9) or the call is made to a concrete function instead.

### Rule 8 - Returning and storing

A function returns tainted data only if it is `#[tainted_result]`. Storing tainted data in a `static`, a global cell, or sending it
over a channel to another context is rule 3's declared-place case. Panicking with a tainted message is allowed (the message is data).

### Rule 9 - `#[untaint]`: the only way back to clean

A function marked `#[untaint]` (a property of the whole function) returns a **clean** value even though its inputs are tainted.
Its parameters that carry tainted data are `#[tainted]`.

**An `#[untaint]` function is pure: a verifier and a speculation barrier, nothing else.** It may
compute a result from its arguments, and it may use `spec::barrier()` / `spec::value()` (section 7). It must have **no side
effects**: no store through a reference parameter, no write to a `static` or a cell, no allocation, no I/O, no `panic`, no
`unsafe`, and no call except to another `#[untaint]` function, a `ct::` intrinsic or a `spec::` barrier. Violations are `KIFT009`.
It also may not take a `&mut` parameter at all.

**Rejecting input.** A sanitizer must untaint the value instead of aborting. To reject, it returns a clean error code (for
example `ct::select(ok, v, ERR)` or a `result` whose error tag is clean) and puts `spec::value` on the result, so the caller's
`match` or `?` on it sees only clean data behind a barrier. `panic` stays forbidden because it is a decision on tainted data.

*Why.* A sanitizer is the one place where a tainted value is allowed to become clean, so it must be small enough to audit
completely. If it could write memory or call arbitrary code, tainted data could be laundered through its side effects (stored
somewhere clean), and the guarantee "the result is clean because nothing decided on it" would stop being locally checkable. Purity also
lets the compiler call it, duplicate it, or drop it without changing the program's meaning, and lets the provers keep every fact
across the call. `#[untaint(trusted)]` declarations are *asserted* pure by their author and listed by `grep`.

Two forms:

| Form | Body | Used for |
|------|------|----------|
| `#[untaint]` | **checked** under the normal rules and for purity (`KIFT009`), with its `#[tainted]` parameters tainted; only the *result* is declared clean | branch-free sanitizers: masking, clamping, `ct::select` |
| `#[untaint(trusted)]` | none (an `extern`, an `asm` helper, a C macro) or not checked | the platform's own primitive: `array_index_nospec`, a speculation barrier, a hardware-validated copy |

```kif
// Clamp an attacker-chosen index into 0..len without a branch: if i >= len the result is 0.
// The body is checked: it only uses ct:: intrinsics and arithmetic, never `if i < len`.
#[untaint]
fn clamp_index(#[tainted] i: usize, len: usize) -> usize {
    ct::select(ct::lt(i, len), i, 0)        // branch-free: a mask, not a jump (section 7)
}

fn handle(#[tainted] n: usize, t: &[u32]) -> u32 {
    // Every call of an #[untaint] function states why it is acceptable; the reason is copied into the generated code.
    let i = untaint!("clamped to 0..len by clamp_index", clamp_index(n, t.len()));   // i is clean now
    t[i]                                    // fine: the index is architecturally bounded, and bounded under speculation too
}
```

**Every call states a reason.** A call of an `#[untaint]` or `#[untaint(trusted)]` function must be written
`untaint!("reason", f(args))`. The reason is a non-empty string literal that says why treating the result as clean is justified at
*this* site (what the sanitizer guarantees and why that is enough here). The compiler copies it into the generated code as an audit
comment next to the call (section 7), shows it in the LSP, and prints it in `kif ir --explain-taint`. A call without the form, an
empty reason, or an `untaint!` that does not wrap a direct call of an `#[untaint]` function is `KIFT010`; there is no way to call a
sanitizer silently.

*Why a reason at every call.* `#[untaint]` is the one place the guarantee is deliberately weakened, and a certification reviewer
has to see each use and the argument for it, not find the sanitizer's name and infer one. A reason written at the call site is
reviewed with the code it justifies, goes stale visibly when the code changes, and is the audit trail in the emitted C. A single
blanket reason on the sanitizer would say nothing about the site. `untaint!` is a macro form because it needs no new syntax, wraps
exactly one call, and reads as a directive in the source.

*Why the checked body.* If a sanitizer could branch on its input, it would itself be the gadget. Checking its body means the only
tainted decisions in the program are in `#[untaint(trusted)]` code, which is small, audited and written once (in the runtime, see
section 7), not scattered through drivers.

*Why `trusted` is separate and visible.* It is the unavoidable hole, the same role as `#[unsafe]` in this language. It is named so
that `grep untaint(trusted)` lists every place the guarantee rests on a human.

*What `#[untaint]` does **not** claim.* It does not claim the result is *valid* (a clamped index is in range but not necessarily the
right one); it claims no tainted *decision or address* remains. The sanitizer's author owns the semantics.

### Rule 10 - No opt-out

The checks are always errors. There is no `warn` mode, no `off` switch, no per-line suppression comment and no allow-list in the
target. Reason: the feature has few users and limited scope, and anything that can be turned off is turned off by the first
developer it inconveniences, which would leave the guarantee decorative. The only ways to make a use legal are to restructure it,
declare a parameter `#[tainted]` (and fix the callee), or call an `#[untaint]` function. A project with no taint sources pays
nothing: with no source, nothing is ever tainted.

### Rule 11 - Diagnostics list mitigations

Every taint error carries the **origin** and a short list of mitigations that apply to *that* error, so the fix is a choice and
not a search (section 5). The lint (`kif lint`) prints the same list for a use that is accepted only because of an implicit barrier.

## 4. Surface syntax

```kif
// A request as it arrives from user space: the whole type is tainted wherever it appears.
#[tainted]
struct request { op: u32, off: usize, len: usize }

// One field tainted, the rest trusted.
struct frame { magic: u32, #[tainted] payload: u64 }

// A parameter source.
fn parse(#[tainted] raw: u64) -> u64 { (raw >> 8) & 0xff }      // arithmetic on tainted data is fine; result is tainted

// A result source (typically a body-less extern or a copy-in helper).
#[tainted_result]
extern "C" fn copy_in(src: usize) -> u64;

// A sanitizer and its trusted primitive.
#[untaint] fn clamp_index(#[tainted] i: usize, len: usize) -> usize { ct::select(ct::lt(i, len), i, 0) }
#[untaint(trusted)] extern "C" fn array_index_nospec(#[tainted] i: usize, len: usize) -> usize;

// An exported function whose parameters are all trusted although the target taints exports by default.
#[export]
#[trusted]
fn getpid_wrapper(cmd: u32) -> i32 { 0 }

// A pure sanitizer that also barriers its result (section 7).
#[untaint]
fn load_checked(#[tainted] n: usize, t: &[u32]) -> u32 {
    let i = untaint!("clamped to 0..len", clamp_index(n, t.len()));
    spec::value(t[i])           // the loaded value is opaque to later speculation
}
```

Attribute summary (all validated by `facts.py`-style checks; a misplaced one is `KIFT007`):

| Attribute | On | Meaning |
|-----------|----|---------|
| `tainted` | parameter, struct field, struct/enum type | a taint source (rule 1) |
| `tainted_result` | function | the result is tainted |
| `trusted` | `#[export]` function | opt the whole function out of `taint.exports: "all"` |
| `untaint` | function with a body | result is clean; body is checked and must be pure (rule 9) |
| `untaint(trusted)` | function, usually body-less | result is clean; body (if any) is not checked |

## 5. Diagnostics

| Code | Meaning |
|------|---------|
| `KIFT001` | a tainted value decides control flow (condition, bound, scrutinee, `&&`, `\|\|`); `?` is allowed with a barrier (rule 5a) |
| `KIFT002` | a tainted value is used as an index, range, address, size, shift count or divisor (rule 5, 6) |
| `KIFT003` | a tainted argument is passed to a parameter that is not `#[tainted]` (rule 7) |
| `KIFT004` | a tainted value is written into a declared-clean place: field, `&mut` referent, `static` (rule 3) |
| `KIFT005` | a tainted value is returned from a function that is neither `#[tainted_result]` nor `#[untaint]` (rule 7) |
| `KIFT006` | the body of an `#[untaint]` function contains a tainted decision (rule 9); reported as KIFT001/002 with this note |
| `KIFT007` | a misplaced or contradictory taint attribute (`#[untaint]` with no `#[tainted]` parameter, `#[tainted]` on a local, both `untaint` and `tainted_result`) |
| `KIFT008` | a tainted call target, or a tainted argument to an indirect call: function value, interface (rule 7) |
| `KIFT010` | a call of an `#[untaint]` function lacks `untaint!("reason", ..)`, the reason is empty, or `untaint!` wraps something else (rule 9) |
| `KIFT011` | *information, never an error*: an `#[untaint]` function builds its result from comparisons, `bool` casts or masks instead of `ct::` intrinsics and a `spec::value` barrier |
| `KIFT009` | an `#[untaint]` function has a side effect: a store, a `&mut` parameter, an allocation, a panic, or a call to a non-pure function (rule 9) |

### Mitigations listed with every error

| Code | Mitigations the message lists |
|------|-------------------------------|
| `KIFT001` | call an `#[untaint]` function that maps the value to a clean code first; compute with `ct::select` instead of branching |
| `KIFT002` | clamp the index with an `#[untaint]` function (the `array_index_nospec` idiom); use `ct::select` to pick the element; mask the index to a constant size |
| `KIFT003` | mark the callee's parameter `#[tainted]` (and make its body pass); sanitise the value before the call |
| `KIFT004` | declare the destination field or referent `#[tainted]`; keep the value in a local |
| `KIFT005` | mark the function `#[tainted_result]`; sanitise before returning |
| `KIFT008` | call a concrete function instead of the indirect one; sanitise first |
| `KIFT010` | write `untaint!("why this is safe here", f(x))`; state what the sanitizer guarantees at this site |
| `KIFT011` | replace the hand-built mask with `ct::select` / `ct::lt` / `ct::eq` / `ct::mask` / `ct::clamp`; put `spec::value(..)` on the result |
| `KIFT009` | return the computed value instead of storing it; move the effect to the caller; use `#[untaint(trusted)]` for a platform primitive |

Each message names the **origin**: `'n' is tainted (parameter 'n', line 12); it is used as an index here`. The checker records the
first source on the path to every tainted temp, so the message says where the data came from and not only where it was misused.

## 6. Implementation plan

### 6.1 Two layers, as with borrows

Following rule 1 of docs/kif-ir-passes.md ("a check is a second opinion, never the authority"):

1. **The authority: the AST checker** (`kiflang/bodies.py`, with a small `kiflang/taint.py` shared by both layers). It sees
   source names, attributes and spans, so it can give good messages and can run in the editor on every keystroke. It carries a
   taint with every `Ty` value it already computes (`Ty` gains a `tainted` flag, `_Var` gains a taint map by field path).
2. **The backstop: an IR pass `taint`** (`kind="check"`, `requires=("cfg",)`), a forward dataflow over the block graph. It reuses the
   facts rule 5 is about: every `Branch` condition, `Access index/range/deref`, `Check`, `Call` target and variable-latency
   `Binary` (per the target's sink list) is a sink, and a tainted `Load` from a clean index is followed by a `Barrier`; `Load`/`Store`/`Make`/`Project`/`Call` propagate. It catches what the IR builder introduces (a check the
   AST never wrote, an `elide`-style rewrite) and is the one that can be run after optimisation passes.

*Why both.* The AST layer gives the diagnostics people read; the IR layer is where the guarantee actually holds for the code that is
emitted, because by then every implicit decision (a check, a `for` desugaring, a `?`) is an explicit `Branch`.

### 6.2 IR representation

- `Local.taint: bool` (declared) and `Temp.taint: bool` (computed by the pass, stored as a pass result, not in the IR).
- `Call.taint_args: List[int]` (operands that are tainted, same numbering as `Call.flows`) for the codegen's benefit; the pass
  itself recomputes it.
- Function-level: `Function.attrs` already carries `tainted_result` / `untaint`; per-parameter taint is read from the declaration.
- `IR_VERSION` bump to 2 with the fields above plus `Call.flows` / `ensures` / `preserves`, which already changed the shape.

### 6.3 Analysis

A forward dataflow with state `Dict[Local-or-path, bool]` plus a taint per temp, joined by union, run to a fixed point on the CFG
(the same worklist as `borrowck.py`). Transfer functions are rule 2 and 3. Complexity is linear in instructions; the lattice has
height 2 per tracked place, so it converges in a few rounds.

Summaries for calls are the declaration (rule 7), not the callee body, so the pass stays local to one function (rule 4 of
docs/kif-ir-passes.md): deterministic, cacheable, parallel.

### 6.4 Interaction with existing passes

| Pass | Interaction |
|------|-------------|
| `prove` / `elide` | never read taint, and never remove it. `taint` runs on the IR *before* `elide`; a check that was elided still counts as the sink it was |
| `refuted` / `require_proved` | independent |
| `borrows` | independent; a tainted `&mut` loan follows rule 4 |
| `contracts` (`#[preserves]`) | independent; `preserves` says nothing about taint |
| `#[ensures]` | a call's `ensures` fact never un-taints its result |
| `drops` / `tempdrops` | independent |
| structured C output / folding | must not turn a branch-free `ct::` select into a branch: see section 7 |

### 6.5 Editor and tools

- Diagnostics flow through `check_module` and the LSP like `KIFI0xx`.
- Hover over a value shows `tainted (from parameter 'n')`.
- `kif ir FILE --taint-report` is the audit for a security review, one text page per file: the sanitizers (`#[untaint]`, trusted ones
  marked) with their call-site counts, the `#[trusted]` exports, every `untaint!` site with its reason, every speculation barrier
  (explicit and implicit, with counts) and every tainted source. It runs the default pipeline, so the implicit barriers are in it.
  Rationale: a reviewer signs off the *list* of places where the guarantee is weakened or paid for, not the whole program.

  ```
  Taint report: t.kif
  Sanitizers (#[untaint]): 1
    t.kif:2: s, 1 call site(s)
  untaint! sites: 1
    t.kif:11:13: s(): masked to 0..3 [g]
  ```

- `kif ir FILE --explain-taint` prints, per function, the taint of every temp and the origin chain (like `--explain-checks`).
- **Completion and signature help.** After `ct::` the editor offers the five intrinsics with their synthesized signatures
  (`fn ct::select(c: bool, a: T, b: T) -> T`) and parameter help while typing the arguments; `ct` is also offered as a module name.
  The attribute names `tainted`, `tainted_result`, `untaint` (with the argument `trusted`) and `trusted` are completed and
  highlighted. One table, `kiflang/intrinsics.py`, feeds the checker, the completer and this document.
- **Suggestions (`KIFT011`).** An `#[untaint]` body that builds its result from comparisons, `bool` casts or `&`/`|`/`*` with a
  negated or inverted operand, and uses neither a `ct::` intrinsic nor a `spec::` call, gets an *information* diagnostic (severity
  3, printed as `note`; `-Werror` ignores it) at the function name. Rationale: a C compiler may turn a hand-built mask back into a
  branch and the result is not behind a speculation barrier. There is nothing to opt out of: it is advice, and it disappears when
  the body uses the intrinsics.

  ```kif
  #[untaint]
  fn clamp_index(#[tainted] i: usize, len: usize) -> usize {
      let ok = (i < len) as usize;       // KIFT011 (note): hand-built mask
      i & (ok * !0usize)
  }
  // the suggested form: branch-free and barriered by construction
  #[untaint]
  fn clamp_index2(#[tainted] i: usize, len: usize) -> usize { ct::select(ct::lt(i, len), i, 0) }
  ```

- Code lens on each `#[untaint]` function (done): "N callers" so the audited surface is visible.
- **Barrier sites in the LSP.** Every barrier (explicit and implicit) is shown as an information diagnostic and an inlay hint at the
  construct that causes it (`?`, a tainted load, `spec::value`), with the reason and tainted origin, so the author sees what the
  code generator will emit before building. A `trusted untaint` call site is shown the same way.

## 7. Code generation

Taint is a compile-time property: **no run-time representation, no cost**, except for the sanitizers' branch-free code.

- **`ct` intrinsics are built into the compiler.** `ct::select(c, a, b)`, `ct::eq(a, b)`, `ct::lt(a, b)`, `ct::mask(c)` (all ones or
  zero, as `usize`) and `ct::clamp(x, n)` (`x` if `x < n`, else 0). They are *not* declared in `reloco.kif` or in any `extern`
  block: the compiler synthesizes their signatures when it sees a `ct::name(..)` call (a project that declares its own `ct`
  module or local shadows them). Operands are integers of one type (an untyped literal takes it); `select` and `mask` take a
  `bool` condition. They are pure, allowed in `#[untaint]` bodies and on tainted operands, and their result is tainted like any
  computation, except `clamp`, which is `#[untaint(trusted)]` by definition and so needs `untaint!("reason", ct::clamp(x, n))`.
  In the IR a `ct::` call is `Call(kind="intrinsic")`. The C backend emits a branch-free expression wrapped in the value barrier
  (stage 5); the C++ backend emits a call into reloco's constant-time helpers, so the runtime stays reloco's and no kif-level
  contract mirrors it.
- **Value barrier.** A C compiler may turn `mask & x` back into a branch. The emitter wraps the result of a `ct::` operation in the
  target's `taint.barrier` macro, e.g. `__asm__("" : "+r"(v))`, which is opaque to the optimizer. Per target:

  ```json
  { "taint": { "barrier": "__asm__ volatile (\"\" : \"+r\"({v}))", "nospec": "KIF_ARRAY_INDEX_NOSPEC({i}, {n})", "fence": "__asm__ volatile (\"lfence\")" } }
  ```

- **`#[untaint(trusted)]` primitives** map to the target's own: `nospec` (the `array_index_nospec` idiom), `fence` (a speculation
  barrier after an architecturally checked branch). A FreeBSD kernel target uses the kernel's own macros and examples throughout
  the docs.
- **The expression folder** must treat a barriered value as opaque: it already refuses to nest what it does not understand, so the
  barrier call is simply an impure call (one per statement, never reordered).
- The structured output (`structure.py`) is unaffected: tainted values never reach `Branch`, so no structured `if`/`while` is
  built from one.
- **C++ backend** shares everything above: taint is decided before lowering, and `taint.barrier` is per target.

### Speculation barriers in the language and the IR

A barrier is a first-class construct, usable without taint (a hand-written hardened path) and inserted by the compiler where rule 5a
says so. It is allowed in `#[untaint]` functions (it has no side effect on program state).

| Level | Form | Meaning |
|-------|------|---------|
| language | `spec::barrier();` | statement: no later instruction executes speculatively before every earlier branch has resolved |
| language | `spec::value(x)` | expression: returns `x`, opaque to later speculation; a dependent access waits until it is architecturally known |
| IR | `Barrier(kind, operand=None)` | `kind` is `"fence"` (statement form) or `"value"` (the operand's result temp is the barriered one) |

The IR instruction is not a call: the borrow check and provers keep their facts across it, since it changes timing, not state. For
ordering, passes treat it as a fence. `elide` and `fold` never remove or move it; the structured output keeps it at the statement
where it stands.

**Lowering.** Per target, in order of preference:

1. **reloco** (the C++ backend, when it exists): `reloco::nospec::barrier()` for the statement form, which `../reloco` already has for every
   architecture it supports; the value form needs a `reloco::nospec::value(x)` there, added together with that backend (not before: nothing calls it).
2. **plain C** when reloco is not in the build: the target's `taint.fence` / `taint.barrier` snippets, for example
   `__asm__ volatile ("lfence")` on x86, `__builtin_speculation_safe_value(x)` on GCC 9 and later, or an explicitly declared no-op
   on a target that does not speculate (stated in the target, never assumed).

A target that supplies neither lowering makes any use of a barrier an error at lowering time, not a silent no-op.

**Aggregates get one barrier per field.** `spec::value(agg)` on a struct, tuple or array value is lowered to one value barrier per
scalar field (recursively), because a barrier only constrains a register value. An implicit barrier on a tainted aggregate load
does the same. Fields that are not tainted are left alone. Enums barrier the tag and each payload field of the active variant.

**Audit comments.** Every barrier the code generator emits, explicit or implicit, is preceded by a comment so auditors and
certification reviewers can find each one without reading the IR:

```c
/* kif: spec barrier (implicit): `?` on tainted result `r`, kif/net.kif:42 [KIFT-rule 5a] */
KIF_SPEC_BARRIER();
/* kif: spec value barrier (explicit): spec::value(t[i]), kif/net.kif:57 */
v = KIF_SPEC_VALUE(v);
/* kif: spec value barrier (implicit): tainted load of table[i] field `len`, kif/net.kif:63 [rule 5a] */
```

The comment names the kind (explicit or implicit), the reason (rule and source construct), the tainted origin when implicit,
and the source line. The comments are always emitted (no feature flag) because they are the audit trail; `kif ir --explain-taint`
prints the same list. Every call site of an `#[untaint]` function (checked or trusted) carries the author's reason:

```c
/* kif: untaint: clamp_index(): clamped to 0..len by clamp_index (kif/net.kif:61) */
```

## 8. Worked example (what is accepted and why)

```kif
#[tainted]
struct msg { kind: u32, slot: usize, val: u64 }          // rule 1: the type is a source

fn route(m: &msg, table: &mut [u64]) -> bool {
    if m.kind == 7 { return false; }                     // KIFT001: m.kind is tainted (a field of a #[tainted] type)
    table[m.slot] = m.val;                               // KIFT002 (index) and KIFT004 (a tainted value into a clean referent)
    true
}

#[untaint]
fn pick(#[tainted] s: usize, n: usize) -> usize { ct::select(ct::lt(s, n), s, 0) }

fn route_ok(m: &msg, #[tainted] table: &mut [u64]) -> usize {
    let s = untaint!("slot clamped to table.len() by pick", pick(m.slot, table.len()));                   // clean: no decision on m.slot happened outside the sanitizer
    table[s] = m.val;                                    // ok: the index is clean; the referent is declared #[tainted]
    s
}
```

`route_ok` still cannot test `m.kind`: a kind check is a decision on tainted data. The way to dispatch on it is a sanitizer that
maps the value to a bounded clean code first, for example `#[untaint] fn kind_code(#[tainted] k: u32) -> u32` implemented with
`ct::select` chains, then `match kind_code(m.kind)`. That is deliberately inconvenient: every place where attacker data picks a
path is a place a reviewer sees, and the set is small.

## 9. Ways to make a use legal, summarised

| Need | Way |
|------|-----|
| an exported function whose parameters are all trusted although the target taints exports | `#[trusted]` on the function |
| turn tainted into clean | call a pure `#[untaint]` function (branch-free, checked) or an `#[untaint(trusted)]` primitive, as `untaint!("reason", f(x))` |
| a callee that must accept tainted data (or a reference to tainted data) | mark the parameter `#[tainted]`; the callee is then checked for it |
| a `?` on a tainted `result`, a load of a tainted element | nothing: a barrier is inserted automatically (rule 5a) |
| switch the checking off | **not possible** (rule 10) |

## 10. Alternatives considered

- **Taint the program counter (information-flow with implicit flows).** Allows branching on tainted data and taints everything
  assigned under it. Rejected: the goal is *no speculation on attacker-chosen decisions*; allowing the decision defeats it, and the
  analysis (control-dependence, post-dominators) is far heavier. Forbidding the decision is simpler and stronger.
- **Run-time taint bits / shadow memory.** Rejected: cost, and the property is about code shape, not values.
- **Index masking inserted automatically by the compiler** (as some compilers do for `a[i]` under `-mspeculative-load-hardening`).
  Complementary, not a replacement: it hides the problem instead of making the author see it, and does not cover branches.
- **A `tainted<T>` wrapper type** in the library. Cheap to try, but then taint is invisible to the checker's flow rules and cannot
  carry through struct fields and calls without every API cooperating. An attribute on the existing types is checked everywhere.
- **Inferring sources from `#[export]` alone.** Too coarse: trusted functions exist. Hence the policy plus `#[trusted]`.
- **An `enforce: warn|off` mode.** Rejected (rule 10): it would be switched off.
- **Taint in function types and interface declarations.** Rejected (rule 7): compile-time provability only.
- **Sanitizers with side effects.** Rejected (rule 9): side effects can launder tainted data into clean places.

## 11. Stages

1. **Attributes and checker plumbing.** Parse and validate the attributes (`KIFT007`), `Ty.tainted`, field-sensitive local taint in
   `bodies.py`; sinks `KIFT001` / `KIFT002` for locals and parameters only. Tests: one per sink, one per propagation rule.
2. **Calls and places.** Rule 7 (`KIFT003`, `KIFT005`, `KIFT008`) and rule 3 (`KIFT004`), `#[tainted_result]`, struct/field taint. **Done.** Limits: a local carries one taint (no per-field), prelude/builtin methods are not annotated (a tainted receiver or argument gives `KIFT003`), and lambdas are not walked.
3. **Sanitizers.** `#[untaint]` with checked, pure bodies (`KIFT009`), `#[untaint(trusted)]`, the `ct` intrinsics. **Done:** `KIFT009` is checked (`&mut` parameter, store through a reference or to a static/parameter, `unsafe`, lambdas, any macro but `untaint!`, any call that is not `#[untaint]` or in the `ct::`/`spec::` namespaces) and `#[untaint(trusted)]` bodies are skipped.  The `ct::` intrinsics are typed, tainted-checked, lowered to the IR and interpreted; their C emission is stage 5.
4. **IR backstop.** Done: the `taint` pass (`kiflang/taint_ir.py`) runs by default and in `check_module`, so `kif lint` and the editor show it.
   `Local.taint` (a `#[tainted]` parameter) and `Call.taint` (`untaint` / `tainted_result`) are new IR fields, serialized, `IR_VERSION` is 2.
   It reports KIFT001, 002 and 008 with the walker's message format; a test checks it agrees with the walker on the sink corpus.
   Limits: KIFT003-005 need declared types and stay walker-only; `kif ir FILE --explain-taint` lists, in source order, the tainted sources, every barrier (explicit or implicit, with the reason) and every `untaint!` reason (`taint_ir.explain`, also what the editor will show in stage 7); `check_module` runs only when the walker found no error, so the pass is a pure backstop and never repeats a walker diagnostic.
5. **Codegen.** Done for the `ct::` intrinsics: the `taint` target section (`barrier`, `nospec`, `fence`, validated placeholders; `c` sets `barrier`),
   branch-free C for `select`/`eq`/`lt`/`mask`/`clamp` on the unsigned type of the same width (signed order by flipping the top bit), the
   barrier after each result, an audit comment before it, and a `LowerError` naming `taint.barrier` when the target has none. Tests compile
   with gcc and compare with the interpreter, exhaustively on 8 bits, and check the comment and barrier in the text. `nospec` and `fence`
   are schema-only until stage 6 uses them.
6. **Barriers.** Done in part: `spec::barrier()` and `spec::value(x)` are compiler intrinsics like `ct::` (typed in `bodies.spec_call`, KIFP011 on a wrong
   argument count, interpreted as identity), lowered to the new IR instruction `Barrier(kind, dst, a, why)` (serialized; the `IR_VERSION`
   bump to 2 already covers it), printed by `kif ir`, pure in `#[untaint]` bodies, completed and signature-helped in the editor. `spec::value`
   passes taint through (only an `#[untaint]` function cleans). The C backend emits `taint.fence` / `taint.barrier` with an audit comment
   (`explicit`, or `implicit` plus the reason when `why` is set) and refuses with an error naming the missing key. Implicit barriers (rule 5a) are done: the `barriers` transform pass (`taint_ir.insert_barriers`, after `taint`, in the default pipeline) puts a fence at the
   start of both successors of a `?` on tainted data and a value barrier after a tainted element load through a clean index, each with `why` (rule,
   origin, line) for the audit comment; running it twice adds nothing. Per-field barriers are done in the C backend (`ir_c.barrier_leaves`): a struct field by field, an array element by element in a loop (in place, an array temp is the
   pointer), nested to any depth, an enum as its tag. Any other type (a tuple, a vector, a data-carrying enum) is a lowering error, not a silent no-op.
   Stage 6 is complete.
7. **Reasons.** `untaint!("reason", f(..))`, `KIFT010`, `Call.reason` in the IR and the audit comment in the C output (done with stage 1).
   The editor part is done: inlay hints `⛓ barrier` (explicit or implicit, the tooltip has the reason and origin) and `🛈 untaint: <reason>` at the end of the
   expression, from the same `taint_ir.explain` as `--explain-taint`, cached per function with the check hints, switched by `kif.server.inlayHints.taint`
   (default on, independent of `checks`).
8. **Policy and tooling.** *(policy done: `taint.exports` and `taint.sinks` are target keys, carried to the checker through the cfg environment
   (`taint_exports`, `taint_sinks`) and to the IR backstop through `Function.taint_sinks`; `mul` is the opt-in sink; `KIFT009` was done earlier.
   Hover and code lens are done too: hovering a function shows what its `#[untaint]`, `#[tainted_result]`, `#[trusted]` and `#[tainted]` attributes mean, and a code lens above each `#[untaint]` function reads `untaint: N callers` (`trusted untaint: ...` for the trusted ones), a click lists them.)* Original list: `taint.exports`, `taint.sinks`, mitigation lists, purity check `KIFT009`, hover, lens.

Each stage ends with documentation in this file's style (rule, rationale, opt-out, commented example) and a `tests/c` case.

## 12. Decisions from review

| Question | Decision |
|----------|----------|
| `#[trusted]` granularity | per **function** |
| `?` on a tainted `result` | allowed; the compiler inserts a speculation barrier on both successors (rule 5a) |
| arrays of tainted data | allowed with a clean index; one taint for all elements; the loaded value passes through a barrier |
| references to tainted data | a parameter must be `#[tainted]`; that taints the **contents**, not the pointer (rule 4) |
| multiplication | a target-configured sink (`taint.sinks`); not a sink by default |
| indirect calls and interfaces | tainted arguments are always an error; no taint syntax on function types or interface methods (rule 7) |
| enforcement | always an error; no warn/off/suppression (rule 10) |
| diagnostics | list applicable mitigations (rule 11) |
| barriers | first-class in the language and IR, lowered to reloco or plain C (section 7) |
| `#[untaint]` | per function; pure: verifier and speculation barrier only (rule 9) |
| barrier visibility | emitted code always carries an audit comment per barrier; the LSP shows each as information (section 7, 6.5) |
| aggregates | one barrier per scalar field (section 7) |
| sanitizer rejection | a sanitizer cannot panic; it untaints by returning a clean error code, with the rejecting path mitigated by a speculation barrier (rule 9) |

## 13. Still open

Nothing from review is open. Implementation will raise detail questions (the exact comment grammar, the per-field barrier for
nested aggregates and enums) and they are recorded here as they appear.
