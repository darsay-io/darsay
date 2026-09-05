"""Synthetic payloads for hermetic tests.

These are just enough to satisfy completeness rules and metadata extractors.
They never talk to a network.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping

_DTYPE_WIDTH = {"F32": 4, "F16": 2, "BF16": 2, "I8": 1, "I32": 4, "F64": 8}


def make_safetensors(
    tensors: Mapping[str, tuple[str, list[int]]],
    *,
    metadata: dict | None = None,
) -> bytes:
    """Build a minimal .safetensors file (zeros for tensor bodies)."""
    header: dict = {}
    if metadata is not None:
        header["__metadata__"] = {str(k): str(v) for k, v in metadata.items()}
    offset = 0
    body = bytearray()
    for name, (dtype, shape) in tensors.items():
        n = 1
        for dim in shape:
            n *= dim
        nbytes = n * _DTYPE_WIDTH[dtype]
        header[name] = {
            "dtype": dtype,
            "shape": shape,
            "data_offsets": [offset, offset + nbytes],
        }
        body.extend(b"\x00" * nbytes)
        offset += nbytes
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + bytes(body)


def model_files(
    *,
    extra: dict[str, bytes] | None = None,
    param_shape: list[int] | None = None,
) -> dict[str, bytes]:
    """A complete model payload: config + tokenizer + weights + license + card."""
    shape = param_shape or [2, 4]
    files = {
        "config.json": json.dumps(
            {
                "model_type": "testlm",
                "architectures": ["TestLMForCausalLM"],
                "max_position_embeddings": 128,
                "hidden_size": 8,
                "num_hidden_layers": 1,
                "num_attention_heads": 2,
                "num_key_value_heads": 2,
                "torch_dtype": "float32",
                "vocab_size": 16,
                "tie_word_embeddings": True,
                "transformers_version": "4.40.0",
            }
        ).encode("utf-8"),
        "generation_config.json": json.dumps(
            {"temperature": 0.7, "top_p": 0.9, "do_sample": False}
        ).encode("utf-8"),
        "tokenizer_config.json": json.dumps(
            {
                "tokenizer_class": "PreTrainedTokenizer",
                "model_max_length": 128,
                "bos_token": "<s>",
                "eos_token": "</s>",
            }
        ).encode("utf-8"),
        "tokenizer.json": json.dumps(
            {"version": "1.0", "model": {"type": "WordLevel"}}
        ).encode("utf-8"),
        "model.safetensors": make_safetensors({"weight": ("F32", shape)}),
        "LICENSE": b"Apache License 2.0\n",
        "README.md": b"# Toy model\n\nA synthetic fixture.\n",
    }
    if extra:
        files.update(extra)
    return files


def dataset_files(*, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """A complete dataset payload: jsonl rows + card + license."""
    files = {
        "train.jsonl": b'{"text": "hello"}\n{"text": "world"}\n',
        "README.md": b"# Toy dataset\n",
        "LICENSE": b"MIT License\n",
        "dataset_infos.json": json.dumps(
            {
                "default": {
                    "features": {"text": {"dtype": "string"}},
                    "splits": {
                        "train": {"num_examples": 2, "num_bytes": 40},
                    },
                    "download_size": 40,
                    "dataset_size": 40,
                }
            }
        ).encode("utf-8"),
    }
    if extra:
        files.update(extra)
    return files


def code_files(*, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """A serving-recipe repository: launcher scripts, a container recipe,
    a compose file, Python requirements, an env template, a source dir."""
    files = {
        "README.md": b"# Toy recipe\n\nServes acme/toy on one box.\n",
        "LICENSE": b"MIT License\n",
        "Dockerfile": b"FROM python:3.12-slim\nCOPY . /app\n",
        "compose.yaml": b"services:\n  serve:\n    build: .\n",
        "requirements.txt": b"vllm\n",
        ".env.sample": b"MODEL_ID=acme/toy\n",
        "start.sh": b"#!/bin/sh\ndocker compose up\n",
        "stop.sh": b"#!/bin/sh\ndocker compose down\n",
        "files/patch_engine.py": b"print('patched')\n",
    }
    if extra:
        files.update(extra)
    return files


def _gguf_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _gguf_value(value) -> tuple[int, bytes]:
    """(type id, encoded bytes) with types inferred from Python values."""
    if isinstance(value, bool):
        return 7, struct.pack("<?", value)
    if isinstance(value, int):
        if value < 0:
            return 5, struct.pack("<i", value)
        return 4, struct.pack("<I", value)
    if isinstance(value, float):
        return 6, struct.pack("<f", value)
    if isinstance(value, str):
        return 8, _gguf_string(value)
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, str) for v in value):
            elem_id, payload = 8, b"".join(_gguf_string(v) for v in value)
        elif value and all(isinstance(v, float) for v in value):
            elem_id, payload = 6, b"".join(struct.pack("<f", v) for v in value)
        else:
            elem_id, payload = 5, b"".join(struct.pack("<i", v) for v in value)
        return 9, struct.pack("<IQ", elem_id, len(value)) + payload
    raise TypeError(f"cannot encode GGUF value {value!r}")


def make_gguf(kv: Mapping[str, object], *, tensor_count: int = 0, version: int = 3):
    """Build a minimal GGUF header (KV table only, no tensor data)."""
    out = bytearray(b"GGUF")
    out += struct.pack("<IQQ", version, tensor_count, len(kv))
    for key, value in kv.items():
        type_id, payload = _gguf_value(value)
        out += _gguf_string(key) + struct.pack("<I", type_id) + payload
    return bytes(out)


def parquet_magic_file(body: bytes = b"payload") -> bytes:
    """Enough of a parquet file for the stdlib magic-byte check (not a real table)."""
    return b"PAR1" + body + b"PAR1"
