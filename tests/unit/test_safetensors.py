from __future__ import annotations

from darsay.safetensors_meta import (
    read_header,
    read_header_via,
    summarize_safetensors,
)
from tests.payloads import make_safetensors


def test_summarize_counts_parameters(tmp_path):
    blob = make_safetensors(
        {"w1": ("F32", [2, 4]), "w2": ("F16", [2, 2])},
        metadata={"format": "pt"},
    )
    path = tmp_path / "model.safetensors"
    path.write_bytes(blob)
    header = read_header(path)
    assert "__metadata__" in header
    summary = summarize_safetensors([path])
    assert summary["parameter_count"] == 8 + 4
    assert summary["parameters_by_dtype"]["float32"] == 8
    assert summary["parameters_by_dtype"]["float16"] == 4
    assert summary["dominant_dtype"] == "float32"
    assert summary["shard_count"] == 1
    assert summary["tensor_count"] == 2


def test_summarize_empty_is_none():
    assert summarize_safetensors([]) is None


def test_summarize_shards(tmp_path):
    a = tmp_path / "a.safetensors"
    b = tmp_path / "b.safetensors"
    a.write_bytes(make_safetensors({"w": ("F32", [2])}))
    b.write_bytes(make_safetensors({"w": ("F32", [3])}))
    summary = summarize_safetensors([a, b])
    assert summary["parameter_count"] == 5
    assert summary["shard_count"] == 2


def test_unreadable_header_is_a_value_error(tmp_path):
    """Garbage named .safetensors must not escape as OverflowError or struct.error."""
    import pytest

    garbage = tmp_path / "model.safetensors"
    garbage.write_bytes(b"\xff" * 8 + b"not json")
    with pytest.raises(ValueError, match="exceeds the file"):
        read_header(garbage)
    garbage.write_bytes(b"\x01\x02")
    with pytest.raises(ValueError, match="too short"):
        read_header(garbage)
    garbage.write_bytes(b"\x02" + b"\x00" * 7 + b"[]")
    with pytest.raises(ValueError, match="not an object"):
        read_header(garbage)


def test_read_header_via_remote_fetch():
    """The remote path shares the local parse: same header, same errors."""
    import pytest

    blob = make_safetensors({"w": ("BF16", [2, 3])})

    def fetch(start, end):
        return blob[start:end]

    header = read_header_via(fetch, name="model-00001.safetensors")
    assert header["w"]["dtype"] == "BF16"
    assert header["w"]["shape"] == [2, 3]

    truncated = blob[: len(blob) // 4]
    with pytest.raises(ValueError, match="exceeds the file"):
        read_header_via(lambda s, e: truncated[s:e])
    with pytest.raises(ValueError, match="too short"):
        read_header_via(lambda s, e: b"\x01"[s:e])
    import struct

    absurd = struct.pack("<Q", 10**12)
    with pytest.raises(ValueError, match="unreasonable"):
        read_header_via(lambda s, e: (absurd + b"\x00" * 8)[s:e])
