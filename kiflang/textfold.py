# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Folds the temporaries of generated C-family code back into the expressions they came from (docs/kif-ir-codegen.md, "Expression folding").

The IR is in three-address form, so the emitter writes `kif_t1 = it; kif_t2 = (kif_t1 < 4); if (kif_t2) ...`. This pass turns that
into `if ((it < 4)) ...` where it is safe, working on the statement lines of one block (and the branch condition after them):

* **A temp used once, by the next statement** (comments and `#line` lines do not count) is replaced by its expression, if at most one
  call is involved in the two statements together. Moving the evaluation one statement later cannot change what it reads, and with a
  single call nothing can reorder two side effects.
* **A copy of a local or a constant** (`kif_t5 = i;`) replaces every use in the block, provided nothing between the copy and the last use
  assigns that local, and its address is never taken anywhere in the function (so no call can change it behind our back).

Never folded: a temp with a `_v` companion (a `result` pair), one whose address is taken, one used on a left-hand side, one used in
another block, an expression with a compound literal, and anything that is not a plain `kif_tN = expr;` statement. The rules are
deliberately small: a missed fold only costs readability, a wrong one costs correctness."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_DEF = re.compile(r"\t(kif_t\d+) = (.+);")
_TOKEN = re.compile(r"\bkif_t\d+\b")
_ATOM = re.compile(r"[A-Za-z_]\w*|-?\d+[uUlL]*|U?INT64_C\(-?\d+\)|\(\(\w+\)-?\d+\)|true|false")
_CALL = re.compile(r"\b(\w+)\(")
PURE_C = ("KIF_", "UINT64_C", "INT64_C", "sizeof")  # call-like names that have no side effects in generated C; a backend passes its own list
_COND = "\t@COND("


def _is_code(line: str) -> bool:
    return not line.startswith("#") and not line.lstrip().startswith("/*")


def _impure_calls(text: str, pure: Tuple[str, ...] = PURE_C) -> int:
    n = 0
    for m in _CALL.finditer(text):
        name = m.group(1)
        if name in ("if", "while", "return", "COND") or name.startswith(pure) or name == "KIF_PANIC":
            continue
        n += 1
    return n


def _wrapped(expr: str) -> bool:
    """`(...)` whose first parenthesis closes at the last character."""
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for k, ch in enumerate(expr):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and k < len(expr) - 1:
            return False
    return True


def _substitute(line: str, name: str, expr: str) -> str:
    """Replace the uses of `name` in `line` by `expr`, parenthesised unless the surroundings make that redundant."""
    inner = expr[1:-1] if _wrapped(expr) else expr
    atomic = _ATOM.fullmatch(expr) is not None or _wrapped(expr) or re.fullmatch(r"[\w.>\-\[\]*]+\(.*\)|[\w.>\-]+\[.*\]", expr) is not None

    def repl(m: "re.Match[str]") -> str:
        before, after = line[:m.start()], line[m.end():]
        bare = before.endswith(("if (", "= ", "return ", "[", "(", ", ")) and after[:1] in (")", ";", "]", ",")
        if before.endswith("@COND(") or (before.endswith(("if (", "[", "= ", "return ")) and after[:1] in (")", ";", "]")):
            return inner if not atomic or _wrapped(expr) else expr
        return expr if atomic and bare else (expr if atomic else f"({expr})")

    return re.sub(rf"\b{name}\b", repl, line)


def _modifies(line: str, x: str) -> bool:
    if re.search(rf"&{re.escape(x)}\b", line):
        return True
    return re.search(rf"(?<![\w.>]){re.escape(x)}\s*(\[[^\]]*\]\s*)?(\.\w+\s*|->\w+\s*)*(=(?!=)|[-+*/%&|^]=|<<=|>>=|\+\+|--)", line) is not None


def fold(chunks: Dict[object, List[str]], conds: Dict[object, str], notes: bool = False, pure: Tuple[str, ...] = PURE_C) -> None:
    """Fold the temporaries of every block of a function in place. `chunks` maps a block to its statement lines, `conds` to the text of
    its branch condition (blocks without a branch are not in it). `pure` names the call-like tokens of the backend that have no side effects (macros, literals, `sizeof`). With `notes`, a definition that stays gets a trailing `/* kif: kept, why */`."""
    uses_total: Dict[str, int] = {}
    address_taken = set()
    text_all = [ln for ls in chunks.values() for ln in ls] + list(conds.values())
    for ln in text_all:
        for t in _TOKEN.findall(ln):
            uses_total[t] = uses_total.get(t, 0) + 1
        address_taken.update(re.findall(r"&\s*(\w+)", ln))
    companions = {m for ln in text_all for m in re.findall(r"\b(kif_t\d+)_v\b", ln)}

    why: Dict[str, str] = {}
    for b, lines in chunks.items():
        work = list(lines)
        has_cond = b in conds
        if has_cond:
            work.append(f"{_COND}{conds[b]})")
        changed = True
        while changed:
            changed = False
            for k, line in enumerate(work):
                m = _DEF.fullmatch(line)
                if m is None:
                    continue
                name, expr = m.group(1), m.group(2)
                if uses_total.get(name, 0) < 2:
                    continue  # never read: `_drop_dead`'s business
                if name in companions:
                    why[name] = "it has a `_v` companion (a result pair)"
                    continue
                if name in address_taken:
                    why[name] = "its address is taken"
                    continue
                if "{" in expr:
                    why[name] = "a compound literal cannot be nested"
                    continue
                use_at = [j for j in range(k + 1, len(work)) if re.search(rf"\b{name}\b", work[j])]
                if len(use_at) != uses_total[name] - 1:
                    why[name] = "it is read in another block"
                    continue
                if any(re.match(rf"\s*{name}\b", work[j]) or f"&{name}" in work[j] for j in use_at):
                    why[name] = "it is assigned or its address is taken later"
                    continue
                if _ATOM.fullmatch(expr) and expr not in address_taken and not expr.startswith("kif_t"):
                    last = use_at[-1]
                    if any(_modifies(work[j], expr) for j in range(k + 1, last)):
                        why[name] = f"{expr} is assigned before its last use"
                        continue
                    for j in use_at:
                        work[j] = _substitute(work[j], name, expr)
                    del work[k]
                    why.pop(name, None)
                    changed = True
                    break
                if len(use_at) != 1:
                    why[name] = f"it is read {len(use_at)} times"
                    continue
                nxt = next((j for j in range(k + 1, len(work)) if _is_code(work[j])), None)
                if nxt != use_at[0]:
                    why[name] = "other statements run between its definition and its use"
                    continue
                if _impure_calls(expr, pure) + _impure_calls(work[nxt], pure) > 1:
                    why[name] = "two calls would be reordered"
                    continue
                work[nxt] = _substitute(work[nxt], name, expr)
                del work[k]
                why.pop(name, None)
                changed = True
                break
        if has_cond:
            conds[b] = work.pop()[len(_COND):-1]
        chunks[b] = work
    _drop_dead(chunks, conds, address_taken, companions, pure)
    if notes:
        for b, lines in chunks.items():
            chunks[b] = [f"{ln} /* kif: kept, {why[m.group(1)]} */" if (m := _DEF.fullmatch(ln)) and m.group(1) in why else ln for ln in lines]


def _drop_dead(chunks: Dict[object, List[str]], conds: Dict[object, str], address_taken, companions, pure: Tuple[str, ...]) -> None:
    """Final dead-store elimination: a `kif_tN = expr;` whose temp is read nowhere (folding often leaves the copy of a value that only an
    elided check used) is dropped, or kept as the bare `expr;` when it still calls something."""
    reads: Dict[str, int] = {}
    for ln in [ln for ls in chunks.values() for ln in ls] + list(conds.values()):
        if _DEF.fullmatch(ln):
            ln = ln[ln.index(" = ") + 3:]
        for t in _TOKEN.findall(ln):
            reads[t] = reads.get(t, 0) + 1
    for b, lines in chunks.items():
        out: List[str] = []
        for ln in lines:
            m = _DEF.fullmatch(ln)
            if m and m.group(1) not in companions and m.group(1) not in address_taken and not reads.get(m.group(1)) and "{" not in m.group(2):
                if _impure_calls(m.group(2), pure):
                    out.append(f"\t{m.group(2)};")
                continue
            out.append(ln)
        chunks[b] = out
