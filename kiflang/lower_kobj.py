# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The FreeBSD kobj emitter: `#[kobj]` interfaces to `<iface>_if.h` / `<iface>_if.c`, what `makeobjops.awk` makes from a `.m`.

See docs/kif-c-backends.md section 5. The kobj header is included only by these files (the target names it), never by the
procedural C output. Written to be byte-compatible with makeobjops where the input has the same meaning.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import ast
from .astutil import find_attr, has_attr
from .lower_c import BITS, LowerError, Lowerer, stem_of

LINE_WIDTH = 80  # makeobjops.awk wraps declarations here


def format_line(line: str, maxlength: int, indent: int) -> str:
    """makeobjops' `format_line`: break at the rightmost ", " that keeps the line within `maxlength`."""
    out = ""
    while len(line) > maxlength:
        cut = line[: maxlength + 1].rfind(", ")
        if cut < 0:
            break
        out += line[: cut + 1] + "\n"
        line = " " * indent + line[cut + 2:]
    return out + line


class Emitter:
    def __init__(self, mod, target, filename: str) -> None:
        self.mod, self.t, self.file = mod, target, filename
        self.structs = {i.name.text for i in mod.tree.items if isinstance(i, ast.StructDecl)}
        self.enums = {i.name.text for i in mod.tree.items if isinstance(i, ast.EnumDecl)}
        self.externs = {i.path.text for i in mod.tree.items if isinstance(i, ast.ExternType) and i.abi == "C"}

    def err(self, msg: str, node=None) -> LowerError:
        return LowerError(msg, getattr(node, "range", None))

    def ctype(self, t, node=None) -> str:
        """C spelling of a type in a kobj method signature (scalars, `c_ptr<T>`, `&T`, extern "C" types, struct/enum tags)."""
        if isinstance(t, ast.TupleType) and not t.elems:
            return "void"
        if isinstance(t, ast.RefType):
            return ("" if t.mut else "const ") + self.ctype(t.inner, node) + " *"
        if isinstance(t, ast.PathType) and len(t.segments) == 1:
            seg = t.segments[0]
            if seg.name in ("c_ptr", "c_cptr") and len(seg.args) == 1:
                inner = self.ctype(seg.args[0], node)
                return ("const " if seg.name == "c_cptr" else "") + inner + (" *" if not inner.endswith("*") else "*")
            if not seg.args:
                n = seg.name
                if n == "void":
                    return "void"
                if n in BITS or n in ("bool", "char"):
                    return self.t.c_type(n)
                if n == "error":
                    return "int"
                if n == "cstr":
                    return "const char *"
                if n in self.externs:
                    return n
                if n in self.structs:
                    return f"struct {n}"
                if n in self.enums:
                    return f"enum {n}"
        raise self.err(f"type {t.text if isinstance(t, ast.PathType) else type(t).__name__} has no kobj C spelling yet", t if hasattr(t, "range") else node)

    def decl(self, ctype: str, name: str) -> str:
        return ctype + name if ctype.endswith("*") else f"{ctype} {name}"

    def self_type(self, it: ast.Interface) -> str:
        a = find_attr(it.attrs, "self")
        if a is not None and a.raw:
            return a.raw.strip()
        return self.t.get("kobj.self_type", "kobj_t")

    def version(self, it: ast.Interface) -> Optional[str]:
        a = find_attr(it.attrs, "version")
        return a.raw.strip() if a is not None and a.raw else None

    def banner(self, kind: str) -> str:
        return ("/*\n * This file is @" "generated automatically.\n * Do not modify anything in here by hand.\n *\n"
                f" * Created from source file\n *   {self.file}\n * with\n *   kifc\n *\n"
                " * See the source file for legal information\n */\n")

    def verbatim(self, kind: str) -> List[str]:
        """Top-level `header(c) { }` / `code(c) { }` blocks (a tag other than c is for the C++ output)."""
        return [v.text.strip("\n") for v in self.mod.tree.items
                if isinstance(v, ast.Verbatim) and v.kind == kind and v.target in (None, "c")]

    def interface(self, it: ast.Interface) -> Dict[str, str]:
        name = it.name.text
        if find_attr(it.attrs, "extends") is not None:
            raise self.err(f"interface {name!r}: `extends` is not lowered to kobj C output yet", it)
        guard = f"_{name}_if_h_"
        selft = self.self_type(it)
        h: List[str] = [self.banner("h"), f"#ifndef {guard}", f"#define {guard}\n"]
        for inc in self.t.includes("header", kobj=True):
            h.append(f"#include {inc}")
        h.append("")
        ver = self.version(it)
        if ver is not None:
            h.append(f"#define {name.upper()}_IF_VERSION {ver}\n")
        h += [b + "\n" for b in self.verbatim("header")]
        c: List[str] = [self.banner("c"),
                        '\n'.join(f"#include {i}" for i in self.t.includes("source", kobj=True)
                                  + [f'"{name}_if.h"'] + [(f"<{i.path}>" if i.system else f'"{i.path}"')
                                                           for i in self.mod.tree.items if isinstance(i, ast.Include)]),
                        ""]
        c += [b + "\n" for b in self.verbatim("code")]
        prolog = epilog = ""
        for m in it.members:
            if isinstance(m, ast.Hook):
                raise self.err("`before` / `after` hooks of a kobj interface are not lowered to kobj C output yet (use m_prolog / m_epilog)", m)
            if isinstance(m, ast.Verbatim):
                if m.kind == "m_prolog":
                    prolog = m.text
                elif m.kind == "m_epilog":
                    epilog = m.text
                continue
            h_, c_ = self.method(it, m, selft, prolog, epilog)
            h.append(h_)
            c.append(c_)
            prolog = epilog = ""
        h.append(f"#endif /* {guard} */")
        return {f"{name}_if.h": "\n".join(h) + "\n", f"{name}_if.c": "\n".join(c) + "\n"}

    def method(self, it: ast.Interface, m: ast.Method, selft: str, prolog: str, epilog: str) -> Tuple[str, str]:
        iname, mname = it.name.text, m.name.text
        full = f"{iname}_{mname}"
        if m.body is not None:
            raise self.err(f"method {mname!r}: a kif default body on a kobj interface is not lowered yet (use `default <c function>`)", m)
        if any(p.receiver for p in m.params):
            raise self.err(f"method {mname!r}: a kobj method declares no receiver; the handle comes from `self(..)`", m)
        ret = self.ctype(m.ret, m) if m.ret is not None else "void"
        args: List[Tuple[str, str]] = []
        if not m.static:
            args.append((selft, "dev"))
        for p in m.params:
            args.append((self.ctype(p.ty, p), p.name.text))
        if not args:
            raise self.err(f"static method {mname!r} needs a first parameter: it is the class whose method table is searched", m)
        arglist = ", ".join(self.decl(t, n) for t, n in args)
        names = ", ".join(n for _, n in args)
        first = args[0][1] if m.static else f"((kobj_t){args[0][1]})"
        default = m.default_sym.text if m.default_sym is not None else "kobj_error_method"
        h: List[str] = [f"/** @brief Unique descriptor for the {full.upper()}() method */",
                        f"extern struct kobjop_desc {full}_desc;",
                        f"/** @brief A function implementing the {full.upper()}() method */"]
        proto = f"typedef {ret} {full}_t("
        h.append(format_line(proto + arglist + ");", LINE_WIDTH, len(proto)) + "\n")
        w = f"static __inline {ret}\n{full.upper()}("
        h.append(format_line(w + arglist + ")", LINE_WIDTH, len(w)))  # makeobjops indents by the whole prototype text
        h.append("{")
        h.append("\tkobjop_t _m;")
        if ret != "void":
            h.append(f"\t{ret} rc;")
        if prolog.strip():
            h.append(self.raw(prolog))
        h.append(f"\tKOBJOPLOOKUP({first}->ops, {full});")
        h.append(f"\t{'rc = ' if ret != 'void' else ''}(({full}_t *) _m)({names});")
        if epilog.strip():
            h.append(self.raw(epilog))
        if ret != "void":
            h.append("\treturn (rc);")
        h.append("}\n")
        c = (f"struct kobjop_desc {full}_desc = {{\n\t0, {{ &{full}_desc, (kobjop_t){default} }}\n}};\n")
        return "\n".join(h), c

    def classes(self, stem: str) -> Dict[str, str]:
        """`<stem>_kobj.h/.c`: a kobj class for every struct with `impl <kobj iface> for S` (docs/kif-c-backends.md 5.2)."""
        ifaces = {i.name.text: i for i in self.mod.tree.items
                  if isinstance(i, ast.Interface) and has_attr(i.attrs, "kobj") and not has_attr(i.attrs, "cxx_only")}
        by_struct: Dict[str, List[Tuple[ast.Interface, ast.ImplDecl]]] = {}
        for im in self.mod.tree.items:
            if isinstance(im, ast.ImplDecl) and im.iface is not None and im.iface.text in ifaces and isinstance(im.target, ast.PathType):
                by_struct.setdefault(im.target.text, []).append((ifaces[im.iface.text], im))
        if not by_struct:
            return {}
        handles = self.t.get("kobj.handles", None) or {}
        lifecycle = self.t.get("kobj.lifecycle", None) or []
        lower = Lowerer(self.mod, self.t, stem)
        lower.collect()
        lower.find_dropable()
        h = [self.banner("h"), f"#ifndef _{stem}_kobj_h_", f"#define _{stem}_kobj_h_\n", "struct kobj_class;\n"]
        c = [self.banner("c")]
        used = sorted({i.name.text for pairs in by_struct.values() for i, _ in pairs})
        c += [f"#include {i}" for i in self.t.includes("source", kobj=True)]
        c += [f'#include "{n}_if.h"' for n in used] + [f'#include "{stem}_proc.h"', f'#include "{stem}_kobj.h"', ""]
        for sname, pairs in by_struct.items():
            if sname not in self.structs:
                raise self.err(f"`impl .. for {sname}`: only a struct can be a kobj class", pairs[0][1])
            h.append(f"/** @brief The kobj class of struct {sname}; its object size is sizeof(struct {sname}) */")
            h.append(f"extern struct kobj_class {sname}_class;\n")
            table: List[str] = []
            for it, im in pairs:
                selft = self.self_type(it)
                own = {f.name.text: f for f in im.items if isinstance(f, ast.FnDecl)}
                oa = find_attr(im.attrs, "object")
                if oa is not None:
                    if not (oa.args and oa.args[0].value is not None and oa.args[0].value.kind == "str"):
                        raise self.err('#[object("C expression using {handle}")] takes one string', im)
                    expr = oa.args[0].value.value
                else:
                    expr = handles.get(selft)
                    if expr is None:
                        expr = f"(struct {sname} *){{handle}}"
                        sd = next(i for i in self.mod.tree.items if isinstance(i, ast.StructDecl) and i.name.text == sname)
                        first = sd.fields[0].ty if sd.fields else None
                        if not (isinstance(first, ast.PathType) and first.text in self.externs):
                            raise self.err(f"`impl {it.name.text} for {sname}`: handle {selft!r} is cast to the struct, so its first field must be "
                                           "the kobj header (an `extern \"C\" type`); or map the handle with `kobj.handles` or `#[object(..)]`", im)
                managed = oa is None and selft in lifecycle
                for m in it.members:
                    if not isinstance(m, ast.Method) or m.name.text not in own:
                        continue
                    f = own[m.name.text]
                    if any(p.receiver == "value" for p in f.params):
                        raise self.err("a by-value receiver cannot be a kobj method", f)
                    ret = self.ctype(m.ret, m) if m.ret is not None else "void"
                    full = f"{it.name.text}_{m.name.text}"
                    thunk = f"{sname}_{full}_kobj"
                    if m.static:
                        if not m.params:
                            raise self.err(f"static method {m.name.text!r} needs a first parameter: it is the class", m)
                        args = [(self.ctype(p.ty, p), p.name.text) for p in m.params]
                        call = ", ".join(n for _, n in args)
                        body = f"{sname}_{m.name.text}({call})"
                    else:
                        args = [(selft, "dev")] + [(self.ctype(p.ty, p), p.name.text) for p in m.params]
                        rest = "".join(", " + n for _, n in args[1:])
                        acc = expr.replace("{handle}", "dev")
                        body = None
                        if managed and m.name.text in ("attach", "detach"):
                            if ret not in ("int", self.t.c_type("i32")):
                                raise self.err(f"method {m.name.text!r}: a managed lifecycle method returns i32", m)
                            drop = sname in lower.dropable
                            head = f"\tstruct {sname} *sc = {acc};\n\t{ret} error;\n\n"
                            if m.name.text == "attach":
                                body = head + f"\t{sname}_init(sc);\n\terror = {sname}_attach(sc{rest});\n"
                                if drop:
                                    body += f"\tif (error != 0)\n\t\t{sname}_drop(sc);\n"
                            else:
                                body = head + f"\terror = {sname}_detach(sc{rest});\n"
                                if drop:
                                    body += f"\tif (error == 0)\n\t\t{sname}_drop(sc);\n"
                            body += "\treturn (error);"
                        else:
                            body = f"{sname}_{m.name.text}({acc}{rest})"
                    proto = f"static {ret} {thunk}("
                    c.append(format_line(proto + ", ".join(self.decl(t, n) for t, n in args) + ")", LINE_WIDTH, len(proto)))
                    if body.startswith("\t"):
                        c.append(f"{{\n{body}\n}}\n")
                    else:
                        c.append(f"{{\n\t{'' if ret == 'void' else 'return '}{body};\n}}\n")
                    table.append(f"\tKOBJMETHOD({full}, {thunk}),")
            c.append(f"static kobj_method_t {sname}_methods[] = {{\n" + "\n".join(table + ["\tKOBJMETHOD_END"]) + "\n};\n")
            c.append(f"DEFINE_CLASS_0({sname}, {sname}_class, {sname}_methods, sizeof(struct {sname}));\n")
        h.append(f"#endif /* _{stem}_kobj_h_ */")
        return {f"{stem}_kobj.h": "\n".join(h) + "\n", f"{stem}_kobj.c": "\n".join(c)}

    def raw(self, text: str) -> str:
        """A raw C hook body as written in the source, one tab deeper than the wrapper's own statements."""
        lines = [ln.rstrip() for ln in text.strip("\n").split("\n")]
        return "\n".join(ln if ln.startswith("\t") else "\t" + ln.lstrip() for ln in lines)


def lower(mod, target, filename: str) -> Dict[str, str]:
    """`{file name: text}` for every non-`cxx_only` `#[kobj]` interface of the checked module."""
    if not target.supports("kobj"):
        raise LowerError(f"target {target.name!r} has no kobj emitter")
    em = Emitter(mod, target, filename)
    out: Dict[str, str] = {}
    for it in mod.tree.items:
        if isinstance(it, ast.Interface) and has_attr(it.attrs, "kobj") and not has_attr(it.attrs, "cxx_only"):
            out.update(em.interface(it))
    out.update(em.classes(stem_of(filename)))
    return out
