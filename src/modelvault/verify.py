"""Re-verify an archived bundle against its own manifest.

Re-hashes every payload file, compares against the recorded inventory, checks
completeness, recomputes the bundle hash, and records the outcome in the
manifest (validation, archive.last_integrity_check, security flags), in
verification.json (history), and VERIFICATION.md (human report).
"""

from __future__ import annotations

import json
from pathlib import Path

from .hashing import bundle_hash, hash_file, iter_payload_files
from .schema import check_completeness

MAX_HISTORY = 50


def _utc_now():
    from .archiver import utc_now
    return utc_now()


def verify_bundle(bundle_dir: Path, progress=print) -> dict:
    from .archiver import load_manifest, write_manifest

    manifest = load_manifest(bundle_dir)
    payload_root = bundle_dir / "model"
    expected = {r["path"]: r for r in manifest["inventory"]["files"]}

    progress(f"Re-hashing {len(expected)} files in {payload_root} ...")
    actual = {}
    for rel, abs_path in iter_payload_files(payload_root):
        path = f"model/{rel}"
        actual[path] = {"sha256": hash_file(abs_path, with_blake3=False)["sha256"],
                        "size": abs_path.stat().st_size}

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        p for p in set(expected) & set(actual) if expected[p]["sha256"] != actual[p]["sha256"]
    )

    recomputed = bundle_hash([{"path": p, "sha256": a["sha256"]} for p, a in actual.items()])
    bundle_hash_ok = recomputed["value"] == manifest["inventory"]["bundle_hash"]["value"]

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
    for kind, paths in (("modified", mismatched), ("missing", missing), ("extra", extra)):
        for p in paths:
            changes.append({"detected_at": now, "type": kind, "path": p})
    if status == "fail":
        manifest["security"]["integrity_status"] = "compromised"

    write_manifest(bundle_dir, manifest)
    report = write_verification_report(bundle_dir, checksum, completeness)

    progress(f"Verification: {status.upper()} "
             f"({len(actual)} files; {len(mismatched)} modified, {len(missing)} missing, {len(extra)} extra)")
    return report


def write_verification_report(bundle_dir: Path, checksum: dict, completeness: dict,
                              first_run: bool = False) -> dict:
    from .archiver import load_manifest

    manifest = load_manifest(bundle_dir)
    report = {
        "at": checksum["at"],
        "bundle_id": manifest["bundle_id"],
        "type": "initial-archive" if first_run else "re-verification",
        "checksum": checksum,
        "completeness": {
            "status": completeness.get("status"),
            "missing_required": completeness.get("missing_required", []),
            "missing_recommended": completeness.get("missing_recommended", []),
        },
        "result": "pass" if checksum["status"] == "pass" and completeness.get("status") == "complete" else "fail",
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


def _write_verification_md(bundle_dir: Path, manifest: dict, report: dict, run_count: int) -> None:
    c = report["checksum"]
    lines = [
        f"# Verification report — {manifest['bundle_id']}",
        "",
        f"- **Result:** {report['result'].upper()}",
        f"- **Run at:** {report['at']}",
        f"- **Run type:** {report['type']} (run {run_count} recorded in verification.json)",
        f"- **Files checked:** {c['files_checked']}",
        f"- **Bundle hash:** `{manifest['inventory']['bundle_hash']['value']}`"
        + ("" if c.get("bundle_hash_match") is None
           else f" — recomputation {'matches' if c['bundle_hash_match'] else 'DOES NOT MATCH'}"),
        f"- **Completeness:** {report['completeness']['status']}",
    ]
    if report["type"] == "initial-archive":
        mism = c.get("upstream_mismatches", [])
        lines.append(f"- **Upstream cross-check:** "
                     + ("all files match upstream LFS/git checksums" if not mism
                        else f"MISMATCH on {len(mism)} files: {', '.join(mism)}"))
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
        "Re-run with: `modelvault verify " + str(bundle_dir) + "`",
        "",
    ]
    (bundle_dir / "VERIFICATION.md").write_text("\n".join(lines), encoding="utf-8")
