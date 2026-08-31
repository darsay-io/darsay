"""Read safetensors headers directly (no torch/safetensors dependency).

A .safetensors file starts with an 8-byte little-endian header length followed
by a JSON header mapping tensor names to {dtype, shape, data_offsets}. That is
enough to count parameters and identify precision.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

DTYPE_NAMES = {
    "F64": "float64",
    "F32": "float32",
    "F16": "float16",
    "BF16": "bfloat16",
    "I64": "int64",
    "I32": "int32",
    "I16": "int16",
    "I8": "int8",
    "U8": "uint8",
    "BOOL": "bool",
    "F8_E4M3": "float8_e4m3",
    "F8_E5M2": "float8_e5m2",
}


# The reference safetensors implementation refuses headers over 100 MB;
# with no known file size (a remote read) this is the sanity bound.
_MAX_HEADER = 100_000_000


def read_header_via(fetch, *, name: str = "<remote>", size: int | None = None) -> dict:
    """The JSON header through ``fetch(start, end) -> bytes``.

    ``ValueError`` when the bytes are not a safetensors file. ``size``
    tightens the length check when the file size is known (local files,
    upstream inventories); a remote fetch learns it from a short read.
    """
    prefix = fetch(0, 8)
    if len(prefix) < 8:
        raise ValueError(f"{name}: too short to be a safetensors file")
    (header_len,) = struct.unpack("<Q", prefix)
    if size is not None and header_len > size - 8:
        raise ValueError(
            f"{name}: safetensors header length {header_len} exceeds the file"
        )
    if header_len > _MAX_HEADER:
        raise ValueError(f"{name}: unreasonable safetensors header length {header_len}")
    raw = fetch(8, 8 + header_len)
    if len(raw) < header_len:
        raise ValueError(
            f"{name}: safetensors header length {header_len} exceeds the file"
        )
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError(f"{name}: safetensors header is not an object")
    return header


def read_header(path: Path) -> dict:
    """The JSON header of one safetensors file; ``ValueError`` if it is not one."""
    size = path.stat().st_size
    with open(path, "rb") as f:

        def fetch(start: int, end: int) -> bytes:
            f.seek(start)
            return f.read(end - start)

        return read_header_via(fetch, name=path.name, size=size)


def summarize_safetensors(paths: list[Path]) -> dict | None:
    """Aggregate parameter counts across one or more (possibly sharded) safetensors files."""
    if not paths:
        return None
    total = 0
    by_dtype: dict[str, int] = {}
    tensors = 0
    for path in paths:
        header = read_header(path)
        for name, entry in header.items():
            if name == "__metadata__":
                continue
            n = 1
            for dim in entry.get("shape", []):
                n *= dim
            dtype = DTYPE_NAMES.get(
                entry.get("dtype", ""), entry.get("dtype", "unknown")
            )
            by_dtype[dtype] = by_dtype.get(dtype, 0) + n
            total += n
            tensors += 1
    dominant = max(by_dtype, key=by_dtype.get) if by_dtype else None
    return {
        "parameter_count": total,
        "parameters_by_dtype": by_dtype,
        "tensor_count": tensors,
        "dominant_dtype": dominant,
        "shard_count": len(paths),
    }
