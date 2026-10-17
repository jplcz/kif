#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause

"""
Freestanding/bare-metal header policy checker.

Scans a set of public headers for uses of standard-library facilities that
allocate memory, can throw exceptions, or otherwise pull in heavyweight
libstdc++/libc++ (and transitively glibc) runtime support -- containers
(`std::vector`, `std::string`, `std::map`, ...), smart pointers
(`std::shared_ptr`, `std::unique_ptr`, ...), type erasure
(`std::function`, `std::any`, `std::variant`, `std::optional`), the
`std::exception` hierarchy, iostreams, and the concrete OS-backed threading
primitives (`std::thread`, `std::mutex`, `std::condition_variable`, ...).
None of this is appropriate for a library meant to run in kernel/bare-metal
environments without a working allocator, exception support, or libc.

This script is deliberately self-contained and dependency-free (standard
library only) so it can be dropped as-is into sibling projects that share
the same freestanding-friendly design goals (e.g. microfmt, structo):
`check-no-std-types.py --include-dir include`.

Legitimate, deliberate interop with the standard library (e.g. an opt-in
adapter header converting to/from `std::vector`, or a backend that wraps
`<mutex>`/`<thread>` directly) is allowed, but must be marked explicitly
with one of the following comment markers so the exemption is visible in
code review and does not silently widen over time:

- `// std-interop-ok[: reason]` as a trailing comment on the offending
  line, to exempt that single line.
- `// std-interop-begin[: reason]` / `// std-interop-end` wrapping a
  block (e.g. a whole `#if defined(..._BACKEND_STD)` branch).
- `// std-interop-file[: reason]` anywhere in the file (conventionally
  right after `#pragma once`), to exempt the entire file -- appropriate
  for a header whose sole purpose is standard-library interop (e.g.
  `container_ref_std.hpp`).
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

COLOR_RED = "\033[31;1m"
COLOR_YELLOW = "\033[33;1m"
COLOR_GREEN = "\033[32m"
COLOR_RESET = "\033[0m"

# Each entry is (category, pattern of banned identifiers that follow `std::`).
BANNED_CATEGORIES = [
    ("allocating container", r"vector|multimap|unordered_map|map|unordered_multiset|unordered_set|multiset|set|"
                             r"forward_list|list|deque|stack|queue|priority_queue"),
    ("allocating string", r"basic_string|wstring|u8string|u16string|u32string|string"),
    ("fixed-size container (explicitly banned)", r"array|span"),
    ("allocating smart pointer", r"shared_ptr|unique_ptr|weak_ptr|make_shared|make_unique|allocate_shared|"
                                  r"enable_shared_from_this"),
    ("allocating type erasure", r"function|any|variant|optional"),
    ("exception type", r"exception|bad_alloc|bad_cast|bad_typeid|bad_optional_access|bad_variant_access|"
                        r"bad_any_cast|bad_function_call|bad_weak_ptr|runtime_error|logic_error|out_of_range|"
                        r"invalid_argument|length_error|domain_error|range_error|overflow_error|underflow_error|"
                        r"system_error|nested_exception"),
    ("iostream", r"cout|cerr|cin|clog|wcout|wcerr|ostream|istream|iostream|fstream|ifstream|ofstream|"
                 r"stringstream|ostringstream|istringstream"),
    ("OS-backed threading primitive", r"mutex|thread|condition_variable|shared_mutex|recursive_mutex|"
                                       r"timed_mutex|this_thread"),
    ("lock wrapper (prefer a dependency-free custom RAII lock type instead)", r"unique_lock|shared_lock|lock_guard|"
                                                                               r"scoped_lock"),
]

BANNED_PATTERN = re.compile(
    r"\bstd::(?:" + "|".join(p for _, p in BANNED_CATEGORIES) + r")\b"
)
# Bare exception-handling keywords, independent of any `std::` qualification.
KEYWORD_PATTERN = re.compile(r"\b(?:throw|catch)\b")

LINE_MARKER = re.compile(r"//.*\bstd-interop-ok\b")
BLOCK_BEGIN_MARKER = re.compile(r"//.*\bstd-interop-begin\b")
BLOCK_END_MARKER = re.compile(r"//.*\bstd-interop-end\b")
FILE_MARKER = re.compile(r"//.*\bstd-interop-file\b")


class Violation(NamedTuple):
    path: Path
    line: int
    text: str
    match: str


def blank_keep_newlines(s: str) -> str:
    return "".join(c if c == "\n" else " " for c in s)


def strip_comments(text: str) -> str:
    """Blanks out // and /* */ comments while preserving line numbers and
    all non-comment characters (so column/line positions stay meaningful).
    Does not attempt to special-case string/char literals: good enough for
    a documentation-heavy C++ codebase where banned tokens overwhelmingly
    appear in comments, not string literals."""
    out = []
    i = 0
    n = len(text)
    in_block = False
    while i < n:
        if in_block:
            end = text.find("*/", i)
            if end == -1:
                out.append(blank_keep_newlines(text[i:]))
                i = n
            else:
                out.append(blank_keep_newlines(text[i:end + 2]))
                i = end + 2
                in_block = False
            continue
        line_start = text.find("//", i)
        block_start = text.find("/*", i)
        if line_start == -1 and block_start == -1:
            out.append(text[i:])
            break
        if block_start != -1 and (line_start == -1 or block_start < line_start):
            out.append(text[i:block_start])
            i = block_start + 2
            in_block = True
            continue
        newline = text.find("\n", line_start)
        if newline == -1:
            out.append(text[i:line_start])
            break
        out.append(text[i:line_start])
        out.append(text[newline:newline + 1])
        i = newline + 1
    return "".join(out)


def scan_file(path: Path) -> List[Violation]:
    raw_text = path.read_text(encoding="utf-8", errors="ignore")
    if FILE_MARKER.search(raw_text):
        return []

    raw_lines = raw_text.splitlines()
    stripped_lines = strip_comments(raw_text).splitlines()

    violations: List[Violation] = []
    in_block_exemption = False
    for i, (raw_line, code_line) in enumerate(zip(raw_lines, stripped_lines), 1):
        if BLOCK_BEGIN_MARKER.search(raw_line):
            in_block_exemption = True
            continue
        if BLOCK_END_MARKER.search(raw_line):
            in_block_exemption = False
            continue
        if in_block_exemption or LINE_MARKER.search(raw_line):
            continue

        for m in BANNED_PATTERN.finditer(code_line):
            violations.append(Violation(path, i, raw_line.strip(), m.group(0)))
        for m in KEYWORD_PATTERN.finditer(code_line):
            violations.append(Violation(path, i, raw_line.strip(), m.group(0)))

    return violations


def collect_headers(include_dir: Path, extensions: List[str]) -> List[Path]:
    files: List[Path] = []
    for ext in extensions:
        files.extend(include_dir.rglob(f"*.{ext}"))
    return sorted(set(files))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--include-dir", default="include", help="Root directory to scan recursively (default: include)"
    )
    parser.add_argument(
        "--extension", action="append", dest="extensions", default=None,
        help="File extension (without dot) to scan; may be repeated (default: hpp, ipp)"
    )
    args = parser.parse_args()

    include_dir = Path(args.include_dir)
    if not include_dir.is_dir():
        print(f"{COLOR_RED}[ERROR] Include directory not found: {include_dir}{COLOR_RESET}", file=sys.stderr)
        return 1

    extensions = args.extensions or ["hpp", "ipp"]
    headers = collect_headers(include_dir, extensions)
    if not headers:
        print(f"{COLOR_YELLOW}[WARN] No headers found under {include_dir}{COLOR_RESET}")
        return 0

    all_violations: List[Violation] = []
    for header in headers:
        all_violations.extend(scan_file(header))

    print(f"Scanned {len(headers)} header(s) under {include_dir}/ for banned std:: usage.\n")

    if not all_violations:
        print(f"{COLOR_GREEN}[SUCCESS] No disallowed standard-library usage found.{COLOR_RESET}")
        return 0

    for v in all_violations:
        print(f"{COLOR_RED}{v.path}:{v.line}:{COLOR_RESET} {v.text}  (matched `{v.match}`)")

    print(
        f"\n{COLOR_RED}[ERROR] {len(all_violations)} disallowed standard-library use(s) found.{COLOR_RESET}\n"
        "If a use is genuinely a deliberate interop point with libstdc++/libc++, mark it explicitly:\n"
        "  // std-interop-ok[: reason]          (single line)\n"
        "  // std-interop-begin[: reason] ... // std-interop-end   (block)\n"
        "  // std-interop-file[: reason]        (whole file, e.g. right after #pragma once)\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
