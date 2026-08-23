"""Extract model metadata from the archived payload (config.json, tokenizer files).

Everything here reads only the downloaded files — no network — so `regen` and
`verify` can rebuild metadata offline from the archive itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from .safetensors_meta import summarize_safetensors


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def extract_model_metadata(payload_root: Path, card_data: dict | None = None) -> dict:
    card_data = card_data or {}
    config = _load_json(payload_root / "config.json") or {}
    generation_config = _load_json(payload_root / "generation_config.json") or {}
    tokenizer_config = _load_json(payload_root / "tokenizer_config.json") or {}

    safetensors_files = sorted(payload_root.glob("*.safetensors"))
    weights = summarize_safetensors(safetensors_files)

    special_tokens = {
        key: tokenizer_config.get(key)
        for key in ("bos_token", "eos_token", "pad_token", "unk_token", "sep_token", "cls_token", "mask_token")
        if tokenizer_config.get(key) is not None
    }

    chat_template_present = bool(tokenizer_config.get("chat_template")) or any(
        payload_root.glob("chat_template.*")
    )

    languages = card_data.get("language")
    if isinstance(languages, str):
        languages = [languages]

    quantization = None
    if config.get("quantization_config"):
        qc = config["quantization_config"]
        quantization = qc.get("quant_method") or "unknown"

    return {
        "parameter_count": weights["parameter_count"] if weights else None,
        "parameters_by_dtype": weights["parameters_by_dtype"] if weights else None,
        "architecture": (config.get("architectures") or [None])[0],
        "model_type": config.get("model_type"),
        "context_length": config.get("max_position_embeddings"),
        "precision": config.get("torch_dtype") or (weights["dominant_dtype"] if weights else None),
        "quantization": quantization,
        "hidden_size": config.get("hidden_size"),
        "num_hidden_layers": config.get("num_hidden_layers"),
        "num_attention_heads": config.get("num_attention_heads"),
        "num_key_value_heads": config.get("num_key_value_heads"),
        "tie_word_embeddings": config.get("tie_word_embeddings"),
        "tokenizer": {
            "class": tokenizer_config.get("tokenizer_class"),
            "vocab_size": config.get("vocab_size"),
            "model_max_length": tokenizer_config.get("model_max_length"),
            "special_tokens": special_tokens or None,
            "chat_template_present": chat_template_present,
        },
        "languages": languages,
        "training_cutoff": None,  # rarely published; curator fills in when known
        "generation_defaults": {
            key: generation_config[key]
            for key in ("temperature", "top_p", "top_k", "do_sample", "repetition_penalty", "max_new_tokens")
            if key in generation_config
        }
        or None,
        "weight_shards": weights["shard_count"] if weights else None,
    }


def estimate_runtime(payload_root: Path, model_metadata: dict) -> dict:
    """Runtime requirements. Sizes are estimates from weight bytes; tested_hardware
    stays null until a curator actually runs the model somewhere."""
    weight_bytes = sum(p.stat().st_size for p in payload_root.glob("*.safetensors"))
    weight_bytes += sum(p.stat().st_size for p in payload_root.glob("*.bin"))
    weight_bytes += sum(p.stat().st_size for p in payload_root.glob("*.gguf"))
    est_gb = round(weight_bytes * 1.2 / 1024**3, 1) if weight_bytes else None

    has_safetensors = any(payload_root.glob("*.safetensors"))
    has_gguf = any(payload_root.glob("*.gguf"))
    engines = []
    if has_safetensors:
        engines.append("transformers")
    if has_gguf:
        engines.append("llama.cpp")

    return {
        "supported_engines": engines or None,
        "estimated_min_ram_gb": est_gb,
        "estimated_min_vram_gb": est_gb,
        "tested_hardware": None,
        "os_support": ["linux", "macos", "windows"] if has_safetensors or has_gguf else None,
        "cuda_notes": None,
        "rocm_notes": None,
        "cpu_inference": True if (has_safetensors or has_gguf) else None,
        "notes": "RAM/VRAM figures are estimates (weight bytes x1.2); engines listed from shipped formats only.",
    }
