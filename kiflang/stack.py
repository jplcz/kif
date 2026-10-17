# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The stack frame of every function (docs/kif-stack.md sections 3 and 7, stage 2).

`frames(built, items, plan)` returns one `Frame` per IR function: the sum of its locals and by-value parameters and of the aggregate
temporaries kif can size, rounded up to the target's stack alignment, plus the per-call overhead. A local kif cannot size makes the
frame *unknown* (never 0). `#[stack(N)]` replaces the estimate with N. The call graph and the depth come in stage 3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import ir, irtypes, layout
from .irtypes import parse_type
from .target import StackPlan


@dataclass
class Frame:
    name: str
    size: Optional[int]  # bytes of one call, None when unknown
    why: str = ""  # why it is unknown, or "declared" for a `#[stack(N)]` override
    parts: List[Tuple[str, int]] = field(default_factory=list)  # (variable, bytes), biggest first
    temps: int = 0  # aggregate temporaries counted
    unsized_temps: int = 0  # aggregate temporaries kif could not size (not counted)
    declared: bool = False
    line: int = 0


def declared_stack(fn, extra=()) -> Optional[int]:
    """N of `#[stack(N)]` on a function (an `ir.Function` or a declaration node; `extra` are more attributes, those of an `extern` wrapper), or None."""
    node = getattr(fn, "node", fn)
    for a in [*extra, *(getattr(node, "attrs", None) or [])]:
        if a.name.text == "stack":
            for x in a.args or []:
                v = getattr(getattr(x, "value", None), "value", None)
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
    return None


def _up(n: int, align: int) -> int:
    return n + (-n % align)


def frame(fn: ir.Function, plan: StackPlan, table) -> Frame:
    line = fn.node.range.start.line + 1 if fn.node is not None else 0
    n = declared_stack(fn)
    if n is not None:
        return Frame(fn.name, n, "declared", declared=True, line=line)
    total, parts, unknown = 0, [], []
    for loc in fn.locals:
        sa = layout.size_align(loc.type, plan, table)
        if sa is None:
            unknown.append(f"{loc.name}: {loc.ty}")
            continue
        total += _up(total, sa[1]) - total + sa[0]
        parts.append((loc.name, sa[0]))
    counted = unsized = 0
    stored = {i.src.id: i.local for _, i in fn.instrs() if isinstance(i, ir.Store) and isinstance(i.src, ir.Temp)}  # a temp stored into a slot has that slot's type
    for _, i in fn.instrs():
        t = i.defs()
        # Loads and field/index accesses read a slot in place; only a built value (a literal, a call result) needs storage of its own.
        if t is None or t.ty is None or not isinstance(i, (ir.Make, ir.Call)):
            continue
        ty = t.type
        if ty.kind == irtypes.UNKNOWN and t.id in stored:
            ty = stored[t.id].type
        if ty.kind == irtypes.UNKNOWN and isinstance(i, ir.Make) and i.kind in ("array", "repeat", "tuple", "struct"):
            unsized += 1
            continue
        if ty.kind not in (irtypes.ARRAY, irtypes.TUPLE) and not (ty.kind == irtypes.NAMED and ty.name not in ("ptr", "c_ptr", "box")):
            continue
        sa = layout.size_align(ty, plan, table)
        if sa is None:
            unsized += 1
        else:
            total += _up(total, sa[1]) - total + sa[0]
            counted += 1
            parts.append((f"<temp {t.id}>", sa[0]))
    parts.sort(key=lambda p: -p[1])
    if unknown:
        return Frame(fn.name, None, "cannot size " + ", ".join(unknown), parts, counted, unsized, line=line)
    return Frame(fn.name, _up(total, plan.align) + plan.frame_overhead, "", parts, counted, unsized, line=line)


def frames(built: ir.Module, items, plan: StackPlan) -> List[Frame]:
    table = layout.struct_table(items)
    return [frame(f, plan, table) for f in built.functions]


def report(fs: List[Frame], plan: StackPlan, file: str) -> str:
    """The frames as text, biggest first, unknown ones last (`kif ir --stack-report`)."""
    known = sorted((f for f in fs if f.size is not None), key=lambda f: (-f.size, f.name))
    unknown = [f for f in fs if f.size is None]
    out = [f"Stack frames of {file} (align {plan.align}, overhead {plan.frame_overhead} bytes per call, locals summed):", ""]
    for f in known:
        big = ", ".join(f"{n} {s}" for n, s in f.parts[:3])
        note = " [declared #[stack]]" if f.declared else ""
        skipped = f" ({f.unsized_temps} temporar{'y' if f.unsized_temps == 1 else 'ies'} not sized)" if f.unsized_temps else ""
        out.append(f"  {file}:{f.line}: {f.name}: {f.size} bytes{note}" + (f"; biggest: {big}" if big and not f.declared else "") + skipped)
    if unknown:
        out += ["", "Unknown frames:"]
        out += [f"  {file}:{f.line}: {f.name}: {f.why}" for f in unknown]
    return "\n".join(out) + "\n"
