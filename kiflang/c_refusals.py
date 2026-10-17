# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The `c_refusals` check pass: what the C backend will not lower, found on the IR (docs/kif-ir-codegen.md, "Refusals").

These are not checker errors: a program that uses them is valid kif. They are refusals of one target, so the pass is registered but not
part of the `default` pipeline; the C lowering adds it. Each rule says why C cannot honour the program as written."""

from __future__ import annotations

from typing import Dict, List

from . import ir
from . import irtypes
from .passes import Context, Pass, register
from .target import ERROR_ERRNO


@register
class CRefusalsPass(Pass):
    name, kind, requires = "c_refusals", "check", ("cfg",)
    default = False
    summary = "errors for what the C backend refuses: a manual drop, a dropped result, an owning result in a local, errors that share an errno, an owning optional copied out, a match on a temporary optional, a `for` that would move a vector, a `match` on an owning result, a call whose parameter fact provably fails"

    def run(self, ctx: Context) -> int:
        fn = ctx.fn
        n = 0
        used: Dict[int, List[ir.Instr]] = {}
        for i in [x for b in fn.blocks for x in (*b.instrs, b.term) if x is not None]:  # a terminator (`return t`, `branch t`) uses values too
            for u in i.uses():
                if isinstance(u, ir.Temp):
                    used.setdefault(u.id, []).append(i)
        for _, i in fn.instrs():
            if isinstance(i, ir.Call) and i.kind == "method" and i.callee == "drop" and not i.args:
                ctx.report("KIFC101", "`drop` runs by itself when the value goes out of scope (or is moved): remove this call", i.site)
                n += 1
            elif isinstance(i, ir.Call) and i.dst is not None and i.dst.type.kind == irtypes.NAMED and i.dst.type.name == "result" \
                    and i.kind in ("free", "assoc", "method"):
                uses = [u for u in used.get(i.dst.id, []) if not isinstance(u, ir.Release)]
                if not uses:
                    ctx.report("KIFC102", "a call returning result<T> is lowered only under `?` or as the returned value (its error would be dropped)", i.site)
                    n += 1
                elif any(isinstance(u, ir.Store) and u.local.owns for u in uses):
                    ctx.report("KIFC103", "this result carries a value that owns resources: take it with `?` or `match` so it is dropped for you", i.site)
                    n += 1
        defs = {i.defs().id: i for _, i in fn.instrs() if i.defs() is not None}

        def owned(v) -> bool:
            d = defs.get(v.id) if isinstance(v, ir.Temp) else None
            return isinstance(v, ir.Temp) and (v.owns or (isinstance(d, ir.Borrow) and d.local.owns))

        def optional(v) -> bool:
            t = v.type.args[0] if v.type.kind == irtypes.REF else v.type
            return t.kind == irtypes.NAMED and t.name == "optional"

        for _, i in fn.instrs():
            if isinstance(i, ir.Call) and i.kind == "method" and i.callee == "value_or" and i.recv is not None and optional(i.recv) and owned(i.recv):
                ctx.report("KIFC105", "value_or of an optional that owns resources would copy the owner: test has_value() and use value()", i.site)
                n += 1
            elif isinstance(i, ir.Project) and i.path.startswith("Some") and owned(i.base):
                ctx.report("KIFC106", "this optional owns resources: test it with `Some(_)` and read it with value(), a binding would copy the owner", i.site or defs[i.base.id].site)
                n += 1
            elif isinstance(i, ir.Test) and i.kind == "has_next" and i.base.type.kind == irtypes.NAMED and i.base.type.name in ("vector", "secure_vector", "str"):
                ctx.report("KIFC108", "`for` over a vector would move it: iterate a borrow, `for x in &v` or `&mut v`", i.site or defs[i.base.id].site)
                n += 1
            elif isinstance(i, ir.PatTest) and i.pattern.startswith("ok(") and owned(i.base):
                ctx.report("KIFC109", "this result carries a value that owns resources: take it with `?` so it is dropped for you", i.site)
                n += 1
            elif isinstance(i, ir.Test) and i.kind == "some" and isinstance(defs.get(i.base.id), ir.Call):
                ctx.report("KIFC107", "match on an optional needs a variable or field to look at, not a temporary: bind it with `let` first", i.site or defs[i.base.id].site)
                n += 1
        calls = {i.id: i for _, i in fn.instrs() if isinstance(i, ir.Call)}
        for _, c in fn.checks():
            if c.tag and c.refuted and c.call:  # a requirement of a call that provably fails: the call can never succeed
                fact, _, param = c.tag.partition(":")
                callee = calls[c.call].callee if c.call in calls else "the function"
                ctx.report("KIFC110", f"call can never succeed: #[{fact}] of argument {param!r} of {callee}() fails ({c.refuted})", c.site)
                n += 1
        errno = {**ERROR_ERRNO, **(ctx.config.get("errno") or {})}
        seen: Dict[int, Dict[str, str]] = {}
        for _, i in fn.instrs():
            if isinstance(i, ir.PatTest) and i.pattern.startswith("error::") and isinstance(i.base, ir.Temp):
                code = errno.get(i.pattern.split("::")[-1])
                prior = seen.setdefault(i.base.id, {})
                if code and code in prior.values() and prior.get(i.pattern) is None:
                    ctx.report("KIFC104", f"error names that share the errno {code} cannot be told apart in C: match one of them or use `_`", i.site)
                    n += 1
                if code:
                    prior[i.pattern] = code
        return n
