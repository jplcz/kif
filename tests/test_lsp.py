# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""The `.kif` language server (kiflang/lsp.py), driven through Server.handle()."""

import io

import pytest

from kiflang.lsp import Server, path_to_uri, read_message, write_message

SRC = """\
// A device.
#[kobj, version(1)]
interface dev {
    // Reads a register.
    method read(offset: u32) -> u32;
}

struct hpet { base: u64 }
enum mode: u8 { a, b }
fn use_it(m: mode, h: hpet) -> u32 { 0 }
"""


class Client:
    def __init__(self, tmp_path):
        self.s = Server()
        self.n = 0
        self.dir = tmp_path
        self.call("initialize", {})

    def call(self, method, params):
        self.n += 1
        out = self.s.handle({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        assert len(out) == 1
        if "error" in out[0]:
            raise ValueError(out[0]["error"]["message"])
        return out[0]["result"]

    def open(self, name, text):
        uri = path_to_uri(str(self.dir / name))
        out = self.s.handle({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "text": text}}})
        return uri, out[0]["params"]["diagnostics"]

    def at(self, uri, line, col):
        return {"textDocument": {"uri": uri}, "position": {"line": line, "character": col}}


@pytest.fixture
def c(tmp_path):
    return Client(tmp_path)


def find(text, needle, nth=0):
    """(line, column) of the nth occurrence of `needle`, pointing inside it."""
    at = -1
    for _ in range(nth + 1):
        at = text.index(needle, at + 1)
    line = text.count("\n", 0, at)
    return line, at - (text.rfind("\n", 0, at) + 1) + 1


def test_initialize_advertises_the_features(c):
    caps = c.call("initialize", {})["capabilities"]
    for k in ("definitionProvider", "hoverProvider", "referencesProvider", "renameProvider", "documentFormattingProvider"):
        assert caps[k]


def test_diagnostics_come_with_open_and_change(c):
    uri, diags = c.open("a.kif", "struct s { a: nosuch }\n")
    assert [d["code"] for d in diags] == ["KIFP010"] and diags[0]["severity"] == 1
    out = c.s.handle({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri}, "contentChanges": [{"text": "enum m { a }\n"}]}})
    assert [d["code"] for d in out[0]["params"]["diagnostics"]] == ["KIFL011"]  # lint runs once the file compiles
    out = c.s.handle({"method": "textDocument/didClose", "params": {"textDocument": {"uri": uri}}})
    assert out[0]["params"]["diagnostics"] == []


def test_lint_can_be_switched_off(tmp_path):
    c = Client(tmp_path)
    c.call("initialize", {"initializationOptions": {"lint": False}})
    assert c.open("a.kif", "enum m { a }\n")[1] == []


def test_document_symbols_nest_members(c):
    uri, _ = c.open("a.kif", SRC)
    syms = c.call("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    assert [s["name"] for s in syms] == ["dev", "hpet", "mode", "use_it"]
    assert [k["name"] for k in syms[0]["children"]] == ["read"]
    assert [k["name"] for k in syms[2]["children"]] == ["a", "b"]


def test_definition_hover_and_references(c):
    uri, _ = c.open("a.kif", SRC)
    line, col = find(SRC, "hpet", 1)  # the use in `use_it`
    loc = c.call("textDocument/definition", c.at(uri, line, col))
    assert loc["uri"] == uri and loc["range"]["start"]["line"] == 7
    hover = c.call("textDocument/hover", c.at(uri, find(SRC, "interface dev")[0], 12))["contents"]["value"]
    assert "interface dev" in hover and "A device." in hover
    refs = c.call("textDocument/references", c.at(uri, line, col))
    assert sorted(r["range"]["start"]["line"] for r in refs) == [7, 9]
    assert c.call("textDocument/hover", c.at(uri, 0, 0)) is None


def test_import_path_and_cross_file_definition(c):
    (c.dir / "lib.kif").write_text("namespace lib;\nstruct widget { x: u32 }\n")
    text = 'import "lib.kif";\nuse lib::widget;\nstruct s { w: widget, v: lib::widget }\n'
    uri, diags = c.open("main.kif", text)
    assert diags == [] or all(d["severity"] == 2 for d in diags)
    to_lib = c.call("textDocument/definition", c.at(uri, 0, 10))
    assert to_lib["uri"].endswith("lib.kif")
    for needle, nth in (("widget", 2), ("widget", 1)):
        loc = c.call("textDocument/definition", c.at(uri, *find(text, needle, nth)))
        assert loc["uri"].endswith("lib.kif") and loc["range"]["start"]["line"] == 1


def test_rename_changes_every_use_and_validates(c):
    uri, _ = c.open("a.kif", SRC)
    pos = c.at(uri, *find(SRC, "hpet", 1))
    assert c.call("textDocument/prepareRename", pos)["placeholder"] == "hpet"
    edits = c.call("textDocument/rename", {**pos, "newName": "timer"})["changes"][uri]
    assert len(edits) == 2 and {e["newText"] for e in edits} == {"timer"}
    for bad in ("struct", "Bad__Name", "1x"):
        with pytest.raises(ValueError):
            c.call("textDocument/rename", {**pos, "newName": bad})


def test_completion(c):
    uri, _ = c.open("a.kif", SRC + "#[\n")
    labels = {i["label"] for i in c.call("textDocument/completion", c.at(uri, 10, 2))}
    assert {"kobj", "flags", "version"} <= labels
    uri, _ = c.open("b.kif", SRC + "\n")
    labels = {i["label"] for i in c.call("textDocument/completion", c.at(uri, 10, 0))}
    assert {"fn", "u32", "dev", "hpet", "mode"} <= labels


def test_formatting_and_unformattable_files(c):
    uri, _ = c.open("a.kif", "struct s {\na: u32\n}\n")
    edits = c.call("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4}})
    assert edits[0]["newText"] == "struct s {\n    a: u32\n}\n"
    uri, _ = c.open("ok.kif", "struct s {\n    a: u32\n}\n")
    assert c.call("textDocument/formatting", {"textDocument": {"uri": uri}}) == []
    uri, _ = c.open("bad.kif", 'fn f() { "oops }\n')
    with pytest.raises(ValueError):
        c.call("textDocument/formatting", {"textDocument": {"uri": uri}})


def test_code_actions_quick_fix_and_fix_all(c):
    text = "// d\n#[kobj]\ninterface dev {\n    // d\n    method read(offset: u32) -> u32;\n}\nenum mode { a }\n"
    uri, _ = c.open("a.kif", text)
    rng = {"start": {"line": 1, "character": 0}, "end": {"line": 2, "character": 12}}
    acts = c.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": []}})
    titles = [a["title"] for a in acts]
    assert "Add 'version(1)'" in titles and any(t.startswith("Suppress KIFL010") for t in titles)
    fix_all = c.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng,
                                                "context": {"diagnostics": [], "only": ["source.fixAll"]}})
    assert len(fix_all) == 1 and fix_all[0]["kind"] == "source.fixAll.kif"
    new = fix_all[0]["edit"]["changes"][uri][0]["newText"]
    assert "version(1)" in new and "enum mode: i32" in new


def test_import_m_request(c):
    res = c.call("kif/importM", {"text": "INTERFACE widget;\nMETHOD int probe {\n\tdevice_t dev;\n};\n", "name": "widget_if.m"})
    assert "#[kobj" in res["kif"] and "interface widget" in res["kif"]


def test_unknown_requests_and_lifecycle(c):
    out = c.s.handle({"id": 99, "method": "no/such", "params": {}})
    assert out[0]["error"]["code"] == -32601
    assert c.call("shutdown", {}) is None
    assert c.s.handle({"method": "exit"}) == [] and c.s.exit_code == 0


def test_stdio_framing_round_trip():
    buf = io.BytesIO()
    write_message(buf, {"jsonrpc": "2.0", "id": 1, "method": "x"})
    buf.seek(0)
    assert read_message(buf)["id"] == 1 and read_message(buf) is None


COMPL = """\
struct hpet { base: u64, irq: u32 }
enum mode: u8 { fast, slow }
impl hpet {
    fn read(&self, off: u32) -> u32 { 0 }
    fn make() -> hpet { hpet { base: 0, irq: 0 } }
    fn tick(&self) {
        self.
    }
}
fn use_it(h: hpet, m: mode, v: vector<u8>) {
    h.
}
"""


def labels_at(c, name, text, needle, off=0):
    """Completion labels with the cursor `off` characters after the start of `needle`."""
    uri, _ = c.open(name, text)
    line, col = find(text, needle)
    return {i["label"]: i for i in c.call("textDocument/completion", c.at(uri, line, col - 1 + off))}


def test_completion_members_of_self_and_parameters(c):
    got = labels_at(c, "m.kif", COMPL, "self.", 5)
    assert {"base", "irq", "read", "tick"} <= set(got) and "make" not in got
    assert got["read"]["insertText"] == "read(${1:off})" and got["read"]["insertTextFormat"] == 2
    got = labels_at(c, "n.kif", COMPL, "    h.\n", 6)
    assert {"base", "read"} <= set(got)


def test_completion_enum_variants_and_associated_functions(c):
    text = COMPL + "fn g() { let a = mode::; let b = hpet::; }\n"
    assert {"fast", "slow"} <= set(labels_at(c, "e.kif", text, "mode::;", 6))
    got = labels_at(c, "f.kif", text, "hpet::;", 6)
    assert "make" in got and "read" not in got


def test_completion_context(c):
    got = labels_at(c, "t.kif", COMPL + "fn g(x: ) {}\n", "x: )", 3)
    assert "u32" in got and "hpet" in got and "fn" not in got and "g" not in got
    got = labels_at(c, "k.kif", COMPL + "\n", "fn use_it", 0)  # top level: declaration snippets, no statements
    assert "struct" in got and "let" not in got and got["struct"]["insertTextFormat"] == 2
    body = COMPL.replace("        self.\n", "        \n")
    got = labels_at(c, "b.kif", body, "        \n", 8)
    assert "let" in got and "struct" not in got


def expand(snippet):
    """The text a snippet inserts when every placeholder keeps its default."""
    import re
    s = re.sub(r"\$\{\d+:((?:[^{}]|\{[^{}]*\})*)\}", r"\1", snippet)
    return re.sub(r"\$\{\d+\}|\$\d+", "", s)


CTOR = """\
struct hpet { base: u64, ok: bool, name: str }
enum mode: u8 { fast, slow }
#[ops]
interface counter { method add(&mut self, n: u32) -> u32; }
impl hpet { fn make() -> hpet { hpet { base: 0, ok: true, name: str() } } }
fn use_it(m: mode) {
    let h: hpet = ;
    let k = hpet { base: 1, };
    
}
"""


def test_completion_construction_patterns(c):
    got = labels_at(c, "a.kif", CTOR, "= ;", 2)
    assert expand(got["hpet { .. }"]["insertText"]) == "hpet { base: 0, ok: false, name: name }"
    assert "hpet::make" in got and got["hpet::make"]["sortText"] == "0"
    got = labels_at(c, "b.kif", CTOR, "base: 1, }", 9)
    assert {"ok", "name", "remaining fields"} <= set(got) and "base" not in got
    assert expand(got["remaining fields"]["insertText"]) == "ok: false, name: name"
    got = labels_at(c, "c.kif", CTOR, "    \n}", 4)
    assert expand(got["match m { every mode }"]["insertText"]) == "match m {\n\tmode::fast => todo,\n\tmode::slow => todo,\n}"
    assert "hpet { .. }" in got and "ok(..)" in got and "kobj interface" not in got
    got = labels_at(c, "d.kif", CTOR + "\n", "fn use_it", 0)
    assert "impl hpet { new }" in got and "impl hpet { try_create }" in got and "kobj interface" in got and "tls cell" in got
    stub = expand(got["impl counter for .."]["insertText"])
    assert "fn add(&mut self, n: u32) -> u32 {" in stub and stub.startswith("impl counter for type {")


def test_top_level_templates_are_valid_kif(c):
    from kiflang.symbols import analyze
    got = labels_at(c, "e.kif", CTOR + "\n", "fn use_it", 0)
    for label in ("kobj interface", "ops interface", "impl hpet { new }", "impl hpet { try_create }", "result function", "tls cell"):
        src = "struct hpet { base: u64, ok: bool, name: str }\n" + expand(got[label]["insertText"]) + "\n"
        errs = [d for d in analyze("t.kif", src).diagnostics if d.severity == 1 and d.code.startswith("KIF0")]
        assert not errs, (label, errs)


INFER = """\
struct hpet { base: u64 }
impl hpet { fn read(&self) -> u32 { 0 } fn me(&self) -> hpet { hpet { base: 0 } } }
fn make() -> hpet { hpet { base: 1 } }
fn go(v: vector<u8>) {
    let h = make();
    h.
}
"""
CHAIN = INFER.replace("    h.\n", "    let n = make().me().\n")


def test_completion_infers_receiver_types(c):
    got = labels_at(c, "i.kif", INFER, "    h.\n", 6)  # an unannotated local
    assert {"base", "read", "me"} <= set(got)
    got = labels_at(c, "j.kif", CHAIN, "make().me().", 12)  # a call chain
    assert {"base", "read"} <= set(got)
    got = labels_at(c, "k.kif", INFER.replace("    h.\n", "    v.\n"), "    v.\n", 6)  # a prelude generic
    assert "base" not in got and "read" not in got and got  # vector members come from the prelude


def test_completion_infers_closure_parameter(c):
    text = "struct st { n: u32 }\n#[tls] static S: st = st { n: 0 };\nfn go() {\n    S.with(|c| c.)\n}\n"
    assert "n" in labels_at(c, "l.kif", text, "c.)", 2)


SIG = """\
struct hpet { base: u64 }
// Reads a register.
impl hpet { fn read(&self, off: u32, width: u8) -> u32 { 0 } fn make(base: u64) -> hpet { hpet { base: base } } }
// Adds two numbers.
fn add(a: u32, b: u32) -> u32 { a + b }
fn go(h: hpet) {
    add(1, );
    h.read(1, 2);
    let x = hpet::make();
    let y = h.read(add(1, 2), );
}
"""


def sig_at(c, name, text, needle, off):
    uri, _ = c.open(name, text)
    line, col = find(text, needle)
    return c.call("textDocument/signatureHelp", c.at(uri, line, col - 1 + off))


def test_signature_help(c):
    r = sig_at(c, "s1.kif", SIG, "add(1, );", 7)
    sig = r["signatures"][0]
    assert sig["label"] == "fn add(a: u32, b: u32) -> u32" and r["activeParameter"] == 1
    assert sig["label"][slice(*sig["parameters"][1]["label"])] == "b: u32"
    assert "Adds two numbers." in sig["documentation"]["value"]
    r = sig_at(c, "s2.kif", SIG, "h.read(1, 2)", 7)  # a method: the receiver is not a parameter
    assert r["signatures"][0]["label"] == "fn read(off: u32, width: u8) -> u32" and r["activeParameter"] == 0
    r = sig_at(c, "s3.kif", SIG, "hpet::make();", 11)
    assert r["signatures"][0]["label"] == "fn make(base: u64) -> hpet"
    r = sig_at(c, "s4.kif", SIG, "h.read(add(1, 2), );", 18)  # nested call: the outer one, second argument
    assert r["signatures"][0]["label"].startswith("fn read(") and r["activeParameter"] == 1
    assert sig_at(c, "s5.kif", SIG, "fn go", 0) is None


RES = """\
// A timer.
struct hpet {
    // Base address.
    base: u64,
}
// Reads a register.
impl hpet { fn read(&self, off: u32) -> u32 { 0 } }
// Adds two numbers.
fn add(a: u32, b: u32) -> u32 { a + b }
fn go(h: hpet) {
    h.;
    ;
}
"""


def resolved(c, name, text, needle, off, label):
    uri, _ = c.open(name, text)
    line, col = find(text, needle)
    items = c.call("textDocument/completion", c.at(uri, line, col - 1 + off))
    item = next(i for i in items if i["label"] == label)
    assert "documentation" not in item  # the list stays cheap
    return c.call("completionItem/resolve", item)


def test_completion_resolve_adds_detail_and_docs(c):
    assert "completionProvider" in c.call("initialize", {})["capabilities"]
    r = resolved(c, "r1.kif", RES, "h.;", 2, "read")
    assert r["detail"] == "fn read(&self, off: u32) -> u32" and "Reads a register." in r["documentation"]["value"]
    r = resolved(c, "r2.kif", RES, "h.;", 2, "base")
    assert r["detail"] == "base: u64" and "Base address." in r["documentation"]["value"]
    r = resolved(c, "r3.kif", RES, "    ;\n}", 4, "add")
    assert r["detail"].startswith("fn add(a: u32, b: u32)") and "Adds two numbers." in r["documentation"]["value"]
    r = resolved(c, "r4.kif", RES, "    ;\n}", 4, "hpet")
    assert r["detail"] == "struct hpet" and "A timer." in r["documentation"]["value"]


def test_completion_import_paths_and_unimported_names(c):
    (c.dir / "lib.kif").write_text("fn lib_add(a: u32) -> u32 { a }\nstruct libstate { n: u32 }\n")
    (c.dir / "sub").mkdir()
    (c.dir / "sub" / "deep.kif").write_text("fn deep() {}\n")
    text = '// Header.\n\nimport "lib.kif";\nfn go() { lib_ }\nimport ""\n'
    uri, _ = c.open("main.kif", 'import "\n')
    got = {i["label"]: i for i in c.call("textDocument/completion", c.at(uri, 0, 8))}
    assert {"lib.kif", "sub/"} <= set(got) and "main.kif" not in got
    uri, _ = c.open("m2.kif", 'import "sub/\n')
    assert {i["label"] for i in c.call("textDocument/completion", c.at(uri, 0, 12))} == {"deep.kif"}
    src = "// Header.\n\nfn go() { lib_ }\n"
    uri, _ = c.open("m3.kif", src)
    items = c.call("textDocument/completion", c.at(uri, 2, 14))
    it = next(i for i in items if i["label"] == "lib_add")
    assert it["insertText"] == "lib::lib_add"
    edit = it["additionalTextEdits"][0]
    assert edit["newText"] == 'import "lib.kif";\n' and edit["range"]["start"]["line"] == 2  # after the leading comment block
    uri, _ = c.open("m4.kif", 'import "lib.kif";\nfn go() { lib_ }\n')
    assert not [i for i in c.call("textDocument/completion", c.at(uri, 1, 14)) if i["label"] == "lib_add" and "additionalTextEdits" in i]


ATTRS = """\
#[kobj, version(1)]
interface base { method f(); }
struct dev { n: u32 }
gen fn derive_stats(iface: &Interface, out: &mut Emitter) {}
"""


def attr_labels(c, name, line_text):
    uri, _ = c.open(name, ATTRS + line_text + "\n")
    row = ATTRS.count("\n")
    return [i["label"] for i in c.call("textDocument/completion", c.at(uri, row, len(line_text)))]


def test_completion_attribute_arguments(c):
    assert attr_labels(c, "a1.kif", "#[repr(") == ["c", "packed"]
    assert attr_labels(c, "a2.kif", "#[derive(") == ["clone", "trace", "stats"]  # plus the module's own generators
    assert attr_labels(c, "a3.kif", "#[extends(") == ["base"]
    assert attr_labels(c, "a4.kif", "#[self(") == ["dev"]
    assert attr_labels(c, "a5.kif", "#[small_stack(") == ["off", "number"]
    assert attr_labels(c, "a6.kif", "#[kobj, errno(") == ["positive", "negative"]
    assert attr_labels(c, "a7.kif", "#[repr(c, ") == ["packed"]  # what is already written is not offered again
    assert attr_labels(c, "a8.kif", "#[export(") == ["cxx"]
    assert attr_labels(c, "a9.kif", "#[nosuch(") == []


def test_completion_error_names_and_cfg(tmp_path):
    c = Client(tmp_path)
    t = 'fn f() { let e = error::\n}\n'
    assert "not_found" in labels_at(c, "e1.kif", t, "error::", 7)
    t = '#[cfg(os = "fr")]\nfn g() {}\n'
    assert "freebsd" in labels_at(c, "e2.kif", t, '"fr', 3)
    t = "fn g() { cfg(a) {} }\n"
    got = labels_at(c, "e3.kif", t, "cfg(", 4)
    assert {"all", "c", "cxx", "os", "kobj"} <= set(got)
    t = "#[cfg(all(c, ))]\nfn g() {}\n"
    assert "target" in labels_at(c, "e4.kif", t, ", )", 2)


def test_completion_match_on_inferred_local(tmp_path):
    c = Client(tmp_path)
    t = "enum st { a, b }\nfn mk() -> st { st::a }\nfn f() {\n    let s = mk();\n    mat\n}\n"
    assert "match s { every st }" in labels_at(c, "m1.kif", t, "mat", 3)


def test_completion_ranking(tmp_path):
    c = Client(tmp_path)
    t = "fn helper() {}\nfn f(arg: u32) {\n    let first = 1;\n    let second = 2;\n    \n}\n"
    got = labels_at(c, "r1.kif", t, "    \n", 4)
    order = sorted(got, key=lambda n: got[n].get("sortText", n))
    assert order[:3] == ["second", "first", "arg"]
    assert order.index("helper") < order.index("loop")


EDITOR = """struct dev { id: u32 }
fn add(first: u32, second: u32) -> u32 { first + second }
fn run() -> u32 {
    let n = add(1, 2);
    let d = dev { id: n };
    add(n, d.id)
}
"""


def test_editor_features(tmp_path):
    c = Client(tmp_path)
    uri, _ = c.open("ed.kif", EDITOR)
    syms = c.call("workspace/symbol", {"query": "AD"})
    assert [s["name"] for s in syms] == ["add"]
    line, col = find(EDITOR, "let n")
    hl = c.call("textDocument/documentHighlight", c.at(uri, line, col + 4))
    assert len(hl) == 3  # `let n`, `{ id: n }`, `add(n, ..)`
    whole = {"textDocument": {"uri": uri}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 9, "character": 0}}}
    labels = [h["label"] for h in c.call("textDocument/inlayHint", whole)]
    assert ": u32" in labels and "first:" in labels and "second:" in labels
    assert "n:" not in labels
    toks = c.call("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    assert toks and len(toks) % 5 == 0


def test_static_snippets_option_drops_keyword_snippets(tmp_path):
    c = Client(tmp_path)
    assert labels_at(c, "s1.kif", "\n", "\n", 0)["struct"]["kind"] == 15
    c.call("initialize", {"initializationOptions": {"staticSnippets": True}})
    got = labels_at(c, "s2.kif", "\n", "\n", 0)
    assert got["struct"]["kind"] == 14 and "insertText" not in got["struct"]


def test_preview_c_returns_generated_files(tmp_path):
    c = Client(tmp_path)
    uri, _ = c.open("pv.kif", "#[export]\nfn twice(x: u32) -> u32 { x + x }\n")
    got = c.call("kif/previewC", {"uri": uri})
    assert got["ok"] and {"pv_proc.h", "pv_proc.c"} <= {f["name"] for f in got["files"]}
    assert any("twice" in f["text"] for f in got["files"])
    uri, _ = c.open("bad.kif", "fn f( {\n")
    got = c.call("kif/previewC", {"uri": uri})
    assert not got["ok"] and got["messages"]


def test_links_and_folding(tmp_path):
    c = Client(tmp_path)
    (tmp_path / "lib.kif").write_text("fn lib() {}\n")
    text = '// one\n// two\nimport "lib.kif";\nfn f() {\n    let a = 1;\n    let b = 2;\n}\n'
    uri, _ = c.open("lk.kif", text)
    links = c.call("textDocument/documentLink", {"textDocument": {"uri": uri}})
    assert len(links) == 1 and links[0]["target"].endswith("lib.kif")
    r = links[0]["range"]
    assert text.split("\n")[r["start"]["line"]][r["start"]["character"]:r["end"]["character"]] == "lib.kif"
    folds = {(f["startLine"], f["endLine"], f["kind"]) for f in c.call("textDocument/foldingRange", {"textDocument": {"uri": uri}})}
    assert folds == {(0, 1, "comment"), (3, 5, "region")}


def test_secure_wipe_hover_token_and_quickfix(c):
    text = "#[secure_wipe]\nstruct key { k: [u8; 4], p: ptr<u32> }\n"
    uri, _ = c.open("w.kif", text)
    hover = c.call("textDocument/hover", c.at(uri, 1, 8))["contents"]["value"]
    assert "Secure wipe" in hover
    data = c.call("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    assert any(data[i + 4] & 2 for i in range(0, len(data), 5))
    rng = {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 40}}
    acts = c.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": []}})
    fix = next(a for a in acts if "wipe_exempt" in a["title"])
    assert fix["edit"]["changes"][uri][0]["newText"] == "#[wipe_exempt] "


def test_hover_on_secure_locals(c):
    text = ("#[secure_wipe]\nstruct key { k: [u8; 4] }\nfn eat(k: key) -> u32 { return 0; }\n"
            "#[secure_wipe]\nfn f() -> u32 {\n    let a: u32 = 5;\n    let b = key { k: [1, 2, 3, 4] };\n    return eat(b) + a;\n}\n"
            "fn g() -> u32 { let z: u32 = 1; return z; }\n")
    uri, _ = c.open("h.kif", text)
    hover = lambda line, col: (c.call("textDocument/hover", c.at(uri, line, col)) or {}).get("contents", {}).get("value", "")
    assert "wiped after its last use (line 8)" in hover(5, 8)
    assert "holds a `#[secure_wipe]` struct" in hover(6, 8) and "moved from" in hover(6, 8)
    assert "Secure wipe" not in hover(9, 21)
