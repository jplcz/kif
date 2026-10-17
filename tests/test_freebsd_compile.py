# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Compiles the generated FreeBSD kobj output (`_if`, `_proc`, `_kobj`) against real kernel headers.

Needs a FreeBSD source tree: set KIF_FREEBSD_SRC to its root (default ../freebsd-src) and have gcc and awk; skipped otherwise.
Syntax-only: the kernel headers are checked for declarations, not linked.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(os.environ.get("KIF_FREEBSD_SRC", Path(__file__).resolve().parents[2] / "freebsd-src"))
SYS = SRC / "sys"
pytestmark = pytest.mark.skipif(shutil.which("gcc") is None or shutil.which("awk") is None or not (SYS / "sys" / "kobj.h").exists(),
                                reason="needs gcc, awk and a FreeBSD source tree (KIF_FREEBSD_SRC)")

KIF = """
extern "C" type device_t;
extern "C" type driver_t;

#[kobj, self(device_t)]
interface uart {
  method attach() -> i32;
  method detach() -> i32;
  static method identify(driver: c_ptr<driver_t>, parent: device_t);
}

struct sc { n: u32, name: str, }

impl uart for sc {
  fn attach(&mut self) -> i32 { self.n = 1; 0 }
  fn detach(&mut self) -> i32 { 0 }
  fn identify(driver: c_ptr<driver_t>, parent: device_t) { }
}
"""


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def test_generated_output_compiles_against_kernel_headers(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "machine").symlink_to(SYS / "amd64" / "include")
    (inc / "x86").symlink_to(SYS / "x86" / "include")
    for m in ("device", "bus"):  # headers the kernel generates from .m files
        r = run(["awk", "-f", str(SYS / "tools" / "makeobjops.awk"), str(SYS / "kern" / f"{m}_if.m"), "-h"], cwd=inc)
        assert r.returncode == 0, r.stderr
    out = tmp_path / "out"
    out.mkdir()
    (out / "uart.kif").write_text(KIF)
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    for argv in (["--c-common", str(out)], ["--c", str(out), "--freebsd", str(out), str(out / "uart.kif")]):
        r = run([sys.executable, "-m", "kiflang.kifc", "--target", "freebsd-kernel"] + argv, env=env, cwd=root)
        assert r.returncode == 0, r.stderr
    gi = run(["gcc", "-print-file-name=include"]).stdout.strip()
    for f in ("uart_if.c", "uart_proc.c", "uart_kobj.c"):
        r = run(["gcc", "-fsyntax-only", "-Wall", "-Werror", "-std=gnu99", "-ffreestanding", "-D_KERNEL", "-DKLD_MODULE", "-nostdinc",
                 "-isystem", gi, f"-I{out}", f"-I{inc}", f"-I{SYS}", f], cwd=out)
        assert r.returncode == 0, f"{f}: {r.stderr}"
