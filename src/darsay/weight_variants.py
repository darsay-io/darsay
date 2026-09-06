"""GGUF variants from a file inventory, independent of preservation verdicts.

A split GGUF is one variant, not one model per shard. Names establish
grouping and precision labels only; they never establish negative/print
status. Projectors are companions, not alternative copies of the model.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .precision import gguf_level_of
from .subset import matches_include

_SHARD = re.compile(r"^(.*)-(\d+)-of-(\d+)(\.gguf)$", re.IGNORECASE)
_PROJECTOR = re.compile(r"^mmproj(?:[-_.]|$)", re.IGNORECASE)


def is_projector(path: str) -> bool:
    return bool(_PROJECTOR.match(PurePosixPath(path).name))


def _literal_glob(text: str) -> str:
    return "".join({"[": "[[]", "*": "[*]", "?": "[?]"}.get(c, c) for c in text)


def gguf_groups(files: list[dict]) -> list[dict]:
    """Group shards by directory, name and declared shard count.

    A complete group has every shard exactly once. A malformed or missing
    shard stays visible as incomplete. Selectors are checked against the
    entire inventory before a compact glob is used.
    """
    groups: dict[tuple[str, str], dict] = {}
    for item in files:
        path = item["path"]
        if not path.lower().endswith(".gguf"):
            continue
        m = _SHARD.match(path)
        key = (f"{m[1]}-of-{m[3]}{m[4]}", "sharded") if m else (path, "single")
        group = groups.setdefault(key, {"items": [], "shards": [], "match": m})
        group["items"].append(item)
        if m:
            group["shards"].append(int(m[2]))
    out = []
    for key, group in sorted(groups.items()):
        items = sorted(group["items"], key=lambda f: f["path"])
        paths = {f["path"] for f in items}
        m = group["match"]
        count = int(m[3]) if m else 1
        complete = len(items) == len(paths) and (
            sorted(group["shards"]) == list(range(1, count + 1))
            if m and count <= len(items)
            else not m
        )
        include = ["/" + _literal_glob(f["path"]) for f in items]
        if m:
            pattern = "/" + _literal_glob(m[1]) + "-*-of-" + _literal_glob(m[3] + m[4])
            if {
                f["path"] for f in files if matches_include(f["path"], [pattern])
            } == paths:
                include = [pattern]
        out.append(
            {
                "name": (m[1] if m else key[0][:-5]),
                "items": items,
                "complete": complete,
                "include": include,
            }
        )
    return out


def gguf_variants(files: list[dict]) -> list[dict]:
    """Model variants with full shard totals and selectors; no projectors.

    ``imatrix`` is ``None`` here: whether a variant's headers name an
    importance matrix is read by classification (``mark_imatrix``), never
    from the file name.
    """
    return [
        {
            "name": group["name"],
            "precision": gguf_level_of(group["items"][0]["path"]),
            "file_count": len(group["items"]),
            "size_bytes": sum(item["size"] for item in group["items"])
            if all(item.get("size") is not None for item in group["items"])
            else None,
            "complete": group["complete"],
            "include": group["include"],
            "imatrix": None,
        }
        for group in gguf_groups(files)
        if not is_projector(group["items"][0]["path"])
    ]


def mark_imatrix(
    variants: list[dict], files: list[dict], classification: dict | None
) -> list[dict]:
    """Say, per variant, whether its GGUF headers name an importance matrix.

    ``classification`` is a full classification result: its ``sets`` carry
    ``evidence.headers`` per path with the ``imatrix`` fact the header
    read established. A variant whose every file was read is ``True`` when
    any header carries ``quantize.imatrix.*`` (rule R7's test) and ``False``
    when none does; a variant with an unread file stays ``None``. Nothing
    here reads a name.
    """
    headers: dict[str, bool] = {}
    for weight_set in (classification or {}).get("sets") or []:
        if weight_set.get("kind") != "gguf":
            continue
        evidence = (weight_set.get("evidence") or {}).get("headers") or {}
        for path, fact in evidence.items():
            if isinstance(fact, dict) and isinstance(fact.get("imatrix"), bool):
                headers[path] = fact["imatrix"]
    out = []
    for variant in variants:
        paths = [
            f["path"] for f in files if matches_include(f["path"], variant["include"])
        ]
        known = [headers.get(p) for p in paths]
        imatrix = None if not paths or any(k is None for k in known) else any(known)
        out.append({**variant, "imatrix": imatrix})
    return out


def model_weight_bytes(files: list[dict]) -> int | None:
    """Bytes for one complete GGUF variant, or one non-GGUF weight set.

    Pack totals, incomplete shards, and projector-only selections cannot
    describe a single model's bytes per parameter or memory requirement.
    Neither can several weight sets summed over one parameter count: the
    non-GGUF set is the files of one directory in one format, so a
    ``transformer/`` beside a ``vae/`` and a ``text_encoder/``, or a
    ``.pth`` beside safetensors, is a redundancy, not a width.
    """
    if files and all(f["path"].lower().endswith(".gguf") for f in files):
        variants = gguf_variants(files)
        if len(variants) != 1 or not variants[0]["complete"]:
            return None
        return variants[0]["size_bytes"]
    if not files or any(f.get("size") is None for f in files):
        return None
    if any(f["path"].lower().endswith(".gguf") for f in files):
        return None
    directories = {str(PurePosixPath(f["path"]).parent) for f in files}
    suffixes = {PurePosixPath(f["path"]).suffix.lower() for f in files}
    if len(directories) != 1 or len(suffixes) != 1:
        return None
    return sum(f["size"] for f in files)
