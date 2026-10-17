# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Runs examples/secure-wipe like tests/test_c_files.py: lower, compile with gcc under sanitizers, run, compare. The output stays in
examples/secure-wipe/out/<target>/ (git-ignored). The expected wipe counts below are raised as each implementation step lands."""
import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not available")
DEMO = Path(__file__).resolve().parent.parent / "examples" / "secure-wipe"
EXPECTED_ERRORS = ["KIFW001", "KIFW001", "KIFW002", "KIFW003", "KIFW004", "KIFW004", "KIFW004", "KIFW005"]
WIPES_IN_IR = 6  # step: the `wipes` pass inserts them
WIPES_IN_C = 13  # step: C emission


def load():
    spec = importlib.util.spec_from_file_location("secure_wipe_demo", DEMO / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("target", ["c", "freebsd-kernel", "linux-kernel"])
def test_demo(target):
    res = load().run(target)
    assert res["error_codes"] == EXPECTED_ERRORS
    assert res["diagnostics"] == []
    assert res["wipes_in_ir"] == WIPES_IN_IR
    assert res["wipes_in_c"] == WIPES_IN_C
    if target == "c":
        assert res["gcc"] == 0, (res["out"] / "gcc.log").read_text()
        assert res["run"] == 0 and res["output"] == res["expected"]


def test_secure_struct_drop_wipes_each_field_but_not_exempt_ones():
    res = load().run("c")
    h = (res["out"] / "demo_proc.h").read_text()
    body = h[h.index("session_key_drop_fields(struct session_key *self)\n{"):]
    body = body[:body.index("\n}")]
    assert "sizeof(self->bytes)" in body and "sizeof(self->counter)" in body
    assert "sizeof(self->owner)" not in body and "node_delete(self->owner)" in body
    assert body.index("sizeof(self->counter)") < body.index("node_delete")  # wipe first, then the exempt field is released
    assert "nonce_drop(&n);" in (res["out"] / "demo_proc.c").read_text()
