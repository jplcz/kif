# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""What the code a backend generates costs on the stack (docs/kif-stack.md, stage 3, "hidden callees").

A kif function can call code the user never wrote: the vector and string helpers (`kif_vec_u32_try_push`) and the drop glue. What they
cost depends on how the *backend* writes them, so each target language has its own estimator and none is a constant of the estimate:

- `c`: the helpers are the very text `lower_c.Lowerer` writes for this target (its allocator, its wipe primitive, its errno values). The
  estimator parses that text, sizes each function's parameters and locals with the target's pointer width, and follows the calls between
  helpers (`try_push` -> `grow` -> `try_reserve`), the same frame model as `stack.frame`.
- `cxx`: kif has no C++ backend yet, so there is no estimator: the cost is unknown (a lower bound) until `stack.helper_frame` /
  `stack.drop_frame` say what the project's containers use.

`estimate(target, plan)` returns `Costs(ops, drops)`, per helper operation and per dropped type; None when the backend has no estimator."""

from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

SCALARS = {"char": 1, "bool": 1, "_Bool": 1, "short": 2, "int": 4, "long": 8, "unsigned": 4, "int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2,
           "int32_t": 4, "uint32_t": 4, "int64_t": 8, "uint64_t": 8}
LEAF = ("_try_at", "_len", "_is_empty", "_capacity", "_view")  # accessors: no nested call, tiny
FN = re.compile(r"^static __inline [^\n(]*?(\w+)\(([^)]*)\)\n\{\n(.*?)\n\}$", re.M | re.S)
LOCAL = re.compile(r"^\t(const )?((?:struct |enum )?\w+)\s*(\**)\s*(\w+)(\[\d+\])?\s*(?:=[^;]*)?;$")


class Costs(NamedTuple):
    ops: Optional[Dict[str, int]]  # `family.operation` (`vector.try_push`, `str.try_append`) -> bytes below a call of it, nested helpers included; None: no estimator
    drops: Optional[Dict[str, int]]  # `vector`, `str`: bytes below the generated drop glue of that type


def _size(ctype: str, stars: str, bits: int) -> Tuple[int, int]:
    """`(size, align)` of a C parameter or local type text; structs the estimator cannot see are taken as two pointers."""
    word = bits // 8
    if stars:
        return word, word
    name = ctype.strip()
    if name == "size_t":
        return word, word
    if name in SCALARS:
        return SCALARS[name], SCALARS[name]
    return 2 * word, word


def _parse(text: str, plan) -> Dict[str, Tuple[int, List[str]]]:
    """`{function: (own frame, helpers it calls)}` for every helper function in the generated C `text`."""
    found = [(m.group(1), m.group(2), m.group(3)) for m in FN.finditer(text)]
    names = {n for n, _, _ in found}
    out: Dict[str, Tuple[int, List[str]]] = {}
    for name, params, body in found:
        total = 0
        decls: List[Tuple[str, str]] = []
        for p in [x for x in params.split(",") if x.strip() and x.strip() != "void"]:
            q = p.replace("const ", "").strip()
            decls.append(("", "*") if "*" in q else (q.rsplit(None, 1)[0], ""))
        for line in body.splitlines():
            m = LOCAL.match(line)
            if m and m.group(2) not in ("return", "goto") and (m.group(2) in SCALARS or m.group(2) == "size_t" or m.group(2).startswith("struct ") or m.group(3)
                                                              or m.group(2).endswith("_t")):
                decls.append((m.group(2), m.group(3)))
        for ctype, stars in decls:
            size, align = _size(ctype, stars, plan.pointer_bits)
            total += (-total % align) + size
        frame = total + (-total % plan.align) + plan.frame_overhead
        out[name] = (frame, [n for n in names if n != name and re.search(rf"\b{re.escape(n)}\(", body)])
    return out


def _depth(fns: Dict[str, Tuple[int, List[str]]]) -> Dict[str, int]:
    memo: Dict[str, int] = {}

    def go(n: str, seen: Set[str]) -> int:
        if n in memo:
            return memo[n]
        own, calls = fns[n]
        d = own + max((go(c, seen | {n}) for c in calls if c not in seen), default=0)
        memo[n] = d
        return d

    for n in fns:
        go(n, set())
    return memo


def _c(target, plan) -> Costs:
    from . import lower_c

    lw = lower_c.Lowerer(None, target, "kif_stackhelpers")
    lw.vecs["u32"] = ("uint32_t", False)
    lw.svecs["u32"] = ("uint32_t", False)
    lw.uses_str = True
    try:
        _, vec = lw.vec_defs()
        _, text = lw.str_defs()
    except lower_c.LowerError:
        return Costs(None, None)
    fns = _parse("\n".join(vec + text), plan)
    depth = _depth(fns)
    ops: Dict[str, int] = {}
    drops: Dict[str, int] = {}
    for name, d in depth.items():
        for prefix in ("kif_vec_u32_", "kif_svec_u32_", "kif_str_"):
            if name.startswith(prefix):
                op = name[len(prefix):]
                if op == "drop":
                    kind = "str" if prefix == "kif_str_" else "vector"
                    drops[kind] = max(drops.get(kind, 0), d)
                else:
                    key = f"{'str' if prefix == 'kif_str_' else 'vector'}.{op}"
                    ops[key] = max(ops.get(key, 0), d)
    return Costs(ops, drops)


ESTIMATORS = {"c": _c}  # target language -> function(target, plan) -> Costs


def estimate(target, plan) -> Costs:
    """The hidden-callee costs for `target`'s backend; `Costs(None, None)` when its language has no estimator."""
    cached = getattr(target, "_stack_costs", None)
    if cached is not None and cached[0] == plan[:4]:
        return cached[1]
    fn = ESTIMATORS.get(target.language)
    costs = fn(target, plan) if fn is not None else Costs(None, None)
    target._stack_costs = (plan[:4], costs)
    return costs


def helper_cost(plan, op: str) -> Optional[int]:
    """Bytes below a call of the generated helper `op` (`vector.try_push`; a bare `try_push` when the receiver is unknown): the target's `stack.helper_frame` if it sets one, else the backend's own
    estimate (the most expensive helper for an operation it does not know), else None (no estimator: unknown)."""
    if plan.helper_frame is not None:
        return plan.helper_frame
    if plan.helpers is None:
        return None
    if op in plan.helpers:
        return plan.helpers[op]
    same = [v for k, v in plan.helpers.items() if k.split(".", 1)[-1] == op.split(".", 1)[-1]]  # the operation of another family, or of an unknown receiver
    return max(same or plan.helpers.values(), default=0)


def drop_cost(plan, kind: str) -> Optional[int]:
    """Bytes below the generated drop glue of a `vector`/`str`/`optional`/`ptr`; like `helper_cost`."""
    if plan.drop_frame is not None:
        return plan.drop_frame
    if plan.drops is None:
        return None
    return plan.drops.get(kind, max(plan.drops.values(), default=0))
