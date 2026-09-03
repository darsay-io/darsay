"""Regenerate the schema-1.8.0 fixtures with the last darsay that wrote 1.x.

The records in this directory were produced by darsay 0.14.10 (git
``c67be59``, manifest schema 1.8.0) archiving the synthetic repos below
through its own ``TestProvider`` — the genuine 1.x writer, not an
imitation. ``tests/integration/test_migrate.py`` holds ``darsay migrate``
to them: a migrated 1.8.0 record must equal what today's ``archive``
writes for the same source, section by section.

To regenerate (only if the fixtures themselves must change):

    git worktree add /tmp/darsay-0.14.10 c67be59
    .venv/bin/python tests/fixtures/schema-1.8.0/make.py /tmp/darsay-0.14.10
    git worktree remove /tmp/darsay-0.14.10

The payloads are ``tests/payloads.py``'s ``model_files`` / ``dataset_files``
of that commit, which today's tests rebuild byte-for-byte; only the
records and one export are frozen here.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_HOST = "darsay-0.14.10-fixture"

# The three sources, mirrored in tests/integration/test_migrate.py.
MODEL_LOCATOR = "acme/Toy-3.5-7B-Instruct"
MODEL_METADATA = {
    "card_data": {
        "license": "apache-2.0",
        "base_model": "acme/Toy-3.5-7B",
        "base_model_relation": "finetune",
        "datasets": ["acme/corpus"],
        "language": ["en", "fr"],
    },
    "tags": [
        "base_model:finetune:acme/Toy-3.5-7B",
        "base_model:quantized:acme/Toy-3.5-7B-FP8",
        "text-generation",
    ],
    "gated": False,
    "created_at": "2026-01-01T00:00:00+00:00",
    "last_modified": "2026-01-01T00:00:00+00:00",
    "downloads": 12,
    "likes": 3,
}
PLAIN_LOCATOR = "acme/plain"
DATASET_LOCATOR = "datasets/acme/rows"
DATASET_METADATA = {
    "card_data": {
        "license": "mit",
        "source_datasets": ["acme/raw-rows"],
        "task_categories": ["text-classification"],
    },
    "tags": ["task_categories:text-classification"],
    "gated": False,
    "created_at": "2026-01-01T00:00:00+00:00",
    "last_modified": "2026-01-01T00:00:00+00:00",
    "downloads": 0,
    "likes": 0,
}


def main(worktree: Path) -> None:
    sys.path.insert(0, str(worktree))
    sys.path.insert(0, str(worktree / "src"))
    import darsay
    from darsay import sources
    from darsay.archiver import archive
    from darsay.export import export_bundle
    from darsay.providers.huggingface import parse_base_model_tags
    from tests.fakes import TestProvider
    from tests.payloads import dataset_files, make_gguf, model_files

    assert darsay.SCHEMA_VERSION == "1.8.0", darsay.SCHEMA_VERSION

    class RecordingProvider(TestProvider):
        """The 0.14.10 fake with the real 1.x Hub ``relationships`` shape."""

        def relationships(self, source, metadata):
            card = metadata.get("card_data") or {}
            tags = list(metadata.get("tags") or [])
            as_list = lambda v: [v] if isinstance(v, str) else (v or None)  # noqa: E731
            if source.artifact_type == "dataset":
                return {
                    "source_datasets": as_list(card.get("source_datasets")),
                    "models_trained_on": ["acme/Toy-3.5-7B-Instruct"],
                    "ecosystem_snapshot_as_of": "2026-01-01T00:00:00+00:00",
                    "query_limit": 100,
                }
            base_models = [
                b for b in (as_list(card.get("base_model")) or []) if isinstance(b, str)
            ]
            tag_bases, tag_relations = parse_base_model_tags(tags)
            for b in tag_bases:
                if b not in base_models:
                    base_models.append(b)
            relation = card.get("base_model_relation")
            if not isinstance(relation, str):
                distinct = sorted(set(tag_relations.values()))
                relation = distinct[0] if len(distinct) == 1 else None
            primary = base_models[0] if base_models else None
            return {
                "base_models": base_models or None,
                "base_model": primary,
                "base_model_relation": relation,
                "finetuned_from": primary if relation == "finetune" else None,
                "training_datasets": as_list(card.get("datasets")),
                "quantized_versions": ["acme/Toy-3.5-7B-Instruct-GGUF"],
                "gguf_repos": ["acme/Toy-3.5-7B-Instruct-GGUF"],
                "finetunes_count": 2,
                "adapters_count": 0,
                "related_variants": None,
                "successors": None,
                "ecosystem_snapshot_as_of": "2026-01-01T00:00:00+00:00",
                "query_limit": 100,
            }

    sources._ensure_providers()
    provider = RecordingProvider()
    sources.register_provider(provider)
    provider.add_repo(
        MODEL_LOCATOR,
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
        metadata=MODEL_METADATA,
    )
    provider.add_repo(PLAIN_LOCATOR, model_files())
    provider.add_repo(
        DATASET_LOCATOR.removeprefix("datasets/"),
        dataset_files(),
        metadata=DATASET_METADATA,
        artifact_type="dataset",
    )

    quiet = lambda *a, **k: None  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        for target in HERE.iterdir():
            if target.is_dir():
                shutil.rmtree(target)
        for locator in (
            f"test:{MODEL_LOCATOR}",
            f"test:{PLAIN_LOCATOR}",
            f"test:{DATASET_LOCATOR}",
        ):
            bundle = archive(locator, vault=vault, progress=quiet, jobs=1)
            manifest_path = bundle / "manifest.json"
            record = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert record["schema_version"] == "1.8.0"
            # Machine-local, not record shape: keep the generating host out
            # of the repository.
            record["archive"]["host"] = FIXTURE_HOST
            manifest_path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            out = HERE / bundle.parent.name
            out.mkdir()
            shutil.copy2(manifest_path, out / "manifest.json")
            if locator.endswith(MODEL_LOCATOR):
                # One record keeps its ledger (archive-time card data travels
                # with an rsync'd bundle); the others test the fallback.
                ledger_path = bundle / "transfer.json"
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                for session in ledger.get("sessions") or []:
                    session["host"] = FIXTURE_HOST
                (out / "transfer.json").write_text(
                    json.dumps(ledger, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                exports = HERE / "exports"
                exports.mkdir()
                export_bundle(bundle, exports, progress=quiet)
            if locator.endswith(PLAIN_LOCATOR):
                (bundle / "curation.md").write_text("# by hand\n", encoding="utf-8")
                shutil.copy2(bundle / "curation.md", out / "curation.md")
    print(f"wrote fixtures under {HERE}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]).resolve())
