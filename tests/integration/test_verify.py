from __future__ import annotations

import json

from darsay.archiver import load_manifest
from darsay.hashing import hash_file
from darsay.verify import verify_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_verify_pass_does_not_touch_payload(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    before = {
        path: hash_file(path, with_blake3=False)["sha256"]
        for path in (bundle / "model").rglob("*")
        if path.is_file()
    }
    report = verify_bundle(bundle, progress=silent)
    assert report["result"] == "pass"
    after = {
        path: hash_file(path, with_blake3=False)["sha256"]
        for path in (bundle / "model").rglob("*")
        if path.is_file()
    }
    assert after == before


def test_verify_detects_modified_missing_extra(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    (bundle / "model" / "model.safetensors").write_bytes(b"tampered")
    (bundle / "model" / "config.json").unlink()
    (bundle / "model" / "extra.bin").write_bytes(b"new")
    report = verify_bundle(bundle, progress=silent)
    assert report["result"] == "fail"
    checksum = report["checksum"]
    assert any("model.safetensors" in p for p in checksum["mismatched"])
    assert any("config.json" in p for p in checksum["missing"])
    assert any("extra.bin" in p for p in checksum["extra"])
    manifest = load_manifest(bundle)
    assert manifest["security"]["integrity_status"] == "compromised"
    history = json.loads((bundle / "verification.json").read_text())
    assert history["latest"]["result"] == "fail"
    assert len(history["history"]) >= 2  # initial archive + this re-verify
