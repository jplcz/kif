# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The IR pass manager (docs/kif-ir.md, "Passes").

Every analysis over the IR is a `Pass` with a name and the passes it needs first. `run_function` / `run_module` take the names you
want (or a pipeline), put them in dependency order, run each once per function and cache what an analysis returned, so two
passes that both need the CFG share one. Three kinds exist, which only differ in what the manager does with their result:

* `analysis`  computes something other passes read (`ctx.get("cfg")`); the result is cached until a later pass invalidates it;
* `transform` annotates or rewrites the IR (`prove` sets `Check.proof`); it lists the analyses it `invalidates`;
* `check`     reports diagnostics (`ctx.report`) and changes nothing. A check is a second opinion that complements the checker
  on the syntax tree (kiflang/bodies.py), which stays the authority: a check pass may add errors, never remove one.

A function the IR builder cannot handle (`ir.Unsupported`) is not run through any pass; `run_module` lists it in
`ModuleResult.skipped`, so callers can say that the function had no second opinion.

Order is configurable. `requires` is a hard dependency (the needed pass runs first and is pulled in); `after` / `before` are soft
constraints between passes that do not need each other's result (they apply only when both are in the run); and the order in which
you list passes is kept wherever no constraint says otherwise. `pipeline("default,-prove,borrows")` builds a list from named
pipelines, pass names and `-name` removals, so a new pass can be inserted, dropped or moved without touching the others.

Add a pass with `@register`; the pipeline names (`default` = every pass, `verify`, `prove`) are what `kif ir --passes` and the editor use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

from . import diagnostics as dg
from . import ir

KINDS = ("analysis", "transform", "check")


class PassError(Exception):
    """A pass name that is not registered, or a dependency cycle."""


@dataclass(eq=False)
class Pass:
    """One step. Subclass it, set `name`, `kind`, `requires` and `invalidates`, and implement `run`."""

    name: ClassVar[str] = ""
    kind: ClassVar[str] = "analysis"
    requires: ClassVar[Tuple[str, ...]] = ()
    invalidates: ClassVar[Tuple[str, ...]] = ()  # analyses whose cached result this pass makes stale
    after: ClassVar[Tuple[str, ...]] = ()  # soft: run after these when they are in the run too (no result is needed)
    before: ClassVar[Tuple[str, ...]] = ()  # soft: run before these when they are in the run too
    summary: ClassVar[str] = ""
    default: ClassVar[bool] = True  # False: a target's pass (`c_refusals`), added by that target, left out of the `default` pipeline

    def run(self, ctx: "Context"):
        raise NotImplementedError


@dataclass(eq=False)
class Context:
    """What a pass sees: the function, an optional call resolver, the cached results and the diagnostics reported so far."""

    fn: ir.Function
    resolve: Optional[Callable] = None
    config: Dict[str, object] = field(default_factory=dict)  # what the caller tells the passes about the target, e.g. {"access": {"index": "checked"}}
    results: Dict[str, object] = field(default_factory=dict)
    diagnostics: List[dg.Diagnostic] = field(default_factory=list)

    def get(self, name: str):
        """The cached result of a pass named in `requires` (the manager has run it already)."""
        return self.results[name]

    def report(self, code: str, message: str, site, error: bool = True) -> None:
        """A diagnostic at the AST node `site` (an instruction's `site`); an instruction without one cannot be reported."""
        rng = getattr(site, "range", None)
        if rng is not None:
            self.diagnostics.append((dg.error if error else dg.warning)(rng, code, message))


@dataclass
class Stat:
    """How a pass behaved on one function (`kif ir --passes ... --stats`)."""

    name: str
    seconds: float
    cached: bool = False


@dataclass
class Result:
    fn: ir.Function
    diagnostics: List[dg.Diagnostic]
    results: Dict[str, object]
    stats: List[Stat]


@dataclass
class ModuleResult:
    module: ir.Module
    functions: List[Result] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def diagnostics(self) -> List[dg.Diagnostic]:
        return [d for r in self.functions for d in r.diagnostics]


REGISTRY: Dict[str, Pass] = {}


def register(cls):
    """Class decorator: make the pass available by name."""
    if not cls.name or cls.kind not in KINDS:
        raise PassError(f"{cls.__name__}: a pass needs a `name` and a `kind` of {', '.join(KINDS)}")
    if cls.name in REGISTRY:
        raise PassError(f"pass {cls.name!r} is registered twice")
    REGISTRY[cls.name] = cls()
    return cls


def order(names: Sequence[str]) -> List[str]:
    """`names` plus everything they require, in a valid order.

    Hard `requires` come first, then the soft `after` / `before` constraints between passes that are both present; among passes
    that no constraint orders, the position in `names` decides (a requirement sits just before its first user)."""
    out: List[str] = []
    state: Dict[str, int] = {}

    def visit(n: str, trail: Tuple[str, ...]) -> None:
        p = REGISTRY.get(n)
        if p is None:
            raise PassError(f"no pass named {n!r} (known: {', '.join(sorted(REGISTRY))})")
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise PassError("passes require each other: " + " -> ".join(trail + (n,)))
        state[n] = 1
        for r in p.requires:
            visit(r, trail + (n,))
        state[n] = 2
        out.append(n)

    for n in names:
        visit(n, ())
    present = set(out)
    must: Dict[str, set] = {n: set(REGISTRY[n].requires) for n in out}  # n -> passes that have to run before it
    for n in out:
        for a in REGISTRY[n].after:
            if a in present:
                must[n].add(a)
        for b in REGISTRY[n].before:
            if b in present:
                must[b].add(n)
    done: List[str] = []
    rest = list(out)
    while rest:
        nxt = next((n for n in rest if must[n] <= set(done)), None)
        if nxt is None:
            raise PassError("pass ordering constraints conflict: " + ", ".join(f"{n} (needs {', '.join(sorted(must[n] - set(done)))})" for n in rest))
        done.append(nxt)
        rest.remove(nxt)
    return done


def run_function(fn: ir.Function, names: Sequence[str], resolve: Optional[Callable] = None, config: Optional[Dict[str, object]] = None) -> Result:
    """Run `names` (and what they need) on one built function.

    An analysis is cached until a transform lists it in `invalidates`; it is then recomputed, lazily, by the next pass that
    `requires` it (and only then), so a transform that changes nothing an analysis reads costs nothing."""
    ctx = Context(fn, resolve, dict(config or {}))
    stats: List[Stat] = []

    def run(n: str) -> None:
        p = REGISTRY[n]
        for r in p.requires:
            if r not in ctx.results:
                run(r)  # never run, or invalidated by a transform since
            else:
                stats.append(Stat(r, 0.0, True))
        t0 = time.perf_counter()
        # a pass that is not an analysis leaves its return value (often None) as a marker, so it counts as run
        ctx.results[n] = p.run(ctx)
        stats.append(Stat(n, time.perf_counter() - t0))
        for gone in p.invalidates:
            ctx.results.pop(gone, None)

    for n in order(names):
        if n not in ctx.results:
            run(n)
    return Result(fn, ctx.diagnostics, ctx.results, stats)


def run_module(tree, names: Sequence[str], build: Optional[Callable] = None) -> ModuleResult:
    """Build the IR of a checked module tree and run `names` on every function."""
    order(names)  # a bad pass name fails before any work
    built = (build or ir.build_module)(tree)
    out = ModuleResult(built, skipped=list(built.skipped))
    for f in built.functions:
        out.functions.append(run_function(f, names, built.resolver))
    return out


# ---------------------------------------------------------------- the standard passes


@register
class CfgPass(Pass):
    name, kind, summary = "cfg", "analysis", "the control-flow graph (successors, dominators, loops)"

    def run(self, ctx: Context):
        return ir.CFG(ctx.fn)


@register
class VerifyPass(Pass):
    name, kind, summary = "verify", "check", "structural well-formedness of the IR (a failure is a bug in the builder)"

    def run(self, ctx: Context):
        problems = ir.verify(ctx.fn)
        for p in problems:
            ctx.report("KIFI000", f"internal: malformed IR of {ctx.fn.name}: {p}", ctx.fn.node)
        return problems


@register
class ProvePass(Pass):
    name, kind, requires, summary = "prove", "transform", ("cfg",), "show which run-time checks cannot fail (sets Check.proof)"

    def run(self, ctx: Context) -> int:
        from . import prove

        return prove.prove_function(ctx.fn, ctx.get("cfg"))


@register
class RefutedPass(Pass):
    name, kind, requires, summary = "refuted", "check", ("prove",), "report an operation whose run-time check provably fails (a constant index past the end of an array)"

    def run(self, ctx: Context) -> int:
        n = 0
        for _, c in ctx.fn.checks():
            if c.refuted and not c.tag:
                ctx.report("KIFI010", f"{c.kind} check always fails: {c.refuted}", c.site, error=False)
                n += 1
        return n


@register
class RequireProvedPass(Pass):
    name, kind, requires, before = "require_proved", "check", ("prove",), ("elide",)
    summary = "an error for every run-time check that `#[require_proved]` demands and the provers could not discharge"

    def run(self, ctx: Context) -> int:
        kinds = ctx.fn.require_proved
        if not kinds:
            return 0
        n = 0
        for _, c in ctx.fn.checks():
            if c.kind in kinds and c.proof is None and not c.tag:
                why = f": {c.refuted}" if c.refuted else ""
                ctx.report("KIFI011", f"{c.kind} check could not be proved, but #[require_proved] demands it{why}", c.site)
                n += 1
        return n


@register
class BorrowsPass(Pass):
    name, kind, requires, summary = "borrows", "check", ("cfg",), "the borrow check on the block graph: a reference used after its referent changed, died or was borrowed incompatibly"

    def run(self, ctx: Context) -> int:
        from . import borrowck

        return borrowck.check(ctx)


@register
class TaintPass(Pass):
    name, kind, requires, summary = "taint", "check", ("cfg",), "the IR backstop of the taint check: tainted data deciding a branch, an index, a divisor, a shift or a call target (KIFT001, 002, 008)"

    def run(self, ctx: Context) -> int:
        from . import taint_ir

        return taint_ir.check(ctx)


@register
class ContractsPass(Pass):
    name, kind, requires, summary = "contracts", "check", (), "a `#[preserves]` function must not change what it promises to keep (KIFI030)"

    def run(self, ctx: Context) -> int:
        from . import contracts

        return contracts.check(ctx)


@register
class DropsPass(Pass):
    name, kind, requires, summary = "drops", "transform", ("cfg",), "decide where owned values are dropped (sets Dead.drop and Store.drop_old)"

    def run(self, ctx: Context) -> int:
        from . import drops

        return drops.analyze(ctx.fn, ctx.get("cfg"))


@register
class TempDropsPass(Pass):
    name, kind, requires, summary = "tempdrops", "transform", ("cfg",), "drop owned temporaries nobody holds (adds Release)"

    def run(self, ctx: Context) -> int:
        from . import tempdrops

        return tempdrops.analyze(ctx.fn, ctx.get("cfg"))


@register
class ElidePass(Pass):
    name, kind, requires, summary = "elide", "transform", ("prove",), "remove the checks the provers showed cannot fail; mark calls that may skip the callee's guard"

    def run(self, ctx: Context) -> int:
        from . import elide

        return elide.elide(ctx.fn, ctx.config.get("access") or {})


PIPELINES: Dict[str, Tuple[str, ...]] = {
    "drops": ("drops",),
    "verify": ("verify",),
    "prove": ("prove",),
    "elide": ("elide",),
}


def check_module(tree) -> List[dg.Diagnostic]:
    """What the IR check passes (`kind == "check"` and cheap: `refuted`) say about a module that compiles: the diagnostics `kif lint` and the
    editor show next to the checker's. A function the IR builder cannot handle has none."""
    return run_module(tree, ["refuted", "require_proved", "borrows", "contracts", "taint"]).diagnostics


def named_pipeline(name: str) -> Optional[List[str]]:
    """The passes of a pipeline name, or None when `name` is not one. `default` is every registered pass: a pass that should not run
    by default is the exception, and is left out with `-name`."""
    if name == "default":
        return [n for n, p in REGISTRY.items() if p.default]
    return list(PIPELINES[name]) if name in PIPELINES else None


def pipeline(spec: str) -> List[str]:
    """Pass names from a comma-separated spec: pipeline names (`default` = every pass, `verify`, `prove`) and pass names are added in the
    order written, `-name` removes a pass added so far (`default,-prove,borrows`). The result is not yet ordered by constraints:
    `order()` / `run_function` do that."""
    out: List[str] = []
    for tok in (s.strip() for s in spec.split(",")):
        if not tok:
            continue
        if tok.startswith("-"):
            out = [n for n in out if n != tok[1:]]
            continue
        for n in named_pipeline(tok) or [tok]:
            if n not in out:
                out.append(n)
    return out


from . import c_refusals  # noqa: E402,F401  (registers `c_refusals`)
