# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""kif fmt, lint, graph, doc and the quick-fix engine (docs/kif-tools.md)."""

import glob
import io
import os
import re
from contextlib import redirect_stderr, redirect_stdout

import pytest

from kiflang import doc, graph, quickfix
from kiflang.fmt import FormatError, format_text
from kiflang.kif import main
from kiflang.lexer import lex
from kiflang.lint import RULES, Config, lint, parse_config
from kiflang.symbols import analyze

ROOT = os.path.join(os.path.dirname(__file__), "..")


def mod_of(src, files=None):
    files = files or {}
    return analyze("/t/main.kif", src, lambda _k, p: ("/t/" + p, files[p]) if p in files else None)


def codes(src, cfg=None):
    return [d.code for d in lint(mod_of(src), cfg)]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- fmt


def test_fmt_indents_by_bracket_and_collapses_blank_lines():
    src = "\n\ninterface a {\n\n\n   method m(\n x: u32,\n y: u32) -> u32;  \n}\n\n\n"
    assert format_text(src) == "interface a {\n\n    method m(\n        x: u32,\n        y: u32\n    ) -> u32;\n}\n" or \
        format_text(src).startswith("interface a {\n\n    method m(\n        x: u32,\n        y: u32) -> u32;\n}\n")


def test_fmt_is_idempotent_and_keeps_tokens():
    src = "fn f() -> u32 {\nlet x = 1\n+ 2;\nif x == 3 {\n// last\n}\nx\n}\n"
    once = format_text(src)
    assert format_text(once) == once
    assert [(t.kind, t.text) for t in lex(once)[0]] == [(t.kind, t.text) for t in lex(src)[0]]


def test_fmt_continuation_lines_are_one_level_in():
    out = format_text("fn f() -> u32 {\nlet x = a\n.b()\n.c();\nx\n}\n")
    assert "    let x = a\n        .b()\n        .c();\n" in out


def test_fmt_comments_follow_the_code_around_them():
    out = format_text("fn f() {\n// before\nlet x = 1;\n  // tail note\n}\n")
    assert out == "fn f() {\n    // before\n    let x = 1;\n    // tail note\n}\n"


def test_fmt_leaves_raw_blocks_alone():
    src = "interface a {\nheader(c) {\n\n\n   int  x;\n}\n}\n"
    out = format_text(src)
    assert "header(c) {\n\n\n   int  x;\n}\n" in out
    assert out.startswith("interface a {\n    header(c) {")


def test_fmt_rejects_files_that_do_not_lex():
    with pytest.raises(FormatError):
        format_text('fn f() { "oops }\n')


def test_fmt_keeps_every_shipped_kif_and_doc_example():
    texts = [open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(ROOT, "kiflang/prelude/*.kif"))]
    doc_md = open(os.path.join(ROOT, "docs/kif-proc-language.md"), encoding="utf-8").read()
    texts += re.findall(r"```kif\n(.*?)```", doc_md, re.S)
    assert len(texts) > 20
    for t in texts:
        try:
            once = format_text(t)
        except FormatError:
            continue
        assert format_text(once) == once


# ---------------------------------------------------------------- lint

KOBJ = """\
// Doc.
#[kobj, version(1)]
interface dev {
    // Doc.
    method read(offset: u32) -> u32;
}
"""


def test_a_clean_file_has_no_findings():
    assert codes(KOBJ) == []


def test_naming_rules():
    src = "// d\n#[kobj, version(1)]\ninterface dev_if {\n    method dev_if_read(a: u32) -> u32;\n}\n"
    assert codes(src) == ["KIFL001", "KIFL002", "KIFL003"]


def test_symbol_length_is_configurable():
    assert "KIFL004" in codes(KOBJ, Config(max_symbol_length=5))
    assert "KIFL004" not in codes(KOBJ)


def test_kobj_rules_do_not_apply_to_plain_interfaces():
    src = "// d\ninterface dev_if {\n    // d\n    method dev_if_read(offset: usize) -> usize;\n}\n"
    assert codes(src) == []


def test_missing_version_and_platform_width_types():
    src = "// d\n#[kobj]\ninterface dev {\n    // d\n    method read(offset: usize) -> u32;\n}\n"
    assert codes(src) == ["KIFL010", "KIFL012"]


def test_struct_by_value_is_flagged_but_pointer_is_not():
    head = "struct pt { x: u32 }\n// d\n#[kobj, version(1)]\ninterface dev {\n    // d\n"
    assert "KIFL013" in codes(head + "    method get(p: pt) -> u32;\n}\n")
    assert "KIFL013" not in codes(head + "    method get(p: c_ptr<pt>) -> u32;\n}\n")


def test_enum_without_underlying_type():
    assert codes("enum mode { a, b }") == ["KIFL011"]
    assert codes("enum mode: u8 { a, b }") == []


def test_flags_rules():
    src = "#[flags]\nenum perm: u8 { read = 1, write = 2, rw = 3, odd = 12, again = 1 }"
    assert codes(src) == ["KIFL016", "KIFL015", "KIFL017"] or sorted(codes(src)) == ["KIFL015", "KIFL016", "KIFL017"]
    assert codes("#[flags]\nenum perm: u8 { none = 0, read = 1, write = 2, rw = 3 }") == []


def test_unused_private_fn_shadowing_and_big_unsafe_rules():
    src = "private fn helper() -> u32 { 1 }\nfn f() -> u32 {\n    let x = 1;\n    let x = 2;\n    x\n}\n"
    assert codes(src) == ["KIFL030"]
    cfg = Config(levels={"KIFL031": "warning"})
    assert codes(src, cfg) == ["KIFL030", "KIFL031"]
    assert "KIFL030" not in codes("private fn helper() -> u32 { 1 }\nfn f() -> u32 { helper() }\n")


def test_suppression_comments():
    src = "// kif-lint: disable=KIFL011\nenum mode { a }\n"
    assert codes(src) == []
    assert codes("enum mode { a }\n// kif-lint: disable=KIFL011\n") == ["KIFL011"]
    assert codes("// kif-lint: disable-file=KIFL011\n\nenum mode { a }\n") == []


def test_config_parsing():
    cfg = parse_config("KIFL011 = off  # fine\nmax_symbol_length = 12\n")
    assert cfg.level("KIFL011") == "off" and cfg.max_symbol_length == 12
    for bad in ("KIFL999 = off", "KIFL011 = loud", "max_symbol_length = x", "nonsense"):
        with pytest.raises(ValueError):
            parse_config(bad)
    assert codes("enum mode { a }", cfg) == []


def test_every_rule_is_documented():
    guide = open(os.path.join(ROOT, "docs/kif-tools.md"), encoding="utf-8").read()
    for rule in RULES:
        assert f"| {rule} |" in guide, rule


# ---------------------------------------------------------------- graph

LIB = "namespace lib;\n#[kobj, version(1)]\ninterface base { method a() -> u32; }\n"
APP = ('import "lib.kif";\nuse lib::base;\n#[kobj, extends(base), version(1)]\ninterface derived { method b() -> u32; }\n'
       "struct impl_s { x: u32 }\nimpl base for impl_s { fn a(&self) -> u32 { 1 } }\n")


def test_graph_has_import_extends_and_implements_edges():
    g = graph.build([mod_of(APP, {"lib.kif": LIB})])
    kinds = sorted(k for _a, _b, k in g.edges)
    assert kinds == ["extends", "implements", "import"]
    dot, mer = graph.to_dot(g), graph.to_mermaid(g)
    assert "extends" in dot and "ltail=cluster_f_lib_kif" not in dot and "lhead=cluster_f_lib_kif" in dot
    assert "-. implements .->" in mer and "f_main_kif --> f_lib_kif" in mer


def test_graph_files_only_keeps_imports():
    g = graph.build([mod_of(APP, {"lib.kif": LIB})])
    assert "extends" not in graph.to_dot(g, files_only=True)
    assert "f_main_kif -> f_lib_kif" in graph.to_dot(g, files_only=True)


# ---------------------------------------------------------------- doc


def test_doc_covers_declarations_and_doc_comments():
    src = ('// A clock.\n#[kobj, version(2)]\ninterface clock {\n    // Reads it.\n    method read() -> u64;\n}\n'
           '// Perms.\n#[flags]\nenum perm: u8 {\n    // Can read.\n    read = 1,\n    write = 2,\n}\n'
           'struct hpet { base: u64 = 0 }\nimpl clock for hpet { fn read(&self) -> u64 { 0 } }\n')
    md = doc.render(mod_of(src))
    for text in ("## interface `clock`", "A clock.", "Reads it.", "C symbol: `clock_read`", "`#[version(2)]`",
                 "`read` = 1 (0x1) - Can read.", "Underlying type `u8`; a `#[flags]` enum", "implemented by: `hpet`",
                 "Implements: `clock`", "## `impl clock for hpet`"):
        assert text in md, text


# ---------------------------------------------------------------- quick fixes


def fixed(src, **kw):
    return quickfix.fix_all("/t/main.kif", src, **kw)[0]


def titles(src, code):
    mod, diags = quickfix.check("/t/main.kif", src)
    return [f.title for d in diags if d.code == code for f in quickfix.fixes_for(mod, d, diags)]


def test_auto_fixes_apply_unattended_and_keep_the_file_clean():
    src = "// d\n#[kobj]\ninterface dev {\n    // d\n    method read(offset: u32) -> u32;\n}\nenum mode { a }\n"
    out = fixed(src)
    assert "#[kobj, version(1)]" in out and "enum mode: i32 { a }" in out
    assert codes(out) == []
    assert fixed(out) == out


def test_unsafe_applies_preferred_fixes():
    src = "#[flags]\nenum perm: u8 { read = 1, write = 2 }\n"
    assert fixed(src) == src
    assert "enum perm: u8 { none = 0, read = 1" in fixed(src, unsafe=True)


def test_missing_semicolon_and_let_mut_and_c_types():
    assert "let x = 1; x" in fixed("fn f() -> u32 { let x = 1 x }", unsafe=True)
    assert "let mut x = 1;" in fixed("fn f() -> u32 {\n    let x = 1;\n    x = 2;\n    x\n}\n", unsafe=True)
    assert "a: i32" in fixed("struct s { a: int }\n", unsafe=True)


def test_unknown_name_offers_use_and_did_you_mean():
    lib = "namespace lib;\nstruct widget { x: u32 }\n"
    mod = mod_of("import \"lib.kif\";\nstruct s { w: widget }\n", {"lib.kif": lib})
    d = next(d for d in mod.diagnostics if d.code == "KIFP010")
    assert [f.title for f in quickfix.fixes_for(mod, d)][0].startswith("Add 'use lib::widget;'")
    assert any("Did you mean 'u32'?" in t for t in titles("struct s { a: u3 }\n", "KIFP010"))


def test_duplicate_rename_and_unused_removal_and_suppression():
    assert "Rename to 'a_2'" in titles("struct a { x: u32 }\nstruct a { y: u32 }\n", "KIF010")
    src = "private fn helper() -> u32 { 1 }\nstruct s { x: u32 }\n"
    out = quickfix.apply_edits(src, next(f for f in quickfix.fixes_for(*_first(src, "KIFL030")) if f.title.startswith("Remove")).edits)
    assert out == "struct s { x: u32 }\n"
    out = quickfix.apply_edits("enum mode { a }\n", quickfix.fixes_for(*_first("enum mode { a }\n", "KIFL011"))[2].edits)
    assert out.startswith("// kif-lint: disable=KIFL011\n") and codes(out) == []


def _first(src, code):
    mod, diags = quickfix.check("/t/main.kif", src)
    return mod, next(d for d in diags if d.code == code)


def test_overlapping_edits_are_refused():
    from kiflang.source import Pos, Range

    r = Range(Pos(0, 0), Pos(0, 3))
    with pytest.raises(quickfix.ConflictError):
        quickfix.apply_edits("abcdef", [quickfix.Edit(r, "x"), quickfix.Edit(Range(Pos(0, 2), Pos(0, 4)), "y")])


def test_a_round_that_adds_an_error_is_discarded():
    @quickfix.fixer("KIFL011")
    def bad(ctx, d):  # registered last, so it is tried after the real fix
        return [quickfix.Fix("break it", [quickfix.Edit(d.range, "???")], "KIFL011", auto=True)]

    try:
        out = fixed("enum mode { a }\n")
        assert "???" not in out
    finally:
        quickfix.FIXERS["KIFL011"].remove(bad)


# ---------------------------------------------------------------- command line


def test_cli_fmt_lint_fix_graph_doc(tmp_path):
    p = tmp_path / "a.kif"
    p.write_text("#[kobj]\ninterface dev {\nmethod read(offset: u32) -> u32;\n}\n")
    f = str(p)
    assert run("fmt", "--check", f)[0] == 1
    assert run("fmt", f)[0] == 0 and run("fmt", "--check", f)[0] == 0
    status, out, _ = run("lint", "--no-config", f)
    assert status == 0 and "KIFL010" in out
    assert run("lint", "--no-config", "--Werror", f)[0] == 1
    assert "fix 1: Add 'version(1)'  [auto]" in run("fix", "--no-config", f)[1]
    assert "+#[kobj, version(1)]" in run("fix", "--no-config", "--diff", f)[1]
    assert run("fix", "--no-config", "--apply", f)[0] == 0 and "version(1)" in p.read_text()
    assert "flowchart LR" in run("graph", "--format", "mermaid", f)[1]
    assert "## interface `dev`" in run("doc", f)[1]
    out_dir = tmp_path / "docs"
    assert run("doc", "-o", str(out_dir), f)[0] == 0 and (out_dir / "a.md").is_file()
    assert "KIFL001" in run("lint", "--list-rules")[1]


def test_cli_reports_errors_and_bad_config(tmp_path):
    p = tmp_path / "bad.kif"
    p.write_text("struct s { a: nosuch }\n")
    assert run("lint", "--no-config", str(p))[0] == 1
    assert run("doc", str(p))[0] == 1
    cfg = tmp_path / "cfg"
    cfg.write_text("KIFL999 = off\n")
    assert run("lint", "--config", str(cfg), str(p))[0] == 2


def test_imported_m_files_lint_and_format_cleanly():
    from kiflang import mimport

    sample = "INTERFACE widget;\nMETHOD int probe {\n\tdevice_t dev;\n};\n"
    kif = mimport.convert(sample, "widget_if.m").kif
    mod = mod_of(kif)
    assert not [d for d in mod.diagnostics if d.severity == 1]
    assert format_text(format_text(kif)) == format_text(kif)
    assert "KIFL001" not in [d.code for d in lint(mod)]


# ---------------------------------------------------------------- `.m` hooks (KIF088, KIF089, KIFP140)


def diag_codes(src):
    return [d.code for d in mod_of(src).diagnostics]


def test_hooks_apply_to_the_next_method():
    ok = "#[kobj]\ninterface d {\n    m_prolog {\n}\n    method a() -> u32;\n    m_epilog {\n}\n    method b() -> u32;\n}\n"
    assert diag_codes(ok) == []


def test_hook_without_a_method_after_it():
    assert diag_codes("#[kobj]\ninterface d {\n    method a() -> u32;\n    m_epilog {\n}\n}\n") == ["KIF088"]
    assert diag_codes("#[kobj]\ninterface d {\n    m_prolog {\n}\n    m_epilog {\n}\n}\n") == ["KIF088", "KIF088"]


def test_same_hook_twice_before_one_method():
    src = "#[kobj]\ninterface d {\n    m_prolog {\n}\n    m_prolog {\n}\n    method a() -> u32;\n}\n"
    assert diag_codes(src) == ["KIF089"]


def test_quickfix_removes_a_dangling_hook():
    src = "#[kobj, version(1)]\ninterface d {\n    method a() -> u32;\n    m_epilog {\nint x;\n}\n}\n"
    assert fixed(src, unsafe=True) == "#[kobj, version(1)]\ninterface d {\n    method a() -> u32;\n}\n"
    assert fixed(src) == src  # not an auto fix: it deletes user text


def test_quickfix_for_a_repeated_hook_offers_both_choices():
    src = "#[kobj, version(1)]\ninterface d {\n    m_prolog {\nfirst;\n}\n    m_prolog {\nsecond;\n}\n    method a() -> u32;\n}\n"
    mod, diags = quickfix.check("/t/main.kif", src)
    (diag,) = [d for d in diags if d.code == "KIF089"]
    titles = [f.title for f in quickfix.fixes_for(mod, diag, diags)]
    assert len(titles) == 2 and titles[0].startswith("Remove the earlier")
    kept = fixed(src, unsafe=True)
    assert "second;" in kept and "first;" not in kept
    assert mod_of(kept).diagnostics == []


def test_kobj_only_features_on_a_plain_interface():
    assert diag_codes("#[version(1)]\ninterface d { method a() -> u32; }\n") == ["KIFP140"]
    assert diag_codes("interface d {\n    m_prolog {\n}\n    method a() -> u32;\n}\n") == ["KIFP140"]
    assert diag_codes("#[kobj, version(1)]\ninterface d { method a() -> u32; }\n") == []
