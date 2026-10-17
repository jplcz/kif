# kif tools: fmt, lint, graph, doc, fix, manifest, lsp

These tools work on `.kif` files of the procedural language (`docs/kif-proc-language.md`).
They are written against the new AST in `kiflang/`.

```
python3 -m kiflang.kif fmt   [--check|--diff] [--indent N] FILE...   # '-' reads stdin
python3 -m kiflang.kif lint  [--config F|--no-config] [--baseline MANIFEST]... [--Werror] [--list-rules] [--sysroot D] FILE...
python3 -m kiflang.kif graph [--format dot|mermaid] [--files-only] [-o OUT] FILE...
python3 -m kiflang.kif doc   [-o DIR] FILE...
python3 -m kiflang.kif ir    [--format text|json|cbor] [--ast] [--prove] [--passes LIST (e.g. default,-prove)] [--list-passes] [--stats] [-o OUT] [--sysroot D] FILE   # mid-level IR (--prove marks provable checks; --passes runs IR passes, docs/kif-ir-passes.md) or syntax tree (docs/kif-ir.md)
python3 -m kiflang.kif fix   [--apply] [--unsafe] [--diff] FILE...
python3 -m kiflang.kif manifest [-o DIR] [--sysroot D] FILE...        # ABI manifest for `lint --baseline`
```

Unified front end: `kif check|build|export|modules|lsp|import-m` hand their arguments to `kif-check`, `kifc`, `kif-lsp` and
`kif-mimport`. Extras:

```
kif new DIR [--kind driver|library] [--target T] [--toml]   # CMake project (CMakeLists.txt first; kif.toml only with --toml)
kif completion bash|zsh|fish                                 # completion script generated from the real option parsers
kif check|build --watch [--interval SECONDS]                 # rerun on every .kif/.kifm/.m/.toml change (polling)
kif check|lint --format text|json|sarif|github               # machine-readable findings (SARIF 2.1.0; GitHub annotations)
kif project [--cmake|--json]                                 # show the resolved kif.toml
```

`--watch` is for quick feedback; a CMake build (`cmake --build`) is the primary path. With several files `--format json|sarif`
prints one document per file. `[watch] interval` in `kif.toml` sets the polling interval.

Exit codes: `0` ok, `1` findings (fmt `--check` differs, lint errors or `--Werror` warnings, compile errors), `2` usage or bad config.

## fmt

Token based and idempotent. Rules:

* indent by open-bracket depth (`{ ( [ #[`), 4 spaces by default (`--indent`);
* a line ending in an operator, or starting with `.`, `&&`, `||` and similar, gets one extra level;
* comment-only lines take the indent of the next code line (the inner level before a closer);
* blank lines collapse to one, trailing whitespace is removed;
* raw blocks (`header(c) { ... }`, `code(cxx) { ... }`, `m_prolog`, `m_epilog`) are never touched, and their closing `}` stays in column 0;
* if the token stream would change, `FormatError` is raised and the file is left alone.

```python
from kiflang.fmt import format_text

# Returns the formatted text; format_text(format_text(x)) == format_text(x).
out = format_text(open("dev.kif").read(), indent=4)
```

## lint

Configuration is a `.kiflint` file found by walking up from the input (`--config` overrides, `--no-config` ignores):

```
# <rule> = off | warning | error
KIFL011 = error        # enums must name their type
KIFL005 = warning      # turn the doc-comment rule on
max_symbol_length = 40 # KIFL004 threshold
max_unsafe_statements = 8  # KIFL032 threshold
```

Opt out in source (the comment applies to the declaration on the next line, or the whole file):

```
// kif-lint: disable=KIFL011
enum mode { a, b }

// kif-lint: disable-file=KIFL001   (anywhere before the first declaration)
```

Interface rules apply to `#[kobj]` interfaces only (except KIFL003 and KIFL005, which apply to all).

| Rule | Default | What it flags, why, how to fix |
|------|---------|--------------------------------|
| KIFL001 | warning | `#[kobj]` interface name ends in `_if`; generated C names would repeat it. Rename `dev_if` to `dev`. |
| KIFL002 | warning | Method name starts with the interface name; the C symbol would stutter (`dev_dev_read`). Rename `dev_read` to `read`. |
| KIFL003 | warning | One-letter parameter name in an interface method; unreadable in generated docs. Use a descriptive name. |
| KIFL004 | warning | C symbol `<interface>_<method>` longer than `max_symbol_length`. Shorten a name or raise the limit. |
| KIFL005 | off | Interface, method, struct or enum without a `//` doc comment. Add one (the fix inserts a stub). |
| KIFL010 | warning | `#[kobj]` without `version(..)`; ABI changes cannot be tracked. Add `version(1)`. |
| KIFL011 | warning | Enum without an underlying type. Write `enum mode: u8`. |
| KIFL012 | warning | Platform-width type (`usize`, `isize`, `long`, `size_t`) in a `#[kobj]` signature; the ABI differs per target. Use `u32`/`u64`. |
| KIFL013 | warning | Struct passed or returned by value in a `#[kobj]` signature. Pass `c_ptr<T>` or a reference. |
| KIFL014 | warning | `override` adds a default body that no ancestor interface had. Use a plain method. |
| KIFL015 | warning | `#[flags]` enumerator that is not 0, a single bit, or a union of the enum's single bits. |
| KIFL016 | warning | `#[flags]` enum without a zero enumerator. Add `none = 0`. |
| KIFL017 | warning | `#[flags]` enumerators with the same value. Remove or merge them. |
| KIFL020 | error | With `--baseline`: a `#[kobj]` interface of the released manifest is gone. Keep it. See `kif-abi.md`. |
| KIFL021 | error | With `--baseline`: a method was removed without raising `version`. Keep it or bump the version. |
| KIFL022 | error | With `--baseline`: a method's signature hash changed without a version bump. Add a new method instead. |
| KIFL023 | error | With `--baseline`: a method was added without a default and without a version bump. Give it a default body. |
| KIFL024 | error | With `--baseline`: `version` went down. |
| KIFL025 | off | With `--baseline`: methods were added but `version` is unchanged. |
| KIFL026 | error | With `--baseline`: `extends(..)` changed. |
| KIFL027 | error | With `--baseline`: an enum or struct of the released manifest is gone. |
| KIFL028 | error | With `--baseline`: enum or struct layout changed incompatibly (only appending enumerators is compatible). |
| KIFL029 | error | With `--baseline`: a method lost its default without a version bump. |
| KIFL030 | warning | `private fn` that nothing in the file uses. Remove it. |
| KIFL031 | off | `let` that shadows a visible binding. Rename it. |
| KIFL032 | off | `unsafe` block with more than `max_unsafe_statements` statements; keep unsafe regions small. |
| KIFL033 | error | With `--baseline`: the `self(..)` handle type changed without a version bump. |

The baseline rules (KIFL020-029, KIFL033) run only when a released manifest is given with `--baseline`; the manifest, its
format, the rules with examples and the workflow are in [`kif-abi.md`](kif-abi.md).

```python
from kiflang.symbols import analyze
from kiflang.lint import lint, Config

mod = analyze("dev.kif", text)              # symbols.Module
for d in lint(mod, Config(max_symbol_length=30)):
    print(d.code, d.range.start.line + 1, d.message)
```

## graph

Nodes are files and their declarations; edges are `import`, `extends` (`#[extends(base)]`) and `implements` (`impl I for S`).
Prelude interfaces are skipped. `--files-only` keeps only file-to-file import edges.

```python
from kiflang import graph

g = graph.build([mod])                      # list of symbols.Module (imports are followed)
print(graph.to_mermaid(g))                  # flowchart LR ...
print(graph.to_dot(g, files_only=True))     # Graphviz
```

## doc

`doc.render(mod)` produces Markdown: enums (values, `#[flags]` notes), structs, interfaces (C symbols, defaults, implementors),
impls, functions, constants, statics, aliases, extern types, imports and `use`. Signatures are copied from the source and
`//` runs above a declaration become its description. `doc -o DIR` writes one `.md` per input (`doc.doc_path`).

## fix (quick-fix engine)

`kiflang/quickfix.py` maps diagnostics to edits. `fix` lists them; `--diff` previews; `--apply` writes the *auto* fixes;
`--unsafe` also applies the *preferred* fix of each remaining diagnostic. After each round the file is re-checked; a round that
introduces a new error is reverted and its fixes are skipped. Overlapping edits raise `ConflictError`.

| Diagnostic | Fix | Auto |
|------------|-----|------|
| KIFP010 | map C type (`int` to `i32`), add `use mod::name;`, did-you-mean | no |
| KIF010 | rename the duplicate (`a_2`) | no |
| KIF012 | remove the unused import | no |
| KIF001 | insert the missing `;` | no |
| KIFP020 | `let` to `let mut` | no |
| KIF088 | remove the dangling `m_prolog`/`m_epilog` block | no |
| KIF089 | remove the earlier block of the pair (preferred) or this one | no |
| KIFL001, KIFL002 | rename (other files may refer to the name) | no |
| KIFL010 | add `version(1)` | yes |
| KIFL011 | add `: i32` (or `: u8` alternative) | yes |
| KIFL016 | add `none = 0` | no |
| KIFL005 | doc stub | no |
| KIFL030 | remove the function | no |
| every KIFLnnn | suppression comment (declaration or file) | no |
| KIFT005 | mark the function `#[tainted_result]` (callers then get tainted data) | no |
| KIFT003 | mark the callee's parameter `#[tainted]` (same module only) | no |
| KIFT004 | mark the destination struct or static `#[tainted]` (taints the whole struct) | no |
| KIFT001, KIFT002 | wrap the expression in `untaint!("TODO: justify", clean(..))` for each one-argument `#[untaint]` function of the module (at most 3) | no |
| KIFT010 | add the missing reason: `untaint!("TODO: justify", ..)` | no |

The KIFT fixes decide what is trusted at a security boundary, so none is `auto` or `preferred`: `kif fix` never applies them and the
editor offers them only as a lightbulb action. The `TODO: justify` reason is a placeholder for you to write the real one (see docs/kif-taint.md).

```python
from kiflang import quickfix

# Fix everything that is safe; unsafe=True also takes the preferred fix of each remaining finding.
text, applied = quickfix.fix_all("dev.kif", text, unsafe=False)

# Or drive it from an editor: fixes for one diagnostic, preferred first.
mod, diags = quickfix.check("dev.kif", text)
for f in quickfix.fixes_for(mod, diags[0], diags):
    print(f.title, f.auto)
new_text = quickfix.apply_edits(text, f.edits)

# Add your own fixer: return Fix objects with Edit(range, new_text).
@quickfix.fixer("KIFL017")
def drop_duplicate(ctx, d):
    return []
```

## lsp (language server)

`python3 -m kiflang.lsp` (installed as `kif-lsp`) speaks LSP over stdio; the standard library is all it needs.
It is built on the tools above, so the editor shows exactly what the command line prints.

| Request | Behaviour |
|---------|-----------|
| diagnostics | compiler diagnostics, plus the lint rules once the file compiles (like `kif lint`); updated on every change |
| documentSymbol | declarations with interface methods, enum members and struct fields nested |
| definition | names (also `a::b::c` and `use`d names) and `import "x.kif"` paths |
| hover | the declaration signature plus its `//` doc comment, and for a function with taint attributes what they mean |
| code lens | `untaint: N callers` above each `#[untaint]` function (docs/kif-taint.md); a click lists the call sites |
| references, rename | every use of a declaration in the open documents; rename validates the new name |
| completion | context-aware, see "Completion" below (`kiflang/complete.py`) |
| completionItem/resolve | for the highlighted completion item: `detail` becomes the full declaration (`fn read(&self, off: u32) -> u32`, `base: u64`, `struct hpet`) and `documentation` the `//` comment above it. The list itself carries neither (it stays cheap); each item has a `data` key (kind, type, name, document) that the client sends back |
| signatureHelp | inside `f(`, `T::f(` or `x.m(` (triggered by `(` and `,`): the signature, the argument under the cursor highlighted, the `//` doc comment. Works for free functions, associated functions, methods (receiver type inferred as for completion, the receiver is not listed) and prelude types; nested calls pick the innermost open one |
| workspace/symbol | declarations of the open documents whose name contains the query (case-insensitive), with their members and the container's name |
| documentHighlight | every use of the symbol under the cursor in this document; for a local or field (no declaration to resolve), the same name inside the enclosing function |
| inlayHint | `: type` after an unannotated `let` (the checker's type) and `name:` before each argument of a call to a known free function (left out when the argument is a variable of that very name); and the run-time checks of the visible functions, see "Check hints" below |
| semanticTokens/full | names that resolve to a struct, enum, interface, function or constant, so they are coloured by what they are; everything else stays with the TextMate grammar |
| documentLink | the path of each `import "x.kif";` links to the resolved file (not the C/C++ header imports) |
| foldingRange | multi-line `{ .. }` blocks (the closing-brace line stays visible) and runs of two or more `//` lines (kind `comment`) |
| formatting | `fmt` for the whole document; an unformattable file is reported as an error |
| codeAction | the quick fixes of `kiflang/quickfix.py` for the diagnostics in range, and `source.fixAll.kif` (the auto fixes) |
| `kif/previewC` | custom request: runs the C backend (`kifc --c --emit-source`, plus `--freebsd` when the target has a kobj section) on the open text in a temporary directory and returns `{ok, files: [{name, text}], messages}`; `{uri, target}` in, target default `c`. Nothing is written next to the source |
| `kif/importM` | custom request: converts FreeBSD `.m` text to `.kif` (`{text, name}` gives `{kif, warnings}`) |

**Completion.** The text before the cursor picks the list; names come from the module, its imports and the prelude.
Triggered by `:`, `[`, `.` and `(`.

| Cursor after | Offered | Example |
| --- | --- | --- |
| `#[` / `#![` | attribute names | `#[kobj` |
| `#[name(` | the attribute's arguments, in the table `ATTR_ARGS` of `kiflang/complete.py` (below); words already written after a comma are left out; an unknown attribute offers nothing | `#[repr(c` |
| `x.` | fields and methods (with receiver) of the type of `x`. The type comes from the checker: the half-typed `x.` is cut back to `x`, analysed, and the type of the expression before the dot is used, so unannotated locals, call chains (`make().me().`), closure parameters (`CELL.with(\|c\| c.`) and prelude types (`vector`, `optional`) all work. Only when that fails does it fall back to scanning the function text for `x: T`, `let x = T { .. }`, `let x = T::f(..)` | `self.base`, `h.read(off)` |
| `a::b::` | names of module `a::b` | `lib::` |
| `Enum::` | the variants | `mode::fast` |
| `Type::` | the associated functions (no receiver) | `hpet::make(..)` |
| `:` `->` `<` | types only (no keywords, functions or constants) | `fn f(x: hpet) -> u32` |
| anything else | keywords (declaration words only at top level or in `impl`/`interface`, statement words only inside bodies), snippets for `fn struct enum impl interface if while for loop match let const import`, builtin types, names in scope, modules | `fn` expands to a function skeleton |
| `import "` | the `.kif` files and directories next to the document and in the configured include directories (`includeDirs`), below the part of the path already typed; a directory inserts `name/` and re-opens the list | `import "sub/deep.kif"` |
| two or more letters of a name | declarations (`fn struct enum interface const`) of neighbouring `.kif` files this file has not imported, ranked last; accepting one inserts `stem::name` **and** adds `import "stem.kif";` (after the last import, else after the leading comment block) | `lib_` becomes `lib::lib_add` plus the import |
| `Name {` / `Name { a: 1, ` | the fields not written yet, and `remaining fields` that fills all the missing ones | `hpet { base: 1, ` |
| `let x: T = ` | constructors of `T` first (sort key `0`): its struct literal, the associated functions returning `T`/`Self`, its enum variants; `some`/`none` for `optional`, `ok`/`err` for `result` | `let h: hpet = hpet::make()` |

**Ranking** (`sortText`, so every editor shows the same order): `0` construction patterns for the expected type · `1_n` locals of the enclosing function (parameters, `let`, `for`), the nearest declaration first · `2_` names of the module and its imports, the ones the file already uses most first · `3_` keywords, snippets and modules · `4_` prelude names · `5_` builtin types · `9_` names from files not imported yet. A local hides a global of the same name. The server cannot see which item the user accepts (LSP has no such notification), so "recently used" means "used in this file".

**`error::` and `cfg`**: `error::` lists reloco's error names (`ERROR_ERRNO` in `kiflang/target.py`, with the errno each maps to) unless the module has its own `error` module. Inside `cfg(` or `#[cfg(`, including nested `all(`/`any(`/`not(`, it offers `all`/`any`/`not`, the bare flags and the `key = "value"` keys; after `key = "` it offers that key's values. The flags and values are the union over every built-in target and profile (`cfg_environment` in `kiflang/complete.py`), because the checker examines all variants rather than one target; a project-local target file is not read.

**Attribute arguments** (what `#[name(` offers): `repr`: `c`, `packed` · `unsafe`: `relocatable` · `derive`: `clone`, `trace` and every `gen fn derive_<name>` of the module and its imports · `tls`: `atomic_ok` · `per_cpu`: `irq` · `small_stack`: `off` or a number · `stack`, `align`, `version`: a number · `errno`: `positive`, `negative` · `extends`: the interfaces in scope · `self`, `softc`: the structs in scope · `send`, `foreign`: the types in scope · `lang`: the `lang` items of the prelude · `export`: `cxx` · `target`: `c`, `no_i64_div` · `header`, `link_name`, `cxx_name`, `operator`, `object`: a string. When a new attribute takes arguments, add it to `ATTR_ARGS`.

**Construction patterns and templates** (snippets; the placeholder text is what stays if you just press Tab):

| Where | Label | Inserts |
| --- | --- | --- |
| top level | `impl S { new }` | a constructor taking every field without a default and building `S { .. }` |
| top level | `impl S { try_create }` | the fallible form: `fn try_create(..) -> result<Self> { ok(Self { .. }) }` |
| top level | `impl I for ..` | an `impl` of an interface in scope with every required method stubbed, receiver (`&self`, `&mut self`, `self`) and signature copied from the interface |
| top level | `kobj interface`, `ops interface` | a documented `#[kobj, version(1)]` / `#[ops]` interface skeleton |
| top level | `tls cell`, `per_cpu cell`, `tls slot`, `per_cpu slot` | the `#[tls]` / `#[per_cpu]` static declarations (slots with `slot::new(init)`) |
| top level | `exported function`, `inline function`, `result function` | `#[export]` / `#[inline]` functions and a `-> result<T>` function ending in `ok(..)` |
| in a body | `S { .. }` | a struct literal of every struct in scope, fields filled with `0`/`false`/a named placeholder |
| in a body | `match v { every E }` | an exhaustive `match` for each parameter, annotated local or unannotated `let v = ..` (typed by the checker; enums carry no data, so arms are plain variants) of enum type `E`, one arm per variant |
| in a body | `ok(..)`, `err(..)`, `some(..)`, `none()`, `if cond return err`, `cell with / with_mut` | the usual result/optional/guard/cell-access shapes |

Placeholders use the LSP snippet syntax (`${1:name}`, `$0`); a client without snippet support shows them as plain text.

Methods and functions insert as snippets with a placeholder per argument. Unknown receivers (a type the scan cannot find)
return an empty list rather than unrelated names.

Initialization options (VS Code: `kif.server.*`): `includeDirs` (extra import roots), `lint` (default true), `lintConfig`
(a `.kiflint` path; the nearest `.kiflint` is used when empty) and `baselines` (ABI manifest paths; the one named like the
document turns on KIFL020-033 for it, see `kif-abi.md`) and `inlayHints` (`{checks, delayMs}`, see below).

### Check hints

The inlay hints also show what the IR provers (`kif-ir.md`) decided about each run-time check, at the end of the guarded
expression: `⚠ bounds` where the check stays, `✓ bounds` where it is dropped (the tooltip gives the proof). Kinds: `bounds`
(index), `range` (slice range), `has-value` (`optional::value()`), `divisor`, `shift`. They follow the `checked` / `prove`
tiers only in that they show the provers' verdict, not the target's: use `kif ir --prove` or the C output for the real one.

```jsonc
// settings.json
"kif.server.inlayHints.checks": "kept",   // off | kept (only ⚠, the default) | all (⚠ and ✓)
"kif.server.inlayHints.taint": true,      // ⛓ speculation barriers and 🛈 untaint! reasons (docs/kif-taint.md), independent of `checks`
"kif.server.inlayHints.delayMs": 400      // grace period, see below
```

Proving costs more than the rest of the hints, and editing re-requests hints on every keystroke, so:

- **Pull only.** Nothing is computed outside `textDocument/inlayHint`, and only for the functions that overlap the visible range.
- **Per-function cache.** A function's hints are keyed by its own text plus the text of every declaration that is not a function
  (structs, enums, constants, impl headers), because those decide what can be proved. Editing function A recomputes only A; a
  function that merely moved keeps its hints (they are stored relative to its start). A file with errors shows no check hints.
- **Grace period.** A function whose text is not in the cache is left without check hints until the document has been quiet for
  `delayMs` (stale hints would sit at wrong places). When that time has passed the server sends `workspace/inlayHint/refresh` (if the
  client advertises `workspace.inlayHint.refreshSupport`; VS Code does) and the client asks again. `Server.wait_time()` and
  `Server.tick()` drive this; `kif-lsp` reads stdin on a thread so it can wake without a message. Opening a file computes at once.
- `checks: "off"` skips all of it.

```python
from kiflang.lsp import Server

s = Server()                                    # transport independent: no process needed
s.handle({"id": 1, "method": "initialize", "params": {}})
# didOpen returns the publishDiagnostics notification to send back to the client.
out = s.handle({"method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": "file:///tmp/a.kif", "text": "enum m { a }\n"}}})
```

The C++ preview request (`kif/cxxPreview`) of the old server is not provided until code generation is ported.

## `.m` hook diagnostics

`m_prolog` and `m_epilog` apply to the next method only.

| Code | Meaning |
|------|---------|
| KIF088 | A hook with no method after it in the interface; it would never run. Move it before a method or delete it (quick fix). |
| KIF089 | The same hook twice before one method; the first would be ignored. Delete one (quick fix offers both). |
| KIFP140 | `m_prolog`/`m_epilog` or `#[self]`, `#[softc]`, `#[errno]`, `#[version]` on an interface without `#[kobj]`; add `#[kobj]`. |

```
#[kobj, version(1)]            // kobj: hooks and version are allowed
interface dev {
    m_prolog {                 // raw C that runs before `read` (closing brace in column 0)
}
    method read(offset: u32) -> u32;
    m_epilog {                 // KIF088: nothing follows, it never runs
}
}
```

## Syntax highlighters

`kif-editor/` carries highlighters for VS Code/JetBrains (TextMate), Vim, Emacs, Sublime Text and tree-sitter
(Neovim, Helix, Zed). The compiler is the single source of the word lists: `python3 kif-editor/gen_syntax.py`
regenerates them from `kiflang/lexer.py`, `kiflang/symbols.py` and `kiflang/lsp.py`, and `--check` fails if a file is stale.
Raw blocks (`header(c)`, `code(cxx)`, `m_prolog`, ...) are highlighted as embedded C or C++ by tag. See
`kif-editor/README.md` for installation and what is verified.

```
python3 kif-editor/gen_syntax.py          # rewrite the generated regions (those between GEN-BEGIN / GEN-END)
python3 kif-editor/gen_syntax.py --check  # CI: exit 1 if anything is out of date
```
