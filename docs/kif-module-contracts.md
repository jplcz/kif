<!--
SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF

SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
-->

# Safety contracts and security boundaries in `.kifm` modules

Status: **design**; stages 1 (the `security` and `compiler` sections, `security_digest`, the cross-check), 2 (effective taint read by the checks), 3 (KIFI040, KIFI041, KIF016, KIF017, KIFT012), 4 (`inspect`, audit labels, hover origin) 5 (signing, verification, import policy, project config, CMake) 6a (`kif ir --hash`) and 6b (the C comment) and 6c (`.kifa`, `verify --source`, `attest.section`) are implemented
(`kiflang/modsec.py`, `kiflang/prover_id.py`, `kiflang/irhash.py`, `tests/test_modsec.py`, `tests/test_irhash.py`); the rest is not. Related: `docs/kif-proc-language.md` section 15 (the `.kifm` format),
`docs/kif-taint.md` (the taint feature), `docs/kif-ir.md` (the IR file).

## 1. Requirement

A `.kifm` is what a vendor ships instead of source. The importer's compiler has no bodies, so it can only reason from what the
file *says*. The module writer therefore has to write down **every safety contract and every security-boundary fact** the
importer's checks depend on, and the module reader has to give them to the checker as first-class data. Three properties:

1. **Complete.** Any check that gives a result for a source import gives the *same* result for the `.kifm` of that source
   (same diagnostics, same refusals, same proved/kept checks at the call site). A test that analyzes a program against the source
   module and against its `.kifm` and compares the results is the acceptance test of this whole feature.
2. **Explicit.** A fact is never recovered by guessing from a missing body. If the importer needs it, it is in the file as data.
3. **Tamper-evident.** The recorded facts are bound to the text, so editing a fact (making a sanitizer look trusted, dropping a
   `#[tainted]`) without the vendor's build is detected (KIF014), exactly as a modified `text` already is.

*Why.* A security boundary that exists only in the vendor's source is invisible to the customer's build. If the customer's compiler
cannot see that `read_packet()` returns tainted data, or that `clamp()` is a trusted sanitizer, it either misses a bug (the
dangerous direction) or has to reject every call (unusable). Certification needs the contract the customer's code was checked
against to be the one the vendor's code was checked for.

## 2. What is already preserved, and what is not

`text` keeps every declaration with its attributes; only ordinary function bodies become `;`. So the *spelled* contracts survive:
`#[tainted]`, `#[tainted_result]`, `#[untaint]`, `#[trusted]`, `#[some]`, `#[non_null]`, `#[non_empty]`, `#[len_ge(N)]`,
`#[lt_len(v)]`, `#[ensures(..)]`, `#[preserves]`, `#[unsafe]`, `#[require_proved]`, lifetime and ownership annotations, `cfg`.

What is **not** recoverable from `text` alone:

| Fact | Why the importer cannot get it from the text |
| --- | --- |
| The *effective* taint of a parameter under the vendor's `taint.exports: "all"` policy | the policy is the vendor target's data, not an attribute: an `#[export]` function has tainted parameters that no `#[tainted]` spells |
| The vendor's `taint.sinks` | decides which operations on tainted data the vendor body was checked against (`mul` is opt-in) |
| That an `#[untaint]` body **was checked** (pure, KIFT009) or is `#[untaint(trusted)]` | the body is stripped; only the attribute remains, with no evidence behind it |
| The speculation barriers the vendor's code contains | implicit barriers are inserted by a pass, never written |
| Which facts the vendor's body **proved** versus assumed | a `requires` fact is proved at call sites, but the importer cannot tell whether the callee body relies on a guard or on the caller |
| Which checks the compiled body kept | `#[require_proved]` modules promise none are kept; the promise lives in the body's analysis |
| The target and profile the module was compiled for | decides the ABI, the barrier snippets and the overflow behaviour the importer links against |
| Struct and enum layout facts a checker may use (field taint through `#[tainted]` fields is spelled; the layout is not) | nothing in text, and the importer must not guess |

Each of these becomes a field of the new `security` section (section 3).

## 3. The `security` section

The header map gains a **required** `security` key. This is part of format version 1 itself, which is not released yet: there is no legacy to read, so there is no
migration, no fallback and no "old file" branch anywhere. Its shape, as the writer emits it and the reader validates it:

```
security = {
  "target":   "freebsd-kernel",                      # the target the vendor compiled for
  "profile":  "release",
  "taint":    { "exports": "all", "sinks": ["div", "rem", "shift"] },   # the policy in force; absent keys are the defaults
  "checked":  true,                                  # the vendor's analysis finished without errors (always true: export refuses otherwise)
  "functions": {                                     # one entry per function and method with a signature in `text`
    "read_packet": {
      "export": true,
      "tainted_params": ["buf", "len"],              # the EFFECTIVE set: attributes plus the exports policy, after #[trusted]
      "tainted_result": false,
      "untaint": null,                               # null, "checked" (pure body, KIFT009 passed) or "trusted" (body not checked)
      "trusted": false,                              # #[trusted] on an #[export] function
      "contracts": ["len_ge(4)@buf", "lt_len(buf)@i", "ensures(non_null)", "preserves(out)"],
      "unsafe": false,
      "secure_wipe": false,                          # the function carries #[secure_wipe] (derived; checked against the declaration)
      "wipes": 0,                                    # informational: wipe sites in the compiled body
      "stack": 48,                                   # informational: the estimated frame in bytes (null: cannot be sized), docs/kif-stack.md
      "stack_depth": 412,                            # informational: frame plus the deepest callee chain (null: unbounded or unknown)
      "stack_exact": true,                           # false: a lower bound, some callee has no known frame
      "barriers": { "explicit": 1, "implicit": 2 },  # counts in the compiled body; the sites stay in the vendor's report
      "kept_checks": ["index"],                      # check kinds the compiled body still performs; [] means fully proved
      "proved": ["div", "shift"],                    # check kinds the vendor's prover discharged
      "ir_hash": "sha256:5e1a...",                   # the IR attestation hash of the compiled body (section 13)
    },
    "Ring::push": { ... }                            # methods are keyed `Type::name`
  },
  "types": {                                         # taint and secure-wipe facts carried by type declarations
    "packet": { "tainted": true, "tainted_fields": ["payload"], "secure_wipe": false, "wipe_exempt": [] }
  }
}
```

The header also gains a required top-level `compiler` key, **outside** `security` and bound by the signature:

```
compiler = {
  "name":            "kif",
  "version":         "1.4.0",                # the compiler release that wrote the file
  "prover":          "sha256:9c1f...",       # the identity of the check machinery: a hash over the version of the prover and of every
                                             # pass that decides `proved` / `kept_checks` (kiflang/prover_id.py), bumped by any change
                                             # of what those passes can prove
  "security_schema": 1,                      # the layout of `security` above
}
```

*Why.* `proved` and `kept_checks` are claims made by the vendor's prover. An importer that relies on them (`#[require_proved]`,
section 5) is relying on that prover, and two compilers can disagree on what is provable. Recording the prover identity makes the
dependence visible and checkable instead of implicit.

Field rules:

* `tainted_params` is **effective**: it already includes the exports policy and already excludes `#[trusted]` functions. The
  importer never re-applies a policy to a precompiled function; it uses this list. (Reason: the importer's own target may have
  a different `taint.exports`, and the callee was compiled under the vendor's.)
* `untaint` records the evidence for the sanitizer guarantee. `"checked"` means the vendor's compiler verified the body pure
  and branch-free; `"trusted"` means the vendor declared it `#[untaint(trusted)]` and nothing was verified. The importer's
  checks treat both as sanitizers (that is the declared contract) but the audit report prints which one, so a reviewer sees that
  a `.kifm` sanitizer rests on the vendor's word.
* `contracts` is the canonical text of every fact attribute with the parameter it applies to, in a fixed order (sorted by
  parameter position, then attribute name), so two builds of the same source give the same bytes.
* `barriers`, `kept_checks`, `proved` are **informational for the importer's checks** but binding for `#[require_proved]`
  (section 5) and for the audit report.
* Entries exist only for functions with a signature in `text`. A generic function, a generic impl method, an interface default
  method, a hook, a `gen fn` and a derive stay embedded in full source (section 15.1), so the importer analyzes their bodies
  itself and the entry would be redundant; for those the writer records `"embedded": true` and nothing else.

Determinism: keys are sorted, lists have a defined order, there are no timestamps (the format is canonical CBOR already).

## 4. Binding the section to the text (tamper evidence)

Two mechanisms, both required:

1. **`security_digest`** = sha256 of the canonical CBOR encoding of `security`, stored next to `digest`. The reader recomputes it; a
   mismatch is KIF014 (`security section does not match its digest`). This catches edits of the section alone.
2. **Cross-check against the text.** Everything in `security` that *can* be derived from the declarations in `text` (the spelled
   attributes, `export`, `unsafe`, `contracts`, the attribute part of `tainted_params`/`untaint`/`trusted`) is recomputed by the
   reader from the parsed `text` and compared. A difference is KIF014. This catches an edit of both `security` and its digest
   that is inconsistent with the declarations, e.g. flipping a sanitizer to `trusted` without changing the attribute.

What neither catches: a vendor who edits `text` *and* the section *and* both digests. That is a vendor signing a different
module, which is the vendor's authority to do (the object code and the `.kifm` ship together, and a signature over the `.kifm`,
if the release process has one, covers all of it). `digest` (text only) stays the identity that `deps` records, so a source
module and its `.kifm` keep the same `digest_of` and importing either satisfies a dependent's `deps` entry. The `security` section is
not part of that identity, because the source module has no such section.

## 5. How the importer's checks use it

The reader attaches the section to the loaded module (`Module.security`) and, after parsing `text`, **annotates the declaration
nodes** with the effective facts, so no check needs to know whether a declaration came from source or from a `.kifm`:

| Check | Reads | From a `.kifm` it reads |
| --- | --- | --- |
| Taint, calls (KIFT003, KIFT008, rule 7) | the callee's `#[tainted]` parameters | `tainted_params` (effective, includes the vendor's exports policy) |
| Taint, results (rule 4) | `#[tainted_result]` | `tainted_result` |
| Sanitizer call (`untaint!`, KIFT010) | `#[untaint]` on the callee | `untaint` is non-null |
| Struct taint (rule 3) | `#[tainted]` types and fields | `types` |
| Fact preconditions (KIFP21x, `require_proved`) | the fact attributes | `contracts` (also spelled in `text`) |
| `#[require_proved]` in the importer | the callee's checks | a callee whose `kept_checks` is non-empty while the importer demands a proof of those kinds is **KIFI040** at the call, naming the vendor function: *the vendor kept a check the importer's policy forbids*. A callee whose `kept_checks` is `"unknown"` is the same error |
| `#[require_proved]` and the vendor's prover | `compiler.prover` | the claims `proved` and `kept_checks` are only as good as the vendor's prover. When the importer's own prover identity differs, the call is **KIFI041**: a *warning* by default (the importer's compiler cannot re-prove a body it does not have, so it is told the claim rests on another prover), an *error* with `[signing] prover = "same"` in the project config (below, 11.3) or `--prover-must-match`. The identity is signed, so a vendor cannot claim another compiler's identity without the signing key |
| Barrier and sink policy | importer's own target | unchanged: the importer's `taint.sinks` applies to the importer's code only |
| Audit report (`--taint-report`) | sanitizers, trusted functions, barriers | the section, labelled `(from foo.kifm, vendor-checked)` or `(from foo.kifm, vendor-trusted)` |
| `kif ir` / LSP hover | attribute meanings | the effective facts, with the module they came from |

Two target-compatibility checks run at import time, and each is an error, never a silent assumption:

* **Target.** `security.target`/`profile` against the importer's target. A different *language* or ABI is KIF016 (the vendor's
  object code cannot be linked). A different target of the same ABI is a warning (KIF017: the barrier snippets and the overflow
  mode inside the vendor's object code are the vendor's, not the importer's).
* **Policy.** The vendor's `taint.sinks`/`exports` are *recorded*, not enforced on the importer. If the importer's policy is
  **stricter** (it lists `mul`, the vendor did not), the vendor body was not checked for it: a `note` (KIFT012) at the import
  says so and the audit report lists it, so it is a conscious acceptance, not a hidden gap.

## 6. What the writer needs

`modfile.export(mod)` runs, on the already-analyzed source module:

1. the effective-taint computation (`taint.policy` of the module's cfg environment, the same code the checker uses);
2. the IR pipeline `default` over each non-generic function (`passes.run_module`), for `barriers`, `kept_checks`, `proved` and the
   `untaint` evidence;
3. canonical encoding and the two digests.

`export` refuses (`ModuleFileError`, exit status 1 from `python -m kiflang export`) to write a module whose analysis has an error,
as today, and additionally when a function the IR builder cannot handle (`ir.Unsupported`) would need a `kept_checks` entry: the
entry then says `"unknown"` and `#[require_proved]` importers reject it, rather than the writer guessing "none".

The CLI gains `python -m kiflang export FILE -o OUT.kifm --target T --profile P` (the target and profile are *inputs* to the
section, as they are to `check`) and `python -m kiflang inspect OUT.kifm` printing the section for a human: the vendor's view of
what a release exposes, next to the existing `embedded` list. Rationale: the vendor must be able to review the contract before
shipping it, just as they review `embedded` to keep secrets out of generic bodies.

## 7. Compatibility rules for the importer

* An unknown key inside `security` is ignored (forward compatibility inside one version); a missing required key is KIF014.
* A `.kifm` without `security` is malformed (KIF014, like a missing `text`); there is no "legacy file" interpretation. This also keeps the
  safe direction: a file that says nothing about its boundary is never read as "nothing is tainted".
* The reader never runs the vendor's analysis: it trusts the section only as far as sections 4 and 5 allow, and says so in the
  audit report.

## 8. Tests that define done

1. **Parity.** For each program in a corpus (the `tests/c` taint case, the sanitizer and exports examples of `kif-taint.md`, a fact-attribute
   example), diagnostics and `--taint-report` are identical (modulo the module label) whether the library is imported as source or
   as its `.kifm`.
2. **Effective taint.** A vendor module built with `taint.exports: "all"` accepts tainted arguments into its exports and
   rejects clean-by-assumption handling of its results in an importer whose own policy is `declared`.
3. **Tamper.** Each of: flipped `untaint`, dropped `tainted_params` entry, edited `contracts`, mismatched `security_digest`, or a missing `security` section gives KIF014.
4. **`require_proved`.** A vendor function with a kept index check is KIFI040 at an importer call that demands `index`; a module built by a compiler with a different prover identity is KIFI041 (warning by default, error under `prover = "same"`).
5. **Determinism.** Two exports of the same module are byte-identical; exporting twice after a no-op edit of a body gives an identical
   `security` section.
6. **Round trip of the IR file.** `Function.taint_sinks`, `Local.taint`, `Call.taint`, `Call.reason` (already serialized) survive,
   as the `.kifm` section is built from the same data.

## 9. Staging

1. Section writer and reader, `security_digest`, the cross-check (tests 3 and 5).
2. Annotation of loaded declarations and the taint checks reading the effective facts (tests 1 and 2).
3. `kept_checks` / `proved`, the `compiler` key, KIFI040, KIFI041, target and policy compatibility (KIF016, KIF017, KIFT012) (test 4).
4. `inspect`, audit report labels, LSP hover origin, documentation in `kif-proc-language.md` section 15 and `kif-taint.md`.

Implementation notes (stage 5a/5b, signing primitives and commands): `kiflang/modsign.py` is the only crypto module and has no primitives:
back ends `cryptography` then the `openssl` tool (OpenSSL 3+), forced with `KIF_CRYPTO`; none present is `CryptoUnavailable` (exit 2, install
message). `export --sign KEY.pem [--cert CERT]`, `verify FILE.kifm [--trust PEM].. [--revoked PEM].. [--allow-revoked FP]` (exit 0 only when
trusted), and `inspect` shows the signature. A CRL counts only when a *pinned* certificate issued it (else ignored with a note); a certificate
is trusted when pinned or *directly* issued by a pinned CA certificate (`basicConstraints CA:TRUE`; no intermediates). Stage 5c: `kiflang/signpolicy.py` holds the consumer
policy (`--trust`, `--revoked`, `--allow-revoked`, `--signing off|warn|require`, `--prover any|same`, and `[signing]` in `kif.toml`, command line first).
`check`, `kifc` and `kif` build one (default `warn`) and attach it to the `Sysroot`; the library default (language server, programmatic `analyze`) is
no policy. It is evaluated once per `.kifm` when first imported; KIF018-KIF021 are attached to the file and propagated to each importer, prefixed with the
import path, so a source module is told `'b.kif': 'c.kif': it is not signed`. An idle policy (`off`, no lists) never looks for crypto. A new
code, KIF021, covers "cannot decide" (no crypto, unreadable list). `--prover same` / `prover = "same"` replace `--prover-must-match` for KIFI041.
CMake: `KIF_TRUST`, `KIF_REVOKED`, `KIF_SIGNING`, `KIF_PROVER` (build-wide, `kif.toml`, per target; key files are dependencies). Tests: `tests/test_modsign.py`
runs every back end present.

Implementation notes (stage 4): `python -m kiflang inspect`, the `Precompiled ...` block of `--taint-report`
(`taint_ir.vendor_lines`) and the hover origin (`lsp.py`). The server's own file resolver reads `.kif` only, so hover origin is
exercised through a sysroot-based analysis until the server gains one.

Implementation notes (stage 3): the checks are AST-level (`kiflang/vendor.py`, `modsec.compat`) because IR calls into imported modules are
unresolved. `annotate` stamps each non-embedded `.kifm` declaration with `vendor` (module, key, entry, compiler). `security.language`
(`c`/`cxx`) is recorded so KIF016 can tell a language mismatch from a target mismatch. `prover_must_match` is a cfg value set by
`python -m kiflang check --prover-must-match` (the `[signing] prover = "same"` setting arrives with stage 5). Tests: `tests/test_modsec.py`.

Implementation notes (stage 1):

* Function keys: `name` for functions and `extern` functions, `Type::name` for inherent methods, `<Type as Iface>::name` for interface impls (so two
  impls of one type cannot collide), `Iface::name` for interface methods.
* The reader re-derives `export`, `tainted_params`, `tainted_result`, `untaint`, `trusted`, `contracts`, `unsafe` and the `types` table from `text`,
  using the policy recorded in `security.taint` (so the effective set under `exports: "all"` is verified too). `barriers`, `kept_checks`, `proved`
  and `ir_hash` describe bodies the importer never sees: they are bound by `security_digest` (and the signature), not by the text.
* `kept_checks` counts operation checks and call requirements that remain after the default pipeline, plus a call whose callee's own guard
  covers it; an entry assumption (the callee's `requires`) is the caller's to prove and is not counted. Methods of `impl` blocks are built to IR like
  functions and keyed `Type::name` or `<Type as Iface>::name` (by declaration, so equal names on different types stay apart). Only what the IR
  builder skips (an unsupported construct, an interface default method, a generic or embedded function) records `"unknown"` and `ir_hash: null`.
* Stage 2: `modsec.annotate` runs when a module is loaded and sets `Param.effective_taint` on every parameter that takes tainted data. For a
  `.kifm` that is the vendor's recorded `tainted_params`; for source it is the attributes plus the module's own `taint.exports`. The taint
  checker's call rules (`accepts`, `written_by`) read it next to `#[tainted]`, so a call checks identically against source and a `.kifm`
  (`tests/test_modsec.py`, parity corpus). Imported source now loads under the importer's cfg environment, like the importing file.
* `compiler.prover` is `prover_id.prover_id()`: a sha256 over the source of the IR builder, the passes, the provers and the fact rules.

## 10. Decided questions

* `contracts` holds **declared facts only**. Facts the vendor's prover derived beyond the declared `ensures` are not recorded: they would
  be a promise the vendor's compiled code must keep forever, and a contract is something the author wrote down.
* A `.kifm` can be **signed** by the vendor and verified by the consumer (section 11).
* Crypto is a **host system** dependency (the `cryptography` package, else the `openssl` tool), never hand-rolled and never in `pyproject.toml`;
  the toolchain errors out when neither exists (11.4).
* Signing policy is **project** configuration (`kif.toml` `[signing]` and CMake `TRUST`/`REVOKED`/`SIGNING`), not target configuration (11.3).
* The policy applies to the **whole import tree**; unsigned and untrusted modules are warnings by default and errors under `require` (11.3).
* **Revocation** is supported offline: revoked keys, certificates and CRLs from pinned CAs (11.5).
* `#[require_proved]` across a `.kifm` boundary: a kept or unknown check is KIFI040, and the vendor's prover identity is baked into the signed
  `compiler` key, a mismatch being KIFI041 (warning by default, error with `prover = "same"`) (section 5).

## 11. Signing and verification

*Why.* Digests catch accidental damage and inconsistent edits (section 4); they do not say *who* produced the file. A consumer who
accepts a precompiled module is trusting its `security` section (section 5), so the file must be attributable to a vendor the
consumer chose to trust, and any change after signing must be detected.

### 11.1 What is signed

The signature covers the **canonical CBOR encoding of the header map without the `signature` key**: `magic`, `version`, `name`,
`text`, `digest`, `deps`, `stripped`, `embedded`, `compiler`, `security`, `security_digest`. Everything the importer's checks read is therefore
bound, including the section the digests alone could not defend against a vendor-side rewrite. The `signature` key is the last
and only unsigned part:

```
signature = {
  "alg":         "ed25519",              # the only algorithm in format 1: small, deterministic, no parameters to get wrong
  "signer":      "Acme Drivers Ltd",     # the subject name, from the certificate (informational; trust is decided by the key)
  "certificate": <DER bytes>,            # the vendor's X.509 certificate, so a consumer with only the CA / pinned key can verify (optional)
  "public_key":  <32 bytes>,             # the vendor's raw public key; always present, so a consumer who pinned the key needs nothing else
  "value":       <64 bytes>,             # the signature over the canonical encoding above
}
```

The public key is carried in the file as the requirement says, but **carrying it proves nothing by itself**: anyone can sign with a key
they generated and embed it. Trust comes only from the consumer's own trust list (11.3). The embedded key and certificate exist so
that verification needs no network and no separate key distribution, and so the consumer can *show* which key signed.

### 11.2 Vendor side

```
python -m kiflang export ddk.kif -o ddk.kifm --target freebsd-kernel --sign vendor.pem [--cert vendor.crt]
```

* `--sign FILE` is the vendor's **private key** (PKCS#8 PEM, Ed25519), read once and never written anywhere. A file that is
  not an Ed25519 private key is refused with a message naming the file.
* `--cert FILE` is the vendor's certificate (PEM or DER). Without it only `public_key` is embedded and `signer` is the key's
  fingerprint. With it the certificate's public key must equal the private key's, or export fails (a mismatched pair would
  produce a file no one can verify).
* The key is the vendor's release-process secret: the tool accepts it from a file only (no command-line value, so it is not in the shell
  history or process list); an HSM or `ssh-agent`-style signer is a later extension through the same `signature` shape.
* Signing is deterministic (Ed25519), so exporting the same module twice gives the same bytes, signature included.

### 11.3 Consumer side: trust is the consumer's decision

Signing is configured in the **project** (`kif.toml` and CMake), never in the target and never by the vendor: the consumer owns the
policy. The command line wins over `kif.toml`, which wins over the CMake defaults (the order of `docs/kif-project.md`).

```toml
[signing]                           # kif.toml                                                          (command line)
trust   = ["keys/vendors.pem"]      # pinned public keys and certificates, PEM, several allowed          (--trust)
revoked = ["keys/revoked.pem"]      # revoked keys / certificates / CRLs, see 11.5                       (--revoked)
mode    = "warn"                    # off | warn | require: what an unsigned or untrusted module is       (--signing MODE)
prover  = "any"                     # any | same: KIFI041 is a warning, or an error (section 5)         (--prover-must-match)
```

```cmake
kif_use_project()                                   # picks up [signing] from kif.toml
add_kif_library(drv SOURCES uart.kif BACKENDS c
                TRUST keys/vendors.pem REVOKED keys/revoked.pem SIGNING require)   # per target, overrides kif.toml
set(KIF_SIGNING require)                            # or per directory / -DKIF_SIGNING=require; KIF_TRUST, KIF_REVOKED likewise
```

```
python -m kiflang check main.kif --sysroot DDK --trust vendors.pem --signing require
python -m kiflang verify ddk.kifm --trust vendors.pem                     # verify one file, print who signed it
```

CMake passes them to every `kifc` and `kif check` it runs and lists the trust and revocation files as dependencies of the generated
files, so changing a key re-runs the checks.

A **trust list** is a PEM file with any number of public keys and certificates. A `.kifm` signature is **valid** when:

1. the `signature.value` verifies over the canonical encoding (so no byte of the signed part changed), and
2. the signing key is trusted: `public_key` equals a pinned key, or `certificate` is itself a pinned certificate, or it is signed by
   a pinned CA certificate in the list, and
3. neither the key nor the certificate is revoked (11.5).

**Certificate validity dates are not checked**: the file has no trustworthy signing time (it holds no timestamp, so that exports stay
deterministic), and "valid when built" cannot be proved. An old release therefore stays verifiable after its certificate expires.
`verify` prints the dates so a reviewer sees them.

**The policy covers the whole import tree**, not just the file named on the command line. Every `.kifm` the build loads, including the
ones a `.kifm` imports in turn, is checked when it is loaded, and a source module that imports an unsigned `.kifm` is told which
import it came through. Source modules (`.kif`) are not signed: they are the consumer's own code or a sysroot the consumer controls.

Outcomes, each a diagnostic at the `import` that loaded the file, never silent:

| Situation | `warn` (default) | `require` | `off` |
| --- | --- | --- | --- |
| Signed, valid, trusted | accepted, audit report prints `signed by <signer> (<fingerprint>)` | same | same (the signature is still checked when a trust list is given) |
| Signed, the signature does not verify | **KIF018** error | **KIF018** error | KIF018 error |
| Signed, key not in the trust list | **KIF019** *warning* with the fingerprint, so a first-time consumer can pin it | **KIF019** error | not checked |
| Revoked key or certificate | **KIF020** error | **KIF020** error | KIF020 error when a revocation list is given |
| Not signed | **KIF019** *warning* (`unsigned`) | **KIF019** error | accepted, the audit report says `unsigned` |

A signature that does not verify is always an error: a modified file is never "just a warning". `warn` is the default so a first build
works and tells the consumer exactly what to pin; `require` is for builds that must refuse anything not vouched for. A file whose
`signature` key was removed is indistinguishable from an unsigned one, which is why the policy is the consumer's switch, not the file's.

With `--sysroot`, the trust and revocation files are looked up *only* where the project says, never in the sysroot itself: a vendor
cannot ship the key that vouches for its own module.

### 11.4 Interaction with the rest

* `digest` stays the module identity that `deps` records, so signing does not change what a dependent was built against.
  A dependent's own `.kifm` records its imports by `digest`; whether those were signed is checked at its consumer's build.
* **Crypto comes from the host, never from kif.** Ed25519, X.509 and CRL handling are done by a crypto implementation that already
  exists on the build machine; kif contains no primitives of its own (no hand-written, vendored or "simple" versions). The
  dependency is on the **host system**, not on kif's package: it is not in `pyproject.toml`. `kiflang/modsign.py` is the only
  module that touches crypto, and it picks, in this order:
  1. the Python `cryptography` package, if it can be imported;
  2. as a last resort, the `openssl` command line tool (`openssl pkeyutl -sign|-verify -rawin`, `openssl x509`, `openssl crl`,
     `openssl verify`), run without a shell, with a fixed argument vector, key material only through files, a timeout and a
     minimum version check.

  If neither is available, the toolchain **stops with an error** that names what to install (`cryptography` or `openssl`). It is never
  skipped, never downgraded to "unsigned" and never turned into "accept because the library is missing". The same inputs give the
  same result on both back ends (the tests of 11.6 run each one that is present). Code that neither signs nor verifies (no
  `--trust`, `--sign`, `--revoked`, no `[signing]`, `mode = off`) does not look for crypto at all.
* A signature does not replace the checks of sections 4 and 5. A signed file with a `security` section inconsistent with its
  `text` is still KIF014: the signer signed a bad file, which is the vendor's bug, not a reason to accept it.

### 11.5 Revocation

Revocation is supported, offline, with no network lookups (OCSP and CRL download are deliberate non-goals: kernel builds are
hermetic). `revoked` files are PEM and may hold:

* **revoked keys or certificates**: a public key or a certificate in the file is revoked; a module signed by it, or by a certificate
  that has it in its chain, is **KIF020**, even if the same key is also in the trust list (revocation wins);
* **X.509 CRLs**: a CRL issued by a pinned CA lists revoked certificate serial numbers; a module whose certificate is on it is KIF020. A CRL
  that is not signed by a pinned CA is ignored with a note, never trusted.

Revocation is evaluated at **verification time**, not at signing time, which is the point: a leaked key retires every release it
signed. A consumer who must keep an old release then pins an explicit exemption (`allow-revoked = ["<fingerprint>"]` in `[signing]`),
which the audit report prints.

### 11.6 Tests that define done

1. Sign, verify with the right key: accepted, signer printed. The same with a CA-signed certificate.
2. One flipped byte in `text`, in `security`, in `deps`, in `compiler` or in `signature.value`: KIF018 (KIF014 first for `text`, as today).
3. A valid signature by a key that is not in the trust list: KIF019 warning under `warn` (with the fingerprint), error under `require`.
4. An unsigned file: KIF019 warning under `warn`, error under `require`, accepted under `off`; a signed file whose `signature` key was
   deleted behaves as unsigned.
5. The tree: `a.kif` imports signed `b.kifm`, which imports unsigned `c.kifm`: the diagnostic names `c` and the import path through `b`.
6. Mismatched `--cert` and `--sign`, a non-Ed25519 key: export fails.
7. An expired pinned certificate: accepted, `verify` prints its dates; removed from the trust list: KIF019.
8. Revocation: a revoked key in the trust list, a revoked certificate by CRL, a CRL not signed by a pinned CA (ignored with a note), an exempted
   fingerprint: each gives the stated result.
9. Deterministic signing: two exports are byte-identical.
10. A vendor-shipped key inside the sysroot is not read as a trust list.
11. Back ends: each of the `cryptography` and `openssl` paths gives identical verdicts on the whole corpus; with neither present the command
    stops with the install message and exit status 2; no case silently skips verification.
12. CMake: `TRUST`/`REVOKED`/`SIGNING` reach the compiler runs, and changing a trust file re-runs them.

## 12. Staging addition

Signing is stage 5, after stages 1 to 4 of section 9: it signs the finished header, so it needs the `security` section,
`security_digest` and `compiler` first. Codes `KIF018`, `KIF019`, `KIF020` (signing), `KIFI040` and `KIFI041` (`require_proved` across the
boundary) are reserved here.

## 13. IR attestation: a signed audit trail of what was compiled

*Why.* A certification or security reviewer asks "is the code in this binary the code that was reviewed?". Source text cannot answer
that (comments, names and layout change without changing meaning), and object code cannot (it depends on the C compiler). The IR sits
between them and is already the thing the checks, the barriers and the code generators work on, so **a hash of each function's
IR is a stable, meaning-based identity of what kif compiled**. `fn f(x: u32) -> u32 { 2 * x }` has the same hash in every file, under
any name, with any comments, on every machine, until its meaning changes.

### 13.1 The canonical form

`ir_hash(fn)` is `sha256` of the canonical CBOR (the encoder of `kiflang/cbor.py`) of the function's **raw IR**: what `ir.build_function`
produces from the checked source, **before any pass runs**. Raw IR is the right level because it depends only on the source's meaning and on
the builder; everything the passes add (proofs, elided checks, implicit barriers, drops) is *derived* from it by the compiler named in the
signed `compiler` key, so it needs no hash of its own: the same raw IR under the same `compiler.prover` gives the same derived result. Normalizations:

| Part | In the hash? | Why |
| --- | --- | --- |
| Instructions, operands, constants, types, callees, field names, check kinds and fact tags, barrier kinds, terminators | yes | this is the meaning |
| Parameter and local **types, mutability, ownership, taint**, `length` | yes | the contract and the drop behaviour depend on them |
| Function **attributes** that carry contracts (`ensures`, `preserves`, `untaint`, `tainted_result`, ...) and parameter attributes (`tainted`, fact attributes) | yes | the boundary is part of what was compiled |
| **Source positions** (`site`, `at`) | **no** | moving code is not changing it |
| **Comments**, doc comments, the `Call.reason` text of `untaint!`, `Barrier.why`, a check's `proof` explanation, `/* kif: ... */` audit comments | **no** | prose is not meaning; the reason strings are covered by the file signature (section 11) and shown by `--taint-report`, but an edited sentence must not make a function look changed |
| Local, parameter and block **names** and block labels | **no** | names are alpha-renamed: locals, temps and blocks are renumbered in order of first appearance, so renaming a variable gives the same hash |
| The function's own **name** | **no** | `ir_hash` identifies the body; the table maps a name to its hash. Two functions with the same body share a hash |
| What the passes derive (the `Check`s that are proved or elided, implicit barriers, drop decisions) | **no**: they are not in raw IR | derived from the hashed IR by the identified compiler; the `security` section records the outcome (`proved`, `kept_checks`, `barriers`) |
| An explicit `spec::barrier()` / `spec::value(..)` and the `ct::` calls | yes: they are in raw IR as written | the author wrote them |

A one-byte schema prefix (`kif-ir-hash/1`) is hashed first, so a future change of the canonical form changes every hash visibly instead of
silently. A change of the IR *builder* that changes the IR of a function changes its hash too: that is intended, it is exactly the
event an audit wants to see ("this release compiled the same source to different IR"). A change only in a pass (a better prover) leaves the
hash alone and changes `compiler.prover` instead, which is the signal for that case.

`2 * x` gives, in the IR, one `Binary(*)` over a parameter load and a constant, one return. Its canonical form contains none of the
comments, names or positions around it, so every copy of it in every project hashes the same, and `kif ir --hash` prints that value.

### 13.2 Where the hash goes

1. **`kif ir FILE --hash`**: one line per function, `sha256:<hex>  <name>  <file>:<line>`. A reviewer diffing two releases sees exactly which
   functions *meaningfully* changed.
2. **The generated C** (implemented, stage 6b; the hash is taken before any pass, so it equals `kif ir --hash`) carries `/* kif: ir sha256:<first 16 hex> */` above each function. It is a comment (the C compiler ignores it), so it does
   not change the object code, but it ties a line of generated code to the attestation. With a target key `attest.section` the C backend
   also emits a constant `{ name, full hash }` entry into that linker section, so the hash survives into the binary and
   `kif verify-object` (a later tool) can read it without trusting the comment.
3. **The `.kifm` `security.functions[name].ir_hash`**, signed with the rest of the header (section 11.1). A consumer verifying the
   file therefore also verifies *which IR each shipped function was compiled from*, and the importer's audit report prints it.
4. **A standalone attestation**, a first-class artifact (not a fallback), for any build, with or without a `.kifm`: `kif ir FILE --attest -o FILE.kifa --sign vendor.pem` writes a canonical CBOR map `{ magic "KIFA",
   version, target, profile, compiler, functions: { name: ir_hash } , signature }` with the same `signature` shape and the same trust,
   revocation and `[signing]` policy as a `.kifm` (section 11). `kif verify FILE.kifa --source FILE.kif` re-analyzes the source, recomputes
   every hash and reports each function as **match**, **changed**, **added** or **removed**.

### 13.3 What the attestation proves and does not

It proves that the signer attests that, with compiler `compiler` and target `target`, these functions have this IR. It does not
prove that the C compiler produced correct object code, that the IR is *correct* (the IR is checked by the passes, not certified by the
hash), or anything about a function that is not listed. A consumer with the source can *reproduce* the hash (13.2, 4), so for source
builds nobody has to take the signer's word; a `.kifm` consumer, who has no bodies, takes the vendor's, within the trust list of section 11.

### 13.4 Tests that define done

1. `fn f(x: u32) -> u32 { 2 * x }` has one hash; adding comments, doc comments, blank lines, a different function name, renaming the
   parameter, and moving it to another line and file do not change it; `3 * x`, `2 * x` with another type, or `x * 2` do.
2. Editing the `reason` of an `untaint!` or the text of a `Barrier.why` does not change the hash; changing which function is called does.
3. Adding or removing an explicit `spec::barrier()`, or changing a `#[tainted]` parameter, changes it; running the passes, or an improved
   prover proving one more check, does not.
4. The hash is the same on repeated runs and in a different process and directory; unrelated functions in the file do not affect it.
5. `--hash`, the C comment and the `.kifm` entry agree; the `kifa` verifies, and each of match, changed, added, removed is reported.
6. A flipped byte in a `.kifa` is KIF018; the trust, revocation and mode rules of section 11 apply unchanged.
7. The canonical form drops exactly the fields listed in 13.1: a test that adds a new IR field fails until the field is classified (in
   the hash or out), so nothing leaks in or out by accident.

### 13.5 Staging

**Stage 6 is implemented**: 6a `kiflang/irhash.py` and `kif ir FILE --hash`; 6b the C comment and the `.kifm` `ir_hash` key; 6c `kiflang/attest.py`
(`kif ir FILE --attest -o F.kifa [--target T --profile P --sign K --cert C]`, `python -m kiflang verify F.kifa [--source F.kif]`) and the target key
`attest.section` (a constant string per function in that linker section). Tests: `tests/test_irhash.py`, `tests/test_modsign.py`. `kif verify-object`
(reading the section back from an object file) is not written; `strings` or `objcopy` read it.

Stage 6, after signing: (a) canonical form and `--hash`, with the classification test of 13.4 (7); (b) the C comment and the `ir_hash` key of
`security.functions`; (c) the `.kifa` writer, `verify`, and the `attest.section` target key. Stage (a) is independent of signing and can be done
before stage 5.
