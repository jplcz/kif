# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack budgets: `#[small_stack(N)]`, `#![small_stack(N)]`, `--small-stack` (docs/kif-stack.md sections 2 and 6, stage 4).

`resolve` finds the budget in force for each function: the narrowest scope that sets one wins (the function, its `impl`, its struct, the
file, the command line, the target's `stack.default_budget`), and `#[small_stack(off)]` lifts a wider one. `check` compares the depths
of `stackgraph` with them: `KIFW010` (over budget), `KIFP084` (one variable alone over budget), `KIFW016` (a malformed attribute)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ast
from . import diagnostics as dg
from .modsec import _owner
from .stackgraph import Depth
from .target import StackPlan

OFF = "off"


@dataclass(frozen=True)
class Budget:
    limit: Optional[int]  # bytes; None: no budget (or lifted)
    source: str  # where it came from, for the message


def parse_option(text: str) -> Optional[int]:
    """The value of `--small-stack=N|off` (argparse `type`): None for off."""
    if text == OFF:
        return None
    try:
        n = int(text, 0)
    except ValueError:
        raise ValueError(f"expected a positive byte count or 'off', not {text!r}") from None
    if n <= 0:
        raise ValueError("the stack budget must be positive")
    return n


def _setting(attrs, where: str, bad: List[Tuple[object, str]]):
    """The `#[small_stack(..)]` among `attrs` as `(limit or None, source)`, or None when absent."""
    for a in attrs or []:
        if a.name.text != "small_stack":
            continue
        args = a.args or []
        if len(args) == 1 and args[0].name == OFF and args[0].value is None:
            return None, f"{where} #[small_stack(off)]", True
        v = getattr(args[0].value, "value", None) if len(args) == 1 else None
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v, f"{where} #[small_stack({v})]", True
        bad.append((a, "#[small_stack] takes one positive byte count or `off`"))
    return None


def resolve(items, tree, cli: Optional[int], cli_set: bool, plan: StackPlan) -> Tuple[Dict[str, Budget], List[dg.Diagnostic]]:
    """`(budget per function key, KIFW016 diagnostics for malformed attributes)`. `cli_set` is true when `--small-stack` was given (even `off`)."""
    bad: List[Tuple[object, str]] = []
    structs = {it.name.text: it for it in items if isinstance(it, ast.StructDecl)}
    file_set = _setting(getattr(tree, "inner_attrs", []), "the file's", bad)
    if cli_set:
        base = Budget(cli, "--small-stack")
    elif plan.default_budget is not None:
        base = Budget(plan.default_budget, "the target's stack.default_budget")
    else:
        base = Budget(None, "")

    def pick(*scopes) -> Budget:
        for s in scopes:
            if s is not None:
                return Budget(s[0], s[1])
        return base

    out: Dict[str, Budget] = {}
    for it in items:
        if isinstance(it, ast.FnDecl):
            out[it.name.text] = pick(_setting(it.attrs, "this function's", bad), file_set)
        elif isinstance(it, ast.ImplDecl):
            impl_set = _setting(it.attrs, "the impl's", bad)
            struct = structs.get(it.target.text) if isinstance(it.target, ast.PathType) else None
            struct_set = _setting(struct.attrs, "the struct's", bad) if struct is not None else None
            for m in it.items:
                if isinstance(m, ast.FnDecl):
                    out[_owner(it.target, it.iface) + m.name.text] = pick(_setting(m.attrs, "this function's", bad), impl_set, struct_set, file_set)
    diags = [dg.error(a.range, "KIFW016", msg) for a, msg in bad]
    return out, diags


def check(ds: List[Depth], budgets: Dict[str, Budget]) -> List[dg.Diagnostic]:
    """`KIFP084` for a variable bigger than the budget, `KIFW010` for a depth over it. Functions without a budget are not checked."""
    by_name = {d.name: d for d in ds}
    out: List[dg.Diagnostic] = []
    for d in ds:
        b = budgets.get(d.name)
        if b is None or b.limit is None or d.node is None:
            continue
        rng = d.node.name.range
        for name, size in d.parts:
            if not name.startswith("<temp") and size > b.limit:
                out.append(dg.error(rng, "KIFP084", f"{d.name}(): variable '{name}' is {size} bytes, more than the whole stack budget of {b.limit} bytes ({b.source}); "
                                                    "use #[heap] or pass it by borrow"))
        if d.unbounded or d.depth is None or d.depth <= b.limit:
            continue
        top = sorted(((n, s, c) for c in d.chain for n, s in (by_name[c].parts if c in by_name else [])[:2] if not n.startswith("<temp")), key=lambda x: -x[1])[:3]
        biggest = ", ".join(f"{c}::{n} {s}" for n, s, c in top)
        lower = " (at least; some calls have no known frame)" if d.incomplete else ""
        out.append(dg.warning(rng, "KIFW010", f"{d.name}() uses {d.depth} bytes of stack{lower}, over the budget of {b.limit} bytes ({b.source}); chain "
                                              + " -> ".join(d.chain) + (f"; biggest variables: {biggest}" if biggest else "") + " (move big locals to the heap with #[heap] or borrow them)"))
    return out
