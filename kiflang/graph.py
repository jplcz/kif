# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""`kif graph`: imports, `extends` and `impl .. for` of .kif files as Graphviz DOT or Mermaid (docs/kif-tools.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple

from . import ast
from .astutil import attr_name_arg
from .symbols import Module

Edge = Tuple[str, str, str]  # (from, to, kind: import | extends | implements); import ends are file keys, the rest node ids


@dataclass
class Graph:
    """Files (with the interfaces they declare and the structs that implement interfaces) and the edges between them."""

    files: Dict[str, str] = field(default_factory=dict)  # file key -> label
    members: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # file key -> [(node id, label)]
    edges: List[Edge] = field(default_factory=list)

    def add(self, mod: Module) -> None:
        if mod.key in self.files:
            return
        self.files[mod.key] = os.path.basename(mod.key)
        nodes: List[Tuple[str, str]] = []
        self.members[mod.key] = nodes
        implementers = {i.target.segments[-1].name for i in mod.tree.of(ast.ImplDecl)
                        if i.iface is not None and isinstance(i.target, ast.PathType)}
        for it in mod.tree.items:
            if isinstance(it, ast.Interface):
                kind = "kobj interface" if any(a.name.text == "kobj" for a in it.attrs) else "interface"
                nodes.append((node_id(mod.key, it.name.text), f"{it.name.text} ({kind})"))
            elif isinstance(it, ast.StructDecl) and it.name.text in implementers:
                nodes.append((node_id(mod.key, it.name.text), f"{it.name.text} (struct)"))
        for other in mod.imported.values():
            self.edges.append((mod.key, other.key, "import"))
            self.add(other)
        for it in mod.tree.items:
            if isinstance(it, ast.Interface):
                owner = _owner(mod, attr_name_arg(it.attrs, "extends"))
                if owner is not None:
                    self.edges.append((node_id(mod.key, it.name.text), owner, "extends"))
            elif isinstance(it, ast.ImplDecl) and it.iface is not None and isinstance(it.target, ast.PathType):
                owner = _owner(mod, it.iface.segments[-1].name)
                if owner is not None:
                    self.edges.append((node_id(mod.key, it.target.segments[-1].name), owner, "implements"))


def _owner(mod: Module, name: Optional[str]) -> Optional[str]:
    """The node id of the interface `name` as seen from `mod`, or None when it is unknown or from the prelude."""
    sym = mod.scope_types.get(name) if name else None
    if sym is None or sym.kind != "interface" or (mod.prelude is not None and sym.module.key == mod.prelude.key):
        return None
    return node_id(sym.module.key, name)


def node_id(file_key: str, name: str) -> str:
    return "n_" + "".join(c if c.isalnum() else "_" for c in f"{os.path.basename(file_key)}_{name}")


def file_id(key: str) -> str:
    return "f_" + "".join(c if c.isalnum() else "_" for c in os.path.basename(key))


def build(mods: List[Module]) -> Graph:
    """The graph of `mods` and, transitively, of everything they import."""
    g = Graph()
    for m in mods:
        g.add(m)
    return g


def _edges(g: Graph, files_only: bool, import_to_file: bool) -> Iterator[Tuple[str, str, str, str, str]]:
    """Distinct edges as (from key, to key, source node, target node, kind).

    An import edge joins two files: Mermaid can point at a subgraph (`import_to_file`), DOT cannot, so there it leaves
    from the first member of each file (or its placeholder) and `ltail`/`lhead` clip it to the clusters.
    """
    def member(key: str) -> str:
        m = g.members[key]
        return m[0][0] if m else file_id(key) + "_empty"

    seen: Set[Tuple[str, str, str]] = set()
    for a, b, kind in g.edges:
        if files_only and kind != "import":
            continue
        if kind == "import":
            src, dst = (file_id(a), file_id(b)) if files_only or import_to_file else (member(a), member(b))
        else:
            src, dst = a, b
        if (src, dst, kind) not in seen:
            seen.add((src, dst, kind))
            yield a, b, src, dst, kind


def to_dot(g: Graph, files_only: bool = False) -> str:
    out = ["digraph kif {", "  rankdir=LR;", "  compound=true;", '  node [shape=box, fontname="monospace"];']
    for key, label in g.files.items():
        if files_only:
            out.append(f'  {file_id(key)} [label="{label}"];')
            continue
        out += [f"  subgraph cluster_{file_id(key)} {{", f'    label="{label}";']
        out += [f'    {nid} [label="{text}"];' for nid, text in g.members[key]] or [f'    {file_id(key)}_empty [label="(no interfaces)", style=dashed];']
        out.append("  }")
    for a, b, src, dst, kind in _edges(g, files_only, False):
        if kind == "import":
            style = "" if files_only else f" [ltail=cluster_{file_id(a)}, lhead=cluster_{file_id(b)}, color=gray]"
        else:
            style = f' [label="{kind}", style={"dashed" if kind == "extends" else "dotted"}]'
        out.append(f"  {src} -> {dst}{style};")
    out.append("}")
    return "\n".join(out) + "\n"


def to_mermaid(g: Graph, files_only: bool = False) -> str:
    arrows = {"import": "-->", "extends": "-. extends .->", "implements": "-. implements .->"}
    out = ["flowchart LR"]
    for key, label in g.files.items():
        if files_only:
            out.append(f'  {file_id(key)}["{label}"]')
            continue
        out.append(f'  subgraph {file_id(key)}["{label}"]')
        out += [f'    {nid}["{text}"]' for nid, text in g.members[key]] or [f'    {file_id(key)}_empty["(no interfaces)"]']
        out.append("  end")
    out += [f"  {src} {arrows[kind]} {dst}" for _a, _b, src, dst, kind in _edges(g, files_only, True)]
    return "\n".join(out) + "\n"
