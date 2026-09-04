"""Release precision labels and bytes per parameter — the size explained."""

from __future__ import annotations

from darsay.precision import (
    bytes_per_param,
    describe_bytes_per_param,
    gguf_level_of,
    human_bytes_per_param,
    precision_facts,
    precision_from_config,
)

KIMI_K3 = {
    "quantization_config": {
        "config_groups": {
            "group_0": {
                "format": "mxfp4-pack-quantized",
                "targets": ["Linear"],
                "weights": {
                    "group_size": 32,
                    "num_bits": 4,
                    "strategy": "group",
                    "symmetric": True,
                    "type": "float",
                },
            }
        },
        "format": "mxfp4-pack-quantized",
        "quant_method": "compressed-tensors",
        "quantization_status": "compressed",
    }
}
DEEPSEEK_FP8 = {
    "quantization_config": {
        "activation_scheme": "dynamic",
        "fmt": "e4m3",
        "quant_method": "fp8",
        "weight_block_size": [128, 128],
    }
}


def test_native_mxfp4_release_is_named_and_marked_quantized():
    got = precision_facts(
        config=KIMI_K3, dominant_dtype="U8", dominant_format="safetensors"
    )
    assert got["label"] == "MXFP4"
    assert got["method"] == "compressed-tensors"
    assert got["bits"] == 4
    assert got["quantized"] is True
    assert "group 32" in got["detail"]


def test_fp8_and_bf16_releases():
    fp8 = precision_facts(
        config=DEEPSEEK_FP8, dominant_dtype="F8_E4M3", dominant_format="safetensors"
    )
    assert (fp8["label"], fp8["bits"], fp8["quantized"]) == ("FP8", 8, True)
    bf16 = precision_facts(
        config={"torch_dtype": "bfloat16"},
        dominant_dtype="BF16",
        dominant_format="safetensors",
    )
    assert (bf16["label"], bf16["bits"], bf16["quantized"]) == ("BF16", 16, False)
    assert bf16["method"] == "safetensors dtype"
    # A dtype alone, without a config, still names the precision.
    assert (
        precision_facts(config=None, dominant_dtype="F8_E4M3", dominant_format=None)[
            "label"
        ]
        == "FP8"
    )


def test_awq_gptq_bitsandbytes_and_mlx_labels():
    assert (
        precision_from_config(
            {
                "quantization_config": {
                    "quant_method": "awq",
                    "bits": 4,
                    "group_size": 128,
                }
            }
        )["label"]
        == "AWQ INT4"
    )
    assert (
        precision_from_config(
            {"quantization_config": {"quant_method": "gptq", "bits": 8}}
        )["label"]
        == "GPTQ INT8"
    )
    assert (
        precision_from_config(
            {
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_4bit": True,
                    "bnb_4bit_quant_type": "nf4",
                }
            }
        )["label"]
        == "NF4"
    )
    assert (
        precision_from_config(
            {
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_8bit": True,
                }
            }
        )["label"]
        == "INT8"
    )
    assert (
        precision_from_config({"quantization": {"bits": 4, "group_size": 64}})["label"]
        == "MLX 4-bit"
    )
    assert precision_from_config({"torch_dtype": "bfloat16"}) is None
    assert precision_from_config(None) is None


def test_gguf_levels_from_file_names():
    assert gguf_level_of("Qwen3.8-27B-Q4_K_M.gguf") == "Q4_K_M"
    assert gguf_level_of("model.IQ2_XS.gguf") == "IQ2_XS"
    assert gguf_level_of("x/UD-Q4_K_XL-00001-of-00002.gguf") == "UD-Q4_K_XL"
    assert gguf_level_of("Qwen3.8-27B-BF16.gguf") == "BF16"
    assert gguf_level_of("mmproj-F16.gguf") == "F16"
    assert gguf_level_of("weird.gguf") is None
    one = precision_facts(
        config=None,
        dominant_dtype=None,
        dominant_format="gguf",
        weight_paths=["m-Q4_K_M.gguf"],
    )
    assert (one["label"], one["bits"], one["quantized"]) == ("Q4_K_M", 4, True)
    pack = precision_facts(
        config=None,
        dominant_dtype=None,
        dominant_format="gguf",
        weight_paths=["m-Q4_K_M.gguf", "m-Q8_0.gguf", "m-IQ2_XS.gguf"],
    )
    assert pack["label"] == "GGUF"
    assert "3 quant levels" in pack["detail"]
    bf16 = precision_facts(
        config=None,
        dominant_dtype=None,
        dominant_format="gguf",
        weight_paths=["m-BF16.gguf"],
    )
    assert bf16["quantized"] is False


def test_nothing_known_is_all_none():
    got = precision_facts(config=None, dominant_dtype=None, dominant_format=None)
    assert got == {
        "label": None,
        "method": None,
        "detail": None,
        "bits": None,
        "quantized": None,
    }


def test_bytes_per_param_is_measured_and_described():
    assert bytes_per_param(4_892_361_000_000, 2_446_180_000_000) == 2.0
    assert bytes_per_param(1_561_000_000_000, 2_779_931_837_184) == 0.562
    assert bytes_per_param(None, 5) is None
    assert bytes_per_param(5, 0) is None
    assert bytes_per_param(True, 5) is None
    assert describe_bytes_per_param(2.0) == "about one 16-bit weight copy"
    assert describe_bytes_per_param(0.56).startswith("about half a byte")
    assert describe_bytes_per_param(1.0).startswith("about one byte")
    assert describe_bytes_per_param(8.6).startswith("more than two bytes per weight")
    assert describe_bytes_per_param(0.2).startswith("under half")
    assert describe_bytes_per_param(None) is None
    assert human_bytes_per_param(0.562) == "0.56 B/param"
    assert human_bytes_per_param(None) == "?"


def test_a_nested_quantization_config_is_found_and_the_dtype_key_too():
    nested = {"model_type": "kimi_k3", "text_config": KIMI_K3 | {"dtype": "bfloat16"}}
    assert precision_from_config(nested)["label"] == "MXFP4"
    got = precision_facts(
        config=nested, dominant_dtype="U8", dominant_format="safetensors"
    )
    assert (got["label"], got["quantized"]) == ("MXFP4", True)
    # No header dtype: the newer ``dtype`` key names the precision.
    plain = precision_facts(
        config={"text_config": {"dtype": "bfloat16"}},
        dominant_dtype=None,
        dominant_format=None,
    )
    assert (plain["label"], plain["quantized"]) == ("BF16", False)


def test_ggufs_beside_a_safetensors_negative_do_not_name_the_precision():
    got = precision_facts(
        config={"torch_dtype": "bfloat16"},
        dominant_dtype="BF16",
        dominant_format="gguf",
        weight_paths=[
            "model-00001-of-00002.safetensors",
            "x-Q4_K_M.gguf",
            "x-Q8_0.gguf",
        ],
    )
    assert (got["label"], got["quantized"]) == ("BF16", False)
