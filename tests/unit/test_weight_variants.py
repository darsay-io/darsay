"""Variant sizes use complete shard groups, never the whole quant pack."""

import json
from pathlib import Path

from darsay.subset import matches_include, select_subset
from darsay.weight_variants import gguf_variants, mark_imatrix, model_weight_bytes


def test_one_non_gguf_weight_set_is_one_directory_in_one_format():
    shards = [
        {"path": "model-00001-of-00002.safetensors", "size": 10},
        {"path": "model-00002-of-00002.safetensors", "size": 20},
    ]
    assert model_weight_bytes(shards) == 30
    assert model_weight_bytes([{"path": "weights/model.safetensors", "size": 40}]) == 40
    # Several directories are several sets: a diffusion repository's
    # transformer/, vae/ and text_encoder/ summed over one parameter count
    # is the redundant hint's arithmetic, not a width.
    assert (
        model_weight_bytes(
            [
                {"path": "transformer/model.safetensors", "size": 66},
                {"path": "vae/model.safetensors", "size": 10},
            ]
        )
        is None
    )
    # A legacy copy beside safetensors is a second copy, not a wider one.
    assert (
        model_weight_bytes(shards + [{"path": "pytorch_model.bin", "size": 30}]) is None
    )
    assert (
        model_weight_bytes(
            [
                {"path": "model.safetensors", "size": 40},
                {"path": "original/consolidated.00.pth", "size": 40},
            ]
        )
        is None
    )


def test_imatrix_is_read_from_headers_never_from_names():
    files = [
        {"path": "m-i1-Q4_K_M.gguf", "size": 10},
        {"path": "m-Q8_0-00001-of-00002.gguf", "size": 10},
        {"path": "m-Q8_0-00002-of-00002.gguf", "size": 10},
        {"path": "m-IQ2_XXS.gguf", "size": 5},
    ]
    variants = gguf_variants(files)
    assert all(v["imatrix"] is None for v in variants)
    classification = {
        "sets": [
            {
                "kind": "gguf",
                "evidence": {
                    "headers": {"m-i1-Q4_K_M.gguf": {"imatrix": False}},
                    "complete": True,
                },
            },
            {
                "kind": "gguf",
                "evidence": {
                    "headers": {
                        "m-Q8_0-00001-of-00002.gguf": {"imatrix": False},
                        "m-Q8_0-00002-of-00002.gguf": {"imatrix": True},
                    },
                    "complete": True,
                },
            },
            {"kind": "support", "evidence": {}},
        ]
    }
    marked = {
        v["name"]: v["imatrix"] for v in mark_imatrix(variants, files, classification)
    }
    # The name says i1; the header says no matrix. The header wins.
    assert marked["m-i1-Q4_K_M"] is False
    # Any shard's header naming a matrix marks the variant, as rule R7 does.
    assert marked["m-Q8_0"] is True
    # A variant whose header was not read stays unknown, IQ level or not.
    assert marked["m-IQ2_XXS"] is None
    assert all(v["imatrix"] is None for v in mark_imatrix(variants, files, None))


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
