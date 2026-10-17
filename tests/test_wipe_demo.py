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
WIPES_IN_IR = 5  # step: the `wipes` pass inserts them
WIPES_IN_C = 5  # step: C emission


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
