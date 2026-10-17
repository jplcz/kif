# Object lifetime and construction in kif

This guide collects, in one place, how a kif value lives and dies and the idiomatic ways to create one. The normative rules
are in `kif-proc-language.md` (4.2 ownership, 4.7 constructors and `drop`, 4.11 heap types, 4.12 cloning); how the C backend
realises them is in `kif-c-backends.md`. Every example is commented with what it is, why it is needed and what the
arguments mean.

## 1. The lifetime model in five rules

1. **Every value has exactly one owner.** A local, a field, a `ptr<T>`, a `vector<T>` element or a function result owns it.
2. **Structs are move-only.** Assigning, passing or returning by value *moves*; the source is dead (use is KIFP023).
   `#[copy]` plain-data structs, scalars and enums are copied instead.
3. **Borrowing is `&T` / `&mut T`.** A borrow never owns, never outlives its referent, and carries a named lifetime when it is
   stored or returned (`&'a T`, 4.2).
4. **The compiler runs `drop`.** A value that still owns something at the end of its scope is destroyed there, unless it was
   moved. Users never call `drop` (calling it is rejected); in C the compiler inserts the calls and drop flags.
5. **Nothing is built implicitly and nothing throws.** Construction that can fail returns `result<T>`; there are no partial
   objects.

## 2. When a value is destroyed

```kif
fn demo(alloc: allocator_ref) -> result<()> {
    let a = ring::try_allocate(alloc, 64)?;    // `a` owns a ring: destroyed at the closing brace of `demo`
    let b = ring::try_allocate(alloc, 32)?;    // `b` is declared later, so it is destroyed BEFORE `a` (reverse order)
    consume(a);                                // `a` is moved into `consume`; `demo` no longer drops it, `consume` does
    if cond() { return ok(()); }               // early exit: `b` is dropped here too (the compiler adds the cleanup)
    let c = ring::try_allocate(alloc, 8)?;     // if this fails, `?` returns the error and drops `b` on the way out
    ok(())                                     // `c` then `b` are dropped
}
```

- **Order.** Locals are destroyed in reverse declaration order. For one struct, the user's `drop` body runs first, then the
  owned fields in reverse declaration order (the C++ rule; C and C++ agree).
- **Moves.** A moved value is not dropped by its old owner. Where a move happens on only some paths the C backend keeps a
  *drop flag*; C++ uses the moved-from state (4.7). Either way a value is dropped exactly once.
- **Temporaries.** A temporary that owns something (`make()?.len()`) is dropped at the end of its statement.
- **Early exits.** `return`, `?`, `break` and `continue` drop everything declared inside the scopes they leave.
- **Assignment.** `x = new_value;` drops the old value of `x` first (when it owns something), then stores the new one.
- **Fields.** Dropping a struct drops its fields: `vector<T>` frees its elements, `ptr<T>` its pointee, `optional<T>` its
  payload when it holds one, `arc<T>` decrements its count.
- **Panics.** A panic (`KIF_PANIC`/`RELOCO_TRAP`) is fatal; no drops run. Kernel code treats it as the end of the system.

### Writing `drop`

```kif
impl ring {
    // Runs when a ring dies. Why: it releases the raw resource that kif's owning types do not manage for you.
    // `&mut self`: the object is still alive but is being torn down. It must tolerate the default state, because a
    // moved-from (C++) or shell-constructed value is dropped too. Fields that are owning types (`log` above) are
    // destroyed AFTER this body, so do not free them here.
    fn drop(&mut self) {
        if self.buf != null { self.alloc.free_bytes(self.buf); }
    }
}
```

Prefer owning types (`vector<u8>`, `ptr<T>`) over raw `c_ptr` fields: then no `drop` is needed at all.

## 3. Idiomatic construction

Pick the first form that fits.

### 3.1 Plain data: a struct literal

```kif
struct point { x: i32, y: i32 }

let p = point { x: 1, y: 2 };       // all fields named; a field with a default may be left out
let q = point { y: 5, ..p };         // functional update: other fields come from `p` (copied if #[copy], else moved)
```

Why: no invariant to protect, no allocation, cannot fail. A struct with `private` fields, or one that needs `drop`, can only
be built by its own `impl` (`Self { .. }` inside it).

### 3.2 A fallible factory: `try_create` / `try_allocate`

```kif
// `try_create` uses the default allocator; `try_allocate` takes one. Both return `result<Self>`: the caller must handle
// failure, and `?` forwards it. Use these when construction allocates or validates.
let r  = ring::try_create(64)?;                       // arg: capacity in bytes
let r2 = ring::try_allocate(my_allocator, 64)?;       // arg 1: where memory comes from; arg 2: capacity
```

Why: it is reloco's protocol (`has_try_create_v` / `has_try_allocate_v`), so the type composes with `ptr<T>`, `vector<T>` and
other containers. Inside the factory, build the value with `ok(Self { .. })` after every fallible step has succeeded; a
failed step returns early and the already-built locals drop, so there is no half-made object.

### 3.3 In place: `try_construct`

```kif
impl ring {
    // The shell (every field at its default) already exists; this fills it. Why: the object is already at its final
    // address (a heap block, a container slot), so it is never built elsewhere and moved.
    fn try_construct(&mut self, cap: usize) -> result<()> { self.buf = self.alloc.try_allocate_bytes(cap)?; self.cap = cap; ok(()) }
}
```

This is the **only** form for `#[heap]` structs (4.11) and for types that register their own address. It is what
`ptr<T>::try_allocate(alloc, args..)` runs on the new storage.

### 3.4 On the heap: `ptr<T>`, `arc<T>`, `rc<T>`

```kif
// `ptr<T>`: sole owner on the heap. The trailing arguments are forwarded to T's own construction protocol (#[forward_args]).
let big: ptr<ring> = ptr::try_allocate(alloc, 64)?;   // arg 1: allocator; arg 2..: what ring::try_construct takes
let s: arc<ring>   = arc::try_create(ring::try_create(8)?)?;   // shared, atomic count: many owners, freed with the last
let w: weak<ring>  = s.downgrade();                    // non-owning observer; `w.lock()` yields optional<arc<ring>>
let t: arc<ring>   = s.share();                        // another owner: only a count increment, cannot fail
```

Why: large objects stay off kernel stacks, ownership can be handed around as one pointer, and a `#[heap]` type has no other
way to exist. Use `ptr` by default; `arc`/`rc` only when ownership really is shared (`rc` when the sharers are single-threaded).

### 3.5 An independent copy: `try_clone`

```kif
let copy = original.try_clone()?;        // deep, may allocate, so fallible; `original` stays valid
let copy2 = original.try_clone_in(alloc)?;   // same with an explicit allocator
```

Moves never clone and nothing clones implicitly (4.12). `#[derive(clone)]` generates both for a struct whose fields are all
cloneable.

### 3.6 Optional and absent values

```kif
let none_yet: optional<u32> = none();    // no value; the type comes from the annotation or the expected type
let some_n = some(5_u32);                // holds a value (an owning payload is dropped with the optional)
```

Prefer `optional<T>` over a sentinel such as `null` or `-1`.

### 3.7 Defaults for shells and moved-from values

A field default (`cap: usize = 0`) describes the shell that `try_construct` fills and the state a moved-from C++ value is left
in. Give every scalar, pointer and enum field of a struct with `drop` or `try_construct` a default.

### 3.8 Choosing

| Need | Use |
| --- | --- |
| plain data, cannot fail | struct literal (3.1) |
| allocates or validates | `try_create` / `try_allocate` (3.2) |
| object must not move; large; self-referential | `#[heap]` + `try_construct` + `ptr<T>` (3.3, 3.4) |
| shared ownership | `arc<T>` / `rc<T>` (3.4) |
| duplicate | `try_clone` (3.5) |
| value may be absent | `optional<T>` (3.6) |

## 4. Idioms to avoid

- **Raw `c_ptr` fields that own memory.** Use `ptr<T>` / `vector<T>`; if you must, write `drop`.
- **Manual `drop` calls, manual frees of owned values.** The compiler does it, and doing it twice is a memory-safety bug.
- **Constructing in two steps by hand** (`let mut r = ring{..}; r.init()`): the object exists half-made. Use a factory.
- **Returning a reference to a local.** It does not outlive the function; return the value (a move) instead.
- **Cloning to avoid a move.** Borrow with `&`/`&mut`, or move and reuse the result.

## 5. Where each guarantee is enforced

| Guarantee | Checker | C backend | C++ backend |
| --- | --- | --- | --- |
| use after move | KIFP023 | static | static, plus runtime panic on a moved-from `optional`/`ptr`/`vector` |
| drop exactly once | | compiler-inserted calls and drop flags | destructors |
| drop order | | locals reversed; `drop` body, then fields reversed | the same (C++ rules) |
| no partial objects | factory result types (4.7) | `int` result plus out-parameter | `reloco::result` |
| lifetimes of borrows | KIFP160-162 | dropped (plain pointers) | `RELOCO_LIFETIMEBOUND` |
