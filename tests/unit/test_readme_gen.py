from __future__ import annotations

from darsay.readme_gen import human_params, human_size


def test_human_size():
    assert human_size(None) == "?"
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.0 KiB"
    assert human_size(1024**3) == "1.0 GiB"


def test_human_params():
    assert human_params(None) == "unknown"
    assert human_params(128) == "128"
    assert human_params(1_500_000) == "1.5M"
    assert human_params(600_000_000) == "600.0M"
    assert human_params(1_200_000_000) == "1.20B"


def test_changed_lines_counts_a_rewrite_against_disk():
    from darsay.readme_gen import changed_lines

    assert changed_lines("a\nb\nc\n", "a\nb\nc\n") == (0, 0)
    assert changed_lines("a\nb\nc\n", "a\nB\nc\nd\n") == (2, 1)
    assert changed_lines(None, "one\ntwo\n") == (2, 0)


def test_kv_cache_lines_price_the_second_occupant_of_memory():
    from darsay.readme_gen import _kv_cache_lines

    shape = {
        "kind": "gqa",
        "full_layers": 64,
        "sliding_layers": 0,
        "sliding_window": None,
        "recurrent_layers": 0,
        "kv_heads": 8,
        "head_dim": 128,
        "values": 2,
        "kv_bytes_per_token": 262144,
    }
    assert _kv_cache_lines(shape) == [
        "- KV cache: 256 KiB per token at 16-bit (64 layers × 8 KV heads × 128 (GQA)); "
        "32k context ≈ 8.0 GiB — on top of the weights, for the context you run at"
    ]
    assert _kv_cache_lines(None) == []
