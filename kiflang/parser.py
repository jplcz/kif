# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Recovering parser (docs/kif-proc-language.md section 3.1).

`parse(text)` never raises. A syntax error is recorded as KIF001 and the parser resynchronises (at the next
`;`/`}` inside a block, at the next item keyword at the top level), so a half-typed file still yields a tree for
outlines and navigation. The parser is syntactic only: it does not resolve names, validate attributes or check types.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from . import ast
from . import diagnostics as dg
from . import lexer as lx
from .lexer import EOF, IDENT, INT, LIFETIME, PUNCT, STRING, SYSPATH, VERBATIM, KEYWORDS
from .source import Pos, Range, span

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

# Words that start a top-level item; used to resynchronise after an error.
_ITEM_START = frozenset(
    "namespace import use include header code interface foreign enum struct block fn gen const impl extern type private".split()
)
_STMT_ITEM_START = frozenset("fn gen const struct enum impl extern type private".split())
_BLOCK_LIKE = frozenset("if while loop for match unsafe cfg".split())
_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
_BINARY_PREC = {
    "||": 3, "&&": 4,
    "==": 5, "!=": 5, "<": 5, ">": 5, "<=": 5, ">=": 5,
    "|": 6, "^": 7, "&": 8, "<<": 9, ">>": 9, "+": 10, "-": 10, "*": 11, "/": 11, "%": 11,
}
_COMPARE = frozenset({"==", "!=", "<", ">", "<=", ">="})
_EXPR_END = frozenset({")", "]", "}", ",", ";", "=>"})


class _Stop(Exception):
    """Internal: abandon the current construct (the error is already recorded)."""


def valid_name(text: str) -> bool:
    """Names of interfaces, methods, enums, structs, blocks and registers: snake_case, no `__`, no trailing `_`."""
    return bool(_NAME_RE.fullmatch(text)) and "__" not in text and not text.endswith("_") and text not in KEYWORDS


def parse(text: str) -> "Tuple[ast.KifFile, List[dg.Diagnostic]]":
    tokens, diags = lx.lex(text)
    p = _Parser(text, tokens, diags)
    return p.parse_file(), diags


class _Parser:
    def __init__(self, text: str, tokens: List[lx.Token], diags: List[dg.Diagnostic]) -> None:
        self.lines = text.split("\n")
        self.toks = tokens
        self.i = 0
        self.d = diags
        self.prev_end = Pos(0, 0)

    # ------------------------------------------------------------------ token helpers

    @property
    def tok(self) -> lx.Token:
        return self.toks[self.i]

    def peek(self, n: int = 1) -> lx.Token:
        return self.toks[min(self.i + n, len(self.toks) - 1)]

    def at(self, text: str) -> bool:
        t = self.tok
        return t.kind in (PUNCT, IDENT) and t.text == text

    def at_word(self, text: str, n: int = 0) -> bool:
        t = self.peek(n)
        return t.kind == IDENT and t.text == text

    def at_punct(self, text: str, n: int = 0) -> bool:
        t = self.peek(n)
        return t.kind == PUNCT and t.text == text

    def advance(self) -> lx.Token:
        t = self.tok
        if t.kind != EOF:
            self.i += 1
        self.prev_end = t.range.end
        return t

    def eat(self, text: str) -> Optional[lx.Token]:
        return self.advance() if self.at(text) else None

    def describe(self, t: lx.Token) -> str:
        return "end of file" if t.kind == EOF else f"{t.text!r}"

    def fail(self, message: str, code: str = "KIF001", rng: Optional[Range] = None) -> None:
        self.d.append(dg.error(rng or self.tok.range, code, message))
        raise _Stop()

    def expect(self, text: str, what: str = "") -> lx.Token:
        if not self.at(text):
            self.fail(f"expected {what or repr(text)}, found {self.describe(self.tok)}")
        return self.advance()

    def error(self, rng: Range, code: str, message: str) -> None:
        self.d.append(dg.error(rng, code, message))

    def adjacent(self, n: int = 0) -> bool:
        """True if the token at offset n ends exactly where the next one starts (no whitespace between them)."""
        return self.peek(n).range.end == self.peek(n + 1).range.start

    def slice(self, start: Pos, end: Pos) -> str:
        if start.line == end.line:
            return self.lines[start.line][start.col : end.col]
        parts = [self.lines[start.line][start.col :]]
        parts += self.lines[start.line + 1 : end.line]
        parts.append(self.lines[end.line][: end.col])
        return "\n".join(parts)

    # ------------------------------------------------------------------ names

    def ident(self, what: str, validate: bool = False) -> ast.Name:
        t = self.tok
        if t.kind != IDENT:
            self.fail(f"expected {what} name, found {self.describe(t)}")
        if t.text in KEYWORDS:
            self.fail(f"{t.text!r} is a keyword and cannot be used as a {what} name", "KIF003")
        if validate and not valid_name(t.text):
            self.error(t.range, "KIF003",
                       f"invalid {what} name {t.text!r}: must match [a-z][a-z0-9_]*, with no '__' or trailing '_'")
        self.advance()
        return ast.Name(t.range, t.text)

    def path_text(self, what: str) -> ast.Name:
        """`a::b::c` as one name (used by `extern` declarations that name C++ entities)."""
        first = self.ident(what)
        parts = [first.text]
        last = first.range
        while self.at("::") and self.peek().kind == IDENT:
            self.advance()
            n = self.ident(what)
            parts.append(n.text)
            last = n.range
        return ast.Name(span(first.range, last), "::".join(parts))

    # ------------------------------------------------------------------ file and items

    def parse_file(self) -> ast.KifFile:
        f = ast.KifFile(Range(Pos(0, 0), self.toks[-1].range.end))
        while self.tok.kind != EOF:
            start = self.i
            try:
                if self.at("#!["):
                    f.inner_attrs.extend(self.attributes(True))
                    continue
                item = self.item()
                if item is not None:
                    f.items.append(item)
            except _Stop:
                self.recover_item(start)
        return f

    def recover_item(self, start: int) -> None:
        depth = 0
        first = True
        while self.tok.kind != EOF:
            t = self.tok
            if t.kind == PUNCT:
                if t.text in ("{", "(", "["):
                    depth += 1
                elif t.text in ("}", ")", "]"):
                    depth = max(0, depth - 1)
                    if depth == 0 and t.text == "}":
                        self.advance()
                        return
                elif t.text == ";" and depth == 0:
                    self.advance()
                    return
                elif t.text in ("#[", "#![") and depth == 0 and not first:
                    return
            elif t.kind == IDENT and depth == 0 and t.text in _ITEM_START and (self.i > start or not first):
                if not first or self.i > start:
                    return
            self.advance()
            first = False

    def attributes(self, inner: bool = False) -> List[ast.Attribute]:
        """A run of `#[..]` groups, or of `#![..]` groups when inner is set."""
        out: List[ast.Attribute] = []
        while self.at("#![" if inner else "#["):
            opener = self.advance()
            inner = opener.text == "#!["
            while True:
                out.append(self.attribute(opener.range, inner))
                if not self.eat(","):
                    break
                if self.at("]"):
                    break
            self.expect("]", "']' to close the attribute")
        return out

    def attribute(self, start: Range, inner: bool) -> ast.Attribute:
        t = self.tok
        if t.kind != IDENT:
            self.fail(f"expected attribute name, found {self.describe(t)}")
        self.advance()  # attribute names may coincide with keywords (`unsafe(relocatable)`, `export`, `cfg`)
        name = ast.Name(t.range, t.text)
        attr = ast.Attribute(t.range, name, None, None, inner)
        if self.at("("):
            open_tok = self.advance()
            close_i = self.matching(self.i - 1)
            if close_i is None:
                self.fail("unterminated attribute arguments")
            close_tok = self.toks[close_i]
            attr.raw = self.slice(open_tok.range.end, close_tok.range.start).strip()
            attr.args = self.try_attr_args(close_i)
            self.i = close_i
            self.expect(")")
            attr.range = span(t.range, close_tok.range)
        elif self.at("="):
            self.advance()
            lit = self.literal()
            attr.raw = self.slice(lit.range.start, lit.range.end)
            attr.args = [ast.AttrArg(lit.range, None, lit)]
            attr.range = span(t.range, lit.range)
        return attr

    def matching(self, open_i: int) -> Optional[int]:
        """Index of the token that closes the bracket at open_i, or None."""
        depth = 0
        for j in range(open_i, len(self.toks)):
            t = self.toks[j]
            if t.kind == PUNCT:
                if t.text in ("(", "[", "{", "#[", "#!["):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    depth -= 1
                    if depth == 0:
                        return j
            if t.kind == EOF:
                return None
        return None

    def try_attr_args(self, end: int) -> Optional[List[ast.AttrArg]]:
        """Structured arguments up to token index `end`, or None when they are not names/literals (`self(struct s *)`)."""
        save, ndiag = self.i, len(self.d)
        try:
            args = self.attr_arglist(end)
            return args if self.i == end else None
        except _Stop:
            return None
        finally:
            del self.d[ndiag:]
            self.i = save

    def attr_arglist(self, end: int) -> List[ast.AttrArg]:
        args: List[ast.AttrArg] = []
        while self.i < end:
            args.append(self.attr_arg(end))
            if self.i < end:
                self.expect(",")
        return args

    def attr_arg(self, end: int) -> ast.AttrArg:
        t = self.tok
        if t.kind in (INT, STRING) or t.text in ("true", "false"):
            lit = self.literal()
            return ast.AttrArg(lit.range, None, lit)
        if t.kind != IDENT:
            self.fail("not a structured attribute argument")
        self.advance()
        arg = ast.AttrArg(t.range, t.text)
        if self.at("("):
            close = self.matching(self.i)
            if close is None or close > end:
                self.fail("unterminated")
            self.advance()
            arg.args = self.attr_arglist(close)
            self.expect(")")
        elif self.at("="):
            self.advance()
            arg.value = self.literal()
        arg.range = span(t.range, Range(self.prev_end, self.prev_end))
        return arg

    ATTR_ITEMS = (
        ast.FnDecl, ast.ConstDecl, ast.StaticDecl, ast.TypeAlias, ast.ImplDecl, ast.ExternFn, ast.ExternType, ast.ExternImpl,
        ast.Interface, ast.ForeignClass, ast.EnumDecl, ast.StructDecl, ast.BlockDecl,
    )

    def item(self, in_block: bool = False) -> Optional[ast.Item]:
        attrs = self.attributes()
        t = self.tok
        if t.kind == EOF:
            if attrs:
                self.fail("attributes must be followed by an item")
            return None
        if t.kind != IDENT:
            self.fail(f"expected an item, found {self.describe(t)}")
        word = t.text
        if word in ("namespace", "import", "use", "include", "header", "code"):
            if attrs:
                self.error(attrs[0].range, "KIF086", f"attributes cannot be applied to {word!r}")
            return {
                "namespace": self.namespace, "import": self.import_, "use": self.use, "include": self.include,
                "header": self.verbatim, "code": self.verbatim,
            }[word]()
        if word == "interface":
            return self.interface(attrs)
        if word == "foreign":
            return self.foreign(attrs)
        if word == "enum":
            return self.enum(attrs)
        if word == "struct":
            return self.struct(attrs)
        if word == "block" and self.peek().kind == IDENT:
            return self.block_decl(attrs)
        if word == "impl":
            return self.impl(attrs)
        if word == "extern":
            return self.extern(attrs)
        if word == "type":
            return self.type_alias(attrs)
        private = bool(self.eat("private"))
        if self.at("const"):
            return self.const(attrs, private)
        if self.tok.kind == IDENT and self.tok.text == "static":
            return self.static(attrs, private)
        if self.at("fn") or self.at("gen"):
            return self.fn(attrs, private)
        self.fail(f"expected an item, found {self.describe(self.tok)}")

    # -- simple items

    def namespace(self) -> ast.Namespace:
        kw = self.advance()
        path = [self.ident("namespace", True)]
        while self.eat("::"):
            path.append(self.ident("namespace", True))
        semi = self.expect(";")
        return ast.Namespace(span(kw.range, semi.range), path)

    def import_(self) -> ast.Import:
        kw = self.advance()
        s = self.tok
        if s.kind != STRING:
            self.fail("expected a string path after 'import'")
        self.advance()
        semi = self.expect(";")
        return ast.Import(span(kw.range, semi.range), s.text[1:-1])

    def use(self) -> ast.Use:
        kw = self.advance()
        entries: List[ast.UseEntry] = []
        self.use_tree([], entries)
        semi = self.expect(";")
        return ast.Use(span(kw.range, semi.range), entries)

    def use_tree(self, prefix: List[ast.Name], out: List[ast.UseEntry]) -> None:
        path = list(prefix)
        while True:
            if self.at("{"):
                self.advance()
                while not self.at("}"):
                    self.use_tree(path, out)
                    if not self.eat(","):
                        break
                self.expect("}")
                return
            if self.at("*"):
                star = self.advance()
                if not path:
                    self.fail("a glob import needs a module path: use module::*;", rng=star.range)
                out.append(ast.UseEntry(span(path[0].range, star.range), path, None, True))
                return
            path.append(self.ident("module or item"))
            if not self.eat("::"):
                break
        alias = self.ident("alias") if self.eat("as") else None
        out.append(ast.UseEntry(span(path[0].range, (alias or path[-1]).range), path, alias))

    def include(self) -> ast.Include:
        kw = self.advance()
        s = self.tok
        if s.kind not in (STRING, SYSPATH):
            self.fail("expected <path> or \"path\" after 'include'")
        self.advance()
        semi = self.expect(";")
        return ast.Include(span(kw.range, semi.range), s.text[1:-1], s.kind == SYSPATH)

    def verbatim(self) -> ast.Verbatim:
        kw = self.advance()
        target = None
        if self.at("("):
            self.advance()
            tag = self.tok
            if tag.kind != IDENT or not self.at_punct(")", 1):
                self.fail(f"expected 'c' or 'cxx' in {kw.text}(..)")
            self.advance()
            self.advance()
            target = tag.text
            if kw.text in ("m_prolog", "m_epilog"):
                self.error(tag.range, "KIF090", f"{kw.text} is C text and takes no language tag")
            elif target not in ("c", "cxx"):
                self.error(tag.range, "KIF090", f"unknown language {target!r} in {kw.text}(..); use 'c' or 'cxx'")
        if not (self.at("{") and self.peek().kind == VERBATIM):
            self.fail(f"'{kw.text}' must be followed by '{{' ending its line (the body is raw text up to a line with '}}' in column 1)")
        self.advance()
        body = self.advance()
        close = self.expect("}")
        return ast.Verbatim(span(kw.range, close.range), kw.text, body.text, kw.range, target)

    def type_alias(self, attrs: List[ast.Attribute]) -> ast.TypeAlias:
        kw = self.advance()
        name = self.ident("type")
        generics = self.generic_params()
        self.expect("=")
        ty = self.type_()
        semi = self.expect(";")
        return ast.TypeAlias(span(kw.range, semi.range), attrs, name, generics, ty)

    def const(self, attrs: List[ast.Attribute], private: bool) -> ast.ConstDecl:
        kw = self.expect("const")
        name = self.ident("constant")
        self.expect(":", "':' and a type")
        ty = self.type_()
        self.expect("=")
        value = self.expr()
        semi = self.expect(";")
        return ast.ConstDecl(span(kw.range, semi.range), attrs, name, ty, value, private)

    def static(self, attrs: List[ast.Attribute], private: bool) -> ast.StaticDecl:
        kw = self.advance()  # `static` is contextual: it is only a keyword at the start of an item
        name = self.ident("cell")
        self.expect(":", "':' and a type")
        ty = self.type_()
        self.expect("=")
        value = self.expr()
        semi = self.expect(";")
        return ast.StaticDecl(span(kw.range, semi.range), attrs, name, ty, value, private)

    # -- functions

    def fn(self, attrs: List[ast.Attribute], private: bool, body_required: bool = True) -> ast.FnDecl:
        first = self.tok
        is_gen = bool(self.eat("gen"))
        self.expect("fn")
        name = self.ident("function")
        decl = self.fn_rest(first, attrs, name, private, body_required)
        decl.is_gen = is_gen
        return decl

    def fn_rest(self, first: lx.Token, attrs: List[ast.Attribute], name: ast.Name, private: bool,
                body_required: bool) -> ast.FnDecl:
        generics = self.generic_params()
        params, variadic = self.params()
        ret = self.type_() if self.eat("->") else None
        body = None
        if self.at("{"):
            body = self.block()
        elif self.at(";"):
            self.advance()  # a precompiled module (`.kifm`) declares its functions without bodies; the checker decides
        elif body_required:
            self.fail(f"expected a function body '{{', found {self.describe(self.tok)}")
        else:
            self.expect(";")
        return ast.FnDecl(span(first.range, Range(self.prev_end, self.prev_end)), attrs, name, params, ret, body,
                          generics, private, False, variadic)

    def params(self) -> "Tuple[List[ast.Param], bool]":
        self.expect("(")
        out: List[ast.Param] = []
        variadic = False
        while not self.at(")"):
            if self.at("..."):
                self.advance()
                variadic = True
                break
            out.append(self.param())
            if not self.eat(","):
                break
        self.expect(")")
        return out, variadic

    def param(self) -> ast.Param:
        attrs = self.attributes()
        p = self.param_core()
        p.attrs = attrs
        return p

    def param_core(self) -> ast.Param:
        t = self.tok
        if self.at("&"):
            amp = self.advance()
            lt = self.lifetime() if self.tok.kind == LIFETIME else None
            mut = bool(self.eat("mut"))
            s = self.expect("self", "'self'")
            p = ast.Param(span(amp.range, s.range), ast.Name(s.range, "self"), None, "mut" if mut else "ref")
            p.lifetime = lt
            return p
        if self.at("self"):
            self.advance()
            return ast.Param(t.range, ast.Name(t.range, "self"), None, "value")
        name = self.ident("parameter")
        self.expect(":", "':' and a type")
        ty = self.type_()
        return ast.Param(span(name.range, ty.range), name, ty)

    def generic_params(self) -> List[ast.GenericParam]:
        if not self.at("<"):
            return []
        self.advance()
        out: List[ast.GenericParam] = []
        while not self.at(">"):
            start = self.tok
            if start.kind == LIFETIME:
                lt = self.lifetime()
                gp = ast.GenericParam(start.range, ast.Name(lt.range, lt.text), is_lifetime=True)
                if self.eat(":"):
                    if self.tok.kind != LIFETIME:
                        self.fail("expected a lifetime after ':' in a lifetime parameter")
                    gp.outlives.append(self.lifetime())
                    while self.eat("+"):
                        gp.outlives.append(self.lifetime())
                gp.range = span(start.range, Range(self.prev_end, self.prev_end))
                out.append(gp)
                if not self.eat(","):
                    break
                continue
            if self.eat("const"):
                name = self.ident("generic parameter")
                self.expect(":", "':' and a type")
                gp = ast.GenericParam(start.range, name, [], None, self.type_())
                if self.eat("="):
                    gp.default = self.primary()
            else:
                name = self.ident("generic parameter")
                gp = ast.GenericParam(start.range, name)
                if self.eat(":"):
                    gp.bounds.append(self.path_type())
                    while self.eat("+"):
                        gp.bounds.append(self.path_type())
                if self.eat("="):
                    gp.default = self.type_()
            gp.range = span(start.range, Range(self.prev_end, self.prev_end))
            out.append(gp)
            if not self.eat(","):
                break
        self.expect(">")
        return out

    # -- impl and extern

    def impl(self, attrs: List[ast.Attribute]) -> ast.ImplDecl:
        kw = self.advance()
        generics = self.generic_params()
        target = self.type_()
        iface = None
        if self.eat("for"):
            if not isinstance(target, ast.PathType):
                self.fail("the interface in 'impl <interface> for <type>' must be a name", rng=target.range)
            iface, target = target, self.type_()
        self.expect("{")
        items: List = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated 'impl' block")
            start = self.i
            try:
                a = self.attributes()
                private = bool(self.eat("private"))
                if self.at("const"):
                    items.append(self.const(a, private))
                else:
                    items.append(self.fn(a, private))
            except _Stop:
                self.recover_member(start)
        close = self.expect("}")
        return ast.ImplDecl(span(kw.range, close.range), attrs, target, items, iface, generics)

    def recover_member(self, start: int) -> None:
        """Skip to the end of the current member (a `;` or a balanced `{..}`), stopping before the closing `}`."""
        self._skip_member()
        if self.i == start and self.tok.kind != EOF:
            self.advance()  # a stray ')' or ']' must not stall the caller's loop

    def _skip_member(self) -> None:
        depth = 0
        while self.tok.kind != EOF:
            t = self.tok
            if t.kind == PUNCT:
                if t.text in ("{", "(", "["):
                    depth += 1
                elif t.text in ("}", ")", "]"):
                    if depth == 0:
                        return
                    depth -= 1
                    if depth == 0 and t.text == "}":
                        self.advance()
                        return
                elif t.text == ";" and depth == 0:
                    self.advance()
                    return
            self.advance()

    def extern(self, attrs: List[ast.Attribute]) -> ast.Item:
        kw = self.advance()
        abi_tok = self.tok
        if abi_tok.kind != STRING or abi_tok.text not in ('"C"', '"C++"'):
            self.fail("expected \"C\" or \"C++\" after 'extern'")
        self.advance()
        abi = abi_tok.text[1:-1]
        t = self.tok
        if self.at("fn") or self.at("macro"):
            self.advance()
            name = self.path_text("function")
            decl = self.fn_rest(t, [], name, False, False)
            return ast.ExternFn(span(kw.range, decl.range), attrs, abi, t.text, decl)
        if self.at("type"):
            self.advance()
            path = self.bare_path()
            generics = self.generic_params()  # declared parameters, not arguments: `K, V, A = allocator_ref`
            self.expect(";")
            return ast.ExternType(span(kw.range, Range(self.prev_end, self.prev_end)), attrs, abi, path, generics)
        if self.at("impl"):
            self.advance()
            generics = self.generic_params()
            target = self.type_()
            self.expect("{")
            fns: List[ast.FnDecl] = []
            while not self.at("}"):
                if self.tok.kind == EOF:
                    self.fail("unterminated 'extern impl' block")
                start = self.i
                try:
                    a = self.attributes()
                    first = self.tok
                    self.expect("fn")
                    name = self.ident("function")
                    fns.append(self.fn_rest(first, a, name, False, False))
                except _Stop:
                    self.recover_member(start)
            close = self.expect("}")
            return ast.ExternImpl(span(kw.range, close.range), attrs, abi, target, fns, generics)
        self.fail("expected 'fn', 'macro', 'type' or 'impl' after extern \"" + abi + "\"")

    # -- interfaces

    def interface(self, attrs: List[ast.Attribute]) -> ast.Interface:
        kw = self.advance()
        name = self.ident("interface", True)
        self.expect("{")
        members: List = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated interface (missing '}')")
            start = self.i
            try:
                members.append(self.interface_member())
            except _Stop:
                self.recover_member(start)
        close = self.expect("}")
        return ast.Interface(span(kw.range, close.range), attrs, name, members)

    def interface_member(self):
        a = self.attributes()
        t = self.tok
        if t.kind == IDENT and t.text in ("m_prolog", "m_epilog", "header", "code"):
            return self.verbatim()
        if t.kind == IDENT and t.text in ("before", "after") and self.at_punct("{", 1):
            self.advance()
            return ast.Hook(span(t.range, Range(self.prev_end, self.prev_end)), t.text, self.block())
        override = static = False
        while self.tok.kind == IDENT and self.tok.text in ("override", "static"):
            if self.tok.text == "override":
                override = True
            else:
                static = True
            self.advance()
        if not self.at("method"):
            if self.tok.kind == IDENT and self.tok.text in ("cxx_only", "self", "softc", "errno", "version", "extends"):
                self.fail(f"clauses moved into attributes: write #[{self.tok.text}(...)] before 'interface'", "KIF087")
            self.fail(f"expected 'method', found {self.describe(self.tok)}")
        kw = self.advance()
        name = self.ident("method", True)
        params, _ = self.params()
        ret = self.type_() if self.eat("->") else None
        m = ast.Method(kw.range, a, name, params, ret, override=override, static=static)
        if self.at("{"):
            m.body = self.block()
            m.has_default = True
        elif self.at("default"):
            self.advance()
            m.has_default = True
            if self.tok.kind == IDENT:
                m.default_sym = ast.Name(self.tok.range, self.advance().text)
            self.expect(";")
        else:
            self.expect(";", "';', 'default' or a method body")
        m.range = span(kw.range, Range(self.prev_end, self.prev_end))
        return m

    def foreign(self, attrs: List[ast.Attribute]) -> ast.ForeignClass:
        kw = self.advance()
        if not self.at("class"):
            self.fail("expected 'class' after 'foreign'")
        self.advance()
        name = self.ident("foreign class", True)
        semi = self.expect(";")
        return ast.ForeignClass(span(kw.range, semi.range), attrs, name)

    # -- enums, structs, register blocks

    def enum(self, attrs: List[ast.Attribute]) -> ast.EnumDecl:
        kw = self.advance()
        name = self.ident("enum", True)
        repr_ty = self.type_() if self.eat(":") else None
        self.expect("{")
        members: List[ast.EnumMember] = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated enum")
            start = self.i
            try:
                a = self.attributes()
                n = self.ident("enumerator", True)
                m = ast.EnumMember(n.range, a, n)
                if self.eat("="):
                    m.value = self.expr()
                    m.range = span(n.range, m.value.range)
                members.append(m)
                if not self.eat(","):
                    break
            except _Stop:
                self.skip_to_comma_or_close()
        close = self.expect("}")
        return ast.EnumDecl(span(kw.range, close.range), attrs, name, members, repr_ty)

    def skip_to_comma_or_close(self) -> None:
        depth = 0
        while self.tok.kind != EOF:
            t = self.tok
            if t.kind == PUNCT:
                if t.text in ("(", "[", "{"):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    if depth == 0:
                        return
                    depth -= 1
                elif t.text == "," and depth == 0:
                    self.advance()
                    return
            self.advance()

    def struct(self, attrs: List[ast.Attribute]) -> ast.StructDecl:
        kw = self.advance()
        name = self.ident("struct", True)
        generics = self.generic_params()
        self.expect("{")
        fields: List[ast.StructField] = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated struct")
            try:
                a = self.attributes()
                first = self.tok
                private = bool(self.eat("private"))
                n = self.ident("field")
                self.expect(":", "':' and a type")
                ty = self.type_()
                f = ast.StructField(first.range, a, n, ty, None, private)
                if self.eat("="):
                    f.default = self.expr()
                f.range = span(first.range, Range(self.prev_end, self.prev_end))
                fields.append(f)
                if not (self.eat(",") or self.eat(";")):
                    break
            except _Stop:
                self.skip_to_comma_or_close()
        close = self.expect("}")
        return ast.StructDecl(span(kw.range, close.range), attrs, name, fields, generics)

    def block_decl(self, attrs: List[ast.Attribute]) -> ast.BlockDecl:
        kw = self.advance()
        name = self.ident("block", True)
        self.expect("{")
        regs: List[ast.Register] = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated block (missing '}')")
            start = self.i
            try:
                regs.append(self.register())
            except _Stop:
                self.recover_member(start)
        close = self.expect("}")
        return ast.BlockDecl(span(kw.range, close.range), attrs, name, regs)

    def register(self) -> ast.Register:
        a = self.attributes()
        kw = self.expect("register", "'register'")
        name = self.ident("register", True)
        self.expect(":", "':' and a type")
        ty = self.type_()
        self.expect("{")
        fields: List[ast.RegField] = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated register (missing '}')")
            start = self.i
            try:
                fa = self.attributes()
                n = self.ident("field", True)
                self.expect(":")
                lo = self.int_value()
                self.expect("..")
                hi = self.int_value()
                self.eat(";") or self.eat(",")
                fields.append(ast.RegField(span(n.range, Range(self.prev_end, self.prev_end)), fa, n, lo, hi))
            except _Stop:
                self.recover_member(start)
        close = self.expect("}")
        return ast.Register(span(kw.range, close.range), a, name, ty, fields)

    def int_value(self) -> int:
        t = self.tok
        if t.kind != INT:
            self.fail(f"{t.text!r} is not an integer", "KIF038")
        self.advance()
        return lx.parse_int(t.text)[0]

    # ------------------------------------------------------------------ types

    def type_(self) -> ast.Type:
        t = self.tok
        if t.kind == PUNCT:
            if t.text in ("&", "&&"):
                self.advance()
                lt = self.lifetime() if self.tok.kind == LIFETIME else None
                mut = bool(self.eat("mut"))
                inner = self.type_()
                ref: ast.Type = ast.RefType(span(t.range, inner.range), mut, inner, lt)
                if t.text == "&&":
                    ref = ast.RefType(ref.range, False, ref)
                return ref
            if t.text == "[":
                self.advance()
                elem = self.type_()
                if self.at("]"):
                    close = self.advance()
                    return ast.SliceType(span(t.range, close.range), elem)
                self.expect(";", "';' and an array length")
                length = self.expr()
                close = self.expect("]")
                return ast.ArrayType(span(t.range, close.range), elem, length)
            if t.text == "(":
                self.advance()
                elems: List[ast.Type] = []
                while not self.at(")"):
                    elems.append(self.type_())
                    if not self.eat(","):
                        break
                close = self.expect(")")
                return ast.TupleType(span(t.range, close.range), elems)
        if t.kind == IDENT and t.text == "fn":
            self.advance()
            self.expect("(")
            params: List[ast.Type] = []
            while not self.at(")"):
                params.append(self.type_())
                if not self.eat(","):
                    break
            close = self.expect(")")
            ret = self.type_() if self.eat("->") else None
            return ast.FnType(span(t.range, (ret or close).range if ret else close.range), params, ret)
        if t.kind == IDENT and t.text == "dyn" and self.peek().kind == IDENT:
            self.advance()
            pt = self.path_type()
            pt.dyn = True
            pt.range = span(t.range, pt.range)
            return pt
        return self.path_type()

    def lifetime(self) -> ast.Lifetime:
        t = self.advance()
        return ast.Lifetime(t.range, t.text)

    def path_type(self) -> ast.PathType:
        segs: List[ast.PathSeg] = []
        while True:
            t = self.tok
            if t.kind != IDENT or (t.text in KEYWORDS and t.text != "Self"):
                self.fail(f"expected a type, found {self.describe(t)}")
            self.advance()
            seg = ast.PathSeg(t.range, t.text)
            if self.at("<"):
                seg.args = self.generic_args()
                seg.range = span(t.range, Range(self.prev_end, self.prev_end))
            segs.append(seg)
            if self.at("::") and self.peek().kind == IDENT:
                self.advance()
                continue
            break
        return ast.PathType(span(segs[0].range, segs[-1].range), segs)

    def bare_path(self) -> ast.PathType:
        """`a::b::name` without generic arguments."""
        segs = []
        while True:
            n = self.ident("type")
            segs.append(ast.PathSeg(n.range, n.text))
            if self.at("::") and self.peek().kind == IDENT:
                self.advance()
                continue
            return ast.PathType(span(segs[0].range, segs[-1].range), segs)

    def generic_args(self) -> List[ast.GenericArg]:
        self.expect("<")
        args: List[ast.GenericArg] = []
        while not self.at(">"):
            t = self.tok
            if t.kind == LIFETIME:
                args.append(self.lifetime())
            elif t.kind == INT or self.at("true") or self.at("false"):
                args.append(ast.ConstArg(t.range, self.literal()))
            elif self.at("{"):
                b = self.block()
                args.append(ast.ConstArg(b.range, b))
            else:
                args.append(self.type_())
            if not self.eat(","):
                break
        self.expect(">", "'>' to close the generic arguments")
        return args

    # ------------------------------------------------------------------ statements and blocks

    def block(self) -> ast.Block:
        open_tok = self.expect("{")
        stmts: List[ast.Stmt] = []
        tail: Optional[ast.Expr] = None
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated block (missing '}')", rng=open_tok.range)
            start = self.i
            try:
                s = self.stmt()
                if s is None:
                    continue
                if isinstance(s, ast.ExprStmt) and not s.semi and self.at("}"):
                    tail = s.expr
                    break
                stmts.append(s)
            except _Stop:
                self.recover_stmt(start)
        close = self.expect("}")
        return ast.Block(span(open_tok.range, close.range), stmts, tail)

    def recover_stmt(self, start: int) -> None:
        depth = 0
        while self.tok.kind != EOF:
            t = self.tok
            if t.kind == PUNCT:
                if t.text in ("{", "(", "["):
                    depth += 1
                elif t.text in (")", "]"):
                    depth = max(0, depth - 1)
                elif t.text == "}":
                    if depth == 0:
                        break
                    depth -= 1
                    if depth == 0:
                        self.advance()
                        return
                elif t.text == ";" and depth == 0:
                    self.advance()
                    return
            self.advance()
        if self.i == start and self.tok.kind != EOF and not self.at("}"):
            self.advance()

    def stmt(self) -> Optional[ast.Stmt]:
        t = self.tok
        if self.eat(";"):
            return None
        if self.at("#[") and self.let_attrs_ahead():
            attrs = self.attributes()
            if self.at_punct("(", 1):
                self.fail("attributes cannot be applied to a destructuring let")
            stmt = self.let()
            stmt.attrs = attrs
            stmt.range = span(attrs[0].range, stmt.range)
            return stmt
        if self.at("#[") or (t.kind == IDENT and t.text in _STMT_ITEM_START and not self.at_punct("{", 1)):
            item = self.item(True)
            return ast.ItemStmt(item.range, item)
        if self.at("let"):
            return self.let()
        if t.kind == IDENT and t.text in _BLOCK_LIKE and not (t.text == "cfg" and not self.at_punct("(", 1)) or self.at("{"):
            e = self.block_like()
            if self.at(".") or self.at("?"):
                e = self.postfix(e)
                return self.finish_expr_stmt(e)
            semi = self.eat(";")
            return ast.ExprStmt(span(e.range, semi.range if semi else e.range), e, bool(semi))
        return self.finish_expr_stmt(self.expr())

    def let_attrs_ahead(self) -> bool:
        """Whether the `#[..]` groups at the cursor are followed by `let`."""
        j = self.i
        while self.toks[j].kind == PUNCT and self.toks[j].text == "#[":
            close = self.matching(j)
            if close is None:
                return False
            j = close + 1
        return self.toks[j].kind == IDENT and self.toks[j].text == "let"

    def finish_expr_stmt(self, e: ast.Expr) -> ast.ExprStmt:
        if self.at(";"):
            semi = self.advance()
            return ast.ExprStmt(span(e.range, semi.range), e, True)
        if self.at("}"):
            return ast.ExprStmt(e.range, e, False)
        self.fail(f"expected ';' or '}}', found {self.describe(self.tok)}")

    def let(self) -> ast.Stmt:
        kw = self.advance()
        if self.at("("):
            pat = self.sub_pattern()
            ty = self.type_() if self.eat(":") else None
            init = self.expr() if self.eat("=") else None
            semi = self.expect(";")
            return ast.LetPat(span(kw.range, semi.range), pat, ty, init)
        mut = bool(self.eat("mut"))
        name = self.ident("variable")
        ty = self.type_() if self.eat(":") else None
        init = self.expr() if self.eat("=") else None
        semi = self.expect(";")
        return ast.Let(span(kw.range, semi.range), name, mut, ty, init)

    # ------------------------------------------------------------------ expressions

    def expr(self, no_struct: bool = False) -> ast.Expr:
        lhs = self.range_expr(no_struct)
        op = self.assign_op()
        if op:
            text, n = op
            for _ in range(n):
                self.advance()
            rhs = self.expr(no_struct)
            return ast.Assign(span(lhs.range, rhs.range), text, lhs, rhs)
        return lhs

    def assign_op(self) -> "Optional[Tuple[str, int]]":
        t = self.tok
        if t.kind != PUNCT:
            return None
        if t.text in _ASSIGN_OPS:
            return t.text, 1
        if t.text == ">" and self.adjacent() and self.at_punct(">", 1):
            if self.adjacent(1) and self.at_punct("=", 2):
                return ">>=", 3
        return None

    def binary_op(self) -> "Optional[Tuple[str, int]]":
        t = self.tok
        if t.kind != PUNCT:
            return None
        if t.text == ">":
            if self.adjacent() and self.at_punct(">", 1):
                if self.adjacent(1) and self.at_punct("=", 2):
                    return None  # `>>=` is an assignment
                return ">>", 2
            if self.adjacent() and self.at_punct("=", 1):
                return ">=", 2
            return ">", 1
        if t.text in _BINARY_PREC:
            return t.text, 1
        return None

    def starts_expr(self, no_struct: bool) -> bool:
        t = self.tok
        if t.kind == EOF:
            return False
        if t.kind == PUNCT and (t.text in _EXPR_END or (t.text == "{" and no_struct)):
            return False
        return True

    def range_expr(self, no_struct: bool) -> ast.Expr:
        if self.at("..") or self.at("..="):
            op = self.advance()
            hi = self.binary(3, no_struct) if self.starts_expr(no_struct) else None
            return ast.RangeExpr(span(op.range, hi.range if hi else op.range), None, hi, op.text == "..=")
        lo = self.binary(3, no_struct)
        if self.at("..") or self.at("..="):
            op = self.advance()
            hi = self.binary(3, no_struct) if self.starts_expr(no_struct) else None
            return ast.RangeExpr(span(lo.range, hi.range if hi else op.range), lo, hi, op.text == "..=")
        return lo

    def binary(self, min_prec: int, no_struct: bool) -> ast.Expr:
        lhs = self.cast(no_struct)
        compared = False
        while True:
            op = self.binary_op()
            if not op:
                return lhs
            text, n = op
            prec = _BINARY_PREC[text]
            if prec < min_prec:
                return lhs
            if text in _COMPARE:
                if compared:
                    self.fail("comparison operators cannot be chained; add parentheses")
                compared = True
            for _ in range(n):
                self.advance()
            rhs = self.binary(prec + 1, no_struct)
            lhs = ast.Binary(span(lhs.range, rhs.range), text, lhs, rhs)

    def cast(self, no_struct: bool) -> ast.Expr:
        e = self.unary(no_struct)
        while self.at("as"):
            self.advance()
            ty = self.type_()
            e = ast.Cast(span(e.range, ty.range), e, ty)
        return e

    def unary(self, no_struct: bool) -> ast.Expr:
        t = self.tok
        if t.kind == PUNCT and t.text in ("-", "!", "*", "&", "&&"):
            self.advance()
            if t.text in ("&", "&&"):
                mut = bool(self.eat("mut"))
                inner = self.unary(no_struct)
                e: ast.Expr = ast.Unary(span(t.range, inner.range), "&mut" if mut else "&", inner)
                if t.text == "&&":
                    e = ast.Unary(e.range, "&", e)
                return e
            inner = self.unary(no_struct)
            return ast.Unary(span(t.range, inner.range), t.text, inner)
        return self.postfix(self.primary(no_struct), no_struct)

    def args(self) -> List[ast.Expr]:
        self.expect("(")
        out: List[ast.Expr] = []
        while not self.at(")"):
            out.append(self.expr())
            if not self.eat(","):
                break
        self.expect(")")
        return out

    def postfix(self, e: ast.Expr, no_struct: bool = False) -> ast.Expr:
        while True:
            if self.at("?"):
                q = self.advance()
                e = ast.Try(span(e.range, q.range), e)
            elif self.at("("):
                args = self.args()
                e = ast.Call(span(e.range, Range(self.prev_end, self.prev_end)), e, args)
            elif self.at("["):
                self.advance()
                idx = self.expr()
                close = self.expect("]")
                e = ast.Index(span(e.range, close.range), e, idx)
            elif self.at(".") and self.peek().kind == INT and self.peek().text.isdigit():
                self.advance()
                n = self.advance()
                e = ast.FieldAccess(span(e.range, n.range), e, ast.Name(n.range, n.text))
            elif self.at(".") and self.peek().kind == IDENT:
                self.advance()
                n = self.ident("member")
                turbofish: List[ast.GenericArg] = []
                if self.at("::") and self.at_punct("<", 1):
                    self.advance()
                    turbofish = self.generic_args()
                if self.at("("):
                    args = self.args()
                    e = ast.MethodCall(span(e.range, Range(self.prev_end, self.prev_end)), e, n, turbofish, args)
                else:
                    e = ast.FieldAccess(span(e.range, n.range), e, n)
            else:
                return e

    def literal(self) -> ast.Lit:
        t = self.tok
        if t.kind == INT:
            self.advance()
            value, suffix = lx.parse_int(t.text)
            return ast.Lit(t.range, "int", value, suffix)
        if t.kind == STRING:
            self.advance()
            return ast.Lit(t.range, "str", lx.unescape(t.text) or "")
        if self.at("true") or self.at("false"):
            self.advance()
            return ast.Lit(t.range, "bool", t.text == "true")
        if self.at("-") and self.peek().kind == INT:
            self.advance()
            n = self.advance()
            value, suffix = lx.parse_int(n.text)
            return ast.Lit(span(t.range, n.range), "int", -value, suffix)
        self.fail(f"expected a literal, found {self.describe(t)}")

    def block_like(self) -> ast.Expr:
        t = self.tok
        if self.at("{"):
            return self.block()
        if self.at("unsafe"):
            self.advance()
            b = self.block()
            return ast.UnsafeBlock(span(t.range, b.range), b)
        if self.at("cfg"):
            self.advance()
            if not self.at("("):
                self.fail("expected '(' after cfg: cfg(predicate) { .. }")
            self.i -= 1  # attribute() takes the name token itself
            attr = self.attribute(t.range, False)
            b = self.block()
            return ast.CfgBlock(span(t.range, b.range), attr, b)
        if self.at("if"):
            return self.if_()
        if self.at("while"):
            self.advance()
            cond = self.expr(True)
            b = self.block()
            return ast.While(span(t.range, b.range), cond, b)
        if self.at("loop"):
            self.advance()
            b = self.block()
            return ast.Loop(span(t.range, b.range), b)
        if self.at("for"):
            self.advance()
            var = self.ident("loop variable")
            self.expect("in")
            it = self.expr(True)
            b = self.block()
            return ast.For(span(t.range, b.range), var, it, b)
        return self.match()

    def if_(self) -> ast.If:
        kw = self.expect("if")
        cond = self.expr(True)
        then = self.block()
        other = None
        if self.eat("else"):
            other = self.if_() if self.at("if") else self.block()
        return ast.If(span(kw.range, Range(self.prev_end, self.prev_end)), cond, then, other)

    def match(self) -> ast.Match:
        kw = self.expect("match")
        scrut = self.expr(True)
        self.expect("{")
        arms: List[ast.MatchArm] = []
        while not self.at("}"):
            if self.tok.kind == EOF:
                self.fail("unterminated 'match' (missing '}')")
            start = self.i
            try:
                pat = self.pattern()
                guard = self.expr(True) if self.eat("if") else None
                self.expect("=>")
                if self.at("{"):
                    body: ast.Expr = self.block()
                    self.eat(",")
                else:
                    body = self.expr()
                    if not self.eat(",") and not self.at("}"):
                        self.fail(f"expected ',' or '}}' after a match arm, found {self.describe(self.tok)}")
                arms.append(ast.MatchArm(span(pat.range, body.range), pat, body, guard))
            except _Stop:
                self.recover_arm(start)
        close = self.expect("}")
        return ast.Match(span(kw.range, close.range), scrut, arms)

    def recover_arm(self, start: int) -> None:
        depth = 0
        while self.tok.kind != EOF:
            t = self.tok
            if t.kind == PUNCT:
                if t.text in ("{", "(", "["):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    if depth == 0:
                        return
                    depth -= 1
                elif t.text == "," and depth == 0:
                    self.advance()
                    return
            self.advance()

    def pattern(self) -> ast.Pattern:
        self.eat("|")
        first = self.pattern_atom()
        if not self.at("|"):
            return first
        alts = [first]
        while self.eat("|"):
            alts.append(self.pattern_atom())
        return ast.OrPat(span(first.range, alts[-1].range), alts)

    def pattern_atom(self) -> ast.Pattern:
        t = self.tok
        if t.kind == IDENT and t.text == "_":
            self.advance()
            return ast.WildPat(t.range)
        if t.kind == INT or self.at("-") or self.at("true") or self.at("false"):
            lo = self.literal()
            if self.eat("..="):
                hi = self.literal()
                return ast.RangePat(span(lo.range, hi.range), lo, hi)
            return ast.LitPat(lo.range, lo)
        if self.at("("):
            return self.sub_pattern()
        if self.at("mut") or (t.kind == IDENT and self.peek().kind == PUNCT and self.peek().text == "@"):
            return self.sub_pattern()
        p = self.path_expr()
        if self.at("("):
            self.advance()
            args: List[ast.Pattern] = []
            while not self.at(")") and self.tok.kind != EOF:
                args.append(self.sub_pattern())
                if not self.eat(","):
                    break
            end = self.tok.range
            self.expect(")")
            return ast.VariantPat(span(p.range, end), p, args)
        return ast.PathPat(p.range, p)

    def sub_pattern(self) -> ast.Pattern:
        t = self.tok
        if self.at("("):
            self.advance()
            elems: List[ast.Pattern] = []
            while not self.at(")") and self.tok.kind != EOF:
                elems.append(self.sub_pattern())
                if not self.eat(","):
                    break
            end = self.tok.range
            self.expect(")")
            return ast.TuplePat(span(t.range, end), elems)
        mut = bool(self.eat("mut"))
        if mut:
            t = self.tok
        nxt = self.peek()
        if (mut or (t.kind == IDENT and t.text != "_" and t.text not in KEYWORDS)) \
                and not (nxt.kind == PUNCT and nxt.text in ("::", "(")):
            n = self.ident("variable")
            sub = self.pattern() if self.eat("@") else None
            return ast.BindPat(span(t.range, (sub or n).range), n, mut, sub)
        return self.pattern()

    def path_expr(self) -> ast.PathExpr:
        segs: List[ast.PathSeg] = []
        while True:
            t = self.tok
            if t.kind != IDENT or (t.text in KEYWORDS and t.text != "Self"):
                self.fail(f"expected a name, found {self.describe(t)}")
            self.advance()
            seg = ast.PathSeg(t.range, t.text)
            segs.append(seg)
            if self.at("<"):
                # `ptr<ring>::try_allocate(..)`: type arguments are only taken when `::name` follows them.
                save, ndiag = self.i, len(self.d)
                try:
                    seg.args = self.generic_args()
                    if not (self.at("::") and self.peek().kind == IDENT):
                        raise _Stop()
                    seg.range = span(t.range, Range(self.prev_end, self.prev_end))
                except _Stop:
                    seg.args = []
                    self.i = save
                    del self.d[ndiag:]
                    break
            if self.at("::"):
                if self.at_punct("<", 1):
                    self.advance()
                    seg.args = self.generic_args()
                    seg.range = span(t.range, Range(self.prev_end, self.prev_end))
                    if self.at("::") and self.peek().kind == IDENT:
                        self.advance()
                        continue
                    break
                if self.peek().kind == IDENT:
                    self.advance()
                    continue
            break
        return ast.PathExpr(span(segs[0].range, segs[-1].range), segs)

    def looks_like_struct_lit(self) -> bool:
        if not self.at("{"):
            return False
        n1 = self.peek()
        if n1.kind == PUNCT and n1.text in ("}", ".."):
            return True
        n2 = self.peek(2)
        return n1.kind == IDENT and n2.kind == PUNCT and n2.text in (":", ",", "}")

    def lambda_(self) -> ast.Expr:
        start = self.tok
        params: List[ast.LambdaParam] = []
        if self.at("||"):
            self.advance()
        else:
            self.expect("|")
            while not self.at("|"):
                n = self.ident("lambda parameter")
                ty = self.type_() if self.eat(":") else None
                params.append(ast.LambdaParam(span(n.range, ty.range if ty else n.range), n, ty))
                if not self.eat(","):
                    break
            self.expect("|", "'|' closing the lambda parameters")
        ret = self.type_() if self.eat("->") else None
        body = self.block() if ret is not None else self.expr()
        return ast.Lambda(span(start.range, body.range), params, body, ret)

    def primary(self, no_struct: bool = False) -> ast.Expr:
        t = self.tok
        if t.kind == PUNCT and t.text in ("|", "||"):
            return self.lambda_()
        if t.kind in (INT, STRING) or self.at("true") or self.at("false"):
            return self.literal()
        if t.kind == IDENT and t.text in _BLOCK_LIKE:
            return self.block_like()
        if self.at("{"):
            return self.block()
        if self.at("return"):
            self.advance()
            v = self.expr(no_struct) if self.starts_expr(no_struct) else None
            return ast.Return(span(t.range, v.range if v else t.range), v)
        if self.at("break"):
            self.advance()
            return ast.Break(t.range)
        if self.at("continue"):
            self.advance()
            return ast.Continue(t.range)
        if self.at("("):
            self.advance()
            elems: List[ast.Expr] = []
            trailing = False
            while not self.at(")"):
                elems.append(self.expr())
                trailing = bool(self.eat(","))
                if not trailing:
                    break
            close = self.expect(")")
            if len(elems) == 1 and not trailing:
                return elems[0]
            return ast.TupleExpr(span(t.range, close.range), elems)
        if self.at("["):
            self.advance()
            elems = []
            repeat = None
            while not self.at("]"):
                elems.append(self.expr())
                if len(elems) == 1 and self.eat(";"):
                    repeat = self.expr()
                    break
                if not self.eat(","):
                    break
            close = self.expect("]")
            return ast.ArrayLit(span(t.range, close.range), elems, repeat)
        if t.kind == IDENT and (t.text not in KEYWORDS or t.text == "Self"):
            if self.at_punct("!", 1) and self.peek(2).kind == PUNCT and self.peek(2).text in ("(", "["):
                self.advance()
                self.advance()
                close_text = ")" if self.at("(") else "]"
                self.advance()
                args: List[ast.Expr] = []
                while not self.at(close_text):
                    args.append(self.expr())
                    if not self.eat(","):
                        break
                close = self.expect(close_text)
                return ast.MacroCall(span(t.range, close.range), ast.Name(t.range, t.text), args)
            path = self.path_expr()
            if not no_struct and self.looks_like_struct_lit():
                return self.struct_lit(path)
            return path
        self.fail(f"expected an expression, found {self.describe(t)}")

    def struct_lit(self, path: ast.PathExpr) -> ast.StructLit:
        self.expect("{")
        fields: List[ast.FieldInit] = []
        base = None
        while not self.at("}"):
            if self.eat(".."):
                base = self.expr()
                break
            n = self.ident("field")
            value = self.expr() if self.eat(":") else None
            fields.append(ast.FieldInit(span(n.range, value.range if value else n.range), n, value))
            if not self.eat(","):
                break
        close = self.expect("}")
        return ast.StructLit(span(path.range, close.range), path, fields, base)
