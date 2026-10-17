# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Step 1 of the IR based C generator (kiflang/ir_c.py): what the IR cannot yet describe."""

from kiflang import ir, ir_c, symbols

SRC = """fn sum(a: u32, b: u32) -> u32 { a + b }
fn count(n: u32) -> u32 { let mut k: u32 = 0; while k < n { k += 1; } k }
fn name() -> str { "x" }
fn pair(a: u32) -> u32 { let t = (a, a); a }
"""


def module():
    m = symbols.analyze("/t/main.kif", SRC, None)
    return ir.build_module(m.tree)


def test_scalar_functions_are_ready_and_others_say_why():
    ready, blocked, hist = ir_c.coverage(module())
    assert ready == ["sum", "count"]
    why = dict(blocked)
    assert why["name"] == ["type:str"]  # the literal itself is a real node now; only the type is missing
    assert why["pair"] == ["type:(u32, u32)"]
    assert hist["type:str"] == 1


def test_report_lists_ready_blocked_and_the_histogram():
    text = ir_c.report(module())
    assert text.startswith("2 of 4 functions can be emitted from the IR alone")
    assert "ready: sum, count" in text and "blocked name:" in text and "what holds the most back:" in text


def test_emitter_report_counts_what_the_emitter_writes():
    m = symbols.analyze("/t/main.kif", "\n".join(SRC.splitlines()[:2]), None)  # the tuple function is not lowerable to C at all
    text = ir_c.emitter_report(m)
    assert text.startswith("2 of 2 functions are emitted from the IR")
