"""Archive a source into a reproducible, auditable bundle.

Bundle layout:

    vault/<bundle-name>/<revision12>/
        model/            immutable payload: exact snapshot of the upstream repo
                          (data/ for dataset bundles — the registry's payload_root)
        manifest.json     machine-readable record (schema.py / SCHEMA_VERSION)
        README.md         human-readable summary, regenerable from the manifest
        SHA256SUMS        the payload's hash list, coreutils format: `sha256sum -c`
                          verifies with no darsay; its own sha256 is the bundle hash
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
import os
import platform
import shlex
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .hashing import bundle_hash
from .licensing import build_licensing_record
from .lineage import lineage_of_source
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
    SourceError,
    SourceGatedError,
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
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` so a reader never sees a torn file, and fsync it down.

    A same-directory temporary is fsynced, renamed over the target, and the
    directory fsynced — the manifest is the record, so a half-written one
    (a crash, a pulled disk) must never be observable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def read_manifest(bundle_dir: Path) -> dict:
    """Read a manifest of any schema major, checking only its shape.

    Everything ``load_manifest`` checks except the major: the file parses,
    ``schema_version`` is present and parseable, ``kind`` is ours. This is
    how ``migrate`` and the vault walk look at a record this darsay does
    not otherwise read; every other caller wants ``load_manifest``.
    """
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
        parse_schema_major(version)
    except ValueError:
        raise SystemExit(
            f"error: unreadable manifest at {path}: schema_version {version!r}"
        ) from None
    kind = data.get("kind") or MANIFEST_KIND
    if kind != MANIFEST_KIND:
        raise SystemExit(
            f"error: unreadable manifest at {path}: kind is not {MANIFEST_KIND!r}"
        )
    data["kind"] = kind
    return data


def load_manifest(bundle_dir: Path) -> dict:
    """Read + validate. A manifest of another major → SystemExit.

    Readers read one major and never read around an older record;
    ``darsay migrate`` brings the record forward (``migrate.py``), and a
    newer one needs a newer darsay.
    """
    data = read_manifest(bundle_dir)
    version = data["schema_version"]
    major = parse_schema_major(version)
    if major > MANIFEST_SCHEMA_MAJOR:
        raise SystemExit(
            f"error: manifest schema {version} is newer than this darsay "
            f"(reads {MANIFEST_SCHEMA_MAJOR}.x) — upgrade darsay"
        )
    if major < MANIFEST_SCHEMA_MAJOR:
        from .migrate import migration_hint

        raise SystemExit(
            f"error: manifest schema {version} predates this darsay "
            f"(reads {MANIFEST_SCHEMA_MAJOR}.x)\n{migration_hint(bundle_dir)}"
        )
    return data


def _check_pin_scope(
    include: list[str] | None, ledger: dict, *, full: bool = False
) -> None:
    from .catalog import include_key

    subset = ledger.get("subset") or {}
    pinned = subset.get("include")
    if include_key(pinned) == ():  # ``/*`` on a pin is the repository itself
        pinned = None
    if include_key(include) == include_key(pinned):
        return
    if pinned is None:
        raise SystemExit(
            "error: this pin is the full file set; the requested collection differs\n"
            "  hint: resume its existing scope, or use --force to re-pin deliberately"
        )
    if include:
        raise SystemExit(
            f"error: the requested collection differs from the pinned subset {pinned}\n"
            "  hint: use matching --include selectors to resume, or --force to re-pin\n"
            "  hint: one source/revision has one collection per vault; combine variants or use separate vaults"
        )
    if full:
        raise SystemExit(
            f"error: this pin is a subset {pinned}; --full cannot widen it\n"
            "  hint: --force --full re-pins the full file set"
        )
    if subset.get("policy"):
        # A negatives-policy pin *is* the default selection; resume silently.
        return
    raise SystemExit(
        f"error: this pin is a subset {pinned}; it is not the full repo\n"
        "  hint: pass matching --include to resume this pin\n"
        "  hint: --force re-pins the full file set"
    )


def _pinned_scope(ledger: dict) -> dict:
    """What an existing pin holds: its selectors and the bytes verified on disk."""
    from .catalog import include_key

    subset = ledger.get("subset") or {}
    include = None if subset.get("policy") else subset.get("include")
    if include_key(include) == ():
        include = None
    sizes = {item["path"]: item.get("size") or 0 for item in ledger["expected"]}
    verified = [
        path
        for path, state in ledger["files"].items()
        if state.get("status") == "verified"
    ]
    return {
        "include": include,
        "verified": len(verified),
        "verified_bytes": sum(sizes.get(path, 0) for path in verified),
    }


def _outside_pin(payload_dir: Path, ledger: dict) -> list[tuple[str, int]]:
    """Payload files on disk that a new pin does not expect; reconcile removes them."""
    from .hashing import iter_payload_files

    if not payload_dir.is_dir():
        return []
    expected = {item["path"] for item in ledger["expected"]}
    return sorted(
        (relative, path.stat().st_size)
        for relative, path in iter_payload_files(payload_dir)
        if relative not in expected
    )


def archive(
    source: str | SourceRef,
    revision: str | None = None,
    vault: Path = Path("vault"),
    force: bool = False,
    dry_run: bool = False,
    max_bytes: int | None = None,
    max_minutes: float | None = None,
    min_free: int | None = None,
    max_rate: int | None = None,
    max_offline: float | None = None,
    rehash: bool = False,
    jobs: int = 4,
    shard: tuple[int, int] | None = None,
    include: list[str] | None = None,
    full: bool = False,
    progress=print,
    confirm=None,
    choose=None,
    resume_scope: bool = False,
) -> Path | None:
    """Archive a source through pin → reconcile → transfer → register.

    A fresh model pin with no explicit ``include`` is classified and
    pinned negatives by default — negatives, everything unclassifiable,
    and support files are fetched; only same-bundle exact duplicates are skipped
    on the record (``source.subset.policy``). ``full=True`` pins the
    whole repo instead; re-runs resume whatever the pin selected.

    ``min_free``, ``max_rate``, and ``max_offline`` are per-run overrides of
    the operator config (``config.py``); ``None`` means use the configured
    value. ``confirm(question) -> bool``, when given, is asked before a
    transfer the disk preflight says cannot finish; declining pauses the
    archive cleanly before any byte moves. ``None`` proceeds, as an
    unattended run must.

    ``choose(snapshot, pinned) -> include | None`` optionally chooses scope
    for a fresh model before any archive directory or transfer ledger is
    created. Explicit includes, full publications, shards, and existing pins
    bypass it; a forced re-pin passes the pin it would replace as ``pinned``
    (its selectors and verified bytes) so the picker can open on it. The
    library has no interactive default; the CLI supplies its picker.
    Before a forced re-pin every payload file outside the new scope is
    listed with its size, ``confirm`` is asked, and a dry run removes nothing.
    ``resume_scope=True`` lets an unqualified direct-source command resume
    the recorded subset without repeating its includes. Board/catalog jobs
    keep this false: their row's scope is a requirement, not a new choice.

    ``/*`` — typed, chosen in the picker, or carried by a board row — is the
    repository itself: no selectors, the same identity and default retention
    policy as an unqualified command. ``full`` is the retention switch that
    keeps hash-identical duplicates too.
    """
    from .catalog import include_key
    from .config import free_space_floor, offline_patience, rate_cap
    from .readme_gen import human_size
    from .transfer import (
        CleanStop,
        LedgerError,
        PartialTransfer,
        StopController,
        add_disk_preflight,
        begin_session,
        disk_outlook,
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

    # A stated scope — even ``/*`` — is a decision the picker must not revisit.
    stated = bool(include) or full
    if include_key(include) == ():
        include = None
    ref = source if isinstance(source, SourceRef) else parse_source(source)
    provider = get_provider(ref.provider)
    floor = free_space_floor(vault, min_free)
    cap = rate_cap(vault, max_rate)
    patience = offline_patience(vault, max_offline)
    root = payload_root_for(ref.artifact_type)
    prior = find_resume(vault, ref, revision, root)
    resume = None if force else prior
    pinned = resume[1] if resume else None
    orphan_dir = resume[0] if resume and pinned is None else None

    if pinned is not None:
        bundle_dir = resume[0]
        progress(
            f"Resuming {ref.artifact_type} {ref.canonical} @ pinned revision "
            f"{pinned['revision'][:12]} (no metadata refresh) ..."
        )
        snapshot = None
        if resume_scope and not stated and not force:
            include = (pinned.get("subset") or {}).get("include")
            if include:
                progress(f"Collection scope: resuming the pinned selectors {include}")
    else:
        pin_revision = orphan_dir.name if orphan_dir is not None else revision
        progress(
            f"Resolving {ref.canonical} @ {pin_revision or provider.default_revision} ..."
        )
        try:
            snapshot = provider.pin(ref, pin_revision, require_access=True)
        except SourceError as exc:
            # Gated, missing, or unreachable: nothing durable has started.
            if isinstance(exc, SourceGatedError) and orphan_dir is not None:
                from .vault import prune_empty_parent

                shutil.rmtree(orphan_dir, ignore_errors=True)
                prune_empty_parent(orphan_dir)
            raise SystemExit(str(exc)) from None
        if snapshot.source.canonical != ref.canonical:
            progress(
                f"Resolved {ref.canonical} as {snapshot.source.artifact_type} "
                f"{snapshot.source.canonical}"
            )
            ref = snapshot.source
            root = payload_root_for(ref.artifact_type)
        bundle_dir = bundle_dir_for(vault, ref, snapshot.revision)

    if (
        choose is not None
        and snapshot is not None
        and resume is None
        and ref.artifact_type == "model"
        and not stated
        and shard is None
        and not (bundle_dir / "manifest.json").exists()
    ):
        replacing = prior[1] if force and prior is not None else None
        selected = choose(
            snapshot, _pinned_scope(replacing) if replacing is not None else None
        )
        if selected is not None:
            if not selected:
                raise SystemExit("No collection selected; no archive has started.")
            include = None if include_key(selected) == () else list(selected)

    payload_dir = bundle_dir / root

    def _fresh_ledger() -> dict:
        """Pin a new ledger; a model pin applies the negatives policy by default."""
        assert snapshot is not None
        effective_include, policy = include, None
        if ref.artifact_type == "model" and not include and not full:
            from .classify import negatives_policy

            effective_include, policy, _classification = negatives_policy(
                provider, ref, snapshot, progress
            )
        return new_ledger(snapshot, include=effective_include, policy=policy)

    pin_recorded = False
    with transfer_lock(bundle_dir, progress=progress):
        manifest_path = bundle_dir / "manifest.json"
        if manifest_path.exists() and not force and not dry_run:
            bundle_id = f"{bundle_dir.parent.name}@{bundle_dir.name}"
            from .vault import command_prefix

            paste = shlex.join(command_prefix(bundle_dir.parent.parent))
            next_hint = f"`{paste} info {bundle_id}` or `{paste} run {bundle_id}`"
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                existing = {}
            if (
                isinstance(existing, dict)
                and existing.get("artifact_type") == "dataset"
            ):
                next_hint = f"`{paste} info {bundle_id}`"
            raise SystemExit(
                f"error: bundle already exists: {bundle_dir}\n"
                f"  {bundle_id} is already in the vault — {next_hint}.\n"
                "  --force re-pins (may follow a new main); it is not resume."
            )

        if pinned is not None:
            ledger = load_ledger(bundle_dir)
            _check_pin_scope(include, ledger, full=full)
        else:
            if manifest_path.exists() and dry_run and not force:
                try:
                    ledger = load_ledger(bundle_dir)
                    _check_pin_scope(include, ledger, full=full)
                except LedgerError:
                    ledger = _fresh_ledger()
            elif force:
                ledger = _fresh_ledger()
                outside = _outside_pin(payload_dir, ledger)
                if outside:
                    # Say what the re-pin discards before anything is discarded.
                    count = f"{len(outside)} file{'s' if len(outside) != 1 else ''}"
                    on_disk = human_size(sum(size for _, size in outside))
                    progress(
                        f"Re-pin removes {count} outside the new scope ({on_disk} on disk)"
                        + (" — dry run, nothing removed:" if dry_run else ":")
                    )
                    for relative, size in outside:
                        progress(f"  {relative}  {human_size(size)}")
                    if (
                        not dry_run
                        and confirm is not None
                        and not confirm("Remove them and re-pin? [y/N] ", default=False)
                    ):
                        raise SystemExit(
                            "Re-pin declined; the existing pin and its files are untouched."
                        )
                if not dry_run:  # a dry run re-plans from a fresh pin on paper only
                    save_ledger(bundle_dir, ledger)
            else:
                try:
                    ledger = load_ledger(bundle_dir)
                    _check_pin_scope(include, ledger, full=full)
                except LedgerError:
                    ledger = _fresh_ledger()
                    # A dry run of a new source records the pin — and only
                    # the pin — so the plan it prints is the plan the real
                    # run continues (docs/INCREMENTAL.md §6).
                    save_ledger(bundle_dir, ledger)
                    pin_recorded = True
            if force and manifest_path.exists() and not dry_run:
                manifest_path.unlink()

        if dry_run:
            import copy

            if pin_recorded:
                progress(
                    f"Pinned {ref.canonical} @ {ledger['revision'][:12]} — recorded "
                    f"in {bundle_dir / 'transfer.json'} (no payload bytes)."
                )
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
            add_disk_preflight(bundle_dir, plan, min_free=floor)
            print_plan(plan, progress=progress, max_rate=cap)
            if shard is not None:
                print_shard_plan(ledger, shard, progress=progress)
            if plan["disk"]["verdict"] == "insufficient":
                for line in disk_outlook(plan, dry_ledger, payload_dir, max_rate=cap):
                    progress(line)
            return None

        stop_controller = StopController(
            max_bytes=max_bytes,
            max_minutes=max_minutes,
            min_free_bytes=floor,
            disk_path=bundle_dir,
        )
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
                    stop=stop_controller,
                )
                add_disk_preflight(bundle_dir, plan, min_free=floor)
                print_plan(plan, progress=progress, max_rate=cap)
                if shard is not None:
                    print_shard_plan(ledger, shard, progress=progress)
                if plan["disk"]["verdict"] == "insufficient":
                    for line in disk_outlook(plan, ledger, payload_dir, max_rate=cap):
                        progress(line)
                    if confirm is not None and not confirm("Continue anyway? [Y/n] "):
                        disk = plan["disk"]
                        usable = max(
                            0, disk["free_bytes"] - (disk.get("min_free_bytes") or 0)
                        )
                        detail = (
                            "declined at the disk preflight — needs "
                            f"{human_size(disk['needed_bytes'])}, "
                            f"{human_size(usable)} usable"
                        )
                        session["stop_detail"] = detail
                        finish_session(bundle_dir, ledger, session, "disk")
                        session_finished = True
                        raise PartialTransfer(bundle_dir, "disk", detail, plan)
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
                    max_rate=cap,
                    max_offline=patience,
                )
                if not plan["complete"]:
                    raise RuntimeError(
                        "transfer ended without verifying every pinned file"
                    )

                finish_session(bundle_dir, ledger, session, "complete")
                session_finished = True
                return _register_bundle(bundle_dir, payload_dir, ledger, progress)
            except (CleanStop, KeyboardInterrupt) as stop:
                # A second Ctrl-C raises KeyboardInterrupt to break out of a
                # stalled transfer; the ledger and payload bytes stay valid, so
                # it pauses the archive the same way a clean stop does.
                aborted = not isinstance(stop, CleanStop)
                reason = "interrupt" if aborted else stop.reason
                detail = "aborted by user" if aborted else stop.detail
                plan = add_disk_preflight(
                    bundle_dir, transfer_plan(payload_dir, ledger), min_free=floor
                )
                if plan["complete"] and not aborted:
                    finish_session(bundle_dir, ledger, session, "complete")
                    session_finished = True
                    return _register_bundle(bundle_dir, payload_dir, ledger, progress)
                session["stop_detail"] = detail
                if not finish_session(bundle_dir, ledger, session, reason):
                    detail += (
                        "; the transfer ledger could not be updated — the next "
                        "run reconciles the payload"
                    )
                session_finished = True
                print_plan(plan, progress=progress)
                raise PartialTransfer(bundle_dir, reason, detail, plan) from stop
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

    progress("Querying lineage: parents declared upstream, descendants listed ...")
    lineage = provider.lineage(source, metadata)

    now = utc_now()
    bundle_id = f"{source.bundle_name}@{commit[:12]}"
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
            "publisher": source.publisher,
            # Family, generation, member, variants, formats, size: read from
            # the publisher's name for the work (lineage.py), and labeled so.
            **lineage_of_source(source.canonical).as_dict(),
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
        "lineage": lineage,
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

    from .hashing import write_sha256sums
    from .readme_gen import write_bundle_readme

    write_bundle_readme(bundle_dir, manifest)
    write_sha256sums(bundle_dir, manifest)

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
