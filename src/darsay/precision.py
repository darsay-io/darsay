"""Precision: the release precision of a work's weights, and bytes per parameter.

The one number that explains every archive size is bytes per parameter:
a 2.4T-parameter BF16 release weighs 4.4 TiB because sixteen bits is two
bytes; a 2.8T-parameter native MXFP4 release weighs 1.4 TiB because four
bits plus a shared scale is about half a byte. This module names the
precision a repo was published at — ``BF16``, ``FP8``, ``MXFP4``,
``AWQ INT4``, ``Q4_K_M`` — from the facts darsay already holds
(``config.json``'s ``quantization_config``, the dominant safetensors dtype,
a GGUF's file name) and records which of them it read. It never opens a
weight file, and a label it cannot establish is ``None``.

A native low-precision release is not a downgrade: when no higher-fidelity
public copy exists, that release is the negative. ``quantized`` says only
that the label sits below full fidelity; whether it is a negative or a
print is the classifier's call (``classify.py``), never this module's.
"""

from __future__ import annotations

import re

FULL_FIDELITY_DTYPES = frozenset({"F64", "F32", "F16", "BF16"})
# Nominal bits per weight for a label — the *design* width, not the measured
# bytes per parameter (scales, attention kept in BF16, and embeddings move
# the measured figure). Used for the "one copy at N bits" phrasing only.
NOMINAL_BITS = {
    "F64": 64,
    "F32": 32,
    "F16": 16,
    "BF16": 16,
    "FP8": 8,
    "INT8": 8,
    "I8": 8,
    "U8": 8,
    "MXFP4": 4,
    "NVFP4": 4,
    "FP4": 4,
    "INT4": 4,
    "NF4": 4,
    "INT3": 3,
    "INT2": 2,
}
_DTYPE_LABELS = {
    # torch names, when a config or header speaks that dialect
    "FLOAT64": "F64",
    "FLOAT32": "F32",
    "FLOAT16": "F16",
    "BFLOAT16": "BF16",
    "F8_E4M3": "FP8",
    "F8_E5M2": "FP8",
    "F8_E4M3FN": "FP8",
    "F8_E8M0": "FP8",
}
# llama.cpp quant levels as they appear in file names: Q4_K_M, IQ2_XS, Q8_0,
# F16, BF16, MXFP4 (llama.cpp's own), UD-Q4_K_XL (unsloth dynamic).
_GGUF_LEVEL_RE = re.compile(
    r"(?:^|[-._])(?P<level>(?:UD-)?(?:I?Q\d(?:_[A-Za-z0-9]+)*|F16|BF16|F32|MXFP4(?:_MOE)?|TQ\d(?:_\d)?))(?=[-._]|$)",
    re.IGNORECASE,
)
_GGUF_BITS_RE = re.compile(r"^(?:UD-)?I?Q(?P<bits>\d)", re.IGNORECASE)


def _bits_of(weights: dict) -> int | None:
    for key in ("num_bits", "bits", "w_bit", "wbits"):
        value = weights.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def quantization_config_of(config: dict | None) -> dict | None:
    """The ``quantization_config``, wherever a config keeps it.

    Multimodal configs nest the language model's block (Kimi-K3 keeps it
    under ``text_config``); the first one found, top level first, wins.
    """
    if not isinstance(config, dict):
        return None
    qc = config.get("quantization_config")
    if isinstance(qc, dict):
        return qc
    for value in config.values():
        if isinstance(value, dict) and isinstance(
            value.get("quantization_config"), dict
        ):
            return value["quantization_config"]
    return None


def torch_dtype_of(config: dict | None) -> str | None:
    """``torch_dtype`` (or the newer ``dtype``), top level or one level down."""
    if not isinstance(config, dict):
        return None
    for key in ("torch_dtype", "dtype"):
        if isinstance(config.get(key), str):
            return config[key]
    for value in config.values():
        if isinstance(value, dict):
            for key in ("torch_dtype", "dtype"):
                if isinstance(value.get(key), str):
                    return value[key]
    return None


def precision_from_config(config: dict | None) -> dict | None:
    """The precision a ``config.json`` declares, or ``None`` when it declares none.

    Returns ``{"label", "method", "detail", "bits"}``. ``method`` is the
    ``quant_method`` (or ``"mlx"`` for an MLX ``quantization`` block); the
    label is a short name a collector recognises; ``detail`` keeps the
    parameters that make the recipe irreproducible from the negative alone.
    """
    if not isinstance(config, dict):
        return None
    qc = quantization_config_of(config)
    if isinstance(qc, dict):
        method = str(qc.get("quant_method") or "unspecified").lower()
        fmt = str(qc.get("format") or "").lower()
        bits = _bits_of(qc)
        group_size = None
        weights_type = None
        groups = qc.get("config_groups")
        if isinstance(groups, dict):
            for group in groups.values():
                if not isinstance(group, dict):
                    continue
                weights = group.get("weights")
                if isinstance(weights, dict):
                    bits = bits or _bits_of(weights)
                    group_size = group_size or weights.get("group_size")
                    weights_type = weights_type or weights.get("type")
                    break
        group_size = group_size or qc.get("group_size") or qc.get("q_group_size")
        if method == "fp8" or fmt.startswith("float-quantized") and bits == 8:
            label = "FP8"
            bits = bits or 8
        elif method == "compressed-tensors":
            if "mxfp4" in fmt:
                label = "MXFP4"
            elif "nvfp4" in fmt:
                label = "NVFP4"
            elif weights_type == "float" and bits == 4:
                label = "FP4"
            elif weights_type == "float" and bits == 8:
                label = "FP8"
            elif bits:
                label = f"INT{bits}"
            else:
                label = "COMPRESSED"
        elif method == "awq":
            label = f"AWQ INT{bits or 4}"
            bits = bits or 4
        elif method == "gptq":
            label = f"GPTQ INT{bits or 4}"
            bits = bits or 4
        elif method == "bitsandbytes":
            if qc.get("load_in_4bit") or qc.get("bnb_4bit_quant_type"):
                label = str(qc.get("bnb_4bit_quant_type") or "nf4").upper()
                bits = 4
            else:
                label = "INT8"
                bits = 8
        elif method in ("mxfp4", "nvfp4", "fp4"):
            label = method.upper()
            bits = 4
        elif method in ("int4", "int8"):
            label = method.upper()
            bits = int(method[3:])
        else:
            label = method.upper() + (f" {bits}-bit" if bits else "")
        detail_parts = [method]
        if fmt:
            detail_parts.append(fmt)
        if bits:
            detail_parts.append(f"{bits}-bit")
        if group_size:
            detail_parts.append(f"group {group_size}")
        return {
            "label": label,
            "method": method,
            "detail": " · ".join(detail_parts),
            "bits": bits,
        }
    mlx = config.get("quantization")
    if isinstance(mlx, dict) and _bits_of(mlx):
        bits = _bits_of(mlx)
        group = mlx.get("group_size")
        return {
            "label": f"MLX {bits}-bit",
            "method": "mlx",
            "detail": f"mlx · {bits}-bit" + (f" · group {group}" if group else ""),
            "bits": bits,
        }
    return None


def gguf_level_of(path: str) -> str | None:
    """``Q4_K_M`` from ``model-Q4_K_M.gguf``; ``None`` when the name says nothing."""
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    m = _GGUF_LEVEL_RE.search(name)
    return m.group("level").upper() if m else None


def gguf_bits(level: str | None) -> int | None:
    if not level:
        return None
    upper = level.upper()
    if upper in ("F16", "BF16"):
        return 16
    if upper == "F32":
        return 32
    if upper.startswith("MXFP4"):
        return 4
    m = _GGUF_BITS_RE.match(upper)
    return int(m.group("bits")) if m else None


def precision_facts(
    *,
    config: dict | None,
    dominant_dtype: str | None,
    dominant_format: str | None,
    weight_paths: list[str] | None = None,
) -> dict:
    """Name the release precision from what is already known.

    Returns ``{"label", "method", "detail", "bits", "quantized"}`` — every
    field ``None`` when nothing establishes it. Order of evidence: a GGUF
    file name's quant level (the format *is* the precision); the
    ``quantization_config``; the dominant safetensors dtype.
    """
    empty = {
        "label": None,
        "method": None,
        "detail": None,
        "bits": None,
        "quantized": None,
    }
    paths = list(weight_paths or [])
    only_gguf = bool(paths) and all(p.lower().endswith(".gguf") for p in paths)
    gguf_shaped = isinstance(dominant_format, str) and dominant_format.lower() == "gguf"
    # A GGUF's name is its precision only when GGUF is all the repo ships:
    # beside a safetensors negative, GGUFs are prints of it and the
    # negative's precision is the one to name.
    if only_gguf or (gguf_shaped and not paths):
        levels = sorted(
            {
                lvl
                for lvl in (gguf_level_of(p) for p in weight_paths or [])
                if lvl is not None
            }
        )
        if len(levels) == 1:
            level = levels[0]
            return {
                "label": level,
                "method": "gguf file name",
                "detail": f"GGUF · {level}",
                "bits": gguf_bits(level),
                "quantized": gguf_bits(level) is not None and gguf_bits(level) < 16,
            }
        if len(levels) > 1:
            return {
                "label": "GGUF",
                "method": "gguf file names",
                "detail": f"GGUF pack · {len(levels)} quant levels ({levels[0]} … {levels[-1]})",
                "bits": None,
                "quantized": True,
            }
        return {**empty, "label": "GGUF", "method": "file format", "quantized": True}
    declared = precision_from_config(config)
    if not dominant_dtype:
        # No header count upstream: the config's torch_dtype names the
        # precision the weights were saved in.
        dominant_dtype = torch_dtype_of(config)
    if declared is not None:
        bits = declared["bits"]
        return {
            **declared,
            "quantized": bits is None or bits < 16,
        }
    if isinstance(dominant_dtype, str) and dominant_dtype:
        upper = dominant_dtype.upper()
        label = _DTYPE_LABELS.get(upper, upper)
        return {
            "label": label,
            "method": "safetensors dtype",
            "detail": upper if label != upper else None,
            "bits": NOMINAL_BITS.get(label),
            "quantized": label not in FULL_FIDELITY_DTYPES,
        }
    return empty


def bytes_per_param(weight_bytes: int | None, parameters: int | None) -> float | None:
    """Measured bytes per parameter: what the release actually spends per weight."""
    if not isinstance(weight_bytes, int) or isinstance(weight_bytes, bool):
        return None
    if not isinstance(parameters, int) or isinstance(parameters, bool):
        return None
    if weight_bytes <= 0 or parameters <= 0:
        return None
    return round(weight_bytes / parameters, 3)


def describe_bytes_per_param(bpp: float | None) -> str | None:
    """The sentence a collector needs beside the number."""
    if bpp is None:
        return None
    if bpp >= 3.5:
        return "well over one full-fidelity copy — the repo likely ships several weight sets"
    if bpp >= 1.75:
        return "about one full-fidelity copy (16-bit)"
    if bpp >= 0.85:
        return "about one byte per weight — an 8-bit release"
    if bpp >= 0.4:
        return "about half a byte per weight — a 4-bit release"
    return "under half a byte per weight — 2- or 3-bit, or a subset of the weights"


def human_bytes_per_param(bpp: float | None) -> str:
    if bpp is None:
        return "?"
    return f"{bpp:.2f} B/param"
