"""Archive a source into a reproducible, auditable bundle.

Bundle layout:

    vault/<bundle-name>/<revision12>/
        model/            immutable payload: exact snapshot of the upstream repo
                          (data/ for dataset bundles — the registry's payload_root)
        manifest.json     machine-readable record (schema.py / SCHEMA_VERSION)
        README.md         human-readable summary, regenerable from the manifest
        VERIFICATION.md   latest verification report
        verification.json verification history
        curation.md       curator notes; the only file meant to be hand-edited

The payload is treated as immutable after archiving; the bundle hash covers it
alone. Metadata at the bundle root is mutable by design.

Acquisition is provider-dispatched (``sources.parse_source``). Hugging Face is
the first provider, not the archive format.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .hashing import bundle_hash
from .licensing import build_licensing_record
from .metadata import estimate_runtime, extract_dataset_metadata, extract_model_metadata
from .schema import (
    ARTIFACT_TYPES,
    BUNDLE_METADATA_FILES,
    MANIFEST_KIND,
    MANIFEST_SCHEMA_MAJOR,
    MANIFEST_TOP_KEYS,
    check_completeness,
    parse_schema_major,
    payload_root_for,
)
from .sources import (
    SourceGatedError,
    SourceNotFoundError,
    SourceRef,
    get_provider,
    parse_source,
    source_from_ledger,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_repo_ref(ref: str) -> tuple[str, str]:
    """Return ``(artifact_type, locator)``. Prefer ``parse_source`` for new code."""
    source = parse_source(ref)
    return source.artifact_type, source.locator


def hub_url(repo_id: str, repo_type: str = "model") -> str:
    """Hugging Face URL for a locator. Prefer ``parse_source(...).url``."""
    path = f"datasets/{repo_id}" if repo_type == "dataset" else repo_id
    return f"https://huggingface.co/{path}"


def bundle_name_for(repo_id: str, repo_type: str = "model") -> str:
    """Vault directory name for a Hugging Face-shaped locator.

    Prefer ``parse_source(...).bundle_name``. Hugging Face names are
    ``owner--name`` / ``datasets--owner--name``. Other providers include
    their id in the name.
    """
    loc = f"datasets/{repo_id}" if repo_type == "dataset" else repo_id
    return parse_source(loc).bundle_name


def bundle_dir_for(
    vault: Path, source: str | SourceRef, revision: str, repo_type: str | None = None
) -> Path:
    """Return the bundle directory for a source + pinned revision.

    Pass a SourceRef or source ref. ``repo_type`` qualifies a bare locator
    as a dataset when needed.
    """
    if isinstance(source, SourceRef):
        ref = source
    elif repo_type is not None:
        loc = f"datasets/{source}" if repo_type == "dataset" else source
        ref = parse_source(loc)
    else:
        ref = parse_source(source)
    return vault / ref.bundle_name / revision[:12]


def write_manifest(bundle_dir: Path, manifest: dict) -> None:
    """Write manifest.json. Known top-level keys first; unknown keys preserved."""
    payload = {key: manifest[key] for key in MANIFEST_TOP_KEYS if key in manifest}
    for key, value in manifest.items():
        if key not in MANIFEST_TOP_KEYS and not str(key).startswith("_"):
            payload[key] = value
    path = bundle_dir / "manifest.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_manifest(bundle_dir: Path) -> dict:
    """Read + validate. Major-newer than 1.x → SystemExit."""
    path = bundle_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"error: no manifest.json in {bundle_dir}") from None
    except OSError as exc:
        raise SystemExit(f"error: unreadable manifest at {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: unreadable manifest at {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"error: unreadable manifest at {path}: not a JSON object")
    version = data.get("schema_version")
    if not version:
        raise SystemExit(
            f"error: unreadable manifest at {path}: schema_version missing"
        )
    try:
        major = parse_schema_major(version)
    except ValueError:
        raise SystemExit(
            f"error: unreadable manifest at {path}: schema_version {version!r}"
        ) from None
    if major > MANIFEST_SCHEMA_MAJOR:
        raise SystemExit(
            f"error: manifest schema {version} is newer than this darsay "
            f"(supports {MANIFEST_SCHEMA_MAJOR}.x)"
        )
    kind = data.get("kind") or MANIFEST_KIND
    if kind != MANIFEST_KIND:
        raise SystemExit(
            f"error: unreadable manifest at {path}: kind is not {MANIFEST_KIND!r}"
        )
    data["kind"] = kind
    return data


def _guess_version(repo_name: str, model_type: str | None) -> str | None:
    # "Qwen3-0.6B" with model_type "qwen3" -> "3"
    if model_type:
        m = re.search(r"(\d+(?:\.\d+)?)$", model_type)
        if m:
            return m.group(1)
    m = re.search(r"[a-zA-Z](\d+(?:\.\d+)?)[-_]", repo_name)
    return m.group(1) if m else None


def _warn_include_vs_pin(include: list[str] | None, ledger: dict, progress) -> None:
    from .catalog import include_key

    pinned = (ledger.get("subset") or {}).get("include")
    if include_key(include) == include_key(pinned):
        return
    if pinned is None:
        progress(
            "WARNING: --include ignored; this pin is the full file set. "
            "Use --force to pin a subset."
        )
        return
    if include:
        progress(
            "WARNING: --include differs from the pinned subset "
            f"{pinned}; resuming the pin. Use --force to re-pin."
        )
        return
    raise SystemExit(
        f"error: this pin is a subset {pinned}; it is not the full repo\n"
        "  hint: pass matching --include to resume this pin\n"
        "  hint: --force re-pins the full file set"
    )


def archive(
    source: str | SourceRef,
    revision: str | None = None,
    vault: Path = Path("vault"),
    force: bool = False,
    dry_run: bool = False,
    max_bytes: int | None = None,
    max_minutes: float | None = None,
    rehash: bool = False,
    jobs: int = 4,
    shard: tuple[int, int] | None = None,
    include: list[str] | None = None,
    progress=print,
) -> Path | None:
    """Archive a source through pin → reconcile → transfer → register."""
    from .transfer import (
        CleanStop,
        LedgerError,
        PartialTransfer,
        StopController,
        add_disk_preflight,
        begin_session,
        find_resume,
        finish_session,
        load_ledger,
        new_ledger,
        print_plan,
        print_shard_plan,
        reconcile,
        record_event,
        save_ledger,
        transfer_all,
        transfer_lock,
        transfer_plan,
    )

    ref = source if isinstance(source, SourceRef) else parse_source(source)
    provider = get_provider(ref.provider)
    root = payload_root_for(ref.artifact_type)
    resume = find_resume(vault, ref, revision, root)
    pinned = resume[1] if resume else None
    orphan_dir = resume[0] if resume and pinned is None else None

    if pinned is not None:
        bundle_dir = resume[0]
        progress(
            f"Resuming {ref.artifact_type} {ref.canonical} @ pinned revision "
            f"{pinned['revision'][:12]} (no metadata refresh) ..."
        )
        snapshot = None
    else:
        pin_revision = orphan_dir.name if orphan_dir is not None else revision
        progress(
            f"Resolving {ref.canonical} @ {pin_revision or provider.default_revision} ..."
        )
        try:
            snapshot = provider.pin(ref, pin_revision, require_access=True)
        except SourceGatedError as exc:
            if orphan_dir is not None:
                shutil.rmtree(orphan_dir, ignore_errors=True)
            raise SystemExit(str(exc)) from None
        except SourceNotFoundError as exc:
            raise SystemExit(str(exc)) from None
        bundle_dir = bundle_dir_for(vault, ref, snapshot.revision)

    payload_dir = bundle_dir / root
    with transfer_lock(bundle_dir, progress=progress):
        manifest_path = bundle_dir / "manifest.json"
        if manifest_path.exists() and not force and not dry_run:
            bundle_id = f"{bundle_dir.parent.name}@{bundle_dir.name}"
            next_hint = f"`darsay info {bundle_id}` or `darsay run {bundle_id}`"
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                existing = {}
            if (
                isinstance(existing, dict)
                and existing.get("artifact_type") == "dataset"
            ):
                next_hint = f"`darsay info {bundle_id}`"
            raise SystemExit(
                f"error: bundle already exists: {bundle_dir}\n"
                f"  {bundle_id} is already in the vault — {next_hint}.\n"
                "  --force re-pins (may follow a new main); it is not resume."
            )

        if pinned is not None:
            ledger = load_ledger(bundle_dir)
            _warn_include_vs_pin(include, ledger, progress)
        else:
            if manifest_path.exists() and dry_run and not force:
                try:
                    ledger = load_ledger(bundle_dir)
                    _warn_include_vs_pin(include, ledger, progress)
                except LedgerError:
                    assert snapshot is not None
                    ledger = new_ledger(snapshot, include=include)
            elif force:
                assert snapshot is not None
                ledger = new_ledger(snapshot, include=include)
                save_ledger(bundle_dir, ledger)
            else:
                try:
                    ledger = load_ledger(bundle_dir)
                    _warn_include_vs_pin(include, ledger, progress)
                except LedgerError:
                    assert snapshot is not None
                    ledger = new_ledger(snapshot, include=include)
                    save_ledger(bundle_dir, ledger)
            if force and manifest_path.exists():
                manifest_path.unlink()

        if dry_run:
            import copy

            dry_ledger = copy.deepcopy(ledger)
            dry_session = {"bytes_adopted": 0, "files_completed": 0}
            plan = reconcile(
                bundle_dir,
                payload_dir,
                dry_ledger,
                dry_session,
                progress=progress,
                apply=False,
                rehash=rehash,
            )
            add_disk_preflight(bundle_dir, plan)
            print_plan(plan, progress=progress)
            if shard is not None:
                print_shard_plan(ledger, shard, progress=progress)
            return None

        stop_controller = StopController(max_bytes=max_bytes, max_minutes=max_minutes)
        stop_controller.start()
        with stop_controller.sigint_handler():
            session = begin_session(bundle_dir, ledger, shard=shard)
            session_finished = False
            try:
                plan = reconcile(
                    bundle_dir,
                    payload_dir,
                    ledger,
                    session,
                    progress=progress,
                    rehash=rehash,
                )
                add_disk_preflight(bundle_dir, plan)
                print_plan(plan, progress=progress)
                if shard is not None:
                    print_shard_plan(ledger, shard, progress=progress)
                if plan["disk"]["verdict"] == "insufficient":
                    progress(
                        "WARNING: disk preflight is insufficient; transfer may end with ENOSPC"
                    )
                stop_controller.check(session)
                plan = transfer_all(
                    bundle_dir,
                    payload_dir,
                    ledger,
                    session,
                    progress=progress,
                    stop_controller=stop_controller,
                    jobs=jobs,
                    shard=shard,
                )
                if not plan["complete"]:
                    raise RuntimeError(
                        "transfer ended without verifying every pinned file"
                    )

                finish_session(bundle_dir, ledger, session, "complete")
                session_finished = True
                return _register_bundle(bundle_dir, payload_dir, ledger, progress)
            except CleanStop as stop:
                plan = add_disk_preflight(
                    bundle_dir, transfer_plan(payload_dir, ledger)
                )
                if plan["complete"]:
                    finish_session(bundle_dir, ledger, session, "complete")
                    session_finished = True
                    return _register_bundle(bundle_dir, payload_dir, ledger, progress)
                session["stop_detail"] = stop.detail
                finish_session(bundle_dir, ledger, session, stop.reason)
                session_finished = True
                print_plan(plan, progress=progress)
                raise PartialTransfer(
                    bundle_dir, stop.reason, stop.detail, plan
                ) from stop
            except SourceGatedError as exc:
                record_event(
                    ledger,
                    None,
                    "gated",
                    "upstream access was denied during transfer; partial archive retained",
                )
                finish_session(bundle_dir, ledger, session, "error")
                session_finished = True
                raise SystemExit(str(exc)) from None
            except BaseException:
                if not session_finished:
                    finish_session(bundle_dir, ledger, session, "error")
                raise


def archive_model(
    repo_id: str,
    revision: str | None = None,
    vault: Path = Path("vault"),
    force: bool = False,
    repo_type: str = "model",
    **kwargs,
) -> Path | None:
    """Archive by locator (``owner/name`` or ``datasets/owner/name``)."""
    loc = f"datasets/{repo_id}" if repo_type == "dataset" else repo_id
    return archive(loc, revision=revision, vault=vault, force=force, **kwargs)


def _register_bundle(
    bundle_dir: Path, payload_dir: Path, ledger: dict, progress
) -> Path:
    """Run completion-time extraction and register from pinned ledger facts."""
    from .transfer import file_records as ledger_file_records
    from .transfer import local_mirrors, transfer_summary

    source = source_from_ledger(ledger)
    provider = get_provider(source.provider)
    repo_type = source.artifact_type
    commit = ledger["revision"]
    root = payload_root_for(repo_type)
    metadata = ledger["metadata"]
    card = metadata.get("card_data") or {}
    tags = list(metadata.get("tags") or [])
    gated = metadata.get("gated", False)

    # Provider transfer caches own resumable partials until every expected
    # file is verified. They are not archival payload.
    shutil.rmtree(payload_dir / ".cache", ignore_errors=True)
    file_records, upstream_mismatches = ledger_file_records(ledger, root)
    inventory_paths = [r["path"] for r in file_records]
    total_size = sum(r["size"] for r in file_records)
    completeness = check_completeness(repo_type, inventory_paths)
    if repo_type == "dataset":
        model_metadata = runtime = None
        dataset_metadata = extract_dataset_metadata(payload_dir, card, file_records)
    else:
        dataset_metadata = None
        model_metadata = extract_model_metadata(payload_dir, card)
        runtime = estimate_runtime(payload_dir, model_metadata)
    license_id = card.get("license")
    if isinstance(license_id, list):
        license_id = license_id[0] if license_id else None
    licensing = build_licensing_record(license_id, payload_dir, gated=bool(gated))

    for lf in licensing["license_files"]:
        name = Path(lf).name
        if name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
            shutil.copy2(bundle_dir / lf, bundle_dir / "LICENSE")
            break

    progress("Querying downstream ecosystem ...")
    relationships = provider.relationships(source, metadata)

    now = utc_now()
    bundle_id = f"{source.bundle_name}@{commit[:12]}"
    model_type = model_metadata.get("model_type") if model_metadata else None
    aliases = [source.canonical, source.locator]
    if source.url not in aliases:
        aliases.append(source.url)

    downloader = {
        "tool": "darsay",
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "provider": provider.name,
        **provider.downloader_versions(),
    }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "artifact_type": repo_type,
        "bundle_id": bundle_id,
        "identity": {
            "model_name": source.name,
            "family": model_type or source.name.split("-")[0].lower(),
            "publisher": source.publisher,
            "version": _guess_version(source.name, model_type),
            "release_date": metadata.get("created_at"),
            "aliases": aliases,
        },
        "source": {
            "origin": provider.name,
            "provider": provider.name,
            "address": source.canonical,
            "repo_id": source.locator,
            "upstream_url": source.url,
            "revision": commit,
            "revision_ref": ledger["revision_ref"],
            "last_modified_upstream": metadata.get("last_modified"),
            "download_timestamp": now,
            "transfer": transfer_summary(ledger),
            "downloader": downloader,
            "mirrors_used": local_mirrors(ledger),
            "signatures": None,
            "access": provider.access_record(metadata),
            "upstream_stats_at_archive": {
                "downloads_last_month": metadata.get("downloads"),
                "likes": metadata.get("likes"),
            },
            "upstream_tags": tags or None,
            "subset": ledger.get("subset"),
        },
        "licensing": licensing,
        "inventory": {
            "file_count": len(file_records),
            "total_size_bytes": total_size,
            "bundle_hash": bundle_hash(file_records, root),
            "layout": {
                "payload_root": ARTIFACT_TYPES[repo_type]["payload_root"],
                "mutable_metadata": [*BUNDLE_METADATA_FILES, "LICENSE"],
            },
            "files": file_records,
        },
        **(
            {"dataset_metadata": dataset_metadata}
            if repo_type == "dataset"
            else {"model_metadata": model_metadata, "runtime": runtime}
        ),
        "validation": {
            "checksum_verification": {
                "at": now,
                "method": "per-file at download completion",
                "status": "pass" if not upstream_mismatches else "fail",
                "files_checked": len(file_records),
                "upstream_mismatches": upstream_mismatches,
            },
            "completeness": completeness,
            "smoke_tests": (
                {"structure": {"status": "not-run"}}
                if repo_type == "dataset"
                else {
                    "tokenizer": {"status": "not-run"},
                    "inference": {"status": "not-run"},
                }
            ),
        },
        "relationships": relationships,
        "archive": {
            "date_archived": now,
            "archived_by": None,
            "location": str(bundle_dir.resolve()),
            "host": socket.gethostname(),
            "storage_tier": "local-disk",
            "backup_status": "none",
            "replicas": [],
            "last_integrity_check": now,
            "last_accessed": now,
        },
        "security": {
            "integrity_status": "verified-against-upstream"
            if not upstream_mismatches
            else "upstream-mismatch",
            "unexpected_changes": [],
            "trust_level": "unreviewed",
            "reviewed_by": None,
            "review_notes": None,
        },
        "curation": {
            "historical_significance": None,
            "major_capabilities": None,
            "known_limitations": None,
            "successor_models": None,
            "personal_notes": None,
            "curation_file": "curation.md",
        },
    }

    write_manifest(bundle_dir, manifest)
    _write_curation_template(bundle_dir, manifest)

    from .readme_gen import write_bundle_readme

    write_bundle_readme(bundle_dir, manifest)

    from .verify import write_verification_report

    write_verification_report(
        bundle_dir,
        manifest["validation"]["checksum_verification"],
        completeness,
        first_run=True,
    )

    progress(
        f"Archived {bundle_id}: {len(file_records)} files, {total_size / 1024**2:.1f} MiB"
    )
    if upstream_mismatches:
        progress(
            f"WARNING: {len(upstream_mismatches)} files did not match upstream checksums!"
        )
    return bundle_dir


def _write_curation_template(bundle_dir: Path, manifest: dict) -> None:
    path = bundle_dir / "curation.md"
    if path.exists():
        return
    path.write_text(
        f"""# Curation notes — {manifest["bundle_id"]}

_This is the curator's file: edit it freely. `darsay regen` folds it into
README.md; nothing here is machine-generated after this template._

## Historical significance

_Why this model matters._

## Derivation & alignment changes

_How this artifact differs from its base — finetune, quantization, merge,
alignment modifications (e.g. abliteration) — beyond what upstream tags say._

## Major capabilities

_What it was known to do well._

## Known limitations

_Where it fell short._

## Successor models

_What replaced it._

## Personal notes

_Anything else worth remembering._
""",
        encoding="utf-8",
    )
