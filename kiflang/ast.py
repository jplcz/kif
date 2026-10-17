# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Syntax tree (docs/kif-proc-language.md section 3). Every node carries its source `range`.

The tree is purely syntactic: names are not resolved, attributes are not validated and types are not checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

from .source import Range


@dataclass
class Node:
    range: Range


@dataclass
class Name(Node):
    text: str


# ---------------------------------------------------------------- attributes


@dataclass
class AttrArg(Node):
    """One attribute argument: `name`, `name(args)`, `name = value`, or a bare literal (`value`)."""

    name: Optional[str] = None
    value: Optional["Lit"] = None
    args: List["AttrArg"] = field(default_factory=list)


@dataclass
class Attribute(Node):
    """`#[name(args)]` (inner=False) or `#![name(args)]` (inner=True).

    `args` is the structured view; it is None when the arguments are not a list of names/literals (for example
    `self(struct foo *)`). `raw` is always the source text between the parentheses, or None when there are none.
    """

    name: Name
    args: Optional[List[AttrArg]] = None
    raw: Optional[str] = None
    inner: bool = False


# ---------------------------------------------------------------- types


@dataclass
class PathSeg(Node):
    name: str
    args: List["GenericArg"] = field(default_factory=list)


@dataclass
class PathType(Node):
    """`a::b::Name<T, 4>`; builtin and prelude types (`u32`, `ptr<T>`, `str`, `Self`) are plain paths."""

    segments: List[PathSeg]
    dyn: bool = False  # `dyn iface`: some implementer of a plain `#[ops]` interface, only behind `&` / `&mut`

    @property
    def text(self) -> str:
        return "::".join(s.name for s in self.segments)


@dataclass
class Lifetime(Node):
    """`'a`, `'static` or `'_` (elided); `text` includes the quote."""

    text: str


@dataclass
class RefType(Node):
    """`&T`, `&mut T`, `&'a T`, `&'a mut T`; `lifetime` is None when it is elided."""

    mut: bool
    inner: "Type"
    lifetime: Optional[Lifetime] = None


@dataclass
class SliceType(Node):
    """`[T]`: only behind a reference (`&[T]`, `&'a mut [T]`); a borrowed contiguous run, lowered to `reloco::span`."""

    elem: "Type"


@dataclass
class ArrayType(Node):
    elem: "Type"
    length: "Expr"


@dataclass
class FnType(Node):
    params: List["Type"]
    ret: Optional["Type"] = None


@dataclass
class TupleType(Node):
    """`()` is the unit type; one-element tuples are not part of the language."""

    elems: List["Type"] = field(default_factory=list)


@dataclass
class ConstArg(Node):
    """A const generic argument: an integer literal or a `{ expr }` block."""

    expr: "Expr"


Type = Union[PathType, RefType, SliceType, ArrayType, FnType, TupleType]
GenericArg = Union[PathType, RefType, SliceType, ArrayType, FnType, TupleType, ConstArg, Lifetime]


# ---------------------------------------------------------------- expressions


@dataclass
class Lit(Node):
    kind: str  # int | str | bool
    value: Union[int, str, bool]
    suffix: str = ""


@dataclass
class PathExpr(Node):
    """`x`, `a::b`, `f::<T>`, `Self::new`, `Enum::Member`. Turbofish arguments sit on the segment they follow."""

    segments: List[PathSeg]

    @property
    def text(self) -> str:
        return "::".join(s.name for s in self.segments)


@dataclass
class Unary(Node):
    op: str  # - ! * & &mut
    operand: "Expr"


@dataclass
class Binary(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class Assign(Node):
    op: str  # = += -= *= /= %= &= |= ^= <<= >>=
    target: "Expr"
    value: "Expr"


@dataclass
class Cast(Node):
    expr: "Expr"
    ty: Type


@dataclass
class Call(Node):
    callee: "Expr"
    args: List["Expr"]


@dataclass
class MethodCall(Node):
    receiver: "Expr"
    name: Name
    turbofish: List[GenericArg]
    args: List["Expr"]


@dataclass
class FieldAccess(Node):
    expr: "Expr"
    name: Name


@dataclass
class Index(Node):
    expr: "Expr"
    index: "Expr"


@dataclass
class Try(Node):
    """`expr?`"""

    expr: "Expr"


@dataclass
class RangeExpr(Node):
    lo: Optional["Expr"]
    hi: Optional["Expr"]
    inclusive: bool = False


@dataclass
class FieldInit(Node):
    name: Name
    value: Optional["Expr"]  # None for the shorthand `S { x }`


@dataclass
class StructLit(Node):
    path: PathExpr
    fields: List[FieldInit]
    base: Optional["Expr"] = None  # `..base`


@dataclass
class ArrayLit(Node):
    elems: List["Expr"]
    repeat: Optional["Expr"] = None  # `[elem; repeat]` has one element


@dataclass
class TupleExpr(Node):
    """`()` is the unit value."""

    elems: List["Expr"] = field(default_factory=list)


@dataclass
class MacroCall(Node):
    """`name!(args)`: `panic!`, `write!`, `here!`, `info!`, ..."""

    name: Name
    args: List["Expr"]


@dataclass
class Block(Node):
    stmts: List["Stmt"]
    tail: Optional["Expr"] = None  # the value of the block


@dataclass
class If(Node):
    cond: "Expr"
    then: Block
    otherwise: Optional[Union["If", Block]] = None


@dataclass
class While(Node):
    cond: "Expr"
    body: Block


@dataclass
class Loop(Node):
    body: Block


@dataclass
class For(Node):
    var: Name
    iter: "Expr"
    body: Block


@dataclass
class WildPat(Node):
    pass


@dataclass
class LitPat(Node):
    lit: Lit


@dataclass
class PathPat(Node):
    path: PathExpr


@dataclass
class RangePat(Node):
    lo: Lit
    hi: Lit


@dataclass
class OrPat(Node):
    alts: List["Pattern"]


@dataclass
class BindPat(Node):
    """A name inside a variant or tuple pattern, `Some(x)` or `(mut a, b)`: binds the value."""

    name: Name
    mut: bool = False
    sub: Optional["Pattern"] = None  # `name @ pattern`


@dataclass
class TuplePat(Node):
    """`(p, q)`: a tuple pattern (also the target of `let (a, b) = ..`)."""

    elems: List["Pattern"]


@dataclass
class VariantPat(Node):
    """`Some(p)`: a built-in generation-time variant with sub-patterns."""

    path: PathExpr
    args: List["Pattern"]


Pattern = Union[WildPat, LitPat, PathPat, RangePat, OrPat, BindPat, VariantPat, TuplePat]


@dataclass
class MatchArm(Node):
    pattern: Pattern
    body: "Expr"
    guard: Optional["Expr"] = None  # `pattern if guard => body`


@dataclass
class Match(Node):
    scrutinee: "Expr"
    arms: List[MatchArm]


@dataclass
class UnsafeBlock(Node):
    body: Block


@dataclass
class CfgBlock(Node):
    """`cfg(pred) { .. }`: the block exists only where the predicate holds (kiflang/cfg.py); it has type unit."""

    attr: Attribute
    body: Block


@dataclass
class Return(Node):
    value: Optional["Expr"] = None


@dataclass
class Break(Node):
    pass


@dataclass
class Continue(Node):
    pass


@dataclass
class LambdaParam(Node):
    name: Name
    ty: Optional[Type] = None  # may be inferred from the target `fn(..)` type


@dataclass
class Lambda(Node):
    """`|a, b: T| expr` or `|a| -> R { .. }`: a non-capturing function value that coerces to a `fn(..)` pointer."""

    params: List[LambdaParam]
    body: "Expr"
    ret: Optional[Type] = None


Expr = Union[
    Lambda, Lit, PathExpr, Unary, Binary, Assign, Cast, Call, MethodCall, FieldAccess, Index, Try, RangeExpr, StructLit,
    ArrayLit, TupleExpr, MacroCall, Block, If, While, Loop, For, Match, UnsafeBlock, CfgBlock, Return, Break, Continue,
]


# ---------------------------------------------------------------- statements


@dataclass
class Let(Node):
    name: Name
    mut: bool
    ty: Optional[Type] = None
    init: Optional[Expr] = None


@dataclass
class LetPat(Node):
    """`let (a, b): (u32, u32) = init;`: destructuring let."""

    pattern: "Pattern"
    ty: Optional[Type] = None
    init: Optional[Expr] = None


@dataclass
class ExprStmt(Node):
    expr: Expr
    semi: bool = True


@dataclass
class ItemStmt(Node):
    item: "Item"


Stmt = Union[Let, LetPat, ExprStmt, ItemStmt]


# ---------------------------------------------------------------- items


@dataclass
class Param(Node):
    """`name: type`, or a receiver: `self` (kind "value"), `&self` ("ref"), `&mut self` ("mut"). Receivers have no type."""

    name: Name
    ty: Optional[Type] = None
    receiver: str = ""
    lifetime: Optional[Lifetime] = None  # `&'a self`
    attrs: List[Attribute] = field(default_factory=list)


@dataclass
class GenericParam(Node):
    """`T`, `T: bound + bound`, `T = Default`, or `const N: usize = 4`."""

    name: Name
    bounds: List[PathType] = field(default_factory=list)
    default: Optional[Union[Type, "Expr"]] = None
    const_ty: Optional[Type] = None  # set for `const N: type`
    is_lifetime: bool = False  # `'a` or `'a: 'b + 'c` (outlives bounds go in `outlives`)
    outlives: List[Lifetime] = field(default_factory=list)


@dataclass
class FnDecl(Node):
    attrs: List[Attribute]
    name: Name
    params: List[Param]
    ret: Optional[Type] = None
    body: Optional[Block] = None  # None for `extern` declarations
    generics: List[GenericParam] = field(default_factory=list)
    private: bool = False
    is_gen: bool = False
    variadic: bool = False


@dataclass
class ConstDecl(Node):
    attrs: List[Attribute]
    name: Name
    ty: Type
    value: Expr
    private: bool = False


@dataclass
class StaticDecl(Node):
    """`#[tls] static NAME: T = init;` / `#[per_cpu] static ...` (4.17): a thread-local or per-CPU cell."""

    attrs: List[Attribute]
    name: Name
    ty: Type
    value: Expr
    private: bool = False


@dataclass
class TypeAlias(Node):
    attrs: List[Attribute]
    name: Name
    generics: List[GenericParam]
    ty: Type


@dataclass
class ImplDecl(Node):
    """`impl S { .. }` (iface is None) or `impl iface for S { .. }`."""

    attrs: List[Attribute]
    target: Type
    items: List[Union[FnDecl, ConstDecl]]
    iface: Optional[PathType] = None
    generics: List[GenericParam] = field(default_factory=list)


@dataclass
class ExternFn(Node):
    """`extern "C" fn f(..) -> T;` or `extern "C" macro m(..);`"""

    attrs: List[Attribute]
    abi: str
    kind: str  # fn | macro
    decl: FnDecl


@dataclass
class ExternType(Node):
    attrs: List[Attribute]
    abi: str
    path: PathType
    generics: List[GenericParam]


@dataclass
class ExternImpl(Node):
    attrs: List[Attribute]
    abi: str
    target: Type
    fns: List[FnDecl]
    generics: List[GenericParam] = field(default_factory=list)


@dataclass
class Verbatim(Node):
    """`header { .. }`, `code { .. }`, `m_prolog { .. }`, `m_epilog { .. }`: raw target-language text."""

    kind: str
    text: str
    keyword_range: Range
    target: Optional[str] = None  # "c" or "cxx" for `header(c) {`; None goes to both outputs


@dataclass
class Hook(Node):
    """`before { .. }` / `after { .. }` inside an interface; applies to the next method."""

    kind: str
    body: Block


@dataclass
class Method(Node):
    """An interface method. `body` is a kif default; `default_sym` is the old `default [symbol]` form."""

    attrs: List[Attribute]
    name: Name
    params: List[Param]
    ret: Optional[Type] = None
    body: Optional[Block] = None
    has_default: bool = False
    default_sym: Optional[Name] = None
    override: bool = False
    static: bool = False


@dataclass
class Interface(Node):
    attrs: List[Attribute]
    name: Name
    members: List[Union[Method, Hook, Verbatim]]

    @property
    def methods(self) -> List[Method]:
        return [m for m in self.members if isinstance(m, Method)]


@dataclass
class ForeignClass(Node):
    attrs: List[Attribute]
    name: Name


@dataclass
class EnumMember(Node):
    attrs: List[Attribute]
    name: Name
    value: Optional[Expr] = None


@dataclass
class EnumDecl(Node):
    attrs: List[Attribute]
    name: Name
    members: List[EnumMember]
    repr: Optional[Type] = None


@dataclass
class StructField(Node):
    attrs: List[Attribute]
    name: Name
    ty: Type
    default: Optional[Expr] = None
    private: bool = False


@dataclass
class StructDecl(Node):
    attrs: List[Attribute]
    name: Name
    fields: List[StructField]
    generics: List[GenericParam] = field(default_factory=list)


@dataclass
class RegField(Node):
    attrs: List[Attribute]
    name: Name
    lo: int
    hi: int


@dataclass
class Register(Node):
    attrs: List[Attribute]
    name: Name
    ty: Type
    fields: List[RegField]


@dataclass
class BlockDecl(Node):
    attrs: List[Attribute]
    name: Name
    registers: List[Register]


@dataclass
class Namespace(Node):
    path: List[Name]


@dataclass
class Import(Node):
    path: str


@dataclass
class UseEntry(Node):
    """One imported name: `use a::b::c;` (path a::b::c), `use a::b::c as d;` (alias d), `use a::b::*;` (glob)."""

    path: List[Name]
    alias: Optional[Name] = None
    glob: bool = False


@dataclass
class Use(Node):
    """`use a::{b, c as d};` is flattened into one entry per name."""

    entries: List[UseEntry]


@dataclass
class Include(Node):
    path: str
    system: bool


Item = Union[
    FnDecl, ConstDecl, StaticDecl, TypeAlias, ImplDecl, ExternFn, ExternType, ExternImpl, Verbatim, Interface, ForeignClass,
    EnumDecl, StructDecl, BlockDecl, Namespace, Import, Use, Include,
]


@dataclass
class KifFile(Node):
    items: List[Item] = field(default_factory=list)
    inner_attrs: List[Attribute] = field(default_factory=list)  # `#![..]` at the top of the file

    def of(self, cls):
        return [i for i in self.items if isinstance(i, cls)]
