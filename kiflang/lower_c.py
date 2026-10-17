# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The C backend, stage 1 (docs/kif-c-backends.md): free functions, plain structs, enums, constants and arrays.

`lower(mod, target, stem)` takes a module the checker accepted and returns `{filename: text}`: `<stem>_proc.h` (types,
constants, `static __inline` functions) and `<stem>_proc.c` (the `#[export]` definitions, only when there are any).
The generated header is self-contained: the helpers it needs (wrapping arithmetic, trapping division, shifts and indexing, the
panic macro) are written into a per-file prologue, each under its own guard, and only the target's headers are included.

Not lowered yet (reported as `LowerError`): `impl` blocks and methods, `str`/`vector`/spans, `result<T>`/`?`, tuples, closures,
interfaces, cells and imports. Those are the later stages of the C backend.
"""

from __future__ import annotations

import os
import re
from dataclasses import fields as dc_fields
from typing import Dict, List, Optional, Set, Tuple

from . import ast
from . import astutil
from . import lexer
from . import cfg
from . import diagnostics as dg
from . import facts
from . import ir
from . import ir_c
from . import irhash
from . import passes
from . import types as T
from .astutil import attr_name_arg, find_attr, has_attr, walk

C_KEYWORDS = frozenset(
    "auto break case char const continue default do double else enum extern float for goto if inline int long register restrict "
    "return short signed sizeof static struct switch typedef union unsigned void volatile while bool true false NULL".split())
# The helpers every generated file shares; kifc writes them once per target into kif_c.h (`common_header`).
HELPER_TEXT = {
    "KIF_IDX": "KIF_INLINE size_t KIF_IDX(uint64_t i, uint64_t n, const char *file, int line)\n"
               "{\n\tif (i >= n) KIF_PANIC(file, line, \"index out of bounds\");\n\treturn (size_t)i;\n}",
    "KIF_NONNEG": "KIF_INLINE uint64_t KIF_NONNEG(int64_t n, const char *file, int line)\n"
                  "{\n\tif (n < 0) KIF_PANIC(file, line, \"negative shift count\");\n\treturn (uint64_t)n;\n}",
    "KIF_RANGE": "KIF_INLINE size_t KIF_RANGE(uint64_t lo, uint64_t hi, uint64_t n, const char *file, int line)\n"
                 "{\n\tif (lo > hi || hi > n) KIF_PANIC(file, line, \"slice range out of bounds\");\n\treturn (size_t)lo;\n}",
    "KIF_NN": "KIF_INLINE void *KIF_NN(void *p, const char *file, int line)\n"
              "{\n\tif (p == NULL) KIF_PANIC(file, line, \"null pointer\");\n\treturn p;\n}",
}
BITS = {"i8": 8, "i16": 16, "i32": 32, "i64": 64, "isize": 64, "u8": 8, "u16": 16, "u32": 32, "u64": 64, "usize": 64}
ARITH = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "REM", "<<": "SHL", ">>": "SHR"}
CMP = {"==", "!=", "<", "<=", ">", ">="}
PANIC_FNS = {"panic", "unreachable"}
OUT_FIRST = {"try_create", "try_allocate", "try_clone"}  # constructors: the new object comes first (docs/kif-c-backends.md 3.2)

from .target import ERROR_ERRNO  # noqa: E402  (reloco's error -> errno defaults; a target overrides entries with errno.map)


def _is_unit(t) -> bool:
    return isinstance(t, ast.TupleType) and not t.elems


class LowerError(Exception):
    def __init__(self, message: str, rng=None, code: str = "KIFC001") -> None:
        super().__init__(message)
        self.message, self.range, self.code = message, rng, code


def _signed(n: str) -> bool:
    return n.startswith("i")


def stem_of(key: str) -> str:
    """The stem generated files of a source are named by: its file name without `.kif` and a trailing `_if`."""
    base = os.path.basename(key)
    base = base[:-4] if base.endswith(".kif") else base
    return base[:-3] if base.endswith("_if") and len(base) > 3 else base


def _upper(s: str) -> str:
    return re.sub(r"\W", "_", s).upper()


def c_string(s: str) -> str:
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out += [f"\\x{b:02x}" for b in ch.encode()] + ['""']
    return '"' + "".join(out) + '"'


class SpanArg:
    """A synthetic expression: `expr` (a borrow of an array, vector, range of one, or a slice) as a slice value of element type `elem`."""

    def __init__(self, expr, mut: bool, elem) -> None:
        self.expr, self.mut, self.elem, self.range, self.ty = expr, mut, elem, getattr(expr, "range", None), None


class Fn:
    """State of the function being lowered: output lines, temporaries, the scope stack of local names."""

    def __init__(self, decl: ast.FnDecl) -> None:
        self.decl = decl
        self.lines: List[str] = []
        self.indent = 1
        self.scopes: List[Dict[str, Tuple[str, object]]] = [{}]
        # Per scope, the objects it owns, in declaration order: (C name, live flag or None, drop statement, only on an error exit).
        self.owners: List[List[Tuple[str, Optional[str], str, bool]]] = [[]]
        self.flags: Dict[str, str] = {}  # C name of an owner -> its `kif_live_` flag (owners that are moved somewhere have one)
        self.moved: Set[str] = set()  # source names of locals that are moved somewhere in the function
        self.loops: List[int] = []  # scope index of each enclosing loop body: break/continue drop the scopes from there
        self.borrowed: Set[int] = set()  # ids of `f()?` nodes used only through a reference (receiver, discarded): they stay owned until scope end
        self.pending: List[Tuple[str, int, int]] = []  # `?` temporaries of the statement being lowered: (C name, scope index, id of the `?` node)
        self.used: Set[str] = set()
        self.ntemp = 0
        self.result: Optional[ast.Node] = None  # the T of a `result<T>` function (lowered to int + `kif_out`)
        self.value_self = False  # `self` by value is a pointer in C
        self.rlocals: Dict[str, Tuple[Optional[str], Optional[str]]] = {}  # `let r = <result call>`: C name of rc -> (value var, T)
        self.returns_void = True
        self.ir_hash: Optional[str] = None  # attestation hash of the raw IR (before any pass), `sha256:<hex>`
        self.ir: Optional[ir.Function] = None  # the function's IR after the pass pipeline (None when the builder could not handle it)
        self.cnames: List[str] = []  # the C parameter names in order (a twinned function's guarded entry forwards them)


class _CallResolver:
    """What the IR builder asks about a call: its declaration (`__call__`), its qualified name (`qualify`) and whether it has a guarded
    entry with a twin, which is where requirement checks are emitted (`has_twin`)."""

    def __init__(self, lw: "Lowerer") -> None:
        self.lw = lw

    def __call__(self, e, owner=None) -> Optional[ast.FnDecl]:
        try:
            return self.lw.resolve(e)
        except LowerError:
            return None

    def qualify(self, d: ast.FnDecl) -> str:
        o = self.lw.owner.get(id(d))
        return f"{o}::{d.name.text}" if o else d.name.text

    def has_twin(self, d: ast.FnDecl) -> bool:
        return self.lw.twinned(d)


VISIBILITIES = ("default", "hidden", "protected", "internal")


class Lowerer:
    def __init__(self, mod, target, stem: str, filename: Optional[str] = None, note: str = "") -> None:
        self.mod, self.t, self.stem, self.note = mod, target, stem, note
        self.file = filename or stem + ".kif"
        self.helpers: Dict[str, str] = {}  # helper key -> text, in order of first use
        self.fn: Optional[Fn] = None
        self.impl_attrs: Dict[int, List[ast.Attribute]] = {}  # method -> the attributes of its impl block (a visibility scope)
        self.owned: Dict[str, List[ast.Field]] = {}  # struct with an impl -> its owned (vector / str) fields
        self.dropable: Set[str] = set()  # structs that have a user `drop` or own something: `S_drop` exists and locals of them are dropped
        self.heap: Set[str] = set()  # structs used through `ptr<S>`: `S_try_new` / `S_delete` are generated for them
        self.spans: Dict[str, Tuple[str, bool]] = {}  # element tag -> (C element type, is a struct): `kif_span_<tag>` / `kif_cspan_<tag>`
        self.uses_str = self.uses_view = False  # `str` / `str_view` appear (their C structs and operations are emitted)
        self.let_ty = None  # the annotation of the `let` being lowered (names the T of `vector::try_create`)
        self.opts: Dict[str, Tuple[str, Optional[str], bool]] = {}  # optional<T> tag -> (C element type, drop statement of `o->v` or None, T is a user struct)
        self.vecs: Dict[str, Tuple[str, bool]] = {}  # vector<T> element tag -> (C element type, T is a struct)
        self.svecs: Dict[str, Tuple[str, bool]] = {}  # the same for secure_vector<T> (struct kif_svec_<tag>; docs/kif-secure-wipe.md rule W12)
        self.structs: Dict[str, ast.StructDecl] = {}
        self.enums: Dict[str, ast.EnumDecl] = {}
        self.fns: Dict[str, ast.FnDecl] = {}
        self.consts: Dict[str, ast.ConstDecl] = {}
        self.ir_bails: Dict[str, str] = {}  # function -> why the IR emitter refused it
        self.cells: Dict[str, ast.StaticDecl] = {}  # `#[tls]` / `#[per_cpu]` statics of this module
        self.methods: Dict[Tuple[str, str], ast.FnDecl] = {}
        self.owner: Dict[int, str] = {}  # id(fn decl) -> the struct of its `impl`
        self.impl_structs: Set[str] = set()
        self.opt_inline: Set[str] = set()  # struct-element optionals emitted next to the struct that holds them as a field
        self.externs: Set[str] = {i.path.text for i in (mod.tree.items if mod else []) if isinstance(i, ast.ExternType) and i.abi == "C" and not i.generics}
        self.kobj_ifaces: Set[str] = {i.name.text for i in (mod.tree.items if mod else []) if isinstance(i, ast.Interface) and has_attr(i.attrs, "kobj")}
        self.ops_c: List[str] = []  # `#[ops]` thunks, tables and default bodies: defined in `<stem>_proc.c`
        self.opt_try: Set[str] = set()  # optional tags whose `try_value` is used
        self.ifaces: Dict[str, ast.Interface] = {}  # `#[ops]` interfaces of this module
        self.foreign_ifaces: Dict[str, ast.Interface] = {}  # `#[ops]` interfaces of imported modules: their tables and wrappers come from the library's header
        self.ops_impls: List[Tuple[str, str, ast.ImplDecl]] = []  # (struct, interface, impl) for `impl I for S`
        self.fixed_name: Dict[int, str] = {}
        self.cur_owner: Optional[str] = None  # what `Self` means in the code being lowered
        self.uses_errno = False
        self.uses_wipe_volatile = False  # a `volatile` wipe is emitted: the volatile macro is defined too
        self.uses_wipe = False  # a secure wipe is emitted: the target's wipe macro and header are defined in the header
        self.cast_enums: Set[str] = set()
        self.label = 0
        self.last_line = 0
        self.used_labels: Set[str] = set()
        self.foreign: Set[str] = set()  # struct names declared by imported modules: laid out and dropped by their own headers
        self.declstem: Dict[int, str] = {}  # id(decl) -> stem of the module that declares it (imports only)
        self.includes: List[str] = []  # `<stem>_proc.h` of the direct imports

    # ------------------------------------------------------------------ names and types

    def err(self, msg: str, node=None) -> LowerError:
        return LowerError(msg, getattr(node, "range", None))

    def prefix(self) -> str:
        return "" if self.t.get("naming.prefix", "stem") == "none" else self.stem + "_"

    def stem_for(self, decl) -> str:
        return self.declstem.get(id(decl), self.stem)

    def fn_name(self, f: ast.FnDecl) -> str:
        if id(f) in self.fixed_name:
            return self.fixed_name[id(f)]
        if id(f) in self.owner:
            return f"{self.owner[id(f)]}_{f.name.text}"
        if has_attr(f.attrs, "export") or has_attr(f.attrs, "no_prefix"):
            return f.name.text
        return ("" if self.t.get("naming.prefix", "stem") == "none" else self.stem_for(f) + "_") + f.name.text

    def const_name(self, c: ast.ConstDecl) -> str:
        return (_upper(self.stem_for(c)) + "_" if self.prefix() else "") + c.name.text

    def enumerator(self, e: ast.EnumDecl, member: str) -> str:
        return member if has_attr(e.attrs, "no_prefix") else f"{e.name.text}_{member}"

    def scalar(self, name: str) -> str:
        return self.t.c_type(name)

    def cty(self, t: T.Ty, node=None) -> str:
        if t.name in BITS or t.name in ("bool", "char"):
            return self.scalar(t.name)
        if t.name == "()":
            return "void"
        if t.name == "error":
            return "int"
        if t.name in ("str_view", "{string}"):
            self.uses_view = True
            return "struct kif_strview"
        if t.name == "str" and not t.args:
            self.uses_str = self.uses_view = True
            return "struct kif_str"
        if t.name == "ptr" and len(t.args) == 1:
            return f"struct {self.heap_struct(t.args[0].name, node)} *"
        if t.name == "vector" and len(t.args) == 1:
            return "struct kif_vec_" + self.vec_tag(t.args[0], node)
        if t.name == "secure_vector" and len(t.args) == 1:
            return "struct kif_svec_" + self.vec_tag(t.args[0], node, True)
        if t.name == "optional" and len(t.args) == 1:
            return "struct kif_opt_" + self.opt_tag(t.args[0], node)
        if t.name == "{integer}":
            return self.scalar("i32")
        if t.name == "&":
            inner = t.args[0]
            if inner.name == "[;]" or inner.name == "[]":
                raise self.err("a reference to an array or slice type is not lowered here", node)
            return ("" if t.mut else "const ") + self.cty(inner, node) + " *"
        if t.name in ("c_ptr", "c_cptr") and len(t.args) == 1:
            return ("const " if t.name == "c_cptr" else "") + self.cty(t.args[0], node) + " *"
        if t.sym is not None and t.sym.kind == "struct" and not t.args:
            return "struct " + t.name
        if t.sym is not None and t.sym.kind == "enum":
            return "enum " + t.name
        if t.name in self.externs and not t.args:
            return t.name
        raise self.err(f"type {t} has no C lowering yet", node)

    def base_ast(self, t, node=None) -> str:
        """C spelling of a scalar/struct/enum written in the source."""
        if isinstance(t, ast.TupleType) and not t.elems:
            return "void"
        if isinstance(t, ast.PathType) and t.dyn:
            raise self.err(f"`dyn {t.text}` is lowered only behind `&` / `&mut`", t)
        if self.slice_ref(t) is not None:
            return self.span_type(t)
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name in ("c_ptr", "c_cptr") \
                and len(t.segments[0].args) == 1 and isinstance(t.segments[0].args[0], ast.PathType):
            return ("const " if t.segments[0].name == "c_cptr" else "") + self.base_ast(t.segments[0].args[0]) + " *"
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name in ("str", "str_view") \
                and not any(isinstance(a, ast.PathType) for a in t.segments[0].args):
            self.uses_view = True
            if t.segments[0].name == "str":
                self.uses_str = True
                return "struct kif_str"
            return "struct kif_strview"
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == "ptr" and len(t.segments[0].args) == 1 \
                and isinstance(t.segments[0].args[0], ast.PathType):
            return f"struct {self.heap_struct(t.segments[0].args[0].text, t)} *"
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == "vector" and len(t.segments[0].args) == 1:
            return "struct kif_vec_" + self.vec_tag(t.segments[0].args[0], t)
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == "secure_vector" and len(t.segments[0].args) == 1:
            return "struct kif_svec_" + self.vec_tag(t.segments[0].args[0], t, True)
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and t.segments[0].name == "optional" and len(t.segments[0].args) == 1:
            return "struct kif_opt_" + self.opt_tag(t.segments[0].args[0], t)
        if isinstance(t, ast.PathType) and len(t.segments) == 1 and not t.segments[0].args:
            n = t.segments[0].name
            if n == "Self" and self.cur_owner is not None:
                return "struct " + self.cur_owner
            if n in BITS or n in ("bool", "char"):
                return self.scalar(n)
            if n == "error":
                return "int"
            if n in self.structs:
                return "struct " + n
            if n in self.enums:
                return "enum " + n
            if n in self.externs:
                return n
        raise self.err(f"type {self.type_text(t)} has no C lowering yet", t if hasattr(t, "range") else node)

    @staticmethod
    def type_text(t) -> str:
        if isinstance(t, ast.PathType):
            return t.text
        return type(t).__name__

    @staticmethod
    def slice_ref(t) -> Optional[ast.SliceType]:
        """The `[T]` of a source type `&[T]` / `&mut [T]`, else None."""
        return t.inner if isinstance(t, ast.RefType) and isinstance(t.inner, ast.SliceType) else None

    def span_type(self, t: ast.RefType) -> str:
        """`struct kif_span_<tag>` (`&mut [T]`) or `struct kif_cspan_<tag>` (`&[T]`); registers the element."""
        elem = t.inner.elem
        name = elem.text if isinstance(elem, ast.PathType) else None
        if name in BITS or name in ("bool", "char"):
            self.spans.setdefault(name, (self.scalar(name), False))
        elif name in self.structs:
            self.spans.setdefault(name, (f"struct {name}", True))
        elif name in self.enums:
            self.spans.setdefault(name, (f"enum {name}", False))
        elif name == "str_view" and not elem.segments[0].args:
            self.uses_view = True
            self.spans.setdefault(name, ("struct kif_strview", False))
        elif name == "str" and not elem.segments[0].args:
            self.uses_str = self.uses_view = True
            self.spans.setdefault(name, ("struct kif_str", False))
        else:
            raise self.err(f"a slice of {self.type_text(elem)} is not lowered to C: the element is an integer, bool, char, enum, struct or str", elem)
        return f"struct kif_{'' if t.mut else 'c'}span_{name}"

    def build_ir(self, f: ast.FnDecl) -> None:
        """The IR of `f` after the pass pipeline, in `self.fn.ir`. Code generation is the last stage of the pipeline: every registered
        pass runs first (the target's `ir.passes` selects and orders them, default: all) plus the `c_refusals` target pass, and an error
        a pass reports stops the build. A function the IR builder cannot handle is left without IR, which `fn_body` reports."""
        try:
            fn = ir.build_function(f, self.requirement_resolver, self.twinned(f), frozenset(self.dropable), self.cur_owner, self.cells,
                                 frozenset(n for n in self.dropable if n in self.structs and self.secure(self.structs[n])))
        except ir.Unsupported:
            return
        self.fn.ir_hash = irhash.ir_hash(fn)
        ran = passes.run_function(fn, passes.pipeline(self.t.get("ir.passes", "default") + ",c_refusals"), self.requirement_resolver,
                                 {"errno": self.t.get("errno.map") or {}, "access": {k: self.t.get(f"access.{k}", "prove") for k in ("index", "range", "optional", "div", "shift")}})
        self.fn.ir = fn
        for d in ran.diagnostics:
            if d.severity == dg.ERROR:
                raise LowerError(d.message, d.range, d.code)

    @property
    def requirement_resolver(self) -> "_CallResolver":
        return _CallResolver(self)

    def twinned(self, f: ast.FnDecl) -> bool:
        """Whether `f` has a guarded entry and an unguarded twin: it has parameter facts (kiflang/facts.py `requirements`) and an
        ordinary C form. `F` checks the facts and calls `F__nofacts`, whose body assumes them; a caller that proved them calls the twin."""
        owner = self.owner.get(id(f))
        return (f.body is not None and not f.is_gen and id(f) not in self.fixed_name and not (owner in self.owned and f.name.text == "drop")
                and not (f.generics and any(not g.is_lifetime for g in f.generics)) and bool(facts.requirements(f)))

    @staticmethod
    def elided_note(what: str, why: str) -> str:
        """The C comment that documents an omitted check, so a reader of the output can see what was proved and why."""
        return f"/* kif: {what} check elided: {why.replace('%', '').replace('*/', '* /')} */"

    def dim(self, e) -> str:
        """An array length: an integer literal or a constant (a type position has no checked expression type)."""
        if isinstance(e, ast.Lit) and e.kind == "int":
            return str(e.value)
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1 and e.segments[0].name in self.consts:
            return self.const_name(self.consts[e.segments[0].name])
        raise self.err("an array length must be an integer literal or a constant", e)

    def decl(self, t, name: str, param: bool = False) -> str:
        """A declaration of `name` with source type `t`: arrays get a dimension (a pointer as a parameter)."""
        if isinstance(t, ast.ArrayType):
            if param:
                return f"{self.decl(t.elem, '', False).rstrip()} *{name}".replace("  ", " ")
            return f"{self.decl(t.elem, name)}[{self.dim(t.length)}]"
        if self.slice_ref(t) is not None:
            return f"{self.span_type(t)} {name}".rstrip()
        if isinstance(t, ast.RefType) and isinstance(t.inner, ast.PathType) and t.inner.dyn:
            return f"struct {t.inner.text}_ref {name}".rstrip()
        if isinstance(t, ast.RefType):
            inner = t.inner
            if isinstance(inner, ast.ArrayType):
                return f"{'' if t.mut else 'const '}{self.base_ast(inner.elem)} *{name}"
            return f"{'' if t.mut else 'const '}{self.base_ast(inner)} *{name}"
        b = self.base_ast(t)
        if b.endswith("*"):
            return b + name
        return f"{b} {name}".rstrip() if name else b

    # ------------------------------------------------------------------ output

    def emit(self, line: str) -> None:
        f = self.fn
        f.lines.append("\t" * f.indent + line)

    def own(self, cname: str, src: str, t, force_flag: bool = False, stmt: Optional[str] = None) -> None:
        """Register the local `cname` (source name `src`, type `t`) as owner of a droppable value: it is dropped when its scope ends."""
        stmt = stmt or self.drop_stmt(cname, t)
        if stmt is None:
            return
        flag = None
        if force_flag or src in self.fn.moved:
            flag = f"kif_live_{cname}"
            self.emit(f"bool {flag} = {'false' if force_flag else 'true'};")
            self.fn.flags[cname] = flag
        self.fn.owners[-1].append((cname, flag, stmt, False))

    def lineno(self, node) -> int:
        r = getattr(node, "range", None)
        return r.start.line + 1 if r is not None else 0

    def doc_comment(self, node, indent: str = "") -> str:
        """The `//` comment lines directly above a declaration, as a C block comment (empty when there are none or `features.doc_comments` is false)."""
        src = getattr(self.mod, "source", "") if self.mod else ""
        if not src or not self.t.flag("doc_comments", True):
            return ""
        text = astutil.doc_above(src.split("\n"), node)
        if not text:
            return ""
        body = "\n".join((f"{indent} * " + ln).rstrip() for ln in text.replace("*/", "* /").split("\n"))
        return f"{indent}/**\n{body}\n{indent} */\n"

    def doc_lines(self, node) -> Set[int]:
        """Indexes of the source lines of the doc comment above `node`, which a one-line body must not repeat."""
        src = getattr(self.mod, "source", "") if self.mod else ""
        if not src or node is None or getattr(node, "range", None) is None:
            return set()
        lines = src.split("\n")
        out: Set[int] = set()
        i = astutil.decl_line(lines, node) - 1
        while i >= 0 and lexer.comment_text(lines[i]) is not None:
            out.add(i)
            i -= 1
        return out

    def comments_above(self, line: int, seen: Set[int]) -> List[str]:
        """The `//` lines directly above the 1-based source `line`, oldest first, that are not in `seen` yet (their line indexes are added to it).
        The statements of a body use this where they emit a `#line` marker, so the author's comments follow the lines they annotate."""
        src = getattr(self.mod, "source", "") if self.mod else ""
        if not src or not self.t.flag("body_comments", True):
            return []
        lines = src.split("\n")
        out: List[str] = []
        i = line - 2
        while 0 <= i < len(lines) and i not in seen and (text := lexer.comment_text(lines[i])) is not None:
            seen.add(i)
            out.append(text)
            i -= 1
        return [f"/* {t.replace('*/', '* /')} */".replace("/*  */", "/**/") for t in reversed(out)]

    def line_directive(self, node) -> None:
        if self.t.flag("line_directives") and getattr(node, "range", None) is not None:
            n = self.lineno(node)
            if n != getattr(self.fn, "last_line", None):
                self.fn.last_line = n
                self.fn.lines.append(f'#line {n} "{self.file}"')

    def bind(self, name: str, ty: object) -> str:
        f = self.fn
        cname = name
        taken = {c for s in f.scopes for c, _ in s.values()} | f.used | set(self.fns) | {self.fn_name(d) for d in self.fns.values()}
        n = 1
        while cname in C_KEYWORDS or cname in taken or cname.startswith("kif_") or cname in self.t.data.get("types", {}).values():
            n += 1
            cname = f"{name}_{n}"
        f.used.add(cname)
        f.scopes[-1][name] = (cname, ty)
        return cname

    # ------------------------------------------------------------------ helpers (the prologue)

    def need(self, key: str, text: str) -> str:
        self.helpers.setdefault(key, text)
        return key

    def panic_call(self, msg: str, node) -> str:
        self.need("KIF_PANIC", "")
        return f'KIF_PANIC({c_string(self.file)}, {self.lineno(node)}, {c_string(msg)})'

    def arith_helper(self, op: str, n: str) -> str:
        """The name of `KIF_<OP>_<TYPE>`, writing its definition into the prologue the first time. `+ - *` wrap and take
        two operands; `/ % << >>` can trap and also take the source file and line."""
        t = self.scalar(n)
        name = f"KIF_{ARITH[op]}_{n.upper()}"
        u = "uint32_t" if BITS[n] <= 32 else "uint64_t"
        bits = BITS[n]
        if op in "+-*":
            text = f"KIF_INLINE {t} {name}({t} a, {t} b)\n{{\n\treturn ({t})(({u})a {op} ({u})b);\n}}"
            return self.need(name, text)
        checks = []
        if op in "/%":
            what = "division" if op == "/" else "remainder"
            checks.append(f'if (b == 0) KIF_PANIC(file, line, "integer {what} by zero");')
            if _signed(n):
                checks.append(f'if (a == {self.min_literal(n)} && b == ({t})-1) KIF_PANIC(file, line, "integer overflow in division");')
            sig, ret = f"{t} a, {t} b", f"({t})(a {op} b)"
        else:
            checks.append(f'if (n >= {bits}) KIF_PANIC(file, line, "shift count is not below {bits}");')
            sig = f"{t} a, uint64_t n"
            ret = f"({t})(({u})a << n)" if op == "<<" else f"({t})(a >> n)"
        body = "".join(f"\t{c}\n" for c in checks) + f"\treturn {ret};"
        self.need("KIF_PANIC", "")
        return self.need(name, f"KIF_INLINE {t} {name}({sig}, const char *file, int line)\n{{\n{body}\n}}")

    def plain_wrap(self, op: str, n: str, a: str, b: str) -> Optional[str]:
        """`a op b` written directly for `+ - *` when C already wraps it: unsigned types of 32 bits or more do. `u8`/`u16` are promoted
        to `int`, where `+` and `-` cannot overflow (a cast narrows the result) but `*` can, so `*` keeps its helper. Signed types
        overflow undefined, so they keep their helpers too, unless the target says it builds with `-fwrapv` (`wrapping: builtin`),
        which makes every one of these defined."""
        builtin = self.t.get("wrapping", "helpers") == "builtin"
        if not builtin and (n not in ("u8", "u16", "u32", "u64", "usize") or (BITS[n] < 32 and op == "*")):
            return None
        return f"(({self.scalar(n)})({a} {op} {b}))" if BITS[n] < 32 else f"({a} {op} {b})"

    def min_literal(self, n: str) -> str:
        t = self.scalar(n)
        lo = T.INT_RANGES[n][0]
        return f"(({t})({lo + 1} - 1))" if lo else f"(({t})0)"

    def neg_helper(self, n: str) -> str:
        t = self.scalar(n)
        u = "uint32_t" if BITS[n] <= 32 else "uint64_t"
        name = f"KIF_NEG_{n.upper()}"
        return self.need(name, f"KIF_INLINE {t} {name}({t} a)\n{{\n\treturn ({t})(0u - ({u})a);\n}}")

    def literal(self, v: int, n: str) -> str:
        t = self.scalar(n)
        lo, hi = T.INT_RANGES[n]
        if v == lo and lo < 0:
            return f"(({t})({lo + 1} - 1))"
        if BITS[n] == 64:
            return f"{t[:0]}{'UINT64_C' if not _signed(n) else 'INT64_C'}({v})" if v >= 0 else f"(({t})INT64_C({v}))"
        return f"(({t}){v})" if v >= 0 else f"(({t})({v}))"

    # ------------------------------------------------------------------ expressions

    def const_expr(self, e, hint: Optional[str] = None) -> str:
        """A constant initialiser (a cell's value, a field default) as C: a literal, an enumerator, a named constant or a
        constant integer expression of those. Anything that would need a statement is refused by the caller's context."""
        if isinstance(e, ast.Lit) and e.kind == "bool":
            return "true" if e.value else "false"
        if isinstance(e, ast.Lit) and e.kind == "str":
            self.uses_view = True
            return f"(struct kif_strview){{ {c_string(e.value)}, {len(e.value.encode())} }}"
        if isinstance(e, ast.PathExpr):
            names = [seg.name for seg in e.segments]
            if len(names) == 1 and names[0] in self.consts:
                return self.const_name(self.consts[names[0]])
            if len(names) == 2 and names[0] in self.enums:
                if names[1] not in {m.name.text for m in self.enums[names[0]].members}:
                    raise self.err(f"enum {names[0]} has no member {names[1]}", e)
                return self.enumerator(self.enums[names[0]], names[1])
        elif isinstance(e, (ast.Lit, ast.Unary, ast.Binary)):
            ty = getattr(e, "ty", None)
            n = hint if hint in BITS else (ty.name if ty is not None and ty.name in BITS else "i32")
            return self.literal(self.const_value(e), n)
        raise self.err("a cell's initial value or a field default must be a constant expression the C backend can write as a static initialiser", e)

    def name_of(self, t) -> Optional[str]:
        """The integer type name of a source type, or None."""
        return t.text if isinstance(t, ast.PathType) and t.text in BITS else None

    def cell_kind(self, d: ast.StaticDecl) -> str:
        return "tls" if find_attr(d.attrs, "tls") is not None else "pcpu"

    def cell_defs(self) -> Tuple[List[str], List[str], List[str]]:
        """(block structs, accessors, definitions for the `.c` file) of this module's cells."""
        types, fns, defs = [], [], []
        provider = {"tls": self.t.get("cells.tls", "none"), "pcpu": self.t.get("cells.per_cpu", "none")}
        for kind in ("tls", "pcpu"):
            cs = [d for d in self.cells.values() if self.cell_kind(d) == kind]
            if not cs:
                continue
            pv = provider[kind]
            label = "#[tls]" if kind == "tls" else "#[per_cpu]"
            if pv not in ("hosted", "custom") or (pv == "hosted" and kind == "pcpu"):
                raise self.err(f"{label} cells are not lowered for target {self.t.name!r} (provider {pv!r}): "
                               "the C backend supports the `hosted` TLS provider and `custom` providers", cs[0])
            self.fn = Fn(None)
            tag = f"kif_{kind}_{self.stem}"
            fields = "".join(f"\t{self.decl(d.ty, d.name.text)};\n" for d in cs)
            types.append(f"struct {tag} {{\n{fields}}};")
            inits = ", ".join(self.cell_init(d) for d in cs)
            macro = f"KIF_{kind.upper()}_BLOCK"
            if pv == "hosted":
                types.append(f"extern _Thread_local struct {tag} {tag}_data;")
                fns.append(f"static __inline struct {tag} *{tag}_block(void)\n{{\n\treturn &{tag}_data;\n}}")
                defs.append(f"_Thread_local struct {tag} {tag}_data = {{ {inits} }};")
            else:
                types.append(f"extern const struct {tag} {tag}_image;")
                fns.append(f"static __inline struct {tag} *{tag}_block(void)\n{{\n\treturn {macro}({self.stem});\n}}")
                defs.append(f"/* the template every block starts from; the provider copies it */\nconst struct {tag} {tag}_image = {{ {inits} }};")
        return types, fns, defs

    def cell_init(self, d: ast.StaticDecl) -> str:
        v = d.value
        if isinstance(v, ast.ArrayLit):
            return self.array_init(v, d.ty)
        hint = d.ty.text if isinstance(d.ty, ast.PathType) else None
        c = self.const_expr(v, hint)
        return c

    def errno_value(self, name: str) -> str:
        """The C value of reloco `error::<name>` on this target (negative or positive per `errno.style`)."""
        style = self.t.get("errno.style", "none")
        if style == "none" or not self.t.get("errno.header"):
            raise LowerError(f"target {self.t.name!r} has no errno.style/errno.header, but this file returns errors")
        self.uses_errno = True
        n = (self.t.get("errno.map") or {}).get(name) or ERROR_ERRNO[name]
        return f"(-{n})" if style == "negative" else n

    def opt_tag(self, elem, node=None) -> str:
        """The name part of `struct kif_opt_<tag>` for `optional<elem>` (a Ty or a source type); registers it."""
        if isinstance(elem, T.Ty):
            name, args = elem.name, list(elem.args)
            is_struct = (elem.sym is not None and elem.sym.kind == "struct" and not args) or (elem.sym is None and name in self.structs and not args)
            is_enum = (elem.sym is not None and elem.sym.kind == "enum") or (elem.sym is None and name in self.enums)
        elif isinstance(elem, ast.PathType) and len(elem.segments) == 1:
            name, args = elem.segments[0].name, [a for a in elem.segments[0].args if isinstance(a, ast.PathType)]
            is_struct, is_enum = name in self.structs, name in self.enums
        else:
            name, args, is_struct, is_enum = None, [], False, False
        if name in BITS or name in ("bool", "char") or is_enum or is_struct:
            tag = name
        elif name == "str" and not args:
            tag = "str"
        elif name == "vector" and len(args) == 1:
            tag = "vec_" + self.vec_tag(args[0], node)
        else:
            raise self.err(f"optional<{name}> is not lowered to C: the element is an integer, bool, char, enum, struct, str or vector", node)
        if tag not in self.opts:
            ct = self.cty(elem, node) if isinstance(elem, T.Ty) else self.base_ast(elem)
            self.opts[tag] = (ct, self.drop_stmt("o->v", elem), is_struct)
        return tag

    def opt_struct(self, tag: str) -> str:
        ct = self.opts[tag][0]
        return f"#ifndef KIF_HELPER_opt_{tag}\n#define KIF_HELPER_opt_{tag}\nstruct kif_opt_{tag} {{ bool has; {ct} v; }};\n#endif"

    def opt_defs(self) -> Tuple[List[str], List[str], List[str]]:
        """`struct kif_opt_<tag>` for scalar/str/vector elements, the same for user-struct elements (needs them complete), and the operations."""
        early, late, fns = [], [], []
        for tag, (ct, drop, is_struct) in self.opts.items():
            O = f"struct kif_opt_{tag}"
            if tag not in self.opt_inline:
                (late if is_struct else early).append(self.opt_struct(tag))
            body = f"""static __inline bool kif_opt_{tag}_has_value(const {O} *o)
{{
	return o->has;
}}

static __inline const {ct} *kif_opt_{tag}_value(const {O} *o, const char *file, int line)
{{
	if (!o->has)
		KIF_PANIC(file, line, "optional has no value");
	return &o->v;
}}
"""
            if tag in self.opt_try:
                body += f"""

static __inline int kif_opt_{tag}_try_value(const {O} *o, const {ct} **out)
{{
	if (!o->has)
		return {self.errno_value("not_found")};
	*out = &o->v;
	return 0;
}}"""
            if drop is None:
                body += f"""
static __inline {ct} kif_opt_{tag}_value_or(const {O} *o, {ct} fallback)
{{
	return o->has ? o->v : fallback;
}}

static __inline void kif_opt_{tag}_reset({O} *o)
{{
	o->has = false;
}}"""
            else:
                body += f"""
static __inline void kif_opt_{tag}_drop({O} *o)
{{
	if (o->has) {{
		{drop}
		o->has = false;
	}}
}}

static __inline void kif_opt_{tag}_reset({O} *o)
{{
	kif_opt_{tag}_drop(o);
}}"""
            fns.append(f"#ifndef KIF_HELPER_opt_fns_{tag}\n#define KIF_HELPER_opt_fns_{tag}\n{body}\n#endif")
        return early, late, fns

    def array_init(self, a: ast.ArrayLit, ty=None) -> str:
        """A brace list for an array field; `[0; N]` is `{0}` (C zero-fills the rest)."""
        if a.repeat is not None:
            z = a.elems[0]
            if isinstance(z, ast.Lit) and z.value in (0, False):
                return "{0}"
            raise self.err("only `[0; N]` can initialise an array inside a struct literal here", a)
        return "{ " + ", ".join(self.const_expr(x, self.name_of(getattr(ty, "elem", None))) for x in a.elems) + " }"

    # ------------------------------------------------------------------ statements

    # -- match

    # ------------------------------------------------------------------ items

    def collect(self) -> None:
        seen: Set[int] = set()
        for imp in self.mod.tree.items:
            if isinstance(imp, ast.Import) and imp.path in self.mod.imported:
                other = self.mod.imported[imp.path]
                self.includes.append(f'"{stem_of(other.key)}_proc.h"')
                self.import_module(other, seen, imp)
        if self.externs:
            self.includes += [f'"{n}_if.h"' for n in sorted(self.kobj_ifaces)]  # the only place the kernel headers that declare the extern types come from
        self.import_uses()
        self.qualify(self.mod)
        self.add_items(self.mod, foreign=False)

    def import_module(self, other, seen: Set[int], node) -> None:
        """Make the declarations of an imported module (and of what it imports) known: layouts, signatures, constants."""
        if id(other) in seen:
            return
        seen.add(id(other))
        if other.tree is None or other.header is not None:
            raise self.err(f"{other.name!r} is a precompiled module with no source tree; C lowering needs its .kif source", node)
        if other.namespaced:
            raise self.err(f"{other.name!r} declares a namespace, which C lowering does not support yet", node)
        for sub_ in other.imported.values():
            self.import_module(sub_, seen, node)
        self.qualify(other)
        self.add_items(other, foreign=True)

    def qualify(self, mod) -> None:
        """Rewrite `lib::name` paths of `mod` to what the lowerer looks up: a type or enum loses the module (tags are global),
        a function or constant becomes one segment `lib::name`; a const initializer's bare references to its own constants too."""
        def own(m, name: str) -> bool:
            return any(isinstance(i, (ast.FnDecl, ast.ConstDecl)) and i.name.text == name for i in m.tree.items)

        libs = {m.name: m for m in mod.imported.values() if m.tree is not None}

        def fix(n) -> None:
            segs = n.segments
            if len(segs) >= 2 and segs[0].name in libs:
                lib = libs[segs[0].name]
                rest = segs[1:]
                if len(rest) == 1 and isinstance(n, ast.PathExpr) and own(lib, rest[0].name):
                    segs[:] = [ast.PathSeg(rest[0].range, f"{lib.name}::{rest[0].name}", rest[0].args)]
                else:
                    segs[:] = rest

        for it in mod.tree.items:
            for n in walk(it):
                if isinstance(n, (ast.PathType, ast.PathExpr)):
                    fix(n)
            if isinstance(it, ast.ConstDecl) and mod is not self.mod:
                for n in walk(it.value):
                    if isinstance(n, ast.PathExpr) and len(n.segments) == 1 and n.segments[0].name != it.name.text and own(mod, n.segments[0].name):
                        n.segments[0].name = f"{mod.name}::{n.segments[0].name}"

    def import_uses(self) -> None:
        """`use lib::f [as g]` and `use lib::*` make functions and constants of an import known by their bare name (or alias).
        Types and enums are global tags, so `use` of one needs nothing, except an alias, which C cannot express."""
        for it in self.mod.tree.items:
            if not isinstance(it, ast.Use):
                continue
            for e in it.entries:
                lib = {m.name: m for m in self.mod.imported.values()}.get(e.path[0].text) if e.path else None
                if lib is None or lib is self.mod:
                    continue
                if e.glob:
                    names = [(i.name.text, i.name.text) for i in lib.tree.items if isinstance(i, (ast.FnDecl, ast.ConstDecl))]
                elif len(e.path) == 2:
                    names = [(e.path[1].text, e.alias.text if e.alias else e.path[1].text)]
                else:
                    continue
                for src, dst in names:
                    key = f"{lib.name}::{src}"
                    if key in self.fns:
                        self.fns[dst] = self.fns[key]
                    elif key in self.consts:
                        self.consts[dst] = self.consts[key]
                    elif e.alias is not None and (src in self.structs or src in self.enums):
                        raise self.err(f"`use ... as {dst}` of the type {src!r} cannot be lowered to C (tags are global); drop the alias", e)

    def add_items(self, mod, foreign: bool) -> None:
        stem = stem_of(mod.key)
        for it in mod.tree.items:
            if foreign:
                self.declstem[id(it)] = stem
            if isinstance(it, ast.StructDecl):
                if foreign and it.name.text in self.structs:
                    raise self.err(f"struct {it.name.text!r} is declared by two modules; C tags are global", it)
                self.structs[it.name.text] = it
                if foreign:
                    self.foreign.add(it.name.text)
            elif isinstance(it, ast.EnumDecl):
                self.enums[it.name.text] = it
            elif isinstance(it, ast.FnDecl):
                self.fns[f"{mod.name}::{it.name.text}" if foreign else it.name.text] = it
            elif isinstance(it, ast.ConstDecl):
                self.consts[f"{mod.name}::{it.name.text}" if foreign else it.name.text] = it
            elif isinstance(it, ast.Interface):
                if has_attr(it.attrs, "ops"):
                    (self.foreign_ifaces if foreign else self.ifaces)[it.name.text] = it
            elif isinstance(it, ast.ImplDecl):
                if it.iface is not None and isinstance(it.target, ast.PathType) and not foreign:
                    self.ops_impls.append((it.target.text, it.iface.text, it))
                elif it.iface is not None and foreign and isinstance(it.target, ast.PathType):
                    pass  # the library implements one of its own interfaces: its methods can still be called directly
                elif it.iface is not None or not isinstance(it.target, ast.PathType):
                    if foreign:
                        continue
                    raise self.err("`impl ... for` is not lowered to C", it)
                if it.generics:
                    raise self.err("generic impls are not lowered to C", it)
                owner = it.target.text
                self.impl_structs.add(owner)
                for m in it.items:
                    if not isinstance(m, ast.FnDecl):
                        raise self.err("associated constants are not lowered to C yet", m)
                    self.methods[(owner, m.name.text)] = m
                    self.owner[id(m)] = owner
                    self.impl_attrs[id(m)] = it.attrs
                    if foreign:
                        self.declstem[id(m)] = stem

    def ops_sig(self, m: ast.Method) -> Tuple[str, List[str], List[str]]:
        """(C return type, parameter declarations, argument names) of an interface method without its receiver."""
        if self.result_of(m) is not None:
            raise self.err("a `result<T>` method of an `#[ops]` interface is not lowered to C yet", m)
        if m.static:
            raise self.err("a static method of an `#[ops]` interface is not lowered to C", m)
        ps = [p for p in m.params if not p.receiver]
        ret = self.base_ast(m.ret) if m.ret is not None else "void"
        return ret, [self.decl(p.ty, p.name.text, param=True) for p in ps], [p.name.text for p in ps]

    def ops_defs(self) -> Tuple[List[str], List[str], List[str], List[str]]:
        """`#[ops]` interfaces: the table and handle structs, call wrappers, per-implementer thunks and tables, default bodies."""
        types, protos, fns, bodies = [], [], [], []
        for iname, it in self.ifaces.items():
            for h in it.members:
                if isinstance(h, ast.Hook):
                    raise self.err("`before` / `after` hooks of an `#[ops]` interface are not lowered to C yet", h)
            slots = []
            for m in it.methods:
                ret, params, _ = self.ops_sig(m)
                slots.append(f"\t{ret} (*{m.name.text})({', '.join(['void *self'] + params)});")
            types.append(f"struct {iname}_ops {{\n" + "\n".join(slots) + "\n};")
            types.append(f"struct {iname}_ref {{\n\tvoid *self;\n\tconst struct {iname}_ops *ops;\n}};")
            for m in it.methods:
                ret, params, args = self.ops_sig(m)
                call = f"r.ops->{m.name.text}({', '.join(['r.self'] + args)})"
                fns.append(f"static __inline {ret} {iname}_{m.name.text}({', '.join(['struct ' + iname + '_ref r'] + params)})\n"
                           f"{{\n\t{'' if ret == 'void' else 'return '}{call};\n}}")
                if m.body is None:
                    continue
                mut = any(p.receiver == "mut" for p in m.params)
                me = ast.Param(m.range, ast.Name(m.range, "self"), ast.RefType(m.range, mut, ast.PathType(m.range, [ast.PathSeg(m.range, iname)], dyn=True)))
                f = ast.FnDecl(m.range, [], ast.Name(m.name.range, f"{iname}_{m.name.text}_default"),
                               [me] + [p for p in m.params if not p.receiver], m.ret, m.body)
                self.fixed_name[id(f)] = f"{iname}_{m.name.text}_default"
                protos.append("extern " + self.signature(f, False) + ";")
                self.ops_c.append(self.fn_body(f, False))
        for owner, iname, impl in self.ops_impls:
            it = self.ifaces.get(iname) or self.foreign_ifaces.get(iname)
            if it is None:
                continue  # an impl of a #[kobj] interface becomes a kobj class (lower_kobj)
            own = {x.name.text: x for x in impl.items if isinstance(x, ast.FnDecl)}
            fns.append(f"static __inline struct {iname}_ref {owner}_as_{iname}(struct {owner} *s);")
            slots = []
            for m in it.methods:
                ret, params, args = self.ops_sig(m)
                name = f"{owner}_{iname}_{m.name.text}_thunk"
                if m.name.text in own:
                    if any(p.receiver == "value" for p in own[m.name.text].params):
                        raise self.err("a by-value receiver cannot be called through `dyn`", own[m.name.text])
                    call = f"{owner}_{m.name.text}({', '.join([f'(struct {owner} *)self'] + args)})"
                else:
                    call = f"{iname}_{m.name.text}_default({', '.join([f'{owner}_as_{iname}((struct {owner} *)self)'] + args)})"
                self.ops_c.append(f"static {ret} {name}({', '.join(['void *self'] + params)})\n"
                                  f"{{\n\t{'' if ret == 'void' else 'return '}{call};\n}}")
                slots.append(f"\t.{m.name.text} = {name},")
            self.ops_c.append(f"const struct {iname}_ops {owner}_{iname}_ops = {{\n" + "\n".join(slots) + "\n};")
            fns.append(f"extern const struct {iname}_ops {owner}_{iname}_ops;")
            fns.append(f"static __inline struct {iname}_ref {owner}_as_{iname}(struct {owner} *s)\n"
                       f"{{\n\tstruct {iname}_ref r = {{ s, &{owner}_{iname}_ops }};\n\treturn r;\n}}")
        return types, protos, fns, bodies

    def result_of(self, f: ast.FnDecl):
        """The `T` of a function returning `result<T>` (None for any other return type)."""
        r = f.ret
        if isinstance(r, ast.PathType) and len(r.segments) == 1 and r.segments[0].name == "result":
            a = r.segments[0].args
            return a[0] if a else ast.TupleType(r.range, [])
        return None

    def entry_point(self, fns: List[ast.FnDecl]) -> Optional[ast.FnDecl]:
        """The program entry `fn main()` / `fn main(args: &[str_view]) -> i32` (docs/kif-proc-language.md, Entry point), checked.

        It is lowered as the ordinary function `<stem>_main`; `main_wrapper` adds the C `int main(int argc, char **argv)` around it."""
        mains = [f for f in fns if f.name.text == "main" and f.body is not None and id(f) not in self.owner]
        if not mains:
            return None
        f = mains[0]
        if not self.t.get("hosted", True):
            raise self.err(f"`fn main` is the program entry point, which target {self.t.name!r} (freestanding) does not have", f)
        if f.is_gen or f.generics and any(not g.is_lifetime for g in f.generics):
            raise self.err("`fn main` cannot be a `gen fn` or generic", f)
        ok_ret = f.ret is None or (isinstance(f.ret, ast.TupleType) and not f.ret.elems) or (isinstance(f.ret, ast.PathType) and f.ret.text == "i32")
        if not ok_ret:
            raise self.err("`fn main` returns `()` (exit status 0) or `i32` (the exit status)", f.ret)
        sl = self.slice_ref(f.params[0].ty) if len(f.params) == 1 and not f.params[0].receiver else None
        elem = sl.elem if sl is not None else None
        if f.params and not (sl is not None and not f.params[0].ty.mut and isinstance(elem, ast.PathType) and elem.text == "str_view"):
            raise self.err("`fn main` takes no parameters or one `args: &[str_view]` (argv, program name first)", f)
        self.fixed_name[id(f)] = f"{self.stem}_main"
        return f

    def main_wrapper(self, f: ast.FnDecl) -> str:
        """`int main(int argc, char **argv)`: wraps argv as a slice of string views (no copy) and returns the exit status."""
        call = f"{self.stem}_main({'args_span' if f.params else ''})"
        ret_i32 = isinstance(f.ret, ast.PathType)
        if not f.params:
            return f"int main(int argc, char **argv)\n{{\n\t(void)argc;\n\t(void)argv;\n\t" + (f"return {call};" if ret_i32 else f"{call};\n\treturn 0;") + "\n}"
        self.spans.setdefault("str_view", ("struct kif_strview", False))
        self.uses_view = True
        return ("int main(int argc, char **argv)\n{\n"
                "\tstruct kif_strview args[argc > 0 ? argc : 1];\n"
                "\tstruct kif_cspan_str_view args_span;\n\tint i;\n\n"
                "\tfor (i = 0; i < argc; i++) {\n\t\targs[i].ptr = argv[i];\n\t\targs[i].len = strlen(argv[i]);\n\t}\n"
                "\targs_span.ptr = args;\n\targs_span.len = (size_t)argc;\n\t"
                + (f"return {call};" if ret_i32 else f"{call};\n\treturn 0;") + "\n}")

    def signature(self, f: ast.FnDecl, static: bool, nofacts: bool = False) -> str:
        if f.is_gen:
            raise self.err("`gen fn` runs at generation time and has no C form", f)
        if f.generics and any(not g.is_lifetime for g in f.generics):
            raise self.err("generic functions are not lowered to C", f)
        owner = self.owner.get(id(f))
        self.cur_owner = owner
        self.fn = Fn(f)
        self.build_ir(f)
        self.fn.moved = self.moved_names(f.body)
        inner = self.result_of(f)
        self.fn.result = inner
        params = []
        names: List[str] = []
        for p in f.params:
            if p.receiver:
                if owner is None:
                    raise self.err("a receiver outside an `impl` block", f)
                me = ast.PathType(p.range, [ast.PathSeg(p.range, owner)])
                cname = self.bind("self", ast.RefType(p.range, p.receiver == "mut", me))
                self.fn.value_self = p.receiver == "value"
                if self.fn.value_self and owner in self.dropable:
                    self.own(cname, "self", None, stmt=f"{owner}_drop({cname});")
                params.append(f"{'const ' if p.receiver == 'ref' else ''}struct {owner} *{cname}")
                names.append(cname)
                continue
            cname = self.bind(p.name.text, p.ty)
            names.append(cname)
            params.append(self.decl(p.ty, cname, param=True))
            self.own(cname, p.name.text, p.ty)
        if isinstance(f.ret, ast.ArrayType):
            raise self.err("a function cannot return an array in C", f)
        if inner is not None:
            if not _is_unit(inner):
                if isinstance(inner, ast.ArrayType):
                    raise self.err("a result cannot carry an array in C", f)
                out = f"{self.base_ast(inner)} *kif_out"
                if owner and f.name.text in OUT_FIRST:
                    params.insert(0, out), names.insert(0, "kif_out")
                else:
                    params.append(out), names.append("kif_out")
            ret = "int"
        else:
            ret = self.base_ast(f.ret) if f.ret is not None else "void"
            if isinstance(f.ret, ast.RefType):
                ret = self.decl(f.ret, "").rstrip()
        name = self.fn_name(f)
        if owner in self.owned and f.name.text == "drop":
            name += "_body"  # `S_drop` is the wrapper that also releases the owned fields
        if nofacts:
            name += "__nofacts"
        self.fn.cnames = names
        self.fn.returns_void = ret == "void"
        sig = f"{ret} {name}({', '.join(params) or 'void'})"
        vis = "" if static else self.visibility(f)
        return ("static __inline " if static else "") + (vis + " " if vis else "") + sig

    def visibility_enabled(self) -> bool:
        return bool(self.t.get("visibility.enabled", self.t.get("hosted", True)))

    def visibility(self, f: ast.FnDecl) -> str:
        """The `KIF_VISIBILITY_*` macro (kif_c.h) for an external function, or "" (the compiler's `-fvisibility` then decides).

        `#[visibility(default|hidden|protected|internal)]` is the ELF symbol visibility, so a shared library built with
        `-fvisibility=hidden` exports exactly the functions marked `default`. It is taken from the innermost scope that names one:
        the function, its `impl` block, the file (`#![visibility(..)]`). A function in no such scope gets no attribute, `#[export]`
        included. The target decides whether visibility is used at all (`visibility.enabled`, default: hosted targets); where it is
        disabled (typically kernels) the attribute is checked and then ignored, so one source builds everywhere."""
        chain = [f.attrs, self.impl_attrs.get(id(f), []), self.mod.tree.inner_attrs]
        for attrs in chain:
            a = find_attr(attrs, "visibility")
            if a is None:
                continue
            word = a.args[0].name if len(a.args) == 1 and a.args[0].name else None
            if word not in VISIBILITIES:
                raise self.err(f"`#[visibility(..)]` takes one of {', '.join(VISIBILITIES)}", a)
            if attrs is f.attrs and has_attr(f.attrs, "inline") and id(f) not in self.owner and not has_attr(f.attrs, "export"):
                raise self.err("`#[visibility]` has no effect on a `#[inline]` function (it has no symbol); drop one of the two", a)
            return "KIF_VISIBILITY_" + word.upper() if self.visibility_enabled() else ""
        return ""

    def enum_items(self) -> List[str]:
        """The enums come first: slices, vectors and optionals of them are defined before the user's structs."""
        out: List[str] = []
        for it in self.mod.tree.items:
            if isinstance(it, ast.EnumDecl):
                out += self.enum_decl(it)
        return out

    def type_items(self) -> List[str]:
        out: List[str] = []
        order: List[str] = []
        seen: Set[str] = set()

        def held(f):
            """The struct a field holds by value, directly, in an array, or inside an `optional<..>`."""
            t = f.ty
            while isinstance(t, ast.ArrayType):
                t = t.elem
            if isinstance(t, ast.PathType) and t.segments[0].name == "optional" and len(t.segments[0].args) == 1 \
                    and isinstance(t.segments[0].args[0], ast.PathType):
                t = t.segments[0].args[0]
            return t.text if isinstance(t, ast.PathType) and t.text in self.structs else None

        def visit(name: str) -> None:
            if name in seen or name in self.foreign:
                return
            seen.add(name)
            for f in self.structs[name].fields:
                if held(f):
                    visit(held(f))
            order.append(name)

        for n in self.structs:
            visit(n)
        for n in order:
            for f in self.structs[n].fields:
                t = f.ty
                if isinstance(t, ast.PathType) and t.segments[0].name == "optional" and len(t.segments[0].args) == 1 \
                        and isinstance(t.segments[0].args[0], ast.PathType) and t.segments[0].args[0].text in self.structs:
                    tag = self.opt_tag(t.segments[0].args[0], f)
                    if tag not in self.opt_inline:
                        self.opt_inline.add(tag)
                        out.append(self.opt_struct(tag))
            out += self.struct_decl(self.structs[n])
            if n in self.impl_structs:
                out += self.init_decl(self.structs[n])
        for it in self.mod.tree.items:
            if isinstance(it, ast.ConstDecl):
                out += self.const_decl(it)
        return out

    def enum_decl(self, d: ast.EnumDecl) -> List[str]:
        self.fn = Fn(None)
        nxt = 0
        rows = []
        for m in d.members:
            if m.value is not None:
                v = self.const_int(m.value)
                nxt = v
            rows.append((self.enumerator(d, m.name.text), nxt))
            nxt += 1
        body = ",\n".join(f"{self.doc_comment(m, chr(9))}\t{n} = {v}" for m, (n, v) in zip(d.members, rows))
        out = [f"{self.doc_comment(d)}enum {d.name.text} {{\n{body}\n}};"]
        if has_attr(d.attrs, "typedef"):
            out.append(f"typedef enum {d.name.text} {d.name.text};")
        return out

    def const_int(self, e) -> int:
        if isinstance(e, ast.Lit) and e.kind == "int":
            return e.value
        if isinstance(e, ast.Unary) and e.op == "-":
            return -self.const_int(e.operand)
        raise self.err("enumerator values must be integer literals in the C backend", e)

    def struct_decl(self, d: ast.StructDecl) -> List[str]:
        if d.generics:
            raise self.err("generic structs are not lowered to C", d)
        self.fn = Fn(None)
        attrs = ""
        repr_ = find_attr(d.attrs, "repr")
        if repr_ is not None and any(a.name == "packed" for a in repr_.args or []):
            attrs += " " + (self.t.annotation("packed") or "__attribute__((packed))")
        al = find_attr(d.attrs, "align")
        if al is not None and al.args and al.args[0].value is not None:
            attrs += " " + (self.t.annotation("align") or "__attribute__((aligned({n})))").replace("{n}", str(al.args[0].value.value))
        fields = "".join(f"{self.doc_comment(f, chr(9))}\t{self.decl(f.ty, f.name.text)};\n" for f in d.fields)
        out = [f"{self.doc_comment(d)}struct {d.name.text} {{\n{fields}}}{attrs};"]
        if has_attr(d.attrs, "typedef"):
            out.append(f"typedef struct {d.name.text} {d.name.text};")
        return out

    def init_decl(self, d: ast.StructDecl) -> List[str]:
        """`S_INIT` (the shell: every field at its declared default, else zero) and `S_init(self)` that stores it."""
        self.fn = Fn(None)
        self.cur_owner = d.name.text
        parts = []
        for f in d.fields:
            if f.default is None:
                scalar = isinstance(f.ty, ast.PathType) and (f.ty.text in BITS or f.ty.text in ("bool", "char") or f.ty.text in self.enums)
                parts.append("0" if scalar or (isinstance(f.ty, ast.PathType) and f.ty.segments[0].name in ("c_ptr", "c_cptr", "ptr")) else "{0}")
            elif isinstance(f.default, ast.ArrayLit):
                parts.append(self.array_init(f.default, f.ty))
            else:
                parts.append(self.const_expr(f.default, self.name_of(f.ty)))
        n = d.name.text
        macro = f"{_upper(n)}_INIT"
        return [f"#define {macro} {{ {', '.join(parts)} }}",
                f"static __inline void {n}_init(struct {n} *self)\n{{\n\tstruct {n} shell = {macro};\n\n\t*self = shell;\n}}"]

    def const_decl(self, c: ast.ConstDecl) -> List[str]:
        self.fn = Fn(None)
        if not isinstance(c.ty, ast.PathType) or c.ty.text not in BITS and c.ty.text != "bool":
            raise self.err("only integer and bool constants are lowered to C", c)
        n = c.ty.text
        if n == "bool":
            if not (isinstance(c.value, ast.Lit) and c.value.kind == "bool"):
                raise self.err("a bool constant must be a literal here", c)
            return [f"{self.doc_comment(c)}#define {self.const_name(c)} {'true' if c.value.value else 'false'}"]
        v = self.const_value(c.value)
        return [f"{self.doc_comment(c)}#define {self.const_name(c)} {self.literal(v, n)}"]

    def const_value(self, e) -> int:
        if isinstance(e, ast.Lit) and e.kind == "int":
            return e.value
        if isinstance(e, ast.Unary) and e.op == "-":
            return -self.const_value(e.operand)
        if isinstance(e, ast.Binary) and e.op in "+-*":
            a, b = self.const_value(e.left), self.const_value(e.right)
            return {"+": a + b, "-": a - b, "*": a * b}[e.op]
        if isinstance(e, ast.PathExpr) and len(e.segments) == 1 and e.segments[0].name in self.consts:
            return self.const_value(self.consts[e.segments[0].name].value)
        raise self.err("a constant's value must be an integer expression of literals and constants here", e)

    def guarded_entry(self, f: ast.FnDecl, static: bool) -> str:
        """`F` of a twinned function: it traps when a parameter fact (`#[some]`, `#[non_empty]`, `#[lt_len(v)]`, ..) does not hold, then
        calls `F__nofacts`, the body that assumes them. Every caller outside kif, and every kif call that cannot prove the facts, lands here."""
        sig = self.signature(f, static)
        fn = self.fn
        # the C names follow the declared parameters, with `kif_out` first or last
        ordered = [n for n in fn.cnames if n != "kif_out"]
        by = {p.name.text: n for p, n in zip(f.params, ordered)}
        lines = []

        def length(p) -> str:
            c = by[p.name.text]
            return f"{c}->len" if isinstance(p.ty, ast.RefType) and not isinstance(p.ty.inner, ast.SliceType) else f"{c}.len"

        for r in facts.requirements(f):
            c = by[r.param.name.text]
            deref = isinstance(r.param.ty, ast.RefType)
            if r.fact == "some":
                bad = f"!{c}->has" if deref else f"!{c}.has"
            elif r.fact == "non_null":
                bad = f"{c} == NULL"
            elif r.other is not None:
                bad = f"(uint64_t){c} >= (uint64_t){length(r.other)}"
            else:
                bad = f"(uint64_t){length(r.param)} <= (uint64_t){r.n}"
            msg = f"argument '{r.param.name.text}' of {f.name.text}() violates #[{r.fact}{f'({r.other.name.text})' if r.other is not None else ''}]"
            lines.append(f"\tif ({bad})\n\t\t{self.panic_call(msg, f)};")
        call = f"{self.fn_name(f)}__nofacts({', '.join(fn.cnames)})"
        lines.append(f"\t{'' if fn.returns_void else 'return '}{call};")
        return sig + "\n{\n" + "\n".join(lines) + "\n}"

    def fn_body(self, f: ast.FnDecl, static: bool) -> str:
        sig = self.signature(f, static, nofacts=self.twinned(f))
        fn = self.fn
        self.line_directive(f)
        ret_unit = f.ret is None or (isinstance(f.ret, ast.TupleType) and not f.ret.elems)
        if f.body is None:
            return ""
        if fn.ir is None:
            raise self.err(f"`{f.name.text}` is not lowered to C: the IR builder does not handle it", f)
        text = ir_c.emit_body(self, fn.ir, fn.cnames)
        if text is None:
            raise self.err(f"`{f.name.text}` is not lowered to C: {self.ir_bails.get(fn.ir.name, 'unsupported')}", f)
        stamp = f"/* kif: ir {fn.ir_hash[:23]} */\n" if fn.ir_hash else ""
        section = self.t.get("attest.section")
        entry = ""
        if section and fn.ir_hash:
            # survives into the object file: read it with `kif verify-object` or `strings`; the comment above is not trusted by itself
            entry = (f"\nstatic const char kif_attest_{self.fn_name(f)}[] __attribute__((used, section({c_string(section)}))) = "
                     f"{c_string(f'{fn.ir_hash} {f.name.text}')};")
        return f"{self.doc_comment(f)}{stamp}{sig}\n{{\n{text}\n}}{entry}"

    def enum_cast_helpers(self) -> None:
        for name in sorted(self.cast_enums):
            d = self.enums[name]
            self.fn = Fn(None)
            cases = "\n".join(f"\tcase {v}: return {n};" for n, v in self.enum_rows(d))
            t = f"enum {name}"
            self.need(f"KIF_TOENUM_{name}", f"KIF_INLINE {t} KIF_TOENUM_{name}(int64_t v, const char *file, int line)\n{{\n"
                      f"\tswitch (v) {{\n{cases}\n\tdefault: break;\n\t}}\n"
                      f"\tKIF_PANIC(file, line, \"value is not a declared enumerator of {name}\");\n\treturn ({t})0;\n}}")
            self.need("KIF_PANIC", "")

    def enum_rows(self, d: ast.EnumDecl) -> List[Tuple[str, int]]:
        nxt, rows = 0, []
        for m in d.members:
            if m.value is not None:
                nxt = self.const_int(m.value)
            rows.append((self.enumerator(d, m.name.text), nxt))
            nxt += 1
        return rows

    # ------------------------------------------------------------------ vector<T>

    VEC_RESULT = {"try_push": "T", "try_pop": "", "try_reserve": "n", "try_resize": "n", "try_insert_at": "iT", "try_erase_at": "i",
                  "try_clone": "", "try_create": "n", "try_at": "i"}
    VEC_PLAIN = ("len", "is_empty", "capacity", "clear", "drop")

    def vec_tag(self, elem, node=None, secure: bool = False) -> str:
        """The name part of `struct kif_vec_<tag>` for element type `elem` (a Ty or a source type); registers the element.
        `secure` is for `secure_vector<T>`: `struct kif_svec_<tag>`, with its own wiping helpers."""
        if secure:
            plan = self.t.wipe()
            if plan.error or self.t.get("alloc.malloc") is None or self.t.get("alloc.free") is None:
                raise self.err("KIFW008: secure_vector needs a target with alloc.malloc, alloc.free and a working wipe" + (f" ({plan.error})" if plan.error else ""), node)
            self.uses_wipe = True
            tag = self.vec_tag(elem, node)
            self.svecs.setdefault(tag, self.vecs[tag])
            return tag
        name = elem.name if isinstance(elem, T.Ty) else (elem.text if isinstance(elem, ast.PathType) else None)
        if name in BITS or name in ("bool", "char"):
            self.vecs.setdefault(name, (self.scalar(name), False))
            return name
        known = (isinstance(elem, T.Ty) and elem.sym is not None and elem.sym.kind == "struct") or name in self.structs
        if known and name in self.dropable:
            raise self.err(f"vector<{name}> is not lowered to C: {name} owns resources and a vector would not drop its elements", node)
        if known:
            self.vecs.setdefault(name, (f"struct {name}", True))
            return name
        if (isinstance(elem, T.Ty) and elem.sym is not None and elem.sym.kind == "enum") or name in self.enums:
            self.vecs.setdefault(name, (f"enum {name}", False))
            return name
        raise self.err(f"vector<{name}> is not lowered to C: the element is an integer, bool, char, enum or a struct", node)

    STR_RESULT = {"try_create": "v", "try_clone": "", "try_reserve": "n", "try_assign": "v", "try_append": "v", "try_push": "c",
                  "try_pop": "", "try_insert": "nv", "try_erase": "nn"}
    STR_PLAIN = ("len", "is_empty", "capacity", "view", "clear", "drop", "starts_with", "contains")
    VIEW_PLAIN = ("len", "is_empty", "starts_with", "ends_with", "contains")

    def vec_defs(self) -> Tuple[List[str], List[str]]:
        """The `struct kif_vec_<tag>` definitions (prologue) and their operations (after the user types, which they need complete)."""
        s_structs: List[str] = []
        s_fns: List[str] = []
        for tag, (ct, is_struct) in self.spans.items():
            fwd = f"{ct};\n" if is_struct else ""
            s_structs.append(f"#ifndef KIF_HELPER_span_{tag}\n#define KIF_HELPER_span_{tag}\n{fwd}struct kif_span_{tag} {{ {ct} *ptr; size_t len; }};\n"
                             f"struct kif_cspan_{tag} {{ const {ct} *ptr; size_t len; }};\n#endif")
        if not self.vecs and not self.svecs:
            return s_structs, s_fns
        free = self.t.get("alloc.free")
        nomem = self.errno_value("allocation_failed")
        empty = self.errno_value("container_empty")
        oor = self.errno_value("out_of_range")
        structs, fns = [], []
        rows = [(tag, ec, st, False) for tag, (ec, st) in self.vecs.items()] + [(tag, ec, st, True) for tag, (ec, st) in self.svecs.items()]
        for tag, ec, is_struct, secure in rows:
            pre = "svec" if secure else "vec"
            V = f"struct kif_{pre}_{tag}"
            P = f"kif_{pre}_{tag}"
            fwd = f"struct {tag};\n" if is_struct else ""
            structs.append(f"#ifndef KIF_HELPER_{pre}_{tag}\n#define KIF_HELPER_{pre}_{tag}\n{fwd}{V} {{ {ec} *ptr; size_t len; size_t cap; }};\n#endif")
            f = free.replace("{ptr}", "v->ptr")
            m = self.t.get("alloc.malloc").replace("{size}", "n * sizeof(" + ec + ")")
            r = (self.t.get("alloc.realloc") or "").replace("{ptr}", "v->ptr").replace("{size}", "n * sizeof(" + ec + ")")
            plain_f_reserve = f"""static __inline int {P}_try_reserve({V} *v, size_t n)
{{
	{ec} *p;

	if (n <= v->cap)
		return 0;
	if (n > (size_t)-1 / sizeof({ec}))
		return {nomem};
	p = ({ec} *){r};
	if (p == NULL)
		return {nomem};
	v->ptr = p;
	v->cap = n;
	return 0;
}}"""
            plain_f_pop = f"""static __inline int {P}_try_pop({V} *v)
{{
	if (v->len == 0)
		return {empty};
	v->len--;
	return 0;
}}"""
            plain_f_resize = f"""static __inline int {P}_try_resize({V} *v, size_t n)
{{
	size_t i;
	int rc = {P}_try_reserve(v, n);

	if (rc != 0)
		return rc;
	for (i = v->len; i < n; i++) {{
		{ec} zero = {{0}};

		v->ptr[i] = zero;
	}}
	v->len = n;
	return 0;
}}"""
            plain_f_erase = f"""static __inline int {P}_try_erase_at({V} *v, size_t i)
{{
	if (i >= v->len)
		return {oor};
	for (; i + 1 < v->len; i++)
		v->ptr[i] = v->ptr[i + 1];
	v->len--;
	return 0;
}}"""
            plain_f_clear = f"""static __inline void {P}_clear({V} *v)
{{
	v->len = 0;
}}"""
            plain_f_drop = f"""static __inline void {P}_drop({V} *v)
{{
	{f};
	v->ptr = NULL;
	v->len = 0;
	v->cap = 0;
}}"""

            wipe = lambda p, n: self.t.wipe().macro + f"({p}, {n})"
            sec_f_reserve = f"""static __inline int {P}_try_reserve({V} *v, size_t n)
{{
	{ec} *p;
	size_t i;

	if (n <= v->cap)
		return 0;
	if (n > (size_t)-1 / sizeof({ec}))
		return {nomem};
	p = ({ec} *){m};
	if (p == NULL)
		return {nomem};
	for (i = 0; i < v->len; i++)
		p[i] = v->ptr[i];
	if (v->ptr != NULL) {{
		/* kif: secure wipe: the old block of a secure_vector, capacity bytes, before it is freed (never realloc) */
		{wipe("v->ptr", "v->cap * sizeof(" + ec + ")")};
		{f};
	}}
	v->ptr = p;
	v->cap = n;
	return 0;
}}"""
            sec_f_pop = f"""static __inline int {P}_try_pop({V} *v)
{{
	if (v->len == 0)
		return {empty};
	v->len--;
	/* kif: secure wipe: the element a secure_vector pop vacated */
	{wipe("&v->ptr[v->len]", "sizeof(" + ec + ")")};
	return 0;
}}"""
            sec_f_resize = f"""static __inline int {P}_try_resize({V} *v, size_t n)
{{
	size_t i;
	int rc = {P}_try_reserve(v, n);

	if (rc != 0)
		return rc;
	if (n < v->len) {{
		/* kif: secure wipe: the elements a secure_vector shrink vacated */
		{wipe("&v->ptr[n]", "(v->len - n) * sizeof(" + ec + ")")};
	}}
	for (i = v->len; i < n; i++) {{
		{ec} zero = {{0}};

		v->ptr[i] = zero;
	}}
	v->len = n;
	return 0;
}}"""
            sec_f_erase = f"""static __inline int {P}_try_erase_at({V} *v, size_t i)
{{
	if (i >= v->len)
		return {oor};
	for (; i + 1 < v->len; i++)
		v->ptr[i] = v->ptr[i + 1];
	v->len--;
	/* kif: secure wipe: the slot a secure_vector erase vacated */
	{wipe("&v->ptr[v->len]", "sizeof(" + ec + ")")};
	return 0;
}}"""
            sec_f_clear = f"""static __inline void {P}_clear({V} *v)
{{
	if (v->len != 0) {{
		/* kif: secure wipe: every element of a cleared secure_vector */
		{wipe("v->ptr", "v->len * sizeof(" + ec + ")")};
	}}
	v->len = 0;
}}"""
            sec_f_drop = f"""static __inline void {P}_drop({V} *v)
{{
	if (v->ptr != NULL) {{
		/* kif: secure wipe: the whole capacity of a dropped secure_vector, not just its length */
		{wipe("v->ptr", "v->cap * sizeof(" + ec + ")")};
		{f};
	}}
	v->ptr = NULL;
	v->len = 0;
	v->cap = 0;
}}"""
            if secure:
                f_reserve, f_pop, f_resize, f_erase, f_clear, f_drop = sec_f_reserve, sec_f_pop, sec_f_resize, sec_f_erase, sec_f_clear, sec_f_drop
            else:
                f_reserve, f_pop, f_resize, f_erase, f_clear, f_drop = plain_f_reserve, plain_f_pop, plain_f_resize, plain_f_erase, plain_f_clear, plain_f_drop
            body = f"""{f_reserve}

static __inline int {P}_try_create({V} *out, size_t n)
{{
	out->ptr = NULL;
	out->len = 0;
	out->cap = 0;
	return {P}_try_reserve(out, n);
}}

static __inline int {P}_try_clone({V} *out, const {V} *v)
{{
	size_t i;
	int rc = {P}_try_create(out, v->len);

	if (rc != 0)
		return rc;
	for (i = 0; i < v->len; i++)
		out->ptr[i] = v->ptr[i];
	out->len = v->len;
	return 0;
}}

static __inline int {P}_grow({V} *v)
{{
	if (v->len < v->cap)
		return 0;
	if (v->cap > (size_t)-1 / 2)
		return {nomem};
	return {P}_try_reserve(v, v->cap != 0 ? v->cap * 2 : 4);
}}

static __inline int {P}_try_push({V} *v, {ec} value)
{{
	int rc = {P}_grow(v);

	if (rc != 0)
		return rc;
	v->ptr[v->len++] = value;
	return 0;
}}

{f_pop}

{f_resize}

static __inline int {P}_try_insert_at({V} *v, size_t i, {ec} value)
{{
	size_t j;
	int rc;

	if (i > v->len)
		return {oor};
	rc = {P}_grow(v);
	if (rc != 0)
		return rc;
	for (j = v->len; j > i; j--)
		v->ptr[j] = v->ptr[j - 1];
	v->ptr[i] = value;
	v->len++;
	return 0;
}}

{f_erase}

static __inline int {P}_try_at(const {V} *v, size_t i, const {ec} **out)
{{
	if (i >= v->len)
		return {oor};
	*out = &v->ptr[i];
	return 0;
}}

static __inline size_t {P}_len(const {V} *v)
{{
	return v->len;
}}

static __inline bool {P}_is_empty(const {V} *v)
{{
	return v->len == 0;
}}

static __inline size_t {P}_capacity(const {V} *v)
{{
	return v->cap;
}}

{f_clear}

{f_drop}"""
            fns.append(f"#ifndef KIF_HELPER_{pre}_{tag}_fns\n#define KIF_HELPER_{pre}_{tag}_fns\n{body}\n#endif")
        return s_structs + structs, s_fns + fns

    def prologue(self) -> List[str]:
        """Helpers specific to this file (enum conversions); everything shared lives in kif_c.h."""
        if "KIF_PANIC" in self.helpers and self.t.get("panic.macro") is None:
            raise LowerError(f"target {self.t.name!r} has no panic.macro, but this file can trap")
        return [f"#ifndef KIF_HELPER_{k}\n#define KIF_HELPER_{k}\n{v}\n#endif" for k, v in self.helpers.items() if k.startswith("KIF_TOENUM_")]

    @staticmethod
    def secure(sd) -> bool:
        return any(a.name.text == "secure_wipe" for a in sd.attrs)

    def wipe_stmts(self, n: str, sd, moved: bool = False) -> List[str]:
        """The generated drop of a `#[secure_wipe]` struct `n`: one commented wipe per field that is not `#[wipe_exempt]`, in declaration order
        (rule W5); a field that is itself a secure struct is wiped by its own drop. Exempt fields are released afterwards like any field."""
        plan = self.t.wipe()
        if plan.error:
            raise LowerError(f"KIFW006: target {self.t.name!r} cannot wipe, but struct {n} is #[secure_wipe]: {plan.error}")
        self.uses_wipe = True
        vol = any(a.name.text == "secure_wipe" and any(x.name == "volatile" for x in a.args or []) for a in sd.attrs)
        self.uses_wipe_volatile |= vol
        suffix = "_VOLATILE" if vol else ""
        out = []
        for f in sd.fields:
            if any(a.name.text == "wipe_exempt" for a in f.attrs):
                continue
            nm = f"self->{f.name.text}"
            if not moved and isinstance(f.ty, ast.PathType) and f.ty.segments[-1].name == "secure_vector":
                continue  # its own drop wipes the whole buffer and then frees it; clearing the header first would lose the pointer
            inner = self.structs.get(f.ty.text) if isinstance(f.ty, ast.PathType) else None
            if inner is not None and self.secure(inner):
                out.append(f"	/* kif: secure wipe: {n}.{f.name.text} ({f.ty.text}), by its own generated drop ({self.file}:{f.name.range.start.line + 1}) */")
                out.append(f"	kif_wipe_{f.ty.text}(&{nm});" if moved else f"	{f.ty.text}_drop(&{nm});")
                continue
            out.append(f"	/* kif: secure wipe{" (volatile)" if vol else ""}: {n}.{f.name.text} ({ir.type_text(f.ty)}), generated drop of #[secure_wipe] struct {n} ({self.file}:{f.name.range.start.line + 1}) */")
            out.append(f"	{plan.macro}(&{nm}, sizeof({nm}));")
        return out

    def drop_wrappers(self) -> Tuple[List[str], List[str]]:
        """`S_drop_fields` and `S_drop` for structs that own something: the user's `drop` body first, then each owned field, in reverse order (C++ order)."""
        protos, bodies = [], []
        for n, fields in self.owned.items():
            if n in self.foreign:
                continue
            user = self.methods.get((n, "drop")) is not None
            rel = []
            self.cur_owner = n
            for f in reversed(fields):
                self.fn = Fn(None)
                nm = f"self->{f.name.text}"
                if f.ty.segments[0].name == "ptr":
                    rel.append(f"\t{self.drop_stmt(nm, f.ty)}\n\t{nm} = NULL;")
                else:
                    rel.append("\t" + self.drop_stmt(nm, f.ty))
            if self.secure(self.structs[n]):
                rel = self.wipe_stmts(n, self.structs[n]) + rel
                protos.append(f"static __inline void kif_wipe_{n}(struct {n} *self);")
                bodies.append(f"/* the moved-from slot of a {n}: its fields are wiped, nothing is released (the destination owns it) */\nstatic __inline void kif_wipe_{n}(struct {n} *self)\n{{\n"
                              + "\n".join(self.wipe_stmts(n, self.structs[n], True)) + "\n}")
            call = f"\t{n}_drop_body(self);\n" if user else ""
            protos += [f"static __inline void {n}_drop_fields(struct {n} *self);", f"static __inline void {n}_drop(struct {n} *self);"]
            bodies.append(f"static __inline void {n}_drop_fields(struct {n} *self)\n{{\n" + "\n".join(rel) + "\n}")
            bodies.append(f"static __inline void {n}_drop(struct {n} *self)\n{{\n{call}\t{n}_drop_fields(self);\n}}")
        return protos, bodies

    def drop_stmt(self, c: str, t) -> Optional[str]:
        """The C statement that drops the object `c` of type `t` (a checker `Ty` or a source type), or None when it owns nothing."""
        el = t.args[0] if isinstance(t, T.Ty) and t.name == "[;]" and t.args else (t.elem if isinstance(t, ast.ArrayType) else None)
        if el is not None and self.drop_stmt("x", el) is not None:
            raise LowerError("an array of owning elements (str, vector, ptr, a struct with drop) is not lowered to C: its elements would not be "
                             "dropped; hold them in a struct with an `impl` block that owns them")
        if isinstance(t, T.Ty):
            n, args = t.name, t.args
            if n == "str" and not args:
                self.uses_str = self.uses_view = True
                return f"kif_str_drop(&{c});"
            if n == "secure_vector" and args and args[0] != T.UNKNOWN:
                return f"kif_svec_{self.vec_tag(args[0], None, True)}_drop(&{c});"
            if n == "vector":
                if not args or args[0] == T.UNKNOWN:
                    raise LowerError("a vector whose element type is not known cannot be dropped: give it a type annotation")
                return f"kif_vec_{self.vec_tag(args[0])}_drop(&{c});"
            if n == "ptr" and args:
                return f"{self.heap_struct(args[0].name)}_delete({c});"
            if n == "optional" and len(args) == 1:
                tag = self.opt_tag(args[0])
                return f"kif_opt_{tag}_drop(&{c});" if self.opts[tag][1] else None
            if n in self.dropable and t.sym is not None and t.sym.kind == "struct" and not args:
                return f"{n}_drop(&{c});"
            return None
        if isinstance(t, ast.PathType) and len(t.segments) == 1:
            sg = t.segments[0]
            if sg.name == "str" and not sg.args:
                self.uses_str = self.uses_view = True
                return f"kif_str_drop(&{c});"
            if sg.name == "vector" and len(sg.args) == 1:
                return f"kif_vec_{self.vec_tag(sg.args[0], t)}_drop(&{c});"
            if sg.name == "secure_vector" and len(sg.args) == 1:
                return f"kif_svec_{self.vec_tag(sg.args[0], t, True)}_drop(&{c});"
            if sg.name == "ptr" and len(sg.args) == 1 and isinstance(sg.args[0], ast.PathType):
                return f"{self.heap_struct(sg.args[0].text, t)}_delete({c});"
            if sg.name == "optional" and len(sg.args) == 1:
                tag = self.opt_tag(sg.args[0], t)
                return f"kif_opt_{tag}_drop(&{c});" if self.opts[tag][1] else None
            name = self.cur_owner if sg.name == "Self" else sg.name
            if name in self.dropable and not sg.args:
                return f"{name}_drop(&{c});"
        return None

    def owning_field(self, ty) -> bool:
        if not (isinstance(ty, ast.PathType) and len(ty.segments) == 1):
            return False
        sg = ty.segments[0]
        if sg.name == "optional" and len(sg.args) == 1:
            return self.drop_stmt("x", sg.args[0]) is not None
        return (sg.name == "str" and not sg.args) or (sg.name in ("vector", "secure_vector", "ptr") and len(sg.args) == 1) \
            or (sg.name in self.dropable and not sg.args)

    @staticmethod
    def moved_names(node) -> Set[str]:
        """Source names of the locals a function body moves out of (the checker marked those uses)."""
        out: Set[str] = set()
        stack, seen = [node], set()
        while stack:
            n = stack.pop()
            if isinstance(n, (list, tuple)):
                stack.extend(n)
                continue
            if not isinstance(n, ast.Node) or id(n) in seen:
                continue
            seen.add(id(n))
            if isinstance(n, ast.PathExpr) and getattr(n, "moved", False) and len(n.segments) == 1:
                out.add(n.segments[0].name)
            stack.extend(getattr(n, f.name) for f in dc_fields(n) if f.name != "range")
        return out

    def heap_struct(self, name: str, node=None) -> str:
        """The struct a `ptr<name>` points to; registers it for `S_try_new` / `S_delete`. Only structs with an `impl` block are heap objects."""
        if name in BITS or name in ("bool", "char") or name in self.enums:
            raise self.err(f"ptr<{name}> is not lowered to C: a heap scalar cannot be told from a non-owning `c_ptr<{name}>` by its C type; "
                           f"wrap it in a struct with an `impl` block (`struct cell {{ v: {name} }}`) and use `ptr<cell>`", node)
        if name not in self.structs or name not in self.impl_structs:
            raise self.err(f"ptr<{name}> is lowered to C only for a struct with an `impl` block (it supplies the constructor and `drop`)", node)
        if not self.t.get("alloc.malloc") or not self.t.get("alloc.free"):
            raise self.err(f"target {self.t.name!r} has no alloc.malloc/alloc.free, so ptr<T> cannot be lowered", node)
        self.heap.add(name)
        return name

    def heap_defs(self) -> Tuple[List[str], List[str]]:
        """`S_try_new(struct S **out, args)` (allocate, then `S_try_construct_at` or `S_init`) and `S_delete(struct S *)` (`S_drop`, then free)."""
        protos, bodies = [], []
        nomem = None
        for n in sorted(self.heap):
            ctor = self.methods.get((n, "try_construct_at"))
            self.fn = Fn(None)
            self.cur_owner = n
            params = [self.decl(q.ty, f"kif_a{i}", param=True) for i, q in enumerate(ctor.params[1:])] if ctor is not None else []
            call = f"{self.fn_name(ctor)}(p{''.join(f', kif_a{i}' for i in range(len(params)))})" if ctor is not None else None
            nomem = nomem or self.errno_value("allocation_failed")
            alloc = self.t.get("alloc.malloc").replace("{size}", "sizeof *p")
            free = self.t.get("alloc.free").replace("{ptr}", "p")
            sig = f"static __inline int {n}_try_new(struct {n} **out{''.join(', ' + q for q in params)})"
            build = (f"\trc = {call};\n" if call else f"\t{n}_init(p);\n\trc = 0;\n")
            bodies.append(f"#ifndef KIF_HEAP_{n.upper()}\n#define KIF_HEAP_{n.upper()}\n{sig}\n{{\n\tstruct {n} *p = (struct {n} *){alloc};\n\tint rc;\n\n\tif (p == NULL)\n\t\treturn {nomem};\n{build}"
                          f"\tif (rc != 0) {{\n\t\t{free};\n\t\treturn rc;\n\t}}\n\t*out = p;\n\treturn 0;\n}}")
            drop = f"\t{n}_drop(p);\n" if (n in self.owned or self.methods.get((n, "drop")) is not None) else ""
            bodies.append(f"static __inline void {n}_delete(struct {n} *p)\n{{\n\tif (p == NULL)\n\t\treturn;\n{drop}\t{free};\n}}\n#endif")
            protos += [sig + ";", f"static __inline void {n}_delete(struct {n} *p);"]
        return protos, bodies

    def find_dropable(self) -> None:
        """Fill `owned` and `dropable` (after `collect`): the structs for which `S_drop` exists."""
        self.dropable = {n for n in self.structs if self.methods.get((n, "drop")) is not None}
        for n, sd in self.structs.items():
            if self.secure(sd):
                self.owned[n] = [f for f in sd.fields if self.owning_field(f.ty)]  # the generated drop exists even with nothing to release
                self.dropable.add(n)
        changed = True
        while changed:
            changed = False
            for n, sd in self.structs.items():
                if n in self.owned:
                    continue
                own = [f for f in sd.fields if self.owning_field(f.ty)]
                if own:
                    self.owned[n] = own
                    self.dropable.add(n)
                    changed = True

    def common_header(self) -> str:
        """`kif_c.h`: what generated files share, written from the target (panic, allocator, errno, scalar types): the
        trapping and checked-arithmetic helpers, `struct kif_strview` and the owned `struct kif_str` with their operations."""
        self.fn = Fn(None)
        for op in ARITH:
            for n in BITS:
                self.arith_helper(op, n)
        for n in BITS:
            self.neg_helper(n)
        for k, v in HELPER_TEXT.items():
            self.need(k, v)
        self.uses_view = True
        owned = bool(self.t.get("alloc.realloc") and self.t.get("alloc.free") and self.t.get("errno.header")
                     and self.t.get("errno.style", "none") != "none")
        self.uses_str = owned
        structs, fns = self.str_defs()
        incs = self.t.includes("header", kobj=False)
        for key in ("panic.header", "alloc.header", "errno.header"):
            h = self.t.get(key)
            if h and h not in incs:
                incs.append(h)
        m = self.t.macro("panic", file="file", line="line", msg="msg")
        panic = ("#ifndef KIF_PANIC\n#define KIF_PANIC(file, line, msg) do { (void)(file); (void)(line); " + (m or "(void)(msg)") + "; KIF_UNREACHABLE(); } while (0)\n#endif")
        if m is not None:
            panic = "#if defined(__GNUC__)\n#define KIF_UNREACHABLE() __builtin_unreachable()\n#else\n#define KIF_UNREACHABLE() ((void)0)\n#endif\n" + panic
        else:
            panic = "#define KIF_UNREACHABLE() ((void)0)\n" + panic
        head = [f"/* kif_c.h: the code generated files share, written by kifc for target {self.t.name}; do not edit */",
                "#ifndef KIF_C_H", "#define KIF_C_H", ""]
        for k, v in sorted((self.t.data.get("defines") or {}).items()):
            head.append(f"#define {k} {v}")
        head += [f"#include {i}" for i in incs]
        head.append("#ifndef KIF_INLINE\n#if defined(__GNUC__)\n#define KIF_INLINE static __inline __attribute__((always_inline))\n"
                    "#else\n#define KIF_INLINE static __inline\n#endif\n#endif")
        if self.visibility_enabled():
            head.append("#ifndef KIF_VISIBILITY_DEFAULT\n#if defined(__GNUC__)\n"
                    + "\n".join(f'#define KIF_VISIBILITY_{v.upper()} __attribute__((visibility("{v}")))' for v in VISIBILITIES)
                    + "\n#else\n" + "\n".join(f"#define KIF_VISIBILITY_{v.upper()}" for v in VISIBILITIES) + "\n#endif\n#endif")
        if self.t.get("alloc.declare"):
            head.append(self.t.get("alloc.declare"))
        helpers = [v for k, v in self.helpers.items() if k != "KIF_PANIC"]
        return "\n\n".join(["\n".join(head), panic] + helpers + structs + fns + ["#endif /* KIF_C_H */"]) + "\n"

    def resolve(self, e) -> ast.FnDecl:
        """The declaration a call, `Type::f(..)`, `Self::f(..)` or `x.m(..)` refers to."""
        if isinstance(e, ast.MethodCall):
            t = self.ty_of(e.receiver)
            while t.name == "&" or (t.name == "ptr" and t.args):
                t = t.args[0]
            d = self.methods.get((t.name, e.name.text))
            if d is None:
                raise self.err(f"no method {e.name.text!r} of {t} to lower", e)
            return d
        c = e.callee if isinstance(e, ast.Call) else None
        if isinstance(c, ast.PathExpr):
            names = [s.name for s in c.segments]
            if len(names) == 1 and self.lookup(names[0]) is None and names[0] in self.fns:
                return self.fns[names[0]]
            if len(names) == 2:
                d = self.methods.get((self.cur_owner if names[0] == "Self" else names[0], names[1]))
                if d is not None:
                    return d
        raise self.err("this call is not lowered to C yet (only free functions, `Type::f(..)` and methods are)", e)

    def str_defs(self) -> Tuple[List[str], List[str]]:
        """`struct kif_strview` / `struct kif_str` and their operations (the owned string needs the allocator)."""
        structs = ["#ifndef KIF_HELPER_strview\n#define KIF_HELPER_strview\nstruct kif_strview { const char *ptr; size_t len; };\n#endif"]
        view_fns = """static __inline size_t kif_strview_len(struct kif_strview s)
{
	return s.len;
}

static __inline bool kif_strview_is_empty(struct kif_strview s)
{
	return s.len == 0;
}

static __inline bool kif_strview_at(struct kif_strview s, struct kif_strview t, size_t at)
{
	size_t i;

	if (t.len > s.len || at > s.len - t.len)
		return false;
	for (i = 0; i < t.len; i++)
		if (s.ptr[at + i] != t.ptr[i])
			return false;
	return true;
}

static __inline bool kif_strview_starts_with(struct kif_strview s, struct kif_strview t)
{
	return kif_strview_at(s, t, 0);
}

static __inline bool kif_strview_ends_with(struct kif_strview s, struct kif_strview t)
{
	return t.len <= s.len && kif_strview_at(s, t, s.len - t.len);
}

static __inline bool kif_strview_contains(struct kif_strview s, struct kif_strview t)
{
	size_t i;

	if (t.len > s.len)
		return false;
	for (i = 0; i <= s.len - t.len; i++)
		if (kif_strview_at(s, t, i))
			return true;
	return false;
}"""
        fns = [f"#ifndef KIF_HELPER_strview_fns\n#define KIF_HELPER_strview_fns\n{view_fns}\n#endif"]
        if not self.uses_str:
            return structs, fns
        structs.append("#ifndef KIF_HELPER_str\n#define KIF_HELPER_str\nstruct kif_str { char *ptr; size_t len; size_t cap; };\n#endif")
        nomem = self.errno_value("allocation_failed")
        empty = self.errno_value("container_empty")
        oor = self.errno_value("out_of_range")
        r = self.t.get("alloc.realloc").replace("{ptr}", "s->ptr").replace("{size}", "n + 1")
        f = self.t.get("alloc.free").replace("{ptr}", "s->ptr")
        body = f"""/* cap counts the terminating NUL; a zeroed struct is an empty string with ptr == NULL, every try_ operation leaves it terminated. */
static __inline int kif_str_try_reserve(struct kif_str *s, size_t n)
{{
	char *p;

	if (n < s->cap)
		return 0;
	if (n == (size_t)-1)
		return {nomem};
	p = (char *){r};
	if (p == NULL)
		return {nomem};
	if (s->ptr == NULL)
		p[0] = '\\0';
	s->ptr = p;
	s->cap = n + 1;
	return 0;
}}

static __inline int kif_str_grow(struct kif_str *s, size_t extra)
{{
	size_t need, n;

	if (extra > (size_t)-1 - s->len - 1)
		return {nomem};
	need = s->len + extra;
	if (need < s->cap)
		return 0;
	n = s->cap > 16 ? s->cap : 16;
	if (n < need)
		n = need;
	else if (n <= (size_t)-1 / 2 && n < need * 2)
		n = n * 2 > need ? n * 2 : need;
	return kif_str_try_reserve(s, n);
}}

static __inline struct kif_strview kif_str_view(const struct kif_str *s)
{{
	struct kif_strview v;

	v.ptr = s->ptr;
	v.len = s->len;
	return v;
}}

static __inline void kif_str_copy_in(struct kif_str *s, size_t at, struct kif_strview t)
{{
	size_t i;

	for (i = 0; i < t.len; i++)
		s->ptr[at + i] = t.ptr[i];
}}

static __inline int kif_str_try_assign(struct kif_str *s, struct kif_strview t)
{{
	int rc = kif_str_try_reserve(s, t.len);

	if (rc != 0)
		return rc;
	kif_str_copy_in(s, 0, t);
	s->len = t.len;
	s->ptr[s->len] = '\\0';
	return 0;
}}

static __inline int kif_str_try_create(struct kif_str *out, struct kif_strview t)
{{
	out->ptr = NULL;
	out->len = 0;
	out->cap = 0;
	return kif_str_try_assign(out, t);
}}

static __inline int kif_str_try_clone(struct kif_str *out, const struct kif_str *s)
{{
	return kif_str_try_create(out, kif_str_view(s));
}}

static __inline int kif_str_try_append(struct kif_str *s, struct kif_strview t)
{{
	int rc = kif_str_grow(s, t.len);

	if (rc != 0)
		return rc;
	kif_str_copy_in(s, s->len, t);
	s->len += t.len;
	s->ptr[s->len] = '\\0';
	return 0;
}}

static __inline int kif_str_try_push(struct kif_str *s, char c)
{{
	struct kif_strview t;

	t.ptr = &c;
	t.len = 1;
	return kif_str_try_append(s, t);
}}

static __inline int kif_str_try_pop(struct kif_str *s)
{{
	if (s->len == 0)
		return {empty};
	s->len--;
	s->ptr[s->len] = '\\0';
	return 0;
}}

static __inline int kif_str_try_insert(struct kif_str *s, size_t at, struct kif_strview t)
{{
	size_t j;
	int rc;

	if (at > s->len)
		return {oor};
	rc = kif_str_grow(s, t.len);
	if (rc != 0)
		return rc;
	for (j = s->len + 1; j > at; j--)
		s->ptr[j - 1 + t.len] = s->ptr[j - 1];
	kif_str_copy_in(s, at, t);
	s->len += t.len;
	return 0;
}}

static __inline int kif_str_try_erase(struct kif_str *s, size_t at, size_t count)
{{
	size_t j;

	if (at > s->len)
		return {oor};
	if (count > s->len - at)
		count = s->len - at;
	if (count == 0)
		return 0;
	for (j = at; j + count <= s->len; j++)
		s->ptr[j] = s->ptr[j + count];
	s->len -= count;
	return 0;
}}

static __inline size_t kif_str_len(const struct kif_str *s)
{{
	return s->len;
}}

static __inline bool kif_str_is_empty(const struct kif_str *s)
{{
	return s->len == 0;
}}

static __inline size_t kif_str_capacity(const struct kif_str *s)
{{
	return s->cap != 0 ? s->cap - 1 : 0;
}}

static __inline void kif_str_clear(struct kif_str *s)
{{
	s->len = 0;
	if (s->ptr != NULL)
		s->ptr[0] = '\\0';
}}

static __inline bool kif_str_starts_with(const struct kif_str *s, struct kif_strview t)
{{
	return kif_strview_starts_with(kif_str_view(s), t);
}}

static __inline bool kif_str_contains(const struct kif_str *s, struct kif_strview t)
{{
	return kif_strview_contains(kif_str_view(s), t);
}}

static __inline void kif_str_drop(struct kif_str *s)
{{
	{f};
	s->ptr = NULL;
	s->len = 0;
	s->cap = 0;
}}"""
        fns.append(f"#ifndef KIF_HELPER_str_fns\n#define KIF_HELPER_str_fns\n{body}\n#endif")
        return structs, fns

    def lookup(self, name: str):
        for s in reversed(self.fn.scopes):
            if name in s:
                return s[name]
        return None

    def ty_of(self, e) -> T.Ty:
        t = getattr(e, "ty", None)
        if t is None or t == T.UNKNOWN:
            raise self.err("the type of this expression is not known to the checker", e)
        return t

    def run(self) -> Dict[str, str]:
        self.collect()
        self.find_dropable()
        for sd in self.structs.values():
            for f in sd.fields:
                if isinstance(f.ty, ast.ArrayType) and sd.name.text not in self.foreign:
                    self.drop_stmt("x", f.ty)
        for it in self.mod.tree.items:
            if isinstance(it, ast.StaticDecl):
                self.cells[it.name.text] = it
        guard = _upper(self.stem) + "_PROC_H"
        enums = self.enum_items()
        types = self.type_items()
        protos, bodies, exports = [], [], []
        fns = []
        for it in self.mod.tree.items:
            if isinstance(it, ast.ExternFn):
                raise self.err("`extern` declarations are not lowered to C yet", it)
            if isinstance(it, ast.ImplDecl):
                if it.iface is not None and it.iface.text not in self.ifaces and it.iface.text not in self.foreign_ifaces \
                        and it.iface.text not in self.kobj_ifaces:
                    raise self.err(f"`impl {it.iface.text} for ...` is lowered only for `#[ops]` interfaces", it)
                fns += [m for m in it.items if isinstance(m, ast.FnDecl)]
                continue
            if isinstance(it, ast.StaticDecl):
                continue
            if isinstance(it, ast.Interface) and (it.name.text in self.ifaces or has_attr(it.attrs, "kobj")):  # kobj: lower_kobj
                continue
            if isinstance(it, ast.ExternType) and it.path.text in self.externs:
                continue  # an opaque C type: its header comes with the kobj interface (`<iface>_if.h`) or the target's includes
            if isinstance(it, (ast.Interface, ast.ExternType, ast.ExternImpl, ast.TypeAlias)):
                raise self.err(f"{type(it).__name__} is not lowered to C yet", it)
            if isinstance(it, ast.FnDecl):
                fns.append(it)
        entry = self.entry_point(fns)
        for it in fns:
            if it.body is None:
                continue
            # `#[export]` functions and every method are external symbols defined in `<stem>_proc.c` (a method's address goes into
            # tables such as kobj classes); free functions are `static __inline` in the header
            external = not has_attr(it.attrs, "inline") or has_attr(it.attrs, "export") or id(it) in self.owner
            protos.append(self.doc_comment(it) + ("extern " if external else "static __inline ") + self.signature(it, False) + ";")
            if self.twinned(it):
                protos.append(("extern " if external else "static __inline ") + self.signature(it, False, nofacts=True) + ";")
            if it.is_gen:
                continue
            if self.twinned(it):
                (exports if external else bodies).append(self.guarded_entry(it, not external))
            (exports if external else bodies).append(self.fn_body(it, not external))
        if entry:
            exports.append(self.main_wrapper(entry))
        ops_types, ops_protos, ops_fns, ops_bodies = self.ops_defs()
        protos += ops_protos
        bodies = ops_bodies + bodies
        dp, db = self.drop_wrappers()
        protos += dp
        bodies += db
        hp, hb = self.heap_defs()
        protos += hp
        bodies += hb
        self.enum_cast_helpers()
        cell_types, cell_fns, cell_defs = self.cell_defs() if self.cells else ([], [], [])
        head = [f"/* generated from {self.file} by kifc (target {self.t.name}); do not edit */"]
        if self.note:
            head.append(f"/* kif: {self.note} */")
        head += [f"#ifndef {guard}", f"#define {guard}", ""]
        for k, v in sorted((self.t.data.get("defines") or {}).items()):
            head.append(f"#define {k} {v}")
        vec_structs, vec_fns = self.vec_defs()
        opt_early, opt_late, opt_fns = self.opt_defs()
        incs = self.t.includes("header", kobj=False)
        if cell_types and self.t.get("cells.header") and self.t.get("cells.header") not in incs:
            incs.append(self.t.get("cells.header"))
        if (vec_structs or self.heap) and self.t.get("alloc.header") not in incs:
            incs.append(self.t.get("alloc.header"))
        if self.uses_errno and self.t.get("errno.header") not in incs:
            incs.append(self.t.get("errno.header"))
        wipe = self.t.wipe()
        if self.uses_wipe and wipe.header and wipe.header not in incs:
            incs.append(wipe.header)
        head += [f"#include {i}" for i in incs] + ['#include "kif_c.h"'] + [f"#include {i}" for i in self.includes]
        if self.uses_wipe:
            head += ["", f"#ifndef {wipe.macro}", f"#define {wipe.macro}(p, n) do {{ {wipe.body}; }} while (0)", "#endif", ""]
            if self.uses_wipe_volatile:
                head += [f"#ifndef {wipe.macro}_VOLATILE", f"#define {wipe.macro}_VOLATILE(p, n) do {{ {wipe.volatile_body}; }} while (0)", "#endif", ""]
        if self.uses_str and not (self.t.get("alloc.realloc") and self.t.get("alloc.free")):
            raise LowerError(f"target {self.t.name!r} has no alloc.realloc/alloc.free, so str cannot be lowered")
        if self.uses_str:
            self.errno_value("allocation_failed")
        parts = ["\n".join(head)] + self.prologue() + enums + vec_structs + opt_early + types + ops_types + cell_types + opt_late + vec_fns + [p for p in protos if p] + ops_fns + opt_fns + cell_fns + bodies + [f"#endif /* {guard} */"]
        files = {f"{self.stem}_proc.h": "\n\n".join(parts) + "\n"}
        if exports or cell_defs or self.ops_c:
            files[f"{self.stem}_proc.c"] = (f'/* generated from {self.file} by kifc; do not edit */\n#include "{self.stem}_proc.h"\n'
                                           + ("#include <string.h>\n" if entry and entry.params else "") + "\n"
                                           + "\n\n".join(cell_defs + self.ops_c + exports) + "\n")
        return files


def common_header(target) -> str:
    """The text of `kif_c.h` for `target`: shared by every file generated for it (see `Lowerer.common_header`)."""
    return Lowerer(None, target, "kif_c").common_header()


def lower(mod, target, stem: str, filename: Optional[str] = None, note: str = "") -> Dict[str, str]:
    """Lower a checked module to `{file name: text}`. `note` (e.g. "target=c profile=debug") is recorded in the header."""
    return Lowerer(mod, target, stem, filename, note).run()
