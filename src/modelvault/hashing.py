"""File and tree hashing for bundle inventories.

SHA-256 is the canonical hash recorded for every file. BLAKE3 is recorded
additionally when the `blake3` package is installed. For files that came from
a git-backed source we also compute the git blob SHA-1 so non-LFS files can be
cross-checked against upstream blob ids.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024

try:
    import blake3 as _blake3

    HAVE_BLAKE3 = True
except ImportError:
    _blake3 = None
    HAVE_BLAKE3 = False


def hash_file(path: Path, with_blake3: bool = True, with_git_sha1: bool = False) -> dict:
    """Hash one file in a single pass. Returns {"sha256": ..., "blake3": ...?, "git_sha1": ...?}."""
    sha256 = hashlib.sha256()
    b3 = _blake3.blake3() if (with_blake3 and HAVE_BLAKE3) else None
    git = None
    if with_git_sha1:
        git = hashlib.sha1()
        git.update(b"blob %d\0" % path.stat().st_size)

    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            sha256.update(chunk)
            if b3 is not None:
                b3.update(chunk)
            if git is not None:
                git.update(chunk)

    out = {"sha256": sha256.hexdigest()}
    if b3 is not None:
        out["blake3"] = b3.hexdigest()
    if git is not None:
        out["git_sha1"] = git.hexdigest()
    return out


def iter_payload_files(payload_root: Path):
    """Yield payload files sorted by relative POSIX path, skipping tool caches."""
    files = []
    for p in payload_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(payload_root)
        # snapshot_download bookkeeping; not part of the model itself
        if rel.parts and rel.parts[0] == ".cache":
            continue
        files.append((rel.as_posix(), p))
    files.sort(key=lambda t: t[0])
    return files


def bundle_hash(file_records: list[dict]) -> dict:
    """Deterministic hash over the payload: sha256 of 'sha256  path' lines, sorted by path."""
    lines = [f"{r['sha256']}  {r['path']}" for r in sorted(file_records, key=lambda r: r["path"])]
    digest = hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
    return {
        "algorithm": "sha256-of-sorted-sha256-lines",
        "value": digest,
        "covers": "model/ payload only; bundle-root metadata files are mutable and excluded",
    }
