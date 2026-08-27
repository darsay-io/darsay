#!/usr/bin/env python3
"""Stdlib-only payload verifier for a darsay bundle.

Re-hashes every payload file against ``manifest.json``. No ``huggingface_hub``,
no ``blake3``, no darsay imports. Read-only: never writes the manifest or
verification reports.

This file is copied verbatim into every ``.mvb.tar`` as ``darsay-verify.py``.
Changing it is an MVB format minor bump (see ``export.MVB_FORMAT_VERSION``).

Usage:

    python3 darsay-verify.py [PATH]

PATH is a bundle directory or a ``.mvb.tar``. Default: current directory.
Exit 0 on pass, 1 on fail, 2 on usage/format error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile

CHUNK_SIZE = 1024 * 1024


class VerifyError(Exception):
    """PATH is not a readable bundle or export."""


def hash_stream(fh):
    digest = hashlib.sha256()
    while True:
        chunk = fh.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def payload_root_of(manifest):
    layout = (manifest.get("inventory") or {}).get("layout") or {}
    return (layout.get("payload_root") or "model/").rstrip("/")


def bundle_hash_value(file_records):
    lines = [
        f"{record['sha256']}  {record['path']}"
        for record in sorted(file_records, key=lambda r: r["path"])
    ]
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _is_cache(rel_parts):
    return bool(rel_parts) and rel_parts[0] == ".cache"


def hash_payload_dir(bundle_dir, payload_root):
    payload_dir = os.path.join(bundle_dir, payload_root)
    actual = {}
    if not os.path.isdir(payload_dir):
        return actual
    for dirpath, dirnames, filenames in os.walk(payload_dir):
        rel_dir = os.path.relpath(dirpath, payload_dir).replace(os.sep, "/")
        if rel_dir in (".", os.curdir):
            dirnames[:] = [d for d in dirnames if d != ".cache"]
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, payload_dir).replace(os.sep, "/")
            if _is_cache(rel.split("/")):
                continue
            with open(abs_path, "rb") as fh:
                sha256 = hash_stream(fh)
            path = f"{payload_root}/{rel}"
            actual[path] = {"sha256": sha256, "size": os.path.getsize(abs_path)}
    return actual


def hash_payload_tar(tar, prefix, payload_root):
    actual = {}
    payload_prefix = f"{payload_root}/"
    member_prefix = f"{prefix}/" if prefix else ""
    for member in tar.getmembers():
        if not member.isfile():
            continue
        name = member.name
        if member_prefix:
            if name == prefix or not name.startswith(member_prefix):
                continue
            rel = name[len(member_prefix) :]
        else:
            rel = name
        if not rel.startswith(payload_prefix):
            continue
        inner = rel[len(payload_prefix) :]
        if _is_cache(inner.split("/")):
            continue
        fh = tar.extractfile(member)
        if fh is None:
            continue
        with fh:
            sha256 = hash_stream(fh)
        actual[rel] = {"sha256": sha256, "size": member.size}
    return actual


def compare(expected_files, actual, expected_bundle_hash):
    expected = {record["path"]: record for record in expected_files}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        path
        for path in set(expected) & set(actual)
        if expected[path]["sha256"] != actual[path]["sha256"]
    )
    recomputed = bundle_hash_value(
        [{"path": path, "sha256": info["sha256"]} for path, info in actual.items()]
    )
    bundle_hash_match = recomputed == expected_bundle_hash
    status = (
        "pass" if not (missing or extra or mismatched) and bundle_hash_match else "fail"
    )
    return {
        "status": status,
        "files_checked": len(actual),
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "bundle_hash_match": bundle_hash_match,
        "bundle_hash": recomputed,
    }


def _load_json_bytes(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError("manifest.json is not valid JSON") from exc


def _inventory(manifest):
    inventory = manifest.get("inventory")
    if not isinstance(inventory, dict):
        raise VerifyError("manifest.json has no inventory object")
    files = inventory.get("files")
    if not isinstance(files, list):
        raise VerifyError("manifest inventory.files is missing")
    bundle_hash = inventory.get("bundle_hash") or {}
    value = bundle_hash.get("value")
    if not value:
        raise VerifyError("manifest inventory.bundle_hash.value is missing")
    return files, value


def verify_dir(bundle_dir):
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise VerifyError(f"no manifest.json in {bundle_dir}")
    with open(manifest_path, "rb") as fh:
        manifest = _load_json_bytes(fh.read())
    files, expected_hash = _inventory(manifest)
    root = payload_root_of(manifest)
    actual = hash_payload_dir(bundle_dir, root)
    result = compare(files, actual, expected_hash)
    result["target"] = bundle_dir
    return result


def _tar_prefix_and_manifest(tar):
    names = tar.getnames()
    candidates = []
    for name in names:
        base = name.split("/")[-1]
        if base == "manifest.json":
            parent = name[: -len("manifest.json")].rstrip("/")
            depth = 0 if not parent else parent.count("/") + 1
            candidates.append((depth, name, parent))
    if not candidates:
        raise VerifyError("export contains no manifest.json")
    candidates.sort()
    _depth, manifest_name, prefix = candidates[0]
    fh = tar.extractfile(manifest_name)
    if fh is None:
        raise VerifyError(f"could not read {manifest_name}")
    with fh:
        manifest = _load_json_bytes(fh.read())
    return prefix, manifest


def verify_tar(tar_path):
    try:
        with tarfile.open(tar_path, "r") as tar:
            prefix, manifest = _tar_prefix_and_manifest(tar)
            files, expected_hash = _inventory(manifest)
            root = payload_root_of(manifest)
            actual = hash_payload_tar(tar, prefix, root)
    except tarfile.TarError as exc:
        raise VerifyError(f"{tar_path} is not a readable tar") from exc
    result = compare(files, actual, expected_hash)
    result["target"] = tar_path
    return result


def verify_path(path):
    if not os.path.exists(path):
        raise VerifyError(f"{path} does not exist")
    if os.path.isdir(path):
        return verify_dir(path)
    if tarfile.is_tarfile(path):
        return verify_tar(path)
    raise VerifyError(f"{path} is not a bundle directory or tar")


def format_result(result):
    lines = []
    if result["status"] == "pass":
        lines.append(f"pass: {result['files_checked']} files, bundle hash matches")
        return "\n".join(lines)
    parts = [
        f"fail: {result['files_checked']} files checked",
        f"{len(result['mismatched'])} mismatched",
        f"{len(result['missing'])} missing",
        f"{len(result['extra'])} extra",
    ]
    parts.append(
        "bundle hash matches"
        if result["bundle_hash_match"]
        else "bundle hash does not match"
    )
    lines.append(", ".join(parts))
    for key in ("mismatched", "missing", "extra"):
        if result[key]:
            lines.append(f"{key}:")
            lines.extend(f"  {path}" for path in result[key])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Re-hash a darsay bundle (directory or .mvb.tar) against its "
            "manifest. Stdlib only. Read-only."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="bundle directory or .mvb.tar (default: current directory)",
    )
    args = parser.parse_args(argv)
    path = os.path.abspath(args.path)
    try:
        result = verify_path(path)
    except VerifyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(format_result(result) + "\n")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
