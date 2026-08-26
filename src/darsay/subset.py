"""Subset selection for estimate --include / archive --include.

A glob keeps matching payload files. Small sidecar files (card, license,
config, tokenizer) are kept as well so a GGUF quant glob still yields a
loadable bundle. The full upstream inventory is recorded so the bundle
states exactly what it left out.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath

# Basename globs for files that are never the reason you passed --include
# but that a loadable / honest bundle still wants next to the matched weights.
SIDECAR_GLOBS = (
    "README",
    "README.*",
    "LICENSE",
    "LICENSE.*",
    "LICENCE",
    "LICENCE.*",
    "COPYING",
    "COPYING.*",
    "license",
    "license.*",
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template*",
    "preprocessor_config.json",
    "spiece.model",
    "dataset_infos.json",
)


def _path_of(item) -> str:
    return item.path if hasattr(item, "path") else item["path"]


def _size_of(item) -> int | None:
    return item.size if hasattr(item, "size") else item.get("size")


def _sha256_of(item) -> str | None:
    if hasattr(item, "sha256"):
        return item.sha256
    return item.get("sha256") or item.get("lfs_sha256")


def _git_sha1_of(item) -> str | None:
    if hasattr(item, "git_sha1"):
        return item.git_sha1
    return item.get("git_sha1")


def _basename(path: str) -> str:
    return PurePosixPath(path).name


def matches_include(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, pat) or fnmatch(_basename(path), pat) for pat in patterns)


def is_sidecar(path: str) -> bool:
    name = _basename(path)
    lowered = name.lower()
    return any(
        fnmatch(name, pat) or fnmatch(lowered, pat.lower()) for pat in SIDECAR_GLOBS
    )


def file_record(item) -> dict:
    return {
        "path": _path_of(item),
        "size": _size_of(item),
        "sha256": _sha256_of(item),
        "git_sha1": _git_sha1_of(item),
    }


def select_subset(
    files, include: list[str], *, sidecars: bool = True
) -> tuple[list, dict]:
    """Return ``(kept_items, subset_record)``.

    Raises ``SystemExit`` when no file matches an include glob (sidecars
    alone are not a subset).
    """
    files = list(files)
    matched = [item for item in files if matches_include(_path_of(item), include)]
    if not matched:
        patterns = ", ".join(include)
        raise SystemExit(
            f"error: --include matched no payload files (patterns: {patterns})"
        )
    kept_paths = {_path_of(item) for item in matched}
    kept = list(matched)
    sidecar_count = 0
    if sidecars:
        for item in files:
            path = _path_of(item)
            if path in kept_paths:
                continue
            if is_sidecar(path):
                kept.append(item)
                kept_paths.add(path)
                sidecar_count += 1
    kept.sort(key=lambda item: _path_of(item))
    full_sorted = sorted(files, key=_path_of)
    subset = {
        "include": list(include),
        "sidecars": bool(sidecars),
        "sidecar_file_count": sidecar_count,
        "full_file_count": len(files),
        "full_total_size_bytes": sum(_size_of(item) or 0 for item in files),
        "kept_file_count": len(kept),
        "kept_total_size_bytes": sum(_size_of(item) or 0 for item in kept),
        "omitted_file_count": len(files) - len(kept),
        "full_files": [file_record(item) for item in full_sorted],
    }
    return kept, subset
