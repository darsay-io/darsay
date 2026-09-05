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


def test_runtime_declarations_read_the_inventory_without_a_verdict():
    from darsay.metadata import RUNTIME_DECLARATION_CAP, runtime_declarations

    paths = [
        "README.md",
        "Dockerfile",
        "docker/Dockerfile.gpu",
        "compose.yaml",
        "pyproject.toml",
        "requirements/dev.txt",
        "start.sh",
        "scripts/nested.sh",  # shell counts at the root only
        "Makefile",
        "sub/Makefile",  # so does make
        ".env.example",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "flake.nix",
    ]
    out = runtime_declarations(paths)
    assert out["read_from"] == "inventory"
    found = out["found"]
    assert found["container"] == ["Dockerfile", "docker/Dockerfile.gpu"]
    assert found["compose"] == ["compose.yaml"]
    assert found["python"] == ["pyproject.toml", "requirements/dev.txt"]
    assert found["shell"] == ["start.sh"]
    assert found["make"] == ["Makefile"]
    assert found["env_template"] == [".env.example"]
    assert found["node"] == ["package.json"]
    assert found["rust"] == ["Cargo.toml"]
    assert found["go"] == ["go.mod"]
    assert found["nix"] == ["flake.nix"]
    assert out["counts"]["container"] == 2
    assert runtime_declarations(["README.md"]) == {
        "read_from": "inventory",
        "found": None,
        "counts": None,
    }
    many = [f"s{i}.sh" for i in range(RUNTIME_DECLARATION_CAP + 5)]
    capped = runtime_declarations(many)
    assert len(capped["found"]["shell"]) == RUNTIME_DECLARATION_CAP
    assert capped["counts"]["shell"] == RUNTIME_DECLARATION_CAP + 5


def test_extract_code_metadata_records_upstream_and_inventory(tmp_path):
    from darsay.metadata import extract_code_metadata
    from tests.payloads import code_files

    payload = tmp_path / "code"
    for name, data in code_files().items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (payload / ".cache" / "github").mkdir(parents=True)
    (payload / ".cache" / "github" / "x.sh.incomplete").write_bytes(b"partial")
    meta = extract_code_metadata(
        payload,
        {"repository": {"description": "d", "languages": {}, "default_branch": "main"}},
    )
    assert meta["description"] == "d"
    assert meta["languages"] is None  # empty upstream map is unknown, not {}
    assert meta["default_branch"] == "main"
    assert meta["topics"] is None
    found = meta["runtime_declarations"]["found"]
    assert found["shell"] == [
        "start.sh",
        "stop.sh",
    ]  # the partial in .cache is not payload
    assert found["container"] == ["Dockerfile"]
    # With file records, the inventory (not the disk) is the source of paths.
    records = [
        {"path": "code/only.sh", "size": 1},
        {"path": "code/deep/Dockerfile", "size": 1},
    ]
    from_records = extract_code_metadata(payload, {}, records)
    assert from_records["runtime_declarations"]["found"] == {
        "container": ["deep/Dockerfile"],
        "shell": ["only.sh"],
    }
    assert from_records["description"] is None
