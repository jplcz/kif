# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The kif mid-level IR: a control-flow graph of basic blocks over three-address instructions (docs/kif-ir.md).

It exists so that analyses and transformations (proving a run-time check cannot fail, dropping it, later inlining and more)
work on one small, explicit form instead of on the AST or on generated C. Nothing generates code from it yet: the C backend
still lowers the checked AST directly. The IR is built from a checked function by `build_function` and can be printed
(`dump`), verified (`verify`) and analysed (`CFG`).

Shape:

* A `Function` has `Local` slots (parameters, `let` bindings, compiler temporaries that cross blocks) and a list of `Block`s.
  The first block is the entry. Each block is a list of `Instr` and ends in exactly one terminator.
* Operands are `Temp`s (single-assignment values: defined by one instruction, used after it) or `Const`s. Mutable state lives in
  locals only, through `Load` and `Store`, so there are no phi nodes and a dataflow fact about a local is a fact about a slot.
* Every operation that can panic is split in two: a `Check` (the run-time test, with its kind and operands) and an unchecked
  `Access` that is only valid after it. A pass that proves a check cannot fail sets `Check.proof` (the reason, for
  `--explain-checks`); a later code generator omits proved checks. Nothing is deleted, so the IR stays a record of what the
  source asked for.
* Each instruction keeps `site`, the AST node it came from, so a backend or a diagnostic can map back to the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union

from . import ast
from . import facts
from . import wipe as wipe_mod
from . import types as T
from .irtypes import Type, parse_type, ty_text
from .types import Ty


class Unsupported(Exception):
    """The builder does not handle a construct yet; the function is skipped (it is not an error in the program)."""

    def __init__(self, message: str, node=None) -> None:
        super().__init__(message)
        self.message, self.node = message, node


# ------------------------------------------------------------------ values


@dataclass(eq=False)
class Local:
    """A mutable slot: a parameter, a `let` binding or a compiler temporary that lives across blocks."""

    id: int
    name: str
    ty: str  # the type as written, e.g. `u32`, `&[u32]`, `optional<u32>`; `?` when unknown
    mut: bool = True
    param: bool = False
    length: Optional[int] = None  # the element count when the type is an array with a constant length
    owns: bool = False  # the slot holds a value with drop glue (a vector, a string, a struct with a `drop` or an owning field): see Dead.drop
    taint: bool = False  # a `#[tainted]` parameter: the slot starts out holding untrusted data (docs/kif-taint.md); read by the `taint` pass
    wipe: bool = False  # a secure variable (`#[secure_wipe]`, or a plain local of a `#[secure_wipe]` function): the `wipes` pass wipes it (docs/kif-secure-wipe.md)
    wipe_volatile: bool = False  # `#[secure_wipe(volatile)]`: the wipe uses the target's volatile primitive
    secure_struct: bool = False  # the slot holds a `#[secure_wipe]` struct by value: a moved-from slot is wiped field by field (rule W9)

    @cached_property
    def type(self) -> Type:
        """`ty` as a structured `Type` (kiflang/irtypes.py); an array with a constant `length` carries it."""
        t = parse_type(self.ty)
        return Type(t.kind, t.name, t.args, t.mut, self.length) if t.kind == "array" and self.length is not None else t

    def __str__(self) -> str:
        return f"%{self.name}"


@dataclass(eq=False)
class Temp:
    """A single-assignment value, defined by exactly one instruction."""

    id: int
    ty: Optional[str] = None
    owns: bool = False  # the value has drop glue (see `owns_type`): whoever ends up holding it must drop it, or a `Release` does

    @cached_property
    def type(self) -> Type:
        return parse_type(self.ty)

    def __str__(self) -> str:
        return f"t{self.id}"


@dataclass(frozen=True)
class Const:
    value: Union[int, bool, str]
    ty: Optional[str] = None

    def __str__(self) -> str:
        return ("true" if self.value else "false") if isinstance(self.value, bool) else repr(self.value) if isinstance(self.value, str) else str(self.value)


Value = Union[Temp, Const]

# ------------------------------------------------------------------ instructions


UNSIGNED_TYPES = {"u8", "u16", "u32", "u64", "u128", "usize"}


@dataclass(eq=False)
class Instr:
    site: Optional[ast.Node] = field(default=None, repr=False, kw_only=True)

    def defs(self) -> Optional[Temp]:
        return getattr(self, "dst", None)

    def uses(self) -> List[Value]:
        return []


@dataclass(eq=False)
class Load(Instr):
    """`dst = local`"""
    dst: Temp
    local: Local

    def uses(self) -> List[Value]:
        return []


@dataclass(eq=False)
class Move(Load):
    """`dst = move local`: a read that hands the value on (the checker marked the use as a move: a by-value argument, a returned
    local, `let b = a`). The slot holds no value afterwards until it is stored again. It is a `Load` for every analysis that does
    not care about ownership, so a pass that only reads values needs no rule for it."""


@dataclass(eq=False)
class Store(Instr):
    """`local = src`. `init` is true for the first store of a declaration (`let x = ..`, a pattern binding, a loop variable), false for a reassignment."""
    local: Local
    src: Value
    init: bool = False
    drop_old: str = ""  # set by the `drops` pass for a reassignment of an owning slot: "drop" the old value first, "flag" if it is only sometimes there

    def uses(self) -> List[Value]:
        return [self.src]


@dataclass(eq=False)
class Unary(Instr):
    dst: Temp
    op: str  # - ! ~
    a: Value

    def uses(self) -> List[Value]:
        return [self.a]


@dataclass(eq=False)
class Binary(Instr):
    """Arithmetic keeps the language's overflow behaviour; the IR does not model it separately."""
    dst: Temp
    op: str
    a: Value
    b: Value

    def uses(self) -> List[Value]:
        return [self.a, self.b]


@dataclass(eq=False)
class Cast(Instr):
    dst: Temp
    a: Value
    ty: str

    def uses(self) -> List[Value]:
        return [self.a]


@dataclass(eq=False)
class Call(Instr):
    """A call of a named function or method; `dst` is None when the result is unit. `recv` is the receiver, when there is one."""
    dst: Optional[Temp]
    callee: str
    args: List[Value]
    recv: Optional[Value] = None
    kind: str = "unresolved"  # free | method | assoc (`Type::f`) | value (through a function value) | macro | unresolved
    decl: str = ""  # the declaration a resolved call goes to, `f` or `Type::f`
    id: int = 0  # numbers the call in its function; the `Check`s of the callee's parameter facts carry it in `Check.call`
    flows: Optional[List[int]] = None  # which operands the result may borrow from (-1 the receiver, k the k-th of `args`); None: unknown, assume all
    ensures: Optional[Tuple[str, int]] = None  # `#[ensures(f)]` of the callee: (fact, n) the result has (`some`, `non_null`, `non_empty`, `len_ge` with n >= 1 elements)
    preserves: Optional[List[int]] = None  # `#[preserves]` of the callee: the operands (-1 receiver, k the k-th of `args`) that keep every fact the caller knew
    taint: str = ""  # what the callee's declaration says about its result: `untaint` (clean even from tainted input), `tainted_result`, or `clean` (the callee's body check proved its result clean, `taint.summarized`)
    reason: str = ""  # the reason written in `untaint!("reason", f(..))`: the code generators print it as an audit comment
    unguarded: bool = False  # set by the `elide` pass: every fact the callee requires is proved here, so the call may skip the callee's guard (`F__nofacts`)

    def uses(self) -> List[Value]:
        return ([self.recv] if self.recv is not None else []) + list(self.args)


@dataclass(eq=False)
class Len(Instr):
    """The element count of a vector, slice, string or array value."""
    dst: Temp
    base: Value

    def uses(self) -> List[Value]:
        return [self.base]


@dataclass(eq=False)
class Test(Instr):
    """A side-effect-free question about a value: `some` (optional holds a value), `non_null`, `non_empty`, `failed` (a `result` holds an
    error: the test of `?`), `has_next` (an iterator has another item)."""
    dst: Temp
    kind: str
    base: Value

    def uses(self) -> List[Value]:
        return [self.base]


# kind -> operand count: (idx, len), (lo, hi, len), (v), (v), (dividend, divisor), (amount)
CHECK_KINDS = {"index": 2, "range": 3, "some": 1, "non_null": 1, "div": 2, "shift": 1}


@dataclass(eq=False)
class Check(Instr):
    """The run-time test of an operation that panics when it fails.

    `index`: `args = (i, len)` needs `i < len`. `range`: `(lo, hi, len)` needs `lo <= hi <= len`. `some`: `(v)` an optional holding a
    value. `non_null`: `(v)` a pointer that is not null. `div`: `(a, b)` for `a / b` and `a % b`, needs `b != 0` and, when the
    type `ty` is signed, not `a == MIN && b == -1`. `shift`: `(n)` the amount of `<<` / `>>` on type `ty`, needs `0 <= n < bits(ty)`. `proof` is None while the check stays; a pass that shows it cannot fail
    sets it to the reason, and a code generator then leaves the check out."""
    kind: str
    args: List[Value]
    proof: Optional[str] = None
    ty: Optional[str] = None  # the integer type of a `div` or `shift`
    refuted: Optional[str] = None  # a tagged check that provably fails: why (set by the provers)
    tag: Optional[str] = None  # a parameter fact (`some:p`): the check is the entry assumption of a function or a requirement at a call
    call: int = 0  # for a requirement at a call: the `Call.id` it guards (0 for an entry assumption and the checks of operations)

    def uses(self) -> List[Value]:
        return list(self.args)


ACCESS_KINDS = ("index", "range", "unwrap", "deref", "field", "ok", "error", "item")


@dataclass(eq=False)
class Access(Instr):
    """An unchecked access, valid only once its `Check` has passed (or been proved): `index` `base[i]`, `range` a sub-slice
    `base[lo..hi]`, `unwrap` the value of an optional, `deref` the target of a pointer, `field` `base.name`, `ok` / `error` the success
    value / the error of a `result` (after a `Test` `failed` said which it is; `?` uses both), `item` what an iterator yields (after a `Test` `has_next`)."""
    dst: Temp
    kind: str
    base: Value
    args: List[Value] = field(default_factory=list)
    name: str = ""

    def uses(self) -> List[Value]:
        return [self.base] + list(self.args)


@dataclass(eq=False)
class Live(Instr):
    """The slot `local` comes into scope (its `let` or pattern binding): any value it held from an earlier trip round a loop is gone."""
    local: Local


@dataclass(eq=False)
class Dead(Instr):
    """The slot `local` goes out of scope: at the end of its block, and on every `return`, `break` and `continue` that leaves the block
    (innermost first, after the value that leaves has been computed). A reference to the slot still alive here dangles, and an owned
    value still in the slot is dropped. Parameters never die: they live for the whole function.
    `drop` is filled in by the `drops` pass (kiflang/passes.py) for a slot that `owns` a value: "" no drop (nothing owned, or the value
    was moved out on every path), "drop" the value is always there, "flag" it is there on some paths only, so the code generator keeps a flag."""
    local: Local
    drop: str = ""


@dataclass(eq=False)
class Cell(Instr):
    """`dst = &CELL` or `&mut CELL`: the address of a `#[tls]` / `#[per_cpu]` static in this thread's or CPU's block. The body of the
    closure of `CELL.with(|v| ..)` / `with_mut` follows inline, with `v` bound to `dst`: the reference lives only for that body."""
    dst: Temp
    name: str
    mut: bool = False


@dataclass(eq=False)
class Release(Instr):
    """The owned temporary `value` is dropped here: nobody holds it any more (a receiver or operand used once, or a value a later failing
    `?` abandons). Inserted by the `tempdrops` pass. `drop` is "drop" when the value is certainly there, "flag" when it is there on some
    paths only (a code generator needs a flag)."""
    value: Temp
    drop: str = "drop"

    def uses(self) -> List[Value]:
        return [self.value]


@dataclass(eq=False)
class Borrow(Instr):
    """`dst = &local` or `&mut local`: a reference to a slot (to a part of it, when the source borrows a field or an element).
    `path` names the part: `.f` a field, `[]` an element (any), `*` a pointer target, joined (`.items[]`); empty for the whole slot.

    A `mut` borrow may change the slot through the reference, so an analysis forgets what it knew about the slot here. That is
    sound because the borrow rules forbid reading the slot while the reference lives: facts can only be re-established after it."""
    dst: Temp
    local: Local
    mut: bool = False
    path: str = ""
    rng: List[Value] = field(default_factory=list)  # `[lo, hi]` of a borrow of a sub-range `&a[lo..hi]` (checked by the `range` `Check` before), else empty

    idx: Optional[Value] = None  # the index of a borrow of one element `&a[i]` (checked by the `index` `Check` before)

    def uses(self) -> List[Value]:
        return list(self.rng) + ([self.idx] if self.idx is not None else [])


@dataclass(eq=False)
class Clobber(Instr):
    """A write into a part of a slot (`a.f = x`, `a[i] = x`, `*p = x`): the slot's value changes without a whole-slot `Store`.
    `local` is the root of the place, or None when the root is not a local (memory reached through a call result)."""
    local: Optional[Local]
    src: Value
    elem: bool = False  # a write of one element of an array, slice or vector: no fact about the sequence's length or fields changes
    path: str = ""  # the part written, spelled as for `Borrow.path`
    idx: Optional[Value] = None  # the index of an element write (`a[i] = x`), which `path` (`[]`) does not hold

    def uses(self) -> List[Value]:
        return [self.src] + ([self.idx] if self.idx is not None else [])


@dataclass(eq=False)
class Str(Instr):
    """`dst = "text"`: a string literal (a `str_view` of static storage)."""
    dst: Temp
    text: str


@dataclass(eq=False)
class Global(Instr):
    """`dst = name`: a named item used as a value, written as in the source: a constant, a static, an enum member, a function."""
    dst: Temp
    name: str


MAKE_KINDS = ("struct", "array", "repeat", "tuple", "range", "range_inclusive", "some", "none")


@dataclass(eq=False)
class Make(Instr):
    """`dst = ` a value built from parts. `struct`: `args[k]` is the field `names[k]`, and a final name `..` is the `..base` the rest
    comes from. `array`: `args` are the elements. `repeat`: `(element, count)` of `[x; n]`. `tuple`: `args` are the members. `range` and
    `range_inclusive`: `names` lists which of `lo` / `hi` are given, `args` their values. `some`: `args` is the value an optional
    holds; `none`: an empty optional (the type of `dst` says which optional)."""
    dst: Temp
    kind: str
    args: List[Value] = field(default_factory=list)
    names: List[str] = field(default_factory=list)

    def uses(self) -> List[Value]:
        return list(self.args)


@dataclass(eq=False)
class PatTest(Instr):
    """`dst = base matches pattern`: whether a value matches a pattern that is not an optional's `Some` / `None` (those are `Test`s).
    `pattern` is the pattern as written (`Color::Red | Color::Blue`, `(0, _)`, `1..=9`, `Ok(_)`)."""
    dst: Temp
    base: Value
    pattern: str

    def uses(self) -> List[Value]:
        return [self.base]


@dataclass(eq=False)
class Project(Instr):
    """`dst = base` followed along `path`: the part of a matched value that a pattern binds. A step is `.i` (member `i` of a tuple or
    a variant's payload) or `Variant.i`; the empty path is the whole value. Only meaningful after the `PatTest` of the same pattern passed."""
    dst: Temp
    base: Value
    path: str = ""

    def uses(self) -> List[Value]:
        return [self.base]


@dataclass(eq=False)
class Barrier(Instr):
    """A speculation barrier (docs/kif-taint.md section 7). `kind` is `fence` (a statement: no later instruction runs speculatively
    before every earlier branch resolved) or `value` (`dst` is `a`, opaque to later speculation). It changes timing, not state, so the
    borrow check and the provers keep their facts across it; `elide` and the folders never remove or move it. `why` is empty for a
    `spec::` call the author wrote and names the rule and the source construct for one the compiler inserted; the code generator
    prints it as an audit comment."""
    kind: str
    dst: Optional[Temp] = None
    a: Optional[Value] = None
    why: str = ""

    def uses(self) -> List[Value]:
        return [] if self.a is None else [self.a]


@dataclass(eq=False)
class Wipe(Instr):
    """A secure wipe of the storage of `local` (or of its field `path`, `.name` segments) right here (docs/kif-secure-wipe.md rule W1, W5).
    `ty` is the type of the wiped object, from which the code generator takes `sizeof`. `volatile` asks for the target's volatile wipe.
    The object is dead afterwards: nothing may read it. The IR carries only the decision; the primitive and barrier come from the
    target at emission. Part of the IR hash, and like `Barrier` never removed, duplicated or moved by a pass. `why` is the audit text
    for the generated comment (prose: not hashed)."""
    local: Local
    path: str = ""
    ty: str = ""
    volatile: bool = False
    why: str = ""


@dataclass(eq=False)
class Opaque(Instr):
    """A value the IR does not model: a macro, a closure, a `cfg` condition.
    `what` names it (what is left: closures, macros, `cfg` conditions, the borrow of a place that is not a local); `args` are the values it was computed from. It has no effect on any slot (a mutation inside it shows up as
    a `Borrow` or `Clobber` before it). `dst` is None for a unit result."""
    dst: Optional[Temp]
    what: str
    args: List[Value] = field(default_factory=list)

    def uses(self) -> List[Value]:
        return list(self.args)


# ------------------------------------------------------------------ terminators


@dataclass(eq=False)
class Term:
    site: Optional[ast.Node] = field(default=None, repr=False, kw_only=True)

    def targets(self) -> List["Block"]:
        return []

    def uses(self) -> List[Value]:
        return []


@dataclass(eq=False)
class Jump(Term):
    target: "Block"

    def targets(self) -> List["Block"]:
        return [self.target]


@dataclass(eq=False)
class Branch(Term):
    cond: Value
    then: "Block"
    otherwise: "Block"

    def targets(self) -> List["Block"]:
        return [self.then, self.otherwise]

    def uses(self) -> List[Value]:
        return [self.cond]


@dataclass(eq=False)
class Return(Term):
    value: Optional[Value] = None

    def uses(self) -> List[Value]:
        return [self.value] if self.value is not None else []


@dataclass(eq=False)
class Unreachable(Term):
    """Control never gets here: after a call that does not return, or in a block nothing branches to."""


# ------------------------------------------------------------------ containers


@dataclass(eq=False)
class Block:
    id: int
    label: str = ""
    instrs: List[Instr] = field(default_factory=list)
    term: Optional[Term] = None

    @property
    def name(self) -> str:
        return f"b{self.id}" + (f".{self.label}" if self.label else "")


@dataclass(eq=False)
class Function:
    name: str
    params: List[Local]
    ret: Optional[str]
    locals: List[Local] = field(default_factory=list)
    blocks: List[Block] = field(default_factory=list)
    attrs: List[str] = field(default_factory=list)
    node: Optional[ast.FnDecl] = field(default=None, repr=False)
    elided_at: List[Tuple[Optional["Instr"], "Check"]] = field(default_factory=list)  # (the instruction that followed it, a removed check): where the `elide` pass took it out
    require_proved: Optional[frozenset] = None  # `#[require_proved]` in scope: the check kinds that must be proved (None: no requirement)
    taint_sinks: frozenset = frozenset(("div", "rem", "shift"))  # the target's `taint.sinks`, copied from the checker's decision (not serialized)
    elided: List["Check"] = field(default_factory=list)  # checks the `elide` pass removed (`proof` says why; None: the callee's own guard covers it)

    @property
    def entry(self) -> Block:
        return self.blocks[0]

    @property
    def ret_type(self) -> Type:
        """The result type; unit when the function returns nothing."""
        return parse_type(self.ret) if self.ret is not None else parse_type("()")

    def instrs(self) -> Iterator[Tuple[Block, Instr]]:
        """Every instruction with its block, in block order."""
        for b in self.blocks:
            for i in b.instrs:
                yield b, i

    def checks(self) -> Iterator[Tuple[Block, Check]]:
        for b in self.blocks:
            for i in b.instrs:
                if isinstance(i, Check):
                    yield b, i


@dataclass(eq=False)
class Module:
    functions: List[Function] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (function, why the builder could not handle it)
    resolver: Optional[object] = None  # `resolve(call)` of the module the functions came from (kiflang/passes.py hands it to passes)


# ------------------------------------------------------------------ control-flow graph


class CFG:
    """Successors, predecessors, reverse post-order and dominators of one function's blocks (unreachable blocks are left out)."""

    def __init__(self, fn: Function) -> None:
        self.fn = fn
        self.succ: Dict[Block, List[Block]] = {b: (b.term.targets() if b.term else []) for b in fn.blocks}
        self.pred: Dict[Block, List[Block]] = {b: [] for b in fn.blocks}
        for b, ss in self.succ.items():
            for s in ss:
                self.pred[s].append(b)
        self.rpo = self._rpo()
        self.index = {b: i for i, b in enumerate(self.rpo)}
        self.idom = self._dominators()

    def _rpo(self) -> List[Block]:
        seen: Set[Block] = set()
        order: List[Block] = []
        stack = [(self.fn.entry, iter(self.succ[self.fn.entry]))]
        seen.add(self.fn.entry)
        while stack:
            b, it = stack[-1]
            for s in it:
                if s not in seen:
                    seen.add(s)
                    stack.append((s, iter(self.succ[s])))
                    break
            else:
                order.append(b)
                stack.pop()
        order.reverse()
        return order

    def _dominators(self) -> Dict[Block, Block]:
        """Immediate dominators (Cooper, Harvey, Kennedy); the entry dominates itself."""
        entry = self.fn.entry
        idom: Dict[Block, Block] = {entry: entry}

        def meet(a: Block, b: Block) -> Block:
            while a is not b:
                while self.index[a] > self.index[b]:
                    a = idom[a]
                while self.index[b] > self.index[a]:
                    b = idom[b]
            return a

        changed = True
        while changed:
            changed = False
            for b in self.rpo[1:]:
                done = [p for p in self.pred[b] if p in idom]
                if not done:
                    continue
                new = done[0]
                for p in done[1:]:
                    new = meet(p, new)
                if idom.get(b) is not new:
                    idom[b] = new
                    changed = True
        return idom

    def reachable(self, b: Block) -> bool:
        return b in self.index

    def dominates(self, a: Block, b: Block) -> bool:
        """Whether every path from the entry to `b` passes through `a` (a block dominates itself)."""
        if b not in self.idom or a not in self.idom:
            return False
        while True:
            if a is b:
                return True
            if b is self.fn.entry:
                return False
            b = self.idom[b]

    def loop_headers(self) -> Set[Block]:
        """Targets of back edges: an edge `b -> h` where `h` dominates `b`."""
        return {s for b in self.rpo for s in self.succ[b] if self.dominates(s, b)}


# ------------------------------------------------------------------ printing and verification


def dump(fn: Function) -> str:
    """A readable listing, e.g. `t3 = load %i`, `check index t3, 4 [proved: constant]`, `branch t5, b2.then, b3.else`."""
    out = [f"fn {fn.name}({', '.join(f'{p}: {p.ty}' for p in fn.params)})" + (f" -> {fn.ret}" if fn.ret is not None else "") + " {"]
    for b in fn.blocks:
        out.append(f"  {b.name}:")
        for i in b.instrs:
            out.append("    " + _instr(i))
        out.append("    " + (_term(b.term) if b.term else "<no terminator>"))
    for c in fn.elided:
        out.append("  // elided: " + _instr(c))
    out.append("}")
    return "\n".join(out)


def dump_module(m: Module) -> str:
    return "\n\n".join(dump(f) for f in m.functions) + ("\n" if m.functions else "")


def _instr(i: Instr) -> str:
    d = f"{i.dst} = " if getattr(i, "dst", None) is not None else ""
    if isinstance(i, Move):
        return f"{d}move {i.local}"
    if isinstance(i, Load):
        return f"{d}load {i.local}"
    if isinstance(i, Store):
        return f"store {i.local}, {i.src}" + ("  // init" if i.init else "") + (f"  // drops the old value ({i.drop_old})" if i.drop_old else "")
    if isinstance(i, Live):
        return f"live {i.local}"
    if isinstance(i, Cell):
        return f"{d}cell {'mut ' if i.mut else ''}{i.name}"
    if isinstance(i, Release):
        return f"release {i.value}" + (f"  // drop ({i.drop})" if i.drop != "drop" else "")
    if isinstance(i, Dead):
        return f"dead {i.local}" + (f"  // drop ({i.drop})" if i.drop else "")
    if isinstance(i, Unary):
        return f"{d}{i.op} {i.a}"
    if isinstance(i, Binary):
        return f"{d}{i.a} {i.op} {i.b}"
    if isinstance(i, Cast):
        return f"{d}cast {i.a} as {i.ty}"
    if isinstance(i, Call):
        recv = f"{i.recv}." if i.recv is not None else ""
        return (f"{d}call {recv}{i.callee}({', '.join(str(a) for a in i.args)})"
                + (f"  // #{i.id} {i.kind}" + (f" -> {i.decl}" if i.decl and i.decl != i.callee else "") + (" (unguarded)" if i.unguarded else "") if i.id else ""))
    if isinstance(i, Len):
        return f"{d}len {i.base}"
    if isinstance(i, Test):
        return f"{d}test {i.kind} {i.base}"
    if isinstance(i, Check):
        return f"check {i.kind} {', '.join(str(a) for a in i.args)}" + (f" : {i.ty}" if i.ty else "") + (f"  [proved: {i.proof}]" if i.proof else "") + (f"  // guards call #{i.call}" if i.call else "")
    if isinstance(i, Access):
        return f"{d}access {i.kind} {i.base}" + (f".{i.name}" if i.name else "") + (", " + ", ".join(str(a) for a in i.args) if i.args else "")
    if isinstance(i, Borrow):
        return f"{d}borrow {'mut ' if i.mut else ''}{i.local}{i.path}" + (f" [{i.rng[0]}..{i.rng[1]}]" if i.rng else "") + (f" @ {i.idx}" if i.idx is not None else "")
    if isinstance(i, Clobber):
        return f"clobber {(str(i.local) + i.path) if i.local is not None else '(memory)'}{'' if i.idx is None else f' @ {i.idx}'}, {i.src}"
    if isinstance(i, Str):
        return f"{d}str {i.text!r}"
    if isinstance(i, Global):
        return f"{d}global {i.name}"
    if isinstance(i, Make):
        if i.kind == "struct":
            return f"{d}make struct " + ", ".join(f"{n}: {a}" if n != ".." else f"..{a}" for n, a in zip(i.names, i.args))
        return f"{d}make {i.kind} " + ", ".join(str(a) for a in i.args) + (f"  // {', '.join(i.names)}" if i.names else "")
    if isinstance(i, PatTest):
        return f"{d}pattern {i.base} ~ {i.pattern}"
    if isinstance(i, Project):
        return f"{d}project {i.base}{('.' + i.path.lstrip('.')) if i.path else ''}"
    if isinstance(i, Barrier):
        return f"{d}barrier {i.kind}" + (f" {i.a}" if i.a is not None else "") + (f"  // implicit: {i.why}" if i.why else "")
    if isinstance(i, Wipe):
        return f"wipe {i.local}{i.path}" + (" volatile" if i.volatile else "") + (f"  // {i.why}" if i.why else "")
    if isinstance(i, Opaque):
        return f"{d}opaque {i.what}" + (f"({', '.join(str(a) for a in i.args)})" if i.args else "")
    return repr(i)


def _term(t: Term) -> str:
    if isinstance(t, Jump):
        return f"jump {t.target.name}"
    if isinstance(t, Branch):
        return f"branch {t.cond}, {t.then.name}, {t.otherwise.name}"
    if isinstance(t, Return):
        return "return" + (f" {t.value}" if t.value is not None else "")
    return "unreachable"


def verify(fn: Function) -> List[str]:
    """Structural problems of `fn` (empty when it is well formed): every block ends in a terminator that names blocks of this
    function, every temp has one definition that dominates its uses, and every `Check` has the operand count of its kind."""
    errs: List[str] = []
    blocks = set(fn.blocks)
    for b in fn.blocks:
        if b.term is None:
            errs.append(f"{b.name}: no terminator")
            continue
        for s in b.term.targets():
            if s not in blocks:
                errs.append(f"{b.name}: jumps to a block that is not in the function")
    if errs:
        return errs
    cfg = CFG(fn)
    where: Dict[Temp, Tuple[Block, int]] = {}
    for b in fn.blocks:
        for n, i in enumerate(b.instrs):
            t = i.defs()
            if t is not None:
                if t in where:
                    errs.append(f"{b.name}: {t} is defined twice")
                where[t] = (b, n)
            if isinstance(i, Check) and (i.kind not in CHECK_KINDS or len(i.args) != CHECK_KINDS[i.kind]):
                errs.append(f"{b.name}: bad check {i.kind} with {len(i.args)} operand(s)")
            if isinstance(i, Access) and i.kind not in ACCESS_KINDS:
                errs.append(f"{b.name}: unknown access kind {i.kind}")
    for b in fn.blocks:
        if not cfg.reachable(b):
            continue
        uses = [(n, v) for n, i in enumerate(b.instrs) for v in i.uses()] + [(len(b.instrs), v) for v in b.term.uses()]
        for n, v in uses:
            if not isinstance(v, Temp):
                continue
            if v not in where:
                errs.append(f"{b.name}: {v} is used but never defined")
                continue
            db, dn = where[v]
            if db is b and dn >= n or db is not b and not cfg.dominates(db, b):
                errs.append(f"{b.name}: {v} is used where its definition does not dominate")
    return errs


# ------------------------------------------------------------------ building from the checked AST

_SEQ = ("vector", "str", "str_view", "[]", "[;]")
_BIN_SHORT = ("&&", "||")


def _strip(t: Optional[Ty]) -> Optional[Ty]:
    while t is not None and t.name == "&":
        t = t.args[0]
    return t


def type_text(t) -> str:
    """A written type as text: `u32`, `&mut [u32]`, `optional<u32>`, `[u8; 4]`."""
    if t is None:
        return "()"
    if isinstance(t, ast.RefType):
        return ("&mut " if t.mut else "&") + type_text(t.inner)
    if isinstance(t, ast.SliceType):
        return f"[{type_text(t.elem)}]"
    if isinstance(t, ast.ArrayType):
        return f"[{type_text(t.elem)}; {t.length.value if isinstance(t.length, ast.Lit) else '_'}]"
    if isinstance(t, ast.PathType):
        last = t.segments[-1]
        gen = [type_text(a) if not isinstance(a, ast.Lifetime) else a.text for a in last.args if not isinstance(a, ast.ConstArg)]
        return "::".join(x.name for x in t.segments[:-1] + [last]) + (f"<{', '.join(gen)}>" if gen else "")
    if isinstance(t, ast.TupleType):
        return f"({', '.join(type_text(x) for x in t.elems)}{',' if len(t.elems) == 1 else ''})"
    return "?"


def _array_length(t) -> Optional[int]:
    """The constant length written in an array type, if it is an integer literal."""
    while isinstance(t, ast.RefType):
        t = t.inner
    if isinstance(t, ast.ArrayType) and isinstance(t.length, ast.Lit) and t.length.kind == "int":
        return int(t.length.value)
    return None


_WRAPPERS = ("optional", "result", "expected", "checked_value", "slot")


def owns_type(t: Type, drops) -> bool:
    """Whether a value of type `t` has drop glue: a vector, string or heap `ptr<T>`, a type in `drops`, or an array, tuple or wrapper holding
    one. References, raw `c_ptr`s and slices own nothing."""
    if t.kind in ("array", "tuple"):
        return any(owns_type(a, drops) for a in t.args)
    if t.kind == "named":
        return t.name in ("vector", "str", "ptr") or t.name in drops or t.name in _WRAPPERS and any(owns_type(a, drops) for a in t.args)
    return False


def plain_type(t: Type) -> bool:
    """Whether `t` is plain data as far as the IR can tell from the text: nothing owned, no reference, slice or function (docs/kif-secure-wipe.md W3).
    The checker has already rejected the explicit `#[secure_wipe]` on anything else; this decides which locals a `#[secure_wipe]` function covers."""
    if t.kind == "scalar":
        return True
    if t.kind in ("array", "tuple"):
        return all(plain_type(a) for a in t.args)
    return False


def secure_types(items) -> frozenset:
    """The names of the `#[secure_wipe]` structs of a module."""
    return frozenset(it.name.text for it in items if isinstance(it, ast.StructDecl) and any(a.name.text == "secure_wipe" for a in it.attrs))


def drop_types(items) -> frozenset:
    """The names of the structs of a module with drop glue: they have a `drop` method, or a field that owns something (to a fixpoint)."""
    structs = {it.name.text: it for it in items if isinstance(it, ast.StructDecl)}
    drops = {(it.target.text if isinstance(it.target, ast.PathType) else "") for it in items
             if isinstance(it, ast.ImplDecl) and it.iface is None and any(isinstance(f, ast.FnDecl) and f.name.text == "drop" for f in it.items)}
    drops &= set(structs)
    drops |= secure_types(items)  # a secure struct has a generated drop
    while True:
        more = {n for n, st in structs.items() if n not in drops and any(owns_type(parse_type(type_text(f.ty)), drops) for f in st.fields)}
        if not more:
            return frozenset(drops)
        drops |= more


def pat_text(p) -> str:
    """A pattern as written, for `PatTest.pattern` and listings."""
    if isinstance(p, ast.WildPat):
        return "_"
    if isinstance(p, ast.LitPat):
        return repr(p.lit.value) if p.lit.kind == "str" else str(p.lit.value).lower() if p.lit.kind == "bool" else str(p.lit.value)
    if isinstance(p, ast.PathPat):
        return p.path.text
    if isinstance(p, ast.RangePat):
        return f"{pat_text(ast.LitPat(p.range, p.lo))}..={pat_text(ast.LitPat(p.range, p.hi))}"
    if isinstance(p, ast.OrPat):
        return " | ".join(pat_text(a) for a in p.alts)
    if isinstance(p, ast.BindPat):
        return ("mut " if p.mut else "") + p.name.text + (f" @ {pat_text(p.sub)}" if p.sub is not None else "")
    if isinstance(p, ast.TuplePat):
        return "(" + ", ".join(pat_text(x) for x in p.elems) + ")"
    if isinstance(p, ast.VariantPat):
        return p.path.text + "(" + ", ".join(pat_text(x) for x in p.args) + ")"
    return "?"


class _Builder:
    def __init__(self, fn: ast.FnDecl, resolve=None, entry: bool = True, drops=frozenset(), owner: Optional[str] = None, cells=None) -> None:
        self.cells = cells or {}  # name -> StaticDecl of the module's `#[tls]` / `#[per_cpu]` statics
        self.decl = fn
        self.resolve = resolve
        self.entry = entry
        self.drops = drops  # names of the types with drop glue
        self.owner = owner  # the type an `impl` function belongs to, for `Self::f`
        self.ncalls = 0
        self.last_range: List[Value] = []  # the bounds of the last range-indexed place
        self.last_index: Optional[Value] = None  # the index the last indexed place used, for `Clobber.idx`
        self.fn = Function(fn.name.text, [], type_text(fn.ret) if fn.ret is not None else None, attrs=[a.name.text for a in fn.attrs], node=fn)
        self.nt = 0
        self.nl = 0
        self.nb = 0
        self.scopes: List[Dict[str, Local]] = [{}]
        self.declared: List[List[Local]] = [[]]  # per scope, every local in declaration order (a shadowed name still dies at scope end)
        self.loops: List[Tuple[Block, Block, int]] = []  # (continue target, break target, scope depth outside the loop)
        self.cur: Optional[Block] = None

    # -- plumbing
    def block(self, label: str = "") -> Block:
        b = Block(self.nb, label)
        self.nb += 1
        self.fn.blocks.append(b)
        return b

    def temp(self, ty=None) -> Temp:
        self.nt += 1
        return Temp(self.nt, ty)

    def tys(self, e) -> Optional[str]:
        return ty_text(getattr(e, "ty", None))

    def local(self, name: str, ty: Optional[str], mut: bool = True, param: bool = False, length: Optional[int] = None) -> Local:
        self.nl += 1
        loc = Local(self.nl, name, ty or "?", mut, param, length, owns_type(parse_type(ty), self.drops))
        self.fn.locals.append(loc)
        return loc

    def emit(self, i: Instr) -> None:
        if self.cur is None:
            self.cur = self.block("dead")  # code after `return` / `break`: kept so the AST walk stays simple, nothing reaches it
        self.cur.instrs.append(i)

    def end(self, t: Term) -> None:
        if self.cur is not None and self.cur.term is None:
            self.cur.term = t
        self.cur = None

    def start(self, b: Block) -> None:
        self.cur = b

    def push_scope(self) -> None:
        self.scopes.append({})
        self.declared.append([])

    def unwind(self, depth: int) -> None:
        """Kill the locals of the scopes above `depth`, innermost and last-declared first, without leaving them."""
        if self.cur is None:
            return
        for sc in reversed(self.declared[depth:]):
            for loc in reversed(sc):
                self.emit(Dead(loc))

    def pop_scope(self) -> None:
        self.unwind(len(self.scopes) - 1)
        self.scopes.pop()
        self.declared.pop()

    def declare(self, loc: Local, src: Optional[Value], site) -> None:
        """A new binding: it comes into scope, and holds `src` when there is one."""
        self.scopes[-1][loc.name] = loc
        self.declared[-1].append(loc)
        self.emit(Live(loc, site=site))
        if src is not None:
            self.emit(Store(loc, src, True, site=site))

    def lookup(self, name: str) -> Optional[Local]:
        return next((s[name] for s in reversed(self.scopes) if name in s), None)

    def no(self, what: str, node) -> Unsupported:
        return Unsupported(what, node)

    # -- statements
    def run(self) -> Function:
        f = self.decl
        if f.body is None:
            raise self.no("no body", f)
        if f.is_gen:
            raise self.no("gen functions run at generation time and are not built", f)
        self.cur = self.block("entry")
        for p in f.params:
            if p.receiver:
                loc = self.local("self", {"ref": "&Self", "mut": "&mut Self"}.get(p.receiver, self.owner or "Self"), False, True)
            else:
                loc = self.local(p.name.text, type_text(p.ty), False, True, _array_length(p.ty))
                loc.taint = getattr(f, "taint_all", False) or any(a.name.text == "tainted" for a in getattr(p, "attrs", []))
            loc.wipe = any(a.name.text == "secure_wipe" for a in getattr(p, "attrs", []))
            loc.wipe_volatile = loc.wipe and wipe_mod.is_volatile(p.attrs)
            self.fn.params.append(loc)
            self.scopes[-1][loc.name] = loc
            if loc.owns:
                self.declared[0].append(loc)  # a by-value parameter that owns something is dropped like a local, unless it was moved
        for r in facts.requirements(f) if self.entry else []:
            self.require(r, f"assumed: #[{r.fact}] on parameter {r.param.name.text!r}", *self.entry_operands(r))
        v = self.block_value(f.body)
        self.ensure_result(v)
        self.unwind(0)
        self.end(Return(v))
        for b in self.fn.blocks:
            if b.term is None:
                b.term = Unreachable()
            for i in b.instrs:
                t = i.defs()
                if t is not None and t.ty:
                    t.owns = owns_type(parse_type(t.ty), self.drops)
        return self.fn

    def path(self, name: str, node) -> ast.PathExpr:
        return ast.PathExpr(node.range, [ast.PathSeg(node.range, name)])

    def entry_operands(self, r) -> tuple:
        """(argument expression, its value, the `lt_len` target expression and value) of a requirement on this function's own parameter."""
        e = self.path(r.param.name.text, r.param)
        o = self.path(r.other.name.text, r.other) if r.other is not None else None
        return e, self.expr(e), o, self.expr(o) if o is not None else None

    def require(self, r, proof: Optional[str], arg, val, other, other_val, site=None, call: int = 0) -> None:
        """The `Check` of one parameter fact. Written with `proof` set it is an assumption (the entry of the function, learned by the
        provers); with `site` it is a requirement of a call, which the C backend insists the provers discharge."""
        site = site if site is not None else r.param
        if r.kind != "index":
            self.emit(Check(r.kind, [val], proof=proof, site=site, tag=r.tag, call=call))
            return
        n = self.length_of(self.whole(other if other is not None else arg), other_val if other is not None else val, site)
        i = val if r.other is not None else Const(r.n)
        self.emit(Check("index", [i, n], proof=proof, site=site, tag=r.tag, call=call))

    @staticmethod
    def whole(e):
        """The array behind `&a`, `&a[..]` or `a.as_slice()`: the same elements, so the same known length."""
        while True:
            if isinstance(e, ast.Unary) and e.op in ("&", "&mut"):
                e = e.operand
            elif isinstance(e, ast.MethodCall) and e.name.text in ("as_slice", "as_mut_slice") and not e.args:
                e = e.receiver
            elif isinstance(e, ast.Index) and isinstance(e.index, ast.RangeExpr) and e.index.lo is None and e.index.hi is None:
                e = e.expr
            else:
                return e

    def callee(self, e) -> Tuple[Optional[ast.FnDecl], str, str]:
        """`(declaration, qualified name, kind)` of a call; the declaration is None when the call is not resolved."""
        decl = self.resolve(e, self.owner) if self.resolve is not None else None
        if decl is not None and any(p.receiver for p in decl.params) != isinstance(e, ast.MethodCall):
            decl = None
        if decl is None:
            return None, "", "method" if isinstance(e, ast.MethodCall) else "unresolved"
        qualify = getattr(self.resolve, "qualify", None)
        name = qualify(decl) if qualify is not None else decl.name.text
        return decl, name, "method" if isinstance(e, ast.MethodCall) else "assoc" if "::" in name else "free"

    @staticmethod
    def flows(decl) -> Optional[List[int]]:
        """What `Call.flows` says for a resolved callee: the operands its result's lifetimes name (the checker's elision rules)."""
        if decl is None:
            return None
        from .bodies import _flow_sources

        return _flow_sources(decl)

    @staticmethod
    def ensures_of(decl) -> Optional[Tuple[str, int]]:
        """`(fact, n)` of the callee's `#[ensures(..)]`; `n` is the least element count for the sequence facts."""
        for a in getattr(decl, "attrs", None) or ():
            if a.name.text == "ensures" and a.args and len(a.args) == 1 and a.args[0].name:
                x = a.args[0]
                if x.name == "len_ge":
                    return ("len_ge", x.args[0].value.value) if x.args and x.args[0].value is not None else None
                return ("len_ge", 1) if x.name == "non_empty" else (x.name, 0)
        return None

    @staticmethod
    def preserves_of(decl) -> Optional[List[int]]:
        """The operands of a call that `#[preserves]` keeps the facts of (the same numbering as `Call.flows`); None without the attribute."""
        if decl is None:
            return None
        for a in getattr(decl, "attrs", None) or ():
            if a.name.text == "preserves":
                plain = [p for p in decl.params if not p.receiver]
                out: List[int] = []
                names = [x.name for x in a.args or ()]
                if any(p.receiver == "mut" for p in decl.params) and (not names or "self" in names):
                    out.append(-1)
                for k, p in enumerate(plain):
                    if isinstance(p.ty, ast.RefType) and p.ty.mut and (not names or p.name.text in names):
                        out.append(k)
                return out
        return None

    def ensure_result(self, v: Optional[Value]) -> None:
        """Before a `return`: the run-time check of `#[ensures(..)]`, so that callers may rely on it. The provers remove it where they can show it holds."""
        want = self.ensures_of(self.decl)
        if want is None or v is None or isinstance(v, Const):
            return
        fact, n = want
        tag = f"ensures:{fact}" if n == 0 or fact != "len_ge" else f"ensures:len_ge({n})"
        if fact in ("some", "non_null"):
            self.emit(Check(fact, [v], tag=tag, site=self.decl))
        else:
            ln = self.temp("usize")
            self.emit(Len(ln, v, site=self.decl))
            self.emit(Check("index", [Const(n - 1), ln], tag=tag, site=self.decl))

    def call_requirements(self, e, args: List[Value], arg_exprs: List, decl: Optional[ast.FnDecl], call: int) -> None:
        """Before a call to a known function: the `Check` of each fact the callee's parameters require."""
        if decl is None or not getattr(self.resolve, "has_twin", lambda d: True)(decl):
            return
        ps = [p for p in decl.params if not p.receiver]
        pos = {p.name.text: k for k, p in enumerate(ps)}
        for r in facts.requirements(decl):
            k = pos[r.param.name.text]
            o = pos[r.other.name.text] if r.other is not None else None
            self.require(r, None, arg_exprs[k], args[k], arg_exprs[o] if o is not None else None, args[o] if o is not None else None, e, call)

    def block_value(self, b: ast.Block) -> Optional[Value]:
        self.push_scope()
        n = len(self.scopes)
        try:
            for s in b.stmts:
                self.stmt(s)
            v = self.expr(b.tail) if b.tail is not None else None
            self.unwind(n - 1)
            return v
        finally:
            del self.scopes[n - 1:]
            del self.declared[n - 1:]

    def stmt(self, s) -> None:
        if isinstance(s, ast.Let):
            loc = self.local(s.name.text, type_text(s.ty) if s.ty is not None else self.tys(s.init), s.mut, False, _array_length(s.ty))
            names = [a.name.text for a in s.attrs]
            loc.wipe = "secure_wipe" in names or ("secure_wipe" in self.fn.attrs and "wipe_exempt" not in names and plain_type(loc.type))
            loc.wipe_volatile = loc.wipe and wipe_mod.is_volatile(s.attrs if "secure_wipe" in names else self.decl.attrs)
            self.declare(loc, self.expr(s.init) if s.init is not None else None, s)
        elif isinstance(s, ast.LetPat):
            if s.init is not None:
                self.bind(s.pattern, self.expr(s.init), s)
        elif isinstance(s, ast.ExprStmt):
            self.expr(s.expr)
        elif not isinstance(s, ast.ItemStmt):  # a nested item has its own body and cannot see these locals
            raise self.no(f"statement {type(s).__name__}", s)

    def opaque(self, what: str, args: List[Optional[Value]], site, ty: Optional[str] = None, unit: bool = False) -> Optional[Temp]:
        t = None if unit else self.temp(ty)
        self.emit(Opaque(t, what, [a for a in args if a is not None], site=site))
        return t

    def bind(self, p, value: Optional[Value], site, path: str = "", ty: Optional[str] = None) -> None:
        """Declare a local for every name a pattern binds and store the part of the matched value it names (a `Project` along `path`)."""
        if isinstance(p, ast.BindPat):
            loc = self.local(p.name.text, ty or "?", p.mut)
            part = value
            if value is not None and path:
                part = self.temp(ty)
                self.emit(Project(part, value, path, site=site))
            self.declare(loc, part, site)
            if p.sub is not None:
                self.bind(p.sub, value, site, path, ty)
        elif isinstance(p, ast.OrPat):
            self.bind(p.alts[0], value, site, path)  # every alternative binds the same names
        elif isinstance(p, ast.TuplePat):
            for n, x in enumerate(p.elems):
                self.bind(x, value, site, f"{path}.{n}")
        elif isinstance(p, ast.VariantPat):
            vty = getattr(value, "ty", None) or ""
            inner = vty[len("optional<"):-1] if p.path.text == "Some" and vty.startswith("optional<") and vty.endswith(">") else None
            for n, x in enumerate(p.args):
                self.bind(x, value, site, f"{path}{p.path.text}.{n}", inner)

    # -- places: what can be assigned or borrowed
    def root_local(self, e) -> Optional[Local]:
        """The local a place expression (`a`, `a.f`, `a[i]`, `*a`) is rooted in, or None."""
        while True:
            if isinstance(e, ast.PathExpr):
                return self.lookup(e.segments[0].name) if len(e.segments) == 1 else None
            if isinstance(e, (ast.FieldAccess, ast.Index)):
                e = e.expr
            elif isinstance(e, ast.Unary) and e.op == "*":
                e = e.operand
            else:
                return None

    def is_place(self, e) -> bool:
        while isinstance(e, (ast.FieldAccess, ast.Index)) or isinstance(e, ast.Unary) and e.op == "*":
            e = e.expr if isinstance(e, (ast.FieldAccess, ast.Index)) else e.operand
        return isinstance(e, ast.PathExpr)

    @staticmethod
    def place_path(e) -> str:
        """What part of its root local a place expression names: `.f` for a field, `[]` for an element, `*` for a pointer target."""
        parts: List[str] = []
        while True:
            if isinstance(e, ast.FieldAccess):
                parts.append("." + e.name.text)
                e = e.expr
            elif isinstance(e, ast.Index):
                parts.append("[]")
                e = e.expr
            elif isinstance(e, ast.Unary) and e.op == "*":
                parts.append("*")
                e = e.operand
            else:
                return "".join(reversed(parts))

    @staticmethod
    def elem_write(target) -> bool:
        if not isinstance(target, ast.Index):
            return False
        bt = _strip(getattr(target.expr, "ty", None))
        return bt is not None and bt.name in ("vector", "[]", "[;]")

    def place(self, e) -> Optional[Local]:
        """Evaluate the parts of a place that run code (indices and pointer derefs, with their checks); return its root local."""
        if isinstance(e, ast.Index):
            _, kind, args = self.index_checked(e)
            self.last_index = args[0] if kind == "index" else None
            return self.root_local(e.expr)
        if isinstance(e, ast.FieldAccess):
            return self.place(e.expr)
        if isinstance(e, ast.Unary) and e.op == "*":
            self.deref_checked(e)
            return self.root_local(e.operand)
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1 and self.lookup(e.segments[0].name) is not None:
            return self.lookup(e.segments[0].name)
        self.expr(e)
        return None

    # -- expressions
    def expr(self, e) -> Optional[Value]:
        m = getattr(self, "e_" + type(e).__name__, None)
        if m is None:
            raise self.no(f"expression {type(e).__name__}", e)
        return m(e)

    def e_Lit(self, e: ast.Lit) -> Value:
        if e.kind == "str":
            t = self.temp(self.tys(e))
            self.emit(Str(t, e.value, site=e))
            return t
        if e.kind not in ("int", "bool"):
            return self.opaque("lit:" + e.kind, [], e, self.tys(e))
        return Const(e.value, self.tys(e))

    def e_PathExpr(self, e: ast.PathExpr) -> Value:
        loc = self.lookup(e.segments[0].name) if len(e.segments) == 1 else None
        if loc is None:  # a constant, a static, an enum member or a function used as a value
            t = self.temp(self.tys(e))
            self.emit(Global(t, e.text, site=e))
            return t
        if loc.ty == "?" and self.tys(e):
            loc.ty = self.tys(e)
            loc.owns = owns_type(parse_type(loc.ty), self.drops)
        t = self.temp(loc.ty)
        self.emit((Move if getattr(e, "moved", False) else Load)(t, loc, site=e))  # `moved` is set by the checker on a use that hands the value on
        return t

    def e_Unary(self, e: ast.Unary) -> Value:
        if e.op in ("&", "&mut"):
            if not self.is_place(e.operand):  # a borrow of a temporary: nothing that outlives the expression can change
                return self.expr(e.operand)
            self.last_range = []
            root = self.place(e.operand)
            if root is None:  # a place that is not a local (a static): not tracked
                return self.opaque("borrow", [], e, self.tys(e))
            t = self.temp(self.tys(e))
            rng = self.last_range if isinstance(e.operand, ast.Index) and isinstance(e.operand.index, ast.RangeExpr) else []
            one = isinstance(e.operand, ast.Index) and not isinstance(e.operand.index, ast.RangeExpr)
            self.emit(Borrow(t, root, e.op == "&mut", self.place_path(e.operand), rng=list(rng), idx=self.last_index if one else None, site=e))
            return t
        if e.op == "*":
            return self.deref_checked(e, access=True)
        if e.op not in ("-", "!", "~"):
            raise self.no(f"unary {e.op}", e)
        t = self.temp(self.tys(e))
        self.emit(Unary(t, e.op, self.expr(e.operand), site=e))
        return t

    def e_Binary(self, e: ast.Binary) -> Value:
        if e.op in _BIN_SHORT:
            return self.short_circuit(e)
        a = self.expr(e.left)
        b = self.expr(e.right)
        t = self.temp(self.tys(e))
        self.binary(t, e.op, a, b, e)
        return t

    def binary(self, dst: Temp, op: str, a: Value, b: Value, site) -> None:
        """A `Binary`; division and remainder get their `div` check first (a zero divisor, or `MIN / -1`, panics), a shift its `shift`
        check (the amount must be below the bit width)."""
        if op in ("/", "%"):
            self.emit(Check("div", [a, b], site=site, ty=dst.ty))
        elif op in ("<<", ">>"):
            self.emit(Check("shift", [b], site=site, ty=dst.ty))
        self.emit(Binary(dst, op, a, b, site=site))

    def branch_on(self, cond, then: Block, other: Block, site) -> None:
        """Ends the current block with a branch on `cond`. `&&`, `||` and `!` in a condition become branches of their own, so a
        prover sees each comparison on the path it guards (a value kept in a slot would hide them)."""
        if isinstance(cond, ast.Binary) and cond.op in _BIN_SHORT:
            mid = self.block("and" if cond.op == "&&" else "or")
            if cond.op == "&&":
                self.branch_on(cond.left, mid, other, site)
            else:
                self.branch_on(cond.left, then, mid, site)
            self.start(mid)
            return self.branch_on(cond.right, then, other, site)
        if isinstance(cond, ast.Unary) and cond.op == "!":
            return self.branch_on(cond.operand, other, then, site)
        self.end(Branch(self.expr(cond), then, other, site=site))

    def short_circuit(self, e: ast.Binary) -> Value:
        res = self.local("sc", "bool")
        a = self.expr(e.left)
        self.emit(Store(res, a, site=e))
        rhs, done = self.block("rhs"), self.block("join")
        self.end(Branch(a, rhs, done, site=e) if e.op == "&&" else Branch(a, done, rhs, site=e))
        self.start(rhs)
        self.emit(Store(res, self.expr(e.right), site=e))
        self.end(Jump(done))
        self.start(done)
        t = self.temp("bool")
        self.emit(Load(t, res, site=e))
        return t

    def e_Cast(self, e: ast.Cast) -> Value:
        t = self.temp(self.tys(e))
        self.emit(Cast(t, self.expr(e.expr), self.tys(e) or "?", site=e))
        return t

    def e_Assign(self, e: ast.Assign) -> None:
        loc = self.lookup(e.target.segments[0].name) if isinstance(e.target, ast.PathExpr) and len(e.target.segments) == 1 else None
        if loc is not None:
            rhs = self.expr(e.value)
            if e.op != "=":
                cur = self.temp(loc.ty)
                self.emit(Load(cur, loc, site=e))
                t = self.temp(loc.ty)
                self.binary(t, e.op[:-1], cur, rhs, e)
                rhs = t
            self.emit(Store(loc, rhs, site=e))
            return
        if not self.is_place(e.target):
            raise self.no("assignment to something that is not a place", e)
        rhs = self.expr(e.value)
        self.last_index = None
        if e.op != "=":  # the read does the place's checks, so the write needs none of its own
            self.last_index = None
            cur = self.expr(e.target)
            t = self.temp(self.tys(e.target))
            self.binary(t, e.op[:-1], cur, rhs, e)
            self.emit(Clobber(self.root_local(e.target), t, elem=self.elem_write(e.target), path=self.place_path(e.target), idx=self.last_index, site=e))
        else:
            root = self.place(e.target)
            self.emit(Clobber(root, rhs, elem=self.elem_write(e.target), path=self.place_path(e.target), idx=self.last_index, site=e))

    def e_Return(self, e: ast.Return) -> None:
        v = self.expr(e.value) if e.value is not None else None
        self.ensure_result(v)
        self.unwind(0)
        self.end(Return(v, site=e))

    def e_Break(self, e) -> None:
        if not self.loops:
            raise self.no("break outside a loop", e)
        self.unwind(self.loops[-1][2])
        self.end(Jump(self.loops[-1][1], site=e))

    def e_Continue(self, e) -> None:
        if not self.loops:
            raise self.no("continue outside a loop", e)
        self.unwind(self.loops[-1][2])
        self.end(Jump(self.loops[-1][0], site=e))

    def e_Block(self, e: ast.Block) -> Optional[Value]:
        return self.block_value(e)

    def e_UnsafeBlock(self, e) -> Optional[Value]:
        return self.block_value(e.body)

    def e_If(self, e: ast.If) -> Optional[Value]:
        res = self.local("if", self.tys(e)) if e.otherwise is not None and getattr(e, "ty", None) not in (None, T.UNIT) else None
        then, join = self.block("then"), self.block("join")
        other = self.block("else") if e.otherwise is not None else join
        self.branch_on(e.cond, then, other, e)
        self.start(then)
        v = self.block_value(e.then)
        if res is not None and v is not None:
            self.emit(Store(res, v, site=e))
        self.end(Jump(join))
        if e.otherwise is not None:
            self.start(other)
            v = self.expr(e.otherwise)
            if res is not None and v is not None:
                self.emit(Store(res, v, site=e))
            self.end(Jump(join))
        self.start(join)
        if res is None:
            return None
        t = self.temp(res.ty)
        self.emit(Load(t, res, site=e))
        return t

    def e_While(self, e: ast.While) -> None:
        head, body, done = self.block("while"), self.block("body"), self.block("done")
        self.end(Jump(head))
        self.start(head)
        self.branch_on(e.cond, body, done, e)
        self.start(body)
        self.loops.append((head, done, len(self.scopes)))
        self.block_value(e.body)
        self.loops.pop()
        self.end(Jump(head))
        self.start(done)

    def e_Loop(self, e: ast.Loop) -> None:
        body, done = self.block("loop"), self.block("done")
        self.end(Jump(body))
        self.start(body)
        self.loops.append((body, done, len(self.scopes)))
        self.block_value(e.body)
        self.loops.pop()
        self.end(Jump(body))
        self.start(done)

    # -- accesses that can panic: a Check followed by an unchecked Access
    def index_checked(self, e: ast.Index) -> Tuple[Value, str, List[Value]]:
        """Evaluate `base[index]` up to and including its `Check`; returns the base, the access kind and the access operands."""
        bt = _strip(getattr(e.expr, "ty", None))
        if bt is None or bt.name not in _SEQ:
            raise self.no("index of a value that is not an array, vector, slice or string", e)
        base = self.expr(e.expr)
        if isinstance(e.index, ast.RangeExpr):
            r = e.index
            lo = self.expr(r.lo) if r.lo is not None else Const(0, "usize")
            hi = self.expr(r.hi) if r.hi is not None else None
            n = self.length_of(e.expr, base, e)
            if hi is None:
                hi = n
            elif r.inclusive:
                t = self.temp("usize")
                self.emit(Binary(t, "+", hi, Const(1, "usize"), site=e))
                hi = t
            self.emit(Check("range", [lo, hi, n], site=e))
            self.last_range = [lo, hi]
            return base, "range", [lo, hi]
        if bt.name in ("str", "str_view"):
            raise self.no("index of a string by a value", e)
        idx = self.expr(e.index)
        n = self.length_of(e.expr, base, e)
        self.emit(Check("index", [idx, n], site=e))
        return base, "index", [idx]

    def e_Index(self, e: ast.Index) -> Value:
        base, kind, args = self.index_checked(e)
        self.last_index = args[0] if kind == "index" else None
        t = self.temp(self.tys(e))
        self.emit(Access(t, kind, base, args, site=e))
        return t

    def deref_checked(self, e: ast.Unary, access: bool = False) -> Optional[Value]:
        """`*p`: a pointer (not a reference) is checked for null first; with `access` the target is read."""
        v = self.expr(e.operand)
        ot = getattr(e.operand, "ty", None)
        if ot is not None and ot.name == "ptr":
            self.emit(Check("non_null", [v], site=e))
        if not access:
            return v
        t = self.temp(self.tys(e))
        self.emit(Access(t, "deref", v, site=e))
        return t

    def e_FieldAccess(self, e: ast.FieldAccess) -> Value:
        base = self.expr(e.expr)
        t = self.temp(self.tys(e))
        self.emit(Access(t, "field", base, name=e.name.text, site=e))
        return t

    def field_length(self, e: ast.FieldAccess) -> Optional[int]:
        """The constant length of an array field (`self.a` with `a: [u32; 4]`), read from the struct's declaration."""
        t = getattr(e.expr, "ty", None)
        while isinstance(t, T.Ty) and t.name == "&" and t.args:
            t = t.args[0]
        node = getattr(getattr(t, "sym", None), "node", None)
        if not isinstance(node, ast.StructDecl):
            return None
        f = next((f for f in node.fields if f.name.text == e.name.text), None)
        return _array_length(f.ty) if f is not None else None

    def length_of(self, expr, base: Value, site) -> Value:
        """A constant for an array of known length, else a `Len` of the value."""
        if isinstance(expr, ast.PathExpr) and len(expr.segments) == 1:
            loc = self.lookup(expr.segments[0].name)
            if loc is not None and loc.length is not None:
                return Const(loc.length)
        elif isinstance(expr, ast.FieldAccess):
            n = self.field_length(expr)
            if n is not None:
                return Const(n)
        n = self.temp("usize")
        self.emit(Len(n, base, site=site))
        return n

    def receiver(self, e: ast.MethodCall) -> Value:
        """The receiver of a call. A call that may mutate it (`&mut self`, or a method the checker could not resolve) borrows the
        root of the receiver mutably, so analyses forget what they knew about it; a shared borrow or a move changes nothing."""
        full = getattr(e.receiver, "ty", None)
        shared = full is not None and full.name == "&" and not full.mut
        mode = getattr(e, "recv_mode", None)
        if (mode == "mut" or mode is None and not shared) and self.is_place(e.receiver):
            root = self.place(e.receiver)
            if root is not None:
                rty = self.tys(e.receiver)
                t = self.temp(rty if rty is None or rty.startswith("&") else "&mut " + rty)
                self.emit(Borrow(t, root, True, self.place_path(e.receiver), site=e.receiver))
                return t
            return self.opaque("borrow", [], e.receiver, self.tys(e.receiver))
        return self.expr(e.receiver)

    def e_MethodCall(self, e: ast.MethodCall) -> Optional[Value]:
        rt = _strip(getattr(e.receiver, "ty", None))
        name = e.name.text
        if rt is not None and not e.args:
            recv = None
            if rt.name == "optional" and name in ("has_value", "value"):
                recv = self.expr(e.receiver)
                if name == "has_value":
                    t = self.temp("bool")
                    self.emit(Test(t, "some", recv, site=e))
                    return t
                self.emit(Check("some", [recv], site=e))
                t = self.temp(self.tys(e))
                self.emit(Access(t, "unwrap", recv, site=e))
                return t
            if rt.name == "ptr" and name == "is_null":
                t = self.temp("bool")
                recv = self.expr(e.receiver)
                self.emit(Test(t, "non_null", recv, site=e))
                n = self.temp("bool")
                self.emit(Unary(n, "!", t, site=e))
                return n
            if rt.name in _SEQ and name in ("len", "is_empty"):
                recv = self.expr(e.receiver)
                n = self.length_of(e.receiver, recv, e)
                if name == "len":
                    return n
                t = self.temp("bool")
                self.emit(Binary(t, "==", n, Const(0), site=e))
                return t
        cell = self.cell_access(e)
        if cell is not False:
            return cell
        recv = self.receiver(e)
        args = [self.expr(a) for a in e.args]
        decl, qual, kind = self.callee(e)
        self.ncalls += 1
        self.call_requirements(e, args, e.args, decl, self.ncalls)
        t = self.temp(self.tys(e))
        self.emit(Call(t, name, args, recv, kind, qual, self.ncalls, flows=self.flows(decl), ensures=self.ensures_of(decl), preserves=self.preserves_of(decl),
                       taint=self.taint_of(e, decl), site=e))
        return t

    @staticmethod
    def taint_of(e, decl) -> str:
        """`untaint` / `tainted_result` when the callee's declaration (or the extern node the checker resolved) says so."""
        for node in (getattr(e, "decl", None), decl):
            for a in getattr(node, "attrs", None) or ():
                if a.name.text in ("untaint", "tainted_result"):
                    return a.name.text
        from .taint import summarized

        return "clean" if summarized(getattr(e, "decl", None) or decl) else ""

    def cell_access(self, e: ast.MethodCall):
        """`CELL.with(|v| body)` / `with_mut`: a `Cell` address, then the closure body inline in a scope that binds `v` to it. The value
        is the body's; False when `e` is not a cell access."""
        r = e.receiver
        if not (isinstance(r, ast.PathExpr) and len(r.segments) == 1 and r.segments[0].name in self.cells and self.lookup(r.segments[0].name) is None
                and e.name.text in ("with", "with_mut") and len(e.args) == 1 and isinstance(e.args[0], ast.Lambda) and len(e.args[0].params) == 1):
            return False
        d, lam, mut = self.cells[r.segments[0].name], e.args[0], e.name.text == "with_mut"
        ref = f"&{'mut ' if mut else ''}{type_text(d.ty)}"
        t = self.temp(ref)
        self.emit(Cell(t, d.name.text, mut, site=e))
        self.push_scope()
        self.declare(self.local(lam.params[0].name.text, ref, False), t, e)
        v = self.expr(lam.body)
        self.pop_scope()
        return v

    def e_Call(self, e: ast.Call) -> Optional[Value]:
        if getattr(e, "spec", None):   # `spec::barrier()` / `spec::value(x)`: not calls, an instruction of their own
            if e.spec == "barrier":
                self.emit(Barrier("fence", site=e))
                return None
            a = self.expr(e.args[0])
            t = self.temp(self.tys(e))
            self.emit(Barrier("value", t, a, site=e))
            return t
        if getattr(e, "ct", None):   # a compiler-synthesized `ct::` intrinsic: never resolved to a declaration
            args = [self.expr(a) for a in e.args]
            t = self.temp(self.tys(e))
            self.ncalls += 1
            self.emit(Call(t, "ct::" + e.ct, args, None, "intrinsic", "", self.ncalls, site=e))
            return t
        if isinstance(e.callee, ast.PathExpr) and (len(e.callee.segments) != 1 or self.lookup(e.callee.segments[0].name) is None):
            args = [self.expr(a) for a in e.args]
            decl, qual, kind = self.callee(e)
            if kind == "unresolved" and (e.callee.text, len(args)) in (("some", 1), ("none", 0)):
                return self.make(e.callee.text, args, [], e)
            self.ncalls += 1
            self.call_requirements(e, args, e.args, decl, self.ncalls)
            t = self.temp(self.tys(e))
            self.emit(Call(t, e.callee.text, args, None, kind, qual, self.ncalls, flows=self.flows(decl), ensures=self.ensures_of(decl), preserves=self.preserves_of(decl),
                           taint=self.taint_of(e, decl), site=e))
            return t
        fn = self.expr(e.callee)  # a function value: the callee is the first operand
        args = [self.expr(a) for a in e.args]
        t = self.temp(self.tys(e))
        self.ncalls += 1
        self.emit(Call(t, "<value>", [fn] + args, None, "value", "", self.ncalls, site=e))
        return t

    # -- values the IR does not model, and control flow that is not a plain `if` or loop
    def e_RangeExpr(self, e: ast.RangeExpr) -> Value:
        given = [(n, x) for n, x in (("lo", e.lo), ("hi", e.hi)) if x is not None]
        return self.make("range_inclusive" if e.inclusive else "range", [self.expr(x) for _, x in given], [n for n, _ in given], e)

    def make(self, kind: str, args: List[Value], names: List[str], site) -> Temp:
        t = self.temp(self.tys(site))
        self.emit(Make(t, kind, args, names, site=site))
        return t

    def e_StructLit(self, e: ast.StructLit) -> Value:
        vals = [self.expr(f.value) if f.value is not None else self.expr(ast.PathExpr(f.range, [ast.PathSeg(f.range, f.name.text)])) for f in e.fields]
        names = [f.name.text for f in e.fields]
        if e.base is not None:
            vals.append(self.expr(e.base))
            names.append("..")
        return self.make("struct", vals, names, e)

    def e_ArrayLit(self, e: ast.ArrayLit) -> Value:
        vals = [self.expr(x) for x in e.elems] + ([self.expr(e.repeat)] if e.repeat is not None else [])
        return self.make("repeat" if e.repeat is not None else "array", vals, [], e)

    def e_TupleExpr(self, e: ast.TupleExpr) -> Optional[Value]:
        if not e.elems:
            return None
        return self.make("tuple", [self.expr(x) for x in e.elems], [], e)

    def e_Lambda(self, e: ast.Lambda) -> Value:
        return self.opaque("lambda", [], e, self.tys(e))  # non-capturing: its body cannot touch this function's locals

    def e_MacroCall(self, e: ast.MacroCall) -> Optional[Value]:
        if e.name.text == "untaint" and len(e.args) == 2 and isinstance(e.args[1], (ast.Call, ast.MethodCall)):
            v = self.expr(e.args[1])
            call = next((i for b in [self.cur] if b is not None for i in reversed(b.instrs) if isinstance(i, Call)), None)
            if call is not None and isinstance(e.args[0], ast.Lit):
                call.reason = str(e.args[0].value)
            return v
        args: List[Optional[Value]] = []
        for a in e.args:
            if self.is_place(a) and not isinstance(a, ast.Unary):  # a bare place may be written by the macro
                root = self.place(a)
                if root is not None:
                    t = self.temp(self.tys(a))
                    self.emit(Borrow(t, root, True, self.place_path(a), site=a))
                    args.append(t)
                    continue
            args.append(self.expr(a))
        self.ncalls += 1
        diverges = e.name.text in ("panic", "unreachable", "todo", "unimplemented")
        t = None if diverges else self.temp(self.tys(e))
        self.emit(Call(t, e.name.text + "!", [a for a in args if a is not None], None, "macro", site=e, id=self.ncalls))
        if diverges:
            self.end(Unreachable(site=e))
        return t

    def e_Try(self, e: ast.Try) -> Value:
        v = self.expr(e.expr)
        failed = self.temp("bool")
        self.emit(Test(failed, "failed", v, site=e))
        fail, ok = self.block("try_fail"), self.block("try_ok")
        self.end(Branch(failed, fail, ok, site=e))
        self.start(fail)
        err = self.temp("i32")
        self.emit(Access(err, "error", v, site=e))
        self.unwind(0)
        self.end(Return(err, site=e))
        self.start(ok)
        ok = self.temp(self.tys(e))
        self.emit(Access(ok, "ok", v, site=e))
        return ok

    def e_For(self, e: ast.For) -> None:
        r = e.iter
        if isinstance(r, ast.RangeExpr) and r.lo is not None and r.hi is not None and not r.inclusive:
            ty = self.range_ty(r)
            if ty is not None:
                return self.for_range(e, r, ty)
        it = self.expr(e.iter)
        head, body, done = self.block("for"), self.block("body"), self.block("done")
        self.end(Jump(head))
        self.start(head)
        more = self.temp("bool")
        self.emit(Test(more, "has_next", it, site=e))
        self.end(Branch(more, body, done, site=e))
        self.start(body)
        outer = len(self.scopes)
        self.push_scope()
        var = self.local(e.var.text, "?", False)
        item = self.temp(self.item_ty(it))
        self.emit(Access(item, "item", it, site=e))
        self.declare(var, item, e)
        self.loops.append((head, done, outer))
        self.block_value(e.body)
        self.loops.pop()
        self.pop_scope()
        self.end(Jump(head))
        self.start(done)

    def item_ty(self, it: Value) -> Optional[str]:
        """What iterating `it` yields: `&T` for a reference to a vector or slice of `T`, `T` for an array; None when unknown."""
        t = it.type if isinstance(it, Temp) else None
        if t is None:
            return None
        if t.kind == "ref" and (t.args[0].kind == "slice" or (t.args[0].name == "vector" and len(t.args[0].args) == 1)):
            return ("&mut " if t.mut else "&") + str(t.args[0].args[0])
        return None

    def range_ty(self, r: ast.RangeExpr) -> Optional[str]:
        """The unsigned integer type of `lo..hi`, or None. An integer literal takes the type of the other end."""
        tys = {self.utype(r.lo), self.utype(r.hi)}
        tys.discard("lit")
        if not tys:  # `0..4`: the counter starts at a literal and only grows, so it is never negative whatever its type
            lo = r.lo
            return "usize" if isinstance(lo, ast.Lit) and lo.kind == "int" and lo.value >= 0 and self.utype(r.hi) == "lit" else None
        return tys.pop() if len(tys) == 1 and None not in tys else None

    def utype(self, x) -> Optional[str]:
        """An unsigned type name, "lit" for an integer literal, or None. The checker leaves `x.len()` open inside a range, so
        that is taken as a usize."""
        t = self.tys(x)
        if t in UNSIGNED_TYPES:
            return t
        if t == "{integer}":
            return "lit"
        if isinstance(x, ast.MethodCall) and x.name.text == "len" and not x.args:
            return "usize"
        if isinstance(x, ast.Binary) and x.op in ("+", "-"):
            both = {self.utype(x.left), self.utype(x.right)} - {"lit"}
            return both.pop() if len(both) == 1 and None not in both else ("lit" if not both else None)
        return None

    def for_range(self, e: ast.For, r: ast.RangeExpr, ty: str) -> None:
        """`for i in lo..hi` over an unsigned integer as a counting loop on a hidden counter, so a prover sees `counter < hi` at the
        head of the body and `counter + 1` on the way back. `hi` is evaluated once; `continue` goes to the step block."""
        lo, hi = self.expr(r.lo), self.expr(r.hi)
        ctr = self.local("it", ty, True)
        self.emit(Store(ctr, lo, site=e))
        head, body, step, done = self.block("for"), self.block("body"), self.block("step"), self.block("done")
        self.end(Jump(head))
        self.start(head)
        cur, more = self.temp(ty), self.temp("bool")
        self.emit(Load(cur, ctr, site=e))
        self.emit(Binary(more, "<", cur, hi, site=e))
        self.end(Branch(more, body, done, site=e))
        self.start(body)
        outer = len(self.scopes)
        self.push_scope()
        var = self.local(e.var.text, ty, False)
        item = self.temp(ty)
        self.emit(Load(item, ctr, site=e))
        self.declare(var, item, e)
        self.loops.append((step, done, outer))
        self.block_value(e.body)
        self.loops.pop()
        self.pop_scope()
        self.end(Jump(step))
        self.start(step)
        old, nxt = self.temp(ty), self.temp(ty)
        self.emit(Load(old, ctr, site=e))
        self.emit(Binary(nxt, "+", old, Const(1, ty), site=e))
        self.emit(Store(ctr, nxt, site=e))
        self.end(Jump(head))
        self.start(done)

    def arm_test(self, e: ast.Match, arm: ast.MatchArm, scrut) -> Value:
        """Whether `arm`'s pattern matches. An optional matched with `Some(..)` or `None` is a real `Test`, so a prover knows the
        optional holds a value inside a `Some` arm; any other pattern stays opaque."""
        st = _strip(getattr(e.scrutinee, "ty", None))
        p = arm.pattern
        is_some = isinstance(p, ast.VariantPat) and p.path.text == "Some"
        is_none = isinstance(p, ast.PathPat) and p.path.text == "None"
        if st is not None and st.name == "optional" and (is_some or is_none) and isinstance(scrut, Temp):
            t = self.temp("bool")
            self.emit(Test(t, "some", scrut, site=arm))
            if is_some:
                return t
            n = self.temp("bool")
            self.emit(Unary(n, "!", t, site=arm))
            return n
        t = self.temp("bool")
        self.emit(PatTest(t, scrut, pat_text(p), site=arm))
        return t

    def e_Match(self, e: ast.Match) -> Optional[Value]:
        scrut = self.expr(e.scrutinee)
        res = self.local("match", self.tys(e)) if getattr(e, "ty", None) not in (None, T.UNIT) else None
        done = self.block("match_done")
        for n, arm in enumerate(e.arms):
            last = n == len(e.arms) - 1
            body = self.block("arm")
            nxt = self.block("arm_next") if not last or arm.guard is not None else None
            if last:  # the checker has shown the arms exhaustive, so the last pattern needs no test
                self.end(Jump(body, site=arm))
            else:
                self.end(Branch(self.arm_test(e, arm, scrut), body, nxt, site=arm))
            self.start(body)
            self.push_scope()
            self.bind(arm.pattern, scrut, arm)
            if arm.guard is not None:
                run = self.block("arm_body")
                self.end(Branch(self.expr(arm.guard), run, nxt, site=arm))
                self.start(run)
            v = self.expr(arm.body)
            if res is not None and v is not None:
                self.emit(Store(res, v, site=arm))
            self.pop_scope()
            self.end(Jump(done))
            if nxt is not None:
                self.start(nxt)
        self.end(Unreachable())  # falls out of the last arm's failed guard, which the exhaustiveness check rules out
        self.start(done)
        if res is None:
            return None
        t = self.temp(res.ty)
        self.emit(Load(t, res, site=e))
        return t

    def e_CfgBlock(self, e: ast.CfgBlock) -> None:
        then, join = self.block("cfg"), self.block("join")
        self.end(Branch(self.opaque("cfg", [], e, "bool"), then, join, site=e))
        self.start(then)
        self.block_value(e.body)
        self.end(Jump(join))
        self.start(join)


def build_function(fn: ast.FnDecl, resolve=None, entry: bool = True, drops=frozenset(), owner: Optional[str] = None, cells=None, secure=frozenset()) -> Function:
    """The IR of one checked function; raises `Unsupported` for constructs the builder does not handle yet.

    `resolve(call)` names the declaration a call refers to (None when unknown); with it, a call to a function whose parameters carry
    fact attributes gets a tagged `Check` per fact, which the provers then discharge, refute or leave. `entry=False` leaves out the
    entry assumptions (for a function whose code generator does not guard them)."""
    built = _Builder(fn, resolve, entry, drops, owner, cells).run()
    built.require_proved = required_kinds(fn.attrs)
    built.taint_sinks = getattr(fn, "taint_sinks", built.taint_sinks)
    for loc in built.locals:
        loc.secure_struct = loc.ty in secure
    return built


def required_kinds(*attr_lists) -> Optional[frozenset]:
    """The check kinds that `#[require_proved]` / `#[require_proved(index, div)]` demand to be proved, merged over the attribute lists of
    the enclosing scopes (file `#![..]`, `impl`, function); None when no scope has the attribute. Bare means every kind."""
    out: Optional[Set[str]] = None
    for attrs in attr_lists:
        for a in attrs or ():
            if a.name.text != "require_proved":
                continue
            names = [x.name for x in (a.args or []) if x.name]
            out = (out or set()) | (set(names) if names else set(CHECK_KINDS))
    return frozenset(out) if out is not None else None


def build_module(tree) -> Module:
    """The IR of every function with a body in a checked module tree (methods of `impl` blocks included).

    Functions with a construct the builder does not handle are listed in `Module.skipped`, not built."""
    m = Module()
    items = list(getattr(tree, "items", []))
    fns: List[ast.FnDecl] = []
    owners: Dict[int, str] = {}
    free: Dict[str, ast.FnDecl] = {}
    methods: Dict[Tuple[str, str], ast.FnDecl] = {}
    for it in items:
        if isinstance(it, ast.FnDecl):
            fns.append(it)
            free[it.name.text] = it
        elif isinstance(it, ast.ImplDecl):
            fns.extend(f for f in it.items if isinstance(f, ast.FnDecl))
            owner = it.target.text if isinstance(it.target, ast.PathType) else None
            for f in it.items:
                if isinstance(f, ast.FnDecl) and owner:
                    methods[(owner, f.name.text)] = f
                    owners[id(f)] = owner

    def resolve(e, owner=None):
        if isinstance(e, ast.MethodCall):
            t = _strip(getattr(e.receiver, "ty", None))
            return methods.get((t.name, e.name.text)) if t is not None else None
        c = e.callee if isinstance(e, ast.Call) else None
        names = [s.name for s in c.segments] if isinstance(c, ast.PathExpr) else []
        return free.get(names[0]) if len(names) == 1 else methods.get((owner if names[0] == "Self" else names[0], names[1])) if len(names) == 2 else None

    resolve.qualify = lambda d: f"{owners[id(d)]}::{d.name.text}" if id(d) in owners else d.name.text
    m.resolver = resolve
    drops = drop_types(items)
    cells = {it.name.text: it for it in items if isinstance(it, ast.StaticDecl)}
    scope_attrs: Dict[int, list] = {id(f): list(getattr(tree, "inner_attrs", [])) for f in fns}
    for it in items:
        if isinstance(it, ast.ImplDecl):
            for f in it.items:
                if isinstance(f, ast.FnDecl):
                    scope_attrs[id(f)] += it.attrs
    for f in fns:
        if f.body is None:
            continue
        try:
            m.functions.append(build_function(f, resolve, True, drops, owners.get(id(f)), cells, secure_types(items)))
            m.functions[-1].require_proved = required_kinds(scope_attrs[id(f)], f.attrs)
        except Unsupported as e:
            m.skipped.append((f.name.text, e.message))
    return m
