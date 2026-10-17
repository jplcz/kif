<!--
SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF

SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
-->

# tests/c: kif files checked through the C backend

Each case is a set of files with the same stem, run by `tests/test_c_files.py`:

| File | Meaning |
|------|---------|
| `NAME.kif` | the program, lowered with `lower_c` for the `c` target (the file stem is the C name prefix) |
| `NAME.c` | a driver (`main`) that includes `NAME_proc.h`, calls the lowered code and prints |
| `NAME.out` | the exact stdout the driver must print |
| `NAME.refuse` | instead of `.c`/`.out`: the file must be refused; this text must appear in the error message |
| other `.kif` | a library imported by a case (`imp_lib.kif` for `imp_main.kif`); the runner lowers the imports too, each to its own `_proc.h`, and `kif_c.h` is generated beside them |

The generated C is compiled with `gcc -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined`, so a warning in generated code fails the test, and so does a leak or a double free: drivers never free what kif code owns, because the compiler inserts the drops (see `autodrop.kif`). `cells.kif` covers hosted `#[tls]` cells (its driver also links the generated `.c` and starts a second thread). `optional.kif` covers `optional<T>` (`some`/`none`, `value`, `value_or`, `reset`, an owning `optional<str>`).
`strslice.kif` covers `&[str]` (iterate and index) over a C array of `struct kif_str`.
`forslice.kif` covers `for x in` over borrowed slices, arrays and vectors (references to the elements).
`enumelems.kif` covers enums as slice, vector and optional elements.
`ops.kif` covers `#[ops]` handles (`&mut dyn counter`, two implementers, a default body).
`kvdb.kif` is a larger stress program (text-command parser, growing open-addressing hash table over vectors, `#[ops]` sinks, optionals, `?`); `kvdb_model.py` is an independent dict-based model (with a port of the table's probing and growth rule for the `slots` column) whose output must equal `kvdb.out`. Guide: docs/kvdb-example.md.
Add a case by dropping in new files; no test code changes.

Output: each run leaves the compiler's actual output in `tests/c/out/` (git-ignored): `NAME/NAME_proc.h` (and `NAME_proc.c` when
there are methods, `#[export]` functions or cells), the compiled `driver`, `gcc.log`, `driver.out`; a refused case leaves `NAME.refused` with the message.
Open those files to see exactly what the compiler generated.

`comments.kif` shows how `//` comments are lowered: function documentation becomes a `/** */` block on the prototype and the definition, and comments above statements follow their `#line` markers. Read `out/comments/comments_proc.{h,c}` after a run; the rules are in docs/kif-c-backends.md, "Source comments".
