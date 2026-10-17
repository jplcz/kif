# kif-editor

Editor support for `.kif` files (language: [`docs/kif-proc-language.md`](../docs/kif-proc-language.md)).
`header(c) { }`, `code(cxx) { }`, `m_prolog { }` and `m_epilog { }` raw blocks are highlighted as embedded C (no tag or `c`)
or C++ (`cxx`) where the editor supports embedding; the embedded highlighting is deliberately whatever the editor's stock mode gives.

| Directory | Editor | Contents |
| --- | --- | --- |
| `samples/` | all | `example.kif` and `device.kif`, files that exercise every construct; open them to check highlighting (a test keeps them error-free and `fmt`-clean) |
| `vscode/` | VS Code, **JetBrains IDEs** | TextMate grammar, language configuration, snippets, and `extension.js`, the language-server client (VS Code only) |
| `vim/` | Vim, Neovim | `syntax/`, `ftdetect/`, `ftplugin/` |
| `sublime/` | Sublime Text | `kif.sublime-syntax`, comment preferences |
| `emacs/` | Emacs | `kif-mode.el` (font-lock, `//` comments, brace indentation) |
| `tree-sitter/` | Neovim, Helix, Zed | `grammar.js` (lexical grammar), external scanner for raw blocks, `highlights.scm`, `injections.scm` (C or C++ by tag) |

## Install

**VS Code**: build and install the packaged extension (it bundles the language server):

```
scripts/install-vscode.sh                    # add --profile <name> for a non-default profile, --out DIR to keep the .vsix
```

The script (also the VS Code task *kif: install editor support into VS Code*) builds in a temporary staging directory, so the
checkout gets no `server/`, `node_modules` or `.vsix`; it copies `kiflang/` into the package, runs `vsce package` and
`code --install-extension --force` (`--code codium` / `code-insiders` for other builds, `--no-install` with `--out` to only build).

Then run *Developer: Reload Window* (restart VS Code if the extension is not listed). Or install a released
`.vsix` via *Extensions > ... > Install from VSIX*. Notes:

- Extensions are installed per profile. If the workspace uses a non-default profile, install with
  `--profile <name>` (check *Settings Profile* in the gear menu), otherwise the extension is not visible there.
- Do not symlink the folder into `~/.vscode/extensions`: VS Code did not pick it up, and its stale entry blocked
  later installs ("Please restart VS Code before reinstalling").
- To develop the extension itself, run `code --extensionDevelopmentPath=<path to>/kif-editor/vscode`
  (the extension then finds the server in the checkout).
- Word lists in every highlighter are generated from the compiler: keywords from `kiflang/lexer.py` (`KEYWORDS`,
  `CONTEXTUAL_WORDS`, `VERBATIM_WORDS`), scalar types from `kiflang/symbols.py` (`SCALARS`) and attribute names from
  `kiflang/lsp.py` (`ATTRIBUTES`). After changing them run `python3 kif-editor/gen_syntax.py` (rewrites the
  `GEN-BEGIN`/`GEN-END` regions of the vim, emacs, sublime and tree-sitter files and the whole TextMate grammar);
  `--check` fails if anything is stale (`tests/test_editor_syntax.py` runs it). Never edit inside a GEN region.
- Snippets (prefix): `kobj`, `interface`, `method`, `methoddef`, `static`, `before`, `after`, `m_prolog`, `m_epilog`,
  `fn`, `pfn`, `gen`, `extern`, `struct`, `enum`, `flags`, `impl`, `const`, `type`, `namespace`, `import`, `use`,
  `headerc`, `codec`, `headercxx`, `codecxx`, `if`, `while`, `for`, `match`, `letm`. A test parses every one.

**JetBrains IDEs** | TextMate grammar, language configuration, snippets, and `extension.js`, the language-server client (VS Code only) |
| `vim/` | Vim, Neovim | `syntax/`, `ftdetect/`, `ftplugin/` |
| `sublime/` | Sublime Text | `kif.sublime-syntax`, comment preferences |
| `emacs/` | Emacs | `kif-mode.el` (font-lock, `//` comments, brace indentation) |
| `tree-sitter/` | Neovim, Helix, Zed | `grammar.js` (lexical grammar), external scanner for raw blocks, `highlights.scm`, `injections.scm` (C or C++ by tag) |

## Install

**VS Code**: build and install the packaged extension (it bundles the language server):

```
scripts/install-vscode.sh                    # add --profile <name> for a non-default profile, --out DIR to keep the .vsix
```

The script (also the VS Code task *kif: install editor support into VS Code*) builds in a temporary staging directory, so the
checkout gets no `server/`, `node_modules` or `.vsix`; it copies `kiflang/` into the package, runs `vsce package` and
`code --install-extension --force` (`--code codium` / `code-insiders` for other builds, `--no-install` with `--out` to only build).

Then run *Developer: Reload Window* (restart VS Code if the extension is not listed). Or install a released
`.vsix` via *Extensions > ... > Install from VSIX*. Notes:

- Extensions are installed per profile. If the workspace uses a non-default profile, install with
  `--profile <name>` (check *Settings Profile* in the gear menu), otherwise the extension is not visible there.
- Do not symlink the folder into `~/.vscode/extensions`: VS Code did not pick it up, and its stale entry blocked
  later installs ("Please restart VS Code before reinstalling").
- To develop the extension itself, run `code --extensionDevelopmentPath=<path to>/kif-editor/vscode`
  (the extension then finds the server in the checkout).
- Keyword lists in every highlighter come from `kiflang/ast.py` (`KEYWORD_GROUPS`). After changing them run
  `python3 kif-editor/gen_syntax.py` (rewrites the vim, emacs, vscode, sublime and tree-sitter lists);
  `--check` fails if any is stale (a unit test runs it). `positive`/`negative` are contextual and only highlighted
  by tree-sitter.
- Snippets: `interface`, `interfacecxx`, `interfaceself`, `method`, `methoddefault`, `staticmethod`, `attr`, `deprecated`, `block`, `register`, `regfield`, `foreign`, `enum`, `struct`, `errno`,
  `header`, `code`, `import`, `include`, `namespace`.

**JetBrains IDEs** (CLion, IntelliJ, ...): *Settings > Editor > TextMate Bundles > +* and choose the `vscode/`
directory (a VS Code extension folder is a valid TextMate bundle).

**Vim / Neovim**: add the directory to the runtime path, e.g. `set rtp+=/path/to/kif-editor/vim`, or copy
`vim/*` into `~/.vim` (Neovim: `~/.config/nvim`).

**Sublime Text**: copy the files of `sublime/` into `Packages/User/kif/`.

**Emacs**: `(add-to-list 'load-path "/path/to/kif-editor/emacs") (require 'kif-mode)`; `.kif` files then open in
`kif-mode`. Indentation width: `kif-indent-offset` (default 4, matching `kif fmt`).

**Tree-sitter**: in `tree-sitter/` run `npx tree-sitter generate` (needs `tree-sitter-cli`) to produce `src/parser.c`
(not checked in), then register the directory as a parser in your editor (Neovim: `nvim-treesitter` custom parser
with `files = {"src/parser.c", "src/scanner.c"}` and copy `queries/` into `queries/kif/`; Helix/Zed: add it as a
language grammar). Check with `npx tree-sitter parse ../samples/example.kif`.

## Language server (diagnostics, hover, navigation, rename, code actions and quick fixes, formatting)

The server is [`kiflang.lsp`](../kiflang/lsp.py) (see `docs/kif-tools.md`; Python 3, standard library only, no install step). The VS Code
extension starts it itself (setting `kif.server.python`, default `python3`; `kif.server.includeDirs` adds import
search directories; `kif.server.enabled` turns it off; `kif.server.inlayHints.checks` = `off`/`kept`/`all` and `.delayMs` control the run-time check hints, see `docs/kif-tools.md`). The packaged `.vsix` carries a copy of the server in
`server/`; with `--extensionDevelopmentPath` it uses the one in the checkout. Besides the checker's errors and the lint rules, it shows
warnings from the IR check passes (`KIFI010`: an index that provably fails, e.g. `a[7]` on `[u8; 4]`; docs/kif-ir-passes.md). The
same run-time checks are listed on the command line by `kif ir FILE --explain-checks`.

The extension also sets per-language defaults for `[kif]` (quick suggestions inside strings so `import "` completes, semantic
highlighting, inlay hints and parameter hints on, word-based suggestions off), and the command **kif: Restart language server**
reloads the server after you edit `kiflang/`. Because the extension already ships keyword snippets, it passes
`staticSnippets: true`, so the server offers only the context-aware ones (construction patterns, templates) and nothing is listed twice.
To try it all: run the launch configuration "kif: extension development host" (F5), open `samples/device.kif`, and check completion
(`Ctrl+Space`), signature help (`(`), inlay hints, `Ctrl+T` (workspace symbols) and rename (`F2`).

Other LSP clients: run `cd /path/to/kif && python3 -m kiflang.lsp` over stdio, file type `kif`, and pass
`initializationOptions = { includeDirs = {...} }` if you need import directories; add `staticSnippets = true` if the editor
ships its own keyword snippets.

```lua
-- Neovim (0.11+): .kif files need filetype 'kif' (the vim/ directory above provides it)
vim.lsp.config("kif", { cmd = { "python3", "-m", "kiflang.lsp" }, filetypes = { "kif" }, root_markers = { ".git" } })
vim.lsp.enable("kif")
```

```elisp
;; Emacs (eglot)
(add-to-list 'eglot-server-programs '(kif-mode . ("python3" "-m" "kiflang.lsp")))
```

Helix (`languages.toml`): `[language-server.kif]` with `command = "python3"`, `args = ["-m", "kiflang.lsp"]`, then
`[[language]] name = "kif"`, `file-types = ["kif"]`, `language-servers = ["kif"]`, `comment-token = "#"`.
JetBrains: install the LSP4IJ plugin and add a user-defined server with the same command and the `*.kif` pattern.
Sublime Text: the LSP package with a client entry using the same command and selector `source.kif`.

## FreeBSD `.m` preview and conversion

With the extension installed, open a FreeBSD `*_if.m` file:

- Editor title button / context menu **kif: Preview as .kif (from FreeBSD .m)** opens a read-only, live-updating
  `.kif` view beside the `.m` file.
- Context menu / explorer **kif: Convert FreeBSD .m to .kif file** writes `<name>.kif` next to the `.m` file.

Both use the language server request `kif/importM`; rules are in `../kiflang/README.md`.

## Generated C preview

With a `.kif` file active, the title button, editor context menu or explorer entry **kif: Preview generated C (kifc backend)**
runs the C backend on the text in the editor (saved or not) and shows every generated file (`<stem>_proc.h`, `<stem>_proc.c`,
and the kobj files when the target has a kobj section) in one read-only C document beside it, each under a
`/* ==== name ==== */` banner. When the file does not compile, the compiler's messages are shown as comments instead.
The setting `kif.preview.target` picks the target (default `c`; e.g. `freebsd-kernel`). It uses the server request
`kif/previewC` (`{uri, target}` gives `{ok, files: [{name, text}], messages}`); run the command again to refresh.

## Verification status

Tested (`tests/test_editor_syntax.py`, `tests/test_lsp.py`): the generated lists are current, the TextMate JSON is valid and
every regex compiles, the samples analyze without errors and are `fmt`-stable, every snippet parses, the language server
over stdio. Tree-sitter: generated with `tree-sitter-cli` 0.22.6, both samples parse with no `ERROR` nodes, and the
highlight and injection queries load and match (header/code bodies inject C or C++ by tag).
Not tested: the Vim, Emacs and Sublime Text files in a running editor (Vim/Emacs/Sublime were not available here),
and the editor integrations themselves (extension loading, JetBrains import).

## Keeping in sync

When the language changes, update the compiler's word lists (above) and rerun `gen_syntax.py`. The hand-written parts
(declaration-name rules, raw-block regions, operators, the tree-sitter grammar and the snippets) need a manual look
when new syntax is added.
