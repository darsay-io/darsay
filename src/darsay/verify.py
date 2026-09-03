"""Re-verify an archived bundle against its own manifest.

Re-hashes every payload file, compares against the recorded inventory, checks
completeness, recomputes the bundle hash, and records the outcome in the
manifest (validation, archive.last_integrity_check, security flags), in
verification.json (history), and VERIFICATION.md (human report).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .hashing import bundle_hash, write_sha256sums
from .schema import check_completeness, payload_root

MAX_HISTORY = 50


def _utc_now():
    from .archiver import utc_now

    return utc_now()


def verify_bundle(bundle_dir: Path, progress=print) -> dict:
    """Re-hash every payload file where it lives and record the outcome."""
    from .archiver import load_manifest

    manifest = load_manifest(bundle_dir)
    actual = hash_payload(bundle_dir, manifest, progress=progress)
    report = record_verification(bundle_dir, actual, progress=progress)
    came_from = report.get("relocated_from")
    if came_from:
        progress(
            f"Location: {report['location']} on {report['host']}  "
            f"(the record said {came_from['location']} on {came_from['host']})"
        )
    return report


def hash_payload(
    bundle_dir: Path, manifest: dict, progress=print, *, vault: Path | None = None
) -> dict:
    """Every payload file's sha256 and size, keyed by its recorded path.

    Read wherever the disk actually is: on the host the vault's
    ``config.toml`` names (``[host]``), else here. ``vault`` defaults to
    the bundle's vault root, two levels up.
    """
    from .farside import far_side_label, hash_where_it_lives

    root = payload_root(manifest)
    payload_dir = bundle_dir / root
    vault = bundle_dir.parent.parent if vault is None else vault
    count = len(manifest["inventory"]["files"])
    where = far_side_label(vault)
    progress(
        f"Re-hashing {count} files {where} ..."
        if where
        else f"Re-hashing {count} files in {payload_dir} ..."
    )
    hashed = hash_where_it_lives(vault, payload_dir, progress=progress)
    return {f"{root}/{rel}": entry for rel, entry in hashed.items()}


def record_verification(
    bundle_dir: Path, actual: dict, progress=print, *, at: Path | None = None
) -> dict:
    """Compare hashes read where the payload lives against the record, and write it.

    ``actual`` is what ``hash_payload`` returns — or what ``darsay mv`` /
    ``cp`` gather in their one pass over a destination they land on. The
    outcome goes to the manifest (validation, ``archive.last_integrity_check``,
    security flags), ``verification.json`` (history), and ``VERIFICATION.md``.

    A verification is a fact about bytes at a path on a host, so
    ``archive.location`` / ``archive.host`` become where the payload was
    read: an rsync'd copy verified where it landed carries a true location
    without a ``mv``. The report says ``relocated_from`` when they changed.
    ``at`` names that path when it is not ``bundle_dir`` — a staging copy
    about to be renamed into place is recorded at its final home.
    """
    from .archiver import load_manifest, write_manifest

    manifest = load_manifest(bundle_dir)
    root = payload_root(manifest)
    expected = {r["path"]: r for r in manifest["inventory"]["files"]}

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        p
        for p in set(expected) & set(actual)
        if expected[p]["sha256"] != actual[p]["sha256"]
    )

    recomputed = bundle_hash(
        [{"path": p, "sha256": a["sha256"]} for p, a in actual.items()], root
    )
    bundle_hash_ok = (
        recomputed["value"] == manifest["inventory"]["bundle_hash"]["value"]
    )

    status = "pass" if not (missing or extra or mismatched) else "fail"
    now = _utc_now()
    checksum = {
        "at": now,
        "status": status,
        "files_checked": len(actual),
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "bundle_hash_match": bundle_hash_ok,
    }
    completeness = check_completeness(manifest["artifact_type"], list(actual))

    manifest["validation"]["checksum_verification"] = checksum
    manifest["validation"]["completeness"] = completeness
    manifest["archive"]["last_integrity_check"] = now
    manifest["archive"]["last_accessed"] = now

    changes = manifest["security"].setdefault("unexpected_changes", [])
    for kind, paths in (
        ("modified", mismatched),
        ("missing", missing),
        ("extra", extra),
    ):
        for p in paths:
            changes.append({"detected_at": now, "type": kind, "path": p})
    if status == "fail":
        manifest["security"]["integrity_status"] = "compromised"
    elif manifest["security"].get("integrity_status") != "upstream-mismatch":
        # Heal a previous compromise once the payload matches the inventory
        # again. Archive-time upstream-mismatch is a recorded fact and stays.
        manifest["security"]["integrity_status"] = "verified-against-upstream"

    here = str((at or bundle_dir).resolve())
    host = socket.gethostname()
    archive = manifest["archive"]
    relocated_from = None
    if archive.get("location") != here or archive.get("host") != host:
        relocated_from = {
            "location": archive.get("location"),
            "host": archive.get("host"),
        }
        archive["location"] = here
        archive["host"] = host
    write_manifest(bundle_dir, manifest)
    if relocated_from:
        from .readme_gen import write_bundle_readme

        write_bundle_readme(bundle_dir, manifest)
    # The hash list is a view of the inventory, which never changes; a
    # bundle from before it existed gains it here.
    write_sha256sums(bundle_dir, manifest)
    report = write_verification_report(bundle_dir, checksum, completeness)
    if relocated_from:
        report["relocated_from"] = relocated_from

    progress(
        f"Verification: {status.upper()} "
        f"({len(actual)} files; {len(mismatched)} modified, {len(missing)} missing, {len(extra)} extra)"
    )
    return report


def write_verification_report(
    bundle_dir: Path, checksum: dict, completeness: dict, first_run: bool = False
) -> dict:
    from .archiver import load_manifest

    manifest = load_manifest(bundle_dir)
    report = {
        "at": checksum["at"],
        "bundle_id": manifest["bundle_id"],
        "type": "initial-archive" if first_run else "re-verification",
        "location": manifest["archive"].get("location"),
        "host": manifest["archive"].get("host"),
        "checksum": checksum,
        "completeness": {
            "status": completeness.get("status"),
            "missing_required": completeness.get("missing_required", []),
            "missing_recommended": completeness.get("missing_recommended", []),
        },
        "result": "pass"
        if checksum["status"] == "pass" and completeness.get("status") == "complete"
        else "fail",
    }

    history_path = bundle_dir / "verification.json"
    if history_path.exists():
        data = json.loads(history_path.read_text(encoding="utf-8"))
    else:
        data = {"history": []}
    data["latest"] = report
    data["history"] = (data["history"] + [report])[-MAX_HISTORY:]
    history_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    _write_verification_md(bundle_dir, manifest, report, run_count=len(data["history"]))
    return report


def refresh_verification_md(bundle_dir: Path) -> None:
    """Rewrite ``VERIFICATION.md`` from ``verification.json`` in place.

    The Markdown is a derived view that names the bundle's path (its
    re-run line); after ``darsay mv`` the path changed and nothing else
    did, so the record is kept and only the view is regenerated.
    """
    from .archiver import load_manifest

    history_path = bundle_dir / "verification.json"
    if not history_path.is_file():
        return
    data = json.loads(history_path.read_text(encoding="utf-8"))
    report = data.get("latest")
    if not report:
        return
    _write_verification_md(
        bundle_dir,
        load_manifest(bundle_dir),
        report,
        run_count=len(data.get("history", [])),
    )


def _write_verification_md(
    bundle_dir: Path, manifest: dict, report: dict, run_count: int
) -> None:
    c = report["checksum"]
    lines = [
        f"# Verification report — {manifest['bundle_id']}",
        "",
        f"- **Result:** {report['result'].upper()}",
        f"- **Run at:** {report['at']}",
        f"- **Run type:** {report['type']} (run {run_count} recorded in verification.json)",
        f"- **Where:** `{report.get('location')}` on {report.get('host')}",
        f"- **Files checked:** {c['files_checked']}",
        f"- **Bundle hash:** `{manifest['inventory']['bundle_hash']['value']}`"
        + (
            ""
            if c.get("bundle_hash_match") is None
            else f" — recomputation {'matches' if c['bundle_hash_match'] else 'DOES NOT MATCH'}"
        ),
        f"- **Completeness:** {report['completeness']['status']}",
    ]
    if report["type"] == "initial-archive":
        mism = c.get("upstream_mismatches", [])
        lines.append(
            "- **Upstream cross-check:** "
            + (
                "all files match upstream LFS/git checksums"
                if not mism
                else f"MISMATCH on {len(mism)} files: {', '.join(mism)}"
            )
        )
    for key in ("missing", "extra", "mismatched"):
        if c.get(key):
            lines.append(f"\n## {key.capitalize()} files\n")
            lines += [f"- `{p}`" for p in c[key]]
    if report["completeness"]["missing_required"]:
        lines.append("\n## Missing required components\n")
        lines += [f"- {m}" for m in report["completeness"]["missing_required"]]
    if report["completeness"]["missing_recommended"]:
        lines.append("\n## Missing recommended components\n")
        lines += [f"- {m}" for m in report["completeness"]["missing_recommended"]]
    lines += [
        "",
        "---",
        "Re-run with: `darsay verify " + str(bundle_dir) + "`",
        "",
    ]
    (bundle_dir / "VERIFICATION.md").write_text("\n".join(lines), encoding="utf-8")
