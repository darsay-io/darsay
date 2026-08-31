"""Read GGUF key/value headers directly (no llama.cpp dependency).

A .gguf file starts with magic ``GGUF``, a u32 version, a u64 tensor
count, and a u64 KV count, followed by the KV table. Values are typed;
strings and arrays are length-prefixed. ``llama-quantize`` appends its
own keys (``quantize.imatrix.*``) after the converter's, which follow
the embedded tokenizer arrays — so establishing "no imatrix" means
walking the whole KV table.

The parser reads through a ``fetch(start, end) -> bytes`` callable, so
the same code serves a local file, an in-memory buffer, or a provider's
``read_bytes``. Bulk numeric arrays are skipped by offset arithmetic
without fetching their bytes; string arrays are traversed element by
element. Every fetched byte counts against a recorded cap.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FIRST_CHUNK = 4 * 1024 * 1024
_MAX_CHUNK = 16 * 1024 * 1024
DEFAULT_FETCH_CAP = 64 * 1024 * 1024
# Sanity bounds: a header field beyond these is malformed, not big.
_MAX_KV_COUNT = 65536
_MAX_STRING = 32 * 1024 * 1024

_T_STRING = 8
_T_ARRAY = 9
# type id -> (name, struct format, width) for fixed-width scalars.
_SCALARS = {
    0: ("U8", "<B", 1),
    1: ("I8", "<b", 1),
    2: ("U16", "<H", 2),
    3: ("I16", "<h", 2),
    4: ("U32", "<I", 4),
    5: ("I32", "<i", 4),
    6: ("F32", "<f", 4),
    7: ("BOOL", "<?", 1),
    10: ("U64", "<Q", 8),
    11: ("I64", "<q", 8),
    12: ("F64", "<d", 8),
}


class GGUFError(ValueError):
    """The bytes are not a parseable GGUF header."""


class GGUFTruncated(GGUFError):
    """The file ends inside the header."""


class GGUFReadCapExceeded(GGUFError):
    """Parsing would fetch more bytes than the recorded cap allows."""


@dataclass(frozen=True)
class GGUFArray:
    """A KV array whose elements were skipped or traversed, not retained."""

    type: str
    count: int


class _Reader:
    """A forward window over ``fetch``, with skip-without-fetch."""

    def __init__(self, fetch, cap: int, first_chunk: int):
        self.fetch = fetch
        self.cap = cap
        self.fetched = 0
        self.base = 0
        self.buf = b""
        self.pos = 0
        self.chunk = max(1, first_chunk)
        self.eof_at: int | None = None

    def _ensure(self, n: int) -> None:
        end = self.pos + n
        while self.base + len(self.buf) < end:
            if self.pos >= self.base + len(self.buf):
                # A skip carried us past the window; start fresh at pos.
                self.base = self.pos
                self.buf = b""
            tail = self.base + len(self.buf)
            if self.eof_at is not None and tail >= self.eof_at:
                raise GGUFTruncated("unexpected end of file inside GGUF header")
            want = max(self.chunk, end - tail)
            remaining = self.cap - self.fetched
            take = min(want, remaining)
            if tail + take < end:
                raise GGUFReadCapExceeded(
                    f"GGUF header needs more than the {self.cap}-byte read cap"
                )
            got = self.fetch(tail, tail + take)
            self.fetched += len(got)
            self.buf += got
            if len(got) < take:
                self.eof_at = tail + len(got)
                if self.base + len(self.buf) < end:
                    raise GGUFTruncated("unexpected end of file inside GGUF header")
            self.chunk = min(self.chunk * 2, _MAX_CHUNK)

    def take(self, n: int) -> bytes:
        self._ensure(n)
        offset = self.pos - self.base
        out = self.buf[offset : offset + n]
        self.pos += n
        return out

    def skip(self, n: int) -> None:
        self.pos += n


def _string(reader: _Reader) -> str:
    (n,) = struct.unpack("<Q", reader.take(8))
    if n > _MAX_STRING:
        raise GGUFError(f"unreasonable GGUF string length {n}")
    return reader.take(n).decode("utf-8", "replace")


def _value(reader: _Reader, type_id: int):
    scalar = _SCALARS.get(type_id)
    if scalar is not None:
        _, fmt, width = scalar
        (value,) = struct.unpack(fmt, reader.take(width))
        return value
    if type_id == _T_STRING:
        return _string(reader)
    if type_id == _T_ARRAY:
        (elem_id,) = struct.unpack("<I", reader.take(4))
        (count,) = struct.unpack("<Q", reader.take(8))
        elem_scalar = _SCALARS.get(elem_id)
        if elem_scalar is not None:
            name, _, width = elem_scalar
            # Bulk numeric data (token scores, rope tables): leapfrog.
            reader.skip(count * width)
            return GGUFArray(name, count)
        if elem_id == _T_STRING:
            for _ in range(count):
                (n,) = struct.unpack("<Q", reader.take(8))
                if n > _MAX_STRING:
                    raise GGUFError(f"unreasonable GGUF string length {n}")
                reader.skip(n)
            return GGUFArray("STRING", count)
        if elem_id == _T_ARRAY:
            for _ in range(count):
                _value(reader, elem_id)
            return GGUFArray("ARRAY", count)
        raise GGUFError(f"unknown GGUF array element type {elem_id}")
    raise GGUFError(f"unknown GGUF value type {type_id}")


def read_kv(
    fetch,
    *,
    fetch_cap: int = DEFAULT_FETCH_CAP,
    first_chunk: int = FIRST_CHUNK,
) -> dict:
    """Parse a GGUF KV table through ``fetch(start, end) -> bytes``.

    Returns ``{"version", "tensor_count", "kv_count", "kv",
    "bytes_fetched", "header_end"}`` where ``kv`` maps keys to scalars,
    strings, or :class:`GGUFArray` markers. Raises :class:`GGUFError`
    (malformed), :class:`GGUFTruncated`, or
    :class:`GGUFReadCapExceeded`; never returns a guess.
    """
    reader = _Reader(fetch, fetch_cap, first_chunk)
    if reader.take(4) != b"GGUF":
        raise GGUFError("not a GGUF file (bad magic)")
    (version,) = struct.unpack("<I", reader.take(4))
    if version < 2:
        raise GGUFError(f"unsupported GGUF version {version}")
    (tensor_count,) = struct.unpack("<Q", reader.take(8))
    (kv_count,) = struct.unpack("<Q", reader.take(8))
    if kv_count > _MAX_KV_COUNT:
        raise GGUFError(f"unreasonable GGUF KV count {kv_count}")
    kv: dict[str, object] = {}
    for _ in range(kv_count):
        key = _string(reader)
        (type_id,) = struct.unpack("<I", reader.take(4))
        kv[key] = _value(reader, type_id)
    return {
        "version": version,
        "tensor_count": tensor_count,
        "kv_count": kv_count,
        "kv": kv,
        "bytes_fetched": reader.fetched,
        "header_end": reader.pos,
    }
