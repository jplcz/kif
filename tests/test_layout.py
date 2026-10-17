# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Stack estimate, stage 1 (docs/kif-stack.md): type layout and the `stack` target section."""
import importlib.util
import json
from pathlib import Path

import pytest

from kiflang import layout, symbols, target
from kiflang.irtypes import parse_type

SRC = ("struct h { a: u8, b: u32 }\nstruct s { a: u8, b: u64, c: [u16; 3] }\nenum e { x, y }\n#[repr(packed)] struct p { a: u8, b: u32 }\n"
       "#[align(32)] struct al { a: u8 }\nstruct g<T> { a: T }\nstruct nest { a: h, b: [h; 2] }\n")
PLAN = target.load("c").stack()
TABLE = layout.struct_table(symbols.analyze("/t/m.kif", SRC, None).tree.items)


@pytest.mark.parametrize("ty,want", [
    ("u8", (1, 1)), ("i32", (4, 4)), ("u64", (8, 8)), ("usize", (8, 8)), ("bool", (1, 1)), ("char", (4, 4)), ("u128", (16, 16)),
    ("()", (0, 1)), ("&u8", (8, 8)), ("&mut [u8]", (8, 8)), ("[u8]", (16, 8)), ("[u32; 4]", (16, 4)), ("[[u8; 3]; 2]", (6, 1)),
    ("ptr<u8>", (8, 8)), ("vector<u8>", (24, 8)), ("secure_vector<u8>", (24, 8)), ("str", (24, 8)), ("strview", (16, 8)),
    ("optional<u32>", (8, 4)), ("(u8, u32)", (8, 4)), ("(u8, u8)", (2, 1)),
    ("h", (8, 4)), ("s", (24, 8)), ("e", (4, 4)), ("p", (5, 1)), ("al", (32, 32)), ("nest", (24, 4)),
])
def test_known_sizes(ty, want):
    assert layout.size_align(parse_type(ty), PLAN, TABLE) == want


@pytest.mark.parametrize("ty", ["g<u8>", "other", "?", "[u8; _]", "(u8, other)", "optional<other>"])
def test_unknown_is_none_never_zero(ty):
    assert layout.size_align(parse_type(ty), PLAN, TABLE) is None


def test_pointer_width_comes_from_the_target(tmp_path):
    t = tmp_path / "t32.json"
    t.write_text(json.dumps({"name": "t32", "extends": "c", "stack": {"pointer_bits": 32}}))
    plan = target.load(str(t)).stack()
    assert layout.size_align(parse_type("usize"), plan, TABLE) == (4, 4)
    assert layout.size_align(parse_type("vector<u8>"), plan, TABLE) == (12, 4)
    assert layout.size_align(parse_type("s"), plan, TABLE) == (24, 8)  # u64 still aligns to 8


def test_stack_section_defaults_and_validation(tmp_path):
    assert tuple(PLAN) == (64, 16, 16, "sum", None)
    for bad in ({"align": 3}, {"share": "liveness"}, {"pointer_bits": 24}, {"default_budget": 0}, {"frame_overhead": -1}):
        t = tmp_path / "bad.json"
        t.write_text(json.dumps({"name": "bad", "extends": "c", "stack": bad}))
        with pytest.raises(target.TargetError):
            target.load(str(t))


def test_demo_layout():
    spec = importlib.util.spec_from_file_location("stack_run", Path(__file__).parents[1] / "examples" / "stack" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.run()
    assert r["diagnostics"] == ["KIFP053"]  # the generic fn of stage 2; C cannot lower it, the estimate still reports it
    assert [l.strip() for l in r["layout"].splitlines()[1:]] == [
        "struct header: size 8, align 4", "struct packet: size 80, align 8", "struct wire: size 5, align 1",
        "struct line: size 64, align 64", "enum mode: size 4, align 4", "struct holder: unknown", "struct counter: size 4, align 4"]
