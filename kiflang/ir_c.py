# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""C code generation from the IR (docs/kif-ir-codegen.md): which functions the IR can already describe completely, and the emitter.

The IR was built for analysis, so it leaves parts of the language `Opaque`. A code generator needs all of a function, so the first
step towards one is knowing what is missing. `blockers(fn)` names every reason the IR of `fn` is not enough to emit it; a function
with none is ready for the emitter, and the histogram over a module says which node to add next."""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from . import ir
from . import irtypes
from . import structure
from . import textfold


def blockers(fn: ir.Function) -> List[str]:
    """The reasons `fn` cannot be emitted from its IR alone, without repeats and in first-seen order (empty: it can).

    - `type:T`: a slot or value of a type that is not a scalar (`T` as written), so its C layout is unknown to the IR;
    - `opaque:what`: a value the IR does not model (`opaque:struct`, `opaque:macro:println`, `opaque:bind`, ...);
    - `call:<value>`: a call through a function value; `access:kind`: an access whose base is not a scalar sequence."""
    out: Dict[str, None] = {}
    for loc in fn.locals:
        if not loc.type.is_scalar:
            out[f"type:{loc.type}"] = None
    if not fn.ret_type.is_scalar and fn.ret is not None:
        out[f"type:{fn.ret_type}"] = None
    for _, i in fn.instrs():
        if isinstance(i, ir.Opaque):
            out[f"opaque:{i.what}"] = None
        elif isinstance(i, ir.Call) and i.callee == "<value>":
            out["call:<value>"] = None
        elif isinstance(i, ir.Access) and i.kind != "index":
            out[f"access:{i.kind}"] = None
        elif isinstance(i, ir.Clobber) and i.local is None:
            out["clobber:memory"] = None
    return list(out)


def coverage(m: ir.Module) -> Tuple[List[str], List[Tuple[str, List[str]]], Counter]:
    """`(ready, blocked, histogram)` for a module: the functions the IR describes completely, the others with their blockers, and how many
    functions each blocker holds back (`m.skipped` functions count as blocked by `skipped`)."""
    ready: List[str] = []
    blocked: List[Tuple[str, List[str]]] = []
    hist: Counter = Counter()
    for f in m.functions:
        b = blockers(f)
        if not b:
            ready.append(f.name)
            continue
        blocked.append((f.name, b))
        hist.update(b)
    for name, why in m.skipped:
        blocked.append((name, [f"skipped:{why}"]))
        hist[f"skipped:{why}"] += 1
    return ready, blocked, hist


def report(m: ir.Module) -> str:
    ready, blocked, hist = coverage(m)
    total = len(ready) + len(blocked)
    lines = [f"{len(ready)} of {total} functions can be emitted from the IR alone"]
    if ready:
        lines.append("ready: " + ", ".join(ready))
    for name, why in blocked:
        lines.append(f"blocked {name}: {', '.join(why)}")
    if hist:
        lines.append("what holds the most back:")
        lines += [f"  {n:4}  {why}" for why, n in hist.most_common()]
    return "\n".join(lines) + "\n"


def emitter_report(mod, stem: str = "t") -> str:
    """What the emitter really does with a checked module: how many functions it writes and why it refuses the others
    (`kif ir FILE --coverage`)."""
    from . import lower_c, target

    t = target.load("c")
    lw = lower_c.Lowerer(mod, t, stem, None, "")
    lw.run()
    names = [f.name for f in ir.build_module(mod.tree).functions]
    bailed = [n for n in names if n in lw.ir_bails]
    lines = [f"{len(names) - len(bailed)} of {len(names)} functions are emitted from the IR"]
    for n in bailed:
        lines.append(f"refused {n}: {lw.ir_bails[n]}")
    hist = Counter(lw.ir_bails[n] for n in bailed)
    if hist:
        lines.append("what holds the most back:")
        lines += [f"  {c:4}  {why}" for why, c in hist.most_common()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the emitter


_INT_NAMES = frozenset({"i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32", "u64", "usize"})


class _Bail(Exception):
    """The function has something the emitter does not lower; `fn_body` reports the reason as a build error."""


_CMP = {"==", "!=", "<", "<=", ">", ">="}


class _Emit:
    """Emits the C body of one IR function, instruction by instruction.

    It writes down exactly what the IR holds and decides nothing: every `Check` that is in the IR becomes a test and a trap, every
    `Call` goes where it says. Leaving a check out is the job of an IR pass, which removes the `Check` (docs/kif-ir-passes.md). The
    names, types and helpers come from the `Lowerer` `lw`, so every function shares the module's prologue."""

    def __init__(self, lw, fn: ir.Function) -> None:
        self.lw, self.fn = lw, fn
        self.names: Dict[ir.Local, str] = {}
        self.out: List[str] = []
        self.conds: Dict[ir.Block, str] = {}
        self.strs: Dict[int, str] = {}  # temps holding a string literal: only a `panic!` message uses one so far
        self.errs: Dict[int, str] = {}  # temps holding a reloco `error::<name>`: the C errno value it stands for
        self.rinc: Dict[int, str] = {}  # `lo..=hi` iterators: the C type of the bounds
        self.comment_seen: Set[int] = lw.doc_lines(fn.node)  # source lines whose comment was written (the function's own doc to begin with): a comment is written once
        self.last_line = 0  # the line of the latest instruction that has a source position
        self.ctor: Optional[ir.Local] = None
        self.allocs: Set[int] = set()  # temps holding `default_allocator()`: never materialised
        self.alias: Dict[int, str] = {}  # array temps that only name a local array (a C array cannot be copied by `=`)
        self.arr_ty: Dict[int, irtypes.Type] = {}  # an array literal's type, taken from the local it is stored into
        self.flags: Dict[ir.Local, str] = {}  # owning locals the `drops` pass could not settle: local -> its `kif_live_` flag
        self.out_param = False  # a `result<T>` function with a value: the last C parameter is `kif_out`

    # -- types and operands
    def ctype(self, t: irtypes.Type, param: bool = False) -> str:
        from .types import Ty

        if t.is_scalar and (t.name in self.lw_bits() or t.name == "bool"):
            return self.lw.scalar(t.name)
        if t.kind == irtypes.UNIT:
            return "void"
        if t.name == "error" and not t.args:
            return "int"
        if self.iface(t) is not None:
            return f"struct {self.iface(t)}_ref"
        if self.struct_of(t) is not None:
            return f"struct {self.sname(t)}"
        h = self.heap_of(t)
        if h is not None:
            if self.owning_ptr(t):
                if not self.lw.t.get("alloc.malloc") or not self.lw.t.get("alloc.free"):
                    raise _Bail("no allocator")
                self.lw.heap.add(h)
            return ("const " if t.name == "c_cptr" else "") + f"struct {h} *"
        if t.kind == irtypes.REF and self.heap_of(t.args[0]) is not None:
            return ("" if t.mut else "const ") + self.ctype(t.args[0]) + " *"
        if self.is_enum(t):
            return f"enum {t.name}"
        if t.kind == irtypes.NAMED and not t.args and t.name in self.lw.externs:
            return t.name  # an `extern type` of the target's headers: opaque to kif
        if t.kind == irtypes.NAMED and t.name in ("c_ptr", "c_cptr") and len(t.args) == 1:
            return ("const " if t.name == "c_cptr" else "") + self.ctype(t.args[0]) + " *"
        if self.res_elem(t) is not None:
            return "int"  # the status; the value, if there is one, lives in a second variable `<name>_v`
        o = self.opt_elem(t)
        if o is not None:
            from .types import Ty

            self.lw.opt_tag(Ty(o))
            return f"struct kif_opt_{o}"
        sk = self.str_kind(t)
        if sk is not None:
            self.lw.uses_view = True
            self.lw.uses_str = self.lw.uses_str or sk == "str"
            return "struct kif_str" if sk == "str" else "struct kif_strview"
        if t.kind == irtypes.REF and (self.str_kind(t.args[0]) == "str" or self.opt_elem(t.args[0]) is not None):
            return ("" if t.mut else "const ") + self.ctype(t.args[0]) + " *"
        v = self.vec_elem(t)
        if v is not None:
            return "struct kif_vec_" + self.lw.vec_tag(Ty(v))
        if t.kind == irtypes.REF and self.vec_elem(t.args[0]) is not None:
            return ("" if t.mut else "const ") + self.ctype(t.args[0]) + " *"
        elem = self.span_elem(t)
        if elem is not None:
            scalar = elem in self.lw_bits() or elem == "bool"
            self.lw.uses_view = self.lw.uses_view or elem in ("str", "str_view")
            self.lw.uses_str = self.lw.uses_str or elem == "str"
            self.lw.spans.setdefault(elem, (self.lw.scalar(elem) if scalar else "struct kif_str" if elem == "str" else "struct kif_strview" if elem == "str_view" else f"struct {elem}" if elem in self.lw.structs else f"enum {elem}", elem in self.lw.structs))
            return f"struct kif_{'' if t.mut else 'c'}span_{elem}"
        if t.kind == irtypes.REF and self.arr_elem(t.args[0]) is not None:
            return ("" if t.mut else "const ") + self.elem_c(self.arr_elem(t.args[0])) + " *"  # a pointer to the first element
        if t.kind == irtypes.REF and (self.struct_of(t.args[0]) is not None or self.is_enum(t.args[0])
                                      or (t.args[0].is_scalar and (t.args[0].name in self.lw_bits() or t.args[0].name == "bool"))):
            return ("" if t.mut else "const ") + self.ctype(t.args[0]) + " *"
        if param and t.kind == irtypes.ARRAY and (self.arr_elem(t) or (t.args[0].is_scalar and t.args[0].name in self.lw_bits())):
            return self.elem_c(self.arr_elem(t) or t.args[0].name) + " *"
        if t.kind == irtypes.ARRAY and t.args and self.owns_value(t.args[0]):
            raise _Bail(f"an array of owning elements ({t}) is not lowered to C: nothing would drop its elements")
        raise _Bail(f"type {t}")

    def iface(self, t: irtypes.Type) -> Optional[str]:
        """The name of the `#[ops]` interface a `&I` / `&mut I` handle (`struct I_ref`) refers to, else None."""
        if t.kind == irtypes.REF and t.args[0].kind in (irtypes.NAMED, irtypes.DYN) and not t.args[0].args \
                and (t.args[0].name in self.lw.ifaces or t.args[0].name in self.lw.foreign_ifaces):
            return t.args[0].name
        return None

    def as_iface(self, v: ir.Value, to: irtypes.Type, text: str) -> str:
        """`text` (the C value of `v`) as a handle of the interface `to` refers to, when `v` is a reference to a struct that implements it."""
        n = self.iface(to)
        if n is None or not isinstance(v, ir.Temp) or self.iface(v.type) is not None:
            return text
        if v.type.kind != irtypes.REF or self.struct_of(v.type.args[0]) is None:
            raise _Bail("interface argument")
        return f"{self.sname(v.type.args[0])}_as_{n}({text})"

    def sname(self, t: irtypes.Type) -> str:
        """The name of a named type, with `Self` resolved to the type of the `impl` being lowered."""
        return self.lw.cur_owner if t.name == "Self" and self.lw.cur_owner else t.name

    def struct_of(self, t: irtypes.Type):
        """The declaration of the struct `t` names when it is one the emitter can write: not generic, every field a scalar."""
        d = self.lw.structs.get(self.sname(t)) if t.kind == irtypes.NAMED and not t.args else None
        if d is None or d.generics or not all(self.fieldable(self.field_ir_of(f)) for f in d.fields):
            return None
        return d

    def heap_of(self, t: irtypes.Type) -> Optional[str]:
        """The struct a `ptr<S>` (owning), `c_ptr<S>` or `c_cptr<S>` points to, else None. Only a struct with an `impl` block can be on the heap."""
        if t.kind == irtypes.NAMED and t.name in ("ptr", "c_ptr", "c_cptr") and len(t.args) == 1 and t.args[0].kind == irtypes.NAMED \
                and not t.args[0].args:
            n = self.sname(t.args[0])
            if n in self.lw.structs and (t.name != "ptr" or n in self.lw.impl_structs):
                return n
        return None

    def owning_ptr(self, t: irtypes.Type) -> bool:
        return t.name == "ptr" and self.heap_of(t) is not None

    def nn(self, v: str, site, t: irtypes.Type) -> str:
        """`v`, a `ptr<S>` value, checked for null: dereferencing a null `ptr` panics."""
        from .lower_c import HELPER_TEXT, c_string

        self.lw.need("KIF_PANIC", "")
        self.lw.need("KIF_NN", HELPER_TEXT["KIF_NN"])
        return f"((struct {self.heap_of(t)} *)KIF_NN({v}, {c_string(self.lw.file)}, {self.lw.lineno(site) or self.last_line}))"

    def pointee(self, t: irtypes.Type) -> irtypes.Type:
        """The struct type a pointer or reference `t` points to (`t` itself for a value)."""
        if t.kind == irtypes.REF:
            t = t.args[0]
        h = self.heap_of(t)
        return irtypes.parse_type(h) if h is not None else t

    def field_ir_of(self, f) -> irtypes.Type:
        return irtypes.parse_type(ir.type_text(f.ty))

    def fieldable(self, t: irtypes.Type) -> bool:
        """Whether a field of type `t` has a C form the emitter writes: a scalar, an enum, an array of scalars, an optional of one, or
        another such struct."""
        return (t.is_scalar and (t.name in self.lw_bits() or t.name == "bool")) or self.is_enum(t) or self.arr_elem(t) is not None \
            or self.opt_elem(t) is not None or self.vec_elem(t) is not None or self.str_kind(t) is not None or self.struct_of(t) is not None \
            or self.heap_of(t) is not None

    def arr_elem(self, t: irtypes.Type) -> Optional[str]:
        """The scalar element name of an array `[T; N]` with a known length (a C array `T[N]`), else None."""
        if t.kind == irtypes.ARRAY and t.length is not None:
            e = t.args[0]
            if e.is_scalar and (e.name in self.lw_bits() or e.name == "bool"):
                return e.name
            if (self.struct_of(e) is not None and self.sname(e) not in self.lw.dropable) or self.is_enum(e):
                return self.sname(e)
        return None

    def elem_c(self, e: str) -> str:
        """The C type of an element named by `arr_elem` / `span_elem`: a scalar, a struct or an enum."""
        return self.lw.scalar(e) if e in self.lw_bits() or e == "bool" else f"struct {e}" if e in self.lw.structs else f"enum {e}"

    def declare(self, t: irtypes.Type, name: str, ctype: str) -> str:
        e = self.arr_elem(t)
        if e is not None:
            return f"\t{self.elem_c(e)} {name}[{t.length}] = {{0}};"
        return f"\t{ctype} {name} = {self.zero(t)};"

    def res_elem(self, t: irtypes.Type) -> Optional[str]:
        """For a `result<T>` the C type of `T` (empty for `result<()>`), None for any other type or a `T` the emitter cannot write."""
        if t.kind == irtypes.NAMED and t.name == "result" and len(t.args) <= 1:
            if t.args and (t.args[0].kind == irtypes.UNKNOWN or t.args[0].name == "{integer}") and self.fn.ret_type.name == "result" and self.fn.ret_type.args:
                t = self.fn.ret_type  # `ok(x)` is typed `result<?>` until it meets its context: here, the function's own result
            if not t.args or t.args[0].kind == irtypes.UNIT:
                return ""
            e = t.args[0]
            if (self.fieldable(e) and not self.arr_elem(e)) or self.vec_elem(e) or self.str_kind(e):
                return self.ctype(e)
            if e.kind == irtypes.REF and not e.mut and (self.struct_of(e.args[0]) is not None or self.is_enum(e.args[0])
                                                        or (e.args[0].is_scalar and e.args[0].name in self.lw_bits())):
                return self.ctype(e)
        return None

    def res_inner(self, t: irtypes.Type) -> irtypes.Type:
        if t.args and (t.args[0].kind == irtypes.UNKNOWN or t.args[0].name == "{integer}"):
            t = self.fn.ret_type
        return t.args[0]

    def res_scalar(self, t: irtypes.Type) -> Optional[str]:
        """The scalar name of the `T` of a `result<T>`, when it is a scalar (the hint a literal takes), else None."""
        if t.kind == irtypes.NAMED and t.name == "result" and t.args and t.args[0].is_scalar:
            return t.args[0].name
        if t.args and (t.args[0].kind == irtypes.UNKNOWN or t.args[0].name == "{integer}") and self.fn.ret_type.args and self.fn.ret_type.args[0].is_scalar:
            return self.fn.ret_type.args[0].name
        return None

    def rv(self, name: str) -> str:
        """The variable that holds the value of the result whose status variable is `name`."""
        return name + "_v"

    def opt_elem(self, t: irtypes.Type) -> Optional[str]:
        """The scalar element name of an `optional<T>` (the C struct `kif_opt_T` with `has` and `v`), else None."""
        if t.kind == irtypes.NAMED and t.name == "optional" and len(t.args) == 1:
            e = t.args[0]
            if e.is_scalar and e.name in self.lw_bits():
                return e.name
            if self.struct_of(e) is not None or self.is_enum(e):
                return self.sname(e)  # a struct that owns something is dropped with the optional (`kif_opt_S_drop`)
            if self.str_kind(e) == "str":
                return "str"  # an owning element: the optional is dropped with `kif_opt_str_drop`
        return None

    def opt_owns(self, t: irtypes.Type) -> bool:
        """An `optional<T>` whose element owns something (a `str` or a struct with owned fields): it needs `kif_opt_T_drop`."""
        o = self.opt_elem(t)
        return o is not None and (o == "str" or o in self.lw.dropable)

    def str_kind(self, t: irtypes.Type) -> Optional[str]:
        """`str` (the owned string, `struct kif_str`) or `str_view` (`struct kif_strview`), else None."""
        if t.kind == irtypes.NAMED and not t.args and t.name in ("str", "str_view"):
            return t.name
        return None

    def vec_elem(self, t: irtypes.Type) -> Optional[str]:
        """The scalar element name of a `vector<T>` (the C struct `kif_vec_T`: `ptr`, `len`, `cap`), else None."""
        if t.kind == irtypes.NAMED and t.name == "vector" and len(t.args) == 1:
            e = t.args[0]
            if e.is_scalar and (e.name in self.lw_bits() or e.name == "bool"):
                return e.name
            if (self.struct_of(e) is not None and self.sname(e) not in self.lw.dropable) or self.is_enum(e):
                return self.sname(e)  # a struct that owns nothing, or an enum: stored by value
        return None

    def span_elem(self, t: irtypes.Type) -> Optional[str]:
        """The scalar element name of a `&[T]` / `&mut [T]` (passed as a `kif_span_T` pointer and length), else None."""
        if t.kind == irtypes.REF and t.args[0].kind == irtypes.SLICE:
            e = t.args[0].args[0]
            if e.is_scalar and e.name in self.lw_bits():
                return e.name
            if (self.struct_of(e) is not None and self.sname(e) not in self.lw.dropable) or self.is_enum(e):
                return self.sname(e)
            if self.str_kind(e) == "str":
                return "str"
            if self.str_kind(e) == "str_view":
                return "str_view"
        return None

    def elem_of(self, t: irtypes.Type) -> str:
        """The scalar element name of a slice reference or an array; `_Bail` otherwise."""
        e = self.span_elem(t) or (t.args[0].name if t.kind == irtypes.ARRAY and t.args[0].is_scalar and t.args[0].name in self.lw_bits() else None)
        if e is None:
            raise _Bail("element")
        return e

    def is_enum(self, t: irtypes.Type) -> bool:
        return t.kind == irtypes.NAMED and not t.args and t.name in self.lw.enums

    def enumerator(self, text: str) -> str:
        """The C name of the member an `E::m` path names; anything else is refused."""
        e, _, m = text.strip().partition("::")
        d = self.lw.enums.get(e)
        if d is None or m not in {x.name.text for x in d.members}:
            raise _Bail("enumerator")
        return self.lw.enumerator(d, m)

    def field_type(self, t: irtypes.Type, name: str) -> str:
        """The scalar type name of field `name` of the struct (or reference to a struct) `t`; empty when it is not a scalar."""
        ft = self.field_ir(t, name)
        return ft.name if ft.is_scalar else ""

    def field_ir(self, t: irtypes.Type, name: str) -> irtypes.Type:
        """The type of field `name` of the struct (or reference to a struct) `t`."""
        d = self.struct_of(self.pointee(t))
        for f in d.fields if d is not None else ():
            if f.name.text == name:
                return self.field_ir_of(f)
        raise _Bail("field")

    def vtype(self, v) -> irtypes.Type:
        """The type a local or temp is declared with: an untyped integer is an `i32`."""
        return irtypes.parse_type("i32") if v.ty == "{integer}" else v.type

    def zero(self, t: irtypes.Type) -> str:
        if self.heap_of(t) is not None:
            return "NULL"
        return "{0}" if self.struct_of(t) is not None or self.span_elem(t) or self.opt_elem(t) or self.vec_elem(t) or self.str_kind(t) or self.iface(t) else "0"

    def lw_bits(self):
        from .lower_c import BITS

        return BITS

    def tname(self, v: ir.Value, hint: Optional[str] = None) -> str:
        """The scalar type name of an operand; an untyped literal takes `hint`."""
        t = v.ty if isinstance(v, (ir.Temp, ir.Const)) else None
        if t in self.lw_bits() or t == "bool":
            return t
        if hint is not None and (hint in self.lw_bits() or hint == "bool"):
            return hint
        if t == "{integer}" or hint == "{integer}":
            return "i32"  # an integer the checker left untyped is an i32, as in the language spec
        raise _Bail("untyped operand")

    def val(self, v: ir.Value, hint: Optional[str] = None) -> str:
        if isinstance(v, ir.Temp):
            if v.id in self.strs:
                from .lower_c import c_string

                self.lw.uses_view = True
                return f"(struct kif_strview){{ {c_string(self.strs[v.id])}, {len(self.strs[v.id].encode())} }}"
            if v.id in self.errs:
                return self.errs[v.id]
            if v.ty == "{integer}" and hint in self.lw_bits():
                return f"(({self.lw.scalar(hint)})kif_t{v.id})"  # an untyped integer (an `int32_t`) takes the type its use needs
            return self.alias.get(v.id) or f"kif_t{v.id}"
        if isinstance(v.value, bool):
            return "true" if v.value else "false"
        if isinstance(v.value, int):
            return self.lw.literal(v.value, self.tname(v, hint))
        raise _Bail("constant")

    def line(self, i: ir.Instr) -> int:
        return self.lw.lineno(i.site)

    def put(self, text: str) -> None:
        self.out.append("\t" + text)

    def trap(self, i: ir.Instr, msg: str) -> str:
        if isinstance(i, ir.Check) and (i.tag or "").startswith("ensures:"):
            msg = f"result of {self.fn.name}() violates #[ensures({i.tag[8:]})]"
        return self.lw.panic_call(msg, i.site)

    NOTE_NAMES = {"index": "bounds", "range": "range", "div": "division", "shift": "shift", "some": "has-value", "non_null": "non-null"}

    def elided_notes(self, before) -> None:
        """A comment where the `elide` pass removed a proved check, so the reader of the C sees which checks the compiler dropped and why."""
        for at, c in self.fn.elided_at:
            if at is before and c.proof and not c.call:
                self.put(self.lw.elided_note(self.NOTE_NAMES.get(c.kind, c.kind), c.proof))

    # -- instructions
    def instr(self, i: ir.Instr) -> None:
        lw = self.lw
        line = lw.lineno(i.site)
        if line and line != self.last_line and lw.t.flag("line_directives") and getattr(i.site, "range", None) is not None:
            self.out.append(f'#line {line} "{lw.file}"')
        if line and line != self.last_line and getattr(i.site, "range", None) is not None:
            self.out += [f"\t{c}" for c in lw.comments_above(line, self.comment_seen)]
        self.last_line = line or self.last_line
        if isinstance(i, ir.Cell):
            d = lw.cells.get(i.name)
            if d is None:
                raise _Bail("cell")
            self.ctype(i.dst.type)
            slot = f"kif_{lw.cell_kind(d)}_{lw.stem}_block()->{i.name}"
            self.put(f"kif_t{i.dst.id} = {slot if self.arr_elem(i.dst.type.args[0]) else '&' + slot};")  # an array cell is the pointer to its first element
            return
        if isinstance(i, ir.Release):
            if i.drop != "drop":
                raise _Bail("a temporary held on some paths only")
            self.put(self.drop_call(self.vtype(i.value), self.val(i.value)))
            return
        if isinstance(i, (ir.Live, ir.Dead)):
            if isinstance(i, ir.Dead) and i.drop:
                self.drop(i.local, i.drop)
            return
        if isinstance(i, ir.Load) and self.arr_elem(i.local.type):
            self.alias[i.dst.id] = self.names[i.local]
        elif isinstance(i, ir.Store) and self.arr_elem(i.local.type):
            if i.local in self.fn.params:
                raise _Bail("array parameter store")
            src = self.val(i.src)
            if src != self.names[i.local]:
                self.put(f"for (size_t kif_k = 0; kif_k < {i.local.type.length}; kif_k++)")
                self.put(f"\t{self.names[i.local]}[kif_k] = {src}[kif_k];")
        elif isinstance(i, ir.Make) and i.kind in ("array", "repeat") and self.arr_elem(self.arr_ty.get(i.dst.id, i.dst.type)):
            at = self.arr_ty.get(i.dst.id, i.dst.type)
            e, n = self.arr_elem(at), at.length
            d = f"kif_t{i.dst.id}"
            if i.kind == "array":
                if len(i.args) != n:
                    raise _Bail("array literal")
                for k, a in enumerate(i.args):
                    self.put(f"{d}[{k}] = {self.val(a, e)};")
            else:
                self.put(f"for (size_t kif_k = 0; kif_k < {n}; kif_k++)")
                self.put(f"\t{d}[kif_k] = {self.val(i.args[0], e)};")
        elif isinstance(i, ir.Load):  # also Move
            self.put(f"kif_t{i.dst.id} = {self.names[i.local]};")
            if isinstance(i, ir.Move) and i.local in self.flags:
                self.put(f"{self.flags[i.local]} = false;")
            if self.res_elem(i.local.type):
                self.put(f"{self.rv(f'kif_t{i.dst.id}')} = {self.rv(self.names[i.local])};")
        elif isinstance(i, ir.Store):
            if i.drop_old:
                self.drop(i.local, i.drop_old)
            self.put(f"{self.names[i.local]} = {self.as_iface(i.src, i.local.type, self.val(i.src, i.local.ty))};")
            if i.local in self.flags:
                self.put(f"{self.flags[i.local]} = true;")
            if self.res_elem(i.local.type):
                self.put(f"{self.rv(self.names[i.local])} = {self.rv(self.val(i.src))};")
        elif isinstance(i, ir.Test) and i.kind == "failed" and self.res_elem(i.base.type) is not None:
            self.put(f"kif_t{i.dst.id} = ({self.val(i.base)} != 0);")
        elif isinstance(i, ir.Access) and i.kind == "error" and self.res_elem(i.base.type) is not None:
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)};")
        elif isinstance(i, ir.Access) and i.kind == "ok" and self.res_elem(i.base.type):
            self.put(f"kif_t{i.dst.id} = {self.rv(self.val(i.base))};")
        elif isinstance(i, ir.Access) and i.kind == "ok" and self.res_elem(i.base.type) is not None:
            pass
        elif isinstance(i, ir.Global) and i.name.startswith("error::"):
            from .lower_c import ERROR_ERRNO, LowerError

            if i.name[7:] not in ERROR_ERRNO:
                raise _Bail("error name")
            try:
                self.errs[i.dst.id] = lw.errno_value(i.name[7:])
            except LowerError as e:
                raise _Bail("errno") from e
        elif isinstance(i, ir.Make) and i.kind == "range_inclusive":
            lo, hi = i.args
            if not (isinstance(lo.type, irtypes.Type) and lo.type.is_scalar and lo.type.name in _INT_NAMES):
                raise _Bail("range type")
            n = i.dst.id
            self.rinc[n] = self.lw.scalar(lo.type.name)
            self.put(f"kif_rlo{n} = {self.val(lo)};")
            self.put(f"kif_rhi{n} = {self.val(hi)};")
        elif isinstance(i, ir.Test) and i.kind == "has_next" and i.base.id in self.rinc:
            n = i.base.id
            self.put(f"kif_t{i.dst.id} = (kif_rlo{n} <= kif_rhi{n} && (uint64_t)kif_it{n} <= (uint64_t)kif_rhi{n} - (uint64_t)kif_rlo{n});")
        elif isinstance(i, ir.Access) and i.kind == "item" and i.base.id in self.rinc:
            n = i.base.id
            self.put(f"kif_t{i.dst.id} = ({self.rinc[n]})(kif_rlo{n} + kif_it{n}++);")
        elif isinstance(i, ir.Test) and i.kind == "has_next":
            n, _ = self.iter_parts(i.base)
            self.put(f"kif_t{i.dst.id} = (kif_it{i.base.id} < {n});")
        elif isinstance(i, ir.Access) and i.kind == "item":
            _, elem = self.iter_parts(i.base)
            amp = "&" if i.dst.type.kind == irtypes.REF else ""
            self.put(f"kif_t{i.dst.id} = {amp}{elem(f'kif_it{i.base.id}++')};")
        elif isinstance(i, ir.Unary):
            n = self.tname(i.dst)
            a = self.val(i.a, n)
            if i.op == "-":
                r = f"{lw.neg_helper(n)}({a})"
            elif i.op == "!" and n == "bool":
                r = f"(!{a})"
            elif i.op in ("!", "~") and n != "bool":
                r = f"(({lw.scalar(n)})~{a})"
            else:
                raise _Bail(f"unary {i.op}")
            self.put(f"kif_t{i.dst.id} = {r};")
        elif isinstance(i, ir.Binary):
            self.put(f"kif_t{i.dst.id} = {self.binary(i)};")
        elif isinstance(i, ir.Cast):
            n = i.ty
            if n in lw.enums and self.tname(i.a, n) in self.lw_bits():
                from .lower_c import c_string
                k = i.a.value if isinstance(i.a, ir.Const) and isinstance(i.a.value, int) and not isinstance(i.a.value, bool) else None
                names = [m for m, v in lw.enum_rows(lw.enums[n]) if v == k]
                if names and lw.t.get("access.range", "prove") != "checked":
                    self.put(f"kif_t{i.dst.id} = {names[0]} {lw.elided_note('enum cast', f'{k} is the enumerator {names[0]}')};")
                    return
                lw.cast_enums.add(n)  # the helper panics for a value that is not a declared enumerator
                self.put(f"kif_t{i.dst.id} = KIF_TOENUM_{n}((int64_t){self.val(i.a)}, {c_string(lw.file)}, {self.line(i)});")
                return
            if n not in self.lw_bits() or self.tname(i.a, n) not in self.lw_bits():
                raise _Bail(f"cast to {n}")
            self.put(f"kif_t{i.dst.id} = (({lw.scalar(n)}){self.val(i.a)});")
        elif isinstance(i, ir.Str):
            if "{" in i.text:
                raise _Bail("format string")
            self.strs[i.dst.id] = i.text
        elif isinstance(i, ir.Call):
            self.call(i)
        elif isinstance(i, ir.Check):
            self.check(i)
        elif isinstance(i, ir.Access) and i.kind == "index" and (i.base.type.kind == irtypes.ARRAY or (
                i.base.type.kind == irtypes.REF and self.arr_elem(i.base.type.args[0]))):
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)}[{self.val(i.args[0], 'usize')}];")
        elif isinstance(i, ir.Access) and i.kind == "index" and (self.span_elem(i.base.type) or self.vec_elem(i.base.type)):
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)}.ptr[{self.val(i.args[0], 'usize')}];")
        elif isinstance(i, ir.Len):
            t = i.base.type
            if self.span_elem(t) or self.vec_elem(t) or self.str_kind(t):
                self.put(f"kif_t{i.dst.id} = {self.val(i.base)}.len;")
            elif t.kind == irtypes.REF and self.str_kind(t.args[0]) == "str":
                self.put(f"kif_t{i.dst.id} = {self.val(i.base)}->len;")
            elif t.kind == irtypes.ARRAY and t.length is not None:
                self.put(f"kif_t{i.dst.id} = {lw.literal(t.length, 'usize')};")
            elif t.kind == irtypes.REF and t.args[0].kind == irtypes.ARRAY and t.args[0].length is not None:
                self.put(f"kif_t{i.dst.id} = {lw.literal(t.args[0].length, 'usize')};")
            else:
                raise _Bail("length")
        elif isinstance(i, ir.Access) and i.kind == "field":
            ft = self.field_ir(i.base.type, i.name)
            arrow = "->" if i.base.type.kind == irtypes.REF or self.heap_of(i.base.type) else "."
            base = self.val(i.base)
            if self.owning_ptr(i.base.type):
                base = self.nn(base, i, i.base.type)
            if self.arr_elem(ft):
                self.alias[i.dst.id] = f"{base}{arrow}{i.name}"  # an array field is only named, never copied
            else:
                self.put(f"kif_t{i.dst.id} = {base}{arrow}{i.name};")
        elif isinstance(i, ir.Access) and i.kind == "deref" and self.heap_of(i.base.type):
            self.alias[i.dst.id] = f"(*{self.val(i.base)})"  # the pointee is named, never copied; a `ptr` was checked non-null just before
        elif isinstance(i, ir.Access) and i.kind == "deref" and i.base.type.kind == irtypes.REF:
            self.put(f"kif_t{i.dst.id} = *{self.val(i.base)};")
        elif isinstance(i, ir.Test) and i.kind == "non_null" and self.heap_of(i.base.type):
            self.put(f"kif_t{i.dst.id} = ({self.val(i.base)} != NULL);")
        elif isinstance(i, ir.Global) and i.name in lw.cells:
            raise _Bail("a cell is accessed with a closure literal: `CELL.with(|v| ..)`")
        elif isinstance(i, ir.Global) and i.name in lw.consts and self.tname(i.dst) in (*self.lw_bits(), "bool"):
            self.put(f"kif_t{i.dst.id} = {lw.const_name(lw.consts[i.name])};")
        elif isinstance(i, ir.Global) and self.is_enum(i.dst.type):
            self.put(f"kif_t{i.dst.id} = {self.enumerator(i.name)};")
        elif isinstance(i, ir.PatTest) and i.base.type.is_scalar and i.base.type.name in self.lw_bits():
            n, a = i.base.type.name, self.val(i.base)
            tests = []
            for alt in (x.strip() for x in i.pattern.split("|")):
                m = re.fullmatch(r"(-?\w+)\s*(\.\.=?)\s*(-?\w+)", alt)
                if alt == "_":
                    tests.append("1")
                elif m and all(re.fullmatch(r"-?(0x[0-9a-fA-F]+|\d+)", g) for g in (m.group(1), m.group(3))):
                    lo, hi = (self.lw.literal(int(g, 0), n) for g in (m.group(1), m.group(3)))
                    tests.append(f"({a} >= {lo} && {a} {'<=' if m.group(2) == '..=' else '<'} {hi})")
                elif re.fullmatch(r"-?(0x[0-9a-fA-F]+|\d+)", alt):
                    tests.append(f"{a} == {self.lw.literal(int(alt, 0), n)}")
                else:
                    raise _Bail("pattern")
            self.put(f"kif_t{i.dst.id} = ({' || '.join(tests)});")
        elif isinstance(i, ir.PatTest) and self.res_elem(i.base.type) is not None and re.match(r"(ok|err)\b", i.pattern):
            self.put(f"kif_t{i.dst.id} = ({self.val(i.base)} {'==' if i.pattern.startswith('ok') else '!='} 0);")
        elif isinstance(i, ir.PatTest) and i.base.type.name == "error" and not i.base.type.args:
            from .lower_c import ERROR_ERRNO

            alts = [x.strip() for x in i.pattern.split("|")]
            tests = []
            for x in alts:
                if x == "_":
                    tests.append("1")
                elif x.startswith("error::") and x[7:] in ERROR_ERRNO:
                    tests.append(f"{self.val(i.base)} == {lw.errno_value(x[7:])}")
                else:
                    raise _Bail("error pattern")
            self.put(f"kif_t{i.dst.id} = ({' || '.join(tests)});")
        elif isinstance(i, ir.Project) and self.res_elem(i.base.type) is not None and i.path.lstrip(".") == "err.0":
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)};")
        elif isinstance(i, ir.Project) and self.res_elem(i.base.type) and i.path.lstrip(".") == "ok.0":
            self.put(f"kif_t{i.dst.id} = {self.rv(self.val(i.base))};")
        elif isinstance(i, ir.PatTest) and self.is_enum(i.base.type):
            alts = [p.strip() for p in i.pattern.split("|")]
            test = "1" if "_" in alts else " || ".join(f"{self.val(i.base)} == {self.enumerator(p)}" for p in alts)
            self.put(f"kif_t{i.dst.id} = ({test});")
        elif isinstance(i, ir.Test) and i.kind == "some" and self.opt_elem(i.base.type):
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)}.has;")
        elif isinstance(i, ir.Access) and i.kind == "unwrap" and self.opt_elem(i.base.type):
            amp = "&" if i.dst.type.kind == irtypes.REF else ""  # `value()` hands out a reference to the held value
            self.put(f"kif_t{i.dst.id} = {amp}{self.val(i.base)}.v;")
        elif isinstance(i, ir.Project) and self.opt_elem(i.base.type) and i.path in ("Some.0", ".Some.0"):
            self.put(f"kif_t{i.dst.id} = {self.val(i.base)}.v;")
        elif isinstance(i, ir.Make) and i.kind in ("some", "none") and self.opt_elem(i.dst.type):
            o = self.opt_elem(i.dst.type)
            self.ctype(i.dst.type)
            body = "false" if i.kind == "none" else f"true, {self.val(i.args[0], o)}"
            self.put(f"kif_t{i.dst.id} = (struct kif_opt_{o}){{ {body} }};")
        elif isinstance(i, ir.Make) and i.kind == "struct":
            d = self.struct_of(i.dst.type)
            if d is None or set(i.names) != {f.name.text for f in d.fields}:
                raise _Bail("struct literal")
            arrs = [(n, a) for n, a in zip(i.names, i.args) if self.arr_elem(self.field_ir(i.dst.type, n))]
            parts = ", ".join(f".{n} = {self.val(a, self.field_type(i.dst.type, n) or None)}"
                              for n, a in zip(i.names, i.args) if (n, a) not in arrs)
            self.put(f"kif_t{i.dst.id} = (struct {self.sname(i.dst.type)}){{ {parts} }};")
            for n, a in arrs:  # C cannot assign an array: copy it in
                self.put(f"for (size_t kif_k = 0; kif_k < {self.field_ir(i.dst.type, n).length}; kif_k++)")
                self.put(f"\tkif_t{i.dst.id}.{n}[kif_k] = {self.val(a)}[kif_k];")
        elif isinstance(i, ir.Borrow) and i.rng and re.fullmatch(r"(\.\w+)*\[\]", i.path) and self.span_elem(i.dst.type):
            lt, name = i.local.type, self.names[i.local]
            for step in re.findall(r"\.\w+", i.path):
                name = f"{name}{'->' if lt.kind == irtypes.REF else '.'}{step[1:]}"
                lt = self.field_ir(lt, step[1:])
            if lt.kind == irtypes.REF and lt.args[0].kind == irtypes.SLICE:
                base = f"{name}.ptr"
            elif lt.kind == irtypes.SLICE:
                base = f"{name}.ptr"
            elif (lt.kind == irtypes.ARRAY and self.arr_elem(lt)) or (lt.kind == irtypes.REF and self.arr_elem(lt.args[0])):
                base = name
            elif lt.kind == irtypes.REF and self.vec_elem(lt.args[0]):
                base = f"{name}->ptr"
            elif self.vec_elem(lt):
                base = f"{name}.ptr"
            else:
                raise _Bail("range base")
            self.ctype(i.dst.type)
            lo, hi = (self.val(v, "usize") for v in i.rng)
            self.put(f"kif_t{i.dst.id} = (struct kif_{'' if i.dst.type.mut else 'c'}span_{self.span_elem(i.dst.type)}){{ {base} + {lo}, {hi} - {lo} }};")
        elif isinstance(i, ir.Access) and i.kind == "range" and self.span_elem(i.dst.type):
            bt = i.base.type.args[0] if i.base.type.kind == irtypes.REF else i.base.type
            v = self.val(i.base)
            if bt.kind == irtypes.SLICE or self.vec_elem(bt):
                base = f"{v}{'->' if i.base.type.kind == irtypes.REF and self.vec_elem(bt) else '.'}ptr"
            elif bt.kind == irtypes.ARRAY and self.arr_elem(bt):
                base = v
            else:
                raise _Bail("range base")
            self.ctype(i.dst.type)
            lo, hi = (self.val(a, "usize") for a in i.args)
            self.put(f"kif_t{i.dst.id} = (struct kif_{'' if i.dst.type.mut else 'c'}span_{self.span_elem(i.dst.type)}){{ {base} + {lo}, {hi} - {lo} }};")
        elif isinstance(i, ir.Borrow) and i.idx is not None and re.fullmatch(r"(\.\w+)*\[\]", i.path):
            self.ctype(i.dst.type)
            t, expr = i.local.type, self.names[i.local]
            for step in re.findall(r"\.\w+", i.path):
                expr = f"{expr}{'->' if t.kind == irtypes.REF else '.'}{step[1:]}"
                t = self.field_ir(t, step[1:])
            ref = t.kind == irtypes.REF
            t = t.args[0] if ref else t
            k = self.val(i.idx, "usize")
            if self.vec_elem(t):
                elem = f"{expr}{'->' if ref else '.'}ptr[{k}]"
            elif t.kind == irtypes.SLICE or (ref and t.kind == irtypes.SLICE):
                elem = f"{expr}.ptr[{k}]"
            elif self.arr_elem(t):
                elem = f"{expr}[{k}]"
            else:
                raise _Bail("element borrow")
            self.put(f"kif_t{i.dst.id} = &{elem};")
        elif isinstance(i, ir.Borrow) and re.fullmatch(r"(\.\w+)+", i.path):
            self.ctype(i.dst.type)
            t, expr = i.local.type, self.names[i.local]
            for step in i.path[1:].split("."):
                expr = f"{expr}{'->' if t.kind == irtypes.REF else '.'}{step}"
                t = self.field_ir(t, step)
            if self.arr_elem(t):
                self.alias[i.dst.id] = expr  # an array decays to a pointer to its first element
            else:
                self.put(f"kif_t{i.dst.id} = &{expr};")
        elif isinstance(i, ir.Borrow) and i.path == "*" and i.local.type.kind == irtypes.REF:
            self.ctype(i.dst.type)
            self.put(f"kif_t{i.dst.id} = {self.names[i.local]};")  # `&mut *r`: a reborrow, the same pointer
        elif isinstance(i, ir.Borrow) and not i.path:
            self.ctype(i.dst.type)
            lt, dt = i.local.type, i.dst.type
            if self.iface(lt) is not None and self.iface(dt) is not None:
                self.put(f"kif_t{i.dst.id} = {self.names[i.local]};")  # a reborrow of a handle: the same handle
            elif lt.kind == irtypes.ARRAY and dt.kind == irtypes.REF and self.arr_elem(lt):
                self.put(f"kif_t{i.dst.id} = {self.names[i.local]};")  # the array decays to a pointer to its first element
            elif lt.kind == irtypes.REF and dt.kind == irtypes.REF and self.sname(lt.args[0]) == self.sname(dt.args[0]):
                self.put(f"kif_t{i.dst.id} = {self.names[i.local]};")  # a reborrow of a reference: the same pointer
            else:
                self.put(f"kif_t{i.dst.id} = &{self.names[i.local]};")
        elif isinstance(i, ir.Clobber) and i.local is not None:
            self.put(self.clobber(i))
        else:
            raise _Bail(ir._instr(i))

    def iter_parts(self, b: ir.Value):
        """The item count and the element accessor of the sequence an iterator walks: a vector, a slice or an array. The position lives
        in `kif_it<id>`, set to 0 where the iterator value is made."""
        t = b.type
        v = self.val(b)
        if t.kind == irtypes.REF and self.vec_elem(t.args[0]) is not None:
            return f"{v}->len", lambda k: f"{v}->ptr[{k}]"
        if self.vec_elem(t) is not None:
            return f"{v}.len", lambda k: f"{v}.ptr[{k}]"
        if t.kind == irtypes.REF and t.args[0].kind == irtypes.SLICE and self.slice_c(t.args[0].args[0]):
            return f"{v}.len", lambda k: f"{v}.ptr[{k}]"
        if t.kind == irtypes.ARRAY and t.length is not None and self.arr_elem(t):
            return str(t.length), lambda k: f"{v}[{k}]"
        if t.kind == irtypes.REF and t.args[0].kind == irtypes.ARRAY and t.args[0].length is not None and self.arr_elem(t.args[0]):
            return str(t.args[0].length), lambda k: f"{v}[{k}]"
        raise _Bail("iterator")

    def slice_c(self, e: irtypes.Type) -> bool:
        return self.span_elem(irtypes.parse_type(f"&[{e}]")) is not None

    def drop(self, local: ir.Local, how: str) -> None:
        """Run the drop glue of a local that owns something: `S_drop(&x)`, where the `drops` pass said the value is always there."""
        t = local.type
        if how not in ("drop", "flag"):
            raise _Bail(f"drop {how}")
        call = self.drop_call(t, self.names[local])
        if local in self.flags:
            self.put(f"if ({self.flags[local]}) {{")
            self.put(f"\t{call}")
            self.put(f"\t{self.flags[local]} = false;")
            self.put("}")
        else:
            self.put(call)

    def heap_new(self, c: ir.Call) -> None:
        """`ptr::try_allocate(default_allocator(), args..)`: `S_try_new(&out, args..)` allocates, then runs `S_try_construct_at`."""
        if c.dst is None or self.res_elem(c.dst.type) is None or not self.res_inner(c.dst.type).name == "ptr":
            raise _Bail("ptr allocation")
        target = self.heap_of(self.res_inner(c.dst.type))
        args = list(c.args)
        if c.callee == "ptr::try_allocate":
            if not args or not isinstance(args[0], ir.Temp) or args[0].id not in self.allocs:
                raise _Bail("allocator")
            args.pop(0)
        ctor = self.lw.methods.get((target, "try_construct_at"))
        want = ctor.params[1:] if ctor is not None else []
        hints = [self.scalar_ast(q.ty, target) for q in want]
        if target is None or len(args) != len(want) or None in hints or "[]" in hints:
            raise _Bail("ptr construction")
        self.ctype(self.res_inner(c.dst.type))
        vals = [self.val(a, h if h != "struct" else None) for a, h in zip(args, hints)]
        out = f"&{self.rv(f'kif_t{c.dst.id}')}"
        self.put(f"kif_t{c.dst.id} = {target}_try_new({', '.join([out] + vals)});")

    def drop_call(self, t: irtypes.Type, expr: str) -> str:
        """The statement that releases the owned value `expr` of type `t`."""
        if self.str_kind(t) == "str":
            return f"kif_str_drop(&{expr});"
        if self.vec_elem(t):
            self.ctype(t)  # registers the element so the helpers are written
            return f"kif_vec_{self.vec_elem(t)}_drop(&{expr});"
        if self.opt_owns(t):
            self.ctype(t)
            return f"kif_opt_{self.opt_elem(t)}_drop(&{expr});"
        if self.owning_ptr(t):
            self.ctype(t)
            return f"{self.heap_of(t)}_delete({expr});"
        if not self.struct_of(t) or self.sname(t) not in self.lw.dropable:
            raise _Bail("drop")  # an owner that is not a plain struct is not written yet
        return f"{self.sname(t)}_drop(&{expr});"

    def owns_value(self, t: irtypes.Type) -> bool:
        return self.vec_elem(t) is not None or self.str_kind(t) == "str" or self.opt_owns(t) or self.owning_ptr(t) or (t.kind == irtypes.NAMED and not t.args and self.sname(t) in self.lw.dropable)

    def clobber(self, i: ir.Clobber) -> str:
        """`a.f = x`, `p->f = x` or `*p = x`: a write into part of a local, along `Clobber.path` (`.f` and `*` steps only)."""
        t, expr = i.local.type, self.names[i.local]
        if i.elem and i.path == "[]" and i.idx is not None:
            vt = t.args[0] if t.kind == irtypes.REF and self.vec_elem(t.args[0]) else t
            if self.vec_elem(vt):
                return f"{expr}{'->' if vt is not t else '.'}ptr[{self.val(i.idx, 'usize')}] = {self.val(i.src, self.vec_elem(vt))};"
            if t.kind == irtypes.REF and self.arr_elem(t.args[0]):
                return f"{expr}[{self.val(i.idx, 'usize')}] = {self.val(i.src, self.arr_elem(t.args[0]))};"
            e = self.elem_of(t)
            base = f"{expr}.ptr" if t.kind == irtypes.REF else expr
            return f"{base}[{self.val(i.idx, 'usize')}] = {self.val(i.src, e)};"
        hint = None
        steps = re.findall(r"\.\w+|\*|\[\]", i.path)
        for k, step in enumerate(steps):
            if step == "*" and t.kind == irtypes.REF:
                expr, t = f"(*{expr})", t.args[0]
            elif step == "*" and self.heap_of(t):
                expr, t = f"(*{self.nn(expr, i, t) if self.owning_ptr(t) else expr})", self.pointee(t)
            elif step.startswith("."):
                nt = self.field_ir(t, step[1:])
                if self.owning_ptr(t):
                    expr = self.nn(expr, i, t)
                expr = f"{expr}{'->' if t.kind == irtypes.REF or self.heap_of(t) else '.'}{step[1:]}"
                t, hint = nt, (nt.name if nt.is_scalar else None)
            elif step == "[]" and i.idx is not None and steps.count("[]") == 1 and (self.arr_elem(t) or self.vec_elem(t)):
                expr = f"{expr}{'.ptr' if self.vec_elem(t) else ''}[{self.val(i.idx, 'usize')}]"
                t = t.args[0]
                hint = t.name if t.is_scalar else None
            else:
                raise _Bail("path")
        if not i.path or "".join(steps) != i.path:
            raise _Bail("path")
        if hint is None and t.is_scalar:
            hint = t.name
        old = self.drop_call(t, expr) + " " if self.owns_value(t) else ""  # the value written over is released first
        return f"{old}{expr} = {self.val(i.src, hint)};"

    def scalar_ast(self, t, owner: Optional[str] = None) -> Optional[str]:
        """The scalar type name of a declared type (`u32`), `[]` for an array of scalars (passed as a pointer), None otherwise."""
        from . import ast

        bits = self.lw_bits()
        if isinstance(t, ast.PathType) and t.text == "Self" and owner is not None:
            return "struct" if owner in self.lw.structs else None
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.PathType) and t.inner.text == "Self" and owner in self.lw.structs:
            return "struct"
        if isinstance(t, ast.PathType) and (t.text in bits or t.text == "bool"):
            return t.text
        if isinstance(t, ast.ArrayType) and isinstance(t.elem, ast.PathType) and t.elem.text in bits:
            return "[]"
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.ArrayType) and isinstance(t.inner.elem, ast.PathType) and t.inner.elem.text in bits:
            return "[]"
        if isinstance(t, ast.PathType) and t.text in self.lw.structs and not self.lw.structs[t.text].generics:
            return "struct"
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.PathType) and t.inner.text in self.lw.structs:
            return "struct"
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.SliceType) and isinstance(t.inner.elem, ast.PathType) \
                and (t.inner.elem.text in bits or t.inner.elem.text in self.lw.structs or t.inner.elem.text in self.lw.enums):
            return "struct"
        if isinstance(t, ast.PathType) and t.segments[0].name == "optional":
            return "struct"
        if isinstance(t, ast.PathType) and t.segments[0].name in ("vector", "str", "str_view"):
            return "struct"
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.PathType) and t.inner.segments[0].name in ("vector", "str", "str_view"):
            return "struct"
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.PathType) and t.inner.dyn:
            return "struct"
        if isinstance(t, ast.PathType) and t.text in self.lw.enums:
            return "struct"  # not a scalar: no literal takes its type from here
        return None

    def call(self, c: ir.Call) -> None:
        from . import ast

        lw = self.lw
        if c.kind == "macro":
            if c.callee not in ("panic!", "unreachable!"):
                raise _Bail(f"macro {c.callee}")
            text = "panic: unreachable" if c.callee == "unreachable!" else "explicit panic"
            if c.args:
                if len(c.args) != 1 or c.args[0].id not in self.strs:
                    raise _Bail("panic arguments")
                text = "panic: " + self.strs[c.args[0].id]
            self.put(lw.panic_call(text, c.site) + ";")
            return
        if c.callee == "default_allocator" and not c.args and c.recv is None and c.dst is not None:
            self.allocs.add(c.dst.id)  # the target's allocator is the allocator: nothing to pass
            return
        if c.callee in ("ptr::try_allocate", "ptr::try_create") and c.recv is None:
            self.heap_new(c)
            return
        if self.vec_call(c) or self.slice_call(c) or self.str_call(c) or self.iface_call(c) or self.opt_call(c):
            return
        if c.kind == "unresolved" and c.callee in ("ok", "err") and c.dst is not None and self.res_elem(c.dst.type) is not None:
            self.make_result(c)
            return
        if c.kind not in ("free", "assoc", "method") or not c.decl or (c.recv is not None) != (c.kind == "method"):
            raise _Bail(f"call {c.kind}")
        from .lower_c import OUT_FIRST

        decl = lw.methods.get(tuple(c.decl.split("::"))) if "::" in c.decl else lw.fns.get(c.decl) or lw.fns.get(c.callee)
        recv = decl.params[0].receiver if decl is not None and decl.params else ""
        if decl is None or decl.is_gen or bool(recv) != (c.recv is not None) or any(p.receiver for p in decl.params[1:]) \
                or (decl.generics and any(not g.is_lifetime for g in decl.generics)):
            raise _Bail("callee")
        owner = c.decl.split("::")[0] if "::" in c.decl else None
        inner = lw.result_of(decl)
        if inner is not None and (c.dst is None or self.res_elem(c.dst.type) is None ):
            raise _Bail("result call")
        hints = [self.scalar_ast(p.ty, owner) for p in decl.params[1 if recv else 0:]]
        ret = None if decl.ret is None else self.scalar_ast(decl.ret, owner)
        if inner is not None:
            ret = "result"
        if None in hints or ret == "[]" or (decl.ret is not None and ret is None) or len(hints) != len(c.args):
            raise _Bail("callee signature")
        name = lw.fn_name(decl) + ("__nofacts" if c.unguarded and lw.twinned(decl) else "")
        args = [self.val(a, h if h not in ("[]", "struct") else None) for a, h in zip(c.args, hints)]
        for k, (a, p) in enumerate(zip(c.args, decl.params[1 if recv else 0:])):
            if not isinstance(a, ir.Temp):
                continue
            at = a.type
            if isinstance(p.ty, ast.RefType) and isinstance(p.ty.inner, ast.PathType) and p.ty.inner.dyn:
                args[k] = self.as_iface(a, irtypes.parse_type(f"&{p.ty.inner.text}"), args[k])
            elif isinstance(p.ty, ast.RefType) and isinstance(p.ty.inner, ast.SliceType) and at.kind == irtypes.REF \
                    and at.args[0].kind == irtypes.ARRAY and self.arr_elem(at.args[0]) and at.args[0].length is not None:
                lw.spans.setdefault(self.arr_elem(at.args[0]), (self.elem_c(self.arr_elem(at.args[0])), self.arr_elem(at.args[0]) in lw.structs))
                args[k] = f"(struct kif_{'' if at.mut else 'c'}span_{self.arr_elem(at.args[0])}){{ {args[k]}, {at.args[0].length} }}"  # `&array` as a slice
            elif isinstance(p.ty, ast.RefType) and isinstance(p.ty.inner, ast.SliceType) and at.kind == irtypes.REF and self.vec_elem(at.args[0]):
                e = self.vec_elem(at.args[0])
                self.ctype(irtypes.parse_type(f"&{'mut ' if at.mut else ''}[{e}]"))
                args[k] = f"(struct kif_{'' if at.mut else 'c'}span_{e}){{ {args[k]}->ptr, {args[k]}->len }}"  # `&vector` as a slice
        if recv == "value" and isinstance(c.recv, ir.Temp) and c.recv.owns:
            raise _Bail("method consuming a temporary")  # the IR does not say whether the callee takes it
        if recv:
            r = self.val(c.recv)
            rt = c.recv.type
            if self.owning_ptr(rt) or (rt.kind == irtypes.REF and self.owning_ptr(rt.args[0])):
                args.insert(0, self.nn(f"(*{r})" if rt.kind == irtypes.REF else r, c, rt.args[0] if rt.kind == irtypes.REF else rt))  # a method of the pointee: a null `ptr` panics
            else:
                args.insert(0, f"&{r}" if recv != "value" and rt.kind != irtypes.REF else r)  # `&self` on a value: its address
        if inner is not None and self.res_elem(c.dst.type):
            out = f"&{self.rv(f'kif_t{c.dst.id}')}"
            args.insert(0, out) if owner and decl.name.text in OUT_FIRST else args.append(out)
        args = ", ".join(args)
        if inner is not None:
            self.put(f"kif_t{c.dst.id} = {name}({args});")
        elif c.dst is None or decl.ret is None:
            self.put(f"{name}({args});")
        else:
            self.put(f"kif_t{c.dst.id} = {name}({args});")

    def slice_call(self, c: ir.Call) -> bool:
        """`as_slice` / `as_mut_slice` of an array or a slice: the same elements as a span. False for any other call."""
        if c.recv is None or c.kind != "method" or c.decl or c.callee not in ("as_slice", "as_mut_slice") or c.args or c.dst is None:
            return False
        rt = c.recv.type.args[0] if c.recv.type.kind == irtypes.REF else c.recv.type
        self.ctype(c.dst.type)
        v = self.val(c.recv)
        if rt.kind == irtypes.ARRAY and rt.length is not None and self.arr_elem(rt):
            e = self.arr_elem(rt)
            self.put(f"kif_t{c.dst.id} = (struct kif_{'' if c.callee == 'as_mut_slice' else 'c'}span_{e}){{ {v}, {rt.length} }};")
        elif rt.kind == irtypes.SLICE and self.slice_c(rt.args[0]):
            self.put(f"kif_t{c.dst.id} = {v};")
        else:
            return False
        return True

    def opt_call(self, c: ir.Call) -> bool:
        """`value_or`, `try_value` and `reset` of an `optional<T>`: calls of the `kif_opt_T_*` helpers. False for any other call."""
        if c.recv is None or c.kind != "method" or c.decl:
            return False
        rt = c.recv.type.args[0] if c.recv.type.kind == irtypes.REF else c.recv.type
        o = self.opt_elem(rt)
        if o is None:
            return False
        self.ctype(rt)
        from .types import Ty

        tag = self.lw.opt_tag(Ty(o))
        r = self.val(c.recv) if c.recv.type.kind == irtypes.REF else f"&{self.val(c.recv)}"
        name = c.callee
        if name == "value_or" and len(c.args) == 1 and c.dst is not None and self.lw.opts[tag][1] is None:
            self.put(f"kif_t{c.dst.id} = kif_opt_{tag}_value_or({r}, {self.val(c.args[0], o if o in self.lw_bits() or o == 'bool' else None)});")
        elif name == "try_value" and not c.args and c.dst is not None and self.res_elem(c.dst.type):
            self.lw.opt_try.add(tag)
            self.put(f"kif_t{c.dst.id} = kif_opt_{tag}_try_value({r}, &{self.rv(f'kif_t{c.dst.id}')});")
        elif name == "reset" and not c.args:
            self.put(f"kif_opt_{tag}_reset({r});")
        else:
            raise _Bail(f"optional::{name}")
        return True

    def iface_call(self, c: ir.Call) -> bool:
        """A method call on a `&I` / `&mut I` handle: the wrapper `I_m(handle, args)`. False for any other call."""
        if c.recv is None or c.kind != "method" or c.decl or self.iface(c.recv.type) is None:
            return False
        n = self.iface(c.recv.type)
        it = self.lw.ifaces.get(n) or self.lw.foreign_ifaces.get(n)
        m = next((x for x in it.methods if x.name.text == c.callee), None)
        if m is None or self.lw.result_of(m) is not None:
            raise _Bail("interface method")
        params = [p for p in m.params if not p.receiver]
        hints = [self.scalar_ast(p.ty) for p in params]
        ret = None if m.ret is None else self.scalar_ast(m.ret)
        if None in hints or (m.ret is not None and ret is None) or len(params) != len(c.args):
            raise _Bail("callee signature")
        args = [self.val(c.recv)] + [self.val(a, h if h != "struct" else None) for a, h in zip(c.args, hints)]
        call = f"{n}_{c.callee}({', '.join(args)})"
        self.put(f"kif_t{c.dst.id} = {call};" if c.dst is not None and m.ret is not None else f"{call};")
        return True

    VEC_SHAPE = {"try_push": "T", "try_pop": "", "try_reserve": "n", "try_resize": "n", "try_insert_at": "iT", "try_erase_at": "i",
                 "try_clone": "", "try_create": "n"}
    VEC_PLAIN = {"len": "usize", "is_empty": "bool", "capacity": "usize", "clear": None}

    def vec_call(self, c: ir.Call) -> bool:
        """`vector::try_create(n)` and the methods of a `vector<T>` of scalars: calls of the `kif_vec_T_*` helpers. False for any other call."""
        if c.callee == "vector::try_create" and c.recv is None:
            if c.dst is None or not self.vec_elem(self.res_inner(c.dst.type)):
                raise _Bail("vector element type")
            e = self.vec_elem(self.res_inner(c.dst.type))
            self.ctype(self.res_inner(c.dst.type))
            self.put(f"kif_t{c.dst.id} = kif_vec_{e}_try_create(&{self.rv(f'kif_t{c.dst.id}')}, {self.val(c.args[0], 'usize')});")
            return True
        if c.recv is None or c.kind != "method" or c.decl:
            return False
        rt = c.recv.type.args[0] if c.recv.type.kind == irtypes.REF else c.recv.type
        e = self.vec_elem(rt)
        if e is None:
            return False
        self.ctype(rt)
        name, fn = c.callee, f"kif_vec_{e}_{c.callee}"
        r = self.val(c.recv) if c.recv.type.kind == irtypes.REF else f"&{self.val(c.recv)}"
        if name in self.VEC_SHAPE:
            if c.dst is None or self.res_elem(c.dst.type) is None:
                raise _Bail("result call")
            hints = {"T": e if e in self.lw_bits() or e == "bool" else None, "n": "usize", "i": "usize"}
            shape = self.VEC_SHAPE[name]
            if len(c.args) != len(shape):
                raise _Bail("vector arguments")
            args = [r] + [self.val(a, hints[h]) for a, h in zip(c.args, shape)]
            if name == "try_clone":
                args = [f"&{self.rv(f'kif_t{c.dst.id}')}", r]
            self.put(f"kif_t{c.dst.id} = {fn}({', '.join(args)});")
        elif name == "try_at" and len(c.args) == 1 and c.dst is not None and self.res_elem(c.dst.type):
            self.put(f"kif_t{c.dst.id} = {fn}({r}, {self.val(c.args[0], 'usize')}, &{self.rv(f'kif_t{c.dst.id}')});")
        elif name in ("as_slice", "as_mut_slice") and not c.args and c.dst is not None:
            self.ctype(c.dst.type)
            m = "" if name == "as_mut_slice" else "c"
            self.put(f"kif_t{c.dst.id} = (struct kif_{m}span_{e}){{ ({r})->ptr, ({r})->len }};")
        elif name in self.VEC_PLAIN and not c.args:
            self.put(f"{'' if c.dst is None or self.VEC_PLAIN[name] is None else f'kif_t{c.dst.id} = '}{fn}({r});")
        else:
            raise _Bail(f"vector::{name}")
        return True

    STR_SHAPE = {"try_clone": "", "try_reserve": "n", "try_assign": "v", "try_append": "v", "try_push": "c", "try_pop": "",
                 "try_insert": "nv", "try_erase": "nn"}
    STR_PLAIN = {"len": "usize", "is_empty": "bool", "capacity": "usize", "clear": None, "starts_with": "bool", "contains": "bool", "view": "str_view"}
    VIEW_PLAIN = ("len", "is_empty", "starts_with", "ends_with", "contains")

    def str_call(self, c: ir.Call) -> bool:
        """`str::try_create(s)` and the methods of `str` / `str_view`: calls of the `kif_str_*` / `kif_strview_*` helpers."""
        if c.callee == "str::try_create" and c.recv is None:
            if c.dst is None or self.res_elem(c.dst.type) is None or len(c.args) != 1:
                raise _Bail("str::try_create")
            self.ctype(irtypes.parse_type("str"))
            self.put(f"kif_t{c.dst.id} = kif_str_try_create(&{self.rv(f'kif_t{c.dst.id}')}, {self.val(c.args[0])});")
            return True
        if c.recv is None or c.kind != "method" or c.decl:
            return False
        rt = c.recv.type.args[0] if c.recv.type.kind == irtypes.REF else c.recv.type
        kind = self.str_kind(rt)
        if kind is None:
            return False
        self.ctype(rt)
        name, by_ref = c.callee, c.recv.type.kind == irtypes.REF
        r = self.val(c.recv)
        if kind == "str_view":
            if name not in self.VIEW_PLAIN:
                raise _Bail(f"str_view::{name}")
            fn, r = f"kif_strview_{name}", f"(*{r})" if by_ref else r
            args = [r] + [self.val(a) for a in c.args]
            self.put(f"kif_t{c.dst.id} = {fn}({', '.join(args)});")
            return True
        fn, r = f"kif_str_{name}", r if by_ref else f"&{r}"
        if name in self.STR_SHAPE:
            if c.dst is None or self.res_elem(c.dst.type) is None or len(c.args) != len(self.STR_SHAPE[name]):
                raise _Bail("result call")
            hints = {"n": "usize", "v": None, "c": None}
            args = [r] + [self.val(a, hints[h]) for a, h in zip(c.args, self.STR_SHAPE[name])]
            if name == "try_clone":
                args = [f"&{self.rv(f'kif_t{c.dst.id}')}", r]
            self.put(f"kif_t{c.dst.id} = {fn}({', '.join(args)});")
        elif name in self.STR_PLAIN:
            args = ", ".join([r] + [self.val(a) for a in c.args])
            self.put(f"{'' if c.dst is None or self.STR_PLAIN[name] is None else f'kif_t{c.dst.id} = '}{fn}({args});")
        else:
            raise _Bail(f"str::{name}")
        return True

    def make_result(self, c: ir.Call) -> None:
        """`ok(v)` is status 0 with `v` in the value variable; `err(error::x)` is the errno of `x`."""
        d = f"kif_t{c.dst.id}"
        if c.callee == "ok":
            self.put(f"{d} = 0;")
            if self.res_elem(c.dst.type):
                if len(c.args) != 1:
                    raise _Bail("ok arguments")
                self.put(f"{self.rv(d)} = {self.val(c.args[0], self.res_scalar(c.dst.type))};")
        else:
            if len(c.args) != 1 or c.args[0].id not in self.errs:
                raise _Bail("err argument")
            self.put(f"{d} = {self.errs[c.args[0].id]};")

    def binary(self, i: ir.Binary) -> str:
        lw, op = self.lw, i.op
        if op in ("==", "!=") and self.is_enum(i.a.type):
            return f"({self.val(i.a)} {op} {self.val(i.b)})"
        if op in _CMP:
            n = self.tname(i.a, self.tname(i.b, "i32"))
            return f"({self.val(i.a, n)} {op} {self.val(i.b, n)})"
        n = self.tname(i.dst)
        if op in ("&", "|", "^"):
            a, b = self.val(i.a, n), self.val(i.b, n)
            return f"({a} {op} {b})" if n == "bool" else f"(({lw.scalar(n)})({a} {op} {b}))"
        if n not in self.lw_bits():
            raise _Bail(f"operator {op}")
        t, u = lw.scalar(n), "uint32_t" if self.lw_bits()[n] <= 32 else "uint64_t"
        if op in "+-*" and len(op) == 1:
            a, b = self.val(i.a, n), self.val(i.b, n)
            return lw.plain_wrap(op, n, a, b) or f"{lw.arith_helper(op, n)}({a}, {b})"
        if op in ("/", "%"):
            return f"(({t})({self.val(i.a, n)} {op} {self.val(i.b, n)}))"
        if op in ("<<", ">>"):
            a, b = self.val(i.a, n), self.val(i.b, "u64")
            return f"(({t})(({u}){a} << {b}))" if op == "<<" else f"(({t})({a} >> {b}))"
        raise _Bail(f"operator {op}")

    def check(self, c: ir.Check) -> None:
        if c.kind == "index":
            i, n = c.args
            self.put(f"if ((uint64_t){self.val(i, 'usize')} >= (uint64_t){self.val(n, 'usize')})")
            self.put(f"\t{self.trap(c, 'index out of bounds')};")
        elif c.kind == "div":
            a, b = c.args
            n = c.ty or self.tname(b)
            self.put(f"if ({self.val(b, n)} == 0)")
            self.put(f"\t{self.trap(c, 'integer division by zero')};")
            if irtypes.parse_type(n).is_signed:
                t = self.lw.scalar(n)
                self.put(f"if ({self.val(a, n)} == {self.lw.min_literal(n)} && {self.val(b, n)} == ({t})-1)")
                self.put(f"\t{self.trap(c, 'integer overflow in division')};")
        elif c.kind == "shift":
            (n,) = c.args
            bits = self.lw_bits()[c.ty]
            self.put(f"if ((uint64_t){self.val(n, 'u64')} >= {bits})")
            self.put(f"\t{self.trap(c, f'shift count is not below {bits}')};")
        elif c.kind == "range":
            lo, hi, n = (self.val(a, "usize") for a in c.args)
            zero = isinstance(c.args[0], ir.Const) and c.args[0].value == 0  # `0 > hi` would trip -Wtype-limits
            self.put(f"if ({'' if zero else f'(uint64_t){lo} > (uint64_t){hi} || '}(uint64_t){hi} > (uint64_t){n})")
            self.put(f"\t{self.trap(c, 'slice range out of bounds')};")
        elif c.kind == "non_null" and self.heap_of(c.args[0].type):
            self.put(f"if ({self.val(c.args[0])} == NULL)")
            self.put(f"\t{self.trap(c, 'null pointer')};")
        elif c.kind == "some" and self.opt_elem(c.args[0].type):
            self.put(f"if (!{self.val(c.args[0])}.has)")
            self.put(f"\t{self.trap(c, 'optional has no value')};")
        else:
            raise _Bail(f"check {c.kind}")

    def render(self, items, chunks, gotos, depth: int) -> List[str]:
        """The C lines of a structured tree (`structure.py`); `depth` extra tabs indent the nested ones (`#line` and labels stay at column 0)."""
        pad = "\t" * depth
        out: List[str] = []

        def indent(lines: List[str]) -> List[str]:
            return [ln if ln.startswith("#") or ln.endswith(":;") else pad + ln for ln in lines]

        for it in items:
            if isinstance(it, structure.Blk):
                if it.block in gotos:
                    out.append(f"{it.block.name.replace('.', '_')}:;")
                out += indent(chunks[it.block])
            elif isinstance(it, structure.Goto):
                out.append(f"{pad}\tgoto {it.target.name.replace('.', '_')};")
            elif isinstance(it, structure.Break):
                out.append(f"{pad}\tbreak;")
            elif isinstance(it, structure.Continue):
                out.append(f"{pad}\tcontinue;")
            elif isinstance(it, structure.Loop):
                out += self.render_loop(it, chunks, gotos, depth)
            elif isinstance(it, structure.If):
                out += self.render_if(it, chunks, gotos, depth)
        return out

    @staticmethod
    def vacant(items, chunks) -> bool:
        """Whether an arm writes nothing: no items, or only blocks without lines."""
        return all(isinstance(x, structure.Blk) and not chunks[x.block] for x in items)

    def render_loop(self, it, chunks, gotos, depth: int) -> List[str]:
        """`for (;;)`; a loop that starts with its exit test is `while (c)` when nothing runs before the test, else the test is
        `if (!c) break;` after the lines that compute it."""
        pad = "\t" * depth
        body = it.body
        head = body[0] if body and isinstance(body[0], structure.Blk) else None
        test = body[1] if head is not None and len(body) == 2 and isinstance(body[1], structure.If) and body[1].block is head.block else None
        if test is not None and head.block not in gotos and len(test.other) == 1 and isinstance(test.other[0], structure.Break):
            cond = self.conds[head.block]
            lines = chunks[head.block]
            if all(ln.startswith("#") or ln.lstrip().startswith("/*") for ln in lines):
                out = [ln for ln in lines if ln.startswith("#")]
                out.append(f"{pad}\twhile ({cond}) {{")
                out += self.render(test.then, chunks, gotos, depth + 1)
                out.append(f"{pad}\t}}")
                return out
            neg = f"!{cond}" if re.fullmatch(r"\w+", cond) else f"!({cond})"
            out = [f"{pad}\tfor (;;) {{"]
            out += self.render([head], chunks, gotos, depth + 1)
            out.append(f"{pad}\t\tif ({neg}) break;")
            out += self.render(test.then, chunks, gotos, depth + 1)
            out.append(f"{pad}\t}}")
            return out
        out = [f"{pad}\tfor (;;) {{"]
        out += self.render(body, chunks, gotos, depth + 1)
        out.append(f"{pad}\t}}")
        return out

    def render_if(self, it, chunks, gotos, depth: int) -> List[str]:
        pad = "\t" * depth
        cond = self.conds[it.block]
        then, other = it.then, it.other
        if self.vacant(then, chunks):
            then = []
        if self.vacant(other, chunks):
            other = []
        if not then and not other:
            return []
        if not then:
            cond, then, other = (cond[1:] if re.fullmatch(r"!\w+", cond) else f"!{cond}" if re.fullmatch(r"\w+", cond) else f"!({cond})"), other, []
        out = [f"{pad}\tif ({cond}) {{"]
        out += self.render(then, chunks, gotos, depth + 1)
        while other:
            if len(other) == 1 and isinstance(other[0], structure.If) and other[0].block not in gotos and other[0].then and not chunks[other[0].block]:
                nxt = other[0]
                out.append(f"{pad}\t}} else if ({self.conds[nxt.block]}) {{")
                out += self.render(nxt.then, chunks, gotos, depth + 1)
                other = nxt.other
                continue
            out.append(f"{pad}\t}} else {{")
            out += self.render(other, chunks, gotos, depth + 1)
            break
        out.append(f"{pad}\t}}")
        return out

    def term(self, b: ir.Block) -> None:
        """The lines a terminator writes itself: a return or an unreachable. Jumps and branches are the structure's job (`structure.py`);
        a branch's condition is kept for it."""
        t = b.term
        if isinstance(t, ir.Jump):
            pass
        elif isinstance(t, ir.Branch):
            self.conds[b] = self.val(t.cond)
        elif isinstance(t, ir.Return) and self.res_elem(self.fn.ret_type) is not None:
            v = t.value
            if v is None:
                raise _Bail("result return")
            if self.res_elem(v.type) is not None:
                if self.out_param:
                    self.put(f"if ({self.val(v)} == 0) *kif_out = {self.rv(self.val(v))};")
            elif not (isinstance(v, ir.Temp) and v.id not in self.errs and v.ty == "i32"):
                raise _Bail("result return")
            if self.ctor is not None and self.lw.cur_owner in self.lw.owned:
                self.put(f"if ({self.val(v)} != 0) {self.lw.cur_owner}_drop_fields({self.names[self.ctor]});")  # a failed constructor releases what it built
            self.put(f"return {self.val(v)};")
        elif isinstance(t, ir.Return):
            self.put("return;" if t.value is None else f"return {self.val(t.value, self.fn.ret)};")
        elif isinstance(t, ir.Unreachable):
            self.put("KIF_UNREACHABLE();")
        else:
            raise _Bail("terminator")

    def res_decl(self, decls: List[str], t: irtypes.Type, name: str) -> None:
        """The value variable that goes with the status variable `name` of a `result<T>`."""
        e = self.res_elem(t)
        if e:
            decls.append(f"\t{e} {self.rv(name)} = {self.zero(self.res_inner(t))};")
            decls.append(f"\t(void){self.rv(name)};")

    def unify_unknown(self) -> None:
        """A value the checker left as `optional<?>` (a `none()`, an `if` result) takes the type of what it is stored into, loaded from or
        returned as. Repeats until nothing changes."""
        def unknown(x) -> bool:
            return x.ty is None or "?" in x.ty or (x.ty.startswith("result<") and x.type.kind == irtypes.UNKNOWN)

        def give(x, ty) -> bool:
            if ty is None or "?" in ty or not unknown(x):
                return False
            x.ty = ty
            x.__dict__.pop("type", None)
            return True

        incl = {i.dst.id: i for _, i in self.fn.instrs() if isinstance(i, ir.Make) and i.kind == "range_inclusive"}
        changed = True
        while changed:
            changed = False
            for b in self.fn.blocks:
                for i in [*b.instrs, b.term]:
                    if isinstance(i, ir.Access) and i.kind == "item" and isinstance(i.base, ir.Temp) and i.base.id in incl:
                        a = incl[i.base.id].args
                        changed |= give(i.dst, getattr(a[0], "ty", None)) or give(i.dst, getattr(a[1], "ty", None))
                    elif isinstance(i, ir.Return) and isinstance(i.value, ir.Temp):
                        changed |= give(i.value, self.fn.ret)
                    elif isinstance(i, ir.Load):
                        changed |= give(i.dst, i.local.ty) or give(i.local, i.dst.ty)
                    elif isinstance(i, ir.Store) and isinstance(i.src, ir.Temp):
                        changed |= give(i.src, i.local.ty) or give(i.local, i.src.ty)
                    elif isinstance(i, ir.Len):
                        changed |= give(i.dst, "usize")
                    elif isinstance(i, ir.Binary) and i.op in "+-*/%&|^" and len(i.op) == 1:
                        changed |= give(i.dst, getattr(i.a, "ty", None)) or give(i.dst, getattr(i.b, "ty", None))

    def type_vectors(self) -> None:
        """`vector::try_create(n)?` is typed `vector<?>`: the checker does not infer the element from the expected type. Give the call and
        its `?` value the element type of the local they are stored into."""
        for _, i in self.fn.instrs():  # `T::try_allocate(default_allocator(), args)` is `T::try_create(args)`: the target's allocator is the allocator
            if isinstance(i, ir.Call) and i.callee == "default_allocator" and not i.args and i.recv is None and i.dst is not None:
                self.allocs.add(i.dst.id)
        for _, i in self.fn.instrs():
            if isinstance(i, ir.Call) and i.callee in ("vector::try_allocate", "str::try_allocate") and i.recv is None and i.args \
                    and isinstance(i.args[0], ir.Temp) and i.args[0].id in self.allocs:
                i.callee, i.args = i.callee.replace("try_allocate", "try_create"), i.args[1:]
        defs = {i.defs().id: i for _, i in self.fn.instrs() if i.defs() is not None}
        for _, i in self.fn.instrs():  # `&a[lo..hi]` is typed `&?`: a slice of the elements of `a`
            if isinstance(i, ir.Borrow) and i.rng and i.dst.ty in ("&?", "&mut ?"):
                lt = i.local.type
                for step in re.findall(r"\.\w+", i.path):
                    lt = self.field_ir(lt, step[1:])
                lt = lt.args[0] if lt.kind == irtypes.REF else lt
                if lt.kind in (irtypes.SLICE, irtypes.ARRAY) or (lt.kind == irtypes.NAMED and lt.name == "vector" and len(lt.args) == 1):
                    i.dst.ty = f"{'&mut ' if i.mut else '&'}[{lt.args[0]}]"
                    i.dst.__dict__.pop("type", None)
        for _, i in self.fn.instrs():  # `a[lo..hi]` of an array, slice or vector is the slice of its elements
            if isinstance(i, ir.Access) and i.kind == "range" and i.dst.ty in (None, "?", "&?", "&mut ?") or (isinstance(i, ir.Access) and i.kind == "range" and "?" in (i.dst.ty or "")):
                bt = i.base.type.args[0] if i.base.type.kind == irtypes.REF else i.base.type
                if bt.kind in (irtypes.SLICE, irtypes.ARRAY) or (bt.kind == irtypes.NAMED and bt.name == "vector" and len(bt.args) == 1):
                    i.dst.ty = f"&[{bt.args[0]}]"
                    i.dst.__dict__.pop("type", None)
        for _, i in self.fn.instrs():  # `&mut *a` of an array reference is typed `&mut [T; _]`: the length is the referent's
            if isinstance(i, ir.Borrow) and i.path in ("", "*") and i.local.type.kind == irtypes.REF and i.local.type.args[0].kind == irtypes.ARRAY \
                    and i.local.type.args[0].length is not None and i.dst.type.kind == irtypes.REF and i.dst.type.args[0].kind == irtypes.ARRAY \
                    and i.dst.type.args[0].length is None:
                i.dst.ty = f"{'&mut ' if i.mut else '&'}{i.local.type.args[0]}"
                i.dst.__dict__.pop("type", None)
        for _, i in self.fn.instrs():  # `&arr` is typed `&[T; _]`: the length is the local's
            if isinstance(i, ir.Borrow) and not i.path and i.local.type.kind == irtypes.ARRAY and i.local.type.length is not None \
                    and i.dst.type.kind == irtypes.REF and i.dst.type.args[0].kind == irtypes.ARRAY and i.dst.type.args[0].length is None:
                i.dst.ty = f"{'&mut ' if i.mut else '&'}[{i.local.type.args[0]}; {i.local.type.length}]"
                i.dst.__dict__.pop("type", None)
        for _, i in self.fn.instrs():  # a method the checker could not type: the helpers have fixed result types
            if isinstance(i, ir.Call) and i.kind == "method" and i.recv is not None and i.dst is not None and i.dst.ty in (None, "?"):
                rt = i.recv.type.args[0] if i.recv.type.kind == irtypes.REF else i.recv.type
                ve = self.vec_elem(rt)
                oe = self.opt_elem(rt)
                if oe and i.callee == "try_value":
                    i.dst.ty = f"result<&{oe}>"
                    i.dst.__dict__.pop("type", None)
                    continue
                if ve and i.callee == "try_at":
                    i.dst.ty = f"result<&{ve}>"
                    i.dst.__dict__.pop("type", None)
                    continue
                if i.callee in ("as_slice", "as_mut_slice") and not i.args and (ve or rt.kind in (irtypes.ARRAY, irtypes.SLICE)):
                    el = ve or rt.args[0]
                    i.dst.ty = f"&{'mut ' if i.callee == 'as_mut_slice' else ''}[{el}]"
                    i.dst.__dict__.pop("type", None)
                    continue
                table = self.STR_PLAIN if self.str_kind(rt) else self.VEC_PLAIN if self.vec_elem(rt) else {}
                ty = "bool" if self.str_kind(rt) == "str_view" and i.callee != "len" else table.get(i.callee)
                if self.str_kind(rt) == "str_view" and i.callee == "len":
                    ty = "usize"
                if ty:
                    i.dst.ty = ty
                    i.dst.__dict__.pop("type", None)
        for _, i in self.fn.instrs():
            if isinstance(i, ir.Store) and isinstance(i.src, ir.Temp):
                dest = i.local.type
            elif isinstance(i, ir.Clobber) and i.local is not None and isinstance(i.src, ir.Temp) and i.idx is None and not i.elem \
                    and re.fullmatch(r"(\.\w+|\*)+", i.path):
                dest = i.local.type
                for step in re.findall(r"\.\w+|\*", i.path):
                    dest = self.pointee(dest) if step == "*" else self.field_ir(dest, step[1:])
            else:
                continue
            if not (self.vec_elem(dest) or self.owning_ptr(dest) or self.str_kind(dest) == "str"):
                continue
            acc = defs.get(i.src.id)
            if isinstance(acc, ir.Access) and acc.kind == "ok" and isinstance(acc.base, ir.Temp) and isinstance(defs.get(acc.base.id), ir.Call) \
                    and defs[acc.base.id].callee in ("vector::try_create", "ptr::try_allocate", "ptr::try_create") and "?" in (acc.base.ty or "?"):
                for t, ty in ((i.src, str(dest)), (acc.base, f"result<{dest}>")):
                    t.ty = ty
                    t.__dict__.pop("type", None)

    def run(self, cnames: List[str]) -> str:
        fn, lw = self.fn, self.lw
        res = self.res_elem(fn.ret_type)
        if res:
            if cnames[-1:] == ["kif_out"]:
                self.out_param, cnames = True, cnames[:-1]
            elif cnames[:1] == ["kif_out"]:
                self.out_param, cnames = True, cnames[1:]  # a constructor (`try_create`): the new object comes first
            else:
                raise _Bail("result out parameter")
        if len(cnames) != len(fn.params):
            raise _Bail("parameters")
        by_value_self = fn.node is not None and fn.node.params and fn.node.params[0].receiver == "value"
        if by_value_self and (not fn.params or self.struct_of(fn.params[0].type) is None):
            raise _Bail("by-value self")
        self.ctype(fn.ret_type)
        for p, c in zip(fn.params, cnames):
            self.ctype(p.type, param=True)
            self.names[p] = f"(*{c})" if by_value_self and p is fn.params[0] else c  # C passes `self` by pointer; the IR holds the value
        self.type_vectors()
        self.unify_unknown()
        decls: List[str] = []
        for l in fn.locals:
            if l in self.names:
                continue
            self.names[l] = lw.bind(l.name, None)
            decls.append(self.declare(l.type, self.names[l], self.ctype(self.vtype(l), param=self.arr_elem(l.type) is not None)))
            self.res_decl(decls, l.type, self.names[l])
        used_temps: Dict[int, ir.Temp] = {}
        for _, i in fn.instrs():
            if (isinstance(i, ir.Dead) and i.drop == "flag") or (isinstance(i, ir.Store) and i.drop_old == "flag"):
                self.flags[i.local] = f"kif_live_{self.names[i.local]}"
                decls.append(f"\tbool {self.flags[i.local]} = false;")
        for _, i in fn.instrs():
            if isinstance(i, ir.Store) and isinstance(i.src, ir.Temp) and self.arr_elem(i.local.type):
                self.arr_ty[i.src.id] = i.local.type
        for _, i in fn.instrs():
            if isinstance(i, ir.Make) and i.kind == "struct" and self.struct_of(i.dst.type) is not None:
                for n, a in zip(i.names, i.args):
                    if isinstance(a, ir.Temp) and self.arr_elem(self.field_ir(i.dst.type, n)):
                        self.arr_ty[a.id] = self.field_ir(i.dst.type, n)
        read: Set[ir.Local] = set()
        for _, i in fn.instrs():
            if isinstance(i, ir.Load):
                read.add(i.local)
            if i.defs() is not None:
                used_temps[i.defs().id] = i.defs()
        for l in fn.locals:
            if l not in read and l not in fn.params:
                decls.append(f"\t(void){self.names[l]};")
        used_ids: Set[int] = set()
        for b in fn.blocks:
            for i in [*b.instrs, b.term]:
                if isinstance(i, ir.Store):
                    moved = [i.src]
                elif isinstance(i, ir.Return):
                    moved = [i.value]
                elif isinstance(i, (ir.Call, ir.Make)):
                    moved = list(i.args)
                elif isinstance(i, ir.Clobber):
                    moved = [i.src]
                elif isinstance(i, ir.Release):
                    moved = [i.value]  # dropped here
                else:
                    moved = []
                used_ids.update(u.id for u in moved if isinstance(u, ir.Temp))
        fresh = {i.defs().id for _, i in fn.instrs() if i.defs() is not None and isinstance(i, (ir.Call, ir.Make)) or (isinstance(i, ir.Access) and i.kind == "ok")}
        for t in used_temps.values():
            if t.id in fresh and self.owns_value(t.type) and t.id not in used_ids:
                raise _Bail("a dropped temporary")  # the IR does not drop a value no local holds
        iters = {i.base.id for _, i in fn.instrs() if isinstance(i, ir.Test) and i.kind == "has_next" and isinstance(i.base, ir.Temp)}
        for k in sorted(iters):
            decls.append(f"\tsize_t kif_it{k} = 0;")
        for _, i in fn.instrs():
            if isinstance(i, ir.Make) and i.kind == "range_inclusive" and i.args and i.args[0].type.is_scalar and i.args[0].type.name in _INT_NAMES:
                c = lw.scalar(i.args[0].type.name)
                decls.append(f"\t{c} kif_rlo{i.dst.id} = 0, kif_rhi{i.dst.id} = 0;")
        chunks: Dict[ir.Block, List[str]] = {}
        ctor = lw.cur_owner if fn.name == "try_construct_at" and fn.params and self.heap_of(fn.params[0].type) == lw.cur_owner else None
        self.ctor = fn.params[0] if ctor else None
        for k, b in enumerate(fn.blocks):
            self.out = []
            if k == 0 and ctor:
                self.put(f"{ctor}_init({self.names[fn.params[0]]});")  # construction starts from the default shell
            for i in b.instrs:
                self.elided_notes(i)
                self.instr(i)
                if i.defs() is not None and i.defs().id in iters:
                    self.put(f"kif_it{i.defs().id} = 0;")
            self.elided_notes(b.term)
            self.term(b)
            chunks[b] = self.out
        textfold.fold(chunks, self.conds, notes=self.lw.t.flag("fold_notes", True), pure=textfold.PURE_C)
        st = structure.build(fn) or structure.flat(fn)
        rendered = self.render(st.items, chunks, st.gotos, 0)
        for t in used_temps.values():
            if t.id in self.strs or t.id in self.errs or t.id in self.alias or t.id in self.allocs or t.id in self.rinc or t.type.kind == irtypes.UNIT:
                continue
            if not re.search(rf"\bkif_t{t.id}(_v)?\b", "\n".join(rendered)):
                continue  # folded into the expressions that used it
            tt = self.arr_ty.get(t.id, t.type)
            decls.append(self.declare(tt, f"kif_t{t.id}", self.ctype(tt if tt is not t.type else self.vtype(t), param=True)))
            self.res_decl(decls, t.type, f"kif_t{t.id}")
            name = f"kif_t{t.id}"
            text = "\n".join(rendered)
            read_back = re.search(rf"\b{name}\b(?!(_v)?\s*=[^=])", text) or re.search(rf"[=(,&*+\-/<>!|]\s*{name}\b", text)
            if t.id not in used_ids or not read_back:
                decls.append(f"\t(void)kif_t{t.id};")  # an array decays to a pointer here, so this is fine for those too
        lines = decls + rendered
        return "\n".join(lines)


def emit_body(lw, fn: ir.Function, cnames: List[str]) -> Optional[str]:
    """The C text between the braces of `fn`, or None when the IR holds something the emitter does not handle yet (`fn_body` then reports it
    as an error). `lw` is the `Lowerer` that supplies names, types and the shared helpers; `cnames` are the C parameter names."""
    try:
        return _Emit(lw, fn).run(cnames)
    except _Bail as e:
        lw.ir_bails[fn.name] = str(e) or "bail"
        return None
