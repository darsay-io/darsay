"""Variant sizes use complete shard groups, never the whole quant pack."""

import json
from pathlib import Path

from darsay.subset import matches_include, select_subset
from darsay.weight_variants import gguf_variants, model_weight_bytes


def test_real_glm_pack_has_twelve_models_not_seventy_shards():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/glm-5.3-flash-gguf.json").read_text()
    )
    files = fixture["files"]
    variants = gguf_variants(files)
    assert sum(f["size"] for f in files) == 2_545_636_747_545
    assert len(variants) == 12
    assert all(v["complete"] for v in variants)
    by_level = {v["precision"]: v for v in variants}
    assert by_level["BF16"]["size_bytes"] == 641_641_064_192
    assert by_level["BF16"]["file_count"] == 14
    assert by_level["UD-Q4_K_XL"]["size_bytes"] == 199_707_321_347
    chosen, _ = select_subset(files, by_level["UD-Q4_K_XL"]["include"])
    assert len(chosen) == 7  # six shards plus README, no other quant or projector
    weights = [f for f in chosen if f["path"].endswith(".gguf")]
    assert model_weight_bytes(weights) == 199_707_321_347
    assert model_weight_bytes([f for f in files if f["path"].endswith(".gguf")]) is None


def test_missing_duplicate_or_invalid_shards_are_not_complete():
    for paths in (
        ["m-Q4-00001-of-00002.gguf"],
        ["m-Q4-00001-of-00002.gguf", "m-Q4-00001-of-00002.gguf"],
        ["m-Q4-00000-of-00002.gguf", "m-Q4-00002-of-00002.gguf"],
        ["m-Q4-00001-of-999999999999.gguf"],
    ):
        files = [{"path": p, "size": 10} for p in paths]
        assert gguf_variants(files)[0]["complete"] is False
        assert model_weight_bytes(files) is None


def test_projectors_unknown_sizes_and_exact_selectors():
    files = [
        {"path": "a/[draft]-Q4.gguf", "size": None},
        {"path": "b/[draft]-Q4.gguf", "size": 15},
        {"path": "mmproj-F16.gguf", "size": 2},
    ]
    variants = gguf_variants(files)
    assert len(variants) == 2
    assert variants[0]["size_bytes"] is None
    assert [
        f["path"] for f in files if matches_include(f["path"], variants[0]["include"])
    ] == ["a/[draft]-Q4.gguf"]
    assert model_weight_bytes(files[-1:]) is None


def test_different_builds_at_same_precision_are_distinct():
    files = [{"path": p, "size": 4} for p in ["v1-Q4.gguf", "v2-Q4.gguf"]]
    assert len(gguf_variants(files)) == 2
    assert model_weight_bytes(files) is None


def test_single_filename_cannot_collide_with_a_sharded_group():
    files = [
        {"path": p, "size": 4}
        for p in [
            "model-of-00002.gguf",
            "model-00001-of-00002.gguf",
            "model-00002-of-00002.gguf",
        ]
    ]
    variants = gguf_variants(files)
    assert len(variants) == 2
    assert sorted(v["file_count"] for v in variants) == [1, 2]
    assert all(v["complete"] for v in variants)
