# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Differential tests for the C backend: lower kif, compile with gcc, compare with the interpreter."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from kiflang import interp, kifc, lower_c, target
from kiflang.symbols import analyze

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not available")

SRC = """
enum mode: u8 { a, b = 5, c }
const N: u32 = 4;
fn sum(n: u32) -> u32 { let mut t: u32 = 0; for i in 0..n { t += i * N; } t }
fn fact(n: u64) -> u64 { let mut r: u64 = 1; let mut i: u64 = 2; while i <= n { r *= i; i += 1; } r }
fn pick(m: mode, v: i32) -> i32 { match m { mode::a => v / 2, mode::b if v > 3 => -v, _ => v << 1 } }
fn arr(k: usize) -> u8 { let mut a: [u8; 4] = [1, 2, 3, 4]; a[k] += 250; a[k] }
"""


def compile_run(src, driver, panic=None, cflags=(), target_over=None):
    t = target.load("c")
    t.data.update(target_over or {})
    m = analyze("t.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1]
    files = lower_c.lower(m, t, "t")
    d = Path(tempfile.mkdtemp())
    for k, v in files.items():
        (d / k).write_text(v)
    (d / "kif_c.h").write_text(lower_c.common_header(t))
    head = "#include <stdio.h>\n#include <stdlib.h>\n"
    pre = []
    if panic:
        (d / "pre.h").write_text(head + panic)  # the bodies live in `t_proc.c`, so it needs the override too
        pre = ["-include", str(d / "pre.h")]
    (d / "d.c").write_text(head + '#include "t_proc.h"\nint main(void){' + driver + "return 0;}\n")
    r = subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-fsanitize=address,undefined"] + list(cflags) + pre + [str(d / "d.c")] + [str(d / k) for k in files if k.endswith(".c")] + ["-o", str(d / "d")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return m, subprocess.run([str(d / "d")], capture_output=True, text=True)


def test_matches_interpreter():
    m, r = compile_run(SRC, 'printf("%u %llu %d %d %d %u\\n",t_sum(5),(unsigned long long)t_fact(10),'
                      't_pick(mode_a,9),t_pick(mode_b,9),t_pick(mode_c,3),t_arr(1));')
    assert r.stdout.split() == ["40", "3628800", "4", "-9", "6", "252"]


def test_ir_emitter_matches_the_expected_values():
    m, r = compile_run(SRC, 'printf("%u %llu\\n",t_sum(5),(unsigned long long)t_fact(10));', target_over={})
    assert r.stdout.split() == ["40", "3628800"]


IR_CODEGEN = {}

OPTS = """
fn find(v: &[u32], x: u32) -> optional<usize> {
    let mut i: usize = 0;
    while i < v.len() { if v[i] == x { return some(i); } i = i + 1; }
    none()
}
fn pos(v: &[u32], x: u32) -> usize { let r: optional<usize> = find(v, x); if r.has_value() { *r.value() } else { 99 } }
fn pick(o: optional<u8>) -> u8 { match o { Some(x) => x + 1, None => 0 } }
fn lookup(n: u32) -> usize { let a: [u32; 4] = [5, 6, 7, 8]; pos(&a, n) }
fn picked(n: u8) -> u8 { let o: optional<u8> = if n > 2 { some(n) } else { none() }; pick(o) }
"""


def test_ir_emitter_optionals():
    """`optional<T>` (some / none, has_value, value, Some / None patterns) through the IR emitter; the interpreter has no optionals,
    so the expected values are written out."""
    drv = 'printf("%d %d %d %d %d\\n", (int)t_lookup(5), (int)t_lookup(8), (int)t_lookup(9), (int)t_picked(0), (int)t_picked(3));'
    for over in (None, {}):
        _, r = compile_run(OPTS, drv, target_over=over)
        assert r.stdout.split() == ["0", "3", "99", "0", "4"]
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = "\n".join(lower_c.lower(analyze("t.kif", OPTS, cfg_env=t.cfg_env()), t, "t").values())
    for f in ("find", "pos", "pick"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


RESULTS = """
fn half(x: u32) -> result<u32> { if x % 2 == 0 { ok(x / 2) } else { err(error::invalid_argument) } }
fn quarter(x: u32) -> result<u32> { let h = half(x)?; half(h) }
fn nonzero(x: u32) -> result<()> { if x == 0 { err(error::out_of_range) } else { ok(()) } }
fn both(x: u32) -> result<u32> { nonzero(x)?; let q = quarter(x)?; ok(q + 1) }
"""


def test_ir_emitter_results():
    """`result<T>` of a scalar (ok, err, `?`, a call passing the result on) through the IR emitter: a status `int` and the value in `*kif_out`."""
    drv = ('uint32_t v = 77; int rc;\n'
           'rc = t_half(8, &v); printf("%d %u\\n", rc, v);\n'
           'rc = t_half(7, &v); printf("%d %u\\n", rc, v);\n'
           'rc = t_quarter(8, &v); printf("%d %u\\n", rc, v);\n'
           'rc = t_quarter(6, &v); printf("%d\\n", rc);\n'
           'rc = t_both(0, &v); printf("%d\\n", rc);\n'
           'rc = t_both(12, &v); printf("%d %u\\n", rc, v);\n')
    over = {}
    outs = []
    for extra in ({}, IR_CODEGEN):
        _, r = compile_run(RESULTS, drv, target_over={**over, **extra})
        outs.append(r.stdout)
    assert outs[0] == outs[1]
    assert outs[0].split("\n")[:6] == ["0 4", "-22 4", "0 2", "-22", "-34", "0 4"], outs[0]
    t = target.load("c")
    t.data.update({**over, **IR_CODEGEN})
    text = "\n".join(lower_c.lower(analyze("t.kif", RESULTS, cfg_env=t.cfg_env()), t, "t").values())
    for f in ("half", "quarter", "nonzero", "both"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


OWNED = """
struct guard { n: u32 }
impl guard {
    fn new(n: u32) -> guard { guard { n: n } }
    fn get(&self) -> u32 { self.n }
    fn drop(&mut self) { self.n = 0; }
}
fn take(g: guard) -> u32 { g.get() }
fn use_it(x: u32) -> u32 { let g = guard::new(x); g.get() + 1 }
fn early(x: u32) -> u32 { let g = guard::new(x); if x > 3 { return 7; } g.get() }
fn twice(x: u32) -> u32 { let mut g = guard::new(x); g = guard::new(x + 1); g.get() }
fn moved(x: u32) -> u32 { let g = guard::new(x); take(g) }
fn cond(x: u32) -> u32 { let g = guard::new(x); if x > 3 { take(g) } else { 5 } }
fn reas(x: u32) -> u32 { let mut g = guard::new(x); if x > 3 { take(g); } g = guard::new(x + 100); g.get() }
"""


def test_ir_emitter_drops():
    """A local that owns something is dropped where the `drops` pass put the `dead` / reassignment marker, once on every path; a maybe-moved
    value (a drop flag) gets a `kif_live_` variable. Both generators must call `guard_drop` the same number of times in each function,
    except where the IR proves a move."""
    drv = 'printf("%u %u %u %u %u %u %u %u %u\\n", t_use_it(2), t_early(2), t_early(5), t_twice(2), t_moved(4), t_cond(1), t_cond(9), t_reas(2), t_reas(5));'
    texts = {}
    for name, over in (("off", None), ("auto", IR_CODEGEN)):
        _, r = compile_run(OWNED, drv, target_over=over)
        assert r.stdout.split() == ["3", "2", "7", "3", "4", "5", "9", "102", "105"], r.stdout
        t = target.load("c")
        t.data.update(over or {})
        texts[name] = lower_c.lower(analyze("t.kif", OWNED, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]

    def drops(text, f):
        m = re.search(rf"\bt_{f}\([^)]*\)\n\{{((?:(?!\n\}}\n)[\s\S])*)", text)
        return m.group(1).count("guard_drop(")

    for f in ("use_it", "early", "twice"):
        assert drops(texts["off"], f) == drops(texts["auto"], f), f
    assert drops(texts["auto"], "moved") == 0  # moved into `take` on its only path: the IR knows, the AST generator keeps a flag
    assert "kif_live_g" in texts["auto"]  # `cond` and `reas` move `g` on some paths only: a flag says whether it is still there
    for f in ("take", "use_it", "early", "twice", "cond", "reas"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", texts["auto"]), f


VECS = """
struct pt { x: i32, y: i32 }
fn pts() -> result<i32> {
    let mut v: vector<pt> = vector::try_create(1)?;
    v.try_push(pt { x: 1, y: 2 })?;
    v.try_push(pt { x: 3, y: 4 })?;
    let r = v[1].x + v[0].y;
    ok(r)
}
fn fill(n: u32) -> result<u32> {
    let mut v: vector<u32> = vector::try_create(2)?;
    for i in 0..n { v.try_push(i * i)?; }
    v.try_insert_at(0, 100)?;
    v.try_erase_at(1)?;
    v[0] += 1;
    let mut t: u32 = 0;
    for i in 0..v.len() { t += v[i]; }
    ok(t)
}
fn grow() -> result<usize> {
    let mut v: vector<u8> = vector::try_create(0)?;
    v.try_resize(6)?;
    v.try_pop()?;
    let c = v.try_clone()?;
    let n = c.len() + v.capacity();
    let mut c2 = c;
    v.clear();
    ok(n)
}
fn bad() -> result<()> {
    let mut v: vector<u8> = vector::try_create(0)?;
    v.try_pop()?;
    ok(())
}
"""


def test_ir_emitter_vectors():
    """`vector<T>` of scalars: `try_create`, the `try_*` methods, `len`, indexing, element stores and the drop on every path."""
    drv = ('uint32_t a = 0; size_t b = 0; int r1 = t_fill(4, &a); int r2 = t_grow(&b); int r3 = t_bad(); int p = 0; int r4 = t_pts(&p); '
           'printf("%d %u %d %zu %d %d %d\\n", r1, a, r2, b, r3, r4, p);')
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(VECS, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1]
    assert outs[1].split() == ["0", "115", "0", "11", "-2", "0", "5"], outs[1]
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", VECS, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("fill", "grow", "bad", "pts"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


STRS = """
fn early(fail: bool) -> result<usize> {
    let a: vector<u8> = vector::try_create(4)?;
    let s: str = str::try_create("hi")?;
    if fail { return err(error::busy); }
    ok(a.len() + s.len())
}
fn reassign() -> result<usize> {
    let mut s: str = str::try_create("a")?;
    s = str::try_create("bcd")?;
    ok(s.len())
}
fn grow() -> result<usize> {
    let mut s: str = str::try_create("ab")?;
    s.try_append("cde")?;
    s.try_append("!")?;
    ok(s.len() + s.capacity())
}
fn has(s: str_view) -> bool { s.starts_with("ab") && !s.is_empty() }
"""


def test_ir_emitter_strs():
    """`str` and `str_view`: literals as views, `try_create`, the `try_*` methods, `len` and the drop of the owned string."""
    drv = ('size_t a, b, c; int r1 = t_early(0, &a); int r2 = t_early(1, &a); int r3 = t_reassign(&b); int r4 = t_grow(&c); '
           'printf("%d %d %d %d %zu %zu %zu %d\\n", r1, r2, r3, r4, a, b, c, (int)t_has((struct kif_strview){ "abc", 3 }));')
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(STRS, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1], outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", STRS, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("early", "reassign", "grow", "has"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


ITERS = """
struct entry { state: u8, value: u64 }
struct bag { items: vector<entry>, n: u32 }
fn tot(v: &vector<entry>) -> u64 {
    let mut t: u64 = 0;
    for e in v { if e.state == 1 { t += e.value; } }
    t
}
fn tots(v: &[entry]) -> u64 {
    let mut t: u64 = 0;
    for e in v { t += e.value; }
    t
}
fn mk(n: u32) -> result<u64> {
    let mut v: vector<entry> = vector::try_create(1)?;
    for i in 0..n { v.try_push(entry { state: (i % 2) as u8, value: (i * 10) as u64 })?; }
    let r = tot(&v) + tots(&v) + 1000;
    ok(r)
}
"""


def test_ir_emitter_iterators():
    """`for x in v` over a vector or slice (also of structs) keeps a position `kif_it<id>`, set where the iterator is made."""
    drv = 'uint64_t r = 0; int s = t_mk(5, &r); printf("%d %llu\\n", s, (unsigned long long)r);'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(ITERS, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == "0 1140\n", outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", ITERS, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("tot", "tots", "mk"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f
    assert "kif_it" in text


ARRS = """
struct tl { counts: [u32; 4], total: u64 }
enum color { red, green }
struct slotv { used: vector<u8>, n: u32 }
fn bump(a: &mut [u32; 4], i: usize) { a[i] += 1; }
fn sum(a: &[u32; 4]) -> u32 { a[0] + a[1] + a[2] + a[3] }
fn run() -> u32 { let mut a: [u32; 4] = [0, 1, 2, 3]; bump(&mut a, 2); bump(&mut a, 2); sum(&a) }
fn isred(c: &color) -> bool { *c == color::red }
fn mk() -> tl { tl { counts: [1, 2, 3, 4], total: 5 } }
fn tsum() -> u32 { let t = mk(); t.counts[1] + t.counts[3] }
fn setc(t: &mut tl, v: u32) { t.counts[1] = v; }
fn viasetc() -> u32 { let mut t = mk(); setc(&mut t, 40); t.counts[1] + t.counts[0] }
"""


def test_ir_emitter_array_refs():
    """`&[T; N]` / `&mut [T; N]` are pointers to the first element, a struct literal copies its array fields in, and `a.f[i] = x` writes through."""
    drv = 'printf("%u %d %u %u\\n", t_run(), (int)t_isred(&(enum color){0}), t_tsum(), t_viasetc());'
    _, r = compile_run(ARRS, drv, target_over=IR_CODEGEN)  # the AST generator passes `&mut [T; N]` differently, so there is nothing to compare with
    assert r.stdout == "8 1 6 41\n", r.stdout
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", ARRS, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("bump", "sum", "run", "isred", "mk", "tsum", "setc", "viasetc"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


RANGES = """
fn sum(xs: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..xs.len() { t += xs[i]; } t }
fn tail(xs: &[u32]) -> &[u32] { &xs[1..] }
fn mid(n: usize) -> u32 { let a: [u32; 4] = [1, 2, 3, 4]; sum(&a[1..n]) }
fn tl(xs: &[u32]) -> u32 { sum(tail(xs)) }
fn all() -> u32 { let a: [u32; 4] = [1, 2, 3, 4]; tl(&a) }
"""


def test_ir_emitter_ranges():
    """`&a[lo..hi]` checks `lo <= hi <= len` and makes a span from `ptr + lo` and `hi - lo`; a bad range traps."""
    drv = 'printf("%u %u %u\\n", t_all(), t_mid(3), t_mid(2));'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(RANGES, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == "9 5 2\n", outs
    _, r = compile_run(RANGES, "t_mid(9);", target_over=IR_CODEGEN)
    assert r.returncode != 0
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", RANGES, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("sum", "tail", "mid", "tl", "all"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


MISC = """
struct pt { x: i32, y: i32 }
fn word(n: u32) -> u32 {
    match n { 0 | 1 => 10, 2..=5 => 20, _ => 30 }
}
fn find(v: &[pt], k: usize) -> optional<pt> {
    if k < v.len() { let p = pt { x: v[k].x, y: v[k].y }; some(p) } else { none() }
}
fn pick(v: &[pt], k: usize) -> i32 {
    let o = find(v, k);
    match o { Some(p) => p.x + p.y, None => -1 }
}
"""


def test_ir_emitter_misc():
    """Integer patterns (alternatives, ranges, `_`) and `optional` of a struct, with `none()` typed from where it is returned."""
    drv = ('struct pt a[2] = {{1, 2}, {5, 6}}; struct kif_cspan_pt s = {a, 2}; '
           'printf("%u %u %u %u %d %d\\n", t_word(0), t_word(3), t_word(9), t_word(1), t_pick(s, 1), t_pick(s, 5));')
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(MISC, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == "10 20 30 10 11 -1\n", outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", MISC, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("word", "find", "pick"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


ERRS = """
enum color { red, green, blue }
fn g(x: u32) -> result<u32> { if x > 3 { return err(error::busy); } ok(x) }
fn f(x: u32) -> u32 {
    match g(x) {
        ok(v) => v,
        err(e) => { if e == error::busy { 100 } else { 200 } }
    }
}
fn which(x: u32) -> u32 { match g(x) { ok(_) => 0, err(e) => match e { error::busy => 1, _ => 2 } } }
fn cnt(v: &[color], c: color) -> u32 { let mut n: u32 = 0; for x in v { if *x == c { n += 1; } } n }
fn build() -> result<u32> {
    let mut v: vector<color> = vector::try_create(4)?;
    v.try_push(color::green)?;
    v.try_push(color::blue)?;
    v.try_push(color::green)?;
    let r = v.try_at(1)?;
    let blue = *r == color::blue;
    let n = cnt(v.as_slice(), color::green);
    if blue { ok(n + 10) } else { ok(n) }
}
fn run() -> u32 { match build() { ok(v) => v, err(_) => 99 } }
fn pk(x: u32) -> optional<u32> { if x > 1 { some(x * 2) } else { none() } }
fn opts(a: u32) -> result<u32> {
    let mut o: optional<u32> = pk(a);
    let r: &u32 = o.try_value()?;
    let n: u32 = *r + o.value_or(7);
    o.reset();
    ok(n + o.value_or(1000))
}
fn orun(a: u32) -> u32 { match opts(a) { ok(v) => v, err(_) => 99 } }
fn named(keep: bool) -> result<usize> {
    let mut o: optional<str> = none();
    if keep { o = some(str::try_create("hello")?); }
    let mut n: usize = 0;
    if o.has_value() { n = o.value().len(); }
    if n == 5 { o.reset(); }
    ok(n)
}
fn slen(a: &[str]) -> usize { let mut t: usize = 0; for s in a { t += s.len(); } t + a[0].len() }
struct pt { x: i32, y: i32 }
struct label { text: str }
struct framed { p: optional<pt>, l: optional<label>, n: u32 }
fn frame(x: i32, named: bool) -> result<framed> {
    let mut f: framed = framed { p: some(pt { x: x, y: 1 }), l: none(), n: 2 };
    if named { f.l = some(label { text: str::try_create("ab")? }); }
    ok(f)
}
fn frame_sum(f: &framed) -> i32 {
    let mut t: i32 = 0;
    if f.p.has_value() { t += f.p.value().x; }
    if f.l.has_value() { t += f.l.value().text.len() as i32; }
    t
}
fn frame_total(x: i32, named: bool) -> result<i32> {
    let f: framed = frame(x, named)?;
    ok(frame_sum(&f))
}
fn ftrun(x: i32, named: bool) -> i32 { match frame_total(x, named) { ok(v) => v, err(_) => 99 } }
fn greet(name: str_view) -> result<usize> {
    let mut t: str = str::try_create("hello ")?;
    t.try_append(name)?;
    let ok1 = t.starts_with("hello w");
    let n = t.view().len() + t.len();
    if ok1 { ok(n) } else { ok(0) }
}
fn grun(n: str_view) -> usize { match greet(n) { ok(v) => v, err(_) => 99 } }
fn nrun(k: bool) -> usize { match named(k) { ok(v) => v, err(_) => 99 } }
"""


def test_ir_emitter_errors_and_try_at():
    """`result` patterns `ok(v)` / `err(e)`, `error::x` patterns, `vector::try_at` and `as_slice`."""
    drv = 'printf("%u %u %u %u %u %u %u %zu %zu %d %d %zu\\n", t_f(1), t_f(9), t_which(1), t_which(9), t_run(), t_orun(5), t_orun(0), t_nrun(1), t_nrun(0), t_ftrun(5, 1), t_ftrun(5, 0), t_grun((struct kif_strview){ "world", 5 }));'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(ERRS, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == "1 100 0 1 12 1020 99 5 0 7 5 22\n", outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", ERRS, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("f", "which", "build", "run", "opts", "named", "slen", "frame", "frame_sum", "frame_total", "greet"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


def test_ir_emitter_ops_handles():
    """`&mut dyn I` handles of an `#[ops]` interface: calls through the table, a struct coerced at a call and at a `let`."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "ops.kif"), encoding="utf-8").read()
    drv = 'printf("%u %u %u\\n", t_simple_total(), t_doubler_total(), t_twice_total());'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(src, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] and outs[0].strip(), outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("run", "simple_total", "twice_total"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


def test_ir_emitter_temporary_drops():
    """Owned temporaries nobody holds (`mk(n)?.len()`, `mk(n)?;`, a value built before a later failing `?`) are dropped by the `tempdrops`
    pass: same results as the AST generator on success and on every failing path, with ASan checking for leaks and double drops."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "tempdrop.kif"), encoding="utf-8").read()
    drv = ('size_t n = 0; int rc;'
           'for (size_t a = 0; a < 12; a += 5) for (size_t b = 0; b < 12; b += 5) {'
           ' rc = t_lit2(a, b, &n); printf("lit2 %d %zu\\n", rc, n);'
           ' rc = t_cond(a, b, &n); printf("cond %d %zu\\n", rc, n);'
           ' rc = t_recv(a, b, &n); printf("recv %d %zu\\n", rc, n);'
           ' rc = t_args(a, b, &n); printf("args %d %zu\\n", rc, n); }'
           'rc = t_wh(3, &n); printf("wh %d %zu\\n", rc, n); rc = t_discard(4); printf("discard %d\\n", rc);')
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(src, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] and outs[0].strip(), outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("cond", "wh", "recv", "discard", "lit"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


def test_ir_emitter_array_reborrows():
    """`&mut *a` and `a.as_slice()` of an array reference, and a `for` over a `&mut [T; N]`: IR emitter only (the AST generator passes the array differently)."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "forslice.kif"), encoding="utf-8").read()
    drv = 'uint32_t a[4] = {1, 2, 3, 4}; uint32_t b[4] = {1, 2, 3, 4}; printf("%u %u\\n", t_arrays(a), t_views(b));'
    _, r = compile_run(src, drv, target_over=IR_CODEGEN)
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("arrays", "views"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f
    assert r.stdout.split() == ["24", "28"], r.stdout


def test_ir_emitter_struct_arrays():
    """Arrays of structs: a literal, a borrow passed as a slice, an element read."""
    src = """
struct pt { x: u32, y: u32 }
fn total_x(s: &[pt]) -> u32 { let mut t: u32 = 0; for q in s { t += q.x; } t }
fn points() -> u32 {
    let p: [pt; 2] = [pt { x: 5, y: 1 }, pt { x: 7, y: 2 }];
    total_x(&p) * 10 + p[1].y
}
"""
    drv = 'printf("%u\\n", t_points());'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(src, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == "122\n", outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    assert re.search(r"\bt_points\([^)]*\)\n\{(?:(?!\n\}\n)[\s\S])*\t", text)


def test_index_trap():
    m, r = compile_run(SRC, "t_arr(4);")
    assert r.returncode != 0


DIFF = """
fn add(a: i32, b: i32) -> i32 { a + b }
fn sub8(a: u8, b: u8) -> u8 { a - b }
fn mul(a: i32, b: i32) -> i32 { a * b }
fn div(a: i32, b: i32) -> i32 { a / b }
fn rem(a: i32, b: i32) -> i32 { a % b }
fn neg(a: i32) -> i32 { -a }
fn shl(a: u32, n: u32) -> u32 { a << n }
fn shr(a: i32, n: u32) -> i32 { a >> n }
fn inc(lo: u8, hi: u8) -> u32 { let mut t: u32 = 0; for i in lo..=hi { t += i as u32; } t }
fn cls(v: i32) -> i32 { let r = if v < 0 { -1 } else if v == 0 { 0 } else { 1 }; r }
fn boom(v: i32) -> i32 { if v > 2 { panic!("too big"); } v }
fn inc1(a: i32) -> i32 { a + 1 }
fn two(a: i32) -> i32 { inc1(inc1(a)) + inc1(2) }
enum shade { dark, mid, light }
fn lighter(c: shade) -> shade { match c { shade::dark => shade::mid, shade::mid => shade::light, shade::light => shade::dark } }
fn warm(c: shade) -> bool { match c { shade::dark | shade::mid => true, _ => false } }
fn shades(a: u8) -> u8 { let c = if a > 1 { shade::dark } else { shade::light }; if warm(lighter(c)) && c != shade::mid { 1 } else { 2 } }
fn sum4(x: u8) -> u32 { let mut a: [u32; 4] = [1, 2, 3, 4]; a[1] = x as u32; let mut s: u32 = 0; for i in 0..4 { s = s + a[i]; } s }
fn rep(x: u32) -> u32 { let mut a: [u32; 3] = [x; 3]; a[0] = 1; a[1] = 9; a[1] + a[0] + a[2] }
fn untyped(a: u8) -> u8 { let c = if a > 1 { 3 } else { 0 }; c / 3 + 1 }
fn at_of(v: &[u32], i: usize) -> u32 { v[i] }
fn total_of(v: &[u32]) -> u32 { let mut s: u32 = 0; let mut i: usize = 0; while i < v.len() { s = s + v[i]; i = i + 1; } s }
fn put_at(v: &mut [u8], i: usize) { v[i] = 7; }
fn first_of(#[non_empty] v: &[u32]) -> u32 { v[0] }
fn slices(n: usize) -> u32 { let a: [u32; 4] = [1, 2, 3, 4]; total_of(&a) + at_of(&a, n) + first_of(&a) }
fn fill(n: usize) -> u8 { let mut a: [u8; 4] = [0, 0, 0, 0]; put_at(&mut a, n); a[0] + a[3] }
struct ctr { n: u32, step: u32 }
impl ctr {
    fn new(step: u32) -> Self { Self { n: 0, step: step } }
    fn get(&self) -> u32 { self.n }
    fn bump(&mut self) { self.n = self.n + self.step; }
    fn twice(&mut self) -> u32 { self.bump(); self.bump(); self.get() }
}
fn counted(s: u32) -> u32 { let mut c = ctr::new(s); c.bump(); c.twice() + c.get() }
struct pt { x: i32, y: i32 }
fn mkpt(a: i32, b: i32) -> pt { pt { x: a, y: b } }
fn bump(p: &mut pt) { p.x = p.x + 1; }
fn rdy(p: &pt) -> i32 { p.y }
fn both(a: i32) -> i32 { let mut q = mkpt(a, 2); bump(&mut q); rdy(&q) + q.x }
"""

I32 = [0, 1, -1, 7, -7, 2147483647, -2147483648]
CASES = (
    [("add", (a, b)) for a in I32 for b in (1, -1, 2147483647)]
    + [("sub8", (a, b)) for a in (0, 1, 255) for b in (0, 1, 255)]
    + [("mul", (a, b)) for a in I32 for b in (0, 2, -1, 65536)]
    + [("div", (a, b)) for a in I32 for b in (0, 1, -1, 3)]
    + [("rem", (a, b)) for a in I32 for b in (0, -1, 3)]
    + [("neg", (a,)) for a in I32]
    + [("shl", (1, n)) for n in (0, 31, 32, 40)]
    + [("shr", (a, n)) for a in (-8, 8) for n in (0, 1, 31, 32)]
    + [("inc", (0, 255)), ("inc", (250, 255)), ("inc", (5, 3)), ("inc", (7, 7))]
    + [("cls", (a,)) for a in I32]
    + [("boom", (a,)) for a in (0, 2, 3)]
    + [("two", (a,)) for a in (0, -5, 2147483645, 2147483647)]
    + [("shades", (a,)) for a in (0, 1, 2)]
    + [("untyped", (a,)) for a in (0, 2, 255)]
    + [("sum4", (a,)) for a in (0, 7, 255)]
    + [("rep", (a,)) for a in (0, 5)]
    + [("slices", (a,)) for a in (0, 3, 4)]
    + [("fill", (a,)) for a in (0, 3, 4)]
    + [("counted", (a,)) for a in (0, 1, 1431655765, 2147483648)]
    + [("both", (a,)) for a in (0, -5, 2147483646, 2147483647)]
)


def test_differential_against_interpreter(target_over=None):
    t = target.load("c")
    m = analyze("t.kif", DIFF, cfg_env=t.cfg_env())
    rets = {f.name.text: f.ret.text if f.ret else "()" for f in m.tree.items if hasattr(f, "params")}
    lines, want = [], []
    for fn, args in CASES:
        try:
            want.append(str(interp.run(m, fn, *args)))
        except interp.Trap:
            want.append("TRAP")
        cast = "(unsigned long long)" if rets[fn].startswith("u") else "(long long)"
        fmt = "%llu" if rets[fn].startswith("u") else "%lld"
        lines.append(f'printf("{fmt}\\n", {cast}t_{fn}({", ".join(str(a) for a in args)}));')
    # one process per trap-free run is not needed: the driver forks per case so a trap only ends that case
    driver = "int rc = 0; (void)rc;\n" + "\n".join(
        f'if (fork() == 0) {{ {l} fflush(stdout); _exit(0); }} else wait(0);' for l in lines)
    panic = ('#include <unistd.h>\n#include <sys/wait.h>\n'
             '#define KIF_PANIC(f, l, m) do { (void)(f); (void)(l); (void)(m); printf("TRAP\\n"); fflush(stdout); _exit(0); } while (0)\n')
    _, r = compile_run(DIFF, driver, panic, target_over=target_over)
    assert r.stdout.split() == want





def test_differential_with_the_ir_emitter():
    """Every function the IR emitter handles must behave as the interpreter says; the others still come from the AST generator."""
    test_differential_against_interpreter(IR_CODEGEN)
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    m = analyze("t.kif", DIFF, cfg_env=t.cfg_env())
    text = "\n".join(lower_c.lower(m, t, "t").values())
    emitted = {f for f in ("add", "sub8", "mul", "div", "rem", "neg", "shl", "shr", "cls", "boom", "inc1", "two", "mkpt", "bump", "rdy", "both", "lighter", "warm", "shades", "untyped", "sum4", "rep", "at_of", "total_of", "put_at", "first_of__nofacts", "counted", "ctr_new", "ctr_get", "ctr_bump", "ctr_twice") if re.search(rf"(?:\bt_|\b){f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text)}
    assert {"add", "div", "shl", "shr", "cls", "boom", "inc1", "two", "mkpt", "bump", "rdy", "both", "lighter", "warm", "shades", "untyped", "sum4", "rep", "at_of", "total_of", "put_at", "first_of__nofacts", "counted", "ctr_new", "ctr_get", "ctr_bump", "ctr_twice"} <= emitted


def test_unsupported_constructs_are_refused():
    t = target.load("c")
    m = analyze("t.kif", "fn f() -> u8 { let v = (1, 2); 0 }", cfg_env=t.cfg_env())
    with pytest.raises(lower_c.LowerError):
        lower_c.lower(m, t, "t")


def test_kifc_cli(tmp_path):
    src = tmp_path / "x_if.kif"
    src.write_text("#[export] fn go(a: u8) -> u8 { a + 1 }\n")
    out = tmp_path / "gen"
    assert kifc.main(["--c", str(out), "--target", "freebsd-kernel", "--profile", "release", str(src)]) == 0
    h = (out / "x_proc.h").read_text()
    assert "/* kif: target=freebsd-kernel profile=release */" in h and "extern uint8_t go(uint8_t a);" in h
    assert (out / "x_proc.c").exists()
    before = (out / "x_proc.h").stat().st_mtime_ns
    assert kifc.main(["--c", str(out), "--target", "freebsd-kernel", "--profile", "release", str(src)]) == 0
    assert (out / "x_proc.h").stat().st_mtime_ns == before  # unchanged output is not rewritten


def test_kifc_refuses_errors_and_other_backends(tmp_path, capsys):
    src = tmp_path / "bad.kif"
    src.write_text("fn f() -> u8 { missing }\n")
    assert kifc.main(["--c", str(tmp_path / "o"), str(src)]) == 1
    assert not (tmp_path / "o").exists()
    assert kifc.main(["--cpp", str(tmp_path / "o"), str(src)]) == 2


OBJ = """
struct ring { private cap: usize = 0, count: usize = 0, data: [u8; 4] }
impl ring {
    fn try_create(cap: usize) -> result<Self> {
        if cap > 4 { return err(error::invalid_argument); }
        ok(Self { cap: cap, count: 0, data: [0, 0, 0, 0] })
    }
    fn try_construct_at(mem: c_ptr<Self>, cap: usize) -> result<()> { unsafe { (*mem).cap = cap; } ok(()) }
    fn try_clone(&self) -> result<Self> { let mut c = Self { cap: self.cap, count: self.count, data: [0, 0, 0, 0] }; for i in 0..self.count { c.data[i] = self.data[i]; } ok(c) }
    fn push(&mut self, b: u8) -> result<()> {
        if self.count >= self.cap { return err(error::allocation_failed); }
        self.data[self.count] = b;
        self.count += 1;
        ok(())
    }
    fn sum(&self) -> u32 { let mut t: u32 = 0; for i in 0..self.count { t += self.data[i] as u32; } t }
    fn take(self) -> usize { self.count }
    fn magic() -> u32 { 42 }
    fn drop(&mut self) { self.count = 0; }
}
fn demo(cap: usize) -> result<u32> {
    let mut r = ring::try_create(cap)?;
    r.push(7)?;
    r.push(5)?;
    let mut c = r.try_clone()?;
    c.push(1)?;
    let s = c.sum() + ring::magic();
    ok(s)
}
"""


def test_objects_and_results():
    driver = ('uint32_t v = 0; int rc;\n'
              'rc = t_demo(3, &v); printf("%d %u\\n", rc, v);\n'
              'rc = t_demo(2, &v); printf("%d\\n", rc);\n'
              'rc = t_demo(9, &v); printf("%d\\n", rc);\n'
              'struct ring slot; rc = ring_try_construct_at(&slot, 4); printf("%d %u %u\\n", rc, (unsigned)slot.cap, (unsigned)slot.count);\n'
              'struct ring z = RING_INIT; printf("%u %u\\n", (unsigned)z.cap, (unsigned)ring_take(&z));\n')
    _, r = compile_run(OBJ, driver)
    assert r.stdout.split("\n")[:5] == ["0 55", "-12", "-22", "0 4 0", "0 0"], r.stdout


def lower_files(tmp_path, files, main):
    from kiflang import kifc

    for name, text in files.items():
        (tmp_path / name).write_text(text)
    t = target.load("c")
    m = analyze(str(tmp_path / main), (tmp_path / main).read_text(), kifc.import_resolver([str(tmp_path)]), cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    return lower_c.lower(m, t, lower_c.stem_of(main))


def test_import_include_and_names(tmp_path):
    (tmp_path / "lib.kif").write_text("const K: u32 = 1;\nfn f() -> u32 { K }\n")
    out = lower_files(tmp_path, {"m.kif": 'import "lib.kif";\nfn g() -> u32 { lib::f() + lib::K }\n'}, "m.kif")
    h = out["m_proc.h"]
    assert '#include "lib_proc.h"' in h and "lib_f()" in out["m_proc.c"] and "LIB_K" in out["m_proc.c"]


def test_import_refusals(tmp_path):
    lib = "struct s { v: u32 }\nimpl s { fn n(&self) -> u32 { self.v } }\nfn f() -> u32 { 1 }\n"
    with pytest.raises(lower_c.LowerError, match="aliases|alias"):
        lower_files(tmp_path, {"lib.kif": lib, "m.kif": 'import "lib.kif";\nuse lib::s as t;\nfn g(a: t) -> u32 { a.n() }\n'}, "m.kif")
    out = lower_files(tmp_path, {"lib.kif": lib + "fn mk() -> result<ptr<s>> { let p: ptr<s> = ptr::try_allocate(default_allocator())?; ok(p) }\n",
                                 "m.kif": 'import "lib.kif";\nfn g() -> result<u32> { let p: ptr<lib::s> = ptr::try_allocate(default_allocator())?; ok(p.n()) }\n'}, "m.kif")
    assert "KIF_HEAP_S" in out["m_proc.h"] and "s_try_new" in out["m_proc.h"]


OPT = "fn f(x: u32) -> optional<u32> { if x > 1 { some(x) } else { none() } }\n"


def test_optional_runs():
    m, r = compile_run(OPT + "fn g(x: u32) -> u32 { let o: optional<u32> = f(x); o.value_or(7) }",
                       'struct kif_opt_u32 a = t_f(0), b = t_f(3); printf("%d %d %u %u", a.has, b.has, (unsigned)b.v, (unsigned)t_g(0));')
    assert r.stdout == "0 1 3 7"


def test_optional_panics_when_empty():
    m, r = compile_run("fn g() -> u32 { let o: optional<u32> = none(); *o.value() }", "t_g();",
                       panic='#define KIF_PANIC(f, l, m) (puts("panic"), exit(0))\n')
    assert "panic" in r.stdout


def test_optional_refusals():
    t = target.load("c")

    def low(src):
        m = analyze("t.kif", src, cfg_env=t.cfg_env())
        return lower_c.lower(m, t, "t")

    with pytest.raises(lower_c.LowerError, match="owner"):
        low("fn g() -> u32 { let o: optional<str> = none(); let p = o.value_or(1); 1 }")
    with pytest.raises(lower_c.LowerError, match="not lowered"):
        low("fn g() -> u32 { let o: optional<optional<u32>> = none(); 1 }")


def test_optional_match():
    m, r = compile_run("fn f(x: u32) -> optional<u32> { if x > 1 { some(x) } else { none() } }\n"
                       "fn g(x: u32) -> i32 { let o: optional<u32> = f(x); match o { Some(v) => v as i32, None => -1 } }",
                       'printf("%d %d", (int)t_g(0), (int)t_g(4));')
    assert r.stdout == "-1 4"


def test_optional_match_refusals():
    t = target.load("c")

    def low(src):
        return lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")

    with pytest.raises(lower_c.LowerError, match="owns resources"):
        low("fn g() -> usize { let o: optional<str> = none(); match o { Some(s) => 1, None => 0 } }")
    with pytest.raises(lower_c.LowerError, match="temporary"):
        low("fn f() -> optional<u32> { none() }\nfn g() -> u32 { match f() { Some(v) => v, None => 0 } }")


CELL = "#[tls] static N: u32 = 3;\nfn f() -> u32 { N.with_mut(|n| { *n = *n + 1; }); N.with(|n| *n) }\n"


def test_cells_hosted_tls_runs():
    t = target.load("c")
    m = analyze("t.kif", CELL, cfg_env=t.cfg_env())
    files = lower_c.lower(m, t, "t")
    assert "_Thread_local struct kif_tls_t kif_tls_t_data = { ((uint32_t)3) };" in files["t_proc.c"]


def test_cells_custom_provider(tmp_path):
    (tmp_path / "cells.json").write_text('{"format": "kif-target", "version": 1, "name": "mine", "extends": "c", '
                                         '"cells": {"tls": "custom", "per_cpu": "custom", "header": "\\"mycells.h\\""}}')
    t = target.load(str(tmp_path / "cells.json"))
    src = CELL + "#[per_cpu] static P: u64 = 0;\nfn g() -> u64 { P.with(|p| *p) }\n"
    files = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")
    assert '#include "mycells.h"' in files["t_proc.h"]
    assert "return KIF_TLS_BLOCK(t);" in files["t_proc.h"] and "return KIF_PCPU_BLOCK(t);" in files["t_proc.h"]
    assert "const struct kif_pcpu_t kif_pcpu_t_image" in files["t_proc.c"]


def test_cells_refusals():
    t = target.load("c")

    def low(src):
        return lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")

    with pytest.raises(lower_c.LowerError, match="per_cpu"):
        low("#[per_cpu] static P: u64 = 0;\n#[atomic_only] fn g() -> u64 { P.with(|p| *p) }")
    with pytest.raises(lower_c.LowerError, match="closure literal"):
        low("fn h(a: u32) -> u32 { a }\n#[tls] static N: u32 = 3;\nfn f() -> u32 { N.with(h) }")


def test_errno_map_overrides_defaults(tmp_path):
    src = "fn f(x: u32) -> result<u32> { if x == 0 { return err(error::timed_out); } if x == 1 { return err(error::busy); } ok(x) }\n"
    (tmp_path / "t.json").write_text('{"format": "kif-target", "version": 1, "name": "t", "extends": "c", "errno": {"map": {"timed_out": "EAGAIN"}}}')
    t = target.load(str(tmp_path / "t.json"))
    m = analyze("m.kif", src, None, cfg_env=t.cfg_env())
    h = lower_c.lower(m, t, "m")["m_proc.c"]
    assert "(-EAGAIN)" in h and "(-EBUSY)" in h and "ETIMEDOUT" not in h
    (tmp_path / "bad.json").write_text('{"format": "kif-target", "version": 1, "name": "b", "extends": "c", "errno": {"map": {"nope": "EIO"}}}')
    with pytest.raises(target.TargetError):
        target.load(str(tmp_path / "bad.json"))


def test_array_of_owning_elements_is_refused(tmp_path):
    with pytest.raises(lower_c.LowerError, match="array of owning"):
        lower_files(tmp_path, {"m.kif": "fn f() -> result<usize> { let a: [str; 1] = [str::try_create(\"a\")?]; ok(a[0].len()) }\n"}, "m.kif")
    with pytest.raises(lower_c.LowerError, match="array of owning"):
        lower_files(tmp_path, {"m.kif": "struct s { a: [str; 2] }\nfn f() -> u32 { 1 }\n"}, "m.kif")



ELIDE = """
fn guard(a: [u32; 4], i: usize) -> u32 { if i >= 4 { return 0; } a[i] }
fn sum4(a: [u32; 4]) -> u32 { let mut k: usize = 0; let mut t: u32 = 0; while k < 4 { t += a[k]; k += 1; } t }
fn bare(a: [u32; 4], i: usize) -> u32 { a[i] }
fn fixed(a: [u32; 4]) -> u32 { a[3] }
fn opt(o: optional<u32>) -> u32 { if o.has_value() { return *o.value(); } 0 }
fn raw(o: optional<u32>) -> u32 { *o.value() }
"""


def lowered(src, **target_over):
    t = target.load("c")
    t.data.update(target_over)
    m = analyze("t.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1]
    return lower_c.lower(m, t, "t")["t_proc.c"]


def body(c, name):
    start = c.index(f" t_{name}(")
    return c[start:c.index("\n}\n", start)]


def test_elided_checks_are_documented_in_the_output():
    c = lowered(ELIDE)
    assert "index out of bounds" not in body(c, "guard") and "/* kif: bounds check elided: i < 4 <= 4 is known */" in body(c, "guard")
    assert "/* kif: bounds check elided: k < 4 <= 4 is known */" in body(c, "sum4")
    assert "/* kif: bounds check elided: constant index 3 < 4 */" in body(c, "fixed")
    assert "/* kif: has-value check elided: some(o) is known on every path here */" in body(c, "opt")
    assert "optional has no value" not in body(c, "opt")
    # what is not proved keeps its check and says nothing
    assert "index out of bounds" in body(c, "bare") and "elided" not in body(c, "bare")
    assert "optional has no value" in body(c, "raw") and "elided" not in body(c, "raw")


def test_checked_tier_keeps_every_check_and_adds_no_comment():
    c = lowered(ELIDE, access={"index": "checked", "optional": "checked"})
    assert "check elided" not in c and "index out of bounds" in body(c, "guard") and "optional has no value" in body(c, "opt")


def test_elided_code_runs_and_still_traps_where_unproved():
    drv = ('uint32_t a[4] = {1, 2, 3, 4};'
           'printf("%u %u %u %u\\n", t_guard(a, 2), t_guard(a, 9), t_sum4(a), t_fixed(a));')
    m, r = compile_run(ELIDE, drv)
    assert r.returncode == 0 and r.stdout.split() == ["3", "0", "10", "4"], r.stderr
    assert compile_run(ELIDE, "uint32_t a[4] = {1, 2, 3, 4}; t_bare(a, 4);")[1].returncode != 0


LOOPS = """
fn fsum(a: [u32; 4]) -> u32 { let mut t: u32 = 0; for i in 0..4 { t += a[i]; } t }
fn pairs(a: [u32; 4]) -> u32 { let mut t: u32 = 0; for i in 1..4 { let j: usize = i; t += a[j]; } t }
fn slot(a: [u32; 4]) -> u32 { let n: usize = 3; a[n] }
fn guard(a: [u32; 4], i: usize) -> u32 { if i < 4 && a[i] > 1 { a[i] } else { 0 } }
fn skip(a: [u32; 4]) -> u32 { let mut t: u32 = 0; for i in 0..4 { if a[i] == 2 { continue; } t += a[i]; } t }
fn oob(a: [u32; 4], i: usize) -> u32 { if i < 4 { a[i + 1] } else { 0 } }
"""


def test_loop_and_slot_elisions_run_correctly():
    c = lowered(LOOPS)
    for fn in ("fsum", "pairs", "slot", "guard", "skip"):
        assert "index out of bounds" not in body(c, fn), fn
    assert "index out of bounds" in body(c, "oob")  # i + 1 can be 4
    drv = ('uint32_t a[4] = {1, 2, 3, 4};'
           'printf("%u %u %u %u %u\\n", t_fsum(a), t_pairs(a), t_slot(a), t_guard(a, 2), t_skip(a));')
    m, r = compile_run(LOOPS, drv)
    assert r.returncode == 0 and r.stdout.split() == ["10", "9", "4", "3", "8"], r.stderr
    assert compile_run(LOOPS, "uint32_t a[4] = {1, 2, 3, 4}; t_oob(a, 3);")[1].returncode != 0


DIVS = """
fn half(x: u32) -> u32 { x / 2 }
fn guarded(x: u32, d: u32) -> u32 { if d != 0 { x % d } else { 7 } }
fn bare(x: u32, d: u32) -> u32 { x / d }
fn neg(x: i32) -> i32 { x / -1 }
"""


def test_proved_divisions_drop_their_checks_and_the_rest_still_trap():
    c = lowered(DIVS)
    assert "integer division by zero" not in body(c, "half") and "/* kif: division check elided: the divisor is the constant 2 */" in body(c, "half")
    assert "integer division by zero" not in body(c, "guarded") and "division check elided: d != 0 is known" in body(c, "guarded")
    assert "integer division by zero" in body(c, "bare") and "division" in body(c, "neg")
    assert "integer division by zero" in body(lowered(DIVS, access={"div": "checked"}), "half")
    drv = 'printf("%u %u %u\\n", t_half(9), t_guarded(9, 4), t_guarded(9, 0));'
    m, r = compile_run(DIVS, drv)
    assert r.returncode == 0 and r.stdout.split() == ["4", "1", "7"], r.stderr
    assert compile_run(DIVS, "t_bare(1, 0);")[1].returncode != 0


WRAPS = """
fn u(a: u32, b: u64, c: usize) -> u64 { let x = a * a + a - 1; (x as u64) * b + (c as u64) }
fn n(a: u8, b: u16) -> u16 { (a + 1) as u16 * b + b - 1 }
fn s(a: i32) -> i32 { a + 1 }
fn grow(a: u32) -> u32 { let mut b = a; b += 1; b *= 3; b }
"""


def test_kernel_targets_write_signed_arithmetic_plainly():
    for name in ("linux-kernel", "freebsd-kernel"):
        assert target.load(name).data["wrapping"] == "builtin", name  # both kernels build with -fwrapv / -fno-strict-overflow
    assert target.load("c").data.get("wrapping", "helpers") == "helpers"


def test_wrapping_arithmetic_is_plain_where_c_already_wraps():
    c = lowered(WRAPS)
    assert not any(h in body(c, "u") for h in ("KIF_ADD", "KIF_MUL", "KIF_SUB")) and "KIF_ADD_I32" in body(c, "s")
    assert "KIF_MUL_U16" in body(c, "n") and "KIF_ADD_U8" not in body(c, "n")  # u16 * u16 promotes to int and can overflow it
    assert "KIF_" not in body(c, "grow")
    m, r = compile_run(WRAPS, 'printf("%u %u %u\\n", (unsigned)t_n(255, 65535), (unsigned)t_grow(4294967295u), (unsigned)t_u(1, 2, 3));')
    assert r.returncode == 0 and r.stdout.split() == ["65534", "0", "5"], r.stderr


def test_shared_helpers_are_always_inline_and_signed_math_can_be_plain():
    t = target.load("c")
    h = lower_c.common_header(t)
    assert "#define KIF_INLINE static __inline __attribute__((always_inline))" in h
    assert "KIF_INLINE int32_t KIF_ADD_I32(" in h and "KIF_INLINE size_t KIF_IDX(" in h and "static __inline int32_t KIF_ADD" not in h
    assert "KIF_ADD_I32" in body(lowered(WRAPS), "s")
    c = lowered(WRAPS, wrapping="builtin")
    assert "KIF_" not in body(c, "s") and "return a + ((int32_t)1);" in body(c, "s")
    assert "KIF_" not in body(c, "n")  # u16 * u16 may overflow int, which -fwrapv makes defined too
    assert "wrapping" in target.load("linux-kernel").data and target.load("linux-kernel").data["wrapping"] == "builtin"
    # the plain operators are only right with -fwrapv: compile and run the builtin output that way
    m, r = compile_run(WRAPS, 'printf("%u\\n", (unsigned)t_n(255, 65535));', cflags=["-fwrapv"], target_over={"wrapping": "builtin"})
    assert r.returncode == 0 and r.stdout.split() == ["65534"], r.stderr


SHIFTS = """
fn k(x: u32) -> u32 { x << 3 }
fn mask(x: u32, n: u32) -> u32 { x >> (n & 31) }
fn smask(x: i32, n: i32) -> i32 { x << (n & 7) }
fn cmp(x: u8, n: u32) -> u8 { if n < 8 { x << n } else { 0 } }
fn bare(x: u32, n: u32) -> u32 { x << n }
fn table(t: [u32; 8], h: usize) -> u32 { t[h & 7] + t[h % 8] }
"""


def test_proved_shifts_and_masked_indexes_drop_their_checks():
    c = lowered(SHIFTS)
    for fn in ("k", "mask", "smask", "cmp"):
        assert "shift count" not in body(c, fn) and "shift check elided" in body(c, fn), fn
    assert "shift count" in body(c, "bare")
    assert "index out of bounds" not in body(c, "table") and "bounds check elided" in body(c, "table")
    assert "shift count" in body(lowered(SHIFTS, access={"shift": "checked"}), "k")
    drv = ('uint32_t t[8] = {1,2,3,4,5,6,7,8};'
           'printf("%u %u %d %u %u\\n", t_k(5), t_mask(0x80000000u, 35), (int)t_smask(1, -1), (unsigned)t_cmp(255, 3), t_table(t, 11));')
    m, r = compile_run(SHIFTS, drv)
    assert r.returncode == 0 and r.stdout.split() == ["40", "268435456", "128", "248", "8"], r.stderr
    assert compile_run(SHIFTS, "t_bare(1, 32);")[1].returncode != 0


FIELDS = """
struct bag { a: [u32; 4], n: usize, o: optional<u32> }
fn pick(x: &bag, i: usize) -> u32 { if i < 4 { x.a[i] + x.a[i] } else { 0 } }
fn at_n(x: &bag, v: &[u32]) -> u32 { if x.n < v.len() { v[x.n] + v[x.n] } else { 0 } }
fn opt(x: &bag) -> u32 { if x.o.has_value() { *x.o.value() } else { 9 } }
fn moved(x: &mut bag, v: &[u32]) -> u32 { if x.n < v.len() { x.n = 100; v[x.n] } else { 0 } }
"""


def test_field_facts_elide_checks_and_the_rest_still_trap():
    c = lowered(FIELDS)
    assert "index out of bounds" not in body(c, "pick") and "index out of bounds" not in body(c, "at_n")
    assert "bounds check elided: x.n < len(v) is known" in body(c, "at_n")
    assert "has-value check elided: some(x.o) is known" in body(c, "opt")
    assert "index out of bounds" in body(c, "moved")  # the write to x.n forgot the bound
    drv = ('struct bag b = {{1,2,3,4}, 1, {0}}; uint32_t v[3] = {5,6,7}; struct kif_cspan_u32 s = {v, 3};'
           'printf("%u %u %u\\n", t_pick(&b, 3), t_at_n(&b, s), t_opt(&b));')
    m, r = compile_run(FIELDS, drv)
    assert r.returncode == 0 and r.stdout.split() == ["8", "12", "9"], r.stderr
    m, r = compile_run(FIELDS, 'struct bag b = {{1,2,3,4}, 1, {0}}; uint32_t v[3] = {5,6,7}; struct kif_cspan_u32 s = {v, 3}; t_moved(&b, s);')
    assert r.returncode != 0


def test_cast_of_a_declared_enumerator_needs_no_check():
    src = "enum st { a = 1, b = 5 }\nfn f() -> st { 5 as st }\nfn g(x: u32) -> st { x as st }\nfn h() -> st { 7 as st }"
    c = lowered(src)
    assert "st_b /* kif: enum cast check elided" in body(c, "f")
    assert "KIF_TOENUM_st" in body(c, "g") and "KIF_TOENUM_st" in body(c, "h")
    assert "KIF_TOENUM_st" in body(lowered(src, access={"range": "checked"}), "f")


def test_last_element_of_an_empty_slice_still_traps():
    src = "fn last(s: &[u32]) -> u32 { s[s.len() - 1] }\nfn safe(s: &[u32]) -> u32 { if s.len() > 0 { s[s.len() - 1] } else { 7 } }"
    assert "index out of bounds" not in body(lowered(src), "safe") and "index out of bounds" in body(lowered(src), "last")
    drv = "uint32_t a[2] = {4, 5}; struct kif_cspan_u32 s = {a, 2}, e = {a, 0}; printf(\"%u %u\\n\", t_safe(s), t_safe(e)); fflush(stdout); t_last(e);"
    m, r = compile_run(src, drv)
    assert r.stdout.split() == ["5", "7"] and r.returncode != 0


FACTS = ("fn first(#[non_empty] s: &[u32]) -> u32 { s[0] }\n"
         "fn pick(#[lt_len(s)] i: usize, s: &[u32]) -> u32 { s[i] }\n"
         "fn want(#[some] o: optional<u32>) -> u32 { *o.value() }\n")


def test_proved_calls_skip_the_guarded_entry_and_others_keep_it():
    src = FACTS + ("fn a(s: &[u32]) -> u32 { if s.is_empty() { 0 } else { first(s) } }\n"
                   "fn b(s: &[u32]) -> u32 { let mut t: u32 = 0; for i in 0..s.len() { t = t + pick(i, s); } t }\n"
                   "fn c(o: optional<u32>) -> u32 { if o.has_value() { want(o) } else { 0 } }\n"
                   "fn d(s: &[u32]) -> u32 { first(s) }")
    c = lowered(src)
    assert "t_first__nofacts(" in body(c, "a") and "t_pick__nofacts(" in body(c, "b") and "t_want__nofacts(" in body(c, "c")
    assert "t_first(" in body(c, "d") and "__nofacts" not in body(c, "d")
    assert "KIF_IDX" not in body(c, "first__nofacts") and "KIF_IDX" not in body(c, "pick__nofacts")
    assert "violates #[non_empty]" in body(c, "first") and "violates #[lt_len(s)]" in body(c, "pick")
    assert "t_first(" in body(lowered(src, access={"index": "checked"}), "a")  # the target keeps every check


def test_a_fact_that_can_never_hold_is_an_error():
    for call, what in (("pick(5, &a[..])", "5 is not below the length 2"), ("pick(2, a.as_slice())", "2 is not below")):
        with pytest.raises(lower_c.LowerError, match="can never succeed.*" + what):
            lowered(FACTS + f"fn bad() -> u32 {{ let a: [u32; 2] = [1, 2]; {call} }}")
    lowered(FACTS + "fn fine() -> u32 { let a: [u32; 2] = [1, 2]; pick(1, &a[..]) }")


def test_the_guarded_entry_traps_for_a_caller_that_violates_a_fact():
    src = FACTS + "fn d(s: &[u32]) -> u32 { first(s) }"
    drv = ("uint32_t a[2] = {4, 5}; struct kif_cspan_u32 s = {a, 2}, e = {a, 0}; "
           "printf(\"%u %u\\n\", t_first(s), t_d(s)); fflush(stdout); t_d(e);")
    m, r = compile_run(src, drv)
    assert r.stdout.split() == ["4", "4"] and r.returncode != 0


def test_code_generation_runs_the_ir_pass_pipeline_first(monkeypatch):
    from kiflang import diagnostics as dg
    from kiflang import passes

    monkeypatch.setattr(passes, "REGISTRY", dict(passes.REGISTRY))
    ran = []

    @passes.register
    class Strict(passes.Pass):
        name, kind = "strict", "check"

        def run(self, ctx):
            ran.append(ctx.fn.name)
            if ctx.fn.name == "bad":
                ctx.report("KIFI901", "this function is refused", ctx.fn.node)

    src = "fn ok() -> u32 { 1 }\nfn bad() -> u32 { 2 }"
    with pytest.raises(lower_c.LowerError, match="this function is refused"):
        lowered(src)
    assert "bad" in ran
    ran.clear()
    lowered(src, ir={"passes": "default,-strict"})  # the target leaves the pass out
    assert ran == []
    c = lowered("fn f(s: &[u32]) -> u32 { if s.is_empty() { 0 } else { s[0] } }", ir={"passes": "default,-strict,-elide,-refuted,-require_proved,-prove"})
    assert "index out of bounds" in body(c, "f")  # no prover ran, so the check stays


def test_ir_emitter_heap_objects():
    """`ptr<S>` heap objects: `try_allocate` through `S_try_new`, null-checked dereference, an owning field released by `S_drop_fields`,
    and a constructor over a `c_ptr<Self>` that releases what it built when it fails. Same results as the AST generator, under ASan."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "heap.kif"), encoding="utf-8").read()
    drv = 'uint32_t v = 0; int rc = t_chain(&v); printf("%d %u\\n", rc, v);'
    outs = []
    for over in (None, IR_CODEGEN):
        _, r = compile_run(src, drv, target_over=over)
        outs.append(r.stdout)
    assert outs[0] == outs[1] and outs[0].strip(), outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("t_chain", "node_try_construct_at", "node_get", "node_set"):
        assert re.search(rf"\b{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


def test_ir_emitter_try_allocate_default_allocator():
    """`T::try_allocate(default_allocator(), ..)` lowers like `T::try_create(..)`."""
    src = """
fn alloc_len() -> result<usize> {
    let mut v: vector<u8> = vector::try_allocate(default_allocator(), 4)?;
    v.try_push(1)?;
    let s: str = str::try_allocate(default_allocator(), "abc")?;
    ok(v.len() + s.len())
}
"""
    drv = 'size_t n = 0; int rc = t_alloc_len(&n); printf("%d %zu\\n", rc, n);'
    outs = [compile_run(src, drv, target_over=over)[1].stdout for over in (None, IR_CODEGEN)]
    assert outs[0] == outs[1] and outs[0].strip(), outs


def test_ir_emitter_tls_cells():
    """`#[tls]` cells: `CELL.with(|v| ..)` is a `Cell` address plus the closure body inline; same results as the AST generator."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "cells.kif"), encoding="utf-8").read()
    drv = 'printf("%u %u %d %u %d\\n", t_bump(), t_start(), t_flip(), t_hit(), t_flag());'
    outs = [compile_run(src, drv, target_over=over)[1].stdout for over in (None, IR_CODEGEN)]
    assert outs[0] == outs[1] and outs[0].strip(), outs
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    for f in ("bump", "start", "flip", "hit", "flag"):
        assert re.search(rf"\bt_{f}\([^)]*\)\n\{{(?:(?!\n\}}\n)[\s\S])*\t", text), f


def test_ir_emitter_line_directives():
    """The emitter points `#line` at the `.kif` line of each instruction when `features.line_directives` is on."""
    src = open(os.path.join(os.path.dirname(__file__), "c", "cells.kif"), encoding="utf-8").read()
    t = target.load("c")
    t.data.update(IR_CODEGEN)
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    body = text[text.index("t_hit("):]
    assert re.search(r'\n#line \d+ "t.kif"\n\t\w+ =', body)


def test_function_doc_comments_reach_the_c_output():
    src = "// Adds two numbers.\n// Never `*/` breaks out.\n#[export]\nfn add(a: u32, b: u32) -> u32 { a + b }\n\nfn plain(a: u32) -> u32 { a }\n"
    t = target.load("c")
    files = lower_c.lower(analyze("d.kif", src, cfg_env=t.cfg_env()), t, "d")
    assert "/**\n * Adds two numbers.\n * Never `* /` breaks out.\n */\nextern uint32_t add(" in files["d_proc.h"]
    assert "/**\n * Adds two numbers." in files["d_proc.h"] and "/**\n * Adds two numbers." in files["d_proc.c"]
    assert files["d_proc.h"].count("/**") == 1  # `plain` has no comment above it
    off = target.load("c")
    off.data.setdefault("features", {})["doc_comments"] = False
    assert "Adds two numbers" not in "".join(lower_c.lower(analyze("d.kif", src, cfg_env=off.cfg_env()), off, "d").values())


def test_body_comments_follow_the_line_markers():
    src = "// Doc.\nfn f(a: u32) -> u32 { a }\n\nfn g(n: u32) -> u32 {\n    // start at zero\n    let mut t: u32 = 0;\n    // walk\n    // up\n    t += n;\n    t\n}\n"
    t = target.load("c")
    c = lower_c.lower(analyze("d.kif", src, cfg_env=t.cfg_env()), t, "d")["d_proc.c"]
    assert '#line 6 "d.kif"\n\t/* start at zero */\n\tt = ' in c
    assert '#line 9 "d.kif"\n\t/* walk */\n\t/* up */\n' in c
    assert c.count("Doc.") == 1  # a one-line body does not repeat the function's own comment
    off = target.load("c")
    off.data.setdefault("features", {})["body_comments"] = False
    assert "start at zero" not in lower_c.lower(analyze("d.kif", src, cfg_env=off.cfg_env()), off, "d")["d_proc.c"]


def test_comment_corpus_case_lowers_its_comments():
    path = Path(__file__).parent / "c" / "comments.kif"
    t = target.load("c")
    files = lower_c.lower(analyze(str(path), path.read_text(), cfg_env=t.cfg_env()), t, "comments")
    c, h = files["comments_proc.c"], files["comments_proc.h"]
    assert "/**\n * Sums the multiples of `step` below `n`.\n * Wraps on overflow, like every `u32` add.\n */\nextern uint32_t total(" in h
    assert "\t/* a run of comment lines */\n\t/* stays together, in order */\n" in c
    assert c.count("comments inside loops are written once") == 1
    assert "\t\t/* the larger one */\n\t\treturn a;" in c and "detached" not in c.lower()


# -- taint stage 5: `ct::` intrinsics are branch-free C wrapped in the target's value barrier

CT_SRC = """
fn sel(c: bool, a: u32, b: u32) -> u32 { ct::select(c, a, b) }
fn seli(c: bool, a: i16, b: i16) -> i16 { ct::select(c, a, b) }
fn eq8(a: u8, b: u8) -> bool { ct::eq(a, b) }
fn lti(a: i32, b: i32) -> bool { ct::lt(a, b) }
fn ltu(a: u64, b: u64) -> bool { ct::lt(a, b) }
fn msk(c: bool) -> usize { ct::mask(c) }
fn clampi(x: i32, n: i32) -> i32 { untaint!("bounded by the caller's n", ct::clamp(x, n)) }
"""


def test_ct_intrinsics_match_the_interpreter_in_c():
    m, r = compile_run(CT_SRC, 'printf("%u %u %d %d %d %d %d %d %d %d %d %llu %llu %d %d %d\\n",'
                       't_sel(1,7,9),t_sel(0,7,9),t_seli(1,-3,4),t_seli(0,-3,4),t_eq8(5,5),t_eq8(5,6),t_lti(-1,0),t_lti(0,-1),t_lti(-2147483647-1,2147483647),'
                       't_ltu(1,2),t_ltu(2,1),(unsigned long long)t_msk(1),(unsigned long long)t_msk(0),t_clampi(3,5),t_clampi(5,5),t_clampi(-3,5));')
    assert r.stdout.split() == ["7", "9", "-3", "4", "1", "0", "1", "0", "1", "1", "0", "18446744073709551615", "0", "3", "0", "-3"]


def test_ct_audit_comment_and_barrier_are_in_the_output():
    t = target.load("c")
    files = lower_c.lower(analyze("t.kif", CT_SRC, cfg_env=t.cfg_env()), t, "t")
    text = files["t_proc.c"]
    assert "kif: ct intrinsic ct::select()" in text and '__asm__ volatile ("" : "+r"(kif_t' in text


def test_ct_without_a_target_barrier_is_an_error():
    import pytest

    t = target.load("c")
    t.data["taint"] = {}
    with pytest.raises(lower_c.LowerError, match="taint.barrier"):
        lower_c.lower(analyze("t.kif", CT_SRC, cfg_env=t.cfg_env()), t, "t")


def test_ct_eq_lt_clamp_are_exhaustively_right_on_8_bits():
    src = ("fn e(a: u8, b: u8) -> bool { ct::eq(a, b) }\nfn l(a: u8, b: u8) -> bool { ct::lt(a, b) }\nfn li(a: i8, b: i8) -> bool { ct::lt(a, b) }\n"
           "fn c(a: u8, b: u8) -> u8 { untaint!(\"test\", ct::clamp(a, b)) }\n")
    drv = ("int bad=0; for(int a=0;a<256;a++)for(int b=0;b<256;b++){"
           "bad+=t_e(a,b)!=(a==b); bad+=t_l(a,b)!=(a<b); bad+=t_li((int8_t)a,(int8_t)b)!=((int8_t)a<(int8_t)b); bad+=t_c(a,b)!=(a<b?a:0);}"
           'printf("%d\\n",bad);')
    m, r = compile_run(src, drv)
    assert r.stdout.split() == ["0"]


def test_value_barrier_on_aggregates_is_one_barrier_per_scalar_field():
    src = ("struct inner { a: u8, b: u32 }\nstruct s { x: u16, nest: inner, t: [u32; 3] }\n"
           "fn g(v: s) -> s { spec::value(v) }\nfn h(v: inner) -> u32 { let w = spec::value(v); w.b + (w.a as u32) }\n")
    m, r = compile_run(src, "struct s v = {1, {2, 3}, {4, 5, 6}}; struct s w = t_g(v); struct inner i = {7, 8};"
                       'printf("%u %u %u %u %u %u %u %u\\n", w.x, w.nest.a, w.nest.b, w.t[0], w.t[1], w.t[2], (unsigned)t_h(i), 0u);')
    assert r.stdout.split()[:7] == ["1", "2", "3", "4", "5", "6", "15"]
    t = target.load("c")
    text = lower_c.lower(analyze("t.kif", src, cfg_env=t.cfg_env()), t, "t")["t_proc.c"]
    leaves = [x for x in text.splitlines() if "asm" in x]
    assert any(".x)" in x for x in leaves) and any(".nest.a)" in x for x in leaves) and any(".nest.b)" in x for x in leaves) and any(".t[kif_i0])" in x for x in leaves)
