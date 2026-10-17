<!--
SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF

SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
-->

# kiflang

Front end of the procedural kif language ([`docs/kif-proc-language.md`](../docs/kif-proc-language.md)). This package
replaces the earlier interface-only front end, which has been removed. Code generation has started: `kifc --c DIR` lowers
the C subset (stage 1: functions, structs, enums, constants, arrays, control flow; see `docs/kif-c-backends.md`). The C++
backend and the FreeBSD kobj output are not implemented yet.

| Module | Contents |
| --- | --- |
| `source.py` | `Pos`/`Range` (0-based line and column, same as LSP) |
| `diagnostics.py` | `Diagnostic` (range, severity, code, message) |
| `lexer.py` | `lex(text)`: tokens plus lexical diagnostics, never raises; `unescape`, `parse_int` |
| `ast.py` | Syntax tree dataclasses; every node has a `range` |
| `parser.py` | `parse(text)`: recovering parser; `valid_name()` |
| `lower_c.py` | The C backend: `lower(mod, target, stem, filename, note)` returns `{file: text}` (`<stem>_proc.h`, and `<stem>_proc.c` for `#[export]` functions); `LowerError` for what is not lowered yet |
| `kifc.py` | The compiler command line (`--c DIR`, `--target`, `--profile`, `--sanitize`, `--cfg`, `--manifest`, `--depfile`, `--merge-manifest`) |
| `cmode.py` | C-mode restrictions (`KIFP053`) and construction-protocol signatures (`KIFP086`) |
| `methods.py` | Checker step 4: the method index (`impl`, `extern impl`, interface methods) and `KIFP026-028` |
| `bodies.py`, `types.py` | Checker steps 2-3, run by `analyze`: scopes (`KIFP010`, `KIFP020`, `KIFP170`) and gradual expression types (`KIFP011-014`, `KIFP031`), moves/definite assignment (`KIFP022-024`) borrows (`KIFP021`, `KIFP033`) `match` exhaustiveness (`KIFP030`) recursion (`KIFP032`) `unsafe` (`KIFP070`, `KIFP131`) and atomic context (`KIFP110-113`); only plain data is strict |
| `reflect.py` | Read-only reflection handles (`Module`, `Interface`, `Method`, `Type`, `Attr`, ..) the interpreter gives to `gen fn` (section 6.1) |
| `cells.py` | `#[tls]`/`#[per_cpu]` statics and `slot<T>`: zeroable and constant-initialiser checks, accessors, per-CPU and TLS rules (section 4.17); the interpreter has no cells |
| `sysroot.py` | `--sysroot`: the exact, only place `import` reads from (unlike C/C++, no other search paths or fallbacks; see doc §15.2); `Sysroot` is what tools use |
| `modfile.py`, `cbor.py` | Precompiled `.kifm` modules (bodies stripped, generics kept, digests) and the CBOR codec |
| `__main__.py` | `python -m kiflang check\|export\|modules` |
| `interp.py` | Tree-walking interpreter over the checked AST (plain-data subset): C-exact integer semantics, structs, enums, arrays, references, methods, lambdas; generation-time `str`/`Vec`/`Option`/`format!`; traps `KIFP100`, `fail!` `KIFP102`, unavailable calls `KIFP101`, limits `KIFP103` |
| `mimport.py` | `.m` importer: `python3 -m kiflang.mimport [-o out.kif] file.m` converts a FreeBSD kobj interface to `.kif` (`CODE`/`HEADER` become `code(c)`/`header(c)`, `PROLOG`/`EPILOG` become `m_prolog`/`m_epilog`, C types are declared `extern "C" type`); checked over every `sys/**/*.m` by `tests/test_mimport.py` |
| `lsp.py` | language server over stdio (`python3 -m kiflang.lsp`, `kif-lsp`): diagnostics, symbols, definition, hover, references, rename, completion, formatting, quick fixes, `kif/importM`; see `docs/kif-tools.md` |
| `fmt.py` | token-based formatter (`format_text`), raw blocks untouched; see `docs/kif-tools.md` |
| `lint.py` | lint rules KIFL001-034 (KIFL020-029, 033 need a baseline manifest), `.kiflint` config, `// kif-lint:` suppression |
| `manifest.py` | ABI manifest (`kif manifest`): method ids, signature hashes, defaults, layouts; docs/kif-abi.md |
| `target.py` | Target definitions (JSON): what the C backends emit per platform; `kif target`; built-ins in `targets/`; docs/kif-targets.md |
| `graph.py` | import / extends / implements graph as DOT or Mermaid |
| `doc.py` | Markdown documentation generator |
| `quickfix.py` | diagnostic-to-edit fix engine (`fixes_for`, `fix_all`, `apply_edits`) |
| `kif.py` | CLI: `python3 -m kiflang.kif fmt\|lint\|graph\|doc\|fix` |
| `astutil.py` | AST walking and source-text helpers shared by the tools |
| `prelude/` | The shipped `reloco.kif` (global) and `microfmt.kif`, plus its own [README](prelude/README.md); also the default sysroot |
| `symbols.py` | `analyze(key, text, resolver)`: modules, `use`/`import`, the global `prelude/reloco.kif`, name and lifetime resolution (`KIFP010/016/130/160-162`) |

## Use

```python
from kiflang import parse

# `parse` never raises: it returns the (possibly partial) tree and the diagnostics.
tree, diags = parse(open("clock_if.kif").read())
for d in diags:                           # Diagnostic(range, severity, code, message)
    print(d.format("clock_if.kif"))       # clock_if.kif:3:9: error KIF001: expected ';' ...

# Items are in `tree.items`; helpers: tree.of(ast.FnDecl), iface.methods
from kiflang import ast
for fn in tree.of(ast.FnDecl):
    print(fn.name.text, [p.name.text for p in fn.params])
```

## What the parser does and does not do

* Syntax only: names are not resolved, types are not checked, attributes are not validated (that is the checker's
  job: unknown attributes, `#[kobj]` gating, `KIFP*` rules).
* Recovery: inside a block it resynchronises at the next `;` or the closing `}`; inside an interface, `impl`, enum,
  struct or register block at the next member; at top level at the next item keyword. One syntax error is
  `KIF001`; keyword/ill-formed names are `KIF003`; old `self`/`softc`/... clauses are `KIF087`.
* Lexical diagnostics: `KIF001` unterminated string, `KIF002` unterminated verbatim body, `KIF004` invalid
  character, `KIF005` stray `#`, `KIF006` malformed number, `KIF007` bad string escape.
* `>>`, `>=` and `>>=` are never lexed as one token; the expression parser joins adjacent `>` `>` / `>` `=` so that
  nested generics (`vector<vector<u8>>`) need no special casing.
* Generic arguments in expressions need a turbofish (`f::<u8>(x)`), except a leading type path followed by `::`
  (`ptr<ring>::try_allocate(..)`).
* Struct literals are not recognised in `if`/`while`/`for`/`match` heads (as in Rust); wrap them in parentheses.
* Attributes keep `raw` (source text between the parentheses) and, when the arguments are names/literals, a
  structured `args` list; `self(struct foo *)` has `args = None`.
* Lifetimes (`'a`, `'static`, `'_`) lex as one `lifetime` token; they appear in `&'a T`, `&'a self`, generic parameters
  (`<'a, 'b: 'a, T>`) and generic arguments (`view<'a, u8>`). `symbols.py` checks that they are declared, that struct
  fields name theirs, and that an elided result lifetime has a source.
* Comparison operators are non-associative (`a < b < c` is an error), precedence is Rust's.

Tests: `tests/test_proc_lexer.py`, `tests/test_proc_parser.py` (the latter also parses every example of the design doc).
