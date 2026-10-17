# The kvdb stress program

`tests/c/kvdb.kif` is the largest program in the C corpus: a text-command key/value store written in kif, compiled by `kifc`, linked
with the driver `tests/c/kvdb.c`, and run under `-fsanitize=address,undefined`. It exists to exercise many language features at once
and to check the generated C against an independent model.

## What it contains

| Part | Features exercised |
|---|---|
| tokenizer + `parse` | byte slices, `&[u8]` ranges, `?` on `result`, enums, bounds checks the provers can discharge |
| `table` | `vector<entry>` slots, a `vector<u8>` key arena, open addressing, tombstones, rehash and growth |
| `exec` | dispatch through `&mut dyn reply` (`#[ops]` interface with two implementers `checksum` and `tally`) |
| workload | an LCG line generator (`make_line`, `word`, `put_number`, `put_text`) |
| `run(n, start_slots)` | `result<report>`; the wrappers `checksum_of`, `errors_of`, `live_of`, `slots_of`, `sum_of` give the driver plain values |

## How it is checked

```text
kvdb.kif  --kifc-->  kvdb_proc.{h,c}  +  kvdb.c (driver)  --gcc + ASan/UBSan-->  driver  -->  stdout
                                                                                              ==  kvdb.out
kvdb_model.py (plain Python dict, same generator, same FNV-1a fold)  -->  stdout               ==  kvdb.out
```

```python
# kvdb_model.py: what is independent, and why it is faithful where it matters
db = {}            # answers: values never depend on the hash table, so a dict is the oracle
shape = Shape(8)   # the `slots` column: a port of the table's probing, tombstones and rehash rule (keys and states only),
                   # because the final slot count depends on where keys land (a tombstone reused or not) and not on values
h = (h ^ code) * 1099511628211 & M64   # replies fold into FNV-1a 64: the same numbers must come out of C and Python
```

The table's growth rule (kvdb.kif `set`): before inserting a new key, if `(used + tombs + 1) * 4 > slots * 3` the table rehashes into
`slots * 2` when more than half would be live, else into the same size (this only drops tombstones). The starting size changes the shape
but never the answers, which the `start=` lines of `kvdb.out` check.

## Run it

```sh
python3 tests/c/kvdb_model.py              # the expected output
python3 -m pytest tests/test_c_files.py -q -k kvdb
cat tests/c/out/kvdb/kvdb_proc.c           # the generated C
```
