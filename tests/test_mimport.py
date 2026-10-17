# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Tests for the FreeBSD .m to .kif importer (the .m text here is written for the test, in the makeobjops format)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kiflang.symbols import analyze  # noqa: E402
from kiflang.mimport import convert  # noqa: E402

SAMPLE = """\
#
# comment line
#
INTERFACE widget;

CODE {
	static int
	widget_attach_default(device_t dev)
	{
		return (0);
	}
}

HEADER {
	#include <sys/types.h>
}

/**
 * @brief Reads a register.
 */
METHOD int read {
	device_t dev;
	uint32_t off;
	uint8_t *out;
	const char *label;
	struct res * const r;
};

METHOD void reset {
	device_t dev;
} DEFAULT widget_reset_default;

STATICMETHOD int match {
	uint32_t id;
	unsigned long flags;
};

METHOD int register {
	device_t dev;
	int default;
	void *_cookie;
	const void *cptr;
};
"""


def kif_of(text):
    return convert(text, "widget_if.m")


class MImport(unittest.TestCase):
    def setUp(self):
        self.res = kif_of(SAMPLE)
        self.kif = self.res.kif

    def test_output_checks_clean(self):
        mod = analyze("w.kif", self.kif)
        self.assertEqual([d.format("w") for d in mod.diagnostics if d.severity == 1], [])

    def test_interface_self_and_methods(self):
        self.assertIn("#[kobj, self(device_t)]\ninterface widget {", self.kif)
        self.assertIn("method read(off: u32, out: c_ptr<u8>, label: cstr, r: c_ptr<kif_struct_res>) -> i32;", self.kif)
        self.assertIn("method reset() -> () default widget_reset_default;", self.kif)

    def test_static_keeps_all_params(self):
        self.assertIn("static method match_m(id: u32, flags: kif_unsigned_long) -> i32;", self.kif)
        self.assertIn("typedef unsigned long kif_unsigned_long;", self.kif)

    def test_keyword_names_renamed_and_reported(self):
        self.assertIn("method register(default: i32, cookie: c_ptr<void>, cptr: c_cptr<void>) -> i32;", self.kif)
        msgs = " ".join(m for _, m in self.res.warnings)
        self.assertIn("method 'match' renamed to 'match_m'", msgs)
        self.assertNotIn("'_cookie'", msgs)

    def test_doc_comment_becomes_comment(self):
        self.assertIn("// @brief Reads a register.", self.kif)

    def test_verbatim_keeps_indentation(self):
        # A column-1 '}' would end the block early in kif, so the body must stay indented.
        self.assertIn("code(c) {\n\tstatic int\n\twidget_attach_default(device_t dev)\n\t{\n\t\treturn (0);\n\t}\n}\n", self.kif)
        self.assertIn("header(c) {\n\t#include <sys/types.h>\n}\n", self.kif)

    def test_prolog_becomes_m_prolog(self):
        res = kif_of("INTERFACE a;\nPROLOG {\n\tx;\n}\nMETHOD void m {\n\tdevice_t d;\n};\n")
        self.assertIn("m_prolog {\n\tx;\n}", res.kif)
        self.assertFalse(any("PROLOG" in m for _, m in res.warnings))

    def test_function_pointer_parameter_left_as_comment(self):
        res = kif_of("INTERFACE a;\nMETHOD void m {\n\tdevice_t d;\n\tvoid (*cb)(int);\n};\n")
        self.assertIn("// TODO unconverted:", res.kif)
        self.assertEqual([d for d in analyze("a.kif", res.kif).diagnostics if d.severity == 1], [])

    def test_void_pointer_self_is_dropped(self):
        res = kif_of("INTERFACE a;\nMETHOD int m {\n\tvoid *obj;\n};\n")
        self.assertNotIn(" self ", res.kif)
        self.assertEqual([d for d in analyze("a.kif", res.kif).diagnostics if d.severity == 1], [])

    def test_kobj_t_self_is_default(self):
        self.assertNotIn(" self ", kif_of("INTERFACE a;\nMETHOD int m {\n\tkobj_t o;\n};\n").kif)

    def test_interface_is_always_kobj(self):
        self.assertIn("#[kobj]\ninterface a {", kif_of("INTERFACE a;\nMETHOD int m {\n\tkobj_t o;\n};\n").kif)

    def test_invalid_line_warns(self):
        res = kif_of("INTERFACE a;\nBOGUS\n")
        self.assertTrue(any("invalid line" in m for _, m in res.warnings))

    def test_comments_stay_above_their_item(self):
        res = kif_of("# licence\n\n/** group */\nINTERFACE a;\n# about hdr\nHEADER {\n\tx\n}\n"
                     "/** doc m */\nMETHOD int m {\n\tdevice_t d;\n\tvoid (*cb)(int);\n};\n")
        k = res.kif
        self.assertLess(k.index("// group"), k.index("interface a"))
        self.assertLess(k.index("// about hdr"), k.index("header(c) {"))
        self.assertLess(k.index("// licence"), k.index("// group"))
        self.assertLess(k.index("// doc m"), k.index("// TODO unconverted: METHOD"))

    def test_blank_lines_between_commented_methods_and_paragraphs(self):
        res = kif_of("INTERFACE a;\n/**\n * first\n *\n * second\n */\nMETHOD int m {\n\tdevice_t d;\n};\n"
                     "/** doc n */\nMETHOD int n {\n\tdevice_t d;\n};\n")
        self.assertIn("  // first\n  //\n  // second\n  method m() -> i32;\n\n  // doc n\n  method n() -> i32;\n", res.kif)

    def test_includes_and_detached_comments_kept(self):
        res = kif_of("#include <sys/bus.h>\nINTERFACE a;\n# about a\n\nMETHOD int m {\n\tdevice_t d;\n};\n")
        self.assertIn("include <sys/bus.h>;\n", res.kif)
        self.assertNotIn("kobj.h", res.kif)
        self.assertIn("  // about a\n\n  method m() -> i32;", res.kif)

    def test_unknown_qualifiers_after_method_are_ignored(self):
        for head in ("METHOD FOO int m", "METHOD FOO BAR int m", "STATICMETHOD FOO int m", "METHOD FOO struct x * m"):
            res = kif_of(f"INTERFACE a;\n{head} {{\n\tdevice_t d;\n}};\n")
            self.assertEqual(res.warnings, [], head)
            self.assertIn(f"{'static ' if 'STATIC' in head else ''}method m(", res.kif, head)
            self.assertNotIn("BAR", res.kif)
            self.assertNotIn("FOO", res.kif)

    def test_second_interface(self):
        res = kif_of("INTERFACE a;\nMETHOD int m {\n\tdevice_t d;\n};\nINTERFACE b;\nMETHOD int m {\n\tdevice_t d;\n};\n")
        self.assertIn("#[kobj, self(device_t)]\ninterface a {", res.kif)
        self.assertIn("#[kobj, self(device_t)]\ninterface b {", res.kif)

FREEBSD_SYS = os.environ.get("KIF_FREEBSD_SYS", "/home/jarek/Projekty/freebsd-src/sys")


class Hooks(unittest.TestCase):
    def test_prolog_and_epilog_apply_to_the_next_method_only(self):
        src = "INTERFACE d;\nPROLOG {\n\tA();\n}\nEPILOG {\n\tB();\n}\nMETHOD int one {\n\tdevice_t dev;\n};\nMETHOD int two {\n\tdevice_t dev;\n};\n"
        kif = convert(src, "d.m").kif
        self.assertLess(kif.index("m_prolog"), kif.index("method one"))
        self.assertLess(kif.index("m_epilog"), kif.index("method one"))
        self.assertNotIn("m_prolog", kif.split("method one")[1])
        self.assertEqual([d.code for d in analyze("d.kif", kif).diagnostics if d.severity == 1], [])


@unittest.skipUnless(os.path.isdir(FREEBSD_SYS), "no FreeBSD source tree (set KIF_FREEBSD_SYS)")
class WholeTree(unittest.TestCase):
    def test_every_m_file_imports_without_errors(self):
        import glob

        bad = []
        for path in sorted(glob.glob(os.path.join(FREEBSD_SYS, "**", "*.m"), recursive=True)):
            with open(path, errors="replace") as f:
                res = convert(f.read(), path)
            errs = [d for d in analyze("file:///x.kif", res.kif).diagnostics if d.severity == 1]
            if errs:
                bad.append((path, errs[0].code))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
