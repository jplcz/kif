# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Precompiled modules (.kifm) and --sysroot (docs/kif-proc-language.md section 12)."""

import os
import shutil

import pytest

from kiflang import cbor, modfile
from kiflang.interp import run
from kiflang.symbols import analyze
from kiflang.sysroot import Sysroot, SysrootError

VENDOR = """
namespace ddk;
/// A device.
struct dev { id: u32 = 0 }
fn secret(a: u32) -> u32 { a * 31337 }
fn twice<T: copy>(x: T) -> T { x }
impl dev { fn id_plus(&self, n: u32) -> u32 { self.id + n } }
const LIMIT: u32 = 7;
interface probe { method go() -> u32; #[atomic_entry] method tick() -> () { } }
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w" if isinstance(text, str) else "wb") as f:
        f.write(text)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "sysroot"
    r.mkdir()
    for name in ("reloco.kif", "microfmt.kif"):
        shutil.copy(os.path.join(os.path.dirname(__import__("kiflang").__file__), "prelude", name), r / name)
    return r


def codes(m):
    return [d.code for d in m.diagnostics]


def test_cbor_roundtrip_and_strictness():
    v = {"a": [1, -5, 2**40, "é", b"x", None, True], "bb": {}}
    assert cbor.loads(cbor.dumps(v)) == v
    assert cbor.dumps({"b": 1, "a": 2}) == cbor.dumps({"a": 2, "b": 1})
    for bad in (b"\x9f\xff", b"\xfb" + bytes(8), b"\x82\x01", cbor.dumps(1) + b"\x00", b"\xa2\x61a\x01\x61a\x02"):
        with pytest.raises(cbor.CborError):
            cbor.loads(bad)


def test_export_strips_bodies_keeps_generics(root):
    m = analyze("/v/ddk.kif", VENDOR, sysroot=Sysroot(str(root)))
    assert codes(m) == []
    text, stripped, embedded = modfile.strip(m)
    assert "31337" not in text and stripped == 2          # secret, id_plus
    assert "fn twice<T: copy>(x: T) -> T { x }" in text    # generic: source stays
    assert sorted(embedded) == ["probe::tick", "twice"] and "const LIMIT: u32 = 7;" in text
    assert "/// A device." in text


def test_import_kifm_from_sysroot(root):
    vend = analyze("/v/ddk.kif", VENDOR, sysroot=Sysroot(str(root)))
    write(str(root / "ddk.kifm"), modfile.export(vend))
    src = "import \"ddk.kif\";\nfn f(d: &ddk::dev) -> u32 { ddk::secret(d.id_plus(ddk::LIMIT)) + ddk::twice(1) }"
    m = analyze("/p/main.kif", src, sysroot=Sysroot(str(root)))
    assert codes(m) == []
    assert m.modules["ddk"].header is not None
    # the interpreter cannot run a function that only exists as object code
    from kiflang.interp import Trap
    with pytest.raises(Trap):
        run(m, "f", m.modules["ddk"].types["dev"] and None)


def test_bodyless_fn_rejected_in_source(root):
    assert "KIFP017" in codes(analyze("t.kif", "fn f() -> u8;"))


def test_sysroot_is_exclusive(root, tmp_path):
    sr = Sysroot(str(root))
    write(str(root / "a" / "b.kif"), "namespace b;\nfn g() {}")
    ok = analyze("/p/main.kif", "import \"a/b.kif\";", sysroot=sr)
    assert codes(ok) == []
    # not found: no fallback to anything else, even the shipped microfmt once it is gone from the sysroot
    os.remove(root / "microfmt.kif")
    assert "KIF012" in codes(analyze("/p/main.kif", "import \"microfmt.kif\";", sysroot=sr))
    for bad in ("/etc/passwd", "../x.kif" if False else "a/../b.kif", "a\\b.kif"):
        assert "KIF013" in codes(analyze("/p/main.kif", f"import \"{bad}\";", sysroot=sr)), bad


def test_sysroot_symlink_escape_and_ambiguity(root, tmp_path):
    write(str(tmp_path / "outside.kif"), "fn o() {}")
    os.symlink(str(tmp_path / "outside.kif"), str(root / "evil.kif"))
    sr = Sysroot(str(root))
    assert "KIF013" in codes(analyze("/p/m.kif", "import \"evil.kif\";", sysroot=sr))
    write(str(root / "dup.kif"), "fn d() {}")
    write(str(root / "dup.kifm"), modfile.export(analyze("/v/dup.kif", "fn d() {}", sysroot=sr)))
    assert "KIF013" in codes(analyze("/p/m.kif", "import \"dup.kif\";", sysroot=sr))


def test_missing_prelude(tmp_path):
    (tmp_path / "empty").mkdir()
    m = analyze("/p/m.kif", "fn f() {}", sysroot=Sysroot(str(tmp_path / "empty")))
    assert "KIF013" in codes(m)


def test_project_relative_imports(root, tmp_path):
    write(str(tmp_path / "proj" / "util.kif"), "namespace util;\nfn u() -> u8 { 1 }")
    main = str(tmp_path / "proj" / "main.kif")
    m = analyze(main, "import \"./util.kif\";\nfn f() -> u8 { util::u() }", sysroot=Sysroot(str(root)))
    assert codes(m) == []
    assert run(m, "f") == 1


def test_tampered_and_versioned_kifm(root):
    sr = Sysroot(str(root))
    data = bytearray(modfile.export(analyze("/v/ddk.kif", VENDOR, sysroot=sr)))
    i = data.index(b"31337") if b"31337" in data else data.index(b"LIMIT")
    data[i] ^= 1
    write(str(root / "ddk.kifm"), bytes(data))
    assert "KIF014" in codes(analyze("/p/m.kif", "import \"ddk.kif\";", sysroot=sr))
    with pytest.raises(modfile.ModuleFileError):
        modfile.decode(cbor.dumps({"magic": "KIFM", "version": 99, "name": "x", "text": "", "digest": modfile.sha256(""), "deps": {}}))
    with pytest.raises(modfile.ModuleFileError):
        modfile.decode(b"hello")


def test_dependency_digest_mismatch(root):
    sr = Sysroot(str(root))
    write(str(root / "base.kif"), "namespace base;\nstruct s { a: u8 = 0 }")
    user = analyze("/v/user.kif", "namespace user;\nimport \"base.kif\";\nfn f(x: base::s) -> u8 { x.a }", sysroot=sr)
    write(str(root / "user.kifm"), modfile.export(user))
    assert codes(analyze("/p/m.kif", "import \"user.kif\";", sysroot=sr)) == []
    write(str(root / "base.kif"), "namespace base;\nstruct s { a: u16 = 0 }")
    assert "KIF015" in codes(analyze("/p/m.kif", "import \"user.kif\";", sysroot=sr))


def test_tooling_lists_sysroot(root):
    write(str(root / "ddk.kifm"), modfile.export(analyze("/v/ddk.kif", VENDOR, sysroot=Sysroot(str(root)))))
    sr = Sysroot(str(root))
    assert sr.modules() == ["ddk.kif", "microfmt.kif", "reloco.kif"]
    key, text, header = sr.read("ddk.kif")
    assert key.endswith("ddk.kifm") and "31337" not in text and header["name"] == "ddk"
