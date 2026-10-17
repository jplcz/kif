// SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
//
// SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

// Starts the kif language server (Python, standard library only) and connects it to VS Code.
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;
let clientReady;

// The packaged extension carries the server in ./server; a checkout of the repository uses kif/ two levels up.
function findServerRoot(extensionPath) {
  // A development install is a symlink into ~/.vscode/extensions, so resolve it before walking up to kif/.
  const real = fs.realpathSync(extensionPath);
  const candidates = [path.join(real, "server"), path.resolve(real, "..", "..")];
  return candidates.find((dir) => fs.existsSync(path.join(dir, "kiflang", "lsp.py")));
}

function activate(context) {
  const cfg = vscode.workspace.getConfiguration("kif.server");
  if (!cfg.get("enabled", true)) {
    return;
  }
  const root = findServerRoot(context.extensionPath);
  if (!root) {
    vscode.window.showWarningMessage(
      `kif: language server files not found (looked from ${context.extensionPath}); only syntax highlighting is active.`
    );
    return;
  }
  const run = {
    command: cfg.get("python", "python3"),
    args: ["-m", "kiflang.lsp"],
    options: { cwd: root },
    transport: TransportKind.stdio,
  };
  client = new LanguageClient(
    "kif",
    "kif language server",
    { run, debug: run },
    {
      documentSelector: [{ scheme: "file", language: "kif" }, { scheme: "untitled", language: "kif" }],
      initializationOptions: {
        includeDirs: cfg.get("includeDirs", []),
        lint: cfg.get("lint", true),
        lintConfig: cfg.get("lintConfig", ""),
        baselines: cfg.get("baselines", []),
        inlayHints: { checks: cfg.get("inlayHints.checks", "kept"), delayMs: cfg.get("inlayHints.delayMs", 400) },
        staticSnippets: true, // snippets/kif.code-snippets already offers the keyword snippets; the server adds only construction patterns
      },
    }
  );
  clientReady = client.start();
  registerImporter(context);
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider("kif-c-preview", {
      onDidChange: cPreviewChanged.event,
      provideTextDocumentContent: (uri) => cPreview.get(uri.toString()) ?? "",
    })
  );
  // Picks up edits to the server's Python sources (kiflang/) without reloading the window.
  context.subscriptions.push(
    vscode.commands.registerCommand("kif.restartServer", async () => {
      await client.restart();
      vscode.window.showInformationMessage("kif: language server restarted.");
    })
  );
}

// ---- FreeBSD .m import ----------------------------------------------------------------------------------

// Asks the server to convert .m text; the server must be running, so wait for the client first.
async function importM(doc) {
  await clientReady;
  return client.sendRequest("kif/importM", { text: doc.getText(), name: path.basename(doc.fileName) });
}

function registerImporter(context) {
  const emitter = new vscode.EventEmitter();
  const cache = new Map(); // preview uri -> last converted text

  const provider = {
    onDidChange: emitter.event,
    provideTextDocumentContent(uri) {
      return cache.get(uri.toString()) ?? "";
    },
  };
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider("kif-preview", provider));

  const previewUri = (doc) =>
    vscode.Uri.from({ scheme: "kif-preview", path: doc.uri.path.replace(/\.m$/, ".kif"), query: doc.uri.toString() });

  async function refresh(doc) {
    const uri = previewUri(doc);
    try {
      const res = await importM(doc);
      cache.set(uri.toString(), res.kif);
    } catch (e) {
      cache.set(uri.toString(), `// import failed: ${e.message ?? e}\n`);
    }
    emitter.fire(uri);
  }

  const activeM = () => {
    const doc = vscode.window.activeTextEditor?.document;
    return doc && doc.fileName.endsWith(".m") ? doc : undefined;
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("kif.previewFromM", async (resource) => {
      const doc = resource ? await vscode.workspace.openTextDocument(resource) : activeM();
      if (!doc) {
        vscode.window.showInformationMessage("kif: open a FreeBSD .m file first.");
        return;
      }
      await refresh(doc);
      const view = await vscode.workspace.openTextDocument(previewUri(doc));
      await vscode.languages.setTextDocumentLanguage(view, "kif");
      await vscode.window.showTextDocument(view, { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true, preview: true });
    }),
    vscode.commands.registerCommand("kif.convertMToKif", async (resource) => {
      const doc = resource ? await vscode.workspace.openTextDocument(resource) : activeM();
      if (!doc) {
        vscode.window.showInformationMessage("kif: open a FreeBSD .m file first.");
        return;
      }
      const res = await importM(doc);
      const target = vscode.Uri.file(doc.fileName.replace(/\.m$/, ".kif"));
      await vscode.workspace.fs.writeFile(target, Buffer.from(res.kif, "utf8"));
      await vscode.window.showTextDocument(target, { viewColumn: vscode.ViewColumn.Beside });
      vscode.window.showInformationMessage(`kif: wrote ${path.basename(target.fsPath)} (${res.warnings.length} note(s)).`);
    }),
    vscode.commands.registerCommand("kif.previewC", () => previewC(context))
,
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (e.document.fileName.endsWith(".m") && cache.has(previewUri(e.document).toString())) {
        refresh(e.document);
      }
    })
  );
}

// ---- generated C preview ----------------------------------------------------------------------------------

const cPreview = new Map(); // preview uri -> generated text
const cPreviewChanged = new vscode.EventEmitter();

// Runs the C backend on the open .kif text (server request kif/previewC) and shows every generated file in one C document.
async function previewC(context) {
  const doc = vscode.window.activeTextEditor?.document;
  if (!doc || doc.languageId !== "kif") {
    vscode.window.showInformationMessage("kif: open a .kif file first.");
    return;
  }
  await clientReady;
  const target = vscode.workspace.getConfiguration("kif.preview").get("target", "c");
  const res = await client.sendRequest("kif/previewC", { uri: doc.uri.toString(), target });
  const text = res.ok
    ? res.files.map((f) => `/* ==== ${f.name} ==== */\n${f.text}`).join("\n")
    : `/* kif: the C backend failed (target ${target}) */\n${res.messages.split("\n").map((l) => `// ${l}`).join("\n")}\n`;
  const uri = vscode.Uri.from({ scheme: "kif-c-preview", path: doc.uri.path.replace(/\.kif$/, ".c") });
  cPreview.set(uri.toString(), text);
  cPreviewChanged.fire(uri);
  const view = await vscode.workspace.openTextDocument(uri);
  await vscode.languages.setTextDocumentLanguage(view, "c");
  await vscode.window.showTextDocument(view, { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true, preview: true });
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
