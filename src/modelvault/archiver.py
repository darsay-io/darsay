"""Archive a Hugging Face model repo into a reproducible, auditable bundle.

Bundle layout:

    vault/<publisher>--<name>/<revision12>/
        model/            immutable payload: exact snapshot of the upstream repo
        manifest.json     machine-readable record (schema.py / SCHEMA_VERSION)
        README.md         human-readable summary, regenerable from the manifest
        VERIFICATION.md   latest verification report
        verification.json verification history
        curation.md       curator notes; the only file meant to be hand-edited

The payload under model/ is treated as immutable after archiving; the bundle
hash covers it alone. Metadata at the bundle root is mutable by design.
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
from .metadata import estimate_runtime, extract_model_metadata
from .schema import check_completeness


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bundle_dir_for(vault: Path, repo_id: str, revision: str) -> Path:
    return vault / repo_id.replace("/", "--").lower() / revision[:12]


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


def archive_model(
    repo_id: str,
    revision: str | None = None,
    vault: Path = Path("vault"),
    force: bool = False,
    progress=print,
) -> Path:
    api = HfApi()

    progress(f"Resolving {repo_id} @ {revision or 'main'} ...")
    info = api.model_info(repo_id, revision=revision, files_metadata=True)
    commit = info.sha
    card = info.card_data.to_dict() if info.card_data else {}

    bundle_dir = bundle_dir_for(vault, repo_id, commit)
    payload_root = bundle_dir / "model"
    if (bundle_dir / "manifest.json").exists() and not force:
        raise SystemExit(f"Bundle already exists: {bundle_dir} (use --force to re-archive)")
    bundle_dir.mkdir(parents=True, exist_ok=True)

    progress(f"Downloading snapshot {commit[:12]} into {payload_root} ...")
    snapshot_download(repo_id, revision=commit, local_dir=payload_root)
    # Remove hub bookkeeping so the payload is a pristine copy of the repo tree.
    shutil.rmtree(payload_root / ".cache", ignore_errors=True)

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
    for rel, abs_path in iter_payload_files(payload_root):
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
            "path": f"model/{rel}",
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
    completeness = check_completeness("model", inventory_paths)
    model_metadata = extract_model_metadata(payload_root, card)
    runtime = estimate_runtime(payload_root, model_metadata)
    licensing = build_licensing_record(card.get("license"), payload_root)

    # Surface the primary license file at the bundle root for museum visibility.
    for lf in licensing["license_files"]:
        name = Path(lf).name
        if name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
            shutil.copy2(bundle_dir / lf, bundle_dir / "LICENSE")
            break

    progress("Querying downstream ecosystem (quantizations, finetunes) ...")
    related = _related_repos(api, repo_id)

    publisher, _, name = repo_id.partition("/")
    now = utc_now()
    bundle_id = f"{repo_id.replace('/', '--').lower()}@{commit[:12]}"

    base_model = card.get("base_model")
    if isinstance(base_model, list):
        base_model = base_model[0] if base_model else None

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "model",
        "bundle_id": bundle_id,
        "identity": {
            "model_name": name,
            "family": model_metadata.get("model_type") or name.split("-")[0].lower(),
            "publisher": publisher,
            "version": _guess_version(name, model_metadata.get("model_type")),
            "release_date": info.created_at.isoformat(timespec="seconds") if info.created_at else None,
            "aliases": [repo_id],
        },
        "source": {
            "origin": "huggingface",
            "repo_id": repo_id,
            "upstream_url": f"https://huggingface.co/{repo_id}",
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
            "bundle_hash": bundle_hash(file_records),
            "layout": {
                "payload_root": "model/",
                "mutable_metadata": ["manifest.json", "README.md", "VERIFICATION.md",
                                     "verification.json", "curation.md", "LICENSE"],
            },
            "files": file_records,
        },
        "model_metadata": model_metadata,
        "runtime": runtime,
        "validation": {
            "checksum_verification": {
                "at": now,
                "status": "pass" if not upstream_mismatches else "fail",
                "files_checked": len(file_records),
                "upstream_mismatches": upstream_mismatches,
            },
            "completeness": completeness,
            "smoke_tests": {
                "tokenizer": {"status": "not-run"},
                "inference": {"status": "not-run"},
            },
        },
        "relationships": {
            "base_model": base_model,
            "finetuned_from": base_model,
            "quantized_versions": related["quantized_versions"],
            "gguf_repos": related["gguf_repos"],
            "finetunes_count": len(related["finetunes"]) if related["finetunes"] is not None else None,
            "adapters_count": len(related["adapters"]) if related["adapters"] is not None else None,
            "related_variants": None,
            "successors": None,
            "ecosystem_snapshot_as_of": related["as_of"],
            "query_limit": related["query_limit"],
        },
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
