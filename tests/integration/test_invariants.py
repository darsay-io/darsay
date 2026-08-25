from __future__ import annotations

import json

from darsay.archiver import load_manifest
from darsay.hashing import hash_file
from darsay.readme_gen import write_bundle_readme
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_payload_immutability_across_tool_writes(vault, test_provider):
    """Nothing under model/ changes when metadata files are rewritten."""
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    payload_hashes = {
        p.relative_to(bundle).as_posix(): hash_file(p, with_blake3=False)["sha256"]
        for p in (bundle / "model").rglob("*")
        if p.is_file()
    }
    from darsay.verify import verify_bundle
    from tests.conftest import silent

    verify_bundle(bundle, progress=silent)
    write_bundle_readme(bundle, load_manifest(bundle))
    (bundle / "hydration.json").write_text("{}\n")
    after = {
        p.relative_to(bundle).as_posix(): hash_file(p, with_blake3=False)["sha256"]
        for p in (bundle / "model").rglob("*")
        if p.is_file()
    }
    assert after == payload_hashes


def test_manifest_records_established_facts_only(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest = load_manifest(bundle)
    # Record, don't fabricate.
    assert manifest["model_metadata"]["training_cutoff"] is None
    assert manifest["source"]["signatures"] is None
    assert manifest["curation"]["historical_significance"] is None
    assert manifest["runtime"]["tested_hardware"] is None
    # Query caps are explicit.
    assert manifest["relationships"]["query_limit"] == 100


def test_ledger_has_no_host_absolute_paths(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    # After a complete archive the ledger may still be present as acceleration
    # state; if so, it must be relocatable.
    ledger_path = bundle / "transfer.json"
    if ledger_path.is_file():
        dumped = ledger_path.read_text()
        assert str(vault.resolve()) not in dumped
        assert str(bundle.resolve()) not in dumped
        ledger = json.loads(dumped)
        assert all(not item["path"].startswith("/") for item in ledger["expected"])
