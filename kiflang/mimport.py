# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Converts FreeBSD kobj interface files (.m) to .kif (the procedural-language syntax, docs/kif-proc-language.md).

Command line: python3 -m kiflang.mimport [-o out.kif] file.m

CODE and HEADER become header(c) / code(c) raw blocks (kobj text is C); PROLOG / EPILOG become m_prolog / m_epilog.

The conversion follows the format that sys/tools/makeobjops.awk accepts (INTERFACE, METHOD, STATICMETHOD,
CODE, HEADER, PROLOG, EPILOG, DEFAULT; unknown ALL-CAPS qualifiers after METHOD or STATICMETHOD, e.g. FOO, are ignored; .m import only). Anything kif cannot express is reported as a warning and also written as
a comment at the top of the result, so the output can be reviewed in place.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union

from .lexer import KEYWORDS

# Names a kif identifier cannot take: the language keywords, plus the receiver and the built-in type names.
_RESERVED = KEYWORDS | {"self", "void", "bool", "cstr", "str", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "usize", "isize", "c_ptr", "c_cptr"}

_STD_TYPES = {
    "void": "void",
    "bool": "bool",
    "_Bool": "bool",
    "int": "i32",
    "unsigned": "u32",
    "signed": "i32",
    "int8_t": "i8",
    "int16_t": "i16",
    "int32_t": "i32",
    "int64_t": "i64",
    "uint8_t": "u8",
    "uint16_t": "u16",
    "uint32_t": "u32",
    "uint64_t": "u64",
    "size_t": "usize",
    "ssize_t": "isize",
}
_BUILTIN_TYPES = {"void", "bool", "char", "cstr", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "usize", "isize"}
_QUALIFIERS = {"const", "volatile", "restrict", "__restrict", "register"}
_STRUCT_WORDS = {"struct", "union", "enum"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class Param:
    type: str
    name: str


@dataclass
class _Method:
    static: bool
    ret: str
    name: str
    params: List[Param]
    default: Optional[str]
    doc: List[str]
    line: int
    unconverted: bool = False
    src: str = ""
    prolog: Optional[str] = None  # PROLOG / EPILOG body (raw C lines), emitted as m_prolog / m_epilog
    epilog: Optional[str] = None


@dataclass
class _Comment:
    doc: List[str]


@dataclass
class _Interface:
    name: str
    doc: List[str] = field(default_factory=list)
    methods: List[Union[_Method, _Comment]] = field(default_factory=list)


@dataclass
class Result:
    kif: str
    warnings: List[Tuple[int, str]]


class _Conv:
    def __init__(self) -> None:
        self.warnings: List[Tuple[int, str]] = []
        self.typedefs: List[Tuple[str, str]] = []  # (typedef name, C spelling)
        self.foreign: Set[str] = set()  # C type names the interfaces use; each is declared `extern "C" type`
        self.sections: List[str] = []
        self.includes: List[str] = []
        self.hooks: Dict[str, str] = {}  # pending PROLOG / EPILOG bodies for the next method
        self.iface: Optional[_Interface] = None
        self.line = 0

    def warn(self, msg: str, line: Optional[int] = None) -> None:
        self.warnings.append((self.line if line is None else line, msg))

    # ---- types -------------------------------------------------------------------------------------------

    def split_type(self, text: str):
        """Returns (words without qualifiers, star count, is_const)."""
        words, stars, const = [], 0, False
        for w in text.replace("*", " * ").split():
            if w == "*":
                stars += 1
            elif w in _QUALIFIERS:
                if w == "const":
                    const = const or stars == 0  # 'T * const' is a const pointer, not a pointer to const
                elif w == "volatile":
                    self.warn(f"'volatile' dropped from type {text!r}")
            else:
                words.append(w)
        return words, stars, const

    def base_type(self, words: List[str]) -> str:
        if len(words) == 1:
            w = words[0]
            if w in _STD_TYPES:
                return _STD_TYPES[w]
            if w in KEYWORDS:
                self.warn(f"type name {w!r} is a kif keyword; rename it by hand")
            if w not in _BUILTIN_TYPES:
                self.foreign.add(w)
            return w
        if words[0] in _STRUCT_WORDS and len(words) == 2:
            # `struct res` is not a kif type name: name it through a typedef, and declare that as a C type.
            name = "kif_" + "_".join(words)
            if name not in {n for n, _ in self.typedefs}:
                self.typedefs.append((name, " ".join(words)))
            self.foreign.add(name)
            return name
        if words in (["unsigned", "int"], ["signed", "int"]):
            return "u32" if words[0] == "unsigned" else "i32"
        # "unsigned long" and friends are several words; a kif C type is one identifier, so add a typedef.
        name = "kif_" + "_".join(words)
        if all(_IDENT.fullmatch(w) for w in words) and name not in {n for n, _ in self.typedefs}:
            self.typedefs.append((name, " ".join(words)))
        self.foreign.add(name)
        self.warn(f"multi-word C type {' '.join(words)!r} replaced by typedef {name!r}")
        return name

    def convert_type(self, text: str) -> str:
        words, stars, const = self.split_type(text)
        if not words:
            self.warn(f"empty type in {text!r}")
            return "int"
        base = self.base_type(words)
        if stars == 1 and const and base == "char":
            return "cstr"
        t = base
        for k in range(stars):
            t = f"{'c_cptr' if (k == 0 and const) else 'c_ptr'}<{t}>"
        return t

    def ret_type(self, text: str) -> str:
        t = self.convert_type(text)
        return "()" if t == "void" else t

    def c_text(self, text: str) -> str:
        """Plain C spelling, for clauses that take a C type (self)."""
        words, stars, const = self.split_type(text)
        s = ("const " if const else "") + " ".join(words)
        return s + (" " + "*" * stars if stars else "")

    # ---- parsing -----------------------------------------------------------------------------------------

    def run(self, text: str, name: str) -> Result:
        lines = text.split("\n")
        i = 0
        doc: List[str] = []
        while i < len(lines):
            self.line = i + 1
            raw = lines[i].strip()
            line = re.sub(r"#.*", "", raw).strip()
            i += 1
            inc = re.fullmatch(r"#\s*include\s*(<[^>]*>|\"[^\"]*\")\s*", raw)
            if inc:
                self.emit_comment(doc)
                doc = []
                if inc.group(1) not in self.includes:
                    self.includes.append(inc.group(1))
                continue
            if not line:
                # A '#' comment directly above an item belongs to it; a blank line detaches it, and the
                # detached comment (licence, section text) is kept as a stand-alone comment.
                if raw.startswith("#"):
                    doc.append(raw)
                else:
                    self.emit_comment(doc)
                    doc = []
                continue
            if line.startswith("/*"):
                doc, i = self.read_comment(lines, i - 1)
                continue
            m = re.fullmatch(r"INTERFACE\s+([^\s;]*)\s*;?", line)
            if m:
                self.flush_interface()
                self.iface = _Interface(m.group(1), doc)
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", m.group(1)):
                    self.warn(f"invalid interface name {m.group(1)!r}")
                doc = []
                continue
            m = re.fullmatch(r"(CODE|HEADER)\s*\{", line)
            if m:
                body, i = self.read_block(lines, i)
                self.sections.append(self.comment_block(doc, "") + f"{m.group(1).lower()}(c) {{\n{body}}}\n")
                doc = []
                continue
            m = re.fullmatch(r"(PROLOG|EPILOG)\s*\{", line)
            if m:
                body, i = self.read_block(lines, i)
                self.hooks[m.group(1)] = body
                continue
            if re.match(r"(?:STATIC)?METHOD\b", line):
                before = len(self.iface.methods) if self.iface is not None else 0
                i = self.read_method(lines, i - 1, line.startswith("STATIC"), doc)
                self.attach_hooks(before)
                doc = []
                continue
            self.warn(f"invalid line ignored: {line!r}")
        self.emit_comment(doc)
        self.flush_interface()
        return Result(self.render(name), self.warnings)

    def attach_hooks(self, before: int) -> None:
        """PROLOG / EPILOG apply to the next method only (makeobjops resets them after it)."""
        hooks, self.hooks = self.hooks, {}
        if not hooks:
            return
        if self.iface is None or len(self.iface.methods) == before:
            self.warn("PROLOG/EPILOG dropped: the method after it was not converted")
            return
        m = self.iface.methods[-1]
        if m.unconverted:
            self.warn(f"PROLOG/EPILOG of {m.name!r} dropped: the method itself is left as a comment", m.line)
            return
        m.prolog, m.epilog = hooks.get("PROLOG"), hooks.get("EPILOG")

    def emit_comment(self, doc: List[str]) -> None:
        if not any(l.strip() for l in self.doc_lines(doc)):
            return
        if self.iface is not None:
            self.iface.methods.append(_Comment(doc))
        else:
            self.sections.append(self.comment_block(doc, ""))

    def read_comment(self, lines: List[str], i: int):
        out = []
        while i < len(lines):
            out.append(lines[i])
            if "*/" in lines[i]:
                return out, i + 1
            i += 1
        self.warn("unterminated comment")
        return out, i

    def read_block(self, lines: List[str], i: int):
        """Raw lines up to (not including) the first line that starts with '}', like makeobjops.awk.

        The indentation is kept: kif ends a verbatim block at a '}' in column 1, so de-indenting would
        truncate bodies that contain C functions.
        """
        out = []
        while i < len(lines):
            if lines[i].startswith("}"):
                return "".join(l + "\n" for l in out), i + 1
            out.append(lines[i])
            i += 1
        self.warn("verbatim block not closed")
        return "".join(l + "\n" for l in out), i

    def read_method(self, lines: List[str], i: int, static: bool, doc: List[str]) -> int:
        start = i
        self.line = start + 1
        text = lines[i]
        while "}" not in text and i + 1 < len(lines):
            i += 1
            text += " " + lines[i]
        if self.iface is None:
            self.warn("method outside an interface ignored")
            return i + 1
        m = re.match(r"\s*(?:STATICMETHOD|METHOD)\s+(?P<head>[^{]*)\{(?P<args>[^}]*)\}\s*;?(?P<rest>.*)", text, re.S)
        if not m:
            self.warn("malformed method ignored")
            return i + 1
        head = re.sub(r"#.*", "", m.group("head")).strip()
        # Unknown qualifiers (ALL-CAPS words such as FOO) between METHOD and the return type are ignored.
        head = re.sub(r"^(?:[A-Z][A-Z0-9_]*\s+)+(?=\S+\s+\S)", "", head)
        hm = re.fullmatch(r"(?P<ret>.+?)\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)", head, re.S)
        if not hm:
            self.warn(f"cannot read return type and name from {head!r}")
            return i + 1
        ret, name = hm.group("ret").strip(), hm.group("name")
        args_text = re.sub(r"/\*.*?\*/", " ", m.group("args"), flags=re.S)
        if "(" in args_text or "[" in args_text:
            self.warn(f"method {name!r} uses a function-pointer or array parameter; left as a comment")
            self.iface.methods.append(
                _Method(static, ret, name, [], None, doc, start + 1, unconverted=True, src=" ".join(text.split()))
            )
            return i + 1
        params: List[Param] = []
        for part in args_text.split(";"):
            part = " ".join(part.split())
            if not part:
                continue
            pm = re.fullmatch(r"(?P<t>.*?)(?P<n>[A-Za-z_][A-Za-z0-9_]*)", part)
            if not pm or not pm.group("t").strip():
                self.warn(f"no type for parameter {part!r}")
                continue
            params.append(Param(pm.group("t").strip(), pm.group("n")))
        dm = re.search(r"DEFAULT\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", m.group("rest"))
        self.iface.methods.append(_Method(static, ret, name, params, dm.group(1) if dm else None, doc, start + 1))
        return i + 1

    # ---- output ------------------------------------------------------------------------------------------

    def kif_name(self, name: str, what: str, line: int) -> str:
        new = name.lstrip("_").lower() or "x"
        if new in _RESERVED:
            new += "_arg" if what == "parameter" else "_m"
        new = re.sub(r"__+", "_", new).rstrip("_")
        if new != name.lstrip("_"):  # dropping a leading underscore is routine, not worth a note
            self.warn(f"{what} {name!r} renamed to {new!r}", line)
        return new

    def comment_block(self, doc: List[str], indent: str) -> str:
        return "".join(f"{indent}// {c}\n".replace("// \n", "//\n") for c in self.doc_lines(doc))

    @staticmethod
    def doc_lines(doc: List[str]) -> List[str]:
        out: List[str] = []
        for l in doc:
            l = re.sub(r"^/\*+\s*|\s*\*+/$", "", l.strip())
            l = re.sub(r"^#+-$", "", l)  # '#-' marks a licence for FreeBSD tooling
            l = re.sub(r"^(\*+|#+)", "", l).expandtabs(4)
            l = l[1:] if l.startswith(" ") else l
            out.append(l.rstrip())
        while out and not out[0]:
            out.pop(0)
        while out and not out[-1]:
            out.pop()
        return out

    def flush_interface(self) -> None:
        it, self.iface = self.iface, None
        if it is None:
            return
        self_types: Set[str] = set()
        body: List[str] = []
        comments: List[bool] = []
        for m in it.methods:
            if isinstance(m, _Comment):
                body.append(self.comment_block(m.doc, "  "))
                comments.append(True)
                continue
            comments.append(False)
            self.line = m.line
            comment = self.comment_block(m.doc, "  ")
            if m.unconverted:
                body.append(f"{comment}  // TODO unconverted: {m.src}\n")
                continue
            params = list(m.params)
            if not m.static:
                if params:
                    self_types.add(self.c_text(params.pop(0).type))
                else:
                    self.warn(f"instance method {m.name!r} has no object parameter", m.line)
            kparams = ", ".join(
                f"{self.kif_name(p.name, 'parameter', m.line)}: {self.convert_type(p.type)}" for p in params
            )
            default = f" default {m.default}" if m.default else ""
            hooks = "".join(
                f"  {kw} {{\n{text}}}\n" for kw, text in (("m_prolog", m.prolog), ("m_epilog", m.epilog)) if text is not None
            )
            body.append(
                f"{hooks}{comment}  {'static ' if m.static else ''}method {self.kif_name(m.name, 'method', m.line)}"
                f"({kparams}) -> {self.ret_type(m.ret)}{default};\n"
            )
        head = f"interface {it.name}"
        self_types.discard("kobj_t")
        for t in [t for t in self_types if re.search(r"\bvoid\b", t)]:
            self.warn(f"interface {it.name!r}: object type {t!r} cannot be a kif 'self' type; clause omitted")
            self_types.discard(t)
        if len(self_types) > 1:
            self.warn(f"interface {it.name!r}: methods disagree on the object type {sorted(self_types)}; using the first")
        # A `.m` file declares a kobj interface (docs/kif-proc-language.md 4.15), so `kobj` is always stated.
        attrs = "#[kobj" + (f", self({sorted(self_types)[0]})" if self_types else "") + "]\n"
        # Blank line before every commented method (except the first) so each comment reads with its method.
        text = ""
        for n, b in enumerate(body):
            if n and (b.lstrip().startswith("//") or body[n - 1].count("\n") > 1 or comments[n - 1] or comments[n]):
                text += "\n"
            text += b
        self.sections.append(f"{self.comment_block(it.doc, '')}{attrs}{head} {{\n{text}}}\n")

    def render(self, name: str) -> str:
        head = [f"// Converted from {name} by kiflang.mimport; review before use."]
        head += [f"// NOTE (line {ln}): {msg}" for ln, msg in sorted(self.warnings)]
        parts = ["\n".join(head) + "\n"]
        if self.includes:
            parts.append("".join(f"include {i};\n" for i in self.includes))
        if self.typedefs:  # untagged: the same text is valid in the C and the C++ header
            parts.append("header {\n" + "".join(f"typedef {c} {n};\n" for n, c in self.typedefs) + "}\n")
        if self.foreign:
            parts.append("".join(f'extern "C" type {n};\n' for n in sorted(self.foreign)))
        parts += self.sections
        return "\n".join(parts)


def convert(text: str, name: str = "input.m") -> Result:
    res = _Conv().run(text, name)
    res.warnings.sort()
    return res


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kiflang.mimport", description="Convert a FreeBSD kobj .m file to .kif")
    ap.add_argument("file")
    ap.add_argument("-o", "--output", help="write here instead of standard output")
    args = ap.parse_args(argv)
    with open(args.file, encoding="utf-8", errors="replace") as f:
        res = convert(f.read(), args.file.rsplit("/", 1)[-1])
    for ln, msg in res.warnings:
        print(f"{args.file}:{ln}: warning: {msg}", file=sys.stderr)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(res.kif)
    else:
        sys.stdout.write(res.kif)
    return 0


if __name__ == "__main__":
    sys.exit(main())
