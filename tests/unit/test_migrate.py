"""``migrate.py`` — the one reader of an older schema major.

The pieces here are exercised on hand-built records so every branch of
the 1.x reading is pinned; ``tests/integration/test_migrate.py`` holds
the whole verb to records the real 1.8.0 writer produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest, read_manifest, write_manifest
from darsay.migrate import (
    _inventory_with_layout,
    _parents_from_relationships,
    _record_from_1x,
    _rename_subset_vocabulary,
    migrate_bundle,
    migration_hint,
    migration_plan,
    precision_label,
    record_status,
    work_label,
)
from darsay.schema import MANIFEST_KIND
from tests.conftest import silent
from tests.payloads import model_files

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "schema-1.8.0"
    / "test--acme--toy-3.5-7b-instruct"
    / "manifest.json"
)
BUNDLE_ID = "test--acme--toy-3.5-7b-instruct@aaaaaaaaaaaa"


def test_record_status_reads_the_major():
    assert record_status("1.8.0") == "older"
    assert record_status("1.0.0") == "older"
    assert record_status("2.0.0") == "current"
    assert record_status(SCHEMA_VERSION) == "current"
    assert record_status("3.0.0") == "newer"
    with pytest.raises(ValueError):
        record_status("two")


def test_migration_hint_is_a_pasteable_command(tmp_path):
    odd = tmp_path / "a vault" / "x"
    hint = migration_hint(odd)
    assert hint.startswith("  hint: darsay migrate ")
    assert "'" in hint  # the space is quoted
    assert "payload untouched" in hint


# --- parents without the card ------------------------------------------------


def test_parents_named_by_tags_carry_the_tag_relation_and_provenance():
    rel = {
        "base_models": ["acme/base", "acme/other"],
        "base_model_relation": "finetune",
        "training_datasets": ["acme/corpus"],
    }
    tags = ["base_model:finetune:acme/base", "base_model:quantized:acme/other"]
    edges = _parents_from_relationships(rel, tags, "huggingface:")
    assert edges == [
        {
            "source": "huggingface:acme/base",
            "relation": "finetune",
            "declared_by": "tag",
        },
        {
            "source": "huggingface:acme/other",
            "relation": "quantized",
            "declared_by": "tag",
        },
        {
            "source": "huggingface:datasets/acme/corpus",
            "relation": "trained_on",
            "declared_by": "card",
        },
    ]


def test_card_only_parent_keeps_a_relation_the_card_must_have_declared():
    """No tags at all: the recorded relation can only have come from the card."""
    rel = {"base_models": ["acme/base"], "base_model_relation": "merge"}
    edges = _parents_from_relationships(rel, [], "huggingface:")
    assert edges == [
        {"source": "huggingface:acme/base", "relation": "merge", "declared_by": "card"}
    ]


def test_card_only_parent_gets_no_relation_when_tags_alone_explain_it():
    """1.x fell back to the one relation the tags agreed on; a parent the tags
    do not name cannot be given that relation without fabricating."""
    rel = {
        "base_models": ["acme/from-card", "acme/from-tag"],
        "base_model_relation": "quantized",
    }
    tags = ["base_model:quantized:acme/from-tag"]
    edges = _parents_from_relationships(rel, tags, "huggingface:")
    assert edges[0] == {
        "source": "huggingface:acme/from-card",
        "relation": None,
        "declared_by": "card",
    }
    assert edges[1]["relation"] == "quantized"
    assert edges[1]["declared_by"] == "tag"


def test_no_parents_is_an_empty_list():
    assert _parents_from_relationships({}, [], "huggingface:") == []
    assert (
        _parents_from_relationships(
            {"base_models": None, "training_datasets": None}, [], "huggingface:"
        )
        == []
    )


# --- vocabulary -----------------------------------------------------------------


def test_subset_vocabulary_is_renamed_in_place():
    subset = {
        "include": ["model.safetensors"],
        "policy": "masters",
        "classification": {
            "sets": [
                {"name": "a", "verdict": "master"},
                {"name": "b", "verdict": "print"},
                {"name": "c", "verdict": "master"},
            ],
            "verdict_bytes": {"master": 10, "print": 3, "support": 1, "unknown": 0},
        },
    }
    change = _rename_subset_vocabulary(subset)
    assert subset["policy"] == "negatives"
    assert [s["verdict"] for s in subset["classification"]["sets"]] == [
        "negative",
        "print",
        "negative",
    ]
    assert subset["classification"]["verdict_bytes"] == {
        "print": 3,
        "support": 1,
        "unknown": 0,
        "negative": 10,
    }
    assert change["section"] == "source.subset"
    assert "2 verdicts master → negative" in change["now"]
    assert change["was"] == "policy masters"


def test_subset_without_old_words_is_left_alone():
    assert _rename_subset_vocabulary(None) is None
    explicit = {"include": ["*.gguf"], "policy": None}
    assert _rename_subset_vocabulary(explicit) is None
    assert explicit == {"include": ["*.gguf"], "policy": None}
    already = {
        "policy": "negatives",
        "classification": {"sets": [{"verdict": "negative"}]},
    }
    assert _rename_subset_vocabulary(already) is None


# --- layout and labels ---------------------------------------------------------


def test_inventory_layout_is_filled_for_records_that_predate_it():
    filled = _inventory_with_layout({"file_count": 1, "files": []}, "dataset")
    assert filled["layout"]["payload_root"] == "data/"
    assert "manifest.json" in filled["layout"]["mutable_metadata"]
    kept = _inventory_with_layout(
        {"layout": {"payload_root": "model/", "mutable_metadata": ["x"]}}, "model"
    )
    assert kept["layout"] == {"payload_root": "model/", "mutable_metadata": ["x"]}


def test_work_label_reads_like_the_readme():
    assert work_label({"family": "Qwen", "generation": "3.8", "member": "27B"}) == (
        "Qwen 3.8 · 27B"
    )
    assert work_label({"family": "Kimi", "generation": "K3", "variants": ["base"]}) == (
        "Kimi K3 · base"
    )
    assert work_label({"family": None, "generation": None}) == "—"
    assert work_label({"family": "plain"}) == "plain"


def test_precision_label():
    assert precision_label({"precision": "BF16", "bytes_per_param": 2.0}) == (
        "BF16 · 2.00 bytes/param"
    )
    assert precision_label({"precision": "Q4_K_M", "bytes_per_param": None}) == "Q4_K_M"
    assert precision_label({}) == "?"


# --- a record older than every additive 1.x field -----------------------------


def _oldest_shaped_record() -> dict:
    """The 1.8.0 fixture record stripped back to what a 1.0-era record carried:
    no kind or artifact_type, no layout, no provider, address, transfer, or
    subset, a thin relationships section — plus a curator's edits and an
    unknown top-level key, which must both survive."""
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record["schema_version"] = "1.0.0"
    for key in ("kind", "artifact_type"):
        del record[key]
    for key in ("provider", "address", "transfer", "mirrors_used", "access", "subset"):
        del record["source"][key]
    del record["inventory"]["layout"]
    record["identity"]["version"] = "3.5"
    record["model_metadata"]["training_cutoff"] = "2025-01"
    record["runtime"]["tested_hardware"] = [{"host": "h", "tokens_per_second": 3}]
    record["relationships"] = {"finetuned_from": "acme/Toy-3.5-7B"}
    record["x-vault-notes"] = {"kept": True}
    return record


def _bundle_with(tmp_path, record: dict):
    bundle = tmp_path / "acme--toy" / "aaaaaaaaaaaa"
    (bundle / "model").mkdir(parents=True)
    for name, data in model_files().items():
        (bundle / "model" / name).write_bytes(data)
    (bundle / "manifest.json").write_text(json.dumps(record), encoding="utf-8")
    return bundle


def test_oldest_record_reads_forward_with_nothing_fabricated(tmp_path):
    bundle = _bundle_with(tmp_path, _oldest_shaped_record())
    old = read_manifest(bundle)
    record, changes, carried = _record_from_1x(old, bundle_dir=bundle, metadata=None)

    assert record["schema_version"] == SCHEMA_VERSION
    assert record["kind"] == MANIFEST_KIND
    assert record["artifact_type"] == "model"
    ident = record["identity"]
    assert (ident["family"], ident["generation"], ident["member"]) == (
        "Toy",
        "3.5",
        "7B",
    )
    assert ident["variants"] == ["instruct"]
    assert ident["read_from"] == "name"
    assert "version" not in ident
    assert record["inventory"]["layout"]["payload_root"] == "model/"
    assert record["inventory"]["files"] == old["inventory"]["files"]
    assert record["source"]["provider"] == "test"
    assert record["source"]["address"] == "test:acme/Toy-3.5-7B-Instruct"
    assert record["source"]["repo_id"] == old["source"]["repo_id"]
    meta = record["model_metadata"]
    assert meta["precision"] == "F32"
    assert meta["bytes_per_param"] is not None
    assert meta["parameter_count"] == 8
    assert meta["languages"] == ["en", "fr"]
    assert meta["training_cutoff"] == "2025-01"
    assert record["runtime"]["tested_hardware"] == [
        {"host": "h", "tokens_per_second": 3}
    ]
    assert record["lineage"] == {
        "parents": None,
        "descendants": {
            "quantized": None,
            "gguf": None,
            "finetunes_count": None,
            "adapters_count": None,
        },
        "successors": None,
        "related": None,
        "as_of": None,
        "query_limit": None,
    }
    assert "relationships" not in record
    assert record["x-vault-notes"] == {"kept": True}
    assert [c["section"] for c in changes] == ["identity", "model_metadata", "lineage"]
    assert changes[0]["dropped"][0].startswith("version '3.5'")
    assert "config.json" in changes[0]["dropped"][1]
    assert carried == [
        "source",
        "licensing",
        "inventory",
        "runtime",
        "validation",
        "archive",
        "security",
        "curation",
    ]

    write_manifest(bundle, record)
    assert load_manifest(bundle)["identity"]["family"] == "Toy"


def test_migration_plan_states_what_would_be_written(tmp_path):
    bundle = _bundle_with(tmp_path, _oldest_shaped_record())
    plan = migration_plan(bundle)
    assert plan["status"] == "migrate"
    assert (plan["from_schema"], plan["to_schema"]) == ("1.0.0", SCHEMA_VERSION)
    assert plan["payload"]["files"] == 7
    assert plan["payload"]["missing"] == []
    assert plan["writes"] == ["manifest.json", "README.md", "SHA256SUMS"]
    assert plan["ledger"] is False


def test_migration_plan_refuses_a_newer_major_and_a_partial(tmp_path):
    newer = _oldest_shaped_record()
    newer["schema_version"] = "3.0.0"
    bundle = _bundle_with(tmp_path, newer)
    with pytest.raises(SystemExit, match="newer than this darsay"):
        migration_plan(bundle)

    partial = tmp_path / "acme--other" / "bbbbbbbbbbbb"
    partial.mkdir(parents=True)
    (partial / "transfer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="partial, not a registered bundle"):
        migration_plan(partial)
    with pytest.raises(SystemExit, match="not a darsay bundle"):
        migration_plan(tmp_path)


def test_current_record_is_nothing_to_do(tmp_path):
    current = _oldest_shaped_record()
    current["schema_version"] = SCHEMA_VERSION
    bundle = _bundle_with(tmp_path, current)
    plan = migration_plan(bundle)
    assert plan["status"] == "current"
    lines = []
    migrate_bundle(bundle, progress=lines.append)
    assert lines == [
        f"{BUNDLE_ID} is on schema {SCHEMA_VERSION} — this darsay reads "
        "2.x; nothing to migrate"
    ]
    assert json.loads((bundle / "manifest.json").read_text()) == current


def test_dry_run_writes_nothing_and_a_real_run_records_the_move(tmp_path):
    bundle = _bundle_with(tmp_path, _oldest_shaped_record())
    before = (bundle / "manifest.json").read_bytes()
    lines = []
    migrate_bundle(bundle, progress=lines.append, dry_run=True)
    assert lines[0].startswith(f"Would migrate {BUNDLE_ID}  (schema 1.0.0 → ")
    assert (bundle / "manifest.json").read_bytes() == before
    assert not (bundle / "README.md").exists()

    plan = migrate_bundle(bundle, progress=silent)
    assert plan["written"] == ["manifest.json", "README.md", "SHA256SUMS"]
    manifest = load_manifest(bundle)
    (entry,) = manifest["archive"]["migrations"]
    assert (entry["from_schema"], entry["to_schema"]) == ("1.0.0", SCHEMA_VERSION)
    assert entry["darsay"]
    assert (bundle / "README.md").is_file()
    assert not (bundle / "transfer.lock").exists()
