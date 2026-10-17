# Secure wipe: erasing secrets when they stop being used

Status: **being implemented, stage by stage** (section 12 says which are done). This page is the design for `#[secure_wipe]`, `#[wipe_exempt]` and the `wipe` target section.

## 1. Goal and threat model

Keys, passwords, session secrets and plaintext buffers must not stay in memory after their last use: a later out-of-bounds read, a
core dump, a swapped-out page, a cold-boot image or a leak through reused stack memory would otherwise expose them. The usual fix, a
`memset` before the variable goes away, is silently **removed by the optimizer** (dead-store elimination: nothing reads the memory
afterwards), so the code looks safe and is not.

`#[secure_wipe]` makes the erasure part of the language: the compiler decides where each value is last used, emits the wipe there
through a target-provided primitive (`explicit_bzero` on FreeBSD, or a `memset` plus compiler barriers), says so in a comment in the
generated code, and keeps its own passes from undoing it.

*In scope:* named storage (`let` bindings and struct fields; by-value parameters only when marked individually, rule W8) of plain-data types, and the storage of structs that opt in.
*Out of scope* (section 11 lists them honestly): registers and spill slots the C compiler creates, copies the program makes into
unmarked storage, a crash before the wipe runs, hardware (caches, DRAM remanence), and constant-time behaviour (that is taint, `ct::`).

*Why:* the target use is certification regimes such as FIPS 140, where key material must be destroyed **as soon as possible**. That is why
the wipe follows the last use (rule W1) and why every wipe is explicit and reviewable in the generated code (rule W7).

### 1.1 Applications

**Security certifications.** Several schemes require that secret material is destroyed when it is no longer needed, and a reviewer
asks for evidence, not intent. `#[secure_wipe]` gives that evidence: the source says what is wiped, the generated C says why (rule W7),
`KIF_SECURE_WIPE` is greppable, and `--wipe-report` lists every site (section 8).

| Scheme | Requirement it maps to | How the feature helps |
|--------|------------------------|-----------------------|
| FIPS 140-3 (ISO/IEC 19790) | zeroization of critical security parameters (keys, seeds, passwords) | wipe at the end of the live range, as soon as possible (rule W1); the target primitive can be an approved zeroization routine (section 6) |
| Common Criteria (e.g. `FCS_CKM.4`, cryptographic key destruction) | key destruction by an identified method | the method is the target's `wipe.call`, named in the generated comment |
| PCI PTS payment terminals, smart-card and HSM firmware evaluations | no clear-text key or PIN left in RAM after use | function-level marking covers every local of a routine (rule W8) |
| CWE-14, CWE-226, CERT MSC06-C | the compiler removes a buffer-clearing `memset`; sensitive data not cleared before release | the barrier (section 6) and the guard (rule W11) keep the wipe; the C compiler cannot prove it dead |

The wipe is an engineering control. It supports an evaluation; it does not replace one (section 11 lists what it does not cover).

**Mitigating RAM burn-in and remanence.** Memory that holds one value for a long time can keep a trace of it:

- *SRAM burn-in (imprinting).* A cell that stores the same bit for months ages asymmetrically (bias-temperature instability), so its
  power-up state and speed lean toward that value. A long-lived key held in on-chip SRAM can be distinguished later, and overwriting it
  with zeros does not help, because zeros keep the *same* cells in the same state.
- *DRAM remanence and cold boot.* Contents survive for seconds to minutes after power loss, longer when cooled. The shorter a secret is
  resident, the smaller the window.
- *Reused memory.* Stack and heap slots reused by later code, swap, hibernation images and core dumps copy whatever is still there.

How the feature mitigates them:

| Threat | Mitigation | Rule |
|--------|------------|------|
| secret resident for a long time (burn-in, cold-boot window) | wipe at the **last use**, not the scope end, so the secret lives as briefly as the code allows | W1 |
| a cell keeps one value (imprinting) | the target decides what the bytes become: **inverted or random data** instead of zeros, or a hardware cryptographic erase, so cells do not keep a stable value | section 6 (`wipe.call`) |
| repeated wipes to the same pattern | a two-step erase (value, then its complement) inside one `wipe.call`, or a pattern the target changes per wipe | section 6 |
| a secret copied across stack frames | the callee wipes its own locals; the caller keeps its secret in a secure variable | W8 |
| an extra layer against an aggressive compiler | `#[secure_wipe(volatile)]` | section 6 |
| a secret still in memory when a fault ends the function | `#[secure_wipe(wipe_on_panic)]` | section 11 |

Example target for a device whose SRAM imprints: write the complement, then random data from the on-chip TRNG. The design reasons are
inline comments (target files are JSON without comments; strip them in real use):

```json
{
  "wipe": {
    // Complement first (flips every cell once), then a TRNG fill (no fixed pattern). {p} is the address and {n} the byte count.
    // The routine lives in a separately compiled file, so the C compiler cannot prove the writes dead.
    "call": "board_sram_scrub({p}, {n})",
    "header": "board/scrub.h"
  }
}
```

Zeros remain the default because they need no hardware; a certified product on imprinting-prone memory should set the call above.

## 2. Concepts

| Term | Meaning |
|------|---------|
| **wipe** | destroy the bytes of an object through the target's `wipe` primitive (zeros by default, but the target decides: random bytes, a pattern, a hardware crypto engine that scrambles or key-erases), followed by a compiler barrier so the write cannot be elided |
| **secure variable** | a local (or an individually marked by-value parameter) that is wiped: marked `#[secure_wipe]` itself, of a `#[secure_wipe]` struct type, or in a `#[secure_wipe]` function |
| **trivial type** | plain data with no drop glue and nothing owned: scalars, `bool`, enums of them, arrays and `#[copy]` structs of them (the same set as KIFP180's "plain data") |
| **live range** | from a store of the variable to its **last use** before the next store (or scope exit); computed on the IR, per variable |
| **secure struct** | a struct marked `#[secure_wipe]`: it gets a compiler-generated `drop` that wipes it field by field |
| **secure container** | `secure_vector<T>`: a separate built-in type with the layout of `vector<T>` whose own operations wipe what they release (rule W12); wipe-capable by its type, no attribute needed |
| **exempt field** | a field of a secure struct marked `#[wipe_exempt]`: the generated `drop` skips it |

## 3. Surface syntax

```kif
// A struct whose every value is wiped when it dies. The compiler writes `drop` for it (so the struct may not declare one).
#[secure_wipe]
struct session_key {
    bytes: [u8; 32],              // plain data: wiped
    counter: u64,                 // plain data: wiped
    #[wipe_exempt]                // not plain data (a pointer to a buffer someone else owns): skipped, deliberately
    owner: ptr<device>,
}

// One variable. Wiped right after its last use, not at the end of the scope.
fn derive(#[tainted] secret: u64) -> u64 {
    #[secure_wipe] let mut scratch: [u8; 64] = [0; 64];      // trivial type: wiped at the end of its live range
    fill(&mut scratch, secret);
    digest(&scratch)                                          // last use of `scratch`: the wipe is emitted after this call returns
}

// A whole function: every trivial local in it (not the by-value parameters, rule W8) is a secure variable.
#[secure_wipe]
fn mix(key: [u8; 32], nonce: u64) -> u64 {
    let t = hash(&key, nonce);       // `key`, `nonce` and `t` are each wiped separately, each after its own last use
    #[wipe_exempt] let cheap = t & 1;   // opt one variable out of the function-wide rule (rationale in section 9)
    t ^ cheap
}
```

`#[secure_wipe]` is accepted on: a `let` binding (also `let mut`), a function or method, and a struct. `#[wipe_exempt]` on: a field of a
secure struct, and a `let` binding inside a `#[secure_wipe]` function. `#[wipe_exempt]` takes no arguments; `#[secure_wipe]` takes the optional `volatile` (section 6) and `wipe_on_panic` (section 11); `volatile` on a struct applies to every field that is not exempt.

## 4. Rules and rationale

### Rule W1 - Trivial variables are wiped at the end of their live range

For a secure variable of a trivial type the compiler runs a liveness analysis on the IR (a backward dataflow over the CFG, per
variable, per slot) and emits one wipe at every point where a live range ends: after the last use on each path. A variable that is
stored again starts a new live range with its own wipe; in a loop each iteration's range ends inside the iteration. Wiping at the *last
use*, not at the scope end, keeps the secret in memory for the shortest time and does not depend on how long the scope is.

*Why per live range, not per scope.* A long function would otherwise hold the key until its closing brace. And a scope-end wipe is
wrong for a value that is overwritten and reused in a loop (only the last value would be erased).

If the storage is never read at all (stored, never used), the range ends at the store: the wipe follows the store.

### Rule W2 - Each variable is wiped separately

The *source-level* unit is one wipe per variable (and per field for a struct, rule W5), each with its own reason comment (rule W7).
Two adjacent variables are never merged by kif into one range over both. Separate wipes are auditable one by one, survive a
reordering of the locals, and never touch the padding or a neighbour that is not secure.

**Coalescing is the C compiler's job.** kif emits the separate wipes and the C compiler may fuse them into one `memset` when it can
prove that is equivalent (adjacent fields, one object, no intervening reads). The same holds for a whole struct: kif never emits one
block write itself, but a compiler that sees every field wiped in a row may turn them into a single pass. This is allowed because the
observable result is identical; it is not guaranteed, and nothing relies on it. The barrier (section 6) stays after the group.

### Rule W3 - Only plain data can be wiped

`#[secure_wipe]` on a variable or parameter whose type is not trivial is `KIFW001`: `rc<T>`, `ptr<T>`, `vector<T>`, `str`, `optional`
of an owning type, a struct with a `drop` or an owning field, a reference, a function value. Overwriting such an object's bytes would
leak what it owns, double-free, or break its invariants, and the drop glue is the only code that knows how to release it. (The
function-level attribute never reports this: it marks the trivial variables and leaves the others alone, rule W8.)

`secure_vector<T>` is the one built-in non-trivial type that *is* wipeable, because its own operations do the wiping (rule W12).

A non-trivial value that holds secrets is made a **secure struct** instead (rule W4): its generated `drop` wipes the plain fields and
`wipe_exempt` marks the ones the drop glue owns.

### Rule W4 - A secure struct gets an implicit, field-by-field `drop`

A struct marked `#[secure_wipe]` receives a compiler-generated `drop`. It runs where any `drop` runs (scope exit, overwrite, not after a
move: the move-tracking of the `drops` pass applies unchanged). A struct (or class) that **already has a `drop` cannot be
`#[secure_wipe]`** (`KIFW002`), whether the `drop` is declared on it directly or in an `impl` block: two destructors, one of them
synthesized, would have an unspecified order, and the hand-written one could skip the wipe. The same holds for a struct that is
marked and later gains a `drop`. The check is on the struct itself, not on its fields: a field whose type has a `drop` is rule W6. Because the type
has drop glue, it is not `#[copy]` (the language already forbids that combination); it is moved, and a moved-from value is not wiped
again (the destination owns the bytes now; rule W9 covers the bytes the move leaves behind).

The generated `drop`, in declaration order of the fields (reverse is an equally valid choice; the order is fixed and documented):

1. for each field that is not `#[wipe_exempt]`, in order: wipe that field (rule W5);
2. then the drop glue of the exempt fields, as for any struct (a `ptr<T>` field is released by its own owner logic).

### Rule W5 - Struct wiping is field by field

The generated `drop` makes one wipe call **per field**, on `&self.field` with `sizeof(field)`. It does not wipe the struct as one
block, so it never writes into padding it does not own and never into an exempt field, and every call carries a comment naming the
field. A field that is itself a secure struct is wiped by its own generated `drop` (recursively; every leaf once). A field that is an
array of plain data is one wipe of the whole array.

A C compiler may fuse the per-field wipes of one struct into one pass over the object when it can prove the result is the same (rule W2).
kif does not do it, because kif cannot see layout or padding.

Padding bytes between fields are **not** wiped (they hold no field value). A struct that must be erased entirely should have no padding
(`#[repr(C)]` with explicit layout); `kif` reports the padding of a secure struct as an information note (`KIFW101`).

### Rule W6 - A secure struct may only contain wipeable or exempt fields

Every field of a secure struct must be a plain-data type, a secure struct, a `secure_vector<T>`, or an array of those; any other field is `KIFW003` *unless*
it is marked `#[wipe_exempt]`. Without the tag, a `ptr<T>`, an `rc<T>`, a `vector<T>` etc. would be silently skipped by the wipe and the
reader could believe the struct is fully erased; the tag makes every unwiped field a visible, reviewable decision. `#[wipe_exempt]` on a
field of a struct that is not secure, on a field that is wipeable anyway, or anywhere else is `KIFW004` (it would be dead documentation).

### Rule W7 - Every wipe is stated in the generated code

Each wipe is preceded by a comment that says what is wiped and why, like the `untaint` and barrier comments of docs/kif-taint.md:

```c
/* kif: secure wipe: scratch (u8[64], 64 bytes), end of live range, #[secure_wipe] let (t.kif:12) */
explicit_bzero(&kif_l_scratch, 64);
/* kif: secure wipe: session_key.bytes (u8[32], 32 bytes), generated drop of #[secure_wipe] struct session_key (t.kif:3) */
explicit_bzero(&k->bytes, 32);
/* kif: wipe skipped: session_key.owner (#[wipe_exempt]) */
```

A skipped exempt field gets its own comment, so a reader of the output sees exactly what was and was not erased. `kif ir --wipe-report`
(section 8) lists the same sites for a review.

### Rule W8 - A `#[secure_wipe]` function marks its trivial variables

On a function or method, every **named, trivially destructible local** becomes a secure variable. Left alone:
non-trivial locals (they have their own drop glue, rule W3), references and pointers (a reference is not the object), the return slot
(the caller owns the result), statics and `#[tls]`/`#[per_cpu]` cells (they never die), and variables marked `#[wipe_exempt]`.
Compiler temporaries (an SSA value that never lives in a named slot) are not variables; they are in registers, see section 11.

**Warning: this is the nuclear option.** Every wiped variable is forced into memory and written, with a compiler barrier after
each wipe, so the optimizer can no longer keep it in a register, fold it away or reorder around the wipe. A function-level
`#[secure_wipe]` makes the whole function slower and larger, and the effect grows with the number of locals. Use it only on small
functions that handle secrets throughout (a key schedule, a PIN check). In anything larger mark the individual variables, and put
`#[wipe_exempt]` on the loop counters and other public locals.

**By-value parameters are not marked by the function attribute.** They usually live in registers; wiping them would force a spill to
the stack just to erase it, which costs code and can itself leave the secret in memory. Mark one explicitly (`#[secure_wipe] key: [u8; 32]`)
when it is passed in memory anyway. The parameter's copy is made by the caller's call sequence: only the callee's own storage is wiped, so a
caller that passes a secret by value should keep it in a secure variable of its own. This is a documented limitation (section 11).

### Rule W9 - Wipe the storage, not the value: moves do not cancel a wipe

For a trivial variable the storage is wiped at the end of its live range even if its value was copied or moved elsewhere (a copy
is a `memcpy` that leaves the source bytes behind). The destination is a separate variable with its own rule: a copy into unmarked storage
is not wiped (section 11), and `kif` reports it as `KIFW102` where it can tell. For a secure struct the generated `drop` does not run
after a move (the destination runs it). Whether the **moved-from slot is wiped** follows the language and the memory model of the
backend, not a kif rule of its own: in kif a move leaves the bytes behind with the slot empty, so the slot is wiped once, field by field,
at the end of its live range (the one wipe without a drop). Where the target language already runs a destructor on the moved-from
object, as C++ does, the destructor-based wipe is used and no second wipe is emitted; the C++ backend decides, and a backend that
cannot prove the wipe valid for its memory model does not emit it (and reports `KIFW006`-style at target load).

### Rule W10 - Wiping must not violate the C and C++ memory model

A wipe is a *write* to an object; the generated code must make it a well-defined one:

1. **Never wipe an object whose lifetime ended, or has not started.** For a trivial type the wipe happens *inside* the live range's
   scope (before the storage is released or reused), so the object is alive. For a secure struct the generated `drop` wipes the fields
   *before* the destructor returns, while the object is still alive, and before its storage is released. A wipe after `~T()` or after
   `free` is a use-after-lifetime and is never emitted.
2. **Write through a character-type pointer or a primitive.** The target's wipe primitive (`explicit_bzero`, a `volatile unsigned char`
   loop, `memset_explicit`) accesses the *object representation* through `unsigned char`, which both languages allow for any object. The
   wipe is never an assignment of `0` of the field's own type (that would be a normal store the compiler may remove, and would not zero a
   pointer or enum with a non-zero null representation on an exotic ABI).
3. **Not on a `const` object.** Modifying an object that was *defined* `const` is undefined behaviour. The C and C++ backends therefore emit
   the storage of a secure variable without `const` (a `let` without `mut` is non-`const` storage for a secure variable) and `kif`
   never takes a wipe through a `const T*`.
4. **Not racing with a reader.** A wipe is a plain write. A variable that is shared with another thread or interrupt context (reached
   through an `atomic<T>`, a `&` held across a yield, a `#[per_cpu]` or `#[tls]` cell) is not a secure variable: `KIFW005`. Wiping a
   shared object requires the owner to be the only user, which is exactly what the borrow checker proves for a local.
5. **Not before the last use, including through a reference.** The live range of a variable includes every reference to it that is still
   live (the borrow check already tracks this, `Borrow` instructions in the IR); the wipe follows the last use of the variable or of any
   live borrow of it, so a pending read through a pointer never observes wiped bytes.
6. **Trivially copyable only for bytewise access.** `memset` on a non-trivially-copyable C++ type is undefined behaviour; that is why rule W3
   forbids non-trivial variables and rule W5 wipes a secure struct field by field (each field a trivial object) rather than as one block.
7. **Barrier after the write.** The wipe primitive for a target without a guaranteed non-elidable one is followed by a compiler barrier
   (section 6) so that the write is an observable side effect. This keeps the write in the program order the C/C++ abstract machine already
   gave it; it adds no new aliasing or ordering rule, and generates no store the abstract machine would not allow.

### Rule W12 - `secure_vector<T>`: a growable buffer that wipes what it releases

A plain `vector<T>` is unchanged: no flag, no extra work, no new check in its operations. Growable secrets use a separate type,
`secure_vector<T>`, **overlaid** on the vector: the same `{ptr, len, cap}` layout and lowering, the same operation names, but a distinct
type, so a secret cannot end up in an unwiped buffer by a silent conversion.

| Operation | `vector<T>` | `secure_vector<T>` |
|-----------|-------------|--------------------|
| `try_reserve`, `try_push`, `try_resize`, `try_insert_at` when the buffer must grow | `alloc.realloc` (the block may move, the old block is freed unwiped) | `alloc.malloc` new block, copy, **wipe the whole old block**, `alloc.free` it (never `realloc`) |
| `pop`, `clear`, `try_erase_at`, shrinking `try_resize` | length changes only | the vacated element bytes are **wiped** |
| drop (scope exit, rules of `drops`) | `alloc.free` | **wipe all `cap` elements** (not just `len`), then `alloc.free` |
| `try_clone` | copy | copy into a secure block (the clone is a `secure_vector` too) |
| reads: `v[i]`, `try_at`, `len`, `&v[a..b]`, `for x in &v` | as today | identical; a borrowed slice is plain and not wiped by the borrow |

Rules:

1. **Only trivial element types.** `secure_vector<T>` is for temporary buffers of plain data (key schedules, plaintext and scratch
   buffers), so `T` must be trivial (rule W3's set: scalars, enums, `#[copy]` structs and arrays of them). Anything else, including a
   secure struct (it has a generated `drop`), `rc<T>`, `ptr<T>` or `str`, is `KIFW007`. Elements are copied bitwise and never
   dropped, exactly as for `vector` (docs/kif-c-backends.md 3.2), which is why no element ever needs its own wipe or drop.
2. **No conversion in either direction in v1.** There is no `vector` -> `secure_vector` (the old buffer may already hold copies) and no
   `secure_vector` -> `vector` (it would drop the guarantee). The one escape is the explicit `secure_vector::unwrap_unwiped()`, which
   hands the buffer over as a `vector` and is searchable; `KIFW103` notes each use.
3. **The wipe is the target's wipe** (section 6): each wipe in the generated code is a `KIF_SECURE_WIPE` call with the usual reason
   comment (rule W7), for example `/* kif: secure wipe: v (secure_vector<u8>, capacity bytes), drop (t.kif:9) */`.
4. **The wipe size is `cap * sizeof(T)`**, read from the header at the drop point, so spare capacity that once held a longer secret is erased.
5. **It composes.** A `secure_vector<T>` field of a secure struct needs no `#[wipe_exempt]` (rule W6); the struct's generated `drop` wipes the
   buffer and then frees it. A `secure_vector` local is secure by its type, with no attribute (it is not "trivial", so rule W8's function
   marking neither adds nor removes anything).
6. **Needs the target allocator** `alloc.malloc` and `alloc.free` (docs/kif-targets.md); a target without them cannot use it
   (`KIFW008`). `alloc.realloc` is deliberately not used.
7. **Failure paths are wiped too.** If growth fails after the new block is allocated, the new block is wiped before it is freed.

*Future:* the capability is described by the ABI of the type (a buffer pointer, an element count or capacity, an element size). Making that
description an attribute for user-defined containers and for `str` is deferred; v1 has the built-in `secure_vector` only.

### Rule W11 - No pass may remove or move a wipe

The `wipe` instruction is an impure, ordered operation in the IR. A guard like the `ct::` one (docs/kif-taint.md section 7a) re-checks, after every `transform`
pass on a function that has a wipe, that every wipe is still there, once, in the same order relative to the uses of its variable
and to the other wipes (`KIFW012`, an internal error naming the pass). The folders (`textfold`, `structure`) treat the wipe statement as an impure
call: it is never folded into an expression, duplicated, reordered with another impure statement, or dropped as dead. Section 7a's
rules G1-G7 are unchanged.

## 5. Where a wipe is emitted

| Variable | Wipe happens | Done by |
|----------|--------------|---------|
| trivial local or parameter, secure | after the last use of each live range (rule W1) | the `wipes` pass inserts an IR `Wipe` instruction |
| secure struct value | where its generated `drop` runs, field by field (rule W4/W5) | the `drops` pass already schedules the drop; the synthesized `drop` body contains the wipes |
| moved-from secure struct slot | once, field by field, at the slot's `Dead` (rule W9) | the `wipes` pass |
| every exit path (`return`, `?`, `break`, `continue`) | before leaving, in reverse order of creation | same instruction placement as `Dead` |
| a path that ends in a panic or an infinite loop | **not wiped** (section 11) | not emitted |

*Live range on the IR.* Backward liveness over `Local` slots: `Load`/`Move`/`Borrow`/`Access` of the slot is a use, `Store` is a definition that
begins a new range, `Dead` ends the range. A wipe is placed after the last use on each path, and at a join point where one predecessor
has the slot live and another not, on the edge of the predecessor that leaves it. Slots whose address escapes into a live borrow are
live while any borrow is (rule W10.5).

## 6. Target configuration: the `wipe` section

The target JSON (docs/kif-targets.md) gets a `wipe` section. The whole section is optional; its absence means the default.

```json
{
  "wipe": {
    "call": "explicit_bzero({p}, {n})",
    "barrier": "__asm__ volatile (\"\" : : \"r\"({p}) : \"memory\")",
    "header": "strings.h"
  }
}
```

| Key | Meaning | Default |
|-----|---------|---------|
| `call` | C statement wiping `{n}` bytes at address `{p}` (an expression of pointer type), e.g. `explicit_bzero({p}, {n})`, `memset_explicit({p}, 0, {n})` or a kernel's own macro | `memset({p}, 0, {n})` |
| `barrier` | C statement placed right after every wipe so the compiler cannot treat the write as dead: it must be opaque and "read" the memory | the GCC/Clang form `__asm__ volatile ("" : : "r"({p}) : "memory")` |
| `barrier_after_call` | `true`: emit `barrier` after `call` even when `call` is a guaranteed non-elidable primitive; `false`: only for the default `memset` | `true` for the default, `false` when `call` is set |
| `header` | include for the primitive | `string.h` for the default |
| `macro` | name of the greppable macro every wipe is emitted through (below) | `KIF_SECURE_WIPE` |
| `volatile_call` | C statement wiping `{n}` bytes at `{p}` for `#[secure_wipe(volatile)]`, written to defeat any reasoning by the compiler | a `volatile unsigned char` loop over the bytes |

**The wipe need not write zeros.** kif only requires that the primitive destroys the old contents of the `{n}` bytes and that the compiler cannot
elide it. A target may call a hardware crypto engine or a TRNG fill, write a pattern, or do a cryptographic erase; kif never assumes the
bytes read back as zero afterwards, never reads them, and never relies on a wiped object being a valid value (it is dead). `wipe.call`
and `wipe.volatile_call` are the only places that decide what the bytes become; the default is zeros only because that needs no hardware.

Placeholders are validated at target load like `taint.*` (`{p}` and `{n}` only). A kernel target uses its own primitive and its own
header; the FreeBSD kernel target, for example, sets `call` to `explicit_bzero({p}, {n})` (explicit_bzero(9), `<sys/systm.h>`) and needs
no `barrier`, because the kernel's `explicit_bzero` is not elided. A user-space target with only `memset` keeps the default, with the
barrier.

**No runtime library.** kif ships nothing that is linked in. The generated C file defines one macro from this section, at the top, and
every wipe site calls it, so `grep KIF_SECURE_WIPE out.c` lists every wipe:

```c
/* kif: secure wipe primitive, from target "freebsd-kernel" (wipe.call). */
#define KIF_SECURE_WIPE(p, n) explicit_bzero((p), (n))
...
/* kif: secure wipe: scratch (u8[64]), end of live range, #[secure_wipe] let (t.kif:12) */
KIF_SECURE_WIPE(&kif_l_scratch, 64);
```

The macro body is `call` (plus `barrier` when it applies); nothing else is generated, so the primitive is the target's own.

**`volatile` is an explicit opt-in**, as an extra layer: `#[secure_wipe(volatile)]` on a variable, function or struct makes the wipe
write through `wipe.volatile_call` (default: a `volatile unsigned char` loop) in addition to the normal call, for code whose
certification wants a write the compiler cannot reason about at all. It is off by default (it pessimizes the code, section 13).

**Optimization level.** kif's role ends at code generation. It does not pick or check the C compiler's flags; the caller builds the
output at any level. The default barrier is what keeps the wipe at every level, and `--wipe-report` plus the object harness (stage 8)
let a certification review confirm it for the chosen flags.

*Sane default.* `memset(p, 0, n)` followed by `__asm__ volatile ("" : : "r"(p) : "memory")`. The empty asm takes the address as an input
and clobbers memory, so the compiler must assume the overwritten bytes can be observed and cannot drop the `memset`. This is the portable
fallback for GCC and Clang when `explicit_bzero`/`memset_explicit` is not available.

A target whose compiler has neither a primitive nor an asm barrier must set `call` to a primitive that cannot be elided (a volatile
loop); A target with `wipe` unset still gets the default, which needs only
GCC-style asm; a target whose compiler lacks even that must set `wipe.call` explicitly, or the compiler refuses code that needs a wipe
(`KIFW006`), as it does for a missing `taint.barrier`.

## 7. IR representation

| Item | Representation |
|------|----------------|
| secure variable | `Local.wipe: bool` (set by the builder for a marked `let`, a secure-struct type, or a `#[secure_wipe]` function) |
| the wipe | a new instruction `Wipe(local, path, ty, volatile, why)` (an impure, ordered operation like `Barrier`): `path` is empty for a whole trivial variable and `.field` for a field of a secure struct; `ty` is the wiped object's type (the code generator emits `sizeof` of it, so the IR has no target sizes); `volatile` selects `wipe.volatile_call`; `why` is the audit text for the comment |
| generated drop | the synthesized `drop` is built as IR like a user `drop`, containing `Wipe` instructions per field |
| IR text and hash | `Wipe` is printed (`wipe %scratch  // end of live range`) and **is part of the IR hash**: a function that gained or lost a wipe has a different meaning (docs/kif-module-contracts.md section 13 lists it with the taint attributes) |

The IR carries only the *decision* (a `Wipe` at a point); the primitive and barrier text come from the target at emission. That
keeps the IR hash target-independent and the wipe policy in one place.

## 8. Tools, contracts and attestation

- **`kif ir FILE --wipe-report`**: one page for a review, like `--taint-report`: every secure struct (fields wiped, fields exempt), every
  secure variable and its wipe sites, every variable of a secure struct type (with its moved-from wipes), the functions marked `#[secure_wipe]`, and the exempt variables and fields.
- **`kif wipe-check FILE [--target T] [--cc gcc] [--flags=-O2] [--evidence REGEX] [--keep DIR]`**: the object check (stage 8). kif does
  not choose your compiler flags, so whether a wipe survives optimization is a fact about one build; this makes it visible. It lowers
  the file, compiles the generated C to assembly with *your* compiler and flags, and for every function with wipe sites written in its
  body requires at least as much evidence of wiping in that function's assembly as there are sites. Exit status 1 when a function has
  fewer, or the C does not compile.

  ```console
  $ kif wipe-check demo.kif --flags=-O3                  # does every wipe survive -O3?
  $ kif wipe-check demo.kif --flags="-O2 -flto" --cc clang
  $ kif wipe-check demo.kif --target freebsd-kernel --evidence 'call\s+explicit_bzero'   # your own definition of "one wipe"
  ```

  *Evidence* (default): an inline-asm barrier (the check stamps a comment into each empty `asm volatile ("")` barrier so the compiler
  prints it) or a call to `memset`/`bzero`/`explicit_bzero`-like symbols, whichever is more frequent; with `--evidence`, the number of
  assembly lines matching your regular expression. *Rationale*: a barrier or a call to a non-elidable primitive is exactly what keeps
  the store from being a dead store, and both are visible in the assembly whatever the instruction set. *Limits*: only wipes written
  directly in a function body count (those in generated struct drops and `secure_vector` helpers depend on inlining); a function the
  compiler inlined away has no symbol and is reported `-` (its wipes are in its callers); `#[secure_wipe(volatile)]` sites are not
  counted (volatile stores are guaranteed by the C standard); a primitive the compiler expands into plain stores (small `memset`, a
  builtin) shows no call, so for such a target give `--evidence`. *Opt out*: do not run it; nothing else depends on it.
- **Module contracts** (docs/kif-module-contracts.md): the per-function record of a `.kifm` gains `secure_wipe` (the function wipes its
  trivial locals) so an importer can read the vendor's claim, and the secure-struct list names the types whose values are wiped. The
  checks are AST-level and recorded like `kept_checks`.
- **Attestation**: the `.kifa` per-function IR hash covers the wipes, so a pipeline that dropped one is a hash mismatch (`kif verify`).
- **Editor**: hover on a secure variable (done) says "secure wipe: wiped after its last use (line N)"; a `tainted`-style semantic token modifier
  `secure` marks secure variables; a quick fix offers `#[wipe_exempt]` for `KIFW003`.

## 9. Diagnostics

| Code | Severity | Meaning | Mitigation |
|------|----------|---------|------------|
| `KIFW001` | error | `#[secure_wipe]` on a variable or parameter of a non-trivial type (rule W3) | use plain data; make a secure struct and exempt the owned fields |
| `KIFW002` | error | a `#[secure_wipe]` struct already has a `drop` (declared directly or in an `impl`; rule W4) | remove `#[secure_wipe]` and wipe by hand, or move the `drop` work into a wrapper type that owns a secure struct |
| `KIFW003` | error | a field of a secure struct is not wipeable and not `#[wipe_exempt]` (rule W6) | tag it `#[wipe_exempt]` (a conscious decision) or change its type |
| `KIFW004` | error | a wipe attribute where it has no effect: `#[wipe_exempt]` on a field of a struct that is not secure, on a field that is wipeable and gains nothing, on a parameter, on a variable outside a `#[secure_wipe]` function, or on an item; `#[secure_wipe]` on a field or on an item that is not a `let`, `fn` or `struct` (rules W6, W8) | remove it |
| `KIFW005` | error | a secure variable is shared: atomic, `#[tls]`, `#[per_cpu]`, or a static (rule W10.4) | keep the secret in a local owned by one thread of control |
| `KIFW006` | error | the target has no way to wipe: `wipe.call` is unset and the compiler has no GCC-style asm barrier | set `wipe.call` in the target |
| `KIFW007` | error | `secure_vector<T>` with a non-trivial element type, including a secure struct (rule W12.1) | use trivial elements; hold owning or secure values in a secure struct that has the vector as a field |
| `KIFW008` | error | `secure_vector` used on a target without `alloc.malloc`/`alloc.free` (rule W12.6) | add them to the target |
| `KIFW012` | error | *internal*: a pass removed, duplicated or moved a wipe (rule W11) | a compiler bug; the message names the pass |
| `KIFW101` | note | a secure struct has padding, which is not wiped (rule W5) | give the struct an explicit layout without padding |
| `KIFW103` | note | `secure_vector::unwrap_unwiped()` hands a secret buffer to an unwiped `vector` (rule W12.2) | keep the data in the `secure_vector` |
| `KIFW102` | note | a secure value was copied into a variable that is not secure (rule W9) | mark the destination `#[secure_wipe]` or avoid the copy |

**No opt-out of the guard** (like rule 10 of docs/kif-taint.md): the way to keep a variable unwiped is `#[wipe_exempt]` on it (visible and
searchable), never a switch that turns wiping off.

## 10. Interactions

| Feature | Interaction |
|---------|-------------|
| `drops` / `Dead.drop` | a secure struct's generated `drop` is scheduled by the `drops` pass like a user `drop`; a trivial variable has no drop, its `Wipe` comes from the `wipes` pass |
| borrow check | a live borrow of a secure variable extends its live range (rule W10.5); a reference parameter to a secure value is not itself wiped |
| `#[tainted]` / `ct::` | independent but meant to be used together: a tainted `#[secure_wipe]` buffer holds the secret that `untaint!` code inspects; the wipe adds no branch. The `wipe` statement is not in a `ct::` region, so the ct guard ignores it |
| `elide` / `prove` | never read wipes and never remove them; the guard (rule W11) checks |
| constant-time | wiping is data-independent: `n` is the static type size, no branch depends on the secret |
| `unsafe`, `extern "C"` | a secure variable passed by pointer to C code stays the caller's; the wipe still runs after the call returns |
| C backend | emits the target's `wipe.call` + `barrier` with the comments of rule W7 |
| C++ backend | **not part of this stage.** The rules of W10 apply to it unchanged: it will use reloco's own wipe helper (the project's STL-avoidance rule), and a non-trivial C++ type is never wiped as one block |

### 10a. The C++ backend: `reloco::secure_vector<T>`

The C++ backend (not started; this is the contract it will rely on) does not synthesize growable buffers. Like every container it
uses reloco's (docs/kif-targets.md: C++ targets get allocation from reloco). So the C++ meaning of `secure_vector<T>` is a reloco type, and
**reloco needs a new `reloco::secure_vector<T>`** next to `reloco::vector<T>` (`../reloco/include/reloco/vector.hpp`). reloco has no such type
today; it has `masked_byte_region::wipe()` and the volatile-wiping string buffer of docs/reference.md, which are the nearest precedents.

What the reloco type must provide, mirroring rule W12 so both backends behave alike:

| Requirement | Reason |
|-------------|--------|
| `static_assert(std::is_trivially_copyable_v<T> && std::is_trivially_destructible_v<T>)`; trivial `T` only | W12.1, and it keeps the byte-wise wipe valid in the C++ memory model (rule W10.6) |
| growth by allocate-new, copy, wipe old, free (never the allocator's `realloc`/in-place extend) | the old block must not be released unwiped |
| `pop_back`, `clear`, `erase`, shrinking `resize` wipe the vacated bytes | W12 table |
| the destructor wipes `capacity() * sizeof(T)` bytes, then deallocates | a destructor is the C++ drop; spare capacity is erased too |
| the wipe goes through reloco's one wipe primitive, which is the target's `wipe` hook (a porting-layer entry, like the mutex and sink templates under `detail/porting/`), with `memset` + compiler barrier as the default | one place decides what the bytes become (section 6); no runtime library in the generated code |
| the same checked / `try_` / `unsafe_` tiers and rvalue-access blocking as `reloco::vector`; accessors that alias the buffer are `&` / `const &` qualified or `RELOCO_BLOCK_RVALUE_ACCESS`; lifetime annotations (`RELOCO_LIFETIMEBOUND`, `RELOCO_POINTER`/`RELOCO_OWNER`) as for `vector` | reloco's container contract and lifetime-safety guides |
| move construction and assignment take the buffer without copying and leave the source empty; move-assignment wipes the old target buffer first | the old contents are not leaked by an assignment |
| no implicit conversion to or from `reloco::vector`; an explicit, greppable release of the buffer | W12.2 |

A moved-from `reloco::secure_vector` owns nothing, so the C++ destructor has nothing to wipe: this is the case rule W9 leaves to the
backend. kif emits no extra wipe for it.

A secure struct (rule W4) lowers to a C++ class whose destructor wipes each plain field and lets the `secure_vector` member's own
destructor run (member destructors run after the body, so the struct's field wipes happen first, then the buffer). Work to do in reloco is
listed under stage 3a in section 12; it follows reloco's `docs/extending.md` and container-safety guides, and is a separate change in
that repository.

## 11. What this does not protect

Stated plainly, because the feature is only useful when people know its limit:

- **Registers and spill slots.** The C compiler may copy a secret into a register or spill it to a stack slot kif never sees. The wipe
  erases the variable's own storage; it cannot erase those. Mitigate with the compiler's own options (`-fzero-call-used-regs`, stack
  clearing) and with keeping secrets in few, short-lived variables.
- **Copies into unmarked storage.** `KIFW102` catches the cases kif can see; a copy through `unsafe`, `extern "C"` or a `memcpy` helper is
  invisible.
- **A crash, panic, `abort` or never-returning loop before the wipe runs.** No wipe is emitted on a path that does not return normally.
  Opt in per use with `#[secure_wipe(wipe_on_panic)]`: the caller decides, and the
  wipe is then also emitted on the unwinding or panic exit (an extra cost on cold paths). Without it, a panic path is not wiped.
- **By-value parameters** under the function-level attribute (rule W8) and **struct padding** (rule W5): not wiped in v1; padding is
  only noted (`KIFW101`); a strict mode is not planned for v1.
- **A secret kept in an exempt field or in memory someone else owns** (`ptr<T>` target): the wipe never follows a pointer.
- **Padding** (rule W5) and **the bytes of a value that was moved** into storage kif does not manage.
- **Hardware** (caches, store buffers, DRAM).
- **Timing.** A wipe is a fixed-size write; it does not make surrounding code constant time (that is docs/kif-taint.md).

## 12. Stages

1. **Attributes and checks.** Parse `#[secure_wipe]` on `let`, `fn` and `struct`, `#[wipe_exempt]` on fields and `let`; `KIFW001`-`KIFW005`; trivial-type classification shared with KIFP180. **Done** (`kiflang/wipe.py`, `tests/test_wipe.py`): `#[secure_wipe] let` parses (destructuring lets are rejected); `KIFW005` is reported for statics (kif has no `atomic` type yet, the name check is ready for it); the classification is its own (`wipe.plain`), because KIFP180 asks a different question (all-zero validity).
2. **Liveness and the `Wipe` instruction.** The IR instruction, the `wipes` pass (live ranges per slot, rule W1/W2/W8), `Local.wipe`, IR text, IR hash and serialization. **Done** (`kiflang/wipes.py`: backward liveness; wipe after the last use, after a dead store, at entry for an unused parameter, on branch edges (split when needed); a slot whose address is kept is wiped at its `Dead`; a store does not end a range, so a loop variable is wiped once after the loop; an array load counts as a reference because C passes arrays by address).  The moved-from slot (rule W9) is stage 3. 
3. **Secure structs.** The synthesized `drop` (rule W4/W5), wiring into `drops`, the moved-from slot rule. **Done**, including the moved-from slot (W9): the `wipes` pass puts one `wipe %x  // moved-from slot` before the `Dead` of a secure-struct local that was moved from and has no drop there (`Local.secure_struct`); C calls the generated `kif_wipe_<S>(&x)`, which wipes each non-exempt field and releases nothing (a path that still holds the value runs `S_drop` instead). Also: a secure struct counts as having drop glue (`ir.drop_types`, `Lowerer.find_dropable`), so `drops` and the C emitter call `S_drop` at scope exit; `S_drop_fields` wipes each non-exempt field in declaration order (a nested secure struct by its own drop), then releases the exempt owned fields. Known limitation: a struct built in a temporary and copied into its variable leaves an unwiped temporary copy in C (see section 11).
3a. **`secure_vector<T>`.** (C backend; the reloco C++ type is a separate task in `../reloco`, section 10a.) The type, the overlay on the vector lowering, the wiping operations and drop, `KIFW007`/`KIFW008`/`KIFW103` (rule W12); tests that every growth, shrink and drop emits its wipe. **C backend done**: `secure_vector<T>` is declared in `prelude/reloco.kif` (same operations as `vector<T>` plus `unwrap_unwiped`); C lowers it to `struct kif_svec_<tag>` with its own helpers (`try_reserve` mallocs, copies, wipes the old block's whole capacity and frees it, never `realloc`; `pop`, `erase_at`, a shrinking `resize` and `clear` wipe the vacated elements; `drop` wipes `cap` elements, then frees). `KIFW007` (non-plain element, checked on every written type), `KIFW008` (target without `alloc.malloc`/`alloc.free` or a working wipe) and `KIFW103` (a note for `unwrap_unwiped`) are reported. A `secure_vector` field of a secure struct is released by its own drop (the struct's generated drop does not clear the header first, which would lose the pointer); in a moved-from slot the header is wiped. Test: `tests/c/securevec.*` replaces the allocator to check every freed block is zero. Not done: struct literals that initialise a vector field (`vector::try_create` there is not typed in C, for `vector` as well).
4. **Target section.** `wipe` in `target._SPEC` (placeholders validated), the default, `docs/kif-targets.md` rows, `KIFW006`, the FreeBSD kernel target sets `explicit_bzero`. **Done** (`Target.wipe()` returns a `WipePlan`; `KIFW006` is its `error`, reported by stage 5 when a wipe is emitted; Linux kernel sets `memzero_explicit`). Stage 4 was taken before stage 2 because the later stages read the plan.
5. **C emission.** The call, the barrier and the comments of rule W7; the textual folder keeps it (rule W11). **Partly done**: `ir_c.wipe` emits `/* kif: secure wipe: ... */` and `KIF_SECURE_WIPE(&x, sizeof(x))`, the header defines the macro from `Target.wipe()` and includes its header, `KIFW006` is raised for a target that cannot wipe and for an array parameter. `#[secure_wipe(volatile)]` (on a let, parameter, fn or struct) sets `Local.wipe_volatile`, so the `Wipe` is `volatile` and C calls `KIF_SECURE_WIPE_VOLATILE`, defined from the target's `volatile_call`; any other argument is `KIFW004`. **Done.**
6. **Guard.** Rule W11 (`KIFW012`). **Done** (`kiflang/wipeguard.py`): the pass manager snapshots the wipes before every `transform` pass and verifies after it that each is still present once, in its block, on the same side of every instruction that touches its slot (a removed neighbour is not a move); and `ir_c` checks that the printed C has as many `KIF_SECURE_WIPE` calls as the IR has `Wipe`s, so the text folder cannot drop or duplicate one. A function without wipes costs nothing.
7. **Tools.** `--wipe-report`, contract and attestation fields, hover, semantic token, quick fix, notes `KIFW101`/`KIFW102`. **Partly done**: `kif ir FILE --wipe-report` (`wipes.report`), `KIFW101` (padding of a struct whose field sizes kif knows; natural 64-bit layout; generics, enums and pointers are not sized) and `KIFW102` (a direct copy of a secure variable into an unmarked named variable; reported by the `wipes` pass, so by `kif ir --passes default` and `kif lint`-style IR checks). The attestation hash already covers wipes. The `.kifm` security section records `secure_wipe` per function and `secure_wipe`/`wipe_exempt` per type (checked, KIF014 on mismatch), plus the informational `wipes` count. Editor: hover on a `#[secure_wipe]` fn or struct, the `secure` semantic token modifier on its name, and a `KIFW003` quick fix that inserts `#[wipe_exempt]`. `--wipe-report` also lists struct-typed locals. Hover on a secure local, parameter or struct-typed variable names why it is wiped and the lines of its wipe sites (`wipes.describe_local`, built from the IR of the enclosing function, so it is as current as the buffer). **Done.**
8. **Object harness.** **Done** (`kiflang/wipecheck.py`, `kif wipe-check`, `tests/test_wipecheck.py`; section 8): compiles the generated C to assembly with the caller's flags and checks each function keeps as many wipes as it has sites. A bare `memset` target fails it at `-O2` (the call is removed as a dead store), the default target passes at `-O0`/`-O2`/`-O3`. Not done: inspecting a linked object or LTO output, which needs symbol-level knowledge of the final binary.

## 13. Alternatives considered

- **Wipe at scope end** (like a destructor). Simpler, but holds a secret for the whole scope and misses a variable reused in a loop. Kept
  for secure *structs* (their lifetime is the object's), not for trivial variables.
- **One `memset` over the whole struct.** One call, but writes into padding and exempt fields, and is undefined behaviour on a C++ type that is
  not trivially copyable. Field-by-field is explicit and well defined (rules W5, W10.6).
- **`volatile` everywhere.** Pessimizes every use of the variable; a wipe needs only the final write to survive. Offered per use as `#[secure_wipe(volatile)]`.
- **Trust the C compiler's `-O0`/hardening flags.** Not auditable and not portable; the point is that the *source* says what is wiped.
- **A wipe flag on `vector<T>`.** Changes a type everyone uses, adds a check to its hot operations and lets a secret reach an unwiped vector by a conversion; a separate overlaid type keeps `vector` as it was.
- **A runtime library.** Does not cover stack variables or by-value parameters, the commonest leak, and adds a link dependency; the target's macro and primitive are used instead.
