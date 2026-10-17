# kobj object lifecycle (design, C backends)

Status: **design for review**. Nothing here is implemented except what section 8 lists. The goal is the smallest model that
lets a kif struct be a kobj class on FreeBSD-like kernels without kif owning a lifecycle that belongs to a framework.

## 1. Terms

| Term | Meaning |
|------|---------|
| interface | a `#[kobj]` interface: the methods and their descriptors (`<iface>_if.h/.c`, already generated) |
| class | the method table plus object size for a struct that has `impl <iface> for S` (`S_class`) |
| object | memory of at least `sizeof(struct S)` bound to a class |
| handle | what callers pass to `IFACE_METHOD(handle, ..)`: the interface's `self(..)` type (`device_t`, `kobj_t`, ...) |
| object accessor | the C expression that turns a handle into the `struct S *` a method receives |

## 2. Decision: kif owns the class, never the lifecycle

Frameworks differ in who allocates, binds and frees an object:

| Framework | Allocation | Bind (`ops` set) | Free | Handle | Object accessor |
|-----------|-----------|------------------|------|--------|-----------------|
| newbus `device_t` | the bus (`device_set_driver`, softc sized from the class) | the bus | the bus | `device_t` | `device_get_softc(dev)`: the softc is only a *cookie* the device model stores; it holds no kobj header and no ops |
| plain kobj, heap | caller: `kobj_create(&S_class, mtype, flags)` | `kobj_create` | `kobj_delete(obj, mtype)` | `kobj_t` | cast: the object *is* the handle |
| plain kobj, embedded or static | caller (a field, a global) | `kobj_init(obj, &S_class)` or `kobj_init_static` | `kobj_uninit(obj)` | `kobj_t` | cast |
| other kernels' frameworks | their own | their own | their own | their own | their own |

So the generator emits **only** what is true in every row: `S_class` (methods + `sizeof(struct S)`) and the thunks that call
the kif methods through the accessor. It does not call `kobj_create`, `kobj_init`, `device_set_driver` or any delete function.
The caller (C code, kif code, or the framework) does, exactly as it does for a hand-written class today.

## 3. The two things a struct needs besides the class

1. **Initialising the kif fields.** A framework hands out zero-filled memory (newbus softc) or raw memory (`kobj_create`
   with `M_ZERO` or not). The kif shell (`S_init`, field defaults) is not run by anybody. Rule: the framework's *attach-like*
   step runs `S_init(sc)` (or a kif `try_construct_at`) and the *detach-like* step runs the drop below.
2. **Dropping owned fields.** A struct with `str`, `vector`, `ptr` or a user `drop` is `dropable` (C backend 3.2). The object is
   not a kif local, so the compiler's automatic drop does not exist for it. Rule: the framework's detach-like step must
   call `S_drop(sc)` once. This is the one place the generated code exposes a drop to non-kif callers; kif code itself still
   never writes it (a kif caller that owns the object, section 5, gets it inserted).

Both are plain generated functions (`S_init`, `S_drop`, section 3.2 of `kif-c-backends.md`); the lifecycle document only fixes
*who calls them when*:

| Framework step | Calls |
|----------------|-------|
| newbus `device_attach` (or `probe` for a probe-time softc) | `S_init(sc)` first, then the kif attach work |
| newbus `device_detach` | the kif detach work, then `S_drop(sc)` last (also on a failed attach after `S_init`) |
| `kobj_create` caller | `S_init(obj)` after it, `S_drop(obj)` before `kobj_delete` |
| `kobj_init` caller | `S_init(obj)` after it, `S_drop(obj)` before `kobj_uninit` |

## 4. Managed lifecycle (newbus `device_t`)

A newbus softc is not a kobj: it carries no header, and the device model (not the softc) holds the class. The softc is only a
cookie that `device_get_softc` returns. So the lifecycle can be **managed by the generated class** instead of left to the
caller. For a `device_t` handle (a target `kobj.lifecycle` entry, section 6) the generator wraps the two lifecycle methods:

| Generated wrapper | Does |
|-------------------|------|
| `S_device_attach(dev)` | `sc = device_get_softc(dev)`, then `S_init(sc)`, then dispatches to the kif `attach` method; on failure runs `S_drop(sc)` and returns the error |
| `S_device_detach(dev)` | dispatches to the kif `detach` method; if it succeeds, runs `S_drop(sc)` last; on failure leaves the object alive |

The class table then points `DEVICE_ATTACH`/`DEVICE_DETACH` at these wrappers, so a pure kif driver needs no lifecycle code at
all. The kif `attach`/`detach` bodies see an initialised object and never write `init` or `drop`.

**C code that derives from a kif softc** (a C driver whose softc embeds the kif one) does not get the wrappers, because it has
its own attach/detach. It must base-construct the kif part through exposed callbacks, the same way a C++ derived constructor
calls its base:

```c
struct my_softc {
	struct uart_softc base;		/* the kif part: must be the first member */
	int hw_irq;			/* the C part */
};

static int
my_attach(device_t dev)
{
	struct my_softc *sc = device_get_softc(dev);
	int error;

	error = uart_softc_init(&sc->base);	/* exposed callback: construct the kif base */
	if (error != 0)
		return (error);
	error = uart_softc_attach(&sc->base, dev);	/* the kif attach body, if wanted */
	if (error != 0) {
		uart_softc_drop(&sc->base);	/* undo the base construction */
		return (error);
	}
	return (0);
}
```

The exposed callbacks are exactly `S_init`, the kif methods (already external in `<stem>_proc.c`) and `S_drop`; the wrappers
above are just those calls in the order the C driver would write. Rules for the derived C driver: base first member; init the
base before any kif method; drop it once, after the last kif method, on every exit path.

## 5. kif code that owns the object

Not needed for v1. When it is, it is an ordinary kif struct value (stack, field or `ptr<S>`): its kif lifecycle (4.7 and
`kif-lifetime-and-construction.md`) applies, and the *binding* is a separate step with an explicit, visible pair
(`kobj_init`/`kobj_uninit` or `kobj_create`/`kobj_delete`) written by the caller. Open: whether a library type
`bound<S>` should pair them by scope so nobody can forget the uninit (memory-safety rule: no manual drops).

## 6. Where the object accessor comes from

Not hard-coded to `device_t`. Resolution order, first match wins (a target may also give `kobj.lifecycle`, a list of handle types for which section 4's wrappers are generated; `freebsd-kernel`: `["device_t"]`):

1. `#[object("expr")]` on the `impl` or interface, `expr` using `{handle}` (e.g. `#[object("my_get_state({handle})")]`);
2. the target's `kobj.handles` map from handle type to expression (`freebsd-kernel`: `"device_t": "device_get_softc({handle})"`);
3. a cast, which requires the struct to start with the kobj header (`kobj_ops_t ops` first field).

The target map keeps the knowledge of "newbus uses a separate softc" in one place, and a kernel with another framework adds
a row instead of a language feature.

## 7. The kobj header field (case 3 only)

A struct bound by `kobj_create`/`kobj_init` must start with `KOBJ_FIELDS`. The kif struct must not include the kobj header,
so it declares the field with an extern type from the interface file's header: `struct S { kobj: kobj_hdr, .. }` where
`kobj_hdr` is `extern "C" type` declared in the `_if.kif` (or the `.m` import) that is allowed to name the kobj header. The kobj emitter
(only for a struct whose handle is not mapped) requires the first field to be an `extern "C"` type. A dedicated attribute is not needed.

## 8. Implementation status and open points

Done: interface output (`kifc --freebsd`: `<iface>_if.h/.c`) and the class output `<stem>_kobj.h/.c`: `kobj.handles`,
`kobj.lifecycle`, `#[object("expr")]` (a string, `{handle}` is the handle) and static methods, tested in
`tests/test_lower_kobj.py`. A static method's kif function takes **all** the interface's parameters, the class first (the
checker requires the full signature). The first-field rule (7) is enforced: an unmapped handle needs a first field of an `extern "C"` type (KIFC001 otherwise; the emitter cannot check that it really is the kobj header). Not written: the user-guide section in `kif-c-backends.md`.

Decisions (from review):

1. `S_init`/`S_drop` keep their names; no lifecycle-neutral pair.
2. Instantiation is **C callers only** for now. kif code cannot create, bind or own a kobj; `bound<S>` (section 5) waits
   until a kif caller appears. kif code may still *call* methods through a handle it is given.
3. kif **can implement static (class-level) methods**, section 9.

## 9. Static methods

A `static` interface method (e.g. `DEVICE_IDENTIFY(driver_t *driver, device_t parent)`, `DEVICE_PROBE` is not static) has no
object: its first argument is the class (`kobj_class_t`/`driver_t *`), and the dispatcher looks it up in the class, not in an
object's `ops`. Rules for a kif implementer:

- The kif method is declared without `self` and with the interface's full parameter list, the class argument first
  (`fn identify(driver: c_ptr<driver_t>, parent: device_t)`).
- The thunk `S_<method>_thunk(cls, args..)` calls the kif function directly; no object accessor and no `S_init`/`S_drop`,
  because no object exists. The method table entry is the same `KOBJMETHOD(IFACE_method, thunk)`.
- A static method may not touch `self` state by construction (it has none); it can use only its arguments and globals.

