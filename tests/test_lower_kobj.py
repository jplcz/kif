# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The FreeBSD kobj emitter (`kifc --freebsd`): `<iface>_if.h` / `<iface>_if.c` in the makeobjops format."""

import pytest

from kiflang import kifc, lower_kobj, lower_c, target
from kiflang.symbols import analyze

SRC = """
extern "C" type device_t;
extern "C" type driver_t;
header(c) {
#include <sys/tslog.h>
}
code(c) {
static int null_shutdown(device_t dev) { return (0); }
}

#[kobj, self(device_t), version(2)]
interface dev {
  m_prolog {
TSENTER2(device_get_name(dev));
}
  m_epilog {
TSEXIT2(device_get_name(dev));
}
  method read(off: u32, buf: c_ptr<u8>) -> i32;
  static method identify(driver: c_ptr<driver_t>, parent: device_t) -> ();
  method shutdown() -> i32 default null_shutdown;
}
"""


def gen(src=SRC, tgt="freebsd-kernel"):
    t = target.load(tgt)
    m = analyze("dev_if.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    return lower_kobj.lower(m, t, "dev_if.kif")


def test_header_and_source():
    f = gen()
    assert sorted(f) == ["dev_if.c", "dev_if.h"]
    h, c = f["dev_if.h"], f["dev_if.c"]
    assert "#include <sys/kobj.h>" in h and "kobj.h" not in c.replace('"dev_if.h"', "")
    assert "#define DEV_IF_VERSION 2" in h and "#include <sys/tslog.h>" in h
    assert "extern struct kobjop_desc dev_read_desc;" in h
    assert "typedef int32_t dev_read_t(device_t dev, uint32_t off, uint8_t *buf);" in h
    assert "\tTSENTER2(device_get_name(dev));\n\tKOBJOPLOOKUP(((kobj_t)dev)->ops, dev_read);" in h
    assert "rc = ((dev_read_t *) _m)(dev, off, buf);\n\tTSEXIT2(device_get_name(dev));\n\treturn (rc);" in h
    # a static method looks the method up in its first parameter (a class) and has no return value
    assert "KOBJOPLOOKUP(driver->ops, dev_identify);\n\t((dev_identify_t *) _m)(driver, parent);" in h
    assert "{ &dev_read_desc, (kobjop_t)kobj_error_method }" in c
    assert "{ &dev_shutdown_desc, (kobjop_t)null_shutdown }" in c
    assert "static int null_shutdown" in c


def test_refusals():
    base = "extern \"C\" type device_t;\n#[kobj, self(device_t)]\ninterface d {\n%s\n}\n"
    for body, text in [("method m() -> i32 { 0 }", "default body"), ("static method m() -> i32;", "first parameter"),
                       ("before { } method m() -> i32;", "hooks")]:
        try:
            gen(base % body)
        except lower_c.LowerError as e:
            assert text in e.message, e.message
        else:
            raise AssertionError(body)


def test_target_without_kobj(tmp_path):
    src = tmp_path / "d_if.kif"
    src.write_text(SRC)
    assert kifc.main(["--freebsd", str(tmp_path / "o"), "--target", "c", str(src)]) != 0


def test_cli_writes_outputs(tmp_path):
    src = tmp_path / "dev_if.kif"
    src.write_text(SRC)
    out = tmp_path / "k"
    assert kifc.main(["--freebsd", str(out), "--target", "freebsd-kernel", str(src)]) == 0
    assert (out / "dev_if.h").exists() and (out / "dev_if.c").exists()


CLASS = """
extern "C" type device_t;
extern "C" type driver_t;

#[kobj, self(device_t)]
interface uart {
  method probe() -> i32;
  method attach() -> i32;
  method detach() -> i32;
  static method identify(driver: c_ptr<driver_t>, parent: device_t);
}

struct sc { n: u32, name: str, }
struct plain { n: u32, }

impl uart for sc {
  fn probe(&self) -> i32 { 0 }
  fn attach(&mut self) -> i32 { self.n = 1; 0 }
  fn detach(&mut self) -> i32 { 0 }
  fn identify(driver: c_ptr<driver_t>, parent: device_t) { }
}
"""


def classes(src=CLASS, tgt="freebsd-kernel"):
    t = target.load(tgt)
    m = analyze("u.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    return lower_kobj.lower(m, t, "u.kif")


def test_class_managed_lifecycle_and_static():
    c = classes()["u_kobj.c"]
    assert "struct sc *sc = device_get_softc(dev);" in c
    assert "sc_init(sc);\n\terror = sc_attach(sc);\n\tif (error != 0)\n\t\tsc_drop(sc);" in c
    assert "error = sc_detach(sc);\n\tif (error == 0)\n\t\tsc_drop(sc);" in c
    assert "return sc_probe(device_get_softc(dev));" in c  # not a lifecycle method: only the accessor
    assert "sc_identify(driver, parent);" in c and "KOBJMETHOD(uart_identify, sc_uart_identify_kobj)" in c
    assert "DEFINE_CLASS_0(sc, sc_class, sc_methods, sizeof(struct sc));" in c


def test_class_object_attribute_replaces_accessor_and_lifecycle():
    src = CLASS.replace("impl uart for sc", '#[object("my_state({handle})")]\nimpl uart for sc')
    c = classes(src)["u_kobj.c"]
    assert "my_state(dev)" in c and "device_get_softc" not in c and "sc_init" not in c


def test_class_without_drop_omits_drop_call():
    src = CLASS.replace("name: str, ", "")
    c = classes(src)["u_kobj.c"]
    assert "sc_init(sc);" in c and "sc_drop" not in c


def unmapped(src):
    t = target.load("freebsd-kernel")
    t.data["kobj"]["handles"] = {}
    t.data["kobj"]["lifecycle"] = []
    m = analyze("u.kif", src, cfg_env=t.cfg_env())
    assert not [d for d in m.diagnostics if d.severity == 1], [d.message for d in m.diagnostics]
    return lower_kobj.lower(m, t, "u.kif")["u_kobj.c"]


def test_class_unmapped_handle_is_cast_and_unmanaged():
    src = CLASS.replace('extern "C" type driver_t;', 'extern "C" type driver_t;\nextern "C" type kobj_hdr;').replace("struct sc { n: u32,", "struct sc { kobj: kobj_hdr, n: u32,")
    c = unmapped(src)
    assert "(struct sc *)dev" in c and "sc_init" not in c


def test_class_unmapped_handle_needs_header_first():
    with pytest.raises(lower_c.LowerError, match="first field must be the kobj header"):
        unmapped(CLASS)
