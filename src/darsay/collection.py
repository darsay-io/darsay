"""Collection intent and inventories, shared in meaning with the board picker.

Presets are explicit curator actions, not preservation verdicts. No model
quality, hardware fit, or byte-reconstruction claim is inferred from a name.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files as resource_files

from .precision import gguf_level_of
from .subset import select_subset
from .weight_variants import gguf_groups, gguf_variants, is_projector

GUIDE = json.loads(
    resource_files("darsay")
    .joinpath("collection_guide.json")
    .read_text(encoding="utf-8")
)


def bit_family(precision: str | None) -> int | None:
    """The bit width a GGUF encoding label names, or None when it names none.

    Mirrors ``precision.gguf_bits`` for the families the picker groups:
    ``Q`` and ``IQ`` levels by their digit, MXFP4 as four bits. Ternary and
    unfamiliar labels stay unknown rather than guessed.
    """
    label = (precision or "").upper()
    if label in {"BF16", "F16", "F32", "F64"}:
        return None
    if label.startswith("MXFP4"):
        return 4
    match = re.search(r"(?:^|[-_])I?Q([1-8])(?:_|$)", label)
    return int(match[1]) if match else None


def family(precision: str | None) -> str:
    if (precision or "").upper() in {"BF16", "F16", "F32", "F64"}:
        return "float"
    bits = bit_family(precision)
    return (
        "unknown"
        if bits is None
        else "compact"
        if bits <= 3
        else "middle"
        if bits <= 5
        else "wide"
    )


def starting_selection(variants: list[dict], intent: str) -> list[str]:
    """Select the smallest complete, known-size 4-bit / 4+8-bit options.

    No fallback guess when the named family is absent. The user can select
    any complete variant manually, including variants with unknown sizes.
    """
    if intent == "whole":
        return ["/*"]
    wanted = [4, 8] if intent == "compare" else [4]
    chosen = []
    for bits in wanted:
        options = [
            v
            for v in variants
            if v["complete"]
            and v["size_bytes"] is not None
            and bit_family(v["precision"]) == bits
        ]
        if options:
            chosen.extend(
                min(options, key=lambda v: (v["size_bytes"], v["name"]))["include"]
            )
    return sorted(set(chosen))


def publication(snapshot) -> dict:
    inventory = [{"path": f.path, "size": f.size} for f in snapshot.files]
    companions = []
    for group in gguf_groups(inventory):
        if is_projector(group["items"][0]["path"]):
            companions.append(
                {
                    "name": group["name"],
                    "precision": gguf_level_of(group["items"][0]["path"]),
                    "complete": group["complete"],
                    "include": group["include"],
                    "file_count": len(group["items"]),
                    "size_bytes": sum(f["size"] for f in group["items"])
                    if all(f["size"] is not None for f in group["items"])
                    else None,
                }
            )
    return {
        "source": snapshot.source.canonical,
        "revision": snapshot.revision,
        "files": inventory,
        "variants": gguf_variants(inventory),
        "companions": companions,
    }


def selection_totals(inventory: list[dict], include: list[str]) -> dict:
    selected = select_subset(inventory, include)[0] if include else []
    return {
        "bytes": sum(f["size"] or 0 for f in selected),
        "files": len(selected),
        "unknown": sum(f["size"] is None for f in selected),
    }
