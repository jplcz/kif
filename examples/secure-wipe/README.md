# Secure wipe demo

A small program and a script to watch `#[secure_wipe]` (docs/kif-secure-wipe.md) grow, one implementation step at a time.

| File | Contents |
|------|----------|
| `demo.kif` | A secure struct, a marked variable, a `#[secure_wipe]` function, a marked parameter and a `volatile` variable |
| `errors.kif` | One construct per declaration error `KIFW001`-`KIFW005` |
| `show.sh` | Prints diagnostics, the IR, the generated C and the target's wipe plan |

```sh
./show.sh                              # demo.kif for the generic C target
./show.sh errors.kif                   # the declaration errors
./show.sh demo.kif freebsd-kernel      # explicit_bzero instead of memset
./show.sh demo.kif linux-kernel        # memzero_explicit
```

## What each step adds to the output

| Step | Where to look in `./show.sh` |
|------|------------------------------|
| Stage 1, declarations (done) | diagnostics: `errors.kif` reports `KIFW001`-`KIFW005`; `demo.kif` is clean |
| Stage 4, target section (done) | the last block, "wipe plan": macro, body, volatile body, header, error |
| Stage 2a, `Wipe` instruction (done) | nothing yet: the IR can hold a `wipe %x` line, but no pass inserts it |
| Stage 2b, `wipes` pass | `wipe %acc  // ...` lines in the IR, after the last use of each secure variable |
| Stage 3, secure structs | `session_key_drop_fields` wipes `bytes` and `counter`, and skips `owner` |
| Stage 5, C emission | `#define KIF_SECURE_WIPE(p, n) ...` and a commented call at every wipe in `demo_proc.c` |
| Stage 6, guard | a pass that removes a wipe is rejected with `KIFW010` |

Until stage 2b the IR and C of `demo.kif` contain no wipes, which is expected.
