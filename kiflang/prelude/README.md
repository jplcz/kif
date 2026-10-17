# kiflang/prelude

The `.kif` files the compiler ships. They contain **declarations only** (`extern "C++" type`, `extern "C++" impl`,
`extern "C" fn`): the implementation is the C++ library each one describes. This directory is also the default
[sysroot](../../docs/kif-proc-language.md) (section 15), so `import "microfmt.kif";` finds the file here unless
`--sysroot` points elsewhere.

| File | Module | Visible | Describes |
| --- | --- | --- | --- |
| `reloco.kif` | none (global) | everywhere, with no `import`, `use` or prefix | [reloco](https://github.com/jplcz/reloco): `result`, `optional`, `error`, `ptr`/`arc`/`rc`/`weak`, `str`/`str_view`/`cstr`, `vector` and the maps, sets and queues, `duration`/`instant`, fixed-width numbers, `wrapping`/`saturating`/`checked_value`, `call_location_ref`, `slot<T>` |
| `microfmt.kif` | `microfmt` | after `import "microfmt.kif";` (then `use microfmt::sink;` or `microfmt::sink`) | [microfmt](https://github.com/jplcz/microfmt): `sink`, `memory_buffer`, `c_string_span_sink`, `ring_buffer_sink`, `log::logger` and `log::level` |

## What the attributes mean

The attributes on prelude declarations are read by the checker and by lowering (docs section 4.6, 4.10, 4.13, 4.17):

```kif
// C++ spelling and header the lowered code uses; without `cxx_name` an item of a namespaced module is `<namespace>::<name>`.
#[cxx_name("reloco::unique_ptr"), header("reloco/unique_ptr.hpp"), send(T), sync(T)]
// `lang(x)` names a language item the checker knows how to treat (result, optional, str, deref, index, slot, ...).
// `copy`/`send`/`sync` say what the type may do; a type with none of them is move-only and single-threaded.
// `zeroable` means an all-zero byte pattern is a valid value, so the type may be a `#[tls]`/`#[per_cpu]` cell.
extern "C++" type ptr<T>;
```

Methods also carry fact attributes (`when`, `len_of`, `unchecked_form`, `preserves`; docs section 4.19a) that tell the optimiser
when a checked call may become its `#[unsafe]` sibling. They are hints only; keep them in step with the C++ behaviour.

- `#[unsafe]` marks an operation that needs an `unsafe` block; the `try_` / plain / `unsafe_` tiers follow reloco's container
  contract (checked panics, `result<..>`, unchecked).
- `#[allocates]` marks a function that can allocate; it is what rejects allocation in per-CPU initialisers (KIFP186).
- `#[might_sleep]` marks something that may block (forbidden in atomic context, KIFP110).

## Editing rules

- Declare only what the C++ library really offers, and take the member names and failure modes from its headers; a name that
  differs from the C++ one needs `#[cxx_name]`.
- Mark a type `#[zeroable]` only after checking that its all-zero bytes are valid in the C++ class.
- Both files must keep checking clean: `python3 -m kiflang check kiflang/prelude/reloco.kif` (`tests/test_proc_symbols.py` asserts the same).
- Do not add a new global name casually: everything in `reloco.kif` is visible to every file and can be shadowed but not
  hidden.
