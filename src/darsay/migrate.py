"""Bring a bundle's record to the schema this darsay writes: ``darsay migrate``.

A bundle is bytes and a record. The bytes — the payload under ``model/``
or ``data/`` — never change. The record, ``manifest.json``, is written in
the shape of the model of the models at the time, and that model
improves: when it does the schema major moves and every reader follows
(``load_manifest`` reads one major). A record written under an earlier
major is still a complete set of facts about an unchanged payload.
``migrate`` reads those facts back under the current model and writes
them in the current shape — offline, from the record and the payload,
touching no payload byte and re-hashing nothing.

For 1.x → 2.x that means:

* **carried as recorded** — source, licensing, inventory (the hash
  record), runtime, validation, archive, security, curation, and a
  dataset's ``dataset_metadata``: facts whose meaning did not change;
* **re-read from the name** — family, generation, member, variants,
  formats, size (``lineage.py``), labelled ``read_from: "name"``;
* **re-derived from the payload** — ``model_metadata``, so the release
  precision, ``precision_detail``, and bytes per parameter mean what
  2.x means by them (``precision.py``);
* **translated** — 1.x ``relationships`` into ``lineage``: parents with
  their relation and provenance, the archive-time descendants snapshot
  kept as it was;
* **renamed** — the subset policy and classification verdicts say
  ``negatives`` / ``negative`` where 1.x said masters / master;
* **dropped** — ``identity.version`` (its successor is ``generation``)
  and the config-read ``identity.family`` (the architecture; still
  ``model_metadata.model_type``).

The reading of an older major lives here and nowhere else: a record
moves forward, readers do not move back. Each move is recorded under
``archive.migrations``.
"""

from __future__ import annotations

import shlex
from copy import deepcopy
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .identity import machine_name, same_machine
from .schema import (
    ARTIFACT_TYPES,
    BUNDLE_METADATA_FILES,
    MANIFEST_SCHEMA_MAJOR,
    parse_schema_major,
    payload_root,
)

# Sections a 1.x record carries into 2.x as recorded, in 2.x document order.
_CARRIED_1X = (
    "source",
    "licensing",
    "inventory",
    "dataset_metadata",
    "runtime",
    "validation",
    "archive",
    "security",
    "curation",
)

# 1.x classification vocabulary → 2.x. The rules did not change; the words did.
_POLICY_1X = {"masters": "negatives"}
_VERDICT_1X = {"master": "negative"}


def record_status(version) -> str:
    """``current`` (this darsay reads it), ``older``, or ``newer``."""
    major = parse_schema_major(version)
    if major < MANIFEST_SCHEMA_MAJOR:
        return "older"
    if major > MANIFEST_SCHEMA_MAJOR:
        return "newer"
    return "current"


def migration_hint(bundle_dir: Path) -> str:
    """The line every refusal of an older record ends with."""
    return (
        f"  hint: darsay migrate {shlex.quote(str(bundle_dir))}   "
        f"re-reads the record under the {MANIFEST_SCHEMA_MAJOR}.x model; "
        "payload untouched (-n to preview)"
    )


# --- the plan ---------------------------------------------------------------


def migration_plan(bundle_dir: Path) -> dict:
    """What ``migrate`` would write for one bundle, and from where.

    Raises ``SystemExit`` for the cases the command refuses — not a
    registered bundle, a record newer than this darsay — so a dry run and
    the command say the same thing. A record already on the current major
    comes back with ``status: current`` and nothing to do.
    """
    from .archiver import read_manifest

    bundle_dir = Path(bundle_dir)
    if not (bundle_dir / "manifest.json").is_file():
        if (bundle_dir / "transfer.json").is_file():
            raise SystemExit(
                f"error: {bundle_dir} is a partial, not a registered bundle — "
                "there is no record to migrate yet; `darsay archive` continues it"
            )
        raise SystemExit(
            f"error: no manifest.json in {bundle_dir} — not a darsay bundle"
        )
    old = read_manifest(bundle_dir)
    version = str(old["schema_version"])
    status = record_status(version)
    if status == "newer":
        raise SystemExit(
            f"error: manifest schema {version} is newer than this darsay "
            f"(reads {MANIFEST_SCHEMA_MAJOR}.x) — a record does not move back; "
            "upgrade darsay"
        )
    plan = {
        "bundle_id": old.get("bundle_id")
        or f"{bundle_dir.parent.name}@{bundle_dir.name}",
        "path": str(bundle_dir),
        "artifact_type": old.get("artifact_type") or "model",
        "from_schema": version,
        "to_schema": SCHEMA_VERSION,
        "status": "current" if status == "current" else "migrate",
        "changes": [],
        "carried": [],
        "payload": None,
        "ledger": False,
        "writes": [],
        "record": None,
    }
    if plan["status"] == "current":
        return plan

    metadata = _archive_time_metadata(bundle_dir)
    plan["ledger"] = metadata is not None
    record, changes, carried = _record_from_1x(
        old, bundle_dir=bundle_dir, metadata=metadata
    )
    plan["changes"] = changes
    plan["carried"] = carried
    plan["payload"] = _payload_check(bundle_dir, record)
    plan["verified_here"] = _verified_here(bundle_dir, record, plan["payload"])
    plan["writes"] = ["manifest.json", "README.md", "SHA256SUMS"] + (
        ["VERIFICATION.md"] if (bundle_dir / "verification.json").is_file() else []
    )
    plan["record"] = record
    return plan


def vault_migration_plans(vault: Path) -> list[dict]:
    """A plan per registered bundle in ``vault``; partials are not records."""
    from .vault import iter_bundle_dirs

    return [
        migration_plan(bundle_dir)
        for bundle_dir in iter_bundle_dirs(vault)
        if (bundle_dir / "manifest.json").is_file()
    ]


def _archive_time_metadata(bundle_dir: Path) -> dict | None:
    """The upstream metadata the archive recorded, if its ledger travelled.

    ``transfer.json`` is disposable, but when it is there it holds the
    card and tags exactly as the provider served them at archive time —
    the same input ``archive`` gives the provider's ``lineage()``.
    """
    from .transfer import LedgerError, load_ledger

    if not (bundle_dir / "transfer.json").is_file():
        return None
    try:
        ledger = load_ledger(bundle_dir)
    except LedgerError:
        return None
    metadata = ledger.get("metadata")
    return metadata if isinstance(metadata, dict) else None


def _payload_check(bundle_dir: Path, record: dict) -> dict:
    """Path-and-size presence of the recorded payload — a stat walk, not a hash.

    Migration is a record operation; ``verify`` is the verb that reads
    every byte. This is the cheap honesty check that says whether the
    payload the record describes is even here.
    """
    from .hashing import iter_payload_files

    root = payload_root(record)
    expected = {r["path"]: r["size"] for r in record["inventory"]["files"]}
    actual = {
        f"{root}/{rel}": path.stat().st_size
        for rel, path in iter_payload_files(bundle_dir / root)
    }
    return {
        "files": len(expected),
        "bytes": sum(expected.values()),
        "missing": sorted(set(expected) - set(actual)),
        "size_mismatch": sorted(
            p for p in set(expected) & set(actual) if expected[p] != actual[p]
        ),
        "extra": sorted(set(actual) - set(expected)),
        "checked": "path and size",
    }


def _verified_here(bundle_dir: Path, record: dict, payload: dict) -> str | None:
    """The date the record last saw this payload pass — at this path, on this host.

    ``validation`` and ``archive`` are carried as recorded, so the record's
    own verification still stands for a bundle that has not moved since:
    the date, or ``None`` when the record's last pass was somewhere else
    (an rsync'd-in bundle), failed, or does not match what is on disk.
    """
    check = (record.get("validation") or {}).get("checksum_verification") or {}
    archive = record.get("archive") or {}
    if check.get("status") != "pass" or not check.get("at"):
        return None
    if payload["missing"] or payload["size_mismatch"] or payload["extra"]:
        return None
    location = archive.get("location")
    if not location or not same_machine(archive.get("host"), machine_name()):
        return None
    try:
        if Path(location).resolve() != bundle_dir.resolve():
            return None
    except OSError:
        return None
    return str(check["at"])[:10]


# --- 1.x → 2.x ----------------------------------------------------------------


def _record_from_1x(
    old: dict, *, bundle_dir: Path, metadata: dict | None
) -> tuple[dict, list[dict], list[str]]:
    """A 2.x record from a 1.x one, with what changed and what was carried."""
    from .schema import MANIFEST_KIND

    artifact_type = old.get("artifact_type") or "model"
    source = _source_with_address(deepcopy(old.get("source") or {}), artifact_type)
    changes: list[dict] = []

    identity, identity_change = _identity_from_1x(
        old.get("identity") or {},
        source,
        old_model_type=(old.get("model_metadata") or {}).get("model_type"),
    )
    changes.append(identity_change)

    if artifact_type == "model":
        model_metadata, precision_change = _model_metadata_from_payload(
            old.get("model_metadata") or {}, bundle_dir, artifact_type, metadata
        )
        changes.append(precision_change)
    else:
        model_metadata = None

    lineage, lineage_change = _lineage_from_1x(
        old.get("relationships") or {},
        artifact_type=artifact_type,
        provider=source.get("provider") or source.get("origin") or "huggingface",
        metadata=metadata,
        tags=list(source.get("upstream_tags") or []),
    )
    changes.append(lineage_change)

    subset_change = _rename_subset_vocabulary(source.get("subset"))
    if subset_change is not None:
        changes.append(subset_change)

    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "artifact_type": artifact_type,
        "bundle_id": old["bundle_id"],
        "identity": identity,
        "source": source,
        "licensing": deepcopy(old.get("licensing")),
        "inventory": _inventory_with_layout(old.get("inventory") or {}, artifact_type),
    }
    if artifact_type == "dataset":
        record["dataset_metadata"] = deepcopy(old.get("dataset_metadata"))
    else:
        record["model_metadata"] = model_metadata
        record["runtime"] = deepcopy(old.get("runtime"))
    record["validation"] = deepcopy(old.get("validation"))
    record["lineage"] = lineage
    record["archive"] = deepcopy(old.get("archive") or {})
    record["security"] = deepcopy(old.get("security"))
    record["curation"] = deepcopy(old.get("curation"))
    # Unknown top-level keys round-trip, as every 2.x writer promises;
    # the 1.x sections that 2.x re-expresses do not.
    for key, value in old.items():
        if key not in record and key not in ("relationships", "identity"):
            record[key] = deepcopy(value)

    carried = [key for key in _CARRIED_1X if key in record]
    return record, changes, carried


def _identity_from_1x(
    old: dict, source: dict, *, old_model_type: str | None
) -> tuple[dict, dict]:
    from .lineage import name_of_source, parse_name

    name = old.get("model_name") or name_of_source(source.get("address") or "")
    read = parse_name(name).as_dict()
    identity = {
        "model_name": name,
        "publisher": old.get("publisher"),
        **read,
        "release_date": old.get("release_date"),
        "aliases": old.get("aliases"),
    }
    dropped = []
    if "version" in old:
        dropped.append(
            "version"
            + (f" {old['version']!r}" if old.get("version") is not None else "")
        )
    if old.get("family"):
        # 1.x took the architecture from config.json when it could, else
        # the first token of the name; neither is what family means now.
        note = (
            "read from config.json; still model_metadata.model_type"
            if old_model_type and old["family"] == old_model_type
            else "the name's first token; the grammar now reads the whole name"
        )
        dropped.append(f"family {old['family']!r} ({note})")
    return identity, {
        "section": "identity",
        "action": "re-read",
        "from": "the name",
        "now": work_label(identity),
        "was": None,
        "dropped": dropped,
    }


def _model_metadata_from_payload(
    old: dict, bundle_dir: Path, artifact_type: str, metadata: dict | None
) -> tuple[dict, dict]:
    """``model_metadata`` as 2.x ``archive`` extracts it, from the same payload.

    Everything in the section is read from the payload except two fields:
    ``languages`` comes from the card (the ledger's when it travelled,
    else the 1.x record's) and ``training_cutoff`` is the curator's.
    """
    from .metadata import extract_model_metadata
    from .schema import payload_root_for

    payload_dir = bundle_dir / payload_root_for(artifact_type)
    card = dict((metadata or {}).get("card_data") or {})
    if "language" not in card and old.get("languages") is not None:
        card["language"] = old["languages"]
    fresh = extract_model_metadata(payload_dir, card)
    fresh["training_cutoff"] = old.get("training_cutoff")
    shards = fresh.get("weight_shards")
    read_from = (
        f"config.json and {shards} safetensors header{'s' if shards != 1 else ''}"
        if shards
        else "config.json and the weight file names"
    )
    return fresh, {
        "section": "model_metadata",
        "action": "re-derived",
        "from": read_from,
        "now": precision_label(fresh),
        "was": (
            f"{old.get('precision')!r}, a dtype"
            if old.get("precision") is not None
            else None
        ),
        "dropped": [],
    }


def _lineage_from_1x(
    rel: dict,
    *,
    artifact_type: str,
    provider: str,
    metadata: dict | None,
    tags: list[str],
) -> tuple[dict, dict]:
    """``lineage`` from a 1.x ``relationships`` section.

    Parents come from the archive-time card and tags when the ledger
    travelled (the same reading ``archive`` makes), else from the record
    and its upstream tags. The descendants snapshot, its ``as_of`` and
    cap, and the curator's fields are carried — they describe the
    archive, and nothing is fetched to refresh them.
    """
    prefix = f"{provider}:"
    if artifact_type == "dataset":
        card = (metadata or {}).get("card_data") or {}
        declared = _as_list(card.get("source_datasets")) if metadata else None
        if declared is None:
            declared = _as_list(rel.get("source_datasets"))
        parents = [
            {
                "source": f"{prefix}datasets/{ds}",
                "relation": "derived",
                "declared_by": "card",
            }
            for ds in declared or []
            if isinstance(ds, str) and ds.strip()
        ]
        descendants = {"models_trained_on": rel.get("models_trained_on")}
        trained = rel.get("models_trained_on")
        kept = (
            f"{len(trained)} model{'s' if len(trained) != 1 else ''} trained on it"
            if isinstance(trained, list)
            else "unknown"
        )
    else:
        if metadata is not None:
            from .providers.huggingface import parents_from_metadata

            parents = parents_from_metadata(metadata, canonical_prefix=prefix) or []
        else:
            parents = _parents_from_relationships(rel, tags, prefix)
        descendants = {
            "quantized": rel.get("quantized_versions"),
            "gguf": rel.get("gguf_repos"),
            "finetunes_count": rel.get("finetunes_count"),
            "adapters_count": rel.get("adapters_count"),
        }
        quantized = rel.get("quantized_versions")
        kept = (
            f"{len(quantized)} quantized"
            if isinstance(quantized, list)
            else "quantized unknown"
        )
        if rel.get("finetunes_count") is not None:
            kept += f", {rel['finetunes_count']} finetunes"

    lineage = {
        "parents": parents or None,
        "descendants": descendants,
        "successors": rel.get("successors"),
        "related": rel.get("related_variants") or rel.get("related"),
        "as_of": rel.get("ecosystem_snapshot_as_of") or rel.get("as_of"),
        "query_limit": rel.get("query_limit"),
    }
    as_of = lineage["as_of"]
    edges = ", ".join(
        f"{edge['relation'] or 'parent'} {edge['source']} ({edge['declared_by']})"
        for edge in parents
    )
    now = (f"parents: {edges}" if parents else "no parents declared upstream") + (
        f"; descendants as of {as_of[:10]} kept ({kept})"
        if as_of
        else "; no descendants snapshot"
    )
    return lineage, {
        "section": "lineage",
        "action": "translated",
        "from": (
            "the archive-time card and tags in transfer.json"
            if metadata is not None
            else "relationships and the recorded upstream tags"
        ),
        "now": now,
        "was": None,
        "dropped": [],
    }


def _parents_from_relationships(rel: dict, tags: list[str], prefix: str) -> list[dict]:
    """Parent edges from a 1.x ``relationships`` section, without the card.

    1.x recorded the union of card and tag parents (card first) and one
    relation for the primary edge. The tags are in the record too
    (``source.upstream_tags``), so a parent a tag names gets that tag's
    relation and ``declared_by: tag``; the rest were declared by the
    card. The recorded relation applies to a card-declared parent only
    when it can have come from the card — when it is not simply the one
    relation the tags agree on.
    """
    from .providers.huggingface import parse_base_model_tags

    bases = [b for b in _as_list(rel.get("base_models")) or [] if isinstance(b, str)]
    tag_bases, tag_relations = parse_base_model_tags(tags)
    recorded = rel.get("base_model_relation")
    if not isinstance(recorded, str):
        recorded = None
    distinct = sorted(set(tag_relations.values()))
    tags_alone_explain = len(distinct) == 1 and distinct[0] == recorded
    edges = []
    for repo in bases:
        if repo in tag_bases:
            edges.append(
                {
                    "source": f"{prefix}{repo}",
                    "relation": tag_relations.get(repo) or recorded,
                    "declared_by": "tag",
                }
            )
        else:
            edges.append(
                {
                    "source": f"{prefix}{repo}",
                    "relation": None if tags_alone_explain else recorded,
                    "declared_by": "card",
                }
            )
    for ds in _as_list(rel.get("training_datasets")) or []:
        if isinstance(ds, str) and ds.strip():
            edges.append(
                {
                    "source": f"{prefix}datasets/{ds}",
                    "relation": "trained_on",
                    "declared_by": "card",
                }
            )
    return edges


def _rename_subset_vocabulary(subset: dict | None) -> dict | None:
    """masters → negatives, master → negative, in place. None when nothing to say."""
    if not isinstance(subset, dict):
        return None
    was_policy = subset.get("policy")
    renamed_verdicts = 0
    if was_policy in _POLICY_1X:
        subset["policy"] = _POLICY_1X[was_policy]
    classification = subset.get("classification")
    if isinstance(classification, dict):
        for row in classification.get("sets") or []:
            if isinstance(row, dict) and row.get("verdict") in _VERDICT_1X:
                row["verdict"] = _VERDICT_1X[row["verdict"]]
                renamed_verdicts += 1
        by_verdict = classification.get("verdict_bytes")
        if isinstance(by_verdict, dict):
            for old_word, new_word in _VERDICT_1X.items():
                if old_word in by_verdict:
                    by_verdict[new_word] = by_verdict.pop(old_word)
    if was_policy not in _POLICY_1X and not renamed_verdicts:
        return None
    now = f"policy {subset.get('policy')}"
    if renamed_verdicts:
        now += (
            f"; {renamed_verdicts} verdict{'s' if renamed_verdicts != 1 else ''} "
            "master → negative"
        )
    return {
        "section": "source.subset",
        "action": "renamed",
        "from": "the recorded classification",
        "now": now,
        "was": f"policy {was_policy}" if was_policy in _POLICY_1X else None,
        "dropped": [],
    }


def _source_with_address(source: dict, artifact_type: str) -> dict:
    """The source as recorded; ``provider`` and ``address`` (1.5.0) filled in
    for records that predate them, from the ``origin`` and ``repo_id`` they
    do carry — the canonical spelling of two recorded facts, not a guess."""
    origin = source.get("origin")
    if origin and not source.get("provider"):
        source["provider"] = origin
    repo_id = source.get("repo_id")
    if origin and repo_id and not source.get("address"):
        path = repo_id
        if artifact_type == "dataset" and not path.startswith("datasets/"):
            path = f"datasets/{path}"
        source["address"] = f"{origin}:{path}"
    return source


def _inventory_with_layout(inventory: dict, artifact_type: str) -> dict:
    """The inventory as recorded; ``layout`` filled in for records that predate it."""
    inventory = deepcopy(inventory)
    if not inventory.get("layout"):
        rules = ARTIFACT_TYPES.get(artifact_type) or ARTIFACT_TYPES["model"]
        inventory["layout"] = {
            "payload_root": rules["payload_root"],
            "mutable_metadata": [*BUNDLE_METADATA_FILES, "LICENSE"],
        }
    return inventory


def _as_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value) if isinstance(value, (list, tuple)) else None


# --- labels -------------------------------------------------------------------


def work_label(identity: dict) -> str:
    """``Toy 3.5 · 7B · instruct`` from the name-derived identity, or ``—``."""
    from .lineage import display_generation

    head = display_generation(identity.get("family"), identity.get("generation"))
    tail = [identity.get("member")] if identity.get("member") else []
    tail += list(identity.get("variants") or [])
    tail += list(identity.get("formats") or [])
    if head == "—" and not tail:
        return "—"
    return head + (" · " + " · ".join(tail) if tail else "")


def precision_label(meta: dict) -> str:
    label = meta.get("precision") or "?"
    bpp = meta.get("bytes_per_param")
    return label if bpp is None else f"{label} · {bpp:.2f} bytes/param"


# --- doing it -------------------------------------------------------------------


def print_migration_plan(plan: dict, progress=print, *, dry_run: bool) -> None:
    """The plan as the operator reads it: one line per section, each saying
    what the record now says and where that came from."""
    from .readme_gen import human_size

    head = "Would migrate" if dry_run else "Migrating"
    if plan["status"] == "current":
        progress(
            f"{plan['bundle_id']} is on schema {plan['from_schema']} — this darsay "
            f"reads {MANIFEST_SCHEMA_MAJOR}.x; nothing to migrate"
        )
        return
    progress(
        f"{head} {plan['bundle_id']}  (schema {plan['from_schema']} → {plan['to_schema']})"
    )
    progress(f"  path:       {plan['path']}")
    labels = {"model_metadata": "precision", "source.subset": "subset"}
    for change in plan["changes"]:
        label = labels.get(change["section"], change["section"])
        line = f"  {label + ':':<11} {change['now']} — {change['action']} from {change['from']}"
        if change.get("was"):
            line += f" (was {change['was']})"
        if change.get("dropped"):
            line += "; drops " + " and ".join(change["dropped"])
        progress(line)
    payload = plan["payload"]
    carried = ", ".join(
        f"inventory ({payload['files']} files, {human_size(payload['bytes'])})"
        if name == "inventory"
        else name
        for name in plan["carried"]
    )
    progress(f"  carried:    {carried} — as recorded")
    trouble = len(payload["missing"]) + len(payload["size_mismatch"])
    if trouble or payload["extra"]:
        parts = []
        if payload["missing"]:
            parts.append(f"{len(payload['missing'])} missing")
        if payload["size_mismatch"]:
            parts.append(f"{len(payload['size_mismatch'])} at another size")
        if payload["extra"]:
            parts.append(f"{len(payload['extra'])} not in the record")
        progress(
            f"  payload:    WARNING: of {payload['files']} recorded files, "
            f"{', '.join(parts)} — the record migrates as recorded; "
            "`darsay verify` reports the payload"
        )
    else:
        stands = (
            f" — passed verification at this path on {plan['verified_here']}, "
            "per the record"
            if plan.get("verified_here")
            else ""
        )
        progress(
            f"  payload:    {payload['files']} files present at the recorded sizes; "
            f"bytes untouched, not re-hashed{stands}"
        )
    if plan["ledger"]:
        progress(
            "  ledger:     transfer.json travelled; its archive-time card data was used"
        )


def migrate_bundle(
    bundle_dir: Path, *, progress=print, dry_run: bool = False, plan: dict | None = None
) -> dict:
    """Write the migrated record and regenerate the views that read it.

    Under the bundle's transfer lock: ``manifest.json`` in the current
    schema with ``archive.migrations`` appended, then ``README.md`` and
    ``VERIFICATION.md`` regenerated from it (``curation.md`` is the
    curator's and is not touched). ``dry_run`` prints the plan and writes
    nothing. Returns the plan, with ``written`` set on a real run.
    """
    from .archiver import utc_now, write_manifest

    bundle_dir = Path(bundle_dir)
    plan = migration_plan(bundle_dir) if plan is None else plan
    print_migration_plan(plan, progress, dry_run=dry_run)
    plan["written"] = []
    if dry_run or plan["status"] == "current":
        return plan

    from .hashing import write_sha256sums
    from .readme_gen import write_bundle_readme
    from .transfer import transfer_lock
    from .verify import refresh_verification_md

    record = plan["record"]
    with transfer_lock(bundle_dir, progress=progress):
        now = utc_now()
        archive = record.setdefault("archive", {})
        archive.setdefault("migrations", []).append(
            {
                "at": now,
                "from_schema": plan["from_schema"],
                "to_schema": plan["to_schema"],
                "darsay": __version__,
            }
        )
        archive["last_accessed"] = now
        write_manifest(bundle_dir, record)
        write_bundle_readme(bundle_dir, record)
        write_sha256sums(bundle_dir, record)
        refresh_verification_md(bundle_dir)
    plan["written"] = list(plan["writes"])
    progress(f"Wrote {', '.join(plan['written'])}  (schema {plan['to_schema']})")
    return plan
