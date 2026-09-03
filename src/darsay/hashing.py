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


def hash_file(
    path: Path,
    with_blake3: bool = True,
    with_git_sha1: bool = False,
    interrupt_check=None,
    on_bytes=None,
) -> dict:
    """Hash one file in a single pass. Returns {"sha256": ..., "blake3": ...?, "git_sha1": ...?}.

    ``interrupt_check`` is called every 32 MiB; it may raise to abandon the
    hash (the file on disk is untouched and can simply be re-hashed later).
    ``on_bytes(n, total)`` is called after every chunk so a live panel can
    show hash throughput instead of looking hung.
    """
    sha256 = hashlib.sha256()
    b3 = _blake3.blake3() if (with_blake3 and HAVE_BLAKE3) else None
    git = None
    total = path.stat().st_size
    if with_git_sha1:
        git = hashlib.sha1()
        git.update(b"blob %d\0" % total)

    chunks = 0
    seen = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            seen += len(chunk)
            chunks += 1
            if interrupt_check is not None and chunks % 32 == 0:
                interrupt_check()
            if on_bytes is not None:
                on_bytes(seen, total)
            sha256.update(chunk)
            if b3 is not None:
                b3.update(chunk)
            if git is not None:
                git.update(chunk)
    if on_bytes is not None and seen == 0:
        on_bytes(0, total)

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
        # provider transfer caches; not part of the payload
        if rel.parts and rel.parts[0] == ".cache":
            continue
        files.append((rel.as_posix(), p))
    files.sort(key=lambda t: t[0])
    return files


SHA256SUMS_NAME = "SHA256SUMS"


def sha256sums_text(file_records: list[dict]) -> str:
    """The payload's hash list in coreutils format, sorted by path.

    One ``<sha256>  <path>`` line per payload file, paths from the bundle
    root, trailing newline. ``sha256sum -c SHA256SUMS`` from the bundle
    root verifies the payload with no darsay and no Python, and the
    bundle hash is the sha256 of exactly this text — so ``sha256sum
    SHA256SUMS`` is the value the manifest records.
    """
    return (
        "\n".join(
            f"{r['sha256']}  {r['path']}"
            for r in sorted(file_records, key=lambda r: r["path"])
        )
        + "\n"
    )


def write_sha256sums(bundle_dir: Path, manifest: dict) -> Path:
    """Write the bundle's ``SHA256SUMS`` from its recorded inventory."""
    path = bundle_dir / SHA256SUMS_NAME
    path.write_text(sha256sums_text(manifest["inventory"]["files"]), encoding="utf-8")
    return path


def bundle_hash(file_records: list[dict], payload_root: str = "model") -> dict:
    """Deterministic hash over the payload: sha256 of the ``SHA256SUMS`` text."""
    digest = hashlib.sha256(sha256sums_text(file_records).encode("utf-8")).hexdigest()
    return {
        "algorithm": "sha256-of-sorted-sha256-lines",
        "value": digest,
        "covers": f"{payload_root.rstrip('/')}/ payload only; bundle-root metadata files are mutable and excluded",
    }
