<!--
SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF

SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
-->

# tests

`pytest` suites for the procedural-language front end `kiflang` (standard library only apart from pytest).

| File | Covers |
| --- | --- |
| `test_proc_lexer.py`, `test_proc_parser.py` | Tokens and the recovering parser |
| `test_proc_symbols.py` | Checker: types, generics, moves, borrows, lifetimes, `match`, atomic context |
| `test_proc_interp.py`, `test_proc_cells.py` | Generation-time interpreter, reflection, per-CPU/TLS cells |
| `test_proc_sysroot.py` | Sysroot and precompiled `.kifm` modules |
| `test_proc_tools.py` | fmt, lint, graph, doc and the quick-fix engine |
| `test_abi.py` | ABI manifests and baseline lint rules (KIFL020-029, KIFL033) |
| `test_mimport.py` | `.m` importer: types, comments, defaults, hooks |
| `test_lsp.py` | Language server requests |
| `test_lower_c.py` | C backend: differential tests against the interpreter, CLI |
| `test_c_files.py` | Runs each case in `tests/c/` (kif + C driver + expected output) |
| `test_editor_syntax.py` | Generated syntax-highlighter word lists match the compiler |

Run: `python3 -m pytest tests/test_*.py -q`.
