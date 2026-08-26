from __future__ import annotations

import pytest

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest
from darsay.estimate import estimate
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import dataset_files, model_files


def test_archive_model_writes_immutable_payload_and_manifest(vault, test_provider):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    bundle = archive_quiet("test:acme/toy", vault=vault)
    assert bundle is not None
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["kind"] == "darsay.bundle"
    assert manifest["artifact_type"] == "model"
    assert manifest["source"]["provider"] == "test"
    assert manifest["source"]["address"] == "test:acme/toy"
    assert manifest["identity"]["publisher"] == "acme"
    assert (bundle / "model" / "model.safetensors").is_file()
    assert (bundle / "README.md").is_file()
    assert (bundle / "curation.md").is_file()
    assert (bundle / "VERIFICATION.md").is_file()
    assert (bundle / "LICENSE").is_file()
    # Payload bytes match the catalog exactly.
    for name, data in files.items():
        assert (bundle / "model" / name).read_bytes() == data
    # Parameter count came from the safetensors header, not a guess.
    assert manifest["model_metadata"]["parameter_count"] == 8
    # Unknown facts stay null.
    assert manifest["model_metadata"]["training_cutoff"] is None
    assert manifest["curation"]["historical_significance"] is None
    # Query caps are recorded, never silently truncated.
    assert manifest["relationships"]["query_limit"] == 100
    assert manifest["security"]["integrity_status"] == "verified-against-upstream"


def test_archive_dataset_uses_data_payload_root(vault, test_provider):
    test_provider.add_repo(
        "acme/reviews",
        dataset_files(),
        pipeline_tag=None,
        license_id="mit",
        metadata={
            "card_data": {"license": "mit", "language": "en"},
            "tags": [],
            "gated": False,
        },
    )
    bundle = archive_quiet("test:datasets/acme/reviews", vault=vault)
    manifest = load_manifest(bundle)
    assert manifest["artifact_type"] == "dataset"
    assert manifest["inventory"]["layout"]["payload_root"] == "data/"
    assert (bundle / "data" / "train.jsonl").is_file()
    assert manifest["dataset_metadata"]["declared"]["example_count_total"] == 2


def test_archive_refuses_to_overwrite_without_force(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    with pytest.raises(SystemExit, match="already exists") as exc:
        archive_quiet("test:acme/toy", vault=vault)
    assert "darsay info" in str(exc.value)
    assert "--force re-pins" in str(exc.value)


def test_archive_dry_run_does_not_register(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    result = archive_quiet("test:acme/toy", vault=vault, dry_run=True)
    assert result is None
    # Ledger may exist from pin, but no manifest.
    matches = list(vault.glob("*/*/manifest.json"))
    assert matches == []


def test_estimate_is_read_only(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    est = estimate("test:acme/toy", vault=vault, progress=silent)
    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert est["artifact_type"] == "model"
    assert est["payload"]["file_count"] == len(model_files())
    assert est["source"]["address"] == "test:acme/toy"
    assert "transformers" in est["engines"]
    assert est["completeness"]["status"] == "complete"


def test_estimate_include_subset(vault, test_provider):
    files = model_files(extra={"extra.bin": b"xxxx"})
    test_provider.add_repo("acme/toy", files)
    est = estimate(
        "test:acme/toy",
        vault=vault,
        include=["*.safetensors"],
        progress=silent,
    )
    assert est["subset"]["include"] == ["*.safetensors"]
    assert est["subset"]["full_file_count"] == len(files)
    assert est["subset"]["kept_file_count"] == est["payload"]["file_count"]
    assert est["payload"]["file_count"] < len(files)
    assert est["payload"]["file_count"] > 1  # matched weights plus sidecars
    full_paths = {item["path"] for item in est["subset"]["full_files"]}
    assert "extra.bin" in full_paths
    assert "model.safetensors" in full_paths
    assert "config.json" in full_paths


def test_archive_include_records_subset_and_omits_unmatched(vault, test_provider):
    files = model_files(
        extra={
            "Q4_K_M.gguf": b"gguf-quant",
            "Q8_0.gguf": b"gguf-big",
        }
    )
    test_provider.add_repo("acme/pack", files)
    bundle = archive_quiet("test:acme/pack", vault=vault, include=["*Q4_K_M*"])
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == SCHEMA_VERSION
    subset = manifest["source"]["subset"]
    assert subset["include"] == ["*Q4_K_M*"]
    assert subset["sidecars"] is True
    assert subset["omitted_file_count"] >= 1
    assert "Q8_0.gguf" in {item["path"] for item in subset["full_files"]}
    readme = (bundle / "README.md").read_text()
    assert "Subset:" in readme
    assert "*Q4_K_M*" in readme
    inventory = {item["path"] for item in manifest["inventory"]["files"]}
    assert "model/Q4_K_M.gguf" in inventory
    assert "model/Q8_0.gguf" not in inventory
    assert "model/config.json" in inventory
    assert "model/LICENSE" in inventory
    assert not (bundle / "model" / "Q8_0.gguf").exists()
    assert (bundle / "model" / "Q4_K_M.gguf").read_bytes() == b"gguf-quant"


def test_archive_include_no_match_exits(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    with pytest.raises(SystemExit, match="matched no payload files"):
        archive_quiet("test:acme/toy", vault=vault, include=["*nope*"])


def test_estimate_records_variant_query_limit(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    est = estimate("test:acme/toy", vault=vault, variants=True, progress=silent)
    assert est["variants"]["query_limit"] == 10


def test_gated_archive_writes_nothing(vault, test_provider):
    test_provider.add_repo("acme/secret", model_files(), gated=True, access_denied=True)
    with pytest.raises(SystemExit, match="requires authorization"):
        archive_quiet("test:acme/secret", vault=vault)
    assert list(vault.glob("*")) == []


def test_curation_not_overwritten_on_force(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    curation = bundle / "curation.md"
    curation.write_text(
        "# Curation notes — kept\n\n## Historical significance\n\nHand edited.\n"
    )
    archive_quiet("test:acme/toy", vault=vault, force=True)
    assert "Hand edited." in curation.read_text()
