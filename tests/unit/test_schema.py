from __future__ import annotations

import pytest

from darsay.schema import (
    ARTIFACT_TYPES,
    MANIFEST_KIND,
    check_completeness,
    parse_schema_major,
    payload_root,
    payload_root_for,
)


def test_parse_schema_major():
    assert parse_schema_major("1.6.0") == 1
    assert parse_schema_major("2.0.0") == 2
    with pytest.raises(ValueError):
        parse_schema_major("nope")
    assert MANIFEST_KIND == "darsay.bundle"


def test_payload_root_for_known_types():
    assert payload_root_for("model") == "model"
    assert payload_root_for("dataset") == "data"


def test_payload_root_reads_layout_defaulting_to_model():
    assert payload_root({"inventory": {"layout": {"payload_root": "data/"}}}) == "data"
    assert payload_root({"inventory": {}}) == "model"
    assert payload_root({}) == "model"


def test_model_completeness_complete():
    paths = [
        "model/config.json",
        "model/model.safetensors",
        "model/tokenizer.json",
        "model/README.md",
        "model/LICENSE",
        "model/generation_config.json",
        "model/tokenizer_config.json",
    ]
    result = check_completeness("model", paths)
    assert result["status"] == "complete"
    assert result["missing_required"] == []
    assert result["missing_recommended"] == []


def test_model_completeness_gguf_satisfies_all_required():
    result = check_completeness("model", ["model/toy.gguf"])
    assert result["status"] == "complete"
    assert result["missing_required"] == []


def test_model_completeness_missing_weights():
    result = check_completeness(
        "model", ["model/config.json", "model/tokenizer.json"]
    )
    assert result["status"] == "incomplete"
    assert "weights" in result["missing_required"]


def test_dataset_completeness_nested_parquet():
    result = check_completeness("dataset", ["data/split/train.parquet"])
    assert result["status"] == "complete"


def test_dataset_completeness_missing_data():
    result = check_completeness("dataset", ["data/README.md"])
    assert result["status"] == "incomplete"
    assert result["missing_required"] == ["data"]


def test_unknown_artifact_type():
    result = check_completeness("paper", ["paper/x.pdf"])
    assert result["status"] == "unknown-artifact-type"
    assert result["artifact_type"] == "paper"


def test_registry_has_model_and_dataset():
    assert set(ARTIFACT_TYPES) >= {"model", "dataset"}
