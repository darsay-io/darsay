"""``darsay migrate`` — a record moves forward; the payload never moves.

The records under ``tests/fixtures/schema-1.8.0/`` were written by darsay
0.14.10, the last 1.x writer (see ``make.py`` there). The bar this file
holds the verb to: a migrated 1.8.0 record equals what today's
``archive`` writes for the same source, section by section, with only
archive-time facts (timestamps, the tool that downloaded, the descendants
snapshot) differing — and those are carried as recorded, not refreshed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest, read_manifest
from darsay.cli import main
from darsay.migrate import migrate_bundle, migration_plan
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import dataset_files, make_gguf, model_files

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema-1.8.0"
REV = "aaaaaaaaaaaa"
TOY = "test--acme--toy-3.5-7b-instruct"
PLAIN = "test--acme--plain"
ROWS = "test--datasets--acme--rows"
EXPORT = FIXTURES / "exports" / f"{TOY}@{REV}.mvb.tar"


def _fixture_module():
    spec = importlib.util.spec_from_file_location(
        "schema_1_8_0_make", FIXTURES / "make.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAKE = _fixture_module()


def place(vault: Path, name: str, *, ledger: bool = True) -> Path:
    """A 1.8.0 fixture record with its payload rebuilt, as an rsync would leave it."""
    root, files = {
        TOY: ("model", model_files()),
        PLAIN: ("model", model_files()),
        ROWS: ("data", dataset_files()),
    }[name]
    bundle = vault / name / REV
    (bundle / root).mkdir(parents=True)
    for rel, data in files.items():
        (bundle / root / rel).write_bytes(data)
    for path in (FIXTURES / name).iterdir():
        if path.name == "transfer.json" and not ledger:
            continue
        shutil.copy2(path, bundle / path.name)
    assert read_manifest(bundle)["schema_version"] == "1.8.0"
    return bundle


def register_sources(test_provider) -> None:
    """The same three repos the fixtures were archived from."""
    test_provider.add_repo(
        MAKE.MODEL_LOCATOR,
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
        metadata=deepcopy(MAKE.MODEL_METADATA),
    )
    test_provider.add_repo(MAKE.PLAIN_LOCATOR, model_files())
    test_provider.add_repo(
        MAKE.DATASET_LOCATOR.removeprefix("datasets/"),
        dataset_files(),
        metadata=deepcopy(MAKE.DATASET_METADATA),
        artifact_type="dataset",
    )


# Facts of the archive event itself, not of the work: they differ between
# a 2026-09-03 archive by 0.14.10 and a fresh one, and a migration keeps
# the recorded ones.
ARCHIVE_TIME = (
    "source.download_timestamp",
    "source.transfer",
    "source.downloader",
    "source.subset.classification.read",
    "source.subset.classification.classifier",
    "validation.checksum_verification.at",
    "archive",
    "lineage.descendants",
    "lineage.as_of",
    "lineage.query_limit",
)


def _drop(record: dict, dotted: str) -> None:
    node = record
    *parents, leaf = dotted.split(".")
    for key in parents:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(leaf, None)


def comparable(record: dict) -> dict:
    """The record minus archive-time facts and environment-dependent hashes."""
    record = deepcopy(record)
    for dotted in ARCHIVE_TIME:
        _drop(record, dotted)
    for row in record["inventory"]["files"]:
        row.pop("blake3", None)  # an optional extra; presence is environmental
    subset = (record.get("source") or {}).get("subset") or {}
    for row in (subset.get("classification") or {}).get("sets") or []:
        row.pop("reason", None)  # prose the classifier may reword between versions
    return record


def assert_same_record(migrated: dict, fresh: dict) -> None:
    left, right = comparable(migrated), comparable(fresh)
    assert list(left) == list(right), "top-level key order"
    for section in right:
        assert left[section] == right[section], section


# --- the bar: migrated == archived under 2.x ---------------------------------


@pytest.mark.parametrize("ledger", [True, False], ids=["with-ledger", "no-ledger"])
def test_migrated_model_record_equals_a_fresh_archive(tmp_path, test_provider, ledger):
    register_sources(test_provider)
    fresh_vault = tmp_path / "fresh"
    fresh_vault.mkdir()
    fresh = load_manifest(
        archive_quiet(f"test:{MAKE.MODEL_LOCATOR}", vault=fresh_vault)
    )
    assert fresh["schema_version"] == SCHEMA_VERSION

    old_vault = tmp_path / "inbox"
    old_vault.mkdir()
    bundle = place(old_vault, TOY, ledger=ledger)
    fixture = read_manifest(bundle)
    plan = migrate_bundle(bundle, progress=silent)
    assert plan["ledger"] is ledger
    migrated = load_manifest(bundle)

    edges = migrated["lineage"]["parents"]
    assert [(e["source"], e["relation"]) for e in edges] == [
        (e["source"], e["relation"]) for e in fresh["lineage"]["parents"]
    ]
    if ledger:
        assert_same_record(migrated, fresh)
    else:
        # Without the card, a parent both the card and a tag declared is
        # recorded as the tag's — the declaration the record itself holds —
        # where a fresh archive, having seen the card, says card.
        assert [e["declared_by"] for e in edges] == ["tag", "tag", "card"]
        assert [e["declared_by"] for e in fresh["lineage"]["parents"]] == [
            "card",
            "tag",
            "card",
        ]
        left, right = comparable(migrated), comparable(fresh)
        left["lineage"].pop("parents"), right["lineage"].pop("parents")
        assert list(left) == list(right)
        for section in right:
            assert left[section] == right[section], section
    # Archive-time facts are the fixture's, untouched.
    assert migrated["source"]["downloader"]["version"] == "0.14.10"
    assert (
        migrated["source"]["download_timestamp"]
        == fixture["source"]["download_timestamp"]
    )
    rel = fixture["relationships"]
    assert migrated["lineage"]["descendants"] == {
        "quantized": rel["quantized_versions"],
        "gguf": rel["gguf_repos"],
        "finetunes_count": rel["finetunes_count"],
        "adapters_count": rel["adapters_count"],
    }
    assert migrated["lineage"]["as_of"] == rel["ecosystem_snapshot_as_of"]
    assert migrated["archive"]["date_archived"] == fixture["archive"]["date_archived"]
    (move,) = migrated["archive"]["migrations"]
    assert (move["from_schema"], move["to_schema"]) == ("1.8.0", SCHEMA_VERSION)
    # The words changed; the classification did not.
    assert migrated["source"]["subset"]["policy"] == "negatives"
    assert fixture["source"]["subset"]["policy"] == "masters"
    assert [
        s["verdict"] for s in migrated["source"]["subset"]["classification"]["sets"]
    ] == [
        "negative",
        "print",
        "support",
    ]


def test_migrated_plain_model_equals_a_fresh_archive(tmp_path, test_provider):
    register_sources(test_provider)
    fresh_vault = tmp_path / "fresh"
    fresh_vault.mkdir()
    fresh = load_manifest(
        archive_quiet(f"test:{MAKE.PLAIN_LOCATOR}", vault=fresh_vault)
    )
    old_vault = tmp_path / "inbox"
    old_vault.mkdir()
    bundle = place(old_vault, PLAIN)
    migrate_bundle(bundle, progress=silent)
    migrated = load_manifest(bundle)
    assert_same_record(migrated, fresh)
    assert migrated["lineage"]["parents"] is None
    # The curator's file is never touched; the README is regenerated from it.
    assert (bundle / "curation.md").read_text(encoding="utf-8") == "# by hand\n"
    assert "curation.md" in (bundle / "README.md").read_text(encoding="utf-8")


def test_migrated_dataset_record_equals_a_fresh_archive(tmp_path, test_provider):
    register_sources(test_provider)
    fresh_vault = tmp_path / "fresh"
    fresh_vault.mkdir()
    fresh = load_manifest(
        archive_quiet(f"test:{MAKE.DATASET_LOCATOR}", vault=fresh_vault)
    )
    old_vault = tmp_path / "inbox"
    old_vault.mkdir()
    bundle = place(old_vault, ROWS)
    fixture = read_manifest(bundle)
    migrate_bundle(bundle, progress=silent)
    migrated = load_manifest(bundle)
    # The fake provider's lineage() is model-shaped for every type; the
    # migration writes a dataset's lineage the way the Hub provider does.
    left, right = comparable(migrated), comparable(fresh)
    left.pop("lineage"), right.pop("lineage")
    assert list(left) == list(right)
    for section in right:
        assert left[section] == right[section], section
    assert migrated["lineage"]["parents"] == [
        {
            "source": "test:datasets/acme/raw-rows",
            "relation": "derived",
            "declared_by": "card",
        }
    ]
    assert migrated["lineage"]["descendants"] == {
        "models_trained_on": fixture["relationships"]["models_trained_on"]
    }
    assert "dataset_metadata" in migrated and "model_metadata" not in migrated
    assert migrated["dataset_metadata"] == fixture["dataset_metadata"]


# --- the verb --------------------------------------------------------------------


def test_every_verb_refuses_an_older_record_with_the_command_to_paste(vault, capsys):
    bundle = place(vault, PLAIN)
    for argv in (
        ["info", str(bundle)],
        ["verify", str(bundle)],
        ["regen", str(bundle)],
        ["export", str(bundle)],
        ["mv", str(bundle), str(vault.parent)],
    ):
        with pytest.raises(SystemExit) as refused:
            main(["--vault", str(vault), *argv])
        message = str(refused.value)
        assert "manifest schema 1.8.0 predates this darsay (reads 2.x)" in message
        assert f"hint: darsay migrate {bundle}" in message
    assert (
        json.loads((bundle / "manifest.json").read_text())["schema_version"] == "1.8.0"
    )


def test_cli_dry_run_then_migrate_then_verify(vault, capsys):
    bundle = place(vault, TOY)
    before = (bundle / "manifest.json").read_bytes()

    assert main(["--vault", str(vault), "migrate", str(bundle), "-n"]) == 0
    out = capsys.readouterr().out
    assert f"Would migrate {TOY}@{REV}  (schema 1.8.0 → {SCHEMA_VERSION})" in out
    assert "identity:   Toy 3.5 · 7B · instruct — re-read from the name" in out
    assert (
        "precision:  F32 · " in out
        and "re-derived from config.json and 1 safetensors header" in out
    )
    assert "was 'float32', a dtype" in out
    assert (
        "lineage:    parents: finetune test:acme/Toy-3.5-7B (card), quantized test:acme/Toy-3.5-7B-FP8 (tag), trained_on test:datasets/acme/corpus (card)"
        in out
    )
    assert "subset:     policy negatives; 1 verdict master → negative" in out
    assert (
        "carried:    source, licensing, inventory (7 files, 664 B), runtime, validation, archive, security, curation — as recorded"
        in out
    )
    assert (
        "payload:    7 files present at the recorded sizes; bytes untouched, not re-hashed"
        in out
    )
    assert "ledger:     transfer.json travelled" in out
    assert "Dry run: nothing written. To migrate:" in out
    assert f"darsay --vault {vault} migrate {bundle}" in out
    assert (bundle / "manifest.json").read_bytes() == before

    assert main(["--vault", str(vault), "migrate", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert f"Migrating {TOY}@{REV}  (schema 1.8.0 → {SCHEMA_VERSION})" in out
    assert f"Wrote manifest.json, README.md  (schema {SCHEMA_VERSION})" in out
    assert f"next:  darsay verify {TOY}@{REV}" in out
    assert load_manifest(bundle)["identity"]["family"] == "Toy"

    assert main(["--vault", str(vault), "verify", str(bundle)]) == 0
    assert "Verification: PASS" in capsys.readouterr().out
    assert main(["--vault", str(vault), "info", str(bundle)]) == 0
    assert f"schema v{SCHEMA_VERSION}" in capsys.readouterr().out

    assert main(["--vault", str(vault), "migrate", str(bundle)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_cli_migrate_all_walks_the_vault(vault, test_provider, capsys):
    register_sources(test_provider)
    place(vault, PLAIN)
    place(vault, ROWS)
    archive_quiet(f"test:{MAKE.MODEL_LOCATOR}", vault=vault)

    assert main(["--vault", str(vault), "list"]) == 0
    listed = capsys.readouterr().out
    assert "1 have · 0 partial · 2 to migrate (`darsay migrate --all`)" in listed
    assert "schema 1.8.0 · darsay migrate" in listed
    assert main(["--vault", str(vault), "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    by_status = {r["bundle_id"]: r["status"] for r in rows}
    assert by_status == {
        f"{PLAIN}@{REV}": "migrate",
        f"{ROWS}@{REV}": "migrate",
        f"{TOY}@{REV}": "have",
    }
    assert {r["schema_version"] for r in rows} == {"1.8.0", SCHEMA_VERSION}

    assert main(["--vault", str(vault), "migrate", "--all", "-n"]) == 0
    out = capsys.readouterr().out
    assert f"2 of 3 records in {vault} predate this darsay (2.x); 1 is current" in out
    assert out.count("Would migrate ") == 2
    assert "nothing to migrate" not in out  # the current one is counted, not echoed
    assert "Dry run: nothing written. To migrate:" in out

    assert main(["--vault", str(vault), "migrate", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.count("Wrote manifest.json") == 2
    assert f"Migrated 2 records to schema {SCHEMA_VERSION}." in out
    assert f"  darsay verify {PLAIN}@{REV}\n" in out
    assert f"  darsay verify {ROWS}@{REV}\n" in out

    assert main(["--vault", str(vault), "migrate", "--all"]) == 0
    assert "Every record in" in capsys.readouterr().out
    assert main(["--vault", str(vault), "list"]) == 0
    assert "to migrate" not in capsys.readouterr().out


def test_cli_migrate_json(vault, capsys):
    bundle = place(vault, ROWS, ledger=False)
    assert main(["--vault", str(vault), "migrate", str(bundle), "--json", "-n"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == {"reads": "2.x", "writes": SCHEMA_VERSION}
    assert data["dry_run"] is True
    (plan,) = data["bundles"]
    assert plan["status"] == "migrate"
    assert plan["from_schema"] == "1.8.0"
    assert [c["section"] for c in plan["changes"]] == ["identity", "lineage"]
    assert plan["payload"]["files"] == 4
    assert "record" not in plan
    assert read_manifest(bundle)["schema_version"] == "1.8.0"

    assert main(["--vault", str(vault), "migrate", str(bundle), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bundles"][0]["written"] == ["manifest.json", "README.md"]
    assert load_manifest(bundle)["schema_version"] == SCHEMA_VERSION


def test_cli_migrate_needs_a_bundle_or_all(vault):
    with pytest.raises(SystemExit, match="needs a bundle"):
        main(["--vault", str(vault), "migrate"])
    with pytest.raises(SystemExit, match="not both"):
        main(["--vault", str(vault), "migrate", "x", "--all"])


def test_migrate_warns_about_a_payload_that_is_not_all_there(vault, capsys):
    bundle = place(vault, PLAIN, ledger=False)
    (bundle / "model" / "tokenizer.json").unlink()
    (bundle / "model" / "README.md").write_bytes(b"longer than the record says\n")
    assert main(["--vault", str(vault), "migrate", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert (
        "payload:    WARNING: of 7 recorded files, 1 missing, 1 at another size — "
        "the record migrates as recorded; `darsay verify` reports the payload"
    ) in out
    # The inventory is the hash record and was carried, so verify now tells the truth.
    assert main(["--vault", str(vault), "verify", str(bundle)]) == 1
    assert "1 modified, 1 missing" in capsys.readouterr().out


def test_migrate_refuses_a_partial(vault):
    partial = vault / "test--acme--half" / REV
    partial.mkdir(parents=True)
    (partial / "transfer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="partial, not a registered bundle"):
        main(["--vault", str(vault), "migrate", str(partial)])


# --- import ----------------------------------------------------------------------


def test_import_of_an_older_export_verifies_then_migrates(vault, capsys):
    assert main(["--vault", str(vault), "import", str(EXPORT), "-n"]) == 0
    out = capsys.readouterr().out
    assert (
        f"record:      schema 1.8.0 — migrated to {SCHEMA_VERSION} before it registers"
        in out
    )
    assert not list(vault.glob("*/*/manifest.json"))

    assert main(["--vault", str(vault), "import", str(EXPORT)]) == 0
    out = capsys.readouterr().out
    assert "Verifying payload against embedded manifest" in out
    assert f"Migrating {TOY}@{REV}  (schema 1.8.0 → {SCHEMA_VERSION})" in out
    assert "Verification: PASS" in out
    bundle = vault / TOY / REV
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["identity"]["family"] == "Toy"
    assert manifest["source"]["subset"]["policy"] == "negatives"
    (move,) = manifest["archive"]["migrations"]
    assert move["from_schema"] == "1.8.0"
    assert manifest["archive"]["imported"]["from_file"] == str(EXPORT.resolve())
    assert manifest["archive"]["location"] == str(bundle.resolve())
    # A verified copy of the payload the 1.x writer exported.
    assert manifest["validation"]["checksum_verification"]["status"] == "pass"


def test_import_still_refuses_a_newer_record(vault, tmp_path):
    import tarfile

    newer = tmp_path / "newer.mvb.tar"
    with tarfile.open(EXPORT, "r") as src, tarfile.open(newer, "w") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read()
            if member.name.endswith(".mvb.json"):
                marker = json.loads(data)
                marker["schema_version"] = "3.0.0"
                data = json.dumps(marker).encode("utf-8")
                member.size = len(data)
            import io

            dst.addfile(member, io.BytesIO(data))
    with pytest.raises(SystemExit, match="newer than this darsay"):
        main(["--vault", str(vault), "import", str(newer)])


# --- doctor --------------------------------------------------------------------


def test_doctor_reports_an_older_record_with_the_command(vault, capsys):
    bundle = place(vault, PLAIN)
    code = main(["--vault", str(vault), "doctor", "--json"])
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    (finding,) = [f for f in report["findings"] if f["check_id"] == "bundle.manifest"]
    assert finding["severity"] == "warning"
    assert finding["summary"] == (
        "bundle record is schema 1.8.0, which predates this darsay; "
        "`darsay migrate` brings it forward"
    )
    assert finding["recommended_action"].startswith(f"Run `darsay migrate {bundle}`")
    assert finding["auto_fixable"] is False

    migrate_bundle(bundle, progress=silent)
    assert main(["--vault", str(vault), "doctor", "--json"]) == 0


def test_migration_plan_names_the_ledger_only_when_it_travelled(vault):
    with_ledger = migration_plan(place(vault, TOY))
    assert with_ledger["ledger"] is True
    assert "transfer.json" in with_ledger["changes"][2]["from"]
    without = migration_plan(place(vault / "b", TOY, ledger=False))
    assert without["ledger"] is False
    assert (
        without["changes"][2]["from"] == "relationships and the recorded upstream tags"
    )
    # Both readings agree on who the parents are and how; only the record of
    # who declared a parent named by both card and tag differs (see above).
    edges = lambda plan: [  # noqa: E731
        (e["source"], e["relation"]) for e in plan["record"]["lineage"]["parents"]
    ]
    assert edges(with_ledger) == edges(without)
    assert with_ledger["record"]["lineage"]["parents"][0]["declared_by"] == "card"
    assert without["record"]["lineage"]["parents"][0]["declared_by"] == "tag"
