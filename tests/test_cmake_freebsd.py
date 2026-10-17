# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""cmake/Kif.cmake with BACKENDS c freebsd: both backends get the target, and the kobj class file is a declared output."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("cmake") is None or shutil.which("ninja") is None or shutil.which("c++") is None,
                                reason="needs cmake, ninja and a C++ compiler")

IFACE = """
extern "C" type device_t;
#[kobj, self(device_t)]
interface uart { method attach() -> i32; }
struct sc { n: u32, }
impl uart for sc { fn attach(&mut self) -> i32 { self.n = 1; 0 } }
"""
PLAIN = "fn twice(a: i32) -> i32 { a * 2 }\n"


def test_c_and_freebsd_backends(tmp_path):
    (tmp_path / "uart.kif").write_text(IFACE)
    (tmp_path / "plain.kif").write_text(PLAIN)
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C CXX)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "add_kif_library(drv SOURCES uart.kif BACKENDS c freebsd KIFC_ARGS --target freebsd-kernel)\n"
        "add_kif_library(plain SOURCES plain.kif BACKENDS c freebsd KIFC_ARGS --target freebsd-kernel)\n")
    b = tmp_path / "b"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    want = ["kif/drv/freebsd/uart_kobj.c", "kif/drv/freebsd/uart_if.h", "kif/drv/c/uart_proc.h", "kif/plain/freebsd/plain_kobj.c"]
    r = subprocess.run(["ninja", "-C", str(b)] + want, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sc_class" in (b / want[0]).read_text()
    assert "no kobj class" in (b / want[3]).read_text()


def test_project_file_and_build_type_drive_kifc(tmp_path):
    """kif_use_project() feeds kif.toml to every kifc run, -D beats it, CMAKE_BUILD_TYPE picks the profile, and kifc runs with --no-project."""
    (tmp_path / "uart.kif").write_text(IFACE)
    (tmp_path / "kif.toml").write_text('target = "freebsd-kernel"\n[build]\nsources = ["*.kif"]\n')
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C CXX)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "kif_use_project()\nadd_kif_library(drv BACKENDS c freebsd)\n")

    def configure(*extra):
        b = tmp_path / ("b" + str(len(list(tmp_path.glob("b*")))))
        r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b), *extra], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        build = (b / "build.ninja").read_text()
        r = subprocess.run(["ninja", "-C", str(b), "kif/drv/freebsd/uart_kobj.c"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return build, b

    build, b = configure("-DCMAKE_BUILD_TYPE=Release")
    assert "--no-project" in build and "--target freebsd-kernel" in build and "--profile release" in build
    assert "sc_class" in (b / "kif/drv/freebsd/uart_kobj.c").read_text()
    build, _ = configure("-DKIF_PROFILE=perf", "-DKIF_TARGET=freebsd-kernel", "-DCMAKE_BUILD_TYPE=Debug")
    assert "--profile perf" in build and "--profile debug" not in build


def test_target_properties_and_keywords_feed_kifc(tmp_path):
    """KIF_* keywords of add_kif_library and KIF_* properties of an existing target reach kifc, after the build-wide settings."""
    (tmp_path / "uart.kif").write_text(IFACE)
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C CXX)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "add_kif_library(a SOURCES uart.kif BACKENDS c KIF_TARGET freebsd-kernel KIF_CFG smp KIF_PROFILE perf)\n"
        "add_library(b STATIC dummy.c)\n"
        "set_target_properties(b PROPERTIES KIF_TARGET freebsd-kernel KIF_SANITIZE undefined)\n"
        "kif_target_sources(b SOURCES uart.kif BACKEND c)\n")
    (tmp_path / "dummy.c").write_text("int dummy;\n")
    b = tmp_path / "b"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ninja = (b / "build.ninja").read_text()
    assert "--cfg smp" in ninja and "--profile perf" in ninja and "--sanitize undefined" in ninja
    assert ninja.count("--target freebsd-kernel") >= 2


def test_signing_policy_reaches_kifc(tmp_path):
    """TRUST, REVOKED and SIGNING (keyword, -D and kif.toml [signing]) become kifc options, and the key files are build dependencies."""
    (tmp_path / "uart.kif").write_text(IFACE)
    (tmp_path / "ca.pem").write_text("x\n")
    (tmp_path / "crl.pem").write_text("x\n")
    (tmp_path / "kif.toml").write_text('target = "freebsd-kernel"\n[build]\nsources = ["*.kif"]\n[signing]\ntrust = ["ca.pem"]\nmode = "require"\n')
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C CXX)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "kif_use_project()\nadd_kif_library(a BACKENDS c)\n"
        "add_kif_library(b SOURCES uart.kif BACKENDS c KIF_REVOKED crl.pem KIF_SIGNING warn)\n")
    b = tmp_path / "b"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ninja = (b / "build.ninja").read_text()
    assert f"--trust {tmp_path / 'ca.pem'}" in ninja
    assert "--signing require" in ninja and "--signing warn" in ninja
    assert f"--revoked {tmp_path / 'crl.pem'}" in ninja
    assert str(tmp_path / "ca.pem") in ninja.split("build ", 1)[1]


def test_small_stack_reaches_kifc(tmp_path):
    """The stack budget (docs/kif-stack.md): kif.toml `small-stack`, the KIF_SMALL_STACK keyword and -DKIF_SMALL_STACK become `--small-stack`."""
    (tmp_path / "a.kif").write_text(PLAIN)
    (tmp_path / "b.kif").write_text(PLAIN.replace("twice", "thrice"))
    (tmp_path / "kif.toml").write_text('small-stack = 4096\n[build]\nsources = ["a.kif"]\n')
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C CXX)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "kif_use_project()\nadd_kif_library(a BACKENDS c)\nadd_kif_library(b SOURCES b.kif BACKENDS c KIF_SMALL_STACK off)\n")
    b = tmp_path / "b"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ninja = (b / "build.ninja").read_text()
    assert "--small-stack 4096" in ninja and "--small-stack off" in ninja
    r = subprocess.run(["ninja", "-C", str(b)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("cc", ["gcc", "clang-24"])
def test_stack_check_target_reads_su(tmp_path, cc):
    """kif_stack_check(TARGET) adds -fstack-usage and reads the .su files of the build, for GCC and clang alike."""
    if not shutil.which(cc):
        pytest.skip(cc)
    (tmp_path / "a.kif").write_text(PLAIN)
    (tmp_path / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.20)\nproject(t C)\nlist(APPEND CMAKE_MODULE_PATH {ROOT / 'cmake'})\ninclude(Kif)\n"
        "add_kif_library(a SOURCES a.kif BACKENDS c)\nkif_stack_check(a_stack SOURCES a.kif TARGET a)\n")
    b = tmp_path / "b"
    r = subprocess.run(["cmake", "-G", "Ninja", "-S", str(tmp_path), "-B", str(b), f"-DCMAKE_C_COMPILER={cc}", "-DCMAKE_C_FLAGS=-fno-stack-protector"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["ninja", "-C", str(b), "a_stack"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "-fstack-usage" in (b / "build.ninja").read_text()
    assert "from .su files" in out and "PROBLEM" not in out, out
