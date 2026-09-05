"""The attention shape: what one token of context costs, read from config.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.attention import (
    attention_shape,
    describe_attention,
    human_kv_at,
    human_kv_per_token,
    kv_cache_bytes,
)

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "attention-configs.json"
    ).read_text()
)


@pytest.mark.parametrize("row", FIXTURES, ids=[r["name"] for r in FIXTURES])
def test_shape_matches_the_shared_fixtures(row):
    assert attention_shape(row["config"]) == row["attention"]


def _shape(name: str) -> dict:
    return next(r["attention"] for r in FIXTURES if r["name"] == name)


def test_per_token_is_layers_times_heads_times_dim_times_two_values_at_two_bytes():
    llama = _shape("meta-llama/Llama-3.1-8B")
    assert llama["kv_bytes_per_token"] == 32 * 8 * 128 * 2 * 2 == 128 * 1024
    # MLA stores one latent, not a key and a value per head.
    deepseek = _shape("deepseek-ai/DeepSeek-V3")
    assert deepseek["kv_bytes_per_token"] == 61 * 576 * 2


def test_kv_cache_bytes_grows_with_context_except_where_the_window_stops_it():
    llama = _shape("meta-llama/Llama-3.1-8B")
    assert kv_cache_bytes(llama, 32768) == 128 * 1024 * 32768  # 4 GiB
    assert kv_cache_bytes(llama, 32768, bytes_per_value=1.0625) == round(
        128 * 1024 * 32768 / 2 * 1.0625
    )
    gemma = _shape("google/gemma-3-27b-it")
    per_layer_token = 16 * 128 * 2 * 2
    # Ten global layers price every token; fifty-two sliding layers stop at 1k.
    assert kv_cache_bytes(gemma, 32768) == per_layer_token * (10 * 32768 + 52 * 1024)
    # Recurrent layers add nothing per token.
    hybrid = _shape("Qwen/Qwen3-Next-80B-A3B-Instruct")
    assert kv_cache_bytes(hybrid, 8192) == 12 * 2 * 256 * 2 * 2 * 8192
    assert kv_cache_bytes(None, 8192) is None
    assert kv_cache_bytes(llama, 0) is None
    assert kv_cache_bytes({"kv_heads": 8}, 8192) is None


def test_describe_and_human_figures():
    llama = _shape("meta-llama/Llama-3.1-8B")
    assert describe_attention(llama) == "32 layers × 8 KV heads × 128 (GQA)"
    assert human_kv_per_token(llama) == "128 KiB per token at 16-bit"
    assert human_kv_at(llama, 32768) == "32k context ≈ 4.0 GiB"
    assert human_kv_at(llama, 2048) == "2k context ≈ 256 MiB"
    gemma = _shape("google/gemma-3-27b-it")
    assert (
        describe_attention(gemma)
        == "62 layers: 10 full + 52 sliding at 1k × 16 KV heads × 128 (GQA)"
    )
    hybrid = _shape("Qwen/Qwen3-Next-80B-A3B-Instruct")
    assert (
        describe_attention(hybrid)
        == "48 layers: 12 full + 36 recurrent × 2 KV heads × 256 (GQA)"
    )
    deepseek = _shape("deepseek-ai/DeepSeek-V3")
    assert describe_attention(deepseek) == "61 layers × latent 576 (MLA)"
    assert human_kv_per_token(deepseek) == "69 KiB per token at 16-bit"
    falcon = _shape("tiiuae/falcon-7b")
    assert human_kv_per_token(falcon) == "8.0 KiB per token at 16-bit"
    assert describe_attention(None) is None
    assert human_kv_per_token(None) is None
    assert human_kv_at(None, 1024) is None
