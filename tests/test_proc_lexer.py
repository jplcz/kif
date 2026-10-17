# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Lexer tests for the procedural language (docs/kif-proc-language.md section 3)."""

from kiflang import lexer as lx


def kinds(text):
    toks, diags = lx.lex(text)
    return [(t.kind, t.text) for t in toks[:-1]], diags


def test_integers_and_suffixes():
    toks, diags = kinds("10u8 0xFF 0b1010 0o17 1_000 7isize")
    assert not diags
    assert [t for k, t in toks if k == lx.INT] == ["10u8", "0xFF", "0b1010", "0o17", "1_000", "7isize"]


def test_int_values():
    assert lx.parse_int("10u8") == (10, "u8")
    assert lx.parse_int("0xFF") == (255, "")
    assert lx.parse_int("0b1010") == (10, "")
    assert lx.parse_int("0o17") == (15, "")
    assert lx.parse_int("1_000usize") == (1000, "usize")


def test_bad_number_is_one_diagnostic():
    toks, diags = kinds("9abc")
    assert [d.code for d in diags] == ["KIF006"]
    assert toks == []


def test_range_is_not_a_float():
    toks, _ = kinds("0..10 0..=3")
    assert toks == [
        (lx.INT, "0"), (lx.PUNCT, ".."), (lx.INT, "10"), (lx.INT, "0"), (lx.PUNCT, "..="), (lx.INT, "3"),
    ]


def test_operators_longest_match_and_right_shift_is_split():
    toks, _ = kinds("a <<= b -> c => d :: e ... != == <= >> >=")
    texts = [t for _, t in toks]
    assert texts == ["a", "<<=", "b", "->", "c", "=>", "d", "::", "e", "...", "!=", "==", "<=", ">", ">", ">", "="]


def test_attribute_openers():
    toks, _ = kinds("#![a] #[b]")
    assert [t for _, t in toks] == ["#![", "a", "]", "#[", "b", "]"]


def test_hash_without_bracket_is_an_error():
    _, diags = kinds("# note")
    assert [d.code for d in diags] == ["KIF005"]


def test_strings_and_escapes():
    toks, diags = kinds(r'"a\n\x41\"b"')
    assert not diags and toks[0][0] == lx.STRING
    assert lx.unescape(toks[0][1]) == 'a\nA"b'
    _, diags = kinds(r'"bad\q"')
    assert [d.code for d in diags] == ["KIF007"]
    _, diags = kinds('"open')
    assert [d.code for d in diags] == ["KIF001"]


def test_comments_are_skipped():
    toks, _ = kinds("a // b\n// c\nd")
    assert [t for _, t in toks] == ["a", "d"]


def test_verbatim_body_and_include():
    toks, diags = kinds("header {\nint x = 1 << 2; // raw\n}\ninclude <sys/param.h>;")
    assert not diags
    assert (lx.VERBATIM, "int x = 1 << 2; // raw") in toks
    assert (lx.SYSPATH, "<sys/param.h>") in toks


def test_unterminated_verbatim():
    _, diags = kinds("code {\nint x;\n")
    assert [d.code for d in diags] == ["KIF002"]


def test_invalid_character_keeps_lexing():
    toks, diags = kinds("a $ b")
    assert [d.code for d in diags] == ["KIF004"]
    assert [t for _, t in toks] == ["a", "b"]
