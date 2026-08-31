"""GGUF KV header parsing over a bounded fetch callable."""

from __future__ import annotations

import struct

import pytest

from darsay.gguf_meta import (
    GGUFArray,
    GGUFError,
    GGUFReadCapExceeded,
    GGUFTruncated,
    read_kv,
)
from tests.payloads import make_gguf


def _fetch(data: bytes):
    return lambda start, end: data[start:end]


def test_round_trip_scalars_and_strings():
    data = make_gguf(
        {
            "general.architecture": "llama",
            "general.file_type": 15,
            "flag": True,
            "rope": 1.5,
        },
        tensor_count=3,
    )
    out = read_kv(_fetch(data))
    assert out["version"] == 3
    assert out["tensor_count"] == 3
    assert out["kv_count"] == 4
    kv = out["kv"]
    assert kv["general.architecture"] == "llama"
    assert kv["general.file_type"] == 15
    assert kv["flag"] is True
    assert abs(kv["rope"] - 1.5) < 1e-6
    assert out["header_end"] == len(data)


def test_imatrix_keys_after_bulk_arrays_leapfrog():
    data = make_gguf(
        {
            "general.name": "toy",
            "tokenizer.ggml.tokens": [f"tok{i}" for i in range(500)],
            # 1 MB of F32 the parser must jump, not fetch: the imatrix
            # keys llama-quantize appends come after the tokenizer bulk.
            "tokenizer.ggml.scores": [0.0] * 250_000,
            "quantize.imatrix.file": "imatrix.dat",
            "quantize.imatrix.entries_count": 512,
        }
    )
    out = read_kv(_fetch(data), first_chunk=4096)
    assert out["kv"]["quantize.imatrix.file"] == "imatrix.dat"
    assert out["kv"]["quantize.imatrix.entries_count"] == 512
    assert out["kv"]["tokenizer.ggml.scores"] == GGUFArray("F32", 250_000)
    assert out["kv"]["tokenizer.ggml.tokens"] == GGUFArray("STRING", 500)
    assert out["header_end"] == len(data)
    # The numeric bulk was skipped by offset arithmetic.
    assert out["bytes_fetched"] < 100_000


def test_truncated_header():
    data = make_gguf({"a": "b" * 100})
    with pytest.raises(GGUFTruncated):
        read_kv(_fetch(data[: len(data) // 2]))


def test_bad_magic_and_old_version():
    with pytest.raises(GGUFError, match="magic"):
        read_kv(_fetch(b"NOPE" + b"\x00" * 60))
    with pytest.raises(GGUFError, match="version 1"):
        read_kv(_fetch(make_gguf({"a": 1}, version=1)))


def test_fetch_cap_exceeded():
    data = make_gguf({"big": "x" * 100_000})
    with pytest.raises(GGUFReadCapExceeded):
        read_kv(_fetch(data), fetch_cap=1024, first_chunk=256)


def test_unreasonable_lengths_are_malformed_not_fetched():
    absurd_string = (
        b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**50)  # key length
    )
    with pytest.raises(GGUFError, match="string length"):
        read_kv(_fetch(absurd_string + b"\x00" * 64))
    absurd_count = b"GGUF" + struct.pack("<IQQ", 3, 0, 10**9)
    with pytest.raises(GGUFError, match="KV count"):
        read_kv(_fetch(absurd_count + b"\x00" * 64))


def test_unknown_value_type_is_malformed():
    data = (
        b"GGUF"
        + struct.pack("<IQQ", 3, 0, 1)
        + struct.pack("<Q", 1)
        + b"k"
        + struct.pack("<I", 99)
    )
    with pytest.raises(GGUFError, match="value type 99"):
        read_kv(_fetch(data + b"\x00" * 16))
