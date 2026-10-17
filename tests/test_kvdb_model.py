# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""tests/c/kvdb.out is what an independent Python model of the kvdb workload prints (the C run is compared with it by test_c_files)."""
import subprocess
import sys
from pathlib import Path

C = Path(__file__).parent / "c"


def test_model_matches_expected_output():
    r = subprocess.run([sys.executable, str(C / "kvdb_model.py")], capture_output=True, text=True, check=True)
    assert r.stdout == (C / "kvdb.out").read_text()
