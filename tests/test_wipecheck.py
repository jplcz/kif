# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""`kif wipe-check` (docs/kif-secure-wipe.md, stage 8): a wipe that survives passes, a dead-store-eliminated one fails."""
import json
import os
import shutil

import pytest

from kiflang import wipecheck

SRC = """\
#[export]
fn mix(#[secure_wipe] secret: u64, salt: u64) -> u64 { secret ^ salt }
"""

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")


def run(tmp_path, target="c", flags=("-O2",)):
    p = tmp_path / "m.kif"
    p.write_text(SRC)
    return wipecheck.check(str(p), target, "gcc", list(flags))


def test_default_target_wipe_survives(tmp_path):
    rows, problems = run(tmp_path)
    assert not problems and [(r.name, r.ok) for r in rows] == [("mix", True)]
    assert rows[0].sites == 1 and rows[0].evidence == 1


def test_bare_memset_is_removed_by_the_optimizer(tmp_path):
    t = tmp_path / "bare.json"
    t.write_text(json.dumps({"name": "bare", "extends": "c", "wipe": {"call": "memset({p}, 0, {n})", "header": "<string.h>", "barrier_after_call": False}}))
    rows, problems = run(tmp_path, str(t))
    assert not problems and not rows[0].ok and rows[0].evidence == 0


def test_compile_error_is_a_problem_not_a_crash(tmp_path):
    p = tmp_path / "m.kif"
    p.write_text(SRC)
    rows, problems = wipecheck.check(str(p), "c", "gcc", ["-DKIF_BREAK", "-Wp,-E,-nonsense"])
    assert problems


def test_sites_are_counted_per_function():
    text = "int a(void)\n{\n\tKIF_SECURE_WIPE(&x, 1);\n\tKIF_SECURE_WIPE(&y, 1);\n}\nint b(void)\n{\n\tKIF_SECURE_WIPE_VOLATILE(&x, 1);\n}\n"
    assert wipecheck.sites_per_function(text, "KIF_SECURE_WIPE") == {"a": 2}
