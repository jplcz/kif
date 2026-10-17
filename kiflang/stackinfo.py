# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The stack estimate of a whole module, for the tools that show it (docs/kif-stack.md section 7, stage 5): `kif lint`, the editor, the `.kifm`.

`analyse` runs the default pipeline, then `stackgraph.analyze` and `stackbudget.resolve` once per tree and caches the result on the tree, so
hover, inlay hints and diagnostics of one edit share it. A module the IR builder cannot take has no estimate (None), never a wrong one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import ast
from . import diagnostics as dg
from . import stackbudget, stackgraph, target
from .stackbudget import Budget
from .stackgraph import Depth

_CACHE = "_kif_stack_info"


@dataclass
class Info:
    depths: List[Depth]
    budgets: Dict[str, Budget]
    diagnostics: List[dg.Diagnostic]  # KIFW010, KIFP084, KIFW016, and the KIFW013/KIFW014 of stackgraph
    by_node: Dict[int, Depth]  # id(declaration) -> depth

    def of(self, fn) -> Optional[Depth]:
        return self.by_node.get(id(fn))

    def budget_of(self, fn) -> Optional[Budget]:
        d = self.of(fn)
        return self.budgets.get(d.name) if d is not None else None


def analyse(tree, plan: Optional[target.StackPlan] = None, cli: Optional[int] = None, cli_set: bool = False) -> Optional[Info]:
    """The estimate of `tree` for `plan` (the `c` target's by default), or None when it cannot be made. Cached when called with the defaults."""
    default = plan is None and cli is None and not cli_set
    if default and getattr(tree, _CACHE, False) is not False:
        return getattr(tree, _CACHE)
    from . import passes

    plan = plan or target.load("c").stack()
    try:
        built = passes.run_module(tree, passes.pipeline("default")).module
        depths = stackgraph.analyze(built, tree.items, plan, tree)
        budgets, bad = stackbudget.resolve(tree.items, tree, cli, cli_set, plan)
        info = Info(depths, budgets, stackgraph.diagnostics(depths) + bad + stackbudget.check(depths, budgets),
                    {id(d.node): d for d in depths if d.node is not None})
    except Exception:  # a construct the builder cannot take: no estimate, and the other tools go on
        info = None
    if default:
        try:
            setattr(tree, _CACHE, info)
        except AttributeError:
            pass
    return info


def diagnostics(tree, plan: Optional[target.StackPlan] = None, cli: Optional[int] = None, cli_set: bool = False) -> List[dg.Diagnostic]:
    """What `kif lint`, the editor and `kifc` show: only the budget diagnostics, and only when some budget is in force (a module without one costs nothing)."""
    if tree is None:
        return []
    default = plan is None and cli is None and not cli_set
    plan = plan or target.load("c").stack()
    if not _any_budget(tree, tree.items, plan, cli):
        return []
    info = analyse(tree) if default else analyse(tree, plan, cli, cli_set)
    return [d for d in info.diagnostics if d.code in ("KIFW010", "KIFP084", "KIFW016")] if info else []


def diagnostics_possible(tree) -> bool:
    """True when some budget is in force in `tree`."""
    return _any_budget(tree, tree.items, target.load("c").stack())


def _any_budget(tree, items, plan, cli: Optional[int] = None) -> bool:
    if plan.default_budget is not None or cli is not None:
        return True
    attrs = list(getattr(tree, "inner_attrs", []))
    for it in items:
        attrs += list(getattr(it, "attrs", None) or [])
        if isinstance(it, ast.ImplDecl):
            for m in it.items:
                attrs += list(getattr(m, "attrs", None) or [])
    return any(a.name.text == "small_stack" for a in attrs)


def describe(info: Optional[Info], fn) -> Optional[str]:
    """The hover text of function `fn`: frame, deepest chain, budget."""
    d = info.of(fn) if info else None
    if d is None:
        return None
    if d.frame is None:
        return "**Stack:** the frame cannot be estimated (a local of unknown size); give it `#[stack(N)]`."
    if d.unbounded:
        return f"**Stack:** frame {d.frame} bytes; unbounded (recursion: {' → '.join(d.chain)}). _docs/kif-stack.md_"
    text = f"**Stack:** frame {d.frame} bytes, deepest chain {d.depth} bytes" + (" (at least)" if d.incomplete else "")
    if len(d.chain) > 1:
        text += f" ({' → '.join(d.chain)})"
    b = info.budgets.get(d.name)
    if b is not None and b.limit is not None:
        text += f"; budget {b.limit} bytes ({b.source})"
    return text + ". _docs/kif-stack.md_"


def hint(info: Optional[Info], fn) -> Optional[dict]:
    """`{label, tooltip}` of the inlay hint at the name of `fn`; only for a function that has a budget."""
    d = info.of(fn) if info else None
    b = info.budget_of(fn) if info else None
    if d is None or b is None or b.limit is None or d.unbounded or d.depth is None:
        return None
    over = d.depth > b.limit
    return {"label": f"{'⚠ ' if over else ''}stack {d.depth}/{b.limit}" + ("+" if d.incomplete else ""),
            "tooltip": f"deepest chain {' → '.join(d.chain)}; budget {b.limit} bytes from {b.source}"}
