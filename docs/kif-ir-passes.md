# Writing IR passes

A guide to `kiflang/passes.py`, the pass manager of the mid-level IR (docs/kif-ir.md). Read kif-ir.md first for the IR itself
(blocks, `Local`, `Temp`, `Check`, `site`); this document is only about adding a pass.

## 1. The model

A pass is a class with a name, a kind and the names of the passes it needs first. The manager puts the requested passes in
dependency order, runs each **once per function**, and caches what an analysis returned so two passes needing the CFG share one.

| kind | what it does | what the manager does with it |
|------|--------------|-------------------------------|
| `analysis`  | computes something other passes read | caches the result under the pass name; `ctx.get("name")` returns it |
| `transform` | annotates or rewrites the IR (`prove` sets `Check.proof`) | drops the cached results listed in `invalidates` |
| `check`     | reports diagnostics, changes nothing | collects what `ctx.report` was given |

Rules, and why they exist:

1. **A check is a second opinion, never the authority.** The checker on the syntax tree (kiflang/bodies.py) decides what is
   accepted. A check pass may *add* an error where the AST pass was silent; it must never be needed for a program to be
   rejected that the AST pass accepts as correct, and never suppress a diagnostic. Reason: the IR builder skips constructs it does not model, so
   a rule that only lived in the IR would silently stop applying to those functions.
2. **A transform keeps the IR a record of what the source asked for.** Annotate (`Check.proof`), do not delete. Reason: the
   serialized IR, the editor hints and `--explain-checks` all read the original operations.
3. **A pass declares what it needs and what it breaks.** `requires` for inputs, `invalidates` for the cached analyses it makes
   stale. Reason: the manager cannot see inside `run`; a missing declaration is a stale cache. An analysis does **not** rerun
   after every transform: it is cached until a transform lists it in `invalidates`, and is then recomputed lazily, by the next
   pass that `requires` it. Nobody needing it means no recomputation. So list in `invalidates` only what the transform really
   changes (`prove` changes none).
4. **A pass is deterministic and local to one function.** Reason: results are cached per function and the editor re-runs a
   single function after an edit (kiflang/hints.py).
5. **Be conservative.** On anything the pass does not understand, give up on that fact, never guess. Reason: a wrong proof
   removes a safety check, and a wrong error blocks a correct program.
6. **A function the builder cannot handle is not given to any pass** (`ir.Unsupported`). `run_module` returns it in
   `ModuleResult.skipped`, so a caller can tell the user that it had no second opinion.

## 2. A complete pass

```python
from kiflang import passes


@passes.register                      # makes the pass available by name; registering a name twice is an error
class Unproved(passes.Pass):
    # `name` is what `--passes` and `requires` use. `kind` is one of analysis | transform | check.
    name = "unproved"
    kind = "check"
    # Passes that must have run before this one. The manager runs them first and caches them, so `ctx.get("prove")` is ready.
    # "prove" itself needs "cfg", which is pulled in the same way.
    requires = ("prove",)
    summary = "warn about run-time checks the provers could not remove"   # shown by `kif ir --list-passes`
    # Optional soft ordering against passes that need not be present, e.g.:  after = ("verify",)   before = ("some-transform",)

    def run(self, ctx: passes.Context):
        n = 0
        for _, check in ctx.fn.checks():            # every `Check` instruction of the function, with its block
            if check.proof is None and not check.tag:   # a tagged check is a parameter fact, reported elsewhere
                n += 1
                # `site` is the AST node the instruction came from; `report` ignores instructions that have none.
                # error=False makes it a warning. Codes of IR passes are KIFI###.
                ctx.report("KIFI900", f"{check.kind} check stays at run time", check.site, error=False)
        return n                                    # stored as ctx.results["unproved"]
```

Run it:

```sh
kif ir --list-passes                               # the registered passes, kinds and what each needs
kif ir file.kif --passes unproved --stats          # one pass (its requirements run first); --stats prints the time per pass
kif ir file.kif --passes verify,prove              # several; they are ordered by their dependencies
```

From Python (the editor and the C backend do this):

```python
from kiflang import passes

# One built function, with an optional `resolve(call) -> FnDecl | None` for passes that look at callees.
result = passes.run_function(fn, ["prove"], resolve)
result.diagnostics        # what the check passes reported
result.results["cfg"]     # what an analysis returned (the CFG here, because `prove` required it)
result.stats              # [Stat(name, seconds, cached)]

# A whole checked module: builds the IR, runs the passes on every function, lists what the builder had to skip.
module_result = passes.run_module(tree, passes.pipeline("default"))
module_result.skipped     # [(function name, why)]
```

## 3. Reading the IR in a pass

* **Walk** with `ctx.fn.blocks` (the first is the entry), `block.instrs` and `block.term`; `ctx.fn.checks()` yields
  `(block, Check)` pairs; `ctx.fn.locals` are the slots. The CFG (`ctx.get("cfg")`, from `requires = ("cfg",)`) has successors,
  predecessors, dominators and loop headers.
* **Values** are single-assignment `Temp`s and `Const`s. Mutable state lives only in `Local`s through `Load` and `Store`, so a
  dataflow fact about a variable is a fact about a slot (`prove.py` keys its facts on `Local` objects).
* **Source locations**: every instruction has `site`. Report there. A pass must not invent a site.
* **Dataflow**: for a forward analysis follow `kiflang/prove.py`: a state per block entry, a transfer function over the
  instructions, a join at merges (intersect what holds on every path), and a worklist until nothing changes. Kill facts at
  `Store`, `Clobber` and a mutable `Borrow` (and `Live`, which starts a binding afresh). For ownership and scope questions use
  `Move`, `Dead` and the `path` of a `Borrow` (docs/kif-ir.md, "Ownership and scope nodes"); `fn.instrs()` walks every instruction.

### The standard passes

| Pass | Kind | Requires | What it does |
|------|------|----------|--------------|
| `cfg` | analysis | | builds the control-flow graph |
| `verify` | check | `cfg` | reports malformed IR |
| `prove` | transform | `cfg` | sets `Check.proof` on checks that cannot fail |
| `elide` | transform | `prove` | removes the checks `prove` showed cannot fail (kept in `Function.elided`) and marks calls whose callee requirements are all proved `unguarded`; rules below. Backends emit every `Check` that is left, so they never read proofs |
| `drops` | transform | `cfg` | forward dataflow over each owning slot (`HOLDS` / `EMPTY`); fills `Dead.drop` and `Store.drop_old` with `""`, `"drop"` or `"flag"`. Part of `default`; leave it out with `-drops` when the backend does not need drop decisions |
| `refuted` | check | `prove` | warns `KIFI010` at an operation whose run-time check provably fails: a constant index not below the length of a fixed-size array (`a[7]` on `[u8; 4]`). A warning, not an error: the program still builds and panics there at run time, as before |
| `require_proved` | check | `prove` | an error `KIFI011` for every run-time check that `#[require_proved]` demands in scope and the provers could not discharge (see "Requiring proofs" below) |
| `borrows` | check | `cfg` | the borrow check on the block graph: errors `KIFI020` (a reference used after its slot was written, moved or borrowed incompatibly), `KIFI021` (outlives its referent), `KIFI022` (two conflicting borrows in one call). Rules below |
| `contracts` | check | none | verifies `#[preserves]` on functions with a body: error `KIFI030` where the body writes a field of a preserved `&mut` parameter or passes it to a callee that does not preserve it (element writes and preserving calls are fine). A reference kept in another variable, a struct or a tuple, or taken out of one by a pattern, is followed. One that is returned is the caller's business: its writes through the result are checked where they happen. The pass may miss a violation, it never reports a false one |
| `tempdrops` | transform | `cfg` | adds `Release` for an owned temporary nobody holds. One never handed on (`mk(n)?.len()`, `mk(n)?;`) is released right after its last use, when every use is a call or `len` that borrows nothing from it and all are in its defining block (a loop drops it once per iteration). One handed on later (`pair { a: mk(n)?, b: mks(m)? }`) is tracked by a forward dataflow, and a `Release` is put before every `return` it is still held at (the failing `?`); `Release.drop` is `"flag"` when it is held on some paths only. `result<T>` temps are not tracked. `Temp.owns` says which temps have drop glue |
| `c_refusals` | check | `cfg` | errors `KIFC101`-`KIFC110` for what the C backend will not lower (a manual `drop`, an ignored `result`, an owning value copied out, ...). **Not in `default`** (`Pass.default = False`): other targets may honour these programs, so the C lowering appends it. Rules and rationale: docs/kif-ir-codegen.md, "Refusals" |

### What `elide` removes

* **Operation checks** (`index`, `range`, `div`, `shift`, `some`, `non_null`): when proved, unless the target's `access.<class>` tier is
  `checked` (`optional` covers `some` / `non_null`), which keeps every check of the class (docs/kif-targets.md).
* **Entry assumptions** (a parameter fact at the top of a function with a guarded entry): always; `F__nofacts` assumes them.
* **Requirements at a call** (`Check.call` is the `Call.id`): always, because the callee's guarded entry `F` checks them itself. When
  every requirement of the call is proved (and its tier allows it) the call gets `unguarded = True` and may call `F__nofacts`;
  otherwise it calls `F`. A requirement that provably fails stays in the IR for the caller to report.

Each removed check is appended to `Function.elided` (its `proof` is the reason, `None` for a requirement left to the callee's guard);
the dump lists them as `// elided: ...` and the data encoding keeps them. Because `elide` requires `prove`, leaving out `prove` means
leaving out `elide` too (`default,-elide,-prove`).

## 4. Choosing a kind

* Computes a table other passes use and changes nothing: `analysis`. Return it from `run`.
* Sets a flag or annotation on IR objects (as `prove` does): `transform`. Say in `invalidates` which cached analyses that breaks
  (setting `Check.proof` breaks none; deleting or reordering blocks breaks `cfg`).
* Only produces messages: `check`. Do not mutate anything, so a check can run on IR that other consumers share.

## 5. Order and pipelines

**Ordering.** Three things decide the order, strongest first:

1. `requires` (hard): the needed pass runs first and is pulled in even if it was not asked for.
2. `after = ("x",)` / `before = ("x",)` (soft): for two passes that do not need each other's *result* but whose order matters
   (say a check that must see the IR after a transform). A constraint on a pass that is not in the run is ignored, so a pass can
   name its neighbours without forcing them in. Constraints that contradict each other are an error (`pass ordering constraints conflict`).
3. The order you list them in (`--passes b,a`), for passes nothing orders. A required pass sits just before its first user.

Use `requires` when the pass reads a result; use `after`/`before` when it only needs a position. Reason: a soft constraint
never changes *which* passes run, so adding a pass cannot drag others in.

**Pipelines.** `passes.pipeline(spec)` turns a comma-separated spec into pass names. `default` is **every registered pass**: a new
pass runs by default, and the exception is the pass that should not, which is left out with `-name`. Names are added in the
order written and `-name` removes one added so far:

```sh
kif ir f.kif --passes default                 # everything, ordered by the constraints
kif ir f.kif --passes default,-elide,-prove  # everything except the provers (elide needs prove, so both go)
kif ir f.kif --passes verify,mypass,prove     # exactly these (plus what they require), mypass between verify and prove
```

`verify` and `prove` are also pipeline names; add more to `PIPELINES` in `kiflang/passes.py` when a named set is useful.
`kif ir --prove` is `--passes prove`. `-name` does not remove a pass another one `requires` (it would simply be pulled back in).

## 6. Code generation is the last stage

The C backend runs the pipeline of the target's `ir.passes` (default: every registered pass) on each function before it emits it,
and then reads what the passes left in the IR (`Check.proof`, `Check.refuted`, ...). So a new pass runs in every build without
touching the backend, an error it reports (`ctx.report(.., error=True)`) stops the build, and a target that must not pay for a
pass leaves it out (`ir.passes = "default,-name"`, docs/kif-targets.md). Warnings are not shown by the backend.
A pass the backend *depends on* for its output (`prove`) must not be excluded unless the loss is acceptable: without it every
check stays, which is slower but never wrong.

## 7. Testing a pass

Build a module from a source string, run the pass, and look at the diagnostics or annotations (see tests/test_passes.py):

```python
from kiflang import passes, symbols

tree = symbols.analyze("/t/main.kif", "fn f(s: &[u32]) -> u32 { s[0] }", None).tree
res = passes.run_module(tree, ["unproved"])
assert [d.code for d in res.diagnostics] == ["KIFI900"]     # the check on s[0] could not be proved
```

A test that registers a throw-away pass should copy the registry first (`monkeypatch.setattr(passes, "REGISTRY",
dict(passes.REGISTRY))`), so it does not leak into other tests.

`kif lint` and the language server also run the cheap check passes (`passes.check_module`: today `refuted`) once a file compiles, so
these warnings show next to the checker's and the lint rules'.

## Borrow check: `borrows`

Where the checker (`kiflang/bodies.py`) follows loans through straight-line code, `borrows` (`kiflang/borrowck.py`) follows them over the
block graph, so loans carried round a loop and through the value of an `if`/`match` are caught too.

```text
# Rules, and why each exists
loan        a Borrow (slot + path); it travels with every temp/slot that holds a reference derived from it
            (Store, Make, Project, Call results by Call.flows = the operands the callee's result lifetimes name)
shape reads a load of a slot that only `Len` reads (or nobody: the load before an element borrow or an index check) reads no
            element, so it does not conflict with a `mut` loan on one element
live        a loan is live while a temp or slot holding it is read later (backward liveness) -- non-lexical lifetimes,
            so `let r = &x; x = 2;` is fine when `r` is never read again
KIFI020     write / move / borrow of a slot part while a live loan overlaps it: a shared loan only conflicts with
            writes, moves and `&mut`; a `&mut` loan conflicts with everything, reads included
KIFI021     the slot dies (Dead) while a live loan holds it; or a returned value borrows a by-value parameter
KIFI022     two operands of one call hold overlapping loans on one slot, at least one mutable: `f(&mut x, &x)`
overlap     paths `.f`, `[]`: one is a prefix of the other; two constant, different element indexes do not overlap
two-phase   the `&mut` borrow of a call's receiver is only reserved while the arguments are evaluated: `a.set(a.get() + 1)` is fine
reborrow    a borrow through a reference (`*p`, or a slot whose type is `&T`) creates no loan on the slot, it passes p's loans on
unknown     a call whose callee is unresolved assumes the result may borrow from every operand
```

Opt-out: none (`-borrows` drops the pass from a pipeline). Not modelled: types with lifetimes that are not spelled in the type text
carry no loans (missed errors, never false ones).

```kif
fn f(c: bool) -> u32 {
    let mut x: u32 = 1;
    let mut y: u32 = 5;
    let r = if c { &x } else { &y };   // r may borrow x
    x = 3;                             // KIFI020: x written while r is read below
    *r
}
```

## Explaining checks

`kif ir FILE --explain-checks` lists every run-time check (index, range, optional, div, shift) at the end of the expression it guards,
as `proved: <why>` (the prover's reason, which the code generator then leaves out) or `kept`:

```
$ kif ir m.kif --no-project --explain-checks
m.kif:1:42: index check proved: constant index 2 < 4     // a[2] on [u32; 4]
m.kif:1:49: index check kept                             // a[i]: nothing bounds i
```

The editor shows the same as inlay hints (`kif.server.inlayHints.checks`).

## Requiring proofs: `#[require_proved]`

By default a check the provers cannot discharge simply stays in the generated code. Code that must not panic at run time (an interrupt
handler, a hot path whose bounds were reasoned out on paper) can say so, and the build then fails when the reasoning and the prover
disagree:

```kif
#![require_proved(div)]            // a whole file: every division must have a divisor the prover knows is non-zero

#[require_proved]                  // a whole `impl` (or one function): every check kind in every method
impl ring {
    fn at(&self, i: usize) -> u8 {
        self.buf[i & 7]            // proved: `i & 7` is at most 7, below the length 8
    }
    fn bad(&self, i: usize) -> u8 {
        self.buf[i]                // error KIFI011: index check could not be proved, but #[require_proved] demands it
    }
}

#[require_proved(index, shift)]    // only these kinds; others (`some`, `div`, ...) may stay
fn mix(t: [u8; 8], x: usize, n: u32) -> u8 { t[x & 7] }
```

Rules and rationale:

- **Scopes** are a file (`#![require_proved]`), an `impl` and a function; they add up, and the kinds of every enclosing scope are demanded.
  Narrow the scope by moving the code into its own function: statements and blocks take no attributes.
- **Kinds** are `index`, `range`, `some`, `non_null`, `div` and `shift`; a bare attribute means all of them. Anything else is KIFP217,
  so a typo cannot leave a requirement switched off.
- **Why an error, not a warning:** the point is a guarantee. The prover only grows (an interval or a new fact can turn a failing
  function green), so the error is never a reason to stop using it, but one that appears after a refactor is exactly what the
  attribute is for. Fix it by giving the prover the fact (a mask, a dominating test, a loop bound) or by restructuring.
- Parameter-fact checks (`requires` at a call site) are not affected: they are already enforced.
- It runs in the default pipeline, `kif lint` and the language server, so the error shows in the editor as well.
