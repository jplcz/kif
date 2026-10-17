# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Structured control flow and expression folding in the emitted C (docs/kif-ir-codegen.md)."""

import re

from kiflang import lower_c, target, textfold
from kiflang.symbols import analyze



def emit(src):
    t = target.load("c")
    m = analyze("t.kif", src, cfg_env=t.cfg_env())
    return lower_c.lower(m, t, "t")["t_proc.c"]


def test_loops_and_branches_have_no_gotos():
    c = emit("""
fn f(n: usize) -> u32 {
    let mut k = 0usize;
    let mut t = 0u32;
    while k < n {
        k = k + 1;
        if k > 7 { break; }
        if k == 3 { continue; }
        t = t + 1;
    }
    if t > 3 { t } else { 0 }
}
""")
    assert "while (k < n)" in c and "break;" in c and "else" in c
    assert "goto" not in c


def test_single_use_temps_are_folded():
    c = emit("fn f(a: u32, b: u32) -> u32 { (a + b) * 2 }")
    assert "kif_t" not in c
    assert re.search(r"return \(?a \+ b\)? \* ", c)


def test_text_fold_single_use_and_call_limit():
    chunks = {0: ["\tkif_t1 = a + b;", "\treturn kif_t1;"]}
    textfold.fold(chunks, {})
    assert chunks[0] == ["\treturn a + b;"]
    chunks = {0: ["\tkif_t1 = f(a);", "\tkif_t2 = g(kif_t1);"]}
    textfold.fold(chunks, {})
    assert chunks[0][0].startswith("\tkif_t1 = f(") or "f(a)" in chunks[0][0]  # two impure calls are never merged
    assert not (len(chunks[0]) == 1 and "g(f(a))" in chunks[0][0])


def test_text_fold_keeps_result_pairs_and_other_blocks():
    chunks = {0: ["\tkif_t1 = a;", "\tkif_t1_v = b;"], 1: ["\treturn kif_t1;"]}
    textfold.fold(chunks, {})
    assert any("kif_t1 =" in x for x in chunks[0])


def test_dead_temp_stores_are_dropped():
    from kiflang import textfold

    chunks = {"b": ["\tkif_t1 = line.len;", "\tkif_t2 = f(x);", "\tkif_t3 = 1;", "\treturn kif_t3;"]}
    textfold.fold(chunks, {})
    assert chunks["b"] == ["\tf(x);", "\treturn 1;"]


def test_a_call_folds_into_the_branch_condition():
    from kiflang import textfold

    chunks, conds = {"b": ["\tkif_t1 = f(x);"]}, {"b": "kif_t1"}
    textfold.fold(chunks, conds)
    assert chunks["b"] == [] and conds["b"] == "f(x)"


def test_a_kept_definition_says_why():
    from kiflang import textfold

    chunks = {"b": ["\tkif_t1 = f(x);", "\tkif_t2 = g(y);", "\treturn kif_t1 + kif_t2;"]}
    textfold.fold(chunks, {}, notes=True)
    assert chunks["b"][0].endswith("/* kif: kept, two calls would be reordered */")


def test_notes_can_be_off_and_the_pure_list_is_a_parameter():
    from kiflang import textfold

    chunks = {"b": ["\tkif_t1 = f(x);", "\tkif_t2 = g(y);", "\treturn kif_t1 + kif_t2;"]}
    textfold.fold(chunks, {})
    assert chunks["b"][0] == "\tkif_t1 = f(x);"
    chunks = {"b": ["\tkif_t1 = f(x);", "\tkif_t2 = g(y);", "\treturn kif_t1 + kif_t2;"]}
    textfold.fold(chunks, {}, pure=("g",))  # g has no side effects here, so both fold
    assert chunks["b"] == ["\treturn f(x) + g(y);"]
