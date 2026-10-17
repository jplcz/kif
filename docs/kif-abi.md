# ABI manifest and baseline lint (`kif manifest`, `kif lint --baseline`)

A `#[kobj]` interface is an ABI: C and `.m` code, other kernel modules and previously built classes are compiled
against its method ids and signatures. The enums and structs a file declares are ABI too, because their layout is
generated. This guide describes the tooling that keeps a released ABI from breaking by accident.

1. At release time, `kif manifest` writes a JSON **manifest** of the file.
2. Keep that manifest in the repository (for example `abi/1.0/dev.json`).
3. In CI and in the editor, `kif lint --baseline abi/1.0/dev.json dev.kif` compares the working copy with it
   and reports every incompatible change (rules KIFL020-029 and KIFL033).

The manifest comes from the checked syntax tree alone, so it is the same for the C and the C++ backend and it exists
before code generation does. Bodies never appear in it: a changed default body is not an ABI change.

```
python3 -m kiflang.kif manifest dev.kif                    # one file: JSON on standard output
python3 -m kiflang.kif manifest -o abi/1.0 a.kif b.kif     # several: abi/1.0/a.json, abi/1.0/b.json
python3 -m kiflang.kif lint --baseline abi/1.0/a.json --baseline abi/1.0/b.json a.kif b.kif
```

A manifest is matched to an input by file name (`"file"` in the manifest), so pass every manifest you have; manifests
of files that are not being linted are ignored. A file that does not compile is not linted, so fix compile errors first.
The same rules run in the editor: set `kif.server.baselines` (LSP `initializationOptions.baselines`) to the manifest paths.

## What a manifest contains

```jsonc
{
  "format": "kif-abi-manifest", "version": 2,       // `kif lint` refuses any other format or version
  "file": "dev.kif",                                // matches the input by base name
  "interfaces": {                                   // `#[kobj]` interfaces only; plain interfaces are not ABI
    "dev": {
      "cxx_only": false, "extends": null,
      "self": "device_t",                           // the handle type from `self(..)`
      "version": 1, "errno": "positive",
      "c_symbols": ["dev_read", "dev_write"],       // own (non-`override`) methods only
      "ids": { "read": "0x04a5089ceb4e911e" },      // FNV-1a 64 of "dev::read": what dispatch uses
      "signatures": { "read": "0x530d4581961c8ca1" },  // hash of the signature text below
      "defaults": ["reset"],                        // methods that have a default body
      "attrs": { "read": ["might_sleep"] }          // ABI attributes (atomic_entry, atomic_only, might_sleep)
    }
  },
  "types": { "mode": "enum", "conf": "struct" },
  "layouts": { "mode": "u8:off=0,on=1", "conf": "base:u64,mode:mode" }
}
```

The signature hash is taken over `interface::method(param types)->return type;static|instance`, plus `;errno=<style>`
when the result is a `cresult<>`, `;attrs=<ABI attributes>` when there are any, and `;v=<version>` when the version is
not 1. Types are written canonically: blanks and lifetimes do not matter, parameter *names* do not matter, doc comments,
other attributes (`#[trace]`) and bodies do not matter. The receiver (`&self`) is not part of the text; the handle type is
the interface's `self(..)`.

A layout is the underlying type of an enum followed by every enumerator and its value (implicit values count up from
the previous one), or the fields of a struct as `name:type` in declaration order, preceded by `repr(..);` if the struct has
a `repr` attribute.

## Declaring a deliberate break

Raise the interface's `version`. A version bump declares a break, so the **method** rules (KIFL021, KIFL022, KIFL023,
KIFL025, KIFL029, KIFL033) stay quiet for that interface; the **type** rules (KIFL027, KIFL028) and KIFL020, KIFL024,
KIFL026 never do. After releasing the break, create the next manifest from the new file.
A lowered version reports only KIFL024, since every hash differs when the version does.

## Rules

Levels and opt-outs are those of every lint rule: `KIFL02x = off|warning|error` in `.kiflint`, or
`// kif-lint: disable=KIFL02x` above the declaration / `// kif-lint: disable-file=KIFL02x` for the file. Findings about
something that is *gone* are reported at the top of the file (line 1), so use `disable-file` there. A type break is
silenced only that way, so review it.

| Rule | Default | Finds | Why / fix |
|------|---------|-------|-----------|
| KIFL020 | error | A `#[kobj]` interface of the baseline is gone (or lost `#[kobj]`). | Every class implementing it stops compiling. Keep it, or deprecate it for a release first. |
| KIFL021 | error | A method was removed and `version` is unchanged. | Its callers break. Keep it, or raise `version`. |
| KIFL022 | error | A method's signature hash changed: parameter or result types, `static`, the `errno` style of a `cresult<>`, or an ABI attribute. `version` unchanged. | It is a different method now; old implementers fail the signature check. Add a new method instead. |
| KIFL023 | error | A method was added without a default and `version` is unchanged. | Classes built against the baseline do not implement it. Give it a default body, or raise `version`. |
| KIFL024 | error | `version` went down. | Versions only go up. |
| KIFL025 | off | Methods were added and `version` is unchanged (even with defaults). | Turn it on when you bump the version on every growth. |
| KIFL026 | error | `extends(..)` changed or was removed. | The inherited method set changed. |
| KIFL027 | error | An enum or struct of the baseline is gone. | Everything using it breaks. |
| KIFL028 | error | Enum or struct layout changed: an enumerator or field was removed, renamed, retyped or reordered, an enumerator's value or the underlying type changed, or a field was added to a struct. | Only appending enumerators to an enum is compatible. |
| KIFL029 | error | A method lost its default and `version` is unchanged. | Classes that relied on the default no longer compile. |
| KIFL033 | error | The `self(..)` handle type changed and `version` is unchanged. | Every method now receives a different handle type. |

### Examples

Each pair is the released file (`old`) and the working copy (`new`). Inline comments say what the tool reports.

```kif
// old: released as abi/1.0/dev.json
#[kobj, self(device_t), version(1)]
interface dev {
    method read(off: u32) -> u32;
    method write(off: u32, value: u32) -> i32;
    method reset() -> i32 { 0 }
}
enum mode: u8 { off = 0, on = 1 }
struct conf { base: u64, mode: mode }
```

```kif
// new, KIFL021: `write` was removed and the version is still 1.
// Fix: keep `write`, or declare the break with version(2).
#[kobj, self(device_t), version(1)]
interface dev {
    method read(off: u32) -> u32;
    method reset() -> i32 { 0 }
}

// new, KIFL022: `read` now takes a u64, so it is not the method old implementers have.
// Fix: add `read64` and keep `read`.
//     method read(off: u64) -> u32;

// new, KIFL023: a new method without a default breaks every class built against the baseline.
// Fix: give it a body (compatible, KIFL025 would only ask for a version bump), or raise the version.
//     method flush() -> i32;            // KIFL023
//     method flush() -> i32 { 0 }       // fine

// new, KIFL029: `reset` was abstract-ized; classes that relied on the default stop compiling.
//     method reset() -> i32;

// new, KIFL033: the handle type changed.
//     #[kobj, self(other_t), version(1)]

// new, KIFL028: reordering, retyping or renumbering changes values that are already in use.
//     enum mode: u8 { on = 1, off = 0 }          // reordered
//     enum mode: u16 { off = 0, on = 1 }         // underlying type changed
//     struct conf { mode: mode, base: u64 }      // fields reordered
// Appending is the one compatible change:
//     enum mode: u8 { off = 0, on = 1, auto = 2 }
```

Gone interfaces and types (KIFL020, KIFL027) are the same idea: the item exists in the manifest and not in the file.

## Python API

```python
from kiflang import manifest
from kiflang.lint import lint
from kiflang.symbols import analyze

text = open("dev.kif").read()
mod = analyze("dev.kif", text)                                        # symbols.Module
release = manifest.manifest_of(mod, "dev.kif")                        # dict, JSON serializable
open("abi/1.0/dev.json", "w").write(manifest.dumps(release))

baseline = manifest.loads(open("abi/1.0/dev.json").read(), "abi/1.0/dev.json")   # ValueError if not a manifest
for d in lint(mod, baseline=baseline):                                # same call as plain lint, plus the baseline rules
    print(d.code, d.message)
```

`manifest.method_id(interface, method)` and `manifest.signature_hash(interface_node, method_node)` are the functions
code generation will use for the dispatch tables, so manifests and generated code cannot disagree.
