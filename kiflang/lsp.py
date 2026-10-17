# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Language server for `.kif` files over stdio (docs/kif-tools.md, "lsp").

Everything is built on the tools that already exist: `symbols.analyze` (diagnostics, names), `lint.lint`,
`fmt.format_text` (formatting) and `quickfix` (code actions). `Server.handle(msg)` is transport independent: it takes
one decoded JSON-RPC message and returns the messages to send, so tests need no process.

Supported: diagnostics (compiler + lint), documentSymbol, definition (names and `import` paths), hover, references,
rename, completion, signatureHelp, workspace symbols, documentHighlight, inlay hints, semantic tokens, formatting and
codeAction (quick fixes and `source.fixAll.kif`).
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

from . import ast, hints, passes, taint, wipes
from . import diagnostics as dg
from . import lexer as lx
from . import quickfix
from . import types as T
from .astutil import doc_above, slice_text, walk
from .complete import complete, resolve_item, signature_help
from .fmt import FormatError, format_text
from . import manifest as abi
from .lint import Config, find_config, lint, parse_config
from .parser import valid_name
from .source import Pos, Range
from .symbols import Module, SCALARS, Symbol, analyze

# LSP SymbolKind values.
SK_NAMESPACE, SK_CLASS, SK_METHOD, SK_ENUM, SK_INTERFACE, SK_FUNCTION, SK_VARIABLE, SK_CONSTANT = 3, 5, 6, 10, 11, 12, 13, 14
SEM_MODS = ["tainted", "secure"]
SEM_TYPES = ["namespace", "type", "enum", "interface", "struct", "function", "variable"]
SEM_KIND = {"struct": 4, "enum": 2, "interface": 3, "fn": 5, "const": 6}
SK_FIELD, SK_ENUM_MEMBER, SK_STRUCT, SK_TYPE_PARAMETER = 8, 22, 23, 26

ATTRIBUTES = sorted(
    "align allocates atomic_callback atomic_entry atomic_exempt atomic_lock atomic_only atomic_scope atomic_token caller_location "
    "checked copy count cxx_name derive enter_atomic ensures export extends flags foreign forward_args format header heap inline "
    "kobj lang leave_atomic len_ge link_name len_of lt_len might_sleep no_prefix non_empty non_null operator ops per_cpu preserves provided repr safe self send small_stack "
    "some stack sync target tainted tainted_result tls trace trusted typedef unchecked_form track_caller unsafe untaint version visibility when zeroable".split()
)

_KIND = {"struct": SK_STRUCT, "enum": SK_ENUM, "interface": SK_INTERFACE, "fn": SK_FUNCTION, "const": SK_CONSTANT}


def uri_to_path(uri: str) -> str:
    return url2pathname(unquote(urlparse(uri).path))


def path_to_uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path))


def lsp_range(r: Range) -> dict:
    return {"start": {"line": r.start.line, "character": r.start.col}, "end": {"line": r.end.line, "character": r.end.col}}


def lsp_pos(p: dict) -> Pos:
    return Pos(p["line"], p["character"])


def to_diag(d: dg.Diagnostic) -> dict:
    return {"range": lsp_range(d.range), "severity": d.severity, "code": d.code, "source": "kif",
            "message": d.message}


def from_diag(j: dict) -> dg.Diagnostic:
    r = j["range"]
    return dg.Diagnostic(Range(lsp_pos(r["start"]), lsp_pos(r["end"])), j.get("severity", 1) if j.get("severity", 1) in (1, 2, 3) else 2,
                         str(j.get("code", "")), j.get("message", ""))


def _edit_json(e: quickfix.Edit) -> dict:
    return {"range": lsp_range(e.range), "newText": e.text}


def _intersects(a: Range, b: Range) -> bool:
    return a.start <= b.end and b.start <= a.end


# ---------------------------------------------------------------- finding names in text


def path_at(tokens: List[lx.Token], pos: Pos) -> Optional[Tuple[List[str], int]]:
    """The `a::b::c` path through the identifier under `pos`, cut after that identifier, and the identifier's token index."""
    idx = next((i for i, t in enumerate(tokens) if t.kind == lx.IDENT and t.range.contains(pos)), None)
    if idx is None:
        return None
    parts = [tokens[idx].text]
    i = idx
    while i >= 2 and tokens[i - 1].text == "::" and tokens[i - 2].kind == lx.IDENT:
        parts.insert(0, tokens[i - 2].text)
        i -= 2
    return parts, idx


def lookup(mod: Module, parts: List[str]) -> Optional[Symbol]:
    """The declaration a (possibly qualified) name refers to, as the checker resolves it."""
    if len(parts) == 1:
        name = parts[0]
        for scope in (mod.scope_types, mod.scope_values):
            if name in scope:
                return scope[name]
        pre = mod.prelude
        if pre is not None:
            return pre.types.get(name) or pre.values.get(name)
        return None
    owner = mod.modules.get(parts[0]) or (mod.prelude.modules.get(parts[0]) if mod.prelude else None)
    for p in parts[1:-1]:
        owner = owner.modules.get(p) if owner else None
    if owner is None:
        return None
    return owner.types.get(parts[-1]) or owner.values.get(parts[-1])


# ---------------------------------------------------------------- the server


class Server:
    """Transport-independent core: `handle()` takes one decoded message and returns the messages to send."""

    def __init__(self) -> None:
        self.docs: Dict[str, str] = {}  # uri -> text of the open documents
        self.include_dirs: List[str] = []
        self.lint_enabled = True
        self.lint_config: Optional[str] = None
        self.baselines: List[str] = []  # ABI manifest files; the one named like the document enables KIFL020-033
        self.shutdown = False
        self.exit_code: Optional[int] = None
        self.hint_mode = "kept"  # which run-time check hints to show: off | kept | all (kif.server.inlayHints.checks)
        self.hint_taint = True  # show the speculation barriers and untaint reasons (kif.server.inlayHints.taint)
        self.hint_delay = 0.4  # seconds the document must be quiet before check hints are computed (kif.server.inlayHints.delayMs)
        self.hint_cache = hints.HintCache()
        self.now = time.monotonic  # replaced in tests
        self.last_change: Dict[str, float] = {}
        self.refresh_due: Optional[float] = None
        self.refresh_support = False
        self.request_id = 0

    # ---- analysis ----------------------------------------------------------------------------------------

    def resolve(self, importer: str, path: str) -> Optional[Tuple[str, str]]:
        """Import resolution: next to the importing file, then the include directories; open documents win over disk."""
        for cand in [os.path.join(os.path.dirname(importer), path)] + [os.path.join(d, path) for d in self.include_dirs]:
            real = os.path.realpath(cand)
            for uri, text in self.docs.items():
                if os.path.realpath(uri_to_path(uri)) == real:
                    return real, text
            if os.path.isfile(real):
                try:
                    with open(real, encoding="utf-8") as f:
                        return real, f.read()
                except OSError:
                    return None
        return None

    def key(self, uri: str) -> str:
        return os.path.realpath(uri_to_path(uri))

    def module(self, uri: str) -> Module:
        return analyze(self.key(uri), self.docs[uri], self.resolve)

    def config_for(self, uri: str) -> Config:
        path = self.lint_config or find_config(uri_to_path(uri))
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    return parse_config(f.read())
            except (OSError, ValueError):
                pass
        return Config()

    def baseline_for(self, uri: str) -> Optional[dict]:
        """The released manifest of this document, or None (unreadable manifests are skipped, never fatal)."""
        loaded = []
        for path in self.baselines:
            try:
                with open(path, encoding="utf-8") as f:
                    loaded.append(abi.loads(f.read(), path))
            except (OSError, ValueError):
                continue
        return abi.pick(loaded, uri_to_path(uri))

    def diagnostics(self, uri: str, mod: Module) -> List[dg.Diagnostic]:
        """Compiler diagnostics, plus lint once the file compiles (the same as `kif lint`)."""
        found = list(mod.diagnostics)
        if not any(d.severity == dg.ERROR for d in found):
            found += passes.check_module(mod.tree)
        if self.lint_enabled and not any(d.severity == dg.ERROR for d in found):
            found += lint(mod, self.config_for(uri), self.baseline_for(uri))
        return found

    def publish(self, uri: str) -> List[dict]:
        mod = self.module(uri)
        params = {"uri": uri, "diagnostics": [to_diag(d) for d in self.diagnostics(uri, mod)]}
        return [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params}]

    def _uri_of_key(self, key: str) -> str:
        for uri in self.docs:
            if self.key(uri) == key:
                return uri
        return path_to_uri(key)

    # ---- dispatch ----------------------------------------------------------------------------------------

    def handle(self, msg: dict) -> List[dict]:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            return []
        name = re.sub(r"[/$.]", "_", method)
        if "id" in msg:
            fn = getattr(self, "req_" + name, None)
            if fn is None:
                return [self.error(msg["id"], -32601, f"method not found: {method}")]
            if self.shutdown and method != "shutdown":
                return [self.error(msg["id"], -32600, "server is shutting down")]
            try:
                return [{"jsonrpc": "2.0", "id": msg["id"], "result": fn(params)}]
            except ValueError as e:
                return [self.error(msg["id"], -32602, str(e))]
            except KeyError as e:
                return [self.error(msg["id"], -32602, f"unknown document or missing parameter: {e}")]
        fn = getattr(self, "note_" + name, None)
        return fn(params) if fn else []

    @staticmethod
    def error(req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    # ---- lifecycle ---------------------------------------------------------------------------------------

    def req_initialize(self, p: dict) -> dict:
        opts = p.get("initializationOptions") or {}
        self.include_dirs = [d for d in opts.get("includeDirs", []) if isinstance(d, str)]
        self.lint_enabled = bool(opts.get("lint", True))
        self.lint_config = opts.get("lintConfig")
        self.baselines = [b for b in opts.get("baselines", []) if isinstance(b, str)]
        self.set_hint_options(opts.get("inlayHints") or {})
        self.refresh_support = bool(((p.get("capabilities") or {}).get("workspace") or {}).get("inlayHint", {}).get("refreshSupport"))
        self.static_snippets = bool(opts.get("staticSnippets", False))  # the client ships its own keyword snippets
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1},
                "documentSymbolProvider": True,
                "definitionProvider": True,
                "hoverProvider": True,
                "codeLensProvider": {"resolveProvider": False},
                "referencesProvider": True,
                "renameProvider": {"prepareProvider": True},
                "completionProvider": {"triggerCharacters": [":", "[", ".", "(", "\"", "/"], "resolveProvider": True},
                "signatureHelpProvider": {"triggerCharacters": ["(", ","]},
                "workspaceSymbolProvider": True,
                "documentLinkProvider": {"resolveProvider": False},
                "foldingRangeProvider": True,
                "documentHighlightProvider": True,
                "inlayHintProvider": True,
                "semanticTokensProvider": {"legend": {"tokenTypes": SEM_TYPES, "tokenModifiers": SEM_MODS}, "full": True},
                "documentFormattingProvider": True,
                "codeActionProvider": {"codeActionKinds": ["quickfix", "source.fixAll.kif"]},
            },
            "serverInfo": {"name": "kif-lsp"},
        }

    def set_hint_options(self, o: dict) -> None:
        if o.get("checks") in hints.MODES:
            self.hint_mode = o["checks"]
        if isinstance(o.get("taint"), bool):
            self.hint_taint = o["taint"]
        if isinstance(o.get("delayMs"), (int, float)) and not isinstance(o.get("delayMs"), bool) and o["delayMs"] >= 0:
            self.hint_delay = o["delayMs"] / 1000.0

    def req_shutdown(self, _p: dict) -> None:
        self.shutdown = True
        return None

    def note_exit(self, _p: dict) -> List[dict]:
        self.exit_code = 0 if self.shutdown else 1
        return []

    def note_initialized(self, _p: dict) -> List[dict]:
        return []

    # ---- documents ---------------------------------------------------------------------------------------

    def note_textDocument_didOpen(self, p: dict) -> List[dict]:
        d = p["textDocument"]
        self.docs[d["uri"]] = d["text"]
        return self.publish(d["uri"])

    def note_textDocument_didChange(self, p: dict) -> List[dict]:
        uri = p["textDocument"]["uri"]
        if uri not in self.docs or not p.get("contentChanges"):
            return []
        self.docs[uri] = p["contentChanges"][-1]["text"]  # full sync
        self.last_change[uri] = self.now()
        return self.publish(uri)

    def note_textDocument_didClose(self, p: dict) -> List[dict]:
        uri = p["textDocument"]["uri"]
        self.docs.pop(uri, None)
        return [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}}]

    def note_workspace_didChangeConfiguration(self, p: dict) -> List[dict]:
        s = (p.get("settings") or {}).get("kif") or {}
        self.include_dirs = [d for d in s.get("includeDirs", self.include_dirs) if isinstance(d, str)]
        self.lint_enabled = bool(s.get("lint", self.lint_enabled))
        self.lint_config = s.get("lintConfig", self.lint_config)
        self.baselines = [b for b in s.get("baselines", self.baselines) if isinstance(b, str)]
        self.set_hint_options(s.get("inlayHints") or {})
        return [m for uri in list(self.docs) for m in self.publish(uri)]

    def req_kif_importM(self, p: dict) -> dict:
        """Custom request: convert the text of a FreeBSD .m file to .kif (used by the editor preview)."""
        from .mimport import convert

        res = convert(p["text"], p.get("name", "input.m"))
        return {"kif": res.kif, "warnings": [{"line": ln, "message": m} for ln, m in res.warnings]}

    def req_kif_previewC(self, p: dict) -> dict:
        """Custom request: run the C backend (kifc) on the open text and return the generated files, or the compiler's messages.

        `target` is a built-in target name or a .json path (default `c`). A target with a kobj section also gets the kobj
        outputs (`--freebsd`). Nothing is written next to the source: the text goes to a temporary directory and imports are
        searched in the document's own directory and the include directories.
        """
        import subprocess
        import tempfile

        uri, target = p["uri"], p.get("target") or "c"
        src_dir = os.path.dirname(self.key(uri))
        with tempfile.TemporaryDirectory(prefix="kif-preview-") as tmp:
            src = os.path.join(tmp, os.path.basename(self.key(uri)))
            with open(src, "w", encoding="utf-8") as f:
                f.write(self.docs.get(uri, p.get("text", "")))
            cmd = [sys.executable, "-m", "kiflang.kifc", "--target", target, "--c", tmp, "--emit-source", "-I", src_dir]
            for d in self.include_dirs:
                cmd += ["-I", d]
            try:
                from . import target as targetdef
                if targetdef.load(target).supports("kobj"):
                    cmd += ["--freebsd", tmp]
            except Exception:  # an unreadable target is reported by kifc itself
                pass
            run = subprocess.run(cmd + [src], capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            files = []
            for name in sorted(os.listdir(tmp)):
                if name != os.path.basename(src):
                    with open(os.path.join(tmp, name), encoding="utf-8") as f:
                        files.append({"name": name, "text": f.read()})
            return {"ok": run.returncode == 0, "files": files if run.returncode == 0 else [], "messages": (run.stderr or run.stdout).strip()}

    # ---- symbols -----------------------------------------------------------------------------------------

    def req_textDocument_documentSymbol(self, p: dict) -> List[dict]:
        mod = self.module(p["textDocument"]["uri"])
        return [s for it in (mod.tree.items if mod.tree else []) if (s := self._symbol(it)) is not None]

    def _symbol(self, it: Any) -> Optional[dict]:
        def make(name: ast.Name, kind: int, node: Any, children: Optional[list] = None, detail: str = "") -> dict:
            out = {"name": name.text, "kind": kind, "range": lsp_range(node.range), "selectionRange": lsp_range(name.range),
                   "detail": detail}
            if children:
                out["children"] = children
            return out

        if isinstance(it, ast.Interface):
            kids = [make(m.name, SK_METHOD, m) for m in it.methods]
            return make(it.name, SK_INTERFACE, it, kids, "kobj" if any(a.name.text == "kobj" for a in it.attrs) else "")
        if isinstance(it, ast.EnumDecl):
            return make(it.name, SK_ENUM, it, [make(m.name, SK_ENUM_MEMBER, m) for m in it.members])
        if isinstance(it, ast.StructDecl):
            return make(it.name, SK_STRUCT, it, [make(f.name, SK_FIELD, f) for f in it.fields if hasattr(f, "name")])
        if isinstance(it, ast.FnDecl):
            return make(it.name, SK_FUNCTION, it)
        if isinstance(it, (ast.ConstDecl, ast.StaticDecl)):
            return make(it.name, SK_CONSTANT if isinstance(it, ast.ConstDecl) else SK_VARIABLE, it)
        if isinstance(it, ast.TypeAlias):
            return make(it.name, SK_TYPE_PARAMETER, it)
        if isinstance(it, ast.Namespace) and it.path:
            n = ast.Name(Range(it.path[0].range.start, it.path[-1].range.end), "::".join(p.text for p in it.path))
            return make(n, SK_NAMESPACE, it)
        return None

    def req_workspace_symbol(self, p: dict) -> List[dict]:
        """Every declaration of the open documents whose name contains the query (case-insensitive), nested members included."""
        q = (p.get("query") or "").lower()
        out: List[dict] = []

        def add(uri: str, sym: dict, container: str = "") -> None:
            if q in sym["name"].lower():
                out.append({"name": sym["name"], "kind": sym["kind"], "containerName": container,
                            "location": {"uri": uri, "range": sym["selectionRange"]}})
            for kid in sym.get("children", []):
                add(uri, kid, sym["name"])

        for uri in self.docs:
            mod = self.module(uri)
            for it in (mod.tree.items if mod.tree else []):
                if (sym := self._symbol(it)) is not None:
                    add(uri, sym)
        return out

    def req_textDocument_documentHighlight(self, p: dict) -> List[dict]:
        """Declaration uses of the same symbol in this document; for locals and fields, the same name in the same function."""
        uri, pos = p["textDocument"]["uri"], lsp_pos(p["position"])
        _sym, hits = self._references(uri, pos)
        ranges = [r for u, r in hits if u == uri]
        trail = taint.trail(self.module(uri), pos) if not ranges else None
        if trail is not None:
            sinks = [Range(Pos(a, b), Pos(c, d)) for a, b, c, d, _code in trail[2]]
            return [{"range": lsp_range(r), "kind": 2} for r in trail[0]] + [{"range": lsp_range(r), "kind": 3} for r in sinks]
        if not ranges:
            toks, _ = lx.lex(self.docs[uri])
            tok = next((t for t in toks if t.kind == lx.IDENT and t.range.contains(pos)), None)
            if tok is None:
                return []
            lines = self.docs[uri].split("\n")
            top = [i for i, l in enumerate(lines) if re.match(r"\s*(?:pub\s+)?(?:fn|gen\s+fn)\b", l) or l.startswith(("fn ", "#["))]
            start = max((i for i in top if i <= pos.line), default=0)
            end = min((i for i in top if i > pos.line), default=len(lines))
            ranges = [t.range for t in toks if t.kind == lx.IDENT and t.text == tok.text and start <= t.range.start.line < end]
        return [{"range": lsp_range(r), "kind": 1} for r in ranges]

    def req_textDocument_inlayHint(self, p: dict) -> List[dict]:
        """`: type` after an unannotated `let`, and `name:` before each argument of a call to a known free function."""
        uri, rng = p["textDocument"]["uri"], p["range"]
        mod = self.module(uri)
        lo, hi = rng["start"]["line"], rng["end"]["line"]
        out: List[dict] = []
        for n in walk(mod.tree) if mod.tree else []:
            if not lo <= n.range.start.line <= hi:
                continue
            if isinstance(n, ast.Let) and n.ty is None and n.init is not None:
                t = getattr(n.init, "ty", None)
                if isinstance(t, T.Ty) and t != T.UNKNOWN:
                    out.append({"position": lsp_range(n.name.range)["end"], "label": f": {t}", "kind": 1})
            elif isinstance(n, ast.Call) and isinstance(n.callee, ast.PathExpr):
                sym = lookup(mod, [seg.name for seg in n.callee.segments])
                fn = sym.node if sym is not None else None
                if not isinstance(fn, ast.FnDecl):
                    continue
                for param, arg in zip(fn.params, n.args):
                    same = isinstance(arg, ast.PathExpr) and arg.text == param.name.text
                    if not param.receiver and not same:
                        out.append({"position": lsp_range(arg.range)["start"], "label": f"{param.name.text}:", "kind": 2,
                                    "paddingRight": True})
        return out + self.check_hints(uri, mod, lo, hi)

    def check_hints(self, uri: str, mod: Module, lo: int, hi: int) -> List[dict]:
        """Run-time checks the provers drop (`✓`) or keep (`⚠`) in the functions that overlap the visible lines.

        Proving is the costly part, so it is cached per function and only done once the document has been quiet for the grace
        period: while the user types, a function whose text changed gets no hints (stale ones would sit at wrong places), and a
        `workspace/inlayHint/refresh` is scheduled for when the grace period ends."""
        if (self.hint_mode == "off" and not self.hint_taint) or mod.tree is None or any(d.severity == dg.ERROR for d in mod.diagnostics):
            return []
        lines = self.docs[uri].split("\n")
        quiet_at = self.last_change.get(uri, float("-inf")) + self.hint_delay
        quiet = self.now() >= quiet_at
        ctx = hints.HintCache.context(mod.tree, lines)
        out: List[dict] = []
        for fn in hints.functions_of(mod.tree):
            if fn.range.end.line < lo or fn.range.start.line > hi:
                continue
            try:
                found = self.hint_cache.for_function(fn, lines, ctx, quiet)
            except Exception:  # a construct the builder or provers cannot take must not break the other hints
                found = []
            if found is None:
                self.refresh_due = quiet_at
                continue
            for h in found:
                if not lo <= h.line <= hi:
                    continue
                if h.kind in hints.TAINT_KINDS:
                    if self.hint_taint:
                        label = ("⛓ barrier" if h.kind == "barrier" else "🛈 untaint: " + h.tip.split(": ", 1)[-1])
                        out.append({"position": {"line": h.line, "character": h.col}, "label": label, "paddingLeft": True, "tooltip": h.tip})
                    continue
                if self.hint_mode == "off" or (self.hint_mode == "kept" and h.proved):
                    continue
                name = hints.LABELS.get(h.kind, h.kind)
                tip = f"{name} check dropped: {h.why.replace('%', '')}" if h.proved else f"{name} check stays at run time: it could not be proved"
                out.append({"position": {"line": h.line, "character": h.col}, "label": ("✓ " if h.proved else "⚠ ") + name,
                            "paddingLeft": True, "tooltip": tip})
        return out

    def wait_time(self) -> Optional[float]:
        """Seconds until `tick()` has something to send, or None."""
        return None if self.refresh_due is None else max(0.0, self.refresh_due - self.now())

    def tick(self) -> List[dict]:
        """Ask the client to re-request inlay hints once the grace period of an edited document is over."""
        if self.refresh_due is None or self.now() < self.refresh_due:
            return []
        self.refresh_due = None
        if not self.refresh_support:
            return []
        self.request_id += 1
        return [{"jsonrpc": "2.0", "id": f"kif-refresh-{self.request_id}", "method": "workspace/inlayHint/refresh"}]

    def req_textDocument_semanticTokens_full(self, p: dict) -> dict:
        """Names that resolve to a declaration, classified by what they declare (the grammar colours everything else)."""
        uri = p["textDocument"]["uri"]
        mod = self.module(uri)
        toks, _ = lx.lex(self.docs[uri])
        data, last = [], (0, 0)
        hot = {(r.start.line, r.start.col) for r in taint.tainted_ranges(mod)}
        for t in toks:
            if t.kind != lx.IDENT or t.range.start.line != t.range.end.line:
                continue
            found = path_at(toks, t.range.start)
            sym = lookup(mod, found[0]) if found else None
            idx = SEM_KIND.get(sym.kind) if sym is not None else None
            tainted = (t.range.start.line, t.range.start.col) in hot
            secure = sym is not None and any(a.name.text == "secure_wipe" for a in getattr(sym.node, "attrs", None) or [])
            if idx is None and not tainted:
                continue
            line, col = t.range.start.line, t.range.start.col
            data += [line - last[0], col - (last[1] if line == last[0] else 0), t.range.end.col - col,
                     SEM_TYPES.index("variable") if idx is None else idx, (1 if tainted else 0) | (2 if secure else 0)]
            last = (line, col)
        return {"data": data}

    # ---- navigation --------------------------------------------------------------------------------------

    def _target(self, uri: str, pos: Pos) -> Tuple[Module, Optional[Symbol], Optional[Tuple[List[str], int]], List[lx.Token]]:
        mod = self.module(uri)
        tokens, _ = lx.lex(self.docs[uri])
        found = path_at(tokens, pos)
        return mod, (lookup(mod, found[0]) if found else None), found, tokens

    def _location(self, sym: Symbol) -> Optional[dict]:
        name = getattr(sym.node, "name", None)
        if name is None or not hasattr(name, "range") or sym.module.header is not None:
            return None
        return {"uri": self._uri_of_key(sym.module.key), "range": lsp_range(name.range)}

    def req_textDocument_definition(self, p: dict) -> Optional[dict]:
        uri, pos = p["textDocument"]["uri"], lsp_pos(p["position"])
        mod, sym, _found, tokens = self._target(uri, pos)
        if sym is not None:
            return self._location(sym)
        for t in tokens:
            if t.kind == lx.STRING and t.range.contains(pos):
                imp = next((i for i in (mod.tree.items if mod.tree else []) if isinstance(i, ast.Import) and i.range.contains(pos)), None)
                target = mod.imported.get(imp.path) if imp else None
                if target is not None and target.header is None:
                    return {"uri": self._uri_of_key(target.key), "range": lsp_range(Range(Pos(0, 0), Pos(0, 0)))}
        return None

    def req_textDocument_documentLink(self, p: dict) -> List[dict]:
        """Each `import "x.kif";` path is a link to the file it resolves to."""
        uri = p["textDocument"]["uri"]
        mod = self.module(uri)
        out = []
        for imp in (mod.tree.items if mod.tree else []):
            target = mod.imported.get(imp.path) if isinstance(imp, ast.Import) else None
            if target is None or target.header is not None:
                continue
            line = self.docs[uri].split("\n")[imp.range.start.line]
            q = line.find('"', imp.range.start.col)
            if q < 0:
                continue
            out.append({"range": lsp_range(Range(Pos(imp.range.start.line, q + 1), Pos(imp.range.start.line, q + 1 + len(imp.path)))),
                        "target": self._uri_of_key(target.key), "tooltip": target.key})
        return out

    def req_textDocument_codeLens(self, p: dict) -> List[dict]:
        """`N callers` above each `#[untaint]` function, `trusted` ones marked: the audited surface of the taint feature (docs/kif-taint.md)."""
        uri = p["textDocument"]["uri"]
        out: List[dict] = []
        for f in hints.functions_of(self.module(uri).tree):
            if not any(a.name.text == "untaint" for a in getattr(f, "attrs", [])):
                continue
            at = f.name.range.start
            _sym, refs = self._references(uri, at)
            calls = [(u, r) for u, r in refs if r != f.name.range]
            kind = "trusted untaint" if taint._trusted(f) else "untaint"
            title = f"{kind}: {len(calls)} caller{'' if len(calls) == 1 else 's'}"
            locs = [{"uri": u, "range": lsp_range(r)} for u, r in calls]
            out.append({"range": lsp_range(f.name.range), "command": {
                "title": title, "command": "editor.action.showReferences", "arguments": [uri, {"line": at.line, "character": at.col}, locs]}})
        return out

    def req_textDocument_foldingRange(self, p: dict) -> List[dict]:
        """Multi-line `{ .. }` blocks and runs of two or more `//` comment lines (a doc comment folds like a block)."""
        text = self.docs[p["textDocument"]["uri"]]
        toks, _ = lx.lex(text)
        out, stack = [], []
        for t in toks:
            if t.text == "{" and t.kind != lx.STRING:
                stack.append(t.range.start.line)
            elif t.text == "}" and t.kind != lx.STRING and stack:
                start = stack.pop()
                if t.range.start.line - 1 > start:  # the closing brace line stays visible: `fn f() { ... }`
                    out.append({"startLine": start, "endLine": t.range.start.line - 1, "kind": "region"})
        run = None
        lines = text.split("\n") + [""]
        for i, l in enumerate(lines):
            if l.lstrip().startswith("//"):
                run = i if run is None else run
            else:
                if run is not None and i - 1 > run:
                    out.append({"startLine": run, "endLine": i - 1, "kind": "comment"})
                run = None
        return out

    def req_textDocument_hover(self, p: dict) -> Optional[dict]:
        mod, sym, found, tokens = self._target(p["textDocument"]["uri"], lsp_pos(p["position"]))
        if found is not None and sym is None:
            note = taint.describe_local(mod, lsp_pos(p["position"]), found[0][-1]) if len(found[0]) == 1 else None
            wipe_note = wipes.describe_local(mod, lsp_pos(p["position"]), found[0][-1]) if len(found[0]) == 1 else None
            if note is None and wipe_note is None:
                return None
            if note is None:
                return {"contents": {"kind": "markdown", "value": wipe_note + "\n\n_docs/kif-secure-wipe.md_"}, "range": lsp_range(tokens[found[1]].range)}
            if wipe_note:
                note += "\n\n" + wipe_note
            trail = taint.trail(mod, lsp_pos(p["position"]))
            if trail is not None:
                if len(trail[1]) > 1:
                    note += "\n\n**Flow trail** (highlighted in the editor): " + " → ".join(f"`{n}`" for n in trail[1])
                if trail[2]:
                    note += "\n\n**Reaches** " + ", ".join(f"`{c}` at line {a + 1}" for a, _b, _c, _d, c in trail[2])
            return {"contents": {"kind": "markdown", "value": note + "\n\n_Taint: docs/kif-taint.md_"}, "range": lsp_range(tokens[found[1]].range)}
        if sym is None or found is None:
            return None
        text = f"```kif\n{self._signature(sym)}\n```"
        lines = sym.module.source.split("\n")
        doc = doc_above(lines, sym.node) if sym.module.source else ""
        if doc:
            text += "\n\n" + doc
        notes = taint.describe(sym.node)
        vendor = getattr(sym.node, "vendor", None)
        if vendor is not None:
            e = vendor.entry
            kept = e["kept_checks"]
            origin = f"**From `{vendor.module}.kifm`** (precompiled by {vendor.compiler['name']} {vendor.compiler['version']}); its body is not analyzed here, the vendor recorded: "
            origin += "no analysis" if kept == "unknown" else ("keeps " + ", ".join(kept) + " checks" if kept else "no run-time checks kept")
            if e["untaint"]:
                origin += f"; sanitizer, {'vendor-trusted (body not verified)' if e['untaint'] == 'trusted' else 'vendor-checked'}"
            notes.insert(0, origin + ".")
        if notes:
            text += "\n\n" + "\n\n".join(notes) + "\n\n_Taint: docs/kif-taint.md_"
        wipe = [a for a in getattr(sym.node, "attrs", None) or [] if a.name.text == "secure_wipe"]
        if wipe:
            what = "every local of this function is wiped after its last use (the nuclear option: the body is compiled unoptimized)" \
                if sym.kind == "fn" else "its values are wiped when dropped, field by field (`#[wipe_exempt]` fields excepted)"
            text += f"\n\n**Secure wipe:** {what}.\n\n_docs/kif-secure-wipe.md_"
        return {"contents": {"kind": "markdown", "value": text}, "range": lsp_range(tokens[found[1]].range)}

    @staticmethod
    def _signature(sym: Symbol) -> str:
        """The declaration up to its body, copied from the source."""
        src = sym.module.source
        if not src or sym.node.range.start.line >= len(src.split("\n")):
            return f"{sym.kind} {getattr(getattr(sym.node, 'name', None), 'text', '')}"
        text = slice_text(src.split("\n"), sym.node.range)
        head = re.split(r"\s*\{", text, maxsplit=1)[0].strip()
        return " ".join(head.split()) if len(head) < 200 else head[:200] + "..."

    def _references(self, uri: str, pos: Pos) -> Tuple[Optional[Symbol], List[Tuple[str, Range]]]:
        _mod, sym, _found, _tokens = self._target(uri, pos)
        if sym is None:
            return None, []
        want = (sym.module.key, sym.node.range)
        hits: List[Tuple[str, Range]] = []
        for u in self.docs:
            m = self.module(u)
            toks, _ = lx.lex(self.docs[u])
            for i, t in enumerate(toks):
                if t.kind != lx.IDENT or (i + 1 < len(toks) and toks[i + 1].text == "::"):
                    continue
                found = path_at(toks, t.range.start)
                s = lookup(m, found[0]) if found else None
                if s is not None and (s.module.key, s.node.range) == want and t.text == sym.node.name.text:
                    hits.append((u, t.range))
        return sym, hits

    def req_textDocument_references(self, p: dict) -> List[dict]:
        _sym, hits = self._references(p["textDocument"]["uri"], lsp_pos(p["position"]))
        return [{"uri": u, "range": lsp_range(r)} for u, r in hits]

    def req_textDocument_prepareRename(self, p: dict) -> Optional[dict]:
        sym, hits = self._references(p["textDocument"]["uri"], lsp_pos(p["position"]))
        pos = lsp_pos(p["position"])
        if sym is None or sym.module.header is not None:
            return None
        return next(({"range": lsp_range(r), "placeholder": sym.node.name.text} for u, r in hits if r.contains(pos)), None)

    def req_textDocument_rename(self, p: dict) -> dict:
        new = p["newName"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new) or new in lx.KEYWORDS or new in SCALARS:
            raise ValueError(f"{new!r} is not a valid name")
        sym, hits = self._references(p["textDocument"]["uri"], lsp_pos(p["position"]))
        if sym is None or sym.module.header is not None:
            raise ValueError("nothing to rename here")
        if isinstance(sym.node, (ast.Interface, ast.EnumDecl, ast.StructDecl)) and not valid_name(new):
            raise ValueError(f"{new!r} must be snake_case without '__' or a trailing '_'")
        changes: Dict[str, List[dict]] = {}
        for u, r in hits:
            changes.setdefault(u, []).append({"range": lsp_range(r), "newText": new})
        return {"changes": changes}

    # ---- completion --------------------------------------------------------------------------------------

    def req_textDocument_completion(self, p: dict) -> List[dict]:
        uri, pos = p["textDocument"]["uri"], lsp_pos(p["position"])
        items = complete(self.module(uri), self.docs[uri], pos.line, pos.col, ATTRIBUTES, SCALARS,
                         lambda t: analyze(self.key(uri), t, self.resolve),
                         lambda prefix: self._import_entries(uri, prefix), lambda: self._candidates(uri),
                         keyword_snippets=not getattr(self, "static_snippets", False))
        for i in items:
            if "data" in i:
                i["data"]["u"] = uri  # `completionItem/resolve` gets the item back without its document
        return items

    def _search_dirs(self, uri: str) -> List[str]:
        return [os.path.dirname(self.key(uri))] + [d for d in self.include_dirs if os.path.isdir(d)]

    def _import_entries(self, uri: str, prefix: str) -> List[Tuple[str, bool]]:
        """(name, is_directory) of the `.kif` files and directories under the part of `prefix` before its last `/`."""
        sub = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        seen: Dict[str, bool] = {}
        for base in self._search_dirs(uri):
            d = os.path.join(base, sub)
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                full = os.path.join(d, n)
                if os.path.isdir(full) and not n.startswith("."):
                    seen.setdefault(n, True)
                elif n.endswith(".kif") and os.path.realpath(full) != self.key(uri):
                    seen.setdefault(n, False)
        return sorted(seen.items())

    def _candidates(self, uri: str) -> List[Tuple[str, str, str]]:
        """(import path, module name, text) of the other `.kif` files next to the document and in the include directories."""
        out, names = [], set()
        for base in self._search_dirs(uri):
            try:
                listing = sorted(os.listdir(base))
            except OSError:
                continue
            for n in listing:
                full = os.path.join(base, n)
                if n.endswith(".kif") and n not in names and os.path.realpath(full) != self.key(uri):
                    names.add(n)
                    text = self.docs.get(self._uri_of_key(os.path.realpath(full)))
                    if text is None:
                        try:
                            with open(full, encoding="utf-8") as f:
                                text = f.read()
                        except OSError:
                            continue
                    out.append((n, n[:-4], text))
        return out

    def req_completionItem_resolve(self, p: dict) -> dict:
        uri = (p.get("data") or {}).get("u")
        return resolve_item(self.module(uri), p) if uri in self.docs else p

    def req_textDocument_signatureHelp(self, p: dict) -> Optional[dict]:
        uri, pos = p["textDocument"]["uri"], lsp_pos(p["position"])
        return signature_help(self.module(uri), self.docs[uri], pos.line, pos.col, lambda t: analyze(self.key(uri), t, self.resolve))

    # ---- formatting and fixes ----------------------------------------------------------------------------

    @staticmethod
    def _whole(text: str, new: str) -> List[dict]:
        if new == text:
            return []
        lines = text.split("\n")
        return [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": len(lines) - 1, "character": len(lines[-1])}},
                 "newText": new}]

    def req_textDocument_formatting(self, p: dict) -> List[dict]:
        text = self.docs[p["textDocument"]["uri"]]
        try:
            return self._whole(text, format_text(text, (p.get("options") or {}).get("tabSize", 4)))
        except FormatError as e:
            raise ValueError(str(e))

    def req_textDocument_codeAction(self, p: dict) -> List[dict]:
        uri = p["textDocument"]["uri"]
        text = self.docs[uri]
        want = p["range"]
        rng = Range(lsp_pos(want["start"]), lsp_pos(want["end"]))
        only = (p.get("context") or {}).get("only")
        mod, diags = quickfix.check(self.key(uri), text, self.config_for(uri) if self.lint_enabled else Config(), self.resolve,
                                 baseline=self.baseline_for(uri) if self.lint_enabled else None)
        if not self.lint_enabled:
            diags = [d for d in diags if not d.code.startswith("KIFL")]
        ctx_diags = {(d.code, d.range) for d in (from_diag(j) for j in (p.get("context") or {}).get("diagnostics", []))}
        actions: List[dict] = []

        def wanted(kind: str) -> bool:
            return not only or any(kind == o or kind.startswith(o + ".") for o in only)

        if wanted("quickfix"):
            for d in diags:
                if not _intersects(d.range, rng) or (ctx_diags and (d.code, d.range) not in ctx_diags):
                    continue
                for f in quickfix.fixes_for(mod, d, diags):
                    actions.append({"title": f.title, "kind": "quickfix", "diagnostics": [to_diag(d)], "isPreferred": f.preferred,
                                    "edit": {"changes": {uri: [_edit_json(e) for e in f.edits]}}})
        if wanted("source.fixAll.kif"):
            new, applied = quickfix.fix_all(self.key(uri), text, self.config_for(uri), self.resolve)
            if applied:
                actions.append({"title": f"Apply {len(applied)} automatic kif fix(es)", "kind": "source.fixAll.kif",
                                "edit": {"changes": {uri: self._whole(text, new)}}})
        return actions


# ---------------------------------------------------------------- stdio transport


def read_message(stream) -> Optional[dict]:
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    if length is None:
        return None
    return json.loads(stream.read(length).decode("utf-8"))


def write_message(stream, msg: dict) -> None:
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stream.flush()


def main() -> int:
    code = serve()
    sys.stdout.flush()
    os._exit(code)  # the reader thread may still be blocked on stdin; a normal exit would abort while it holds the lock


def serve() -> int:
    """Reads on a thread so the loop can wake for `Server.tick()` (the inlay hint refresh) while no message arrives."""
    server = Server()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    inbox: "queue.Queue[Optional[dict]]" = queue.Queue()

    def reader() -> None:
        while True:
            try:
                msg = read_message(stdin)
            except (ValueError, OSError):
                msg = None
            inbox.put(msg)
            if msg is None:
                return

    threading.Thread(target=reader, daemon=True).start()
    while server.exit_code is None:
        try:
            msg = inbox.get(timeout=server.wait_time())
        except queue.Empty:
            for r in server.tick():
                write_message(stdout, r)
            continue
        if msg is None:
            return 1
        try:
            replies = server.handle(msg)
        except Exception as e:  # keep the server alive on a bad request
            replies = [Server.error(msg["id"], -32603, f"internal error: {e}")] if "id" in msg else []
        for r in replies:
            write_message(stdout, r)
        for r in server.tick():
            write_message(stdout, r)
    return server.exit_code


if __name__ == "__main__":
    sys.exit(main())
