from __future__ import annotations

import json

from darsay.metadata import (
    estimate_runtime,
    extract_dataset_metadata,
    extract_model_metadata,
)
from tests.payloads import dataset_files, make_safetensors, model_files


def test_extract_model_metadata_from_payload(tmp_path):
    payload = tmp_path / "model"
    payload.mkdir()
    for name, data in model_files().items():
        (payload / name).write_bytes(data)
    meta = extract_model_metadata(payload, {"language": "en"})
    assert meta["architecture"] == "TestLMForCausalLM"
    assert meta["model_type"] == "testlm"
    assert meta["parameter_count"] == 8
    assert meta["context_length"] == 128
    assert meta["precision"] == "F32"
    assert meta["bytes_per_param"] is not None
    assert meta["languages"] == ["en"]
    assert meta["training_cutoff"] is None  # never fabricated
    assert meta["tokenizer"]["chat_template_present"] is False
    assert meta["generation_defaults"]["temperature"] == 0.7


def test_extract_model_metadata_leaves_weights_unknown_for_a_bad_shard(tmp_path):
    payload = tmp_path / "model"
    payload.mkdir()
    for name, data in model_files().items():
        (payload / name).write_bytes(data)
    (payload / "model-00002.safetensors").write_bytes(b"\xff" * 64)
    meta = extract_model_metadata(payload)
    # Unknown, not a partial count over the readable shard.
    assert meta["parameter_count"] is None
    assert meta["weight_shards"] is None


def test_extract_model_metadata_quantization_and_missing_config(tmp_path):
    payload = tmp_path / "model"
    payload.mkdir()
    (payload / "config.json").write_text(
        json.dumps({"quantization_config": {"quant_method": "awq"}})
    )
    (payload / "model.safetensors").write_bytes(make_safetensors({"w": ("I8", [4])}))
    meta = extract_model_metadata(payload)
    assert meta["quantization"] == "awq"


def test_extract_dataset_metadata_declared_not_measured(tmp_path):
    payload = tmp_path / "data"
    payload.mkdir()
    for name, data in dataset_files().items():
        dest = payload / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    records = [
        {"path": "data/train.jsonl", "size": 2},
        {"path": "data/README.md", "size": 1},
    ]
    meta = extract_dataset_metadata(
        payload, {"language": "en", "task_categories": "text"}, records
    )
    assert meta["declared"]["example_count_total"] == 2
    assert meta["declared"]["sources"] == ["dataset_infos.json"]
    # jsonl is not measured; parquet-only, and pyarrow may be absent.
    assert meta["measured"]["status"] in {"skipped", "measured", "partial"}
    assert meta["languages"] == ["en"]


def test_measured_row_counts_skipped_without_parquet(tmp_path):
    from darsay.metadata import _measured_row_counts

    payload = tmp_path / "data"
    payload.mkdir()
    (payload / "rows.jsonl").write_text("{}\n")
    result = _measured_row_counts(payload)
    assert result["status"] == "skipped"
    assert "no parquet" in result["reason"]


def test_estimate_runtime_lists_engines_from_formats(tmp_path):
    payload = tmp_path / "model"
    payload.mkdir()
    (payload / "model.safetensors").write_bytes(make_safetensors({"w": ("F32", [2])}))
    runtime = estimate_runtime(payload, {})
    assert runtime["supported_engines"] == ["transformers"]
    assert runtime["tested_hardware"] is None
    assert runtime["cpu_inference"] is True
