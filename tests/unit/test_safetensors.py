from __future__ import annotations

from modelvault.safetensors_meta import read_header, summarize_safetensors
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
