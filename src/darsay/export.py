"""Single-file bundle export/import (.mvb.tar).

An export is a deterministic tar of the whole bundle directory:

- entries rooted at `<bundle_id>/`, marker file `.mvb.json` first, then all
  files in sorted order (including a frozen `darsay-verify.py`);
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

import io
import json
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from . import SCHEMA_VERSION
from .hashing import bundle_hash, hash_file, iter_payload_files
from .vault import prune_empty_parent

# Bump major on incompatible layout changes; import requires a matching major.
# Minor 1.2 adds a frozen copy of standalone_verify.py as darsay-verify.py.
MVB_FORMAT_VERSION = "1.2"
MARKER_NAME = ".mvb.json"
STANDALONE_VERIFY_NAME = "darsay-verify.py"

# Bundle-root files that never go into an export: volatile machine-local state
# (export log, hydration/run records, resumable-transfer ledger/lock).
EXPORT_EXCLUDE = {"exports.json", "hydration.json", "transfer.json", "transfer.lock"}


def standalone_verify_bytes() -> bytes:
    """Canonical stdlib verifier copied into every export. Do not transform."""
    return Path(__file__).with_name("standalone_verify.py").read_bytes()


def _bundle_files(bundle_dir: Path) -> list[tuple[str, Path]]:
    files = []
    for p in bundle_dir.rglob("*"):
        rel = p.relative_to(bundle_dir).as_posix()
        if rel in EXPORT_EXCLUDE or p.name == ".DS_Store":
            continue
        # Always inject the canonical verifier; ignore an on-disk copy
        # (imports unpack one into the bundle).
        if rel == STANDALONE_VERIFY_NAME:
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


def export_bundle(
    bundle_dir: Path, output_dir: Path, progress=print, *, dry_run: bool = False
) -> Path:
    """Pack one bundle into ``<output_dir>/<bundle_id>.mvb.tar``.

    ``dry_run`` runs the same checks, prints what would be packed and where,
    and writes nothing — not the tar, not ``exports.json``.
    """
    from .archiver import load_manifest, utc_now

    manifest = load_manifest(bundle_dir)
    bundle_id = manifest["bundle_id"]
    mtime = int(
        datetime.fromisoformat(manifest["archive"]["date_archived"]).timestamp()
    )

    marker = {
        "mvb_format_version": MVB_FORMAT_VERSION,
        "bundle_id": bundle_id,
        "artifact_type": manifest["artifact_type"],
        "schema_version": manifest["schema_version"],
        "bundle_hash": manifest["inventory"]["bundle_hash"],
        "payload_file_count": manifest["inventory"]["file_count"],
        "payload_size_bytes": manifest["inventory"]["total_size_bytes"],
        "written_by": {"tool": "darsay"},
    }
    marker_bytes = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")

    out_path = output_dir / f"{bundle_id}.mvb.tar"
    if out_path.exists():
        raise SystemExit(f"error: {out_path} already exists")

    files = _bundle_files(bundle_dir)
    verifier = standalone_verify_bytes()
    members: list[tuple[str, Path | None, bytes | None]] = [
        (STANDALONE_VERIFY_NAME, None, verifier),
        *((rel, abs_path, None) for rel, abs_path in files),
    ]
    members.sort(key=lambda t: t[0])
    exports_path = bundle_dir / "exports.json"
    if dry_run:
        from .readme_gen import human_size

        total = (
            len(marker_bytes)
            + len(verifier)
            + sum(abs_path.stat().st_size for _rel, abs_path in files)
        )
        excluded = sorted(
            name for name in EXPORT_EXCLUDE if (bundle_dir / name).exists()
        )
        progress(
            f"Would export {bundle_id}: {len(members)} files, {human_size(total)} "
            f"-> {out_path}"
        )
        if excluded:
            progress(
                f"  excluded:  {', '.join(excluded)}  (machine-local; never exported)"
            )
        progress(f"  then:      record the tar's sha256 and size in {exports_path}")
        return out_path
    output_dir.mkdir(parents=True, exist_ok=True)
    progress(f"Exporting {bundle_id}: {len(members)} files -> {out_path}")
    with tarfile.open(out_path, "w", format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(
            _tarinfo(f"{bundle_id}/{MARKER_NAME}", len(marker_bytes), mtime),
            io.BytesIO(marker_bytes),
        )
        for rel, abs_path, data in members:
            if data is not None:
                tar.addfile(
                    _tarinfo(f"{bundle_id}/{rel}", len(data), mtime),
                    io.BytesIO(data),
                )
            elif abs_path is not None:
                with open(abs_path, "rb") as f:
                    tar.addfile(
                        _tarinfo(f"{bundle_id}/{rel}", abs_path.stat().st_size, mtime),
                        f,
                    )

    from . import __version__

    sha256 = hash_file(out_path, with_blake3=False)["sha256"]
    record = {
        "at": utc_now(),
        "file": str(out_path.resolve()),
        "sha256": sha256,
        "size_bytes": out_path.stat().st_size,
        "mvb_format_version": MVB_FORMAT_VERSION,
        "written_by": {"tool": "darsay", "version": __version__},
    }
    data = (
        json.loads(exports_path.read_text(encoding="utf-8"))
        if exports_path.exists()
        else {"exports": []}
    )
    data["exports"].append(record)
    exports_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    progress(f"Export sha256: {sha256}")
    progress(f"Recorded in {exports_path}")
    return out_path


def _read_marker(tar_path: Path) -> dict:
    with tarfile.open(tar_path, "r") as tar:
        first = tar.next()
        if first is None or Path(first.name).name != MARKER_NAME:
            raise SystemExit(
                f"error: {tar_path} is not a darsay export (missing leading {MARKER_NAME})"
            )
        marker = json.load(tar.extractfile(first))
    major = str(marker.get("mvb_format_version", "")).split(".")[0]
    if major != MVB_FORMAT_VERSION.split(".")[0]:
        raise SystemExit(
            f"error: export format {marker.get('mvb_format_version')} not supported "
            f"(this tool reads {MVB_FORMAT_VERSION.split('.')[0]}.x)"
        )
    from .schema import MANIFEST_SCHEMA_MAJOR, parse_schema_major

    embedded = marker.get("schema_version")
    if not embedded:
        raise SystemExit("error: export marker schema_version missing")
    try:
        schema_major = parse_schema_major(embedded)
    except ValueError:
        raise SystemExit(f"error: export marker schema_version {embedded!r}") from None
    if schema_major > MANIFEST_SCHEMA_MAJOR:
        raise SystemExit(
            f"error: embedded manifest schema {embedded} is newer than this "
            f"darsay (reads {MANIFEST_SCHEMA_MAJOR}.x) — upgrade darsay to import it"
        )
    # An earlier major imports: the payload verifies against the embedded
    # inventory (its shape has never changed), then the record is migrated
    # before it registers.
    return marker


def import_bundle(
    tar_path: Path,
    vault: Path,
    force: bool = False,
    progress=print,
    *,
    dry_run: bool = False,
) -> Path:
    """Unpack, re-hash, and only then register an export in ``vault``.

    ``dry_run`` reads the marker, runs the same refusals, prints what would
    land where, and unpacks nothing.
    """
    from .archiver import load_manifest, read_manifest, utc_now, write_manifest
    from .migrate import migrate_bundle, migration_plan, record_status
    from .verify import verify_bundle

    marker = _read_marker(tar_path)
    older_record = record_status(marker["schema_version"]) == "older"
    bundle_id = marker["bundle_id"]
    name, _, rev = bundle_id.partition("@")
    if not rev:
        raise SystemExit(f"error: malformed bundle_id in marker: {bundle_id}")
    dest = vault / name / rev
    if dest.exists() and not force:
        raise SystemExit(f"error: {dest} already exists (use --force to replace)")
    if dry_run:
        from .readme_gen import human_size

        count = marker.get("payload_file_count")
        progress(
            f"Would import {bundle_id} ({marker.get('artifact_type') or 'bundle'}, "
            f"format {marker['mvb_format_version']}) from {tar_path} "
            f"({human_size(tar_path.stat().st_size)})"
        )
        progress(
            f"  destination: {dest}  "
            + ("(exists — --force replaces it)" if dest.exists() else "(new)")
        )
        progress(
            f"  payload:     {'?' if count is None else count} files, "
            f"{human_size(marker.get('payload_size_bytes'))} — every file re-hashed "
            "against the embedded manifest before it registers"
        )
        if older_record:
            progress(
                f"  record:      schema {marker['schema_version']} — migrated to "
                f"{SCHEMA_VERSION} before it registers (payload untouched)"
            )
        progress(
            "  then:        stamp archive.imported, move into the vault, verify there"
        )
        return dest

    file_sha256 = hash_file(tar_path, with_blake3=False)["sha256"]
    progress(
        f"Importing {bundle_id} (format {marker['mvb_format_version']}, file sha256 {file_sha256[:16]}…)"
    )

    staging = vault / name / f".import-{rev}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(tar_path, "r") as tar:
            tar.extractall(staging, filter="data")
        root = staging / bundle_id
        if not (root / "manifest.json").is_file():
            raise SystemExit("error: export contains no manifest.json")

        manifest = read_manifest(root)
        if manifest["bundle_id"] != bundle_id:
            raise SystemExit(
                f"error: marker/manifest bundle_id mismatch: {bundle_id} vs {manifest['bundle_id']}"
            )

        progress("Verifying payload against embedded manifest ...")
        from .schema import payload_root

        payload_name = payload_root(manifest)
        expected = {r["path"]: r["sha256"] for r in manifest["inventory"]["files"]}
        actual = {}
        for rel, abs_path in iter_payload_files(root / payload_name):
            actual[f"{payload_name}/{rel}"] = hash_file(abs_path, with_blake3=False)[
                "sha256"
            ]
        bad = sorted(p for p in expected if actual.get(p) != expected[p])
        extra = sorted(set(actual) - set(expected))
        recomputed = bundle_hash(
            [{"path": p, "sha256": s} for p, s in actual.items()], payload_name
        )
        if bad or extra or recomputed["value"] != marker["bundle_hash"]["value"]:
            raise SystemExit(
                "error: import verification FAILED — bundle not registered "
                f"({len(bad)} bad/missing, {len(extra)} extra, "
                f"bundle_hash_match={recomputed['value'] == marker['bundle_hash']['value']})"
            )

        if older_record:
            # Verified as recorded; now the record itself moves forward.
            plan = migration_plan(root)
            plan["path"] = str(dest)
            migrate_bundle(root, progress=progress, plan=plan)
            manifest = load_manifest(root)

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
        # Drop vault/<name>/ if a failed import left it empty.
        prune_empty_parent(staging)

    # Full verify stamps VERIFICATION.md / verification.json at the new location.
    verify_bundle(dest, progress=progress)
    progress(f"Imported to {dest}")
    return dest
