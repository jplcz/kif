# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""A small deterministic CBOR (RFC 8949) codec for `.kifm` files.

Supported: null, booleans, integers (64-bit), byte strings, text strings, arrays and maps with text keys. Encoding
is canonical (shortest integer heads, definite lengths, map keys sorted by encoded bytes), so equal values always give equal
bytes. Decoding is strict: it rejects indefinite lengths, floats, tags, duplicate keys, truncated or trailing data and
nesting deeper than `MAX_DEPTH`, and never allocates more than the input holds.
"""

from __future__ import annotations

import struct
from typing import Any, List, Tuple

MAX_DEPTH = 64


class CborError(ValueError):
    pass


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for extra, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([major << 5 | extra]) + struct.pack(fmt, n)
    raise CborError("integer out of 64-bit range")


def dumps(v: Any) -> bytes:
    if v is None:
        return b"\xf6"
    if v is True:
        return b"\xf5"
    if v is False:
        return b"\xf4"
    if isinstance(v, int):
        return _head(0, v) if v >= 0 else _head(1, -1 - v)
    if isinstance(v, (bytes, bytearray)):
        return _head(2, len(v)) + bytes(v)
    if isinstance(v, str):
        raw = v.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(v, (list, tuple)):
        return _head(4, len(v)) + b"".join(dumps(x) for x in v)
    if isinstance(v, dict):
        items = []
        for k, x in v.items():
            if not isinstance(k, str):
                raise CborError("map keys must be text")
            items.append((dumps(k), dumps(x)))
        items.sort(key=lambda kv: (len(kv[0]), kv[0]))
        return _head(5, len(items)) + b"".join(k + x for k, x in items)
    raise CborError(f"cannot encode {type(v).__name__}")


def loads(data: bytes, max_depth: int = MAX_DEPTH) -> Any:
    """Decode `data`; `max_depth` bounds the nesting (a syntax tree needs more than the `.kifm` default)."""
    v, pos = _read(memoryview(data), 0, 0, max_depth)
    if pos != len(data):
        raise CborError("trailing data")
    return v


def _arg(buf: memoryview, pos: int, info: int) -> Tuple[int, int]:
    if info < 24:
        return info, pos
    if info > 27:
        raise CborError("indefinite lengths and reserved values are not supported")
    size = 1 << (info - 24)
    if pos + size > len(buf):
        raise CborError("truncated")
    return int.from_bytes(buf[pos:pos + size], "big"), pos + size


def _read(buf: memoryview, pos: int, depth: int, limit: int = MAX_DEPTH) -> Tuple[Any, int]:
    if depth > limit:
        raise CborError("nested too deeply")
    if pos >= len(buf):
        raise CborError("truncated")
    major, info = buf[pos] >> 5, buf[pos] & 31
    pos += 1
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info == 22:
            return None, pos
        raise CborError("unsupported simple value or float")
    n, pos = _arg(buf, pos, info)
    if major == 0:
        return n, pos
    if major == 1:
        return -1 - n, pos
    if major in (2, 3):
        if pos + n > len(buf):
            raise CborError("truncated")
        raw = bytes(buf[pos:pos + n])
        if major == 2:
            return raw, pos + n
        try:
            return raw.decode("utf-8"), pos + n
        except UnicodeDecodeError as exc:
            raise CborError("invalid UTF-8") from exc
    if major == 4:
        if n > len(buf) - pos:
            raise CborError("truncated")
        out: List[Any] = []
        for _ in range(n):
            x, pos = _read(buf, pos, depth + 1, limit)
            out.append(x)
        return out, pos
    if major == 5:
        if n > len(buf) - pos:
            raise CborError("truncated")
        d = {}
        for _ in range(n):
            k, pos = _read(buf, pos, depth + 1, limit)
            if not isinstance(k, str):
                raise CborError("map keys must be text")
            if k in d:
                raise CborError(f"duplicate key {k!r}")
            d[k], pos = _read(buf, pos, depth + 1, limit)
        return d, pos
    raise CborError("tags are not supported")
