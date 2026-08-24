"""Archive a Hugging Face repo (model or dataset) into a reproducible, auditable bundle.

Bundle layout:

    vault/<publisher>--<name>/<revision12>/           (datasets--<publisher>--<name>/ for datasets)
        model/            immutable payload: exact snapshot of the upstream repo
                          (data/ for dataset bundles — the registry's payload_root)
        manifest.json     machine-readable record (schema.py / SCHEMA_VERSION)
        README.md         human-readable summary, regenerable from the manifest
        VERIFICATION.md   latest verification report
        verification.json verification history
        curation.md       curator notes; the only file meant to be hand-edited

The payload is treated as immutable after archiving; the bundle hash covers it
alone. Metadata at the bundle root is mutable by design.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

import huggingface_hub
from huggingface_hub import HfApi
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError

from . import SCHEMA_VERSION, __version__
from .hashing import bundle_hash
from .licensing import build_licensing_record
from .metadata import estimate_runtime, extract_dataset_metadata, extract_model_metadata
from .schema import ARTIFACT_TYPES, check_completeness, payload_root_for


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_repo_ref(ref: str) -> tuple[str, str]:
    """Parse a Hub address into (repo_type, repo_id).

    Accepts the Hub's own grammar: `owner/name` (model), `datasets/owner/name`
    (dataset), and either huggingface.co URL form with any trailing path or
    query stripped. Anything else exits cleanly.
    """
    s = ref.strip()
    is_url = False
    for prefix in ("https://huggingface.co/", "http://huggingface.co/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            is_url = True
            break
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in s.split("/") if p]
    repo_type = "model"
    if parts and parts[0] == "datasets":
        repo_type = "dataset"
        parts = parts[1:]
    if is_url and len(parts) > 2:
        parts = parts[:2]  # drop a trailing URL path such as /tree/main or /blob/...
    if len(parts) != 2:
        raise SystemExit(
            f"error: cannot parse repo ref {ref!r} — expected owner/name, "
            "datasets/owner/name, or a huggingface.co URL of either"
        )
    return repo_type, "/".join(parts)


def hub_url(repo_id: str, repo_type: str = "model") -> str:
    prefix = "datasets/" if repo_type == "dataset" else ""
    return f"https://huggingface.co/{prefix}{repo_id}"


def bundle_name_for(repo_id: str, repo_type: str = "model") -> str:
    """Vault directory name. Datasets take a `datasets--` prefix: model and
    dataset namespaces can collide on the Hub, and the prefix mirrors its URL
    grammar while preserving the two-level `*/*/manifest.json` vault layout."""
    name = repo_id.replace("/", "--").lower()
    return f"datasets--{name}" if repo_type == "dataset" else name


def bundle_dir_for(vault: Path, repo_id: str, revision: str, repo_type: str = "model") -> Path:
    return vault / bundle_name_for(repo_id, repo_type) / revision[:12]


def write_manifest(bundle_dir: Path, manifest: dict) -> None:
    path = bundle_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(bundle_dir: Path) -> dict:
    return json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))


def _guess_version(repo_name: str, model_type: str | None) -> str | None:
    # "Qwen3-0.6B" with model_type "qwen3" -> "3"
    if model_type:
        m = re.search(r"(\d+(?:\.\d+)?)$", model_type)
        if m:
            return m.group(1)
    m = re.search(r"[a-zA-Z](\d+(?:\.\d+)?)[-_]", repo_name)
    return m.group(1) if m else None


# Hub lineage tags: `base_model:<repo_id>` declares a parent, and
# `base_model:<relation>:<repo_id>` labels the edge — this is what the Hub's
# "model tree" renders. Relations per the Hub card spec.
BASE_MODEL_RELATIONS = ("adapter", "finetune", "merge", "quantized")


def parse_base_model_tags(tags: list[str]) -> tuple[list[str], dict[str, str]]:
    """Parse `base_model[:<relation>]:<repo_id>` repo tags into
    (parent repo ids in tag order, {repo_id: relation})."""
    ids: list[str] = []
    relations: dict[str, str] = {}
    for tag in tags:
        if not tag.startswith("base_model:"):
            continue
        rest = tag[len("base_model:"):]
        for rel in BASE_MODEL_RELATIONS:
            if rest.startswith(rel + ":"):
                rest = rest[len(rel) + 1:]
                if rest:
                    relations[rest] = rel
                break
        if rest and rest not in ids:
            ids.append(rest)
    return ids, relations


def _gated_message(repo_id: str, repo_type: str) -> str:
    return (
        f"error: {repo_type} {repo_id} is gated on the Hub and this account has not been "
        "granted access. The gate is enforced server-side; modelvault does not bypass it.\n"
        f"Visit {hub_url(repo_id, repo_type)} to review and accept the author's terms, "
        "authenticate with `hf auth login`, then re-run.\n"
        "Nothing was archived."
    )


def _related_repos(api: HfApi, repo_id: str) -> dict:
    """Snapshot of downstream ecosystem repos as of archive time (best effort)."""
    related = {"as_of": utc_now(), "query_limit": 100, "quantized_versions": None,
               "gguf_repos": None, "finetunes": None, "adapters": None}
    kinds = {"quantized": "quantized_versions", "finetune": "finetunes", "adapter": "adapters"}
    for kind, key in kinds.items():
        try:
            models = list(api.list_models(filter=f"base_model:{kind}:{repo_id}", limit=100))
            related[key] = sorted(m.id for m in models)
        except Exception:
            pass
    if related["quantized_versions"]:
        ggufs = [m for m in related["quantized_versions"] if "gguf" in m.lower()]
        related["gguf_repos"] = ggufs or None
    return related


def _dataset_related(api: HfApi, repo_id: str) -> dict:
    """Models that declare training on this dataset, as of archive time (best effort)."""
    related = {"as_of": utc_now(), "query_limit": 100, "models_trained_on": None}
    try:
        models = list(api.list_models(filter=f"dataset:{repo_id}", limit=100))
        related["models_trained_on"] = sorted(m.id for m in models)
    except Exception:
        pass
    return related


def _as_list(value) -> list | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value) or None


def archive_model(
    repo_id: str,
    revision: str | None = None,
    vault: Path = Path("vault"),
    force: bool = False,
    repo_type: str = "model",
    dry_run: bool = False,
    max_bytes: int | None = None,
    max_minutes: float | None = None,
    rehash: bool = False,
    jobs: int = 4,
    shard: tuple[int, int] | None = None,
    progress=print,
) -> Path | None:
    """Archive a Hub repo through pin → reconcile → transfer → register."""
    from .transfer import (
        LedgerError,
        CleanStop,
        PartialTransfer,
        StopController,
        add_disk_preflight,
        begin_session,
        find_resume,
        finish_session,
        load_ledger,
        new_ledger,
        reconcile,
        record_event,
        save_ledger,
        print_plan,
        print_shard_plan,
        transfer_all,
        transfer_lock,
        transfer_plan,
    )

    api = HfApi()
    root = payload_root_for(repo_type)
    resume = find_resume(vault, repo_id, repo_type, revision, root)
    pinned = resume[1] if resume else None
    orphan_dir = resume[0] if resume and pinned is None else None

    if pinned is not None:
        bundle_dir = resume[0]
        progress(
            f"Resuming {repo_type} {repo_id} @ pinned commit "
            f"{pinned['revision'][:12]} (no metadata refresh) ..."
        )
        info = None
    else:
        # A missing/corrupt ledger can be rebuilt without changing the pin: the
        # bundle directory carries the first 12 commit characters.
        pin_revision = orphan_dir.name if orphan_dir is not None else revision
        progress(f"Resolving {repo_type} {repo_id} @ {pin_revision or 'main'} ...")
        try:
            if repo_type == "dataset":
                info = api.dataset_info(repo_id, revision=pin_revision, files_metadata=True)
            else:
                info = api.model_info(repo_id, revision=pin_revision, files_metadata=True)
            # Cards and repository metadata are publicly readable for many
            # gated repos. Confirm actual read authorization before creating
            # the ledger so an initially unauthorized archive remains clean;
            # a later gate change still follows the partial-preserving path.
            if getattr(info, "gated", None):
                api.auth_check(repo_id, repo_type=repo_type)
        except GatedRepoError:
            # Pin-time failures have no durable transfer state and must retain
            # the pre-incremental clean-failure behavior.
            if orphan_dir is not None:
                shutil.rmtree(orphan_dir, ignore_errors=True)
            raise SystemExit(_gated_message(repo_id, repo_type))
        except RepositoryNotFoundError:
            raise SystemExit(
                f"error: {repo_type} {repo_id!r} not found on the Hub — it may be private "
                "(authenticate with `hf auth login`), renamed, or removed. Nothing was archived."
            )
        bundle_dir = bundle_dir_for(vault, repo_id, info.sha, repo_type)

    payload_dir = bundle_dir / root
    with transfer_lock(bundle_dir, progress=progress):
        manifest_path = bundle_dir / "manifest.json"
        if manifest_path.exists() and not force and not dry_run:
            raise SystemExit(f"Bundle already exists: {bundle_dir} (use --force to re-archive)")

        if pinned is not None:
            # Reload only after holding the lock so no stale in-memory ledger is
            # used if a prior owner just finished a file.
            ledger = load_ledger(bundle_dir)
        else:
            if manifest_path.exists() and dry_run and not force:
                try:
                    ledger = load_ledger(bundle_dir)
                except LedgerError:
                    assert info is not None
                    ledger = new_ledger(repo_id, repo_type, revision or "main", info)
            elif force:
                assert info is not None
                ledger = new_ledger(repo_id, repo_type, revision or "main", info)
                save_ledger(bundle_dir, ledger)
            else:
                try:
                    ledger = load_ledger(bundle_dir)
                except LedgerError:
                    assert info is not None
                    ledger = new_ledger(repo_id, repo_type, revision or "main", info)
                    save_ledger(bundle_dir, ledger)
            if force and manifest_path.exists():
                # A forced rebuild becomes an ordinary resumable archive after
                # the fresh pin is durable. Existing payload bytes are adopted.
                manifest_path.unlink()

        if dry_run:
            # Reconciliation must answer from actual bytes, but a dry run does
            # not mutate payload state or consume provenance accounting. The
            # next transferring session will durably adopt anything found.
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
                    progress("WARNING: disk preflight is insufficient; transfer may end with ENOSPC")
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
                    raise RuntimeError("transfer ended without verifying every pinned file")

                finish_session(bundle_dir, ledger, session, "complete")
                session_finished = True
                return _register_bundle(api, bundle_dir, payload_dir, ledger, progress)
            except CleanStop as stop:
                plan = add_disk_preflight(bundle_dir, transfer_plan(payload_dir, ledger))
                if plan["complete"]:
                    # A chunk can cross the budget while finishing the final
                    # file. There is nothing left to pause, so register now.
                    finish_session(bundle_dir, ledger, session, "complete")
                    session_finished = True
                    return _register_bundle(api, bundle_dir, payload_dir, ledger, progress)
                session["stop_detail"] = stop.detail
                finish_session(bundle_dir, ledger, session, stop.reason)
                session_finished = True
                print_plan(plan, progress=progress)
                raise PartialTransfer(bundle_dir, stop.reason, stop.detail, plan)
            except GatedRepoError:
                record_event(
                    ledger,
                    None,
                    "gated",
                    "upstream access was denied during transfer; partial archive retained",
                )
                finish_session(bundle_dir, ledger, session, "error")
                session_finished = True
                raise SystemExit(
                    _gated_message(repo_id, repo_type).replace(
                        "Nothing was archived.",
                        "The partial archive was kept and resumes if access returns.",
                    )
                )
            except BaseException:
                if not session_finished:
                    finish_session(bundle_dir, ledger, session, "error")
                raise


def _register_bundle(api: HfApi, bundle_dir: Path, payload_dir: Path, ledger: dict, progress) -> Path:
    """Run completion-time extraction and register from pinned ledger facts."""
    from .transfer import file_records as ledger_file_records, local_mirrors, transfer_summary

    repo_id = ledger["repo_id"]
    repo_type = ledger["repo_type"]
    commit = ledger["revision"]
    root = payload_root_for(repo_type)
    metadata = ledger["metadata"]
    card = metadata.get("card_data") or {}
    tags = list(metadata.get("tags") or [])
    gated = metadata.get("gated", False)

    # Hub local-dir bookkeeping owns resumable partials and therefore lives
    # until every expected file is verified. It is not archival payload.
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
    if isinstance(license_id, list):  # dataset cards may declare a license list
        license_id = license_id[0] if license_id else None
    licensing = build_licensing_record(license_id, payload_dir, gated=bool(gated))

    # Surface the primary license file at the bundle root for museum visibility.
    for lf in licensing["license_files"]:
        name = Path(lf).name
        if name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
            shutil.copy2(bundle_dir / lf, bundle_dir / "LICENSE")
            break

    if repo_type == "dataset":
        progress("Querying downstream ecosystem (models trained on this dataset) ...")
        related = _dataset_related(api, repo_id)
        relationships = {
            "source_datasets": _as_list(card.get("source_datasets")),
            "models_trained_on": related["models_trained_on"],
            "ecosystem_snapshot_as_of": related["as_of"],
            "query_limit": related["query_limit"],
        }
    else:
        progress("Querying downstream ecosystem (quantizations, finetunes) ...")
        related = _related_repos(api, repo_id)
        # Upstream lineage: card `base_model` (may list several parents — merges do)
        # plus the Hub's `base_model:*` tags, which also label the derivation edge.
        base_models = [b for b in (_as_list(card.get("base_model")) or []) if isinstance(b, str)]
        tag_bases, tag_relations = parse_base_model_tags(tags)
        for b in tag_bases:
            if b not in base_models:
                base_models.append(b)
        relation = card.get("base_model_relation")
        if not isinstance(relation, str):
            # Fall back to the typed tags, but only when they are unambiguous.
            distinct = sorted(set(tag_relations.values()))
            relation = distinct[0] if len(distinct) == 1 else None
        primary_base = base_models[0] if base_models else None
        relationships = {
            "base_models": base_models or None,
            "base_model": primary_base,
            "base_model_relation": relation,
            # Only a declared finetune edge is a finetune; a quantization or an
            # alignment edit (e.g. abliteration) must not be recorded as one.
            "finetuned_from": primary_base if relation == "finetune" else None,
            "training_datasets": _as_list(card.get("datasets")),
            "quantized_versions": related["quantized_versions"],
            "gguf_repos": related["gguf_repos"],
            "finetunes_count": len(related["finetunes"]) if related["finetunes"] is not None else None,
            "adapters_count": len(related["adapters"]) if related["adapters"] is not None else None,
            "related_variants": None,
            "successors": None,
            "ecosystem_snapshot_as_of": related["as_of"],
            "query_limit": related["query_limit"],
        }

    publisher, _, name = repo_id.partition("/")
    now = utc_now()
    bundle_id = f"{bundle_name_for(repo_id, repo_type)}@{commit[:12]}"
    model_type = model_metadata.get("model_type") if model_metadata else None

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": repo_type,
        "bundle_id": bundle_id,
        "identity": {
            "model_name": name,
            "family": model_type or name.split("-")[0].lower(),
            "publisher": publisher,
            "version": _guess_version(name, model_type),
            "release_date": metadata.get("created_at"),
            "aliases": [repo_id],
        },
        "source": {
            "origin": "huggingface",
            "repo_id": repo_id,
            "upstream_url": hub_url(repo_id, repo_type),
            "revision": commit,
            "revision_ref": ledger["revision_ref"],
            "last_modified_upstream": metadata.get("last_modified"),
            "download_timestamp": now,
            "transfer": transfer_summary(ledger),
            "downloader": {
                "tool": "modelvault",
                "version": __version__,
                "huggingface_hub": huggingface_hub.__version__,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "mirrors_used": local_mirrors(ledger),
            "signatures": None,
            "access": {
                "gated": gated,
                "notes": (
                    f"Upstream repo is gated (mode: {gated}). Download required accepting "
                    "the author's access agreement, which lives in Hub repo settings and "
                    "is NOT part of the archived snapshot; re-fetching from upstream "
                    "requires an account that has accepted it."
                ) if gated else None,
            },
            "upstream_stats_at_archive": {
                "downloads_last_month": metadata.get("downloads"),
                "likes": metadata.get("likes"),
            },
            "upstream_tags": tags,
        },
        "licensing": licensing,
        "inventory": {
            "file_count": len(file_records),
            "total_size_bytes": total_size,
            "bundle_hash": bundle_hash(file_records, root),
            "layout": {
                "payload_root": ARTIFACT_TYPES[repo_type]["payload_root"],
                "mutable_metadata": ["manifest.json", "README.md", "VERIFICATION.md",
                                     "verification.json", "curation.md", "LICENSE"],
            },
            "files": file_records,
        },
        # Per-type sections: model bundles carry model_metadata + runtime,
        # dataset bundles carry dataset_metadata (spec: docs/DATASETS.md §6).
        **({"dataset_metadata": dataset_metadata} if repo_type == "dataset"
           else {"model_metadata": model_metadata, "runtime": runtime}),
        "validation": {
            "checksum_verification": {
                "at": now,
                "method": "per-file at download completion",
                "status": "pass" if not upstream_mismatches else "fail",
                "files_checked": len(file_records),
                "upstream_mismatches": upstream_mismatches,
            },
            "completeness": completeness,
            "smoke_tests": ({"structure": {"status": "not-run"}} if repo_type == "dataset"
                            else {"tokenizer": {"status": "not-run"},
                                  "inference": {"status": "not-run"}}),
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
            "integrity_status": "verified-against-upstream" if not upstream_mismatches else "upstream-mismatch",
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

    from .readme_gen import write_bundle_readme  # late import to avoid cycle
    write_bundle_readme(bundle_dir, manifest)

    from .verify import write_verification_report
    write_verification_report(bundle_dir, manifest["validation"]["checksum_verification"], completeness, first_run=True)

    progress(f"Archived {bundle_id}: {len(file_records)} files, {total_size / 1024**2:.1f} MiB")
    if upstream_mismatches:
        progress(f"WARNING: {len(upstream_mismatches)} files did not match upstream checksums!")
    return bundle_dir


def _write_curation_template(bundle_dir: Path, manifest: dict) -> None:
    path = bundle_dir / "curation.md"
    if path.exists():
        return
    path.write_text(
        f"""# Curation notes — {manifest['bundle_id']}

_This is the curator's file: edit it freely. `modelvault regen` folds it into
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
