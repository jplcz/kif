# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Markdown documentation from a checked module, behind `kif doc` (docs/kif-tools.md).

A doc comment is the run of `//` lines directly above a declaration (above its `#[..]` lines, with no blank line in
between). The text after `//` and one optional space is Markdown and is copied through. Signatures are printed from
the source text, so they read exactly as written.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from . import ast
from .astutil import doc_above, has_attr, slice_text
from .lint import enum_values
from .symbols import Module


class _Doc:
    def __init__(self, mod: Module) -> None:
        self.mod = mod
        self.lines = mod.source.split("\n")
        self.out: List[str] = []

    def text(self, node: Optional[ast.Node]) -> str:
        return "" if node is None else " ".join(slice_text(self.lines, node.range).split())

    def attrs(self, attrs: List[ast.Attribute]) -> str:
        return " ".join(f"#[{self.text(a)}]" for a in attrs)

    def doc(self, node: ast.Node) -> None:
        d = doc_above(self.lines, node)
        if d:
            self.out += [d, ""]

    def generics(self, gs: List[ast.GenericParam]) -> str:
        return "<" + ", ".join(self.text(g) for g in gs) + ">" if gs else ""

    def signature(self, f, kw: str) -> str:
        ret = f" -> {self.text(f.ret)}" if f.ret is not None else ""
        words = [self.attrs(f.attrs), "private" if getattr(f, "private", False) else "",
                 "static" if getattr(f, "static", False) else "", "override" if getattr(f, "override", False) else "",
                 "gen" if getattr(f, "is_gen", False) else ""]
        lead = " ".join(w for w in words if w)
        params = ", ".join(self.text(p) for p in f.params)
        return f"{lead + ' ' if lead else ''}{kw} {f.name.text}{self.generics(getattr(f, 'generics', []))}({params}){ret}"

    def block(self, title: str, node: ast.Node, sig: Optional[str] = None, level: int = 2) -> None:
        self.out += [f"{'#' * level} {title}", ""]
        if sig:
            self.out += ["```kif", sig, "```", ""]
        self.doc(node)

    def enum(self, e: ast.EnumDecl) -> None:
        under = self.text(e.repr) if e.repr is not None else "i32"
        flags = has_attr(e.attrs, "flags")
        self.block(f"enum `{e.name.text}`", e)
        self.out += [f"Underlying type `{under}`" + ("; a `#[flags]` enum: the values are bits." if flags else "."), ""]
        vals = enum_values(e)
        for n, m in enumerate(e.members):
            v = ""
            if vals is not None:
                v = f" = {vals[n]}" + (f" (0x{vals[n]:x})" if flags and vals[n] > 0 else "")
            elif m.value is not None:
                v = f" = `{self.text(m.value)}`"
            d = doc_above(self.lines, m).replace("\n", " ")
            self.out.append(f"- `{m.name.text}`{v}" + (f" - {d}" if d else ""))
        self.out.append("")

    def struct(self, s: ast.StructDecl, impls: Dict[str, List[str]]) -> None:
        self.block(f"struct `{s.name.text}{self.generics(s.generics)}`", s)
        if s.attrs:
            self.out += [f"Attributes: `{self.attrs(s.attrs)}`", ""]
        for f in s.fields:
            d = doc_above(self.lines, f).replace("\n", " ")
            self.out.append(f"- `{f.name.text}: {self.text(f.ty)}`" + (f" - {d}" if d else ""))
        if s.fields:
            self.out.append("")
        if impls.get(s.name.text):
            self.out += ["Implements: " + ", ".join(f"`{n}`" for n in impls[s.name.text]), ""]

    def interface(self, i: ast.Interface, implementers: Dict[str, List[str]]) -> None:
        kobj = has_attr(i.attrs, "kobj")
        self.block(f"interface `{i.name.text}`", i)
        facts = [f"kind: {'kobj (dispatched through a class table)' if kobj else 'plain (devirtualised)'}"]
        facts += [f"`#[{self.text(a)}]`" for a in i.attrs if a.name.text != "kobj"]
        if implementers.get(i.name.text):
            facts.append("implemented by: " + ", ".join(f"`{n}`" for n in implementers[i.name.text]))
        self.out += ["- " + f for f in facts] + [""]
        for m in i.methods:
            self.block(f"`{i.name.text}.{m.name.text}`", m, self.signature(m, "method"), 3)
            notes = []
            if m.override:
                notes.append("overrides the default of an inherited method")
            elif kobj and not has_attr(i.attrs, "cxx_only"):
                notes.append(f"C symbol: `{i.name.text}_{m.name.text}`")
            if m.body is not None or m.has_default or m.default_sym is not None:
                notes.append("has a default" + (f": `{m.default_sym.text}`" if m.default_sym else ""))
            if notes:
                self.out += ["- " + n for n in notes] + [""]

    def impl(self, im: ast.ImplDecl) -> None:
        head = f"impl{self.generics(im.generics)} " + (f"{self.text(im.iface)} for " if im.iface else "") + self.text(im.target)
        self.block(f"`{head}`", im)
        for f in im.items:
            if isinstance(f, ast.FnDecl):
                self.block(f"`{f.name.text}`", f, self.signature(f, "fn"), 3)

    def render(self) -> str:
        f = self.mod.tree
        self.out = [f"# {self.mod.name}", ""]
        for ns in f.of(ast.Namespace)[:1]:
            self.doc(ns)
        if (deps := [i.path for i in f.of(ast.Import)]):
            self.out += ["Imports: " + ", ".join(f"`{d}`" for d in deps), ""]
        if (uses := [" ".join(self.text(u).split()).removeprefix("use ").rstrip(";") for u in f.of(ast.Use)]):
            self.out += ["Uses: " + ", ".join(f"`{u}`" for u in uses), ""]
        impls: Dict[str, List[str]] = {}
        implementers: Dict[str, List[str]] = {}
        for im in f.of(ast.ImplDecl):
            if im.iface is not None and isinstance(im.target, ast.PathType):
                impls.setdefault(im.target.segments[-1].name, []).append(im.iface.text)
                implementers.setdefault(im.iface.segments[-1].name, []).append(im.target.segments[-1].name)
        for it in f.items:
            if isinstance(it, ast.EnumDecl):
                self.enum(it)
            elif isinstance(it, ast.StructDecl):
                self.struct(it, impls)
            elif isinstance(it, ast.Interface):
                self.interface(it, implementers)
            elif isinstance(it, ast.FnDecl):
                self.block(f"fn `{it.name.text}`", it, self.signature(it, "fn"))
            elif isinstance(it, ast.ImplDecl):
                self.impl(it)
            elif isinstance(it, (ast.ConstDecl, ast.StaticDecl)):
                kw = "const" if isinstance(it, ast.ConstDecl) else "static"
                lead = self.attrs(it.attrs) + " " if it.attrs else ""
                self.block(f"{kw} `{it.name.text}`", it, f"{lead}{kw} {it.name.text}: {self.text(it.ty)}")
            elif isinstance(it, ast.TypeAlias):
                self.block(f"type `{it.name.text}`", it, f"type {it.name.text}{self.generics(it.generics)} = {self.text(it.ty)}")
            elif isinstance(it, ast.ExternType):
                self.block(f"extern type `{self.text(it.path)}`", it)
            elif isinstance(it, ast.BlockDecl):
                self.block(f"block `{it.name.text}`", it)
        return "\n".join(self.out).rstrip() + "\n"


def render(mod: Module) -> str:
    """Markdown for one checked .kif file."""
    return _Doc(mod).render()


def doc_path(directory: str, path: str) -> str:
    return os.path.join(directory, os.path.splitext(os.path.basename(path))[0] + ".md")
