"""Single-file bundle export/import (.mvb.tar).

An export is a deterministic tar of the whole bundle directory:

- entries rooted at `<bundle_id>/`, marker file `.mvb.json` first, then all
  files in sorted order;
- normalized tar metadata (mtime = the bundle's date_archived, uid/gid 0,
  mode 0644, no owner names), GNU format, no compression — model weights are
  essentially incompressible and a plain tar stays inspectable;
- so the same bundle state always produces byte-identical output, giving the
  export file a single stable SHA-256.

The marker carries only stable facts (format version, bundle id, bundle hash);
the export *event* (timestamp, path, tar sha256) is recorded in the source
bundle's exports.json, which is excluded from the tar for exactly that reason.

Import streams the marker, checks format compatibility, unpacks to a temp
directory with the safe 'data' filter, re-hashes the payload against the
embedded manifest, and only then registers the bundle in the vault.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from .hashing import bundle_hash, hash_file, iter_payload_files

# Bump major on incompatible layout changes; import requires a matching major.
MVB_FORMAT_VERSION = "1.0"
MARKER_NAME = ".mvb.json"

# Bundle-root files that never go into an export.
EXPORT_EXCLUDE = {"exports.json", ".DS_Store"}


def _bundle_files(bundle_dir: Path) -> list[tuple[str, Path]]:
    files = []
    for p in bundle_dir.rglob("*"):
        rel = p.relative_to(bundle_dir).as_posix()
        if p.name in EXPORT_EXCLUDE:
            continue
        if p.is_symlink():
            raise SystemExit(f"error: refusing to export symlink in bundle: {rel}")
        if p.is_file():
            files.append((rel, p))
    files.sort(key=lambda t: t[0])
    return files


def _tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name=name)
    ti.size = size
    ti.mtime = mtime
    ti.mode = 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def export_bundle(bundle_dir: Path, output_dir: Path, progress=print) -> Path:
    from .archiver import load_manifest, utc_now

    manifest = load_manifest(bundle_dir)
    bundle_id = manifest["bundle_id"]
    mtime = int(datetime.fromisoformat(manifest["archive"]["date_archived"]).timestamp())

    marker = {
        "mvb_format_version": MVB_FORMAT_VERSION,
        "bundle_id": bundle_id,
        "artifact_type": manifest["artifact_type"],
        "schema_version": manifest["schema_version"],
        "bundle_hash": manifest["inventory"]["bundle_hash"],
        "payload_file_count": manifest["inventory"]["file_count"],
        "payload_size_bytes": manifest["inventory"]["total_size_bytes"],
        "written_by": {"tool": "modelvault"},
    }
    marker_bytes = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{bundle_id}.mvb.tar"
    if out_path.exists():
        raise SystemExit(f"error: {out_path} already exists")

    files = _bundle_files(bundle_dir)
    progress(f"Exporting {bundle_id}: {len(files)} files -> {out_path}")
    with tarfile.open(out_path, "w", format=tarfile.GNU_FORMAT) as tar:
        import io

        tar.addfile(_tarinfo(f"{bundle_id}/{MARKER_NAME}", len(marker_bytes), mtime),
                    io.BytesIO(marker_bytes))
        for rel, abs_path in files:
            with open(abs_path, "rb") as f:
                tar.addfile(_tarinfo(f"{bundle_id}/{rel}", abs_path.stat().st_size, mtime), f)

    sha256 = hash_file(out_path, with_blake3=False)["sha256"]
    record = {
        "at": utc_now(),
        "file": str(out_path.resolve()),
        "sha256": sha256,
        "size_bytes": out_path.stat().st_size,
        "mvb_format_version": MVB_FORMAT_VERSION,
    }
    exports_path = bundle_dir / "exports.json"
    data = json.loads(exports_path.read_text(encoding="utf-8")) if exports_path.exists() else {"exports": []}
    data["exports"].append(record)
    exports_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    progress(f"Export sha256: {sha256}")
    progress(f"Recorded in {exports_path}")
    return out_path


def _read_marker(tar_path: Path) -> dict:
    with tarfile.open(tar_path, "r") as tar:
        first = tar.next()
        if first is None or Path(first.name).name != MARKER_NAME:
            raise SystemExit(f"error: {tar_path} is not a modelvault export (missing leading {MARKER_NAME})")
        marker = json.load(tar.extractfile(first))
    major = str(marker.get("mvb_format_version", "")).split(".")[0]
    if major != MVB_FORMAT_VERSION.split(".")[0]:
        raise SystemExit(
            f"error: export format {marker.get('mvb_format_version')} not supported "
            f"(this tool reads {MVB_FORMAT_VERSION.split('.')[0]}.x)"
        )
    return marker


def import_bundle(tar_path: Path, vault: Path, force: bool = False, progress=print) -> Path:
    from .archiver import load_manifest, utc_now, write_manifest
    from .verify import verify_bundle

    marker = _read_marker(tar_path)
    bundle_id = marker["bundle_id"]
    name, _, rev = bundle_id.partition("@")
    if not rev:
        raise SystemExit(f"error: malformed bundle_id in marker: {bundle_id}")
    dest = vault / name / rev
    if dest.exists() and not force:
        raise SystemExit(f"error: {dest} already exists (use --force to replace)")

    file_sha256 = hash_file(tar_path, with_blake3=False)["sha256"]
    progress(f"Importing {bundle_id} (format {marker['mvb_format_version']}, file sha256 {file_sha256[:16]}…)")

    staging = vault / name / f".import-{rev}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(tar_path, "r") as tar:
            tar.extractall(staging, filter="data")
        root = staging / bundle_id
        if not (root / "manifest.json").is_file():
            raise SystemExit("error: export contains no manifest.json")

        manifest = load_manifest(root)
        if manifest["bundle_id"] != bundle_id:
            raise SystemExit(f"error: marker/manifest bundle_id mismatch: {bundle_id} vs {manifest['bundle_id']}")

        progress("Verifying payload against embedded manifest ...")
        expected = {r["path"]: r["sha256"] for r in manifest["inventory"]["files"]}
        actual = {}
        for rel, abs_path in iter_payload_files(root / "model"):
            actual[f"model/{rel}"] = hash_file(abs_path, with_blake3=False)["sha256"]
        bad = sorted(p for p in expected if actual.get(p) != expected[p])
        extra = sorted(set(actual) - set(expected))
        recomputed = bundle_hash([{"path": p, "sha256": s} for p, s in actual.items()])
        if bad or extra or recomputed["value"] != marker["bundle_hash"]["value"]:
            raise SystemExit(
                "error: import verification FAILED — bundle not registered "
                f"({len(bad)} bad/missing, {len(extra)} extra, "
                f"bundle_hash_match={recomputed['value'] == marker['bundle_hash']['value']})"
            )

        now = utc_now()
        manifest["archive"]["location"] = str(dest.resolve())
        import socket

        manifest["archive"]["host"] = socket.gethostname()
        manifest["archive"]["last_integrity_check"] = now
        manifest["archive"]["last_accessed"] = now
        manifest["archive"]["imported"] = {
            "at": now,
            "from_file": str(tar_path.resolve()),
            "file_sha256": file_sha256,
            "mvb_format_version": marker["mvb_format_version"],
        }
        write_manifest(root, manifest)

        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        root.rename(dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.parent.rmdir()  # drop vault/<name>/ if a failed import left it empty
        except OSError:
            pass

    # Full verify stamps VERIFICATION.md / verification.json at the new location.
    verify_bundle(dest, progress=progress)
    progress(f"Imported to {dest}")
    return dest
