# Stack usage demo

Runs the stack analysis of `docs/kif-stack.md` on `demo.kif`, one stage at a time, and writes what each stage computes to
`out/<target>/` (git-ignored) so you can read it. `./run.py [--target T]` prints the summary.

| Stage | Output |
|-------|--------|
| 3, call graph | `stack-depth.txt`: frame + deepest chain per function, the chain itself, incomplete (an extern without `#[stack(N)]`) and `KIFW014`; `recursion.txt`: the recursion file (`KIFP032`, unbounded, `KIFW013`) |
| 4, budgets | `stack-depth.txt` also holds `KIFW010` (over budget, with chain and biggest variables) and `KIFP084` (one variable over budget): `over`, `huge_local`, `counter::bump`; `within`, `exempt` (`#[small_stack(off)]`) and `bump_more` (widened) are quiet |
| 5, tools | `tools.txt`: the hover text and inlay hint of every function and the quick fixes the editor offers for each `KIFW010` |
| 6, measurement | `stack-check.txt`: `measure.kif` compiled with `gcc -O0 -fno-stack-protector -fstack-usage`; per function the estimate, the compiler's frame and the chain. For your own build: `kif stack-check measure.kif --su <build dir>` reads the `.su` files of GCC or clang, or in CMake `kif_stack_check(name SOURCES measure.kif TARGET lib)` (`docs/kif-cmake.md`) |
| 2, frames | `stack-report.txt`: the frame of every function, biggest first, with its biggest variables; unknown frames apart (same as `kif ir --stack-report`) |
| 1, layout | `layout.txt`: size and alignment of every struct and enum (`unknown` for a generic) |

Files: `demo.kif` (the program), `measure.kif` (clean file for stage 6), `recursion.kif` (an error example), `run.py` (the runner; `tests/test_stack_demo.py` pins its output), `out/` (generated).
