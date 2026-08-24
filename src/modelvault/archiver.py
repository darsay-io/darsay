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
from huggingface_hub import HfApi, snapshot_download

from . import SCHEMA_VERSION, __version__
from .hashing import HAVE_BLAKE3, bundle_hash, hash_file, iter_payload_files
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
    progress=print,
) -> Path:
    """Archive a Hub repo (model or dataset) as a bundle — one flow for every
    artifact type, with what varies dispatched via the ARTIFACT_TYPES registry
    and the per-type blocks below."""
    api = HfApi()

    progress(f"Resolving {repo_type} {repo_id} @ {revision or 'main'} ...")
    if repo_type == "dataset":
        info = api.dataset_info(repo_id, revision=revision, files_metadata=True)
    else:
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
    commit = info.sha
    card = info.card_data.to_dict() if info.card_data else {}

    bundle_dir = bundle_dir_for(vault, repo_id, commit, repo_type)
    root = payload_root_for(repo_type)
    payload_dir = bundle_dir / root
    if (bundle_dir / "manifest.json").exists() and not force:
        raise SystemExit(f"Bundle already exists: {bundle_dir} (use --force to re-archive)")
    bundle_dir.mkdir(parents=True, exist_ok=True)

    progress(f"Downloading snapshot {commit[:12]} into {payload_dir} ...")
    snapshot_download(repo_id, revision=commit, local_dir=payload_dir, repo_type=repo_type)
    # Remove hub bookkeeping so the payload is a pristine copy of the repo tree.
    shutil.rmtree(payload_dir / ".cache", ignore_errors=True)

    # Upstream expectations per file: size, LFS sha256 (large files), git blob sha1 (small files).
    upstream = {}
    for sib in info.siblings or []:
        upstream[sib.rfilename] = {
            "size": sib.size,
            "lfs_sha256": sib.lfs.sha256 if sib.lfs else None,
            "git_sha1": sib.blob_id if not sib.lfs else None,
        }

    progress("Hashing payload (sha256%s + upstream cross-check) ..." % ("+blake3" if HAVE_BLAKE3 else ""))
    file_records = []
    upstream_mismatches = []
    for rel, abs_path in iter_payload_files(payload_dir):
        hashes = hash_file(abs_path, with_git_sha1=True)
        up = upstream.get(rel, {})
        verified = None
        if up.get("lfs_sha256"):
            verified = hashes["sha256"] == up["lfs_sha256"]
        elif up.get("git_sha1"):
            verified = hashes["git_sha1"] == up["git_sha1"]
        if verified is False:
            upstream_mismatches.append(rel)
        record = {
            "path": f"{root}/{rel}",
            "size": abs_path.stat().st_size,
            "sha256": hashes["sha256"],
            "blake3": hashes.get("blake3"),
            "upstream_lfs_sha256": up.get("lfs_sha256"),
            "upstream_git_sha1": up.get("git_sha1"),
            "verified_against_upstream": verified,
        }
        file_records.append(record)

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
    licensing = build_licensing_record(license_id, payload_dir)

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
        base_model = card.get("base_model")
        if isinstance(base_model, list):
            base_model = base_model[0] if base_model else None
        relationships = {
            "base_model": base_model,
            "finetuned_from": base_model,
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
            "release_date": info.created_at.isoformat(timespec="seconds") if info.created_at else None,
            "aliases": [repo_id],
        },
        "source": {
            "origin": "huggingface",
            "repo_id": repo_id,
            "upstream_url": hub_url(repo_id, repo_type),
            "revision": commit,
            "revision_ref": revision or "main",
            "last_modified_upstream": info.last_modified.isoformat(timespec="seconds") if info.last_modified else None,
            "download_timestamp": now,
            "downloader": {
                "tool": "modelvault",
                "version": __version__,
                "huggingface_hub": huggingface_hub.__version__,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "mirrors_used": [],
            "signatures": None,
            "upstream_stats_at_archive": {
                "downloads_last_month": getattr(info, "downloads", None),
                "likes": getattr(info, "likes", None),
            },
            "upstream_tags": list(info.tags or []),
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
