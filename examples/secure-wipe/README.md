# Secure wipe demo

A small program and a runner to watch `#[secure_wipe]` (docs/kif-secure-wipe.md) grow, one implementation step at a time.

| File | Contents |
|------|----------|
| `demo.kif` | A secure struct, a marked variable, a `#[secure_wipe]` function, a marked parameter and a `volatile` variable |
| `driver.c`, `demo.out` | A `main` that calls the exported functions, and the exact output it must print |
| `errors.kif` | One construct per declaration error `KIFW001`-`KIFW005` |
| `run.py` | Lowers both files, compiles the driver with `gcc -Wall -Wextra -Werror` and sanitizers, runs it, compares with `demo.out` |

```sh
./run.py                       # generic C target
./run.py --target freebsd-kernel   # explicit_bzero instead of memset (lowered, not compiled)
```

Like `tests/c`, the run keeps everything in `out/<target>/` (git-ignored); open those files to see what the compiler generated:

| Output | Contents |
|--------|----------|
| `demo_proc.h`, `demo_proc.c` | the generated C: look for `KIF_SECURE_WIPE` |
| `demo.ir` | the IR after every pass: look for `wipe %x` lines |
| `errors.txt`, `diagnostics.txt` | diagnostics of `errors.kif` and of `demo.kif` (empty) |
| `wipe-plan.txt` | the target's macro, body, volatile body, header and `KIFW006` state |
| `kif_c.h`, `gcc.log`, `driver`, `driver.out` | the shared header, the compiler's log, the program and its output |

`tests/test_wipe_demo.py` runs the same thing in the suite (all three built-in targets) and pins the expected wipe counts, so each
step that adds wipes fails it until the counts there are raised.

## What each step adds to the output

| Step | Where to look |
|------|------------------------------|
| Stage 1, declarations (done) | diagnostics: `errors.kif` reports `KIFW001`-`KIFW005`; `demo.kif` is clean |
| Stage 4, target section (done) | the last block, "wipe plan": macro, body, volatile body, header, error |
| Stage 2, `Wipe` instruction and `wipes` pass (done) | `wipe %acc  // last use of 'acc'` lines in `demo.ir`, after the last use of each secure variable (`checksum` wipes `acc` once, after the loop) |
| Stage 3, secure structs (done) | `session_key_drop_fields` wipes `bytes` and `counter`, and skips `owner`; `nonce_sum` calls `nonce_drop(&n)` at scope exit |
| Stage 5, C emission (done except `volatile`) | `#define KIF_SECURE_WIPE(p, n) ...` and a commented call at every wipe in `demo_proc.c` |
| Stage 6, guard | a pass that removes a wipe is rejected with `KIFW010` |

`demo.kif` currently has 5 wipes in the IR (`acc`, `a`, `b`, `secret`, `buf`) and 9 in the C (those plus the four field wipes of `session_key` and `nonce`). The `volatile` variant is the remaining part of stage 5.
