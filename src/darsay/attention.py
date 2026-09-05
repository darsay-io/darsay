"""Attention shape: what one token of context costs in memory.

A transformer keeps the key and value of every earlier token — the KV
cache — and its size per token is fixed by the model's shape: how many
layers attend, how many key-value heads each keeps, how wide a head is,
and whether a layer stores two vectors (a key and a value), one latent
(multi-head latent attention), or a bounded window. This module reads
that shape from ``config.json`` — the same file precision.py reads — and
records it so a reader can price a context length without the weights:

    bytes per token = layers × KV heads × head dimension × values × width

``kv_bytes_per_token`` is that figure at sixteen bits (two bytes a value),
the width runtimes default to. Sliding-window layers stop growing at
their window; recurrent layers (linear attention, state-space blocks)
hold a state per sequence that does not grow with context, so they are
counted and not priced. A shape the config does not establish is ``None``
— never a guess from a parameter count.

Mirrored in ``website/src/lib/attention.ts`` against
``tests/fixtures/attention-configs.json``; both suites run the same table.
"""

from __future__ import annotations

KIB = 1024
GIB = 1024**3
# Two bytes a value: F16/BF16, what every runtime's cache defaults to.
DEFAULT_BYTES_PER_VALUE = 2

_LAYER_KEYS = ("num_hidden_layers", "n_layer", "num_layers", "n_layers")
_HEAD_KEYS = ("num_attention_heads", "n_head", "num_heads", "n_heads")
_KV_HEAD_KEYS = ("num_key_value_heads", "num_kv_heads", "n_head_kv", "kv_n_heads")
_HIDDEN_KEYS = ("hidden_size", "n_embd", "d_model", "model_dim")
# ``layer_types`` vocabularies: transformers' own, and the names hybrid
# families use for the same idea. Anything else attends in full.
_SLIDING_TYPES = frozenset(
    {"sliding_attention", "sliding_window", "local_attention", "chunked_attention"}
)
_RECURRENT_TYPES = frozenset(
    {"linear_attention", "mamba", "mamba2", "recurrent", "ssm", "gated_delta_net", "M"}
)


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _first(config: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        got = _int(config.get(key))
        if got is not None:
            return got
    return None


def _text_config(config: dict) -> dict:
    """The block that describes the language model: top level, else ``text_config``."""
    if _first(config, _LAYER_KEYS) is not None:
        return config
    nested = config.get("text_config")
    if isinstance(nested, dict) and _first(nested, _LAYER_KEYS) is not None:
        return nested
    return config


def _layer_kinds(cfg: dict, layers: int) -> tuple[int, int, int]:
    """(full, sliding, recurrent) layer counts from the ways configs say it."""
    types = cfg.get("layer_types")
    if not isinstance(types, list):
        types = cfg.get("layers_block_type")
    if (
        isinstance(types, list)
        and len(types) == layers
        and all(isinstance(t, str) for t in types)
    ):
        return _count_types(types)
    pattern = cfg.get("hybrid_override_pattern")
    if isinstance(pattern, str) and len(pattern) == layers:
        # Nemotron-H: ``M`` a state-space block, ``*`` attention, ``-`` an MLP.
        full = pattern.count("*")
        recurrent = pattern.count("M")
        return full, 0, recurrent
    period = _int(cfg.get("attn_layer_period"))
    if period is not None:
        # Jamba: one attention layer every ``period`` layers, the rest Mamba.
        offset = _int(cfg.get("attn_layer_offset")) or 0
        full = sum(1 for i in range(layers) if i % period == offset % period)
        return full, 0, layers - full
    window = _window(cfg)
    if window is None:
        return layers, 0, 0
    stride = _int(cfg.get("sliding_window_pattern"))
    if stride is not None:
        # Gemma 3: every ``stride``-th layer attends globally, the rest slide.
        full = sum(1 for i in range(layers) if (i + 1) % stride == 0)
        return full, layers - full, 0
    if cfg.get("model_type") == "gemma2":
        # Gemma 2 alternates, even layers local, and says so only in code.
        sliding = sum(1 for i in range(layers) if i % 2 == 0)
        return layers - sliding, sliding, 0
    return 0, layers, 0


def _count_types(types: list[str]) -> tuple[int, int, int]:
    full = sliding = recurrent = 0
    for t in types:
        if t in _SLIDING_TYPES:
            sliding += 1
        elif t in _RECURRENT_TYPES:
            recurrent += 1
        else:
            full += 1
    return full, sliding, recurrent


def _window(cfg: dict) -> int | None:
    """The sliding window, when the config both names one and uses it."""
    if cfg.get("use_sliding_window") is False:
        return None
    return _int(cfg.get("sliding_window"))


def attention_shape(config: dict | None) -> dict | None:
    """The KV cache's shape from a ``config.json``, or None when it says too little.

    Returns ``{kind, full_layers, sliding_layers, sliding_window,
    recurrent_layers, kv_heads, head_dim, values, kv_bytes_per_token}``:
    ``kind`` is ``mha`` / ``gqa`` / ``mqa`` / ``mla``; ``values`` is 2 for
    a key and a value per head, 1 for MLA's single latent; and
    ``kv_bytes_per_token`` is what one token adds at sixteen bits across
    every attending layer, sliding ones included (they stop at their
    window; ``kv_cache_bytes`` applies it).
    """
    if not isinstance(config, dict):
        return None
    cfg = _text_config(config)
    layers = _first(cfg, _LAYER_KEYS)
    if layers is None:
        return None
    full, sliding, recurrent = _layer_kinds(cfg, layers)
    attending = full + sliding
    if attending == 0:
        return None

    lora_rank = _int(cfg.get("kv_lora_rank"))
    if lora_rank is not None:
        # Multi-head latent attention: one compressed latent per token per
        # layer — the rank plus the rotary part of the key that rides beside it.
        rope = _int(cfg.get("qk_rope_head_dim")) or 0
        shape = {
            "kind": "mla",
            "kv_heads": 1,
            "head_dim": lora_rank + rope,
            "values": 1,
        }
    else:
        heads = _first(cfg, _HEAD_KEYS)
        head_dim = _int(cfg.get("head_dim"))
        if head_dim is None:
            hidden = _first(cfg, _HIDDEN_KEYS)
            if hidden is None or heads is None or hidden % heads:
                return None
            head_dim = hidden // heads
        kv_heads = _first(cfg, _KV_HEAD_KEYS)
        if kv_heads is None:
            kv_heads = 1 if cfg.get("multi_query") is True else heads
        if kv_heads is None:
            return None
        if kv_heads == 1:
            kind = "mqa"
        elif heads is not None and kv_heads >= heads:
            kind = "mha"
        else:
            kind = "gqa"
        shape = {"kind": kind, "kv_heads": kv_heads, "head_dim": head_dim, "values": 2}

    per_layer = shape["kv_heads"] * shape["head_dim"] * shape["values"]
    return {
        "kind": shape["kind"],
        "full_layers": full,
        "sliding_layers": sliding,
        "sliding_window": _window(cfg) if sliding else None,
        "recurrent_layers": recurrent,
        "kv_heads": shape["kv_heads"],
        "head_dim": shape["head_dim"],
        "values": shape["values"],
        "kv_bytes_per_token": attending * per_layer * DEFAULT_BYTES_PER_VALUE,
    }


def kv_cache_bytes(
    shape: dict | None, tokens: int, bytes_per_value: float = DEFAULT_BYTES_PER_VALUE
) -> int | None:
    """The cache for ``tokens`` of context: full layers grow with every token,
    sliding layers stop at their window, recurrent layers add nothing."""
    if not isinstance(shape, dict) or tokens <= 0:
        return None
    try:
        per_layer_token = shape["kv_heads"] * shape["head_dim"] * shape["values"]
        full = shape["full_layers"] * tokens
        window = shape.get("sliding_window")
        capped = (
            min(tokens, window) if isinstance(window, int) and window > 0 else tokens
        )
        sliding = shape["sliding_layers"] * capped
    except (KeyError, TypeError):
        return None
    return int(round(per_layer_token * (full + sliding) * bytes_per_value))


def describe_attention(shape: dict | None) -> str | None:
    """``32 layers × 8 KV heads × 128 (GQA)``; the layer mix when it is mixed."""
    if not isinstance(shape, dict):
        return None
    full = shape.get("full_layers") or 0
    sliding = shape.get("sliding_layers") or 0
    recurrent = shape.get("recurrent_layers") or 0
    parts = []
    if sliding or recurrent:
        mix = [f"{full} full"]
        if sliding:
            window = shape.get("sliding_window")
            mix.append(
                f"{sliding} sliding at {_human_tokens(window)}"
                if window
                else f"{sliding} sliding"
            )
        if recurrent:
            mix.append(f"{recurrent} recurrent")
        parts.append(f"{full + sliding + recurrent} layers: {' + '.join(mix)}")
    else:
        parts.append(f"{full} layers")
    if shape.get("kind") == "mla":
        parts.append(f"latent {shape.get('head_dim')}")
        parts.append("MLA")
    else:
        parts.append(f"{shape.get('kv_heads')} KV heads × {shape.get('head_dim')}")
        parts.append(str(shape.get("kind") or "").upper())
    tail = parts.pop()
    return f"{' × '.join(parts)} ({tail})" if tail else " × ".join(parts)


def _human_tokens(n: int | None) -> str:
    if not n:
        return "?"
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}k"
    return str(n)


def human_kv_per_token(shape: dict | None) -> str | None:
    """``128 KiB per token at 16-bit``."""
    if not isinstance(shape, dict):
        return None
    per = shape.get("kv_bytes_per_token")
    if not isinstance(per, int) or per <= 0:
        return None
    if per >= 1024 * 1024:
        return f"{per / 1024**2:.2f} MiB per token at 16-bit"
    if per >= 10 * KIB:
        return f"{per / KIB:.0f} KiB per token at 16-bit"
    return f"{per / KIB:.1f} KiB per token at 16-bit"


def human_kv_at(shape: dict | None, tokens: int) -> str | None:
    """``32k context ≈ 4.0 GiB`` — the cache at one context length, sixteen bits."""
    total = kv_cache_bytes(shape, tokens)
    if total is None:
        return None
    gib = total / GIB
    amount = (
        f"{gib:.0f} GiB"
        if gib >= 100
        else f"{gib:.1f} GiB"
        if gib >= 1
        else f"{total / 1024**2:.0f} MiB"
    )
    return f"{_human_tokens(tokens)} context ≈ {amount}"
